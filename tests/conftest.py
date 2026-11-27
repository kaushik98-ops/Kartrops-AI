import datetime as dt

import pytest

from kartops.config import Settings, reset_settings
from kartops.data import LocalKB, MemoryDataStore, reset_caches
from kartops.deps import Deps
from kartops.fake_llm import FakeLLM
from kartops.guardrails import Guardrails
from kartops.ops import MemoryOpsStore, reset_ops
from kartops.runner import Runner


@pytest.fixture
def settings(monkeypatch):
    for k in ("KARTOPS_MODE", "MODEL_FAST", "MODEL_STRONG"):
        monkeypatch.delenv(k, raising=False)
    reset_settings(); reset_caches(); reset_ops()
    return Settings()


@pytest.fixture
def deps(settings):
    return Deps(s=settings, data=MemoryDataStore(), kb=LocalKB(), ops=MemoryOpsStore(), llm=FakeLLM(),
                guard=Guardrails(settings), today=lambda: dt.date(2026, 11, 22), start_payment_hold=lambda i, p: None)


@pytest.fixture
def runner(deps):
    return Runner(deps)
