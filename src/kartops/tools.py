"""Tools exposed to the specialist nodes (per-node allow-list = prompt rule G6).
Reads are redacted before the model sees them; writes are limited to flag_item / create_draft."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .data import DataStore
from .ops import OpsStore
from .verified import _mask

NODE_TOOLS: dict[str, list[str]] = {
    "router": [], "verifier": [],
    "support": ["kb_retrieve", "get_order", "get_tracking", "get_customer_profile", "create_draft", "flag_item"],
    "invoice": ["get_po", "get_grn", "get_vendor_master", "get_payment_history", "flag_item"],
    "shipment": ["get_open_pos", "get_vendor_scorecard", "get_inventory", "get_sale_calendar", "flag_item",
                 "create_draft"],
}

_S = lambda desc: {"type": "string", "description": desc}


def _obj(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


SPECS: dict[str, tuple[str, dict]] = {
    "kb_retrieve": ("Search the help-centre knowledge base. Returns chunks with doc_id, score, last_updated.",
                    _obj(query=_S("search query"))),
    "get_order": ("Fetch an order by id.", _obj(order_id=_S("order id"))),
    "get_tracking": ("Fetch shipment tracking by AWB.", _obj(awb=_S("airway bill number"))),
    "get_customer_profile": ("Fetch tier and contact history for a customer id.", _obj(customer_id=_S("customer id"))),
    "get_po": ("Fetch a purchase order.", _obj(po_id=_S("PO id"))),
    "get_grn": ("Fetch the goods-receipt note for a PO.", _obj(po_id=_S("PO id"))),
    "get_vendor_master": ("Fetch the vendor master record (bank details masked).", _obj(vendor_id=_S("vendor id"))),
    "get_payment_history": ("Fetch recent payments/invoices for a vendor.", _obj(vendor_id=_S("vendor id"))),
    "get_open_pos": ("List open POs for a vendor.", _obj(vendor_id=_S("vendor id"))),
    "get_vendor_scorecard": ("Fetch on-time-delivery scorecard for a vendor.", _obj(vendor_id=_S("vendor id"))),
    "get_inventory": ("Fetch stock position for a SKU.", _obj(sku=_S("sku"))),
    "get_sale_calendar": ("List upcoming sale events.", {"type": "object", "properties": {}}),
    "flag_item": ("Attach a reversible flag to this item for humans.", _obj(flag=_S("short flag"), reason=_S("why"))),
    "create_draft": ("Stage a draft message. Nothing is sent; drafts are saved only after verification.",
                     _obj(kind=_S("customer_reply|vendor_followup|customer_notice"), text=_S("draft text"))),
}


def _redact_vendor(v: dict | None) -> dict | None:
    if not v:
        return v
    v = dict(v)
    if "bank_account" in v:
        v["bank_account"] = _mask(v["bank_account"])
    return v


@dataclass
class ToolBox:
    s: Settings
    data: DataStore
    kb: Any
    ops: OpsStore
    item_id: str
    allowed: list[str]
    staged_drafts: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    seen_doc_ids: set = field(default_factory=set)

    def specs(self) -> list[dict]:
        return [{"toolSpec": {"name": n, "description": SPECS[n][0], "inputSchema": {"json": SPECS[n][1]}}}
                for n in self.allowed]

    def call(self, name: str, args: dict) -> Any:
        if name not in self.allowed:
            return {"error": f"tool '{name}' is not available to this node"}
        try:
            out = getattr(self, f"_t_{name}")(**args)
        except TypeError as e:
            out = {"error": f"bad arguments: {e}"}
        except Exception as e:  # tool failures must not crash the graph
            out = {"error": f"{type(e).__name__}: {e}"}
        self.calls.append({"tool": name, "args": args})
        return out if out is not None else {"result": None, "note": "not found"}

    # reads -----------------------------------------------------------------
    def _t_kb_retrieve(self, query):
        chunks = self.kb.retrieve(query)
        self.seen_doc_ids.update(c["doc_id"] for c in chunks)
        return {"chunks": chunks}
    def _t_get_order(self, order_id): return self.data.get_order(order_id)
    def _t_get_tracking(self, awb): return self.data.get_tracking(awb)

    def _t_get_customer_profile(self, customer_id):
        c = self.data.get_customer(customer_id) or {}
        return {k: c.get(k) for k in ("tier", "contact_count_30d")} if c else None

    def _t_get_po(self, po_id): return self.data.get_po(po_id)
    def _t_get_grn(self, po_id): return self.data.get_grn(po_id)
    def _t_get_vendor_master(self, vendor_id): return _redact_vendor(self.data.get_vendor(vendor_id))
    def _t_get_payment_history(self, vendor_id): return {"payments": self.data.get_payment_history(vendor_id)[-20:]}
    def _t_get_open_pos(self, vendor_id): return {"open_pos": self.data.open_pos(vendor_id)}
    def _t_get_vendor_scorecard(self, vendor_id): return self.data.get_scorecard(vendor_id)
    def _t_get_inventory(self, sku): return self.data.get_inventory(sku)
    def _t_get_sale_calendar(self): return {"events": self.data.sale_calendar()}

    # writes (reversible) ------------------------------------------------------
    def _t_flag_item(self, flag, reason):
        self.ops.put_flag(self.item_id, flag, reason)
        return {"ok": True}

    def _t_create_draft(self, kind, text):
        self.staged_drafts.append({"kind": kind, "text": text})
        return {"ok": True, "note": "staged; will be saved after verification and gate"}
