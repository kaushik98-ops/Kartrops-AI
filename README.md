# KartOps AI

One operations assistant for a Flipkart-style marketplace, built on **LangGraph + AWS**:

| Job | What it does | Hard rule |
|---|---|---|
| **A. Support** | Drafts replies from the help-centre knowledge base; escalates tricky tickets | Never sends. Escalates money disputes, fraud, legal, safety, angry/VIP/repeat contacts, weak or stale policy |
| **B. Invoice** | Reads invoices (Textract), checks them against PO/GRN/vendor master, explains mismatches | Never approves payment. Anything but `OK_TO_PAY` puts a payment hold in place |
| **C. Shipment** | Reads vendor emails, catches explicit *and hedged* delays, scores RED/AMBER/GREEN | Vague language with no date is never GREEN |

It **drafts, analyses and flags**. It never sends a message, releases a payment, issues a refund or edits a record.

## Architecture

```
Email (SES->S3) / API Gateway (IAM) ──► SQS (+DLQ) ──► Worker Lambda (container) ── LangGraph ──┐
                                                                                               │
 preprocess ─► router ─┬─► support  (KB retrieve, order/customer lookups) ─┐                    │
 (guardrail in,        ├─► invoice  (Textract, deterministic checks)      ─┼─► verifier ─► GATE ─┬─► finalize (draft saved)
  entities,            ├─► shipment (dates, PO match, stock, sale cal.)   ─┘   (LLM + lint +    │
  Textract)            ├─► triage                                              output guardrail)└─► queue_item ─► human_review
        └──────────────┴─► quarantine (prompt attack: never reaches an LLM)                           interrupt() ─► finalize
                                                                                                    │
   Bedrock (Converse + forced tool use) · Bedrock Guardrails · Bedrock Knowledge Base ·             ▼
   DynamoDB: data (read) / ops+audit+queues / checkpoints · Step Functions payment-hold ──► SNS ──► Accounts Payable
```

### Design decisions (and why)

1. **Code computes facts, the model interprets them.** Arithmetic, duplicates, bank-detail comparison, three-way match,
   date maths, slip days, stock-out dates and retrieval scores are plain Python (`verified.py`) and handed to the model as
   `<verified_facts>`. The model explains and drafts; it cannot override them.
2. **The gate is code, not a prompt** (`gate.py`). Injection, bank mismatch, duplicates, non-`OK_TO_PAY` invoices,
   verifier failure, low confidence, weak/stale KB, requester ≠ order owner, VIP/repeat contact, RED shipments, unknown
   senders: all enforced outside the model.
3. **Floors, not ceilings.** After the model answers, code can only make a result *stricter* (`floor_invoice`,
   `floor_shipment`): a model saying `OK_TO_PAY` on a failed three-way match becomes `HOLD_FOR_REVIEW`.
4. **Fail toward a human.** LLM error, schema failure or verifier outage → escalate / hold, never auto-approve.
5. **Untrusted input is quarantined.** Guardrails prompt-attack hit → `quarantine` node, no LLM ever sees it.
   Invoice free-text is guarded too. `<untrusted>` tag break-out is neutralised.
6. **Privacy by construction.** The model only sees bank accounts as `XXXX1234`; another customer's order is withheld
   when the requester doesn't own it; external drafts are linted for internal names, card-like numbers and commitments.
7. **Structured output via Bedrock Converse tool use**: each node ends with `submit_result` (JSON-Schema validated,
   2 repair attempts). Prompt caching on the shared BASE block.
8. **Human-in-the-loop is durable.** `interrupt()` + DynamoDB checkpointer: reviewers can respond hours or days later
   (30-day TTL). The reviewer identity is taken from the IAM caller, not from the request body.
9. **Idempotent.** SQS redelivery of a finished/paused item is a no-op; a crashed run resumes from its checkpoint.
10. **Mixed items** run their secondary modules as parallel branches and merge into `results`.

## Repository layout

```
src/kartops/   prompts/*.md  schemas.py  state.py  nodes.py  gate.py  verified.py  lint.py  guardrails.py
               llm.py (Bedrock)  fake_llm.py (offline)  tools.py  data.py  ops.py  textract.py  actions.py
               graph.py  runner.py  handlers.py (Lambdas)  evals.py  cli.py
infra/         CDK app (Python): tables, bucket, queue, guardrail, Lambdas, API, Step Functions, alarms
samples/       seed data, KB docs, sample invoices        evals/cases.jsonl   regression + adversarial cases
tests/         32 unit/integration tests (run offline)    docs/DATA_MODEL.md  tables your systems must populate
```

## Run locally (no AWS needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make test        # 32 tests
make demo        # 7 scenarios through the whole graph with the rule-based FakeLLM
make eval        # 15 cases: reports escalation recall and false-OK count
kartops run --channel ticket --sender C100 --text "I was charged twice for OD1234567890" --decide approve
```

Local mode uses an in-memory store, a keyword KB and a **rule-based FakeLLM**. That tests the plumbing, the gate and
the floors, not model quality. To exercise real Bedrock models locally, export `MODEL_FAST` / `MODEL_STRONG` and AWS
credentials (the in-memory data is still used).

## Deploy to AWS

**Prerequisites:** AWS account + CLI v2 configured · Python 3.11+ · Node 18+ · **Docker running** (Lambda container
image) · permission to create IAM, Lambda, DynamoDB, KMS, API Gateway, Step Functions, Bedrock Guardrails.

1. **Pick a region** where Bedrock, Textract and your models are available (e.g. `ap-south-1`). Enable model access:
   Bedrock console → *Model access* → request the Claude models you will use.
2. **Choose model IDs** (use inference-profile IDs where the region requires them):
   ```bash
   aws bedrock list-inference-profiles --region ap-south-1 --query 'inferenceProfileSummaries[].inferenceProfileId'
   export MODEL_FAST=<small, fast model id>      # router + verifier
   export MODEL_STRONG=<stronger model id>       # support / invoice / shipment
   ```
3. **Create the Knowledge Base** (Bedrock console → Knowledge bases → *Create* → S3 data source with your help-centre
   docs → sync). For every document add a sidecar `<file>.metadata.json`:
   `{"metadataAttributes": {"last_updated": "2026-10-10"}}`. Undated documents are treated as *stale* (escalate),
   by design (`KB_REQUIRE_DATES=false` to relax). Copy the KB ID.
4. **Bootstrap + deploy:**
   ```bash
   cd infra && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
   npm install -g aws-cdk
   export AWS_REGION=ap-south-1
   cdk bootstrap
   cdk deploy -c model_fast=$MODEL_FAST -c model_strong=$MODEL_STRONG -c kb_id=<KB_ID> \
              -c alert_email=ops@yourco.com -c platform_name="YourMart"
   ```
   (or `make deploy KB_ID=... ALERT_EMAIL=...`). Note the outputs: `ApiUrl`, `OpsTable`, `DataTable`, `BucketName`.
   Confirm the SNS e-mail subscription you receive.
5. **Load demo data** (replace with your ETL later, see `docs/DATA_MODEL.md`):
   ```bash
   export KARTOPS_MODE=aws AWS_REGION=ap-south-1 DATA_TABLE=<DataTable output>
   pip install -e . && kartops seed
   ```
6. **Smoke test** (API uses IAM auth, so sign requests, e.g. with `pip install awscurl`):
   ```bash
   API=<ApiUrl>
   awscurl --service execute-api --region ap-south-1 -X POST $API/items \
     -d '{"channel":"ticket","sender_id":"C100","raw_input":"I was charged twice for order OD1234567890, please refund"}'
   awscurl --service execute-api --region ap-south-1 $API/queues/payments_refunds
   awscurl --service execute-api --region ap-south-1 -X POST $API/items/<item_id>/decision \
     -d '{"action":"approve","notes":"checked with payments"}'
   ```
   Invoice test: upload a PDF/PNG invoice to the bucket, then POST `{"channel":"invoice_doc","attachments":[{"uri":"s3://<bucket>/invoices/x.pdf"}],"raw_input":"Please pay"}`.
7. **(Optional) Inbound e-mail:** verify your domain in SES, add a receipt rule that stores mail in the bucket under
   prefix `inbound-email/`. Mail from a known vendor address with passing SPF + DKIM is treated as that vendor; anything
   else is "unverified sender" and escalates. Check SES email-receiving availability in your region.
8. **Tear down:** `cdk destroy ...` (deploy with `-c retain_data=false` if you want tables/bucket deleted too).

### Reviewer API

| Call | Purpose |
|---|---|
| `POST /v1/items` | submit `{channel, raw_input, sender_id?, attachments?}` → 202 `{item_id}` |
| `GET /v1/queues/{queue}` | pending items (`payments_refunds, support_l2, fraud_risk, legal_compliance, seller_support, ap_review, supply_chain, human_triage`) |
| `GET /v1/items/{id}` | state, results, audit trail, drafts |
| `POST /v1/items/{id}/decision` | `{action: approve\|edit\|reject, edited_draft?, notes?}` resumes the paused graph |

Put your agent desktop / Slack bot behind this API; grant reviewers `execute-api:Invoke` via IAM or front it with Cognito.

## Configuration

All via environment variables (set by CDK): `CONF_THRESHOLD` (0.75) · `KB_MIN_SCORE` (0.55) · `KB_STALE_DAYS` (365) ·
`FIELD_CONF_MIN` (0.90) · `PRICE_TOL_PCT` (2) · `QTY_TOL_PCT` (0) · `AMOUNT_TOL_ABS` (1) · `VIP_TIERS` ·
`PLATFORM_NAME` · `BRAND_TONE` · `ENABLE_PROMPT_CACHE`. Prompts live in `src/kartops/prompts/*.md`; edit and redeploy.
**Tune `KB_MIN_SCORE` against your own Bedrock KB scores**, the default is a placeholder.

## Evaluation and operations

* `kartops eval` runs `evals/cases.jsonl` (clean cases + fake bank-change invoice, injection, vague "slight adjustment" email,
  stale policy, wrong-owner order). Grow it to ~30 cases per node; **track escalation recall and false-OK rate first**.
* Every item writes input hash, node outputs, gate decision and human decision to the ops table. Human edits/rejections
  are your future eval set (`HumanOverride` metric).
* CloudWatch (namespace `KartOps`): `GateDecision{Action,Queue}`, `ItemLatencyMs`, `HumanOverride`, `ItemCompleted`.
  Alarms: DLQ not empty, worker errors → SNS.
* Cost levers: small model for router/verifier, prompt caching, `max_concurrency` on the SQS event source, `ttl` on checkpoints.

## Known limits (read before production)

> **Tested offline only.** CI and local runs use the in-memory store and the rule-based FakeLLM. Nothing here has been run against live Bedrock, Textract, Guardrails or a Knowledge Base yet; do the step 6 smoke test before relying on it.

* **Not run against live AWS in this build.** Logic, gate, graph, handlers and CDK synthesis are tested offline; the Bedrock,
  Textract, Guardrails and KB calls follow the documented APIs but need your smoke test. Verify model IDs, the Textract
  field mapping on *your* invoice layouts (`textract.py`), and Guardrail behaviour (e.g. the Aadhaar regex can also mask
  other 12-digit numbers).
* One worker role serves all nodes; per-node least privilege is enforced by tool allow-lists, read-only data access and
  write-limited tools. For stricter isolation, move tools behind separate Lambdas / AgentCore Gateway.
* The shipment ETA parser is a deliberate heuristic (day-first dates, ETA-keyword sentences). Unparseable → `unknown`.
* The local KB scorer and FakeLLM are test doubles.
* Support quality depends on KB coverage and freshness. Start in shadow mode (drafts only, humans send) and measure.

## License

MIT, see `LICENSE`.
