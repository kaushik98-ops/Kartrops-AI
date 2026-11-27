"""Deterministic 'verified facts'. The LLM never recomputes these (BASE prompt rule) and the
code gate reads them directly. Pure functions -> easy to unit test."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as dparser

from .config import Settings
from .data import kb_is_stale


# ----------------------------------------------------------------------------- helpers
def num(x: Any) -> float | None:
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^\d.\-]", "", str(x).replace(",", ""))
    try:
        return float(s)
    except ValueError:
        return None


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _close(a: float | None, b: float | None, tol: float) -> bool | None:
    if a is None or b is None:
        return None
    return abs(a - b) <= tol


def _mask(s: Any) -> str:
    n = _norm(s)
    return f"XXXX{n[-4:]}" if len(n) >= 4 else ("" if not n else "XXXX")


def _d(s: Any) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        try:
            return dparser.parse(str(s), dayfirst=True).date()
        except (ValueError, OverflowError):
            return None


# ----------------------------------------------------------------------------- invoice
KEY_FIELDS = ("bank_account", "grand_total", "vendor_gstin", "vendor_name", "po_ref", "invoice_no")


def invoice_facts(ex: dict, po: dict | None, grn: dict | None, vendor: dict | None,
                  history: list[dict], s: Settings) -> dict:
    tol = s.amount_tol_abs
    lines = ex.get("lines") or []

    # --- arithmetic
    line_checks = []
    for ln in lines:
        q, u, t = num(ln.get("qty")), num(ln.get("unit_price")), num(ln.get("line_total"))
        line_checks.append(_close(q * u if q is not None and u is not None else None, t, tol))
    line_math_ok = None if not line_checks or all(c is None for c in line_checks) else all(c for c in line_checks if c is not None)

    line_sum = sum(num(l.get("line_total")) or 0 for l in lines) if lines else None
    subtotal = num(ex.get("subtotal")) if ex.get("subtotal") is not None else line_sum
    subtotal_ok = _close(line_sum, num(ex.get("subtotal")), tol) if ex.get("subtotal") is not None and lines else None

    tax = num(ex.get("tax"))
    line_taxes = [num(l.get("tax")) for l in lines]
    if tax is not None and lines and all(t is not None for t in line_taxes):
        tax_math_ok = _close(sum(line_taxes), tax, tol)
    elif tax is not None and subtotal is not None and num(ex.get("tax_rate")) is not None:
        tax_math_ok = _close(subtotal * num(ex["tax_rate"]) / 100, tax, tol)
    else:
        tax_math_ok = None

    expected_total = None
    if subtotal is not None:
        expected_total = subtotal + (tax or 0) + (num(ex.get("shipping")) or 0) - (num(ex.get("discount")) or 0)
    grand_total_ok = _close(expected_total, num(ex.get("grand_total")), tol)

    # --- bank details (never expose full numbers to the LLM)
    bank_match = None
    inv_acct, inv_ifsc = _norm(ex.get("bank_account")), _norm(ex.get("ifsc"))
    if vendor and (inv_acct or inv_ifsc):
        m_acct, m_ifsc = _norm(vendor.get("bank_account")), _norm(vendor.get("ifsc"))
        checks = []
        if inv_acct:
            checks.append(inv_acct == m_acct)
        if inv_ifsc:
            checks.append(inv_ifsc == m_ifsc)
        bank_match = all(checks)

    # --- vendor identity
    vident = None
    if vendor:
        gst = None
        if ex.get("vendor_gstin"):
            gst = _norm(ex["vendor_gstin"]) == _norm(vendor.get("gstin"))
        name = _norm(ex.get("vendor_name")) == _norm(vendor.get("name")) if ex.get("vendor_name") else None
        vident = {"gstin_match": gst, "name_match": name,
                  "ok": (gst is not False) and (name is not False) and (gst is not None or name is not None)}
    vendor_identity_match = vident["ok"] if vident else False

    # --- duplicates
    dups = []
    inv_no = _norm(ex.get("invoice_no"))
    inv_date, total = _d(ex.get("invoice_date")), num(ex.get("grand_total"))
    for h in history:
        if inv_no and _norm(h.get("invoice_no")) == inv_no:
            dups.append({"invoice_no": h.get("invoice_no"), "total": h.get("total"),
                         "invoice_date": h.get("invoice_date"), "status": h.get("status"), "match": "same_invoice_no"})
        elif total is not None and _close(num(h.get("total")), total, tol) and inv_date and _d(h.get("invoice_date")) \
                and abs((inv_date - _d(h.get("invoice_date"))).days) <= 7:
            dups.append({"invoice_no": h.get("invoice_no"), "total": h.get("total"),
                         "invoice_date": h.get("invoice_date"), "status": h.get("status"),
                         "match": "same_amount_within_7_days"})

    # --- three-way match
    po_lines = {_norm(l.get("sku")): l for l in (po or {}).get("lines", [])}
    grn_lines = {_norm(l.get("sku")): l for l in (grn or {}).get("lines", [])}
    twm = []
    for ln in lines:
        k = _norm(ln.get("sku"))
        pl, gl = po_lines.get(k), grn_lines.get(k)
        iq, ip = num(ln.get("qty")), num(ln.get("unit_price"))
        row: dict[str, Any] = {"sku": ln.get("sku"), "inv_qty": iq, "inv_price": ip,
                               "po_qty": num(pl.get("qty")) if pl else None,
                               "po_price": num(pl.get("unit_price")) if pl else None,
                               "grn_qty": num(gl.get("qty_received")) if gl else None}
        if not pl:
            row["status"] = "no_po_line"
        else:
            row["price_delta_pct"] = round((ip - row["po_price"]) / row["po_price"] * 100, 2) \
                if ip is not None and row["po_price"] else None
            allowed_qty = (row["grn_qty"] if row["grn_qty"] is not None else 0) * (1 + s.qty_tol_pct / 100)
            if not gl or iq is None or iq > allowed_qty + 1e-9:
                row["status"] = "qty_exceeds_grn"
            elif row["po_qty"] is not None and iq > row["po_qty"] * (1 + s.qty_tol_pct / 100) + 1e-9:
                row["status"] = "qty_exceeds_po"
            elif row["price_delta_pct"] is not None and abs(row["price_delta_pct"]) > s.price_tol_pct:
                row["status"] = "price_out_of_tolerance"
            else:
                row["status"] = "ok"
        twm.append(row)

    # --- terms
    terms = (vendor or {}).get("payment_terms_days")
    expected_due = (inv_date + timedelta(days=int(terms))) if inv_date and terms is not None else None
    due = _d(ex.get("due_date"))
    due_ok = None if not (expected_due and due) else (due >= expected_due)

    conf = ex.get("confidence") or {}
    low_conf = sorted(k for k, v in conf.items() if v is not None and v < s.field_conf_min)

    return {
        "bank_details_match": bank_match,
        "bank_last4": {"invoice": _mask(ex.get("bank_account")), "vendor_master": _mask((vendor or {}).get("bank_account"))},
        "duplicate_candidates": dups,
        "line_math_ok": line_math_ok,
        "subtotal_ok": subtotal_ok,
        "tax_math_ok": tax_math_ok,
        "grand_total_ok": grand_total_ok,
        "expected_grand_total": round(expected_total, 2) if expected_total is not None else None,
        "po_found": po is not None,
        "grn_found": grn is not None,
        "three_way_match": twm,
        "three_way_ok": bool(po and grn and twm and all(r["status"] == "ok" for r in twm)),
        "vendor_known": vendor is not None,
        "vendor_identity": vident,
        "vendor_identity_match": vendor_identity_match,
        "due_date_terms": {"invoice_date": str(inv_date) if inv_date else None, "due_date": str(due) if due else None,
                           "terms_days": terms, "expected_due": str(expected_due) if expected_due else None,
                           "ok": due_ok},
        "low_confidence_fields": low_conf,
        "unverifiable_key_fields": [f for f in low_conf if f in KEY_FIELDS],
    }


def mask_extraction_for_llm(ex: dict) -> dict:
    """The LLM sees the extraction with the bank account masked to last-4."""
    out = dict(ex)
    if out.get("bank_account"):
        out["bank_account"] = _mask(out["bank_account"])
    return out


# ----------------------------------------------------------------------------- shipment
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    rf"|\b\d{{1,2}}[/.]\d{{1,2}}[/.]\d{{2,4}}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+{_MONTH}\.?(?:,?[\s\-]+\d{{4}})?\b"
    rf"|\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.IGNORECASE)
_ETA_WORDS = re.compile(r"\b(eta|ship|shipp|dispatch|deliver|arriv|expect|revised|new date|by|until|now|ready|"
                        r"fulfil|available)\w*", re.IGNORECASE)


def extract_dates(text: str, today: date) -> list[tuple[date, str]]:
    """Return (date, sentence) pairs for dates that appear in ETA-ish sentences."""
    out = []
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+", text):
        if not _ETA_WORDS.search(sentence):
            continue
        for m in _DATE_RE.finditer(sentence):
            raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(0), flags=re.IGNORECASE)
            try:
                d = dparser.parse(raw, dayfirst=True, default=datetime(today.year, 1, 1)).date()
            except (ValueError, OverflowError):
                continue
            has_year = bool(re.search(r"\d{4}", raw))
            if not has_year and d < today - timedelta(days=60):
                d = d.replace(year=d.year + 1)
            out.append((d, sentence.strip()))
    return out


def shipment_facts(text: str, open_pos: list[dict], inventory: dict[str, dict], calendar: list[dict],
                   today: date) -> dict:
    dates = extract_dates(text, today)
    new_eta = max((d for d, _ in dates), default=None)

    ids = {m.upper() for m in re.findall(r"\bPO[-_ ]?\d+\b", text, re.IGNORECASE)}
    ids = {re.sub(r"[_ ]", "-", i) for i in ids}
    matched, how = [], None
    by_id = [p for p in open_pos if p["po_id"].upper() in ids]
    if by_id:
        matched, how = by_id, "po_number"
    else:
        low = text.lower()
        by_sku = [p for p in open_pos if any(l["sku"].lower() in low for l in p.get("lines", []))]
        if by_sku:
            matched, how = by_sku, "sku"
        elif len(open_pos) == 1:
            matched, how = list(open_pos), "only_open_po"

    per_po, overlaps, stockouts = [], set(), []
    for p in matched:
        promised, need_by = _d(p.get("promised_ship")), _d(p.get("need_by"))
        transit = int(p.get("transit_days", 2))
        row = {"po_id": p["po_id"], "criticality": p.get("criticality", "normal"), "buffer_days": p.get("buffer_days", 0),
               "promised_ship": str(promised) if promised else None, "need_by": str(need_by) if need_by else None,
               "skus": [l["sku"] for l in p.get("lines", [])]}
        if new_eta and promised and need_by:
            arrival = new_eta + timedelta(days=transit)
            row.update(slip_days=(new_eta - promised).days,
                       days_past_need_by=max(0, (arrival - need_by).days),
                       projected_arrival=str(arrival))
            for ev in calendar:
                s_, e_ = _d(ev.get("start")), _d(ev.get("end"))
                if s_ and e_ and arrival > need_by and e_ >= need_by and s_ - timedelta(days=14) <= need_by:
                    overlaps.add(ev.get("name"))
        for sku in row["skus"]:
            inv = inventory.get(sku)
            if inv and num(inv.get("daily_sales")):
                stockouts.append(today + timedelta(days=int(num(inv["on_hand"]) / num(inv["daily_sales"]))))
        per_po.append(row)

    return {
        "parsed_dates": sorted({str(d) for d, _ in dates}),
        "new_eta": str(new_eta) if new_eta else "unknown",
        "has_firm_date": new_eta is not None,
        "po_match_method": how,
        "matched_pos": [r["po_id"] for r in per_po],
        "per_po": per_po,
        "slip_days": max((r.get("slip_days", 0) for r in per_po), default=None) if new_eta else None,
        "days_past_need_by": max((r.get("days_past_need_by", 0) for r in per_po), default=None) if new_eta else None,
        "projected_stockout_date": str(min(stockouts)) if stockouts else None,
        "sale_event_overlap": sorted(overlaps),
    }


# ----------------------------------------------------------------------------- support
def support_facts(chunks: list[dict], order: dict | None, customer: dict | None, sender_id: str | None,
                  s: Settings) -> dict:
    top = chunks[0]["score"] if chunks else 0.0
    tier = (customer or {}).get("tier")
    match = None
    if order and sender_id:
        match = sender_id in (order.get("customer_id"), order.get("seller_id"))
    return {
        "kb_top_score": top,
        "kb_stale": kb_is_stale(chunks, s),
        "contact_count_30d": int((customer or {}).get("contact_count_30d", 0)),
        "customer_tier": tier,
        "vip": bool(tier and str(tier).lower() in s.vip_set),
        "requester_matches_order": match,
        "order_found": order is not None,
    }
