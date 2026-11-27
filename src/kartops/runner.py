"""Run / resume items. Idempotent: SQS is at-least-once, so a re-delivered message must not re-run a finished item."""
from __future__ import annotations

import time

from langgraph.types import Command

from . import metrics
from .deps import Deps
from .graph import build_graph


class Runner:
    def __init__(self, deps: Deps, checkpointer=None):
        self.d = deps
        self.graph = build_graph(deps, checkpointer)

    @staticmethod
    def _cfg(item_id: str) -> dict:
        return {"configurable": {"thread_id": item_id}, "recursion_limit": 40}

    def _status(self, item_id: str) -> dict:
        snap = self.graph.get_state(self._cfg(item_id))
        v = snap.values or {}
        waiting = any(t.interrupts for t in snap.tasks) if snap.next else False
        status = "pending_human" if waiting else ("completed" if v and not snap.next else "in_progress")
        return {"item_id": item_id, "status": status, "gate": v.get("gate"), "outcome": v.get("outcome"),
                "route": v.get("route")}

    def process(self, msg: dict) -> dict:
        """msg: {item_id, channel, raw_input, attachments?, sender_id?, entities?}"""
        item_id = msg["item_id"]
        st = self._status(item_id)
        if st["status"] in ("completed", "pending_human"):
            return {**st, "skipped": True}
        t0 = time.time()
        inp = None if st["status"] == "in_progress" and self.graph.get_state(self._cfg(item_id)).values else {
            "item_id": item_id, "channel": msg.get("channel", "ticket"), "raw_input": msg.get("raw_input", ""),
            "attachments": msg.get("attachments", []), "sender_id": msg.get("sender_id"),
            "entities": msg.get("entities", {}), "guardrail_flags": [], "context": {}, "results": {},
            "verified_facts": {}, "usage": {}}
        self.graph.invoke(inp, self._cfg(item_id))
        metrics.emit("ItemLatencyMs", int((time.time() - t0) * 1000), "Milliseconds")
        return self._status(item_id)

    def resume(self, item_id: str, decision: dict) -> dict:
        st = self._status(item_id)
        if st["status"] != "pending_human":
            raise ValueError(f"item {item_id} is {st['status']}, not awaiting a human decision")
        self.d.ops.put_audit(item_id, "human_decision", decision)
        self.graph.invoke(Command(resume=decision), self._cfg(item_id))
        return self._status(item_id)
