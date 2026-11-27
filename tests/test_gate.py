from kartops.gate import gate


def st(**kw):
    base = {"route": {"early_flags": [], "priority": "P3"}, "guardrail_flags": [], "results": {},
            "verified_facts": {}, "verifier": {"pass": True, "force_escalate": False}}
    base.update(kw)
    return base


def test_injection_wins(settings):
    g = gate(st(route={"early_flags": ["possible_injection"]}, results={"INVOICE": {"recommendation": "OK_TO_PAY", "confidence": 1}}), settings)
    assert (g["action"], g["queue"]) == ("escalate", "fraud_risk")


def test_bank_mismatch_hold_even_if_model_says_ok(settings):
    g = gate(st(results={"INVOICE": {"recommendation": "OK_TO_PAY", "confidence": 0.99}},
                verified_facts={"bank_details_match": False}), settings)
    assert g["action"] == "hold" and g["severity"] == "CRITICAL"


def test_invoice_not_ok_is_held(settings):
    g = gate(st(results={"INVOICE": {"recommendation": "HOLD_FOR_REVIEW", "confidence": 0.9}}), settings)
    assert g["action"] == "hold"


def test_low_confidence_escalates(settings):
    g = gate(st(results={"SUPPORT": {"decision": "draft_reply", "confidence": 0.5}}), settings)
    assert g["action"] == "escalate"


def test_verifier_failure_escalates(settings):
    g = gate(st(results={"SUPPORT": {"decision": "draft_reply", "confidence": 0.9}},
                verifier={"pass": False, "force_escalate": True, "escalate_reason": "promise"}), settings)
    assert g["action"] == "escalate"


def test_clean_support_auto_drafts(settings):
    g = gate(st(results={"SUPPORT": {"decision": "draft_reply", "confidence": 0.9}},
                verified_facts={"kb_top_score": 0.9, "kb_stale": False, "contact_count_30d": 0}), settings)
    assert g["action"] == "auto_draft"


def test_stale_or_weak_kb(settings):
    r = {"SUPPORT": {"decision": "draft_reply", "confidence": 0.9}}
    assert gate(st(results=r, verified_facts={"kb_top_score": 0.9, "kb_stale": True}), settings)["action"] == "escalate"
    assert gate(st(results=r, verified_facts={"kb_top_score": 0.2, "kb_stale": False}), settings)["action"] == "escalate"
