import datetime as dt
import json
from pathlib import Path

from kartops.data import MemoryDataStore
from kartops.verified import extract_dates, invoice_facts, shipment_facts

S = Path(__file__).resolve().parents[1] / "samples"


def _inv(name):
    return json.loads((S / "invoices" / f"{name}.json").read_text())


def _facts(name, settings):
    d = MemoryDataStore()
    ex = _inv(name)
    po = d.get_po(ex["po_ref"])
    return invoice_facts(ex, po, d.get_grn(ex["po_ref"]), d.get_vendor(po["vendor_id"]),
                         d.get_payment_history(po["vendor_id"]), settings)


def test_good_invoice_all_pass(settings):
    f = _facts("good", settings)
    assert f["bank_details_match"] is True and f["three_way_ok"] and f["grand_total_ok"] and f["tax_math_ok"]
    assert f["duplicate_candidates"] == [] and f["vendor_identity_match"]


def test_bank_change_detected_and_masked(settings):
    f = _facts("bad_bank", settings)
    assert f["bank_details_match"] is False
    assert f["bank_last4"] == {"invoice": "XXXX1111", "vendor_master": "XXXX9012"}


def test_duplicate_and_price(settings):
    assert _facts("duplicate", settings)["duplicate_candidates"][0]["match"] == "same_invoice_no"
    f = _facts("price_up", settings)
    assert f["three_way_match"][0]["status"] == "price_out_of_tolerance" and not f["three_way_ok"]


def test_low_confidence_key_field(settings):
    d = MemoryDataStore(); ex = _inv("good"); ex["confidence"]["bank_account"] = 0.5
    f = invoice_facts(ex, d.get_po("PO-1001"), d.get_grn("PO-1001"), d.get_vendor("VND-001"), [], settings)
    assert f["unverifiable_key_fields"] == ["bank_account"]


def test_date_extraction():
    today = dt.date(2026, 11, 22)
    got = {d for d, _ in extract_dates("Revised dispatch is now 8 Dec 2026. Order was placed 1 Nov.", today)}
    assert dt.date(2026, 12, 8) in got and dt.date(2026, 11, 1) not in got
    assert extract_dates("will update soon", today) == []
    assert extract_dates("ETA 05/12/2026", today)[0][0] == dt.date(2026, 12, 5)  # day-first


def test_shipment_red_when_late_for_sale(deps):
    po = deps.data.open_pos("VND-001")
    f = shipment_facts("PO-1001 revised dispatch 8 Dec 2026", po, {"SKU-AAA": deps.data.get_inventory("SKU-AAA")},
                       deps.data.sale_calendar(), dt.date(2026, 11, 22))
    assert f["slip_days"] == 13 and f["days_past_need_by"] == 5
    assert f["sale_event_overlap"] == ["Year-End Sale"] and f["projected_stockout_date"] == "2026-12-02"
    assert f["po_match_method"] == "po_number"
