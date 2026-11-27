"""Invoice extraction. aws mode -> Textract AnalyzeExpense (sync for images, async for PDFs);
local mode -> a pre-extracted .json file. Output is normalised to the shape verified.invoice_facts expects."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Settings

_SUMMARY_MAP = {
    "VENDOR_NAME": "vendor_name", "INVOICE_RECEIPT_ID": "invoice_no", "INVOICE_RECEIPT_DATE": "invoice_date",
    "DUE_DATE": "due_date", "PO_NUMBER": "po_ref", "SUBTOTAL": "subtotal", "TAX": "tax", "TOTAL": "grand_total",
    "DISCOUNT": "discount", "SHIPPING_HANDLING_CHARGE": "shipping",
}
_LINE_MAP = {"PRODUCT_CODE": "sku", "ITEM": "description", "QUANTITY": "qty", "UNIT_PRICE": "unit_price",
             "PRICE": "line_total", "TAX": "tax"}


def _val(field: dict) -> tuple[str, float]:
    v = field.get("ValueDetection", {})
    return v.get("Text", "").strip(), round(v.get("Confidence", 0.0) / 100.0, 3)


def normalize(expense_docs: list[dict]) -> dict:
    out: dict[str, Any] = {"lines": [], "confidence": {}, "currency": "INR"}
    for doc in expense_docs:
        for f in doc.get("SummaryFields", []):
            ftype = f.get("Type", {}).get("Text", "")
            label = (f.get("LabelDetection", {}).get("Text") or "").lower()
            text, conf = _val(f)
            key = _SUMMARY_MAP.get(ftype)
            if not key:  # vendor-specific labels
                if "gstin" in label or "gst no" in label:
                    key = "vendor_gstin"
                elif "ifsc" in label:
                    key = "ifsc"
                elif re.search(r"a/?c|account", label) and re.search(r"\d{6,}", text):
                    key = "bank_account"
                elif "tax rate" in label or "gst %" in label:
                    key = "tax_rate"
            if key and key not in out:  # first occurrence wins
                out[key] = text
                out["confidence"][key] = conf
        for grp in doc.get("LineItemGroups", []):
            for item in grp.get("LineItems", []):
                row: dict[str, Any] = {}
                for f in item.get("LineItemExpenseFields", []):
                    k = _LINE_MAP.get(f.get("Type", {}).get("Text", ""))
                    if k and k not in row:
                        row[k] = _val(f)[0]
                if row:
                    out["lines"].append(row)
    return out


def _split(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), uri
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def analyze_invoice(uri: str, s: Settings, item_id: str) -> dict:
    if not s.is_aws:
        return json.loads(Path(uri.replace("file://", "")).read_text())
    import boto3
    tx = boto3.client("textract", region_name=s.region)
    bucket, key = _split(uri)
    if key.lower().endswith(".pdf"):
        job = tx.start_expense_analysis(DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}})["JobId"]
        docs, token = [], None
        for _ in range(100):  # <= ~5 min
            kw = {"JobId": job, **({"NextToken": token} if token else {})}
            r = tx.get_expense_analysis(**kw)
            if r["JobStatus"] == "IN_PROGRESS":
                time.sleep(3)
                continue
            if r["JobStatus"] != "SUCCEEDED":
                raise RuntimeError(f"Textract job {job} failed: {r.get('StatusMessage')}")
            docs += r["ExpenseDocuments"]
            token = r.get("NextToken")
            if not token:
                break
        else:
            raise TimeoutError("Textract job timed out")
    else:
        docs = tx.analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": key}})["ExpenseDocuments"]
    if s.bucket:  # keep the raw output for audit/debug
        boto3.client("s3", region_name=s.region).put_object(
            Bucket=s.bucket, Key=f"textract/{item_id}.json", Body=json.dumps(docs).encode(),
            ServerSideEncryption="aws:kms")
    return normalize(docs)
