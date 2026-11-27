"""KartOps AI - AWS infrastructure (CDK, Python).

Ingress (API GW / SES->S3 / SQS) -> worker Lambda (LangGraph) -> Bedrock + Guardrails + KB + Textract
 -> DynamoDB (data, ops/audit/queues, LangGraph checkpoints) -> Step Functions payment-hold workflow.
"""
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

ROOT = str(Path(__file__).resolve().parents[1])
QUEUES = ["payments_refunds", "support_l2", "fraud_risk", "legal_compliance", "seller_support",
          "ap_review", "supply_chain", "human_triage"]


class KartOpsStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw):
        super().__init__(scope, cid, **kw)
        ctx = self.node.try_get_context
        retain = RemovalPolicy.RETAIN if ctx("retain_data") in (True, "true") else RemovalPolicy.DESTROY
        model_fast, model_strong = ctx("model_fast"), ctx("model_strong")
        if not (model_fast and model_strong):
            raise ValueError("Pass Bedrock model/inference-profile IDs: -c model_fast=... -c model_strong=...")
        kb_id = ctx("kb_id") or ""

        key = kms.Key(self, "Key", enable_key_rotation=True, alias="alias/kartops", removal_policy=retain)

        # ------------------------------------------------------------- storage
        def table(name, pk="pk", sk="sk", policy=retain):
            return ddb.Table(self, name, partition_key=ddb.Attribute(name=pk, type=ddb.AttributeType.STRING),
                             sort_key=ddb.Attribute(name=sk, type=ddb.AttributeType.STRING),
                             billing_mode=ddb.BillingMode.PAY_PER_REQUEST, removal_policy=policy,
                             point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
                             encryption=ddb.TableEncryption.CUSTOMER_MANAGED, encryption_key=key)

        data_t = table("DataTable")
        ops_t = table("OpsTable")
        ops_t.add_global_secondary_index(index_name="gsi1",
                                         partition_key=ddb.Attribute(name="gsi1pk", type=ddb.AttributeType.STRING),
                                         sort_key=ddb.Attribute(name="gsi1sk", type=ddb.AttributeType.STRING))
        ckpt_t = ddb.Table(self, "CheckpointTable",  # key names required by langgraph-checkpoint-aws
                           partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
                           sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
                           billing_mode=ddb.BillingMode.PAY_PER_REQUEST, time_to_live_attribute="ttl",
                           removal_policy=RemovalPolicy.DESTROY, encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
                           encryption_key=key)

        bucket = s3.Bucket(self, "Bucket", encryption=s3.BucketEncryption.KMS, encryption_key=key,
                           block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, versioned=True,
                           removal_policy=retain, auto_delete_objects=retain == RemovalPolicy.DESTROY,
                           lifecycle_rules=[s3.LifecycleRule(prefix="inbound-email/", expiration=Duration.days(90)),
                                            s3.LifecycleRule(prefix="textract/", expiration=Duration.days(90))])

        # ------------------------------------------------------------- queue
        dlq = sqs.Queue(self, "Dlq", encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                        retention_period=Duration.days(14))
        queue = sqs.Queue(self, "IngestQueue", encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                          visibility_timeout=Duration.minutes(30),
                          dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq))

        # ------------------------------------------------------------- guardrail
        pii = ["PHONE", "EMAIL", "CREDIT_DEBIT_CARD_NUMBER", "INTERNATIONAL_BANK_ACCOUNT_NUMBER"]
        rx = bedrock.CfnGuardrail.RegexConfigProperty  # India-specific identifiers have no built-in entity type
        gr = bedrock.CfnGuardrail(
            self, "Guardrail", name="kartops-guardrail",
            blocked_input_messaging="This request cannot be processed automatically.",
            blocked_outputs_messaging="This response was blocked by policy and sent for human review.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=[
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="HATE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="INSULTS", input_strength="NONE", output_strength="HIGH"),
            ]),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[bedrock.CfnGuardrail.PiiEntityConfigProperty(type=t, action="ANONYMIZE") for t in pii],
                regexes_config=[
                    rx(name="aadhaar", description="Aadhaar number", pattern=r"\b\d{4}\s\d{4}\s\d{4}\b", action="ANONYMIZE"),
                    rx(name="pan", description="PAN", pattern=r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", action="ANONYMIZE")]),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(topics_config=[
                bedrock.CfnGuardrail.TopicConfigProperty(
                    name="legal_advice", type="DENY",
                    definition="Giving legal advice or opinions on legal rights, liability or lawsuits.",
                    examples=["Can I sue you for this?", "Is the company legally liable?"]),
                bedrock.CfnGuardrail.TopicConfigProperty(
                    name="medical_advice", type="DENY",
                    definition="Giving medical diagnosis or treatment advice.",
                    examples=["What medicine should I take for this burn?"])]),
            contextual_grounding_policy_config=bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(filters_config=[
                bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(type="GROUNDING", threshold=0.75),
                bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(type="RELEVANCE", threshold=0.6)]))
        gr_ver = bedrock.CfnGuardrailVersion(self, "GuardrailV1", guardrail_identifier=gr.attr_guardrail_id)

        # ------------------------------------------------------------- payment-hold workflow
        topic = sns.Topic(self, "Alerts", display_name="KartOps alerts")
        if ctx("alert_email"):
            topic.add_subscription(subs.EmailSubscription(ctx("alert_email")))
        record = tasks.DynamoPutItem(
            self, "RecordHold", table=ops_t, result_path=sfn.JsonPath.DISCARD,
            item={"pk": tasks.DynamoAttributeValue.from_string(sfn.JsonPath.format("ITEM#{}", sfn.JsonPath.string_at("$.item_id"))),
                  "sk": tasks.DynamoAttributeValue.from_string("HOLD_WORKFLOW"),
                  "reason": tasks.DynamoAttributeValue.from_string(sfn.JsonPath.string_at("$.reason")),
                  "severity": tasks.DynamoAttributeValue.from_string(sfn.JsonPath.string_at("$.severity"))})
        notify = tasks.SnsPublish(self, "NotifyAP", topic=topic, subject="KartOps: payment held for review",
                                  message=sfn.TaskInput.from_json_path_at("$"))
        sm = sfn.StateMachine(self, "PaymentHold", definition_body=sfn.DefinitionBody.from_chainable(record.next(notify)),
                              timeout=Duration.minutes(5), tracing_enabled=True)

        # ------------------------------------------------------------- lambdas (one image, three handlers)
        env = {
            "KARTOPS_MODE": "aws", "MODEL_FAST": model_fast, "MODEL_STRONG": model_strong,
            "GUARDRAIL_ID": gr.attr_guardrail_id, "GUARDRAIL_VERSION": gr_ver.attr_version,
            "KB_ID": kb_id, "DATA_TABLE": data_t.table_name, "OPS_TABLE": ops_t.table_name,
            "CHECKPOINT_TABLE": ckpt_t.table_name, "BUCKET": bucket.bucket_name, "INGEST_QUEUE_URL": queue.queue_url,
            "PAYMENT_HOLD_SFN_ARN": sm.state_machine_arn, "PLATFORM_NAME": ctx("platform_name") or "KartMart",
        }

        def fn(name, handler, timeout, mem):
            return _lambda.DockerImageFunction(
                self, name,
                code=_lambda.DockerImageCode.from_image_asset(ROOT, file="Dockerfile", cmd=[handler],
                                                              platform=ecr_assets.Platform.LINUX_AMD64),
                architecture=_lambda.Architecture.X86_64, timeout=Duration.seconds(timeout), memory_size=mem,
                environment=env, tracing=_lambda.Tracing.ACTIVE)

        worker = fn("Worker", "kartops.handlers.worker_handler", 300, 1536)
        api_fn = fn("ApiFn", "kartops.handlers.api_handler", 60, 1536)
        mail_fn = fn("EmailIngest", "kartops.handlers.email_ingest_handler", 60, 512)

        # permissions -----------------------------------------------------------
        for f in (worker, api_fn):  # api needs the graph to resume interrupted items
            data_t.grant_read_data(f)
            ops_t.grant_read_write_data(f)
            ckpt_t.grant_read_write_data(f)
            bucket.grant_read_write(f)
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["arn:aws:bedrock:*::foundation-model/*",
                           f"arn:aws:bedrock:*:{self.account}:inference-profile/*"]))
            f.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[gr.attr_guardrail_arn]))
            f.add_to_role_policy(iam.PolicyStatement(actions=["textract:AnalyzeExpense", "textract:StartExpenseAnalysis",
                                                              "textract:GetExpenseAnalysis", "comprehend:DetectDominantLanguage"],
                                                     resources=["*"]))
            if kb_id:
                f.add_to_role_policy(iam.PolicyStatement(
                    actions=["bedrock:Retrieve"],
                    resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id}"]))
            sm.grant_start_execution(f)
        worker.add_event_source(sources.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True,
                                                       max_concurrency=10))
        queue.grant_send_messages(api_fn)
        queue.grant_send_messages(mail_fn)
        data_t.grant_read_data(mail_fn)
        bucket.grant_read_write(mail_fn)
        bucket.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(mail_fn),
                                      s3.NotificationKeyFilter(prefix="inbound-email/"))

        # ------------------------------------------------------------- API (IAM-authenticated)
        api = apigw.RestApi(self, "ReviewApi", rest_api_name="kartops-review-api",
                            default_method_options=apigw.MethodOptions(authorization_type=apigw.AuthorizationType.IAM),
                            deploy_options=apigw.StageOptions(stage_name="v1", throttling_rate_limit=20, throttling_burst_limit=40,
                                                              tracing_enabled=True))
        integ = apigw.LambdaIntegration(api_fn)
        items = api.root.add_resource("items")
        items.add_method("POST", integ)
        item = items.add_resource("{item_id}")
        item.add_method("GET", integ)
        item.add_resource("decision").add_method("POST", integ)
        api.root.add_resource("queues").add_resource("{queue}").add_method("GET", integ)

        # ------------------------------------------------------------- alarms
        alarm_action = cw_actions.SnsAction(topic)
        a1 = cw.Alarm(self, "DlqAlarm", metric=dlq.metric_approximate_number_of_messages_visible(),
                      threshold=1, evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD)
        a2 = cw.Alarm(self, "WorkerErrors", metric=worker.metric_errors(period=Duration.minutes(5)), threshold=3,
                      evaluation_periods=1)
        for a in (a1, a2):
            a.add_alarm_action(alarm_action)

        # ------------------------------------------------------------- outputs
        CfnOutput(self, "OutApiUrl", value=api.url)
        CfnOutput(self, "OutIngestQueueUrl", value=queue.queue_url)
        CfnOutput(self, "OutBucketName", value=bucket.bucket_name)
        CfnOutput(self, "OutDataTable", value=data_t.table_name)
        CfnOutput(self, "OutOpsTable", value=ops_t.table_name)
        CfnOutput(self, "OutGuardrailId", value=gr.attr_guardrail_id)
        CfnOutput(self, "OutAlertsTopic", value=topic.topic_arn)
