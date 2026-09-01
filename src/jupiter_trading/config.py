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


def _csv(name: str) -> tuple:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


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
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", False))
    scheduler_max_positions: int = field(
        default_factory=lambda: int(_float("SCHEDULER_MAX_POSITIONS", 5))
    )
    scheduler_allocation: float = field(
        default_factory=lambda: _float("SCHEDULER_ALLOCATION", 100_000)
    )
    scheduler_initial_cash: float = field(
        default_factory=lambda: _float("SCHEDULER_INITIAL_CASH", 600_000)
    )
    scheduler_cooldown_seconds: float = field(
        default_factory=lambda: _float("SCHEDULER_COOLDOWN_SECONDS", 900)
    )
    scheduler_account_prefix: str = field(
        default_factory=lambda: os.getenv("SCHEDULER_ACCOUNT_PREFIX", "auto")
    )
    cors_allow_origins: tuple = field(default_factory=lambda: _csv("CORS_ALLOW_ORIGINS"))
    upstox_stream_auto_start: bool = field(
        default_factory=lambda: _bool("UPSTOX_STREAM_AUTO_START", False)
    )
    upstox_stream_instruments: tuple = field(
        default_factory=lambda: _csv("UPSTOX_STREAM_INSTRUMENTS")
    )
    upstox_stream_mode: str = field(
        default_factory=lambda: os.getenv("UPSTOX_STREAM_MODE", "full")
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
            delivery_brokerage_flat=_float("PAPER_DELIVERY_BROKERAGE_FLAT", 20),
            intraday_brokerage_bps=_float("PAPER_INTRADAY_BROKERAGE_BPS", 10),
            intraday_brokerage_cap=_float("PAPER_INTRADAY_BROKERAGE_CAP", 20),
            transaction_bps=_float("PAPER_NSE_TRANSACTION_BPS", 0.307),
            sebi_bps=_float("PAPER_SEBI_BPS", 0.01),
            delivery_stt_bps=_float("PAPER_DELIVERY_STT_BPS", 10),
            intraday_sell_stt_bps=_float("PAPER_INTRADAY_SELL_STT_BPS", 2.5),
            delivery_buy_stamp_bps=_float("PAPER_DELIVERY_BUY_STAMP_BPS", 1.5),
            intraday_buy_stamp_bps=_float("PAPER_INTRADAY_BUY_STAMP_BPS", 0.3),
            gst_percent=_float("PAPER_GST_PERCENT", 18),
            delivery_sell_dp_flat=_float("PAPER_DELIVERY_SELL_DP_FLAT", 20),
        )
    )
