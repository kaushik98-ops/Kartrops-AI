"""Runtime configuration. Everything comes from environment variables so the same
image runs locally, in tests and in Lambda."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # "local" = in-memory stores + FakeLLM-capable; "aws" = DynamoDB/Bedrock/etc.
    mode: str = field(default_factory=lambda: os.environ.get("KARTOPS_MODE", "local"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ap-south-1"))

    # Models (Bedrock model IDs or inference-profile IDs). Verify with:
    #   aws bedrock list-inference-profiles --region <region>
    model_fast: str = field(default_factory=lambda: os.environ.get("MODEL_FAST", ""))
    model_strong: str = field(default_factory=lambda: os.environ.get("MODEL_STRONG", ""))
    prompt_cache: bool = field(default_factory=lambda: _b("ENABLE_PROMPT_CACHE", True))
    max_tool_turns: int = field(default_factory=lambda: int(os.environ.get("MAX_TOOL_TURNS", "6")))

    # Guardrails / KB
    guardrail_id: str = field(default_factory=lambda: os.environ.get("GUARDRAIL_ID", ""))
    guardrail_version: str = field(default_factory=lambda: os.environ.get("GUARDRAIL_VERSION", "DRAFT"))
    kb_id: str = field(default_factory=lambda: os.environ.get("KB_ID", ""))
    kb_require_dates: bool = field(default_factory=lambda: _b("KB_REQUIRE_DATES", True))
    kb_stale_days: int = field(default_factory=lambda: int(os.environ.get("KB_STALE_DAYS", "365")))

    # Storage
    data_table: str = field(default_factory=lambda: os.environ.get("DATA_TABLE", ""))
    ops_table: str = field(default_factory=lambda: os.environ.get("OPS_TABLE", ""))
    checkpoint_table: str = field(default_factory=lambda: os.environ.get("CHECKPOINT_TABLE", ""))
    bucket: str = field(default_factory=lambda: os.environ.get("BUCKET", ""))
    state_machine_arn: str = field(default_factory=lambda: os.environ.get("PAYMENT_HOLD_SFN_ARN", ""))
    queue_url: str = field(default_factory=lambda: os.environ.get("INGEST_QUEUE_URL", ""))

    # Policy knobs (substituted into the prompts AND enforced by the code gate)
    platform_name: str = field(default_factory=lambda: os.environ.get("PLATFORM_NAME", "KartMart"))
    brand_tone: str = field(default_factory=lambda: os.environ.get(
        "BRAND_TONE", "warm, plain-spoken, professional, no jargon"))
    threshold: float = field(default_factory=lambda: _f("CONF_THRESHOLD", 0.75))
    min_score: float = field(default_factory=lambda: _f("KB_MIN_SCORE", 0.55))
    vip_tiers: str = field(default_factory=lambda: os.environ.get("VIP_TIERS", "plus,vip"))
    field_conf_min: float = field(default_factory=lambda: _f("FIELD_CONF_MIN", 0.90))
    price_tol_pct: float = field(default_factory=lambda: _f("PRICE_TOL_PCT", 2.0))
    qty_tol_pct: float = field(default_factory=lambda: _f("QTY_TOL_PCT", 0.0))
    amount_tol_abs: float = field(default_factory=lambda: _f("AMOUNT_TOL_ABS", 1.0))

    @property
    def is_aws(self) -> bool:
        return self.mode == "aws"

    @property
    def vip_set(self) -> set[str]:
        return {t.strip().lower() for t in self.vip_tiers.split(",") if t.strip()}

    def prompt_vars(self, language: str = "en") -> dict[str, str]:
        return {
            "platform_name": self.platform_name,
            "brand_tone": self.brand_tone,
            "language": language,
            "threshold": str(self.threshold),
            "min_score": str(self.min_score),
            "vip_tiers": self.vip_tiers,
            "field_conf_min": str(self.field_conf_min),
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
