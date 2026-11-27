"""Effectors. The only things the system ever *does*: save drafts, attach flags, queue items for humans,
start the payment-hold workflow, write the audit log. It never sends, pays, refunds or edits records."""
from __future__ import annotations

import hashlib

from . import metrics
from .deps import Deps

QUEUE_DEFAULT = "human_triage"


def _summary(state: dict) -> dict:
    res = state.get("results", {})
    brief: dict = {}
    if "SUPPORT" in res:
        r = res["SUPPORT"]
        brief["support"] = {k: r.get(k) for k in ("decision", "category", "sentiment", "draft_reply",
                                                  "escalation_reasons", "internal_note")}
    if "INVOICE" in res:
        r = res["INVOICE"]
        brief["invoice"] = {k: r.get(k) for k in ("recommendation", "overall_risk", "findings", "unverifiable_fields",
                                                  "suggested_action", "reviewer_summary")}
    if "SHIPMENT" in res:
        r = res["SHIPMENT"]
        brief["shipment"] = {k: r.get(k) for k in ("severity", "matched_pos", "signals", "new_eta", "business_impact",
                                                   "recommended_actions", "draft_followup_to_vendor")}
    if "TRIAGE" in res:
        brief["triage"] = res["TRIAGE"]
    return brief


def _is_invoice(state: dict) -> bool:
    return state.get("channel") == "invoice_doc" or "INVOICE" in state.get("results", {})


def queue_item(state: dict, d: Deps) -> dict:
    g, item_id = state["gate"], state["item_id"]
    queue = g.get("queue") or QUEUE_DEFAULT
    priority = (state.get("route") or {}).get("priority") or ("P1" if g.get("severity") == "CRITICAL" else "P2")
    summary = {"gate": g, "channel": state.get("channel"), "entities": state.get("entities", {}),
               "flags": state.get("guardrail_flags", []), "verifier": state.get("verifier"), **_summary(state)}
    d.ops.upsert_item(item_id, queue, "PENDING", priority, summary)
    d.ops.put_audit(item_id, "gate", {"gate": g, "verifier": state.get("verifier")})
    if _is_invoice(state):  # money-out path: hold first, ask questions later
        payload = {"reason": g.get("reason"), "severity": g.get("severity"), "queue": queue}
        d.ops.put_hold(item_id, payload)
        d.start_payment_hold(item_id, payload)
    metrics.emit("GateDecision", 1, Action=g["action"], Queue=queue)
    return {"outcome": {"queued": queue, "priority": priority}}


def _save_drafts(state: dict, d: Deps, status: str, override: str | None = None) -> list[str]:
    res, item_id, ids = state.get("results", {}), state["item_id"], []
    if "SUPPORT" in res and res["SUPPORT"].get("draft_reply"):
        ids.append(d.ops.put_draft(item_id, "customer_reply", override or res["SUPPORT"]["draft_reply"],
                                   {"status": status, "citations": res["SUPPORT"].get("doc_citations", [])}))
    if "SHIPMENT" in res:
        r = res["SHIPMENT"]
        if r.get("draft_followup_to_vendor"):
            ids.append(d.ops.put_draft(item_id, "vendor_followup", r["draft_followup_to_vendor"], {"status": status}))
        if r.get("support_impact", {}).get("proactive_notice_suggested"):
            d.ops.put_flag(item_id, "proactive_customer_notice_suggested", ",".join(r["support_impact"].get("skus", [])))
        if r.get("severity") in ("RED", "AMBER"):
            d.ops.put_flag(item_id, f"shipment_{r['severity'].lower()}", r.get("business_impact", ""))
    return ids


def finalize_item(state: dict, d: Deps) -> dict:
    g, item_id = state["gate"], state["item_id"]
    hd = state.get("human_decision")
    draft_ids: list[str] = []
    if g["action"] == "auto_draft":
        draft_ids = _save_drafts(state, d, "auto_draft")
        inv = state.get("results", {}).get("INVOICE")
        if inv and inv.get("recommendation") == "OK_TO_PAY":
            d.ops.put_flag(item_id, "ap_recommendation_ok_to_pay", inv.get("reviewer_summary", ""))
        final_status = "DRAFTED"
    else:
        hd = hd or {}
        act = hd.get("action", "reject")
        if act in ("approve", "edit"):
            draft_ids = _save_drafts(state, d, "approved_by_human", hd.get("edited_draft") if act == "edit" else None)
            if state.get("results", {}).get("INVOICE"):
                d.ops.put_flag(item_id, "ap_human_decision", f"{act} by {hd.get('reviewer', 'unknown')}: {hd.get('notes', '')}")
        else:
            d.ops.put_flag(item_id, "human_rejected", hd.get("notes", ""))
        final_status = "DECIDED"
        d.ops.upsert_item(item_id, g.get("queue") or QUEUE_DEFAULT, final_status,
                          (state.get("route") or {}).get("priority", "P2"),
                          {"gate": g, "human_decision": hd, **_summary(state)})
    d.ops.put_audit(item_id, "final", {
        "input_sha256": hashlib.sha256((state.get("raw_input") or "").encode()).hexdigest(),
        "route": state.get("route"), "gate": g, "human_decision": hd, "verifier": state.get("verifier"),
        "results": state.get("results"), "usage": state.get("usage"), "drafts": draft_ids,
    })
    if hd:  # human overrides become the eval set
        metrics.emit("HumanOverride", 1 if hd.get("action") in ("edit", "reject") else 0, Queue=g.get("queue") or "none")
    metrics.emit("ItemCompleted", 1, Status=final_status)
    return {"outcome": {"status": final_status, "drafts": draft_ids}}
