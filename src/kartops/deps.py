"""Dependency container shared by graph nodes (makes every external call injectable for tests)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .config import Settings, get_settings
from .data import DataStore, get_data_store, get_kb
from .guardrails import Guardrails
from .llm import LLM, get_llm
from .ops import OpsStore, get_ops


@dataclass
class Deps:
    s: Settings
    data: DataStore
    kb: Any
    ops: OpsStore
    llm: LLM
    guard: Guardrails
    today: Callable[[], date]
    start_payment_hold: Callable[[str, dict], None]

    @classmethod
    def build(cls, s: Settings | None = None, **overrides) -> Deps:
        s = s or get_settings()
        d = cls(
            s=s, data=get_data_store(s), kb=get_kb(s), ops=get_ops(s), llm=get_llm(s), guard=Guardrails(s),
            today=lambda: datetime.now(UTC).date(),
            start_payment_hold=_sfn_starter(s),
        )
        for k, v in overrides.items():
            setattr(d, k, v)
        return d


def _sfn_starter(s: Settings) -> Callable[[str, dict], None]:
    if not (s.is_aws and s.state_machine_arn):
        return lambda item_id, payload: None  # local: the ops store already records the hold
    import json

    import boto3
    sfn = boto3.client("stepfunctions", region_name=s.region)

    def start(item_id: str, payload: dict) -> None:
        try:
            sfn.start_execution(stateMachineArn=s.state_machine_arn, name=f"{item_id}-{int(datetime.now().timestamp())}"[:80],
                                input=json.dumps({"item_id": item_id, **payload}, default=str))
        except sfn.exceptions.ExecutionAlreadyExists:
            pass
    return start
