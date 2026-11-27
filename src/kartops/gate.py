"""The code gate. Enforced in code, outside the model: the LLM cannot argue its way around it."""
from __future__ import annotations

from .config import Settings
from .schemas import SUPPORT_QUEUES

ESCALATE, HOLD, AUTO = "escalate", "hold", "auto_draft"


def _d(action: str, queue: str | None, reason: str, severity: str = "HIGH") -> dict:
    return {"action": action, "queue": queue, "reason": reason, "severity": severity}


def result_confidence(results: dict) -> float:
    confs = [r.get("confidence", 0.0) for r in results.values() if isinstance(r, dict)]
    return min(confs) if confs else 0.0


def queue_for(results: dict) -> str:
    if "INVOICE" in results:
        return "ap_review"
    if "SUPPORT" in results:
        q = results["SUPPORT"].get("escalation_queue")
        return q if q in SUPPORT_QUEUES else "support_l2"
    if "SHIPMENT" in results:
        return "supply_chain"
    return "human_triage"


def gate(state: dict, s: Settings) -> dict:
    route = state.get("route") or {}
    flags = set(route.get("early_flags", [])) | set(state.get("guardrail_flags", []))
    results = state.get("results") or {}
    vf = state.get("verified_facts") or {}
    vr = state.get("verifier") or {"pass": True, "force_escalate": False}

    # 1. security first
    if "possible_injection" in flags or "PROMPT_ATTACK" in flags:
        return _d(ESCALATE, "fraud_risk", "prompt-injection / prompt-attack signal", "CRITICAL")
    # 2. payment safety (hard override: bank details differ from vendor master)
    if vf.get("bank_details_match") is False:
        return _d(HOLD, "ap_review", "bank details differ from vendor master", "CRITICAL")
    if vf.get("duplicate_candidates") and "INVOICE" in results:
        return _d(HOLD, "ap_review", "duplicate invoice candidate", "CRITICAL")
    # 3. early signals from router
    if "fraud_signal" in flags:
        return _d(ESCALATE, "fraud_risk", "fraud signal")
    if "legal_threat" in flags:
        return _d(ESCALATE, "legal_compliance", "legal/regulatory threat")
    if "safety" in flags:
        return _d(ESCALATE, "support_l2", "safety report", "CRITICAL")
    if "TRIAGE" in results:
        return _d(ESCALATE, "human_triage", results["TRIAGE"].get("reason", "unclassifiable"), "LOW")
    # 4. invoice: anything but OK_TO_PAY is held for humans
    if "INVOICE" in results and results["INVOICE"].get("recommendation") != "OK_TO_PAY":
        return _d(HOLD, "ap_review", f"invoice recommendation {results['INVOICE'].get('recommendation')}")
    # 5. independent verifier
    if vr.get("force_escalate") or not vr.get("pass", True):
        return _d(ESCALATE, "support_l2" if "SUPPORT" in results else queue_for(results),
                  vr.get("escalate_reason") or "verifier failed")
    # 6. specialist decided to escalate
    sup = results.get("SUPPORT")
    if sup and sup.get("decision") == "escalate":
        return _d(ESCALATE, queue_for(results), "; ".join(sup.get("escalation_reasons", [])) or "support escalation")
    # 7. confidence / retrieval quality / staleness
    if result_confidence(results) < s.threshold:
        return _d(ESCALATE, queue_for(results), f"confidence below {s.threshold}")
    if sup:
        if vf.get("kb_top_score", 1) < s.min_score:
            return _d(ESCALATE, "support_l2", "weak knowledge-base match")
        if vf.get("kb_stale"):
            return _d(ESCALATE, "support_l2", "stale policy document")
        if vf.get("requester_matches_order") is False:
            return _d(ESCALATE, "fraud_risk", "requester does not match order")
        if vf.get("vip") or vf.get("contact_count_30d", 0) >= 3:
            return _d(ESCALATE, "support_l2", "VIP or repeat contact")
    # 8. supply chain: RED needs a human today
    ship = results.get("SHIPMENT")
    if ship and vf.get("sender_verified") is False:
        return _d(ESCALATE, "supply_chain", "shipment email from an unrecognised sender (possible phishing)")
    if ship and ship.get("severity") == "RED":
        return _d(ESCALATE, "supply_chain", "RED shipment risk")
    if route.get("priority") == "P1":
        return _d(ESCALATE, queue_for(results), "P1 priority")
    return {"action": AUTO, "queue": None, "reason": "all checks passed; draft only", "severity": "LOW"}
