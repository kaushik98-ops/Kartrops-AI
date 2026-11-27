"""Rule-based stand-in for Bedrock so the graph, gate and effectors run offline and in CI.
It produces schema-valid outputs from the structured payload. It tests PLUMBING, not model quality."""
from __future__ import annotations

import re


class FakeLLM:
    def run(self, *, node, base, node_prompt, user_text, payload, tools, schema):
        out = getattr(self, f"_{node}")(payload)
        return out, {"input": 0, "output": 0, "model": "fake"}

    # ------------------------------------------------------------------ router
    def _router(self, p):
        t = (p.get("text") or "").lower()
        ent = p.get("entities") or {}
        flags = []
        if re.search(r"consumer court|legal notice|lawyer|sue ", t):
            flags.append("legal_threat")
        if re.search(r"unauthori[sz]ed|hacked|account takeover", t):
            flags.append("fraud_signal")
        if re.search(r"allerg|injur|burn|smoke|expired", t):
            flags.append("safety")
        if re.search(r"ignore (all )?(previous|prior)|system prompt|approve (the )?payment", t):
            flags.append("possible_injection")
        if not t.strip() and not p.get("has_attachment"):
            primary = "TRIAGE"
        elif p.get("channel") == "invoice_doc" or ent.get("invoice_no") or "invoice" in t:
            primary = "INVOICE"
        elif p.get("channel") == "email" or re.search(r"\b(eta|dispatch|backorder|customs|partial shipment)\b", t):
            primary = "SHIPMENT"
        else:
            primary = "SUPPORT"
        prio = "P1" if flags else ("P2" if re.search(r"refund|angry|payment|sale", t) else "P3")
        return {"primary": primary, "secondary": [], "entities": ent, "language": p.get("language", "en"),
                "priority": prio, "early_flags": flags, "confidence": 0.9}

    # ------------------------------------------------------------------ support
    def _support(self, p):
        vf, kb, order = p["verified_facts"], p["kb_results"], p.get("order_context") or {}
        t = (p.get("text") or "").lower()
        reasons, queue = [], ""
        if re.search(r"refund|double charge|charged twice|chargeback|payment (failed|deducted)", t):
            reasons.append("money movement dispute"); queue = "payments_refunds"
        if re.search(r"not received|fake delivery|marked delivered", t):
            reasons.append("delivered but not received"); queue = queue or "support_l2"
        if re.search(r"angry|furious|worst|unacceptable", t):
            reasons.append("angry customer"); queue = queue or "support_l2"
        if vf["kb_top_score"] < 0.55 or vf["kb_stale"] or not kb:
            reasons.append("policy missing or stale"); queue = queue or "support_l2"
        if vf.get("requester_matches_order") is False:
            reasons.append("requester does not match order"); queue = "fraud_risk"
        status = (order.get("tracking_status") or order.get("status") or "being processed")
        cite = kb[0]["doc_id"] if kb else ""
        if reasons:
            return {"decision": "escalate", "category": "other", "sentiment": "neutral",
                    "draft_reply": "Thanks for reaching out. We have passed your request to a specialist "
                                   "who will follow up with you.",
                    "doc_citations": [], "escalation_queue": queue or "support_l2",
                    "escalation_reasons": reasons, "internal_note": f"order {order.get('order_id')}; " + "; ".join(reasons),
                    "confidence": 0.9}
        return {"decision": "draft_reply", "category": "order_status", "sentiment": "neutral",
                "draft_reply": f"Hi, thanks for reaching out. Your order is currently: {status}. "
                               "You can track it any time from the My Orders page.",
                "doc_citations": [cite], "escalation_queue": "", "escalation_reasons": [],
                "internal_note": f"checked order {order.get('order_id')}; kb {cite}", "confidence": 0.88}

    # ------------------------------------------------------------------ invoice
    def _invoice(self, p):
        vf, findings = p["verified_facts"], []

        def add(check, sev, inv, exp, src, expl):
            findings.append({"check": check, "severity": sev, "invoice_value": str(inv), "expected_value": str(exp),
                             "source": src, "explanation": expl})

        if vf.get("bank_details_match") is False:
            add("bank_details_match", "CRITICAL", vf["bank_last4"]["invoice"], vf["bank_last4"]["vendor_master"],
                "vendor_master", "Bank details differ from the vendor master record.")
        if vf.get("duplicate_candidates"):
            d = vf["duplicate_candidates"][0]
            add("duplicate", "CRITICAL", d["invoice_no"], "no earlier payment", "history", f"Matches earlier invoice ({d['match']}).")
        if not vf.get("vendor_known"):
            add("vendor_identity", "CRITICAL", "unknown", "known vendor", "vendor_master", "Vendor not found.")
        if not vf.get("po_found") or not vf.get("grn_found"):
            add("po_grn", "CRITICAL", "missing", "PO and GRN", "po", "No PO or GRN to match against.")
        for k in ("line_math_ok", "tax_math_ok", "grand_total_ok"):
            if vf.get(k) is False:
                add(k, "HIGH", "mismatch", "arithmetic consistent", "invoice", f"{k} failed.")
        for r in vf.get("three_way_match", []):
            if r["status"] != "ok":
                add("three_way_match", "HIGH", f"{r['sku']} qty {r['inv_qty']} @ {r['inv_price']}",
                    f"po {r['po_qty']} @ {r['po_price']}, grn {r['grn_qty']}", "po", r["status"])
        unver = vf.get("unverifiable_key_fields", [])
        dup_confirmed = any(d["match"] == "same_invoice_no" for d in vf.get("duplicate_candidates", []))
        crit = any(f["severity"] == "CRITICAL" for f in findings)
        rec = "REJECT" if dup_confirmed else ("HOLD_FOR_REVIEW" if findings or unver else "OK_TO_PAY")
        return {"recommendation": rec, "overall_risk": "critical" if crit else ("high" if findings else "low"),
                "findings": findings, "unverifiable_fields": unver,
                "suggested_action": "Call the vendor on the number in vendor master." if findings else "Proceed to approval.",
                "reviewer_summary": f"{len(findings)} finding(s).", "confidence": 0.9}

    # ------------------------------------------------------------------ shipment
    def _shipment(self, p):
        vf, t = p["verified_facts"], (p.get("text") or "")
        phrases = re.findall(r"(working to confirm|slight adjustment|pending (?:inspection|approval)|customs hold|"
                             r"capacity constraints?|force majeure|will update soon|delay\w*|backorder\w*)", t, re.IGNORECASE)
        signals = [{"phrase": x, "type": "explicit" if re.match(r"delay|backorder", x, re.IGNORECASE) else "implicit",
                    "meaning": "possible delay"} for x in phrases]
        over = vf.get("days_past_need_by") or 0
        crit = any(r.get("criticality") == "critical" for r in vf.get("per_po", []))
        if over > 0 or vf.get("sale_event_overlap") or (crit and signals):
            sev = "RED"
        elif signals or (vf.get("slip_days") or 0) > 0:
            sev = "AMBER"
        else:
            sev = "GREEN"
        skus = sorted({s for r in vf.get("per_po", []) for s in r.get("skus", [])})
        return {"severity": sev, "matched_pos": vf.get("matched_pos", []), "signals": signals,
                "new_eta": vf.get("new_eta", "unknown"), "slip_days": vf.get("slip_days") or 0,
                "days_past_need_by": over, "business_impact": f"{sev} risk for {', '.join(skus) or 'unmatched PO'}.",
                "recommended_actions": ["Request a firm ship date and root cause."], "notify": ["supply_chain"],
                "support_impact": {"skus": skus, "proactive_notice_suggested": sev != "GREEN"},
                "draft_followup_to_vendor": "Thanks for the update. Could you share a confirmed ship date and the "
                                            "reason for the change so we can plan accordingly?",
                "confidence": 0.85}

    # ------------------------------------------------------------------ verifier
    def _verifier(self, p):
        return {"pass": True, "issues": [], "force_escalate": False, "escalate_reason": ""}
