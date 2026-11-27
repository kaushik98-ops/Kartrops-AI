"""Read-side data access (orders, POs, vendors, inventory, KB).

Two implementations behind one interface:
  * MemoryDataStore  - loads samples/seed.json (local mode, tests)
  * DynamoDataStore  - single-table design on the DATA_TABLE (aws mode)
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .config import Settings

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


# --------------------------------------------------------------------------- key mapping
def _keys(kind: str, key: str, extra: str | None = None) -> list[tuple[str, str]]:
    """Return every (pk, sk) an entity is stored under (seed + lookup share this)."""
    m = {
        "orders": [(f"ORDER#{key}", "META")],
        "tracking": [(f"AWB#{key}", "META")],
        "customers": [(f"CUSTOMER#{key}", "META")],
        "grn": [(f"GRN#{key}", "META")],
        "vendors": [(f"VENDOR#{key}", "MASTER")],
        "scorecards": [(f"VENDOR#{key}", "SCORECARD")],
        "inventory": [(f"SKU#{key}", "INVENTORY")],
        "pos": [(f"PO#{key}", "META"), (f"VENDOR#{extra}", f"PO#{key}")],
        "payments": [(f"VENDOR#{extra}", f"PAY#{key}")],
    }
    return m[kind]


class DataStore:
    def get_order(self, order_id: str) -> dict | None: ...
    def get_tracking(self, awb: str) -> dict | None: ...
    def get_customer(self, customer_id: str) -> dict | None: ...
    def get_po(self, po_id: str) -> dict | None: ...
    def get_grn(self, po_id: str) -> dict | None: ...
    def get_vendor(self, vendor_id: str) -> dict | None: ...
    def get_payment_history(self, vendor_id: str) -> list[dict]: ...
    def open_pos(self, vendor_id: str) -> list[dict]: ...
    def get_scorecard(self, vendor_id: str) -> dict | None: ...
    def get_inventory(self, sku: str) -> dict | None: ...
    def sale_calendar(self) -> list[dict]: ...
    def vendor_id_by_email(self, email: str) -> str | None: ...


class MemoryDataStore(DataStore):
    def __init__(self, seed: dict | None = None):
        if seed is None:
            seed = json.loads((SAMPLES / "seed.json").read_text())
        self.d = seed

    def get_order(self, order_id): return self.d.get("orders", {}).get(order_id)
    def get_tracking(self, awb): return self.d.get("tracking", {}).get(awb)
    def get_customer(self, customer_id): return self.d.get("customers", {}).get(customer_id)
    def get_po(self, po_id): return self.d.get("pos", {}).get(po_id)
    def get_grn(self, po_id): return self.d.get("grn", {}).get(po_id)
    def get_vendor(self, vendor_id): return self.d.get("vendors", {}).get(vendor_id)
    def get_payment_history(self, vendor_id): return list(self.d.get("payments", {}).get(vendor_id, []))

    def open_pos(self, vendor_id):
        return [p for p in self.d.get("pos", {}).values()
                if p.get("vendor_id") == vendor_id and p.get("status", "open") == "open"]

    def get_scorecard(self, vendor_id): return self.d.get("scorecards", {}).get(vendor_id)
    def get_inventory(self, sku): return self.d.get("inventory", {}).get(sku)
    def sale_calendar(self): return list(self.d.get("sale_calendar", []))

    def vendor_id_by_email(self, email):
        email = (email or "").strip().lower()
        for vid, v in self.d.get("vendors", {}).items():
            if email in [e.lower() for e in v.get("emails", [])]:
                return vid
        return None


class DynamoDataStore(DataStore):
    def __init__(self, table_name: str, region: str):
        import boto3
        self.t = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def _get(self, pk: str, sk: str) -> dict | None:
        r = self.t.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return json.loads(r["json"]) if r else None

    def _query(self, pk: str, prefix: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        out, kwargs = [], {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix)}
        while True:
            r = self.t.query(**kwargs)
            out += [json.loads(i["json"]) for i in r["Items"]]
            if "LastEvaluatedKey" not in r:
                return out
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def get_order(self, order_id): return self._get(*_keys("orders", order_id)[0])
    def get_tracking(self, awb): return self._get(*_keys("tracking", awb)[0])
    def get_customer(self, customer_id): return self._get(*_keys("customers", customer_id)[0])
    def get_po(self, po_id): return self._get(*_keys("pos", po_id)[0])
    def get_grn(self, po_id): return self._get(*_keys("grn", po_id)[0])
    def get_vendor(self, vendor_id): return self._get(*_keys("vendors", vendor_id)[0])
    def get_payment_history(self, vendor_id): return self._query(f"VENDOR#{vendor_id}", "PAY#")
    def open_pos(self, vendor_id):
        return [p for p in self._query(f"VENDOR#{vendor_id}", "PO#") if p.get("status", "open") == "open"]
    def get_scorecard(self, vendor_id): return self._get(f"VENDOR#{vendor_id}", "SCORECARD")
    def get_inventory(self, sku): return self._get(*_keys("inventory", sku)[0])
    def sale_calendar(self): return (self._get("CALENDAR#sales", "META") or {}).get("events", [])

    def vendor_id_by_email(self, email):
        r = self._get(f"VENDOREMAIL#{(email or '').strip().lower()}", "META")
        return r["vendor_id"] if r else None

    def seed(self, seed: dict) -> int:
        n = 0
        with self.t.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as bw:
            def put(pk, sk, obj):
                nonlocal n
                bw.put_item(Item={"pk": pk, "sk": sk, "json": json.dumps(obj)})
                n += 1

            for kind in ("orders", "tracking", "customers", "grn", "vendors", "scorecards", "inventory"):
                for k, v in seed.get(kind, {}).items():
                    for pk, sk in _keys(kind, k):
                        put(pk, sk, v)
            for po_id, po in seed.get("pos", {}).items():
                for pk, sk in _keys("pos", po_id, po["vendor_id"]):
                    put(pk, sk, po)
            for vid, rows in seed.get("payments", {}).items():
                for row in rows:
                    pk, sk = _keys("payments", row["invoice_no"], vid)[0]
                    put(pk, sk, row)
            put("CALENDAR#sales", "META", {"events": seed.get("sale_calendar", [])})
            for vid, v in seed.get("vendors", {}).items():
                for e in v.get("emails", []):
                    put(f"VENDOREMAIL#{e.lower()}", "META", {"vendor_id": vid})
        return n


# --------------------------------------------------------------------------- knowledge base
_STOP = set(["a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "be", "been", "i", "my", "me", "we", "our", "you", "your", "it", "this", "that", "with", "at", "by", "from", "as", "not", "no", "do", "does", "did", "have", "has", "had", "will", "can", "could", "would", "should", "please", "hi", "hello", "dear", "thanks", "thank", "regards", "sir", "madam", "order"])


def _terms(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2]
    return [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words]  # crude plural stripping


class LocalKB:
    """Tiny keyword retriever over samples/kb/*.md so the graph runs offline."""

    def __init__(self, folder: Path | None = None):
        self.chunks: list[dict] = []
        for f in sorted((folder or SAMPLES / "kb").glob("*.md")):
            text = f.read_text()
            m = re.search(r"^last_updated:\s*(\S+)", text, re.MULTILINE)
            updated = m.group(1) if m else None
            body = re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL)
            for i, part in enumerate(re.split(r"\n(?=## )", body)):
                if part.strip():
                    self.chunks.append({"doc_id": f"{f.stem}#{i}", "text": part.strip(), "last_updated": updated})

    def retrieve(self, query: str, k: int = 4) -> list[dict]:
        q = set(_terms(query))
        scored = []
        for c in self.chunks:
            overlap = len(q & set(_terms(c["text"])))
            score = min(1.0, overlap / 2)  # two shared keywords = full score (offline stand-in)
            scored.append({**c, "score": round(score, 3)})
        return sorted(scored, key=lambda c: -c["score"])[:k]


class BedrockKB:
    def __init__(self, kb_id: str, region: str):
        import boto3
        self.kb_id = kb_id
        self.c = boto3.client("bedrock-agent-runtime", region_name=region)

    def retrieve(self, query: str, k: int = 4) -> list[dict]:
        r = self.c.retrieve(
            knowledgeBaseId=self.kb_id,
            retrievalQuery={"text": query[:900]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}},
        )
        out = []
        for x in r.get("retrievalResults", []):
            loc = x.get("location", {})
            uri = (loc.get("s3Location") or {}).get("uri") or (loc.get("webLocation") or {}).get("url") or "unknown"
            meta = x.get("metadata", {}) or {}
            out.append({
                "doc_id": uri.rsplit("/", 1)[-1],
                "text": x.get("content", {}).get("text", ""),
                "score": float(x.get("score", 0.0)),
                "last_updated": meta.get("last_updated"),
            })
        return out


def kb_is_stale(chunks: list[dict], s: Settings, today: date | None = None) -> bool:
    """True when the top chunk is older than KB_STALE_DAYS, or undated while dates are required."""
    if not chunks:
        return False
    today = today or datetime.now(UTC).date()
    lu = chunks[0].get("last_updated")
    if not lu:
        return s.kb_require_dates
    try:
        d = date.fromisoformat(str(lu)[:10])
    except ValueError:
        return True
    return (today - d).days > s.kb_stale_days


# --------------------------------------------------------------------------- factories
_cache: dict[str, Any] = {}


def get_data_store(s: Settings) -> DataStore:
    if "data" not in _cache:
        _cache["data"] = DynamoDataStore(s.data_table, s.region) if s.is_aws else MemoryDataStore()
    return _cache["data"]


def get_kb(s: Settings):
    if "kb" not in _cache:
        _cache["kb"] = BedrockKB(s.kb_id, s.region) if (s.is_aws and s.kb_id) else LocalKB()
    return _cache["kb"]


def reset_caches() -> None:
    _cache.clear()
