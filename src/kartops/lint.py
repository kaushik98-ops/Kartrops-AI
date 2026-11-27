"""Deterministic lint for external-facing drafts (belt-and-braces next to the LLM verifier)."""
from __future__ import annotations

import re

_PROMISE = re.compile(
    r"\bwe (will|'ll|shall) (refund|credit|reimburse|compensate|replace|pay)\b|\bwe guarantee\b|\bwe promise\b|"
    r"\b(refund|credit|payment) (will|shall) be (issued|processed|released|made|credited)\b|"
    r"\bwithin \d+\s*(hours?|days?|business days?)\b|\bit was our (fault|mistake)\b|\bwe (are|were) at fault\b|"
    r"\bwe accept (full )?(liability|responsibility)\b", re.IGNORECASE)
_INTERNAL = re.compile(r"\b(dynamodb|langgraph|bedrock|textract|kartops|lambda|step functions|fraud_risk|support_l2|"
                       r"payments_refunds|ap_review|internal_note|verified_facts|guardrail)\b", re.IGNORECASE)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def lint_external(text: str, allowed_text: str = "") -> list[str]:
    """Return issue strings. `allowed_text` = retrieved policy text: a time window that appears
    verbatim there is considered authorised."""
    issues = []
    for m in _PROMISE.finditer(text or ""):
        if m.group(0).lower() not in (allowed_text or "").lower():
            issues.append(f"possible commitment/admission: '{m.group(0)}'")
    for m in _INTERNAL.finditer(text or ""):
        issues.append(f"internal term in external draft: '{m.group(0)}'")
    if _CARD.search(text or ""):
        issues.append("unmasked card/account-like number in external draft")
    return issues
