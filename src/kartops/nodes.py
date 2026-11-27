"""LangGraph node implementations. Pattern for every specialist:
   code fetches facts  ->  LLM interprets/drafts via submit_result  ->  code floors the result.
Any LLM failure fails TOWARD a human (never toward auto-approval)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langgraph.types import interrupt

from . import prompts, schemas
from .deps import Deps
from .gate import gate as run_gate
from .lint import lint_external
from .llm import LLMError
from .textract import analyze_invoice
from .tools import NODE_TOOLS, ToolBox
from .verified import invoice_facts, mask_extraction_for_llm, shipment_facts, support_facts

log = logging.getLogger("kartops.nodes")

# ------------------------------------------------------------------------------ helpers
ENTITY_PATTERNS = {
    "order_id": re.compile(r"\bOD\d{9,20}\b"),
    "po_id": re.compile(r"\bPO[-_ ]?\d{3,}\b", re.IGNORECASE),
    "awb": re.compile(r"\bAWB[\s:#-]*([A-Z0-9]{8,})\b", re.IGNORECASE),
    "vendor_id": re.compile(r"\bVND[-_]?\d+\b", re.IGNORECASE),
    "invoice_no": re.compile(r"\b(?:invoice|inv)\s*(?:no\.?|number|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-/]{3,})", re.IGNORECASE),
}


def extract_entities(text: str) -> dict:
    out: dict[str, Any] = {}
    for k, rx in ENTITY_PATTERNS.items():
        m = rx.search(text or "")
        if m:
            v = m.group(1) if rx.groups else m.group(0)
            out[k] = re.sub(r"[_ ]", "-", v.upper()) if k in ("po_id", "vendor_id") else v
    return out


def _xml(tag: str, obj: Any) -> str:
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str)
    return f"<{tag}>\n{body}\n</{tag}>"


def _untrusted(text: str) -> str:
    safe = re.sub(r"</?\s*untrusted\s*>", "[tag removed]", text or "", flags=re.IGNORECASE)  # no tag breakout
    return f"<untrusted>\n{safe}\n</untrusted>"


def _detect_language(text: str, d: Deps) -> str:
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    if d.s.is_aws and text.strip():
        try:
            import boto3
            c = boto3.client("comprehend", region_name=d.s.region)
            langs = c.detect_dominant_language(Text=text[:4000])["Languages"]
            if langs and langs[0]["Score"] > 0.6:
                return langs[0]["LanguageCode"]
        except Exception as e:  # language detection is best-effort
            log.warning("comprehend failed: %s", e)
    return "en"


def _call(d: Deps, node: str, state: dict, user_text: str, payload: dict, tools: ToolBox | None) -> tuple[dict, dict]:
    base, node_prompt = prompts.system_blocks(node, d.s.prompt_vars(state.get("language", "en")))
    return d.llm.run(node=node, base=base, node_prompt=node_prompt, user_text=user_text, payload=payload,
                     tools=tools, schema=schemas.BY_NODE[node])


def _toolbox(d: Deps, node: str, item_id: str) -> ToolBox:
    return ToolBox(s=d.s, data=d.data, kb=d.kb, ops=d.ops, item_id=item_id, allowed=NODE_TOOLS[node])


def _usage(node: str, u: dict) -> dict:
    return {node: u}


# ------------------------------------------------------------------------------ nodes
def make_nodes(d: Deps) -> dict:
    s = d.s

    # ---------------------------------------------------------------- preprocess
    def preprocess(state: dict) -> dict:
        raw = (state.get("raw_input") or "")[:20000]
        entities = {**extract_entities(raw), **(state.get("entities") or {})}
        language = _detect_language(raw, d)
        masked, flags = d.guard.check_input(raw) if raw.strip() else ("", [])
        extraction: dict = {}
        if state.get("channel") == "invoice_doc":
            att = next((a for a in state.get("attachments", []) if a.get("uri")), None)
            if att:
                extraction = analyze_invoice(att["uri"], s, state["item_id"])
                # free-text fields of the document are untrusted too: guard them
                text_fields = " \n".join([str(extraction.get("vendor_name", ""))] +
                                         [str(l.get("description", "")) for l in extraction.get("lines", [])])
                _, f2 = d.guard.check_input(text_fields)
                flags += [f for f in f2 if f not in flags]
                if extraction.get("invoice_no"):
                    entities.setdefault("invoice_no", extraction["invoice_no"])
                if extraction.get("po_ref"):
                    entities.setdefault("po_id", re.sub(r"[_ ]", "-", extraction["po_ref"].upper()))
        return {"raw_input": masked, "entities": entities, "language": language,
                "guardrail_flags": flags, "extraction": extraction}

    # ---------------------------------------------------------------- router
    def router(state: dict) -> dict:
        text = state.get("raw_input", "")
        payload = {"text": text, "entities": state.get("entities", {}), "channel": state.get("channel"),
                   "language": state.get("language", "en"), "has_attachment": bool(state.get("extraction"))}
        user = "\n".join([_untrusted(text), _xml("entities", state.get("entities", {})),
                          _xml("metadata", {"channel": state.get("channel"), "has_attachment": payload["has_attachment"]}),
                          _xml("guardrail_flags", state.get("guardrail_flags", []))])
        try:
            r, u = _call(d, "router", state, user, payload, None)
        except LLMError as e:
            log.error("router failed: %s", e)
            r, u = {"primary": "TRIAGE", "secondary": [], "entities": {}, "language": state.get("language", "en"),
                    "priority": "P2", "early_flags": [], "confidence": 0.0}, {}
        if state.get("channel") == "invoice_doc" and state.get("extraction"):
            r["primary"] = "INVOICE"  # channel is trusted metadata; do not let the model reroute documents
        r["secondary"] = [x for x in r.get("secondary", []) if x != r["primary"]]
        entities = {**{k: v for k, v in (r.get("entities") or {}).items() if v}, **state.get("entities", {})}
        return {"route": r, "entities": entities, "usage": _usage("router", u)}

    def after_router(state: dict) -> list[str]:
        r = state["route"]
        if "possible_injection" in r.get("early_flags", []) or "PROMPT_ATTACK" in state.get("guardrail_flags", []):
            return ["quarantine"]
        if r["primary"] == "TRIAGE":
            return ["triage"]
        return [r["primary"].lower()] + [x.lower() for x in r.get("secondary", [])]

    def after_preprocess(state: dict) -> str:
        return "quarantine" if "PROMPT_ATTACK" in state.get("guardrail_flags", []) else "router"

    # ---------------------------------------------------------------- support
    def support(state: dict) -> dict:
        text, ent = state.get("raw_input", ""), state.get("entities", {})
        order = d.data.get_order(ent["order_id"]) if ent.get("order_id") else None
        cust_id = (order or {}).get("customer_id") or ent.get("customer_id") or state.get("sender_id")
        customer = d.data.get_customer(cust_id) if cust_id else None
        chunks = d.kb.retrieve(text[:900] or "help")
        facts = support_facts(chunks, order, customer, state.get("sender_id"), s)
        order_ctx = order
        if order and facts["requester_matches_order"] is False:  # privacy: never expose someone else's order
            order_ctx = {"order_id": order.get("order_id"), "withheld": "requester does not match order owner"}
        tools = _toolbox(d, "support", state["item_id"])
        tools.seen_doc_ids.update(c["doc_id"] for c in chunks)
        user = "\n".join([_untrusted(text), _xml("kb_results", chunks), _xml("order_context", order_ctx or {}),
                          _xml("verified_facts", facts), _xml("guardrail_flags", state.get("guardrail_flags", []))])
        payload = {"text": text, "kb_results": chunks, "order_context": order_ctx, "verified_facts": facts}
        try:
            r, u = _call(d, "support", state, user, payload, tools)
        except LLMError as e:
            log.error("support failed: %s", e)
            r, u = {"decision": "escalate", "category": "other", "sentiment": "unknown",
                    "draft_reply": "Thanks for reaching out. A specialist will follow up with you shortly.",
                    "doc_citations": [], "escalation_queue": "support_l2",
                    "escalation_reasons": ["automation failure"], "internal_note": f"LLM failure: {e}",
                    "confidence": 0.0}, {}
        return {"results": {"SUPPORT": r}, "verified_facts": facts, "usage": _usage("support", u),
                "context": {"SUPPORT": {"kb_results": chunks, "order_context": order_ctx,
                                        "kb_doc_ids": sorted(tools.seen_doc_ids), "staged": tools.staged_drafts}}}

    # ---------------------------------------------------------------- invoice
    def invoice(state: dict) -> dict:
        ex, ent = state.get("extraction") or {}, state.get("entities", {})
        po_id = re.sub(r"[_ ]", "-", str(ex.get("po_ref") or ent.get("po_id") or "")).upper() or None
        po = d.data.get_po(po_id) if po_id else None
        vendor_id = (po or {}).get("vendor_id") or ent.get("vendor_id")  # PO owner is the trusted vendor
        vendor = d.data.get_vendor(vendor_id) if vendor_id else None
        grn = d.data.get_grn(po_id) if po_id else None
        history = d.data.get_payment_history(vendor_id) if vendor_id else []
        facts = invoice_facts(ex, po, grn, vendor, history, s)
        vendor_view = None
        if vendor:
            vendor_view = {k: vendor.get(k) for k in ("vendor_id", "name", "gstin", "payment_terms_days", "emails", "phones")}
            vendor_view["bank_account_last4"] = facts["bank_last4"]["vendor_master"]
        tol = {"price_pct": s.price_tol_pct, "qty_pct": s.qty_tol_pct, "amount_abs": s.amount_tol_abs}
        tools = _toolbox(d, "invoice", state["item_id"])
        user = "\n".join([_untrusted(state.get("raw_input", "")), _xml("extraction", mask_extraction_for_llm(ex)),
                          _xml("verified_facts", facts), _xml("po", po or {}), _xml("grn", grn or {}),
                          _xml("vendor_master", vendor_view or {}), _xml("tolerances", tol),
                          _xml("guardrail_flags", state.get("guardrail_flags", []))])
        payload = {"text": state.get("raw_input", ""), "verified_facts": facts}
        try:
            r, u = _call(d, "invoice", state, user, payload, tools)
        except LLMError as e:
            log.error("invoice failed: %s", e)
            r, u = {"recommendation": "HOLD_FOR_REVIEW", "overall_risk": "high", "findings": [],
                    "unverifiable_fields": ["all"], "suggested_action": "Manual review: automation failed.",
                    "reviewer_summary": "Automated analysis unavailable.", "confidence": 0.0}, {}
        r = floor_invoice(r, facts)
        return {"results": {"INVOICE": r}, "verified_facts": facts, "usage": _usage("invoice", u),
                "context": {"INVOICE": {"po": po, "grn": grn, "vendor_master": vendor_view, "extraction": mask_extraction_for_llm(ex)}}}

    # ---------------------------------------------------------------- shipment
    def shipment(state: dict) -> dict:
        text, ent = state.get("raw_input", ""), state.get("entities", {})
        vendor_id = state.get("sender_id") or ent.get("vendor_id")
        open_pos = d.data.open_pos(vendor_id) if vendor_id else []
        skus = {l["sku"] for p in open_pos for l in p.get("lines", [])}
        inventory = {k: v for k in skus if (v := d.data.get_inventory(k))}
        calendar = d.data.sale_calendar()
        facts = shipment_facts(text, open_pos, inventory, calendar, d.today())
        facts["sender_verified"] = bool(state.get("sender_id"))
        tools = _toolbox(d, "shipment", state["item_id"])
        user = "\n".join([_untrusted(text), _xml("open_pos", open_pos), _xml("verified_facts", facts),
                          _xml("entities", ent), _xml("guardrail_flags", state.get("guardrail_flags", []))])
        payload = {"text": text, "verified_facts": facts}
        try:
            r, u = _call(d, "shipment", state, user, payload, tools)
        except LLMError as e:
            log.error("shipment failed: %s", e)
            r, u = {"severity": "AMBER", "matched_pos": facts["matched_pos"], "signals": [], "new_eta": facts["new_eta"],
                    "slip_days": 0, "days_past_need_by": 0, "business_impact": "Automation failed; manual read needed.",
                    "recommended_actions": ["Read the email manually"], "notify": ["supply_chain"],
                    "support_impact": {"skus": [], "proactive_notice_suggested": False},
                    "draft_followup_to_vendor": "", "confidence": 0.0}, {}
        r = floor_shipment(r, facts)
        return {"results": {"SHIPMENT": r}, "verified_facts": facts, "usage": _usage("shipment", u),
                "context": {"SHIPMENT": {"open_pos": open_pos, "inventory": inventory}}}

    # ---------------------------------------------------------------- triage / quarantine (no LLM)
    def triage(state: dict) -> dict:
        return {"results": {"TRIAGE": {"decision": "escalate", "reason": "could not classify item", "confidence": 1.0}}}

    def quarantine(state: dict) -> dict:
        d.ops.put_flag(state["item_id"], "possible_injection", "quarantined before reaching any specialist")
        return {"results": {"TRIAGE": {"decision": "escalate", "quarantined": True, "confidence": 1.0,
                                       "reason": "prompt-injection signal; content quarantined"}}}

    # ---------------------------------------------------------------- verifier
    def verifier(state: dict) -> dict:
        results = state.get("results", {})
        ctx = state.get("context", {})
        flags = list(state.get("guardrail_flags", []))
        issues: list[dict] = []
        force, reason, llm_failed = False, "", False
        updates: dict[str, dict] = {}
        kb_text = " ".join(c["text"] for c in ctx.get("SUPPORT", {}).get("kb_results", []))

        for mod, r in results.items():
            if mod not in ("SUPPORT", "SHIPMENT", "INVOICE"):
                continue
            r = dict(r)
            code: list[dict] = []
            if mod == "SUPPORT":
                for p in lint_external(r.get("draft_reply", ""), kb_text):
                    code.append({"check": "lint", "detail": p, "fix": "remove or escalate"})
                known = set(ctx.get("SUPPORT", {}).get("kb_doc_ids", []))
                bad = [c for c in r.get("doc_citations", []) if c not in known]
                if bad:
                    code.append({"check": "citations", "detail": f"unknown doc ids {bad}", "fix": "cite retrieved docs only"})
                if r.get("decision") == "draft_reply" and not r.get("doc_citations"):
                    code.append({"check": "citations", "detail": "draft has no policy citation", "fix": "cite or escalate"})
                out, gflags = d.guard.check_output(r.get("draft_reply", ""), grounding=kb_text, query=state.get("raw_input", ""))
                r["draft_reply"] = out
                if set(gflags) & {"UNGROUNDED", "DENIED_TOPIC", "CONTENT_FILTERED"}:
                    code.append({"check": "guardrail_output", "detail": ",".join(gflags), "fix": "escalate"})
                flags += [f for f in gflags if f not in flags]
            elif mod == "SHIPMENT":
                for p in lint_external(r.get("draft_followup_to_vendor", "")):
                    code.append({"check": "lint", "detail": p, "fix": "remove"})
                out, gflags = d.guard.check_output(r.get("draft_followup_to_vendor", ""))
                r["draft_followup_to_vendor"] = out
                flags += [f for f in gflags if f not in flags]
            updates[mod] = r
            issues += [{**i, "module": mod} for i in code]
            if code:
                force, reason = True, reason or "draft failed deterministic checks"

            sources = {"result": r, "context": ctx.get(mod, {}), "verified_facts": state.get("verified_facts", {})}
            user = "\n".join([_xml("module", mod), _xml("specialist_result", r), _xml("sources", sources),
                              _xml("guardrail_flags", flags)])
            try:
                v, _u = _call(d, "verifier", state, user, {"module": mod, **sources, "text": state.get("raw_input", "")}, None)
            except LLMError as e:
                v = {"pass": False, "issues": [{"check": "verifier", "detail": str(e), "fix": "human review"}],
                     "force_escalate": True, "escalate_reason": "verifier unavailable"}
            issues += [{**i, "module": mod} for i in v.get("issues", [])]
            llm_failed |= not v.get("pass", False)
            if v.get("force_escalate"):
                force, reason = True, v.get("escalate_reason") or reason or "verifier forced escalation"

        return {"results": updates, "guardrail_flags": flags,
                "verifier": {"pass": not issues and not llm_failed and not force, "issues": issues,
                             "force_escalate": force, "escalate_reason": reason}}

    # ---------------------------------------------------------------- gate / human / finalize
    def gate_node(state: dict) -> dict:
        g = run_gate(state, s)
        return {"gate": g}

    def after_gate(state: dict) -> str:
        return "finalize" if state["gate"]["action"] == "auto_draft" else "queue_item"

    def human_review(state: dict) -> dict:
        decision = interrupt({"item_id": state["item_id"], "gate": state["gate"],
                              "results": state.get("results", {}), "route": state.get("route", {})})
        return {"human_decision": decision}

    from .actions import finalize_item, queue_item  # late import: actions imports from nodes' helpers

    return {
        "preprocess": preprocess, "router": router, "support": support, "invoice": invoice, "shipment": shipment,
        "triage": triage, "quarantine": quarantine, "verifier": verifier, "gate": gate_node,
        "queue_item": lambda st: queue_item(st, d), "human_review": human_review,
        "finalize": lambda st: finalize_item(st, d),
        "_after_router": after_router, "_after_preprocess": after_preprocess, "_after_gate": after_gate,
    }


# ------------------------------------------------------------------------------ deterministic floors
def floor_invoice(r: dict, f: dict) -> dict:
    """The model may be stricter than code, never looser (OK_TO_PAY only if every check passes)."""
    problems = []
    if f.get("bank_details_match") is False:
        problems.append("bank details mismatch")
    if f.get("duplicate_candidates"):
        problems.append("duplicate candidate")
    if not f.get("vendor_known") or not f.get("vendor_identity_match"):
        problems.append("vendor identity not confirmed")
    if not f.get("po_found") or not f.get("grn_found"):
        problems.append("PO/GRN missing")
    if f.get("grand_total_ok") is not True or f.get("line_math_ok") is False or f.get("tax_math_ok") is False:
        problems.append("arithmetic not verified")
    if not f.get("three_way_ok"):
        problems.append("three-way match failed")
    if f.get("unverifiable_key_fields"):
        problems.append("low-confidence key field")
    if problems and r.get("recommendation") == "OK_TO_PAY":
        r = {**r, "recommendation": "HOLD_FOR_REVIEW", "overall_risk": r.get("overall_risk") if r.get("overall_risk") != "low" else "medium",
             "reviewer_summary": (r.get("reviewer_summary", "") + f" [code floor: {', '.join(problems)}]").strip()}
    if any(x["match"] == "same_invoice_no" for x in f.get("duplicate_candidates", [])):
        r = {**r, "recommendation": "REJECT"}
    return r


def floor_shipment(r: dict, f: dict) -> dict:
    """Severity can only be raised by code, never lowered (vague/hedging language is never GREEN)."""
    rank = {"GREEN": 0, "AMBER": 1, "RED": 2}
    floor = "GREEN"
    if (f.get("days_past_need_by") or 0) > 0 or f.get("sale_event_overlap"):
        floor = "RED"
    elif r.get("signals") or (f.get("slip_days") or 0) > 0 or not f.get("matched_pos"):
        floor = "AMBER"  # unmatched PO = needs a manual match
    if rank[r.get("severity", "AMBER")] < rank[floor]:
        r = {**r, "severity": floor}
    return r
