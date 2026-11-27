"""Input/output guardrails. aws mode -> Bedrock ApplyGuardrail. local mode -> regex stand-in
(good enough for tests; NOT a substitute for the managed service)."""
from __future__ import annotations

import re

from .config import Settings

_PII = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"), "[AADHAAR]"),
]
_ATTACK = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules)|disregard (the )?(rules|instructions)|"
    r"you are now|system prompt|reveal (your|the) (prompt|instructions)|"
    r"(approve|release|process) (the )?payment (immediately|now)|mark (this )?(as )?resolved|"
    r"override (the )?(policy|rules)|developer mode|jailbreak", re.IGNORECASE)


def local_mask(text: str) -> tuple[str, bool]:
    changed = False
    for rx, token in _PII:
        text, n = rx.subn(token, text)
        changed |= n > 0
    return text, changed


class Guardrails:
    def __init__(self, s: Settings):
        self.s = s
        self.client = None
        if s.is_aws and s.guardrail_id:
            import boto3
            self.client = boto3.client("bedrock-runtime", region_name=s.region)

    # ------------------------------------------------------------------ input
    def check_input(self, text: str) -> tuple[str, list[str]]:
        if not self.client:
            masked, changed = local_mask(text)
            flags = (["PII_MASKED"] if changed else []) + (["PROMPT_ATTACK"] if _ATTACK.search(text) else [])
            return masked, flags
        resp = self._apply("INPUT", [{"text": {"text": text}}])
        flags = self._flags(resp)
        if "PROMPT_ATTACK" in flags:
            masked, _ = local_mask(text)  # content is quarantined and never reaches an LLM
            return masked, flags
        if resp.get("action") == "GUARDRAIL_INTERVENED" and resp.get("outputs"):
            return resp["outputs"][0]["text"], flags
        return text, flags

    # ------------------------------------------------------------------ output
    def check_output(self, draft: str, grounding: str | None = None, query: str | None = None) -> tuple[str, list[str]]:
        if not draft:
            return draft, []
        if not self.client:
            masked, changed = local_mask(draft)
            return masked, (["PII_MASKED"] if changed else [])
        resp = self._apply("OUTPUT", [{"text": {"text": draft}}])
        flags = self._flags(resp)
        out = draft
        if resp.get("action") == "GUARDRAIL_INTERVENED" and resp.get("outputs"):
            out = resp["outputs"][0]["text"]
        if grounding and query:  # contextual grounding check (separate call: its outputs are not reusable text)
            g = self._apply("OUTPUT", [
                {"text": {"text": grounding[:20000], "qualifiers": ["grounding_source"]}},
                {"text": {"text": query[:2000], "qualifiers": ["query"]}},
                {"text": {"text": draft, "qualifiers": ["guard_content"]}},
            ])
            flags += [f for f in self._flags(g) if f not in flags]
        return out, flags

    # ------------------------------------------------------------------ internals
    def _apply(self, source: str, content: list[dict]) -> dict:
        return self.client.apply_guardrail(guardrailIdentifier=self.s.guardrail_id,
                                           guardrailVersion=self.s.guardrail_version,
                                           source=source, content=content)

    @staticmethod
    def _flags(resp: dict) -> list[str]:
        flags: list[str] = []
        for a in resp.get("assessments", []):
            for f in (a.get("contentPolicy") or {}).get("filters", []):
                if f.get("type") == "PROMPT_ATTACK" and f.get("detected", True):
                    flags.append("PROMPT_ATTACK")
                elif f.get("detected", True):
                    flags.append("CONTENT_FILTERED")
            if (a.get("topicPolicy") or {}).get("topics"):
                flags.append("DENIED_TOPIC")
            sip = a.get("sensitiveInformationPolicy") or {}
            if sip.get("piiEntities") or sip.get("regexes"):
                flags.append("PII_MASKED")
            for f in (a.get("contextualGroundingPolicy") or {}).get("filters", []):
                if f.get("action") == "BLOCKED":
                    flags.append("UNGROUNDED")
        return list(dict.fromkeys(flags))
