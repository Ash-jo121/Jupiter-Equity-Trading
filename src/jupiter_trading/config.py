from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .paper_broker import FeeSchedule, RiskLimits

load_dotenv()


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    initial_cash: float = field(default_factory=lambda: _float("PAPER_INITIAL_CASH", 1_000_000))
    slippage_bps: float = field(default_factory=lambda: _float("PAPER_SLIPPAGE_BPS", 2))
    database_path: str = field(
        default_factory=lambda: os.getenv("PAPER_DB_PATH", "data/paper_trading.db")
    )
    upstox_access_token: str = field(
        default_factory=lambda: os.getenv("UPSTOX_ACCESS_TOKEN", "")
    )
    risk_limits: RiskLimits = field(
        default_factory=lambda: RiskLimits(
            allow_short=_bool("PAPER_ALLOW_SHORT", False),
            max_order_notional=_float("PAPER_MAX_ORDER_NOTIONAL", 250_000),
            max_position_notional=_float("PAPER_MAX_POSITION_NOTIONAL", 500_000),
            max_daily_loss=_float("PAPER_MAX_DAILY_LOSS", 25_000),
        )
    )
    fee_schedule: FeeSchedule = field(
        default_factory=lambda: FeeSchedule(
            brokerage_bps=_float("PAPER_BROKERAGE_BPS", 0),
            transaction_bps=_float("PAPER_TRANSACTION_BPS", 0),
            tax_bps=_float("PAPER_TAX_BPS", 0),
        )
    )
