"""Write-side store: audit log, drafts, flags, human-review queue.
These are the ONLY writes the system makes (all reversible, none move money)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from .config import Settings


def now() -> str:
    return datetime.now(UTC).isoformat()


class OpsStore:
    def put_audit(self, item_id: str, event: str, payload: dict) -> None: ...
    def put_draft(self, item_id: str, kind: str, text: str, meta: dict | None = None) -> str: ...
    def put_flag(self, item_id: str, flag: str, reason: str) -> None: ...
    def upsert_item(self, item_id: str, queue: str, status: str, priority: str, summary: dict) -> None: ...
    def list_queue(self, queue: str, status: str = "PENDING", limit: int = 50) -> list[dict]: ...
    def get_item(self, item_id: str) -> dict | None: ...
    def put_hold(self, item_id: str, payload: dict) -> None: ...


class MemoryOpsStore(OpsStore):
    def __init__(self):
        self.audit: list[dict] = []
        self.drafts: list[dict] = []
        self.flags: list[dict] = []
        self.items: dict[str, dict] = {}
        self.holds: list[dict] = []

    def put_audit(self, item_id, event, payload):
        self.audit.append({"item_id": item_id, "event": event, "ts": now(), "payload": payload})

    def put_draft(self, item_id, kind, text, meta=None):
        did = uuid.uuid4().hex[:12]
        self.drafts.append({"draft_id": did, "item_id": item_id, "kind": kind, "text": text, "meta": meta or {}})
        return did

    def put_flag(self, item_id, flag, reason):
        self.flags.append({"item_id": item_id, "flag": flag, "reason": reason, "ts": now()})

    def upsert_item(self, item_id, queue, status, priority, summary):
        self.items[item_id] = {"item_id": item_id, "queue": queue, "status": status,
                               "priority": priority, "summary": summary, "updated": now()}

    def list_queue(self, queue, status="PENDING", limit=50):
        rows = [i for i in self.items.values() if i["queue"] == queue and i["status"] == status]
        return sorted(rows, key=lambda r: (r["priority"], r["updated"]))[:limit]

    def get_item(self, item_id):
        it = self.items.get(item_id)
        if not it:
            return None
        return {**it, "audit": [a for a in self.audit if a["item_id"] == item_id]}

    def put_hold(self, item_id, payload):
        self.holds.append({"item_id": item_id, **payload})


class DynamoOpsStore(OpsStore):
    MAX_PAYLOAD = 300_000  # DynamoDB item limit is 400 KB

    def __init__(self, table_name: str, region: str):
        import boto3
        self.t = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put_audit(self, item_id, event, payload):
        body = json.dumps(payload, default=str)[: self.MAX_PAYLOAD]
        self.t.put_item(Item={"pk": f"ITEM#{item_id}", "sk": f"AUDIT#{now()}#{event}", "event": event, "json": body})

    def put_draft(self, item_id, kind, text, meta=None):
        did = uuid.uuid4().hex[:12]
        self.t.put_item(Item={"pk": f"ITEM#{item_id}", "sk": f"DRAFT#{did}", "kind": kind, "text": text,
                              "json": json.dumps(meta or {}, default=str), "created": now()})
        return did

    def put_flag(self, item_id, flag, reason):
        self.t.put_item(Item={"pk": f"ITEM#{item_id}", "sk": f"FLAG#{now()}#{flag}", "flag": flag, "reason": reason})

    def upsert_item(self, item_id, queue, status, priority, summary):
        ts = now()
        self.t.put_item(Item={
            "pk": f"ITEM#{item_id}", "sk": "STATE", "queue": queue, "status": status, "priority": priority,
            "json": json.dumps(summary, default=str)[: self.MAX_PAYLOAD], "updated": ts,
            "gsi1pk": f"QUEUE#{queue}#{status}", "gsi1sk": f"{priority}#{ts}",
        })

    def list_queue(self, queue, status="PENDING", limit=50):
        from boto3.dynamodb.conditions import Key
        r = self.t.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(f"QUEUE#{queue}#{status}"),
                         Limit=limit)
        return [{"item_id": i["pk"].split("#", 1)[1], "queue": i["queue"], "status": i["status"],
                 "priority": i["priority"], "summary": json.loads(i["json"]), "updated": i["updated"]}
                for i in r["Items"]]

    def get_item(self, item_id):
        from boto3.dynamodb.conditions import Key
        r = self.t.query(KeyConditionExpression=Key("pk").eq(f"ITEM#{item_id}"))
        rows = r["Items"]
        state = next((x for x in rows if x["sk"] == "STATE"), None)
        if not state:
            return None
        return {"item_id": item_id, "queue": state["queue"], "status": state["status"],
                "priority": state["priority"], "summary": json.loads(state["json"]),
                "audit": [{"event": x["event"], "ts": x["sk"].split("#")[1], "payload": json.loads(x["json"])}
                          for x in rows if x["sk"].startswith("AUDIT#")],
                "drafts": [{"draft_id": x["sk"].split("#")[1], "kind": x["kind"], "text": x["text"]}
                           for x in rows if x["sk"].startswith("DRAFT#")]}

    def put_hold(self, item_id, payload):
        self.t.put_item(Item={"pk": f"ITEM#{item_id}", "sk": "HOLD", "json": json.dumps(payload, default=str),
                              "created": now()})


_ops: OpsStore | None = None


def get_ops(s: Settings) -> OpsStore:
    global _ops
    if _ops is None:
        _ops = DynamoOpsStore(s.ops_table, s.region) if s.is_aws else MemoryOpsStore()
    return _ops


def reset_ops() -> None:
    global _ops
    _ops = None
