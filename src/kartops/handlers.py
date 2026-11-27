"""AWS Lambda entry points.
  worker_handler        SQS -> run the graph (partial-batch-failure aware)
  api_handler           API Gateway (IAM auth): submit items, list queues, record human decisions
  email_ingest_handler  S3 (SES inbound mail) -> parse -> enqueue
"""
from __future__ import annotations

import email
import hashlib
import json
import logging
import uuid
from email import policy

from .config import get_settings
from .deps import Deps
from .runner import Runner

log = logging.getLogger("kartops.handlers")
logging.getLogger().setLevel(logging.INFO)

_runner: Runner | None = None
_deps: Deps | None = None


def deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps.build(get_settings())
    return _deps


def runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner(deps())
    return _runner


def _sqs():
    import boto3
    return boto3.client("sqs", region_name=get_settings().region)


def enqueue(msg: dict) -> str:
    msg.setdefault("item_id", uuid.uuid4().hex)
    _sqs().send_message(QueueUrl=get_settings().queue_url, MessageBody=json.dumps(msg))
    return msg["item_id"]


# ------------------------------------------------------------------------------ SQS worker
def worker_handler(event, context):
    failures = []
    for rec in event.get("Records", []):
        try:
            msg = json.loads(rec["body"])
            out = runner().process(msg)
            log.info("processed %s -> %s", msg.get("item_id"), out.get("status"))
        except Exception:
            log.exception("failed record %s", rec.get("messageId"))
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


# ------------------------------------------------------------------------------ REST API
def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


QUEUES = {"payments_refunds", "support_l2", "fraud_risk", "legal_compliance", "seller_support",
          "ap_review", "supply_chain", "human_triage"}


def api_handler(event, context):
    method, res = event.get("httpMethod"), event.get("resource")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    caller = ((event.get("requestContext") or {}).get("identity") or {}).get("userArn", "unknown")
    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})

    if method == "POST" and res == "/items":
        if body.get("channel") not in ("ticket", "invoice_doc", "email"):
            return _resp(400, {"error": "channel must be ticket|invoice_doc|email"})
        msg = {k: body.get(k) for k in ("item_id", "channel", "raw_input", "attachments", "sender_id") if body.get(k)}
        msg.setdefault("raw_input", "")
        return _resp(202, {"item_id": enqueue(msg)})

    if method == "GET" and res == "/queues/{queue}":
        q = params.get("queue")
        if q not in QUEUES:
            return _resp(404, {"error": "unknown queue"})
        return _resp(200, {"queue": q, "items": deps().ops.list_queue(q, qs.get("status", "PENDING"))})

    if method == "GET" and res == "/items/{item_id}":
        item = deps().ops.get_item(params["item_id"])
        return _resp(200, item) if item else _resp(404, {"error": "not found"})

    if method == "POST" and res == "/items/{item_id}/decision":
        if body.get("action") not in ("approve", "edit", "reject"):
            return _resp(400, {"error": "action must be approve|edit|reject"})
        decision = {"action": body["action"], "notes": str(body.get("notes", ""))[:2000],
                    "edited_draft": body.get("edited_draft"), "reviewer": caller}  # reviewer = IAM identity, not user input
        try:
            return _resp(200, runner().resume(params["item_id"], decision))
        except ValueError as e:
            return _resp(409, {"error": str(e)})

    return _resp(404, {"error": "no such route"})


# ------------------------------------------------------------------------------ inbound email
def email_ingest_handler(event, context):
    import boto3
    s = get_settings()
    s3 = boto3.client("s3", region_name=s.region)
    for rec in event.get("Records", []):
        bucket, key = rec["s3"]["bucket"]["name"], rec["s3"]["object"]["key"]
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        msg = email.message_from_bytes(raw, policy=policy.default)
        item_id = hashlib.sha256(f"{bucket}/{key}".encode()).hexdigest()[:32]
        sender = email.utils.parseaddr(str(msg.get("From", "")))[1]
        authentic = str(msg.get("X-SES-DKIM-Verdict", "")).upper() == "PASS" and \
            str(msg.get("X-SES-SPF-Verdict", "")).upper() == "PASS"
        vendor_id = deps().data.vendor_id_by_email(sender) if authentic else None  # unauthenticated -> unverified
        body = msg.get_body(preferencelist=("plain", "html"))
        text = f"Subject: {msg.get('Subject', '')}\n\n{body.get_content() if body else ''}"
        attachments = []
        for part in msg.iter_attachments():
            ctype, name = part.get_content_type(), part.get_filename() or "attachment"
            if ctype in ("application/pdf", "image/png", "image/jpeg"):
                akey = f"attachments/{item_id}/{name}"
                s3.put_object(Bucket=bucket, Key=akey, Body=part.get_payload(decode=True), ServerSideEncryption="aws:kms")
                attachments.append({"uri": f"s3://{bucket}/{akey}", "name": name})
        channel = "invoice_doc" if attachments else "email"
        enqueue({"item_id": item_id, "channel": channel, "raw_input": text, "attachments": attachments,
                 "sender_id": vendor_id})
    return {"ok": True}
