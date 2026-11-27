"""JSON Schemas for each node's `submit_result` tool call (Bedrock Converse tool use)."""
from __future__ import annotations

_CONF = {"type": "number", "minimum": 0, "maximum": 1}

ROUTER = {
    "type": "object",
    "required": ["primary", "secondary", "entities", "language", "priority", "early_flags", "confidence"],
    "properties": {
        "primary": {"enum": ["SUPPORT", "INVOICE", "SHIPMENT", "TRIAGE"]},
        "secondary": {"type": "array", "items": {"enum": ["SUPPORT", "INVOICE", "SHIPMENT"]}},
        "entities": {"type": "object"},
        "language": {"type": "string"},
        "priority": {"enum": ["P1", "P2", "P3"]},
        "early_flags": {
            "type": "array",
            "items": {"enum": ["possible_injection", "fraud_signal", "legal_threat", "safety"]},
        },
        "confidence": _CONF,
    },
}

SUPPORT = {
    "type": "object",
    "required": ["decision", "category", "sentiment", "draft_reply", "doc_citations",
                 "escalation_queue", "escalation_reasons", "internal_note", "confidence"],
    "properties": {
        "decision": {"enum": ["draft_reply", "escalate"]},
        "category": {"type": "string"},
        "sentiment": {"type": "string"},
        "draft_reply": {"type": "string"},
        "doc_citations": {"type": "array", "items": {"type": "string"}},
        "escalation_queue": {"type": "string"},
        "escalation_reasons": {"type": "array", "items": {"type": "string"}},
        "internal_note": {"type": "string"},
        "confidence": _CONF,
    },
}

INVOICE = {
    "type": "object",
    "required": ["recommendation", "overall_risk", "findings", "unverifiable_fields",
                 "suggested_action", "reviewer_summary", "confidence"],
    "properties": {
        "recommendation": {"enum": ["OK_TO_PAY", "HOLD_FOR_REVIEW", "REJECT"]},
        "overall_risk": {"enum": ["low", "medium", "high", "critical"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["check", "severity", "invoice_value", "expected_value", "source", "explanation"],
                "properties": {
                    "check": {"type": "string"},
                    "severity": {"enum": ["CRITICAL", "HIGH", "LOW"]},
                    "invoice_value": {"type": "string"},
                    "expected_value": {"type": "string"},
                    "source": {"type": "string"},
                    "explanation": {"type": "string"},
                },
            },
        },
        "unverifiable_fields": {"type": "array", "items": {"type": "string"}},
        "suggested_action": {"type": "string"},
        "reviewer_summary": {"type": "string"},
        "confidence": _CONF,
    },
}

SHIPMENT = {
    "type": "object",
    "required": ["severity", "matched_pos", "signals", "new_eta", "slip_days", "days_past_need_by",
                 "business_impact", "recommended_actions", "notify", "support_impact",
                 "draft_followup_to_vendor", "confidence"],
    "properties": {
        "severity": {"enum": ["RED", "AMBER", "GREEN"]},
        "matched_pos": {"type": "array", "items": {"type": "string"}},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["phrase", "type", "meaning"],
                "properties": {
                    "phrase": {"type": "string"},
                    "type": {"enum": ["explicit", "implicit"]},
                    "meaning": {"type": "string"},
                },
            },
        },
        "new_eta": {"type": "string"},
        "slip_days": {"type": "integer"},
        "days_past_need_by": {"type": "integer"},
        "business_impact": {"type": "string"},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "notify": {"type": "array", "items": {"type": "string"}},
        "support_impact": {
            "type": "object",
            "required": ["skus", "proactive_notice_suggested"],
            "properties": {
                "skus": {"type": "array", "items": {"type": "string"}},
                "proactive_notice_suggested": {"type": "boolean"},
            },
        },
        "draft_followup_to_vendor": {"type": "string"},
        "confidence": _CONF,
    },
}

VERIFIER = {
    "type": "object",
    "required": ["pass", "issues", "force_escalate", "escalate_reason"],
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["check", "detail", "fix"],
                "properties": {"check": {"type": "string"}, "detail": {"type": "string"}, "fix": {"type": "string"}},
            },
        },
        "force_escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
    },
}

BY_NODE = {
    "router": ROUTER, "support": SUPPORT, "invoice": INVOICE,
    "shipment": SHIPMENT, "verifier": VERIFIER,
}

SUPPORT_QUEUES = ["payments_refunds", "support_l2", "fraud_risk", "legal_compliance", "seller_support"]
