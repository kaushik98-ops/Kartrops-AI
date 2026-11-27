"""LangGraph state. `results`, `verified_facts` and `context` use merge reducers so that
mixed items can run specialist branches in parallel and merge their outputs."""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


class OpsState(TypedDict, total=False):
    item_id: str
    channel: Literal["ticket", "invoice_doc", "email"]
    sender_id: str | None                 # authenticated requester id, if the channel provides one
    raw_input: str                           # untrusted, PII-masked after preprocess
    attachments: list[dict]                  # S3 URIs
    guardrail_flags: list[str]
    language: str
    entities: dict                           # order_id, po_id, invoice_no, vendor_id, awb, sku
    extraction: dict                         # normalised Textract output (invoice channel)
    route: dict                              # primary, secondary, priority, early_flags
    context: Annotated[dict, merge_dicts]    # KB chunks, order data, PO/GRN, vendor master
    verified_facts: Annotated[dict, merge_dicts]
    results: Annotated[dict, merge_dicts]    # per-module outputs, keyed SUPPORT/INVOICE/SHIPMENT/TRIAGE
    verifier: dict
    gate: dict                               # code-gate decision
    human_decision: dict | None
    outcome: dict                            # what effectors actually did
    usage: Annotated[dict, merge_dicts]
