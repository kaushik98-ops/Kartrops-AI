"""Evaluation harness. Track escalation recall and false-OK rate first (see README)."""
from __future__ import annotations

import json
from pathlib import Path

from .data import SAMPLES
from .runner import Runner


def run_cases(runner: Runner, path: Path) -> dict:
    rows, total_esc, caught, auto_total, false_ok = [], 0, 0, 0, 0
    for i, line in enumerate(l for l in path.read_text().splitlines() if l.strip()):
        c = json.loads(line)
        att = [{"uri": str(SAMPLES / c["attach"])}] if c.get("attach") else []
        out = runner.process({"item_id": f"eval-{i}-{c['name']}", "channel": c["channel"], "raw_input": c.get("text", ""),
                              "sender_id": c.get("sender_id"), "attachments": att})
        act, queue = out["gate"]["action"], out["gate"].get("queue")
        should_escalate = c["expect_action"] != "auto_draft"
        ok = act == c["expect_action"] and (not c.get("expect_queue") or queue == c["expect_queue"])
        if should_escalate:
            total_esc += 1
            caught += act != "auto_draft"
        else:
            auto_total += 1
        false_ok += (act == "auto_draft" and should_escalate)
        rows.append({"case": c["name"], "expected": f"{c['expect_action']}/{c.get('expect_queue') or '-'}",
                     "actual": f"{act}/{queue or '-'}", "pass": ok})
    return {"rows": rows, "passed": sum(r["pass"] for r in rows), "total": len(rows),
            "escalation_recall": caught / total_esc if total_esc else 1.0,
            "false_ok_count": false_ok}
