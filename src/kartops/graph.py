"""Graph wiring.

START -> preprocess -+-> router -+-> support  --\\
                     |           +-> invoice  ---+-> verifier -> gate -+-> finalize -> END
                     |           +-> shipment --/                      |
                     |           +-> triage ----------------------------+
                     +-> quarantine (prompt attack; no LLM) ------------+
                                  gate -> queue_item -> human_review (interrupt) -> finalize
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .config import Settings
from .deps import Deps
from .nodes import make_nodes
from .state import OpsState


def get_checkpointer(s: Settings):
    if s.is_aws and s.checkpoint_table:
        from langgraph_checkpoint_aws import DynamoDBSaver
        return DynamoDBSaver(table_name=s.checkpoint_table, region_name=s.region, ttl_seconds=60 * 60 * 24 * 30,
                             consistent_read=True)
    return MemorySaver()


def build_graph(d: Deps, checkpointer=None):
    n = make_nodes(d)
    g = StateGraph(OpsState)
    for name in ("preprocess", "router", "support", "invoice", "shipment", "triage", "quarantine", "verifier",
                 "gate", "queue_item", "human_review", "finalize"):
        g.add_node(name, n[name])

    g.add_edge(START, "preprocess")
    g.add_conditional_edges("preprocess", n["_after_preprocess"], {"router": "router", "quarantine": "quarantine"})
    g.add_conditional_edges("router", n["_after_router"],
                            ["support", "invoice", "shipment", "triage", "quarantine"])
    for name in ("support", "invoice", "shipment"):
        g.add_edge(name, "verifier")          # mixed items fan in here after all branches finish
    g.add_edge("triage", "gate")
    g.add_edge("quarantine", "gate")
    g.add_edge("verifier", "gate")
    g.add_conditional_edges("gate", n["_after_gate"], {"finalize": "finalize", "queue_item": "queue_item"})
    g.add_edge("queue_item", "human_review")
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer or get_checkpointer(d.s))
