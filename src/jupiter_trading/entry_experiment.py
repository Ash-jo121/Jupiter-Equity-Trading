from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .candle_signals import (
    EXPERIMENT_KIND,
    MACD_EARLY,
    MACD_EARLY_PRICE_CONFIRM,
    MACD_FRESH_CONFIRMED,
    PRIORITY_VERSION,
    SignalConfig,
)

VARIANTS = (
    ("A", MACD_EARLY),
    ("B", MACD_EARLY_PRICE_CONFIRM),
    ("C", MACD_FRESH_CONFIRMED),
)


@dataclass(frozen=True)
class ExperimentConfig:
    initial_cash: float = 1_000_000.0
    allocation_per_position: float = 25_000.0
    max_positions: int = 2
    duration_seconds: int = 22_500
    poll_interval_seconds: float = 5.0
    rescan_interval_seconds: float = 285.0
    candidate_limit: int = 10
    minimum_score: float = 0.15
    minimum_relative_volume: float = 1.2
    require_nifty_confirmation: bool = False
    reentry_cooldown_seconds: float = 0.0
    signal: SignalConfig = field(default_factory=SignalConfig)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0 or self.allocation_per_position <= 0:
            raise ValueError("experiment capital and allocation must be positive")
        if self.initial_cash < self.max_positions * self.allocation_per_position:
            raise ValueError("initial cash must cover every configured position allocation")
        if self.max_positions <= 0 or self.candidate_limit <= 0:
            raise ValueError("position and candidate limits must be positive")
        if min(self.duration_seconds, self.poll_interval_seconds, self.rescan_interval_seconds) <= 0:
            raise ValueError("experiment durations must be positive")
        if self.reentry_cooldown_seconds != 0:
            raise ValueError("V1 permits one filled trade per instrument per session")

    def resolved(self) -> dict:
        value = asdict(self)
        value.update(
            {
                "experiment_kind": EXPERIMENT_KIND,
                "execution_mode": "PAPER",
                "universe_name": "NIFTY 100",
                "entry_timeframe_seconds": 60,
                "exit_mode": "RATCHET",
                "momentum_exit_enabled": True,
                "momentum_exit_version": "BEARISH_MACD_V1",
                "priority_version": PRIORITY_VERSION,
                "volume_mode": "ANNOTATE_ONLY",
                "position_fraction": 1.0,
                "latch_until_flat": True,
            }
        )
        return value

    @property
    def shared_hash(self) -> str:
        encoded = json.dumps(self.resolved(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExperimentIdentity:
    experiment_id: str
    session_id: str

    @classmethod
    def create(
        cls,
        experiment_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ExperimentIdentity:
        moment = now or datetime.now(timezone.utc)
        value = experiment_id or f"exp-{moment.strftime('%Y%m%d')}-{uuid4().hex[:10]}"
        return cls(value, moment.date().isoformat())

    def account_id(self, label: str) -> str:
        # PaperAccount IDs are currently capped at 40 characters by the API.
        return f"{self.experiment_id}-{label}"[:40]
