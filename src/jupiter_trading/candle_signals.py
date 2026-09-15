from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from statistics import median
from typing import Iterable, List, Optional

from .indicators import macd_series
from .market_data import Candle

MACD_EARLY = "MACD_EARLY"
MACD_EARLY_PRICE_CONFIRM = "MACD_EARLY_PRICE_CONFIRM"
MACD_FRESH_CONFIRMED = "MACD_FRESH_CONFIRMED"
EXPERIMENT_ENTRY_MODES = frozenset(
    {MACD_EARLY, MACD_EARLY_PRICE_CONFIRM, MACD_FRESH_CONFIRMED}
)
EXPERIMENT_KIND = "THREE_MACD_ENTRIES_SHARED_EXIT_V1"
EXIT_POLICY_VERSION = "BEARISH_MACD_V1"
PRIORITY_VERSION = "HARD_RATCHET_SIGNAL_V1"


@dataclass(frozen=True)
class SignalConfig:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    warmup_bars: int = 100
    volume_lookback: int = 20
    minimum_rvol_1m: float = 1.50
    lower_upper_wick_ratio: float = 1.50
    minimum_lower_range_ratio: float = 0.20
    minimum_body_range_ratio: float = 0.25
    early_min_delta_bps: float = 0.0
    fresh_cross_max_age_bars: int = 2
    confirmation_window_bars: int = 2
    breakout_buffer_ticks: int = 1
    candle_finalization_grace_seconds: float = 2.0
    max_signal_bar_age_seconds: float = 15.0
    max_quote_age_seconds: float = 10.0
    market_intent_ttl_seconds: float = 15.0
    exit_material_contraction_fraction: float = 0.20
    participation_rvol_min: float = 1.50

    def __post_init__(self) -> None:
        if not 0 < self.fast_period < self.slow_period or self.signal_period <= 0:
            raise ValueError("MACD periods must satisfy 0 < fast < slow and signal > 0")
        if self.warmup_bars < 100:
            raise ValueError("V1 requires at least 100 warm-up bars")
        if self.volume_lookback != 20:
            raise ValueError("V1 requires a 20-bar volume baseline")
        if self.confirmation_window_bars != 2 or self.fresh_cross_max_age_bars != 2:
            raise ValueError("V1 confirmation and fresh-cross windows must both be two bars")
        if min(
            self.minimum_rvol_1m,
            self.lower_upper_wick_ratio,
            self.minimum_lower_range_ratio,
            self.minimum_body_range_ratio,
        ) <= 0:
            raise ValueError("signal ratios must be positive")
        if self.early_min_delta_bps < 0:
            raise ValueError("early MACD slope cannot be negative")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BarFeatures:
    bar: Candle
    bar_end: datetime
    body: float
    price_range: float
    lower_wick: float
    upper_wick: float
    body_ratio: float
    lower_ratio: float
    upper_ratio: float
    close_location: float
    baseline_volume: Optional[float]
    rvol_1m: Optional[float]
    macd: Optional[float]
    signal: Optional[float]
    histogram: Optional[float]
    h1: Optional[float]
    h2: Optional[float]
    delta_bps: Optional[float]
    latest_cross_age: Optional[int]
    warmup_count: int
    warmup_seed_count: int
    session_bar_count: int
    ready: bool
    quality_reasons: tuple

    @property
    def bar_id(self) -> str:
        return self.bar.timestamp.isoformat()

    def to_dict(self) -> dict:
        value = asdict(self)
        value["bar"] = self.bar.to_dict()
        value["bar_end"] = self.bar_end.isoformat()
        value["bar_id"] = self.bar_id
        value["quality_reasons"] = list(self.quality_reasons)
        return value


def admitted_candles(
    candles: Iterable[Candle],
    clock: Optional[datetime] = None,
    finalization_grace_seconds: float = 2.0,
) -> List[Candle]:
    """Validate, deduplicate and admit only finalized one-minute provider bars."""

    now = clock or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    by_start = {}
    for candle in sorted(candles, key=lambda item: item.timestamp):
        if not _valid_candle(candle):
            continue
        start = candle.timestamp
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        if end + timedelta(seconds=finalization_grace_seconds) > now:
            continue
        identity = (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.open_interest,
        )
        existing = by_start.get(start)
        if existing and existing[0] != identity:
            continue  # freeze the first valid online revision
        by_start.setdefault(start, (identity, candle))
    return [value[1] for _, value in sorted(by_start.items())]


def build_features(
    completed_candles: Iterable[Candle],
    config: Optional[SignalConfig] = None,
    warmup_candles: Optional[Iterable[Candle]] = None,
) -> BarFeatures:
    policy = config or SignalConfig()
    candles = list(completed_candles)
    if not candles:
        raise ValueError("at least one completed candle is required")
    segment_start = 0
    for index in range(len(candles) - 1, 0, -1):
        if candles[index].timestamp - candles[index - 1].timestamp != timedelta(minutes=1):
            segment_start = index
            break
    session_candles = candles[segment_start:]
    candle = session_candles[-1]
    first_session_timestamp = session_candles[0].timestamp
    seed = sorted(
        (
            item
            for item in (warmup_candles or [])
            if _valid_candle(item) and item.timestamp < first_session_timestamp
        ),
        key=lambda item: item.timestamp,
    )[-policy.warmup_bars :]
    indicator_candles = [*seed, *session_candles]
    quality = []
    if not _valid_candle(candle):
        quality.append("INVALID_OHLCV")
    if segment_start and len(session_candles) < policy.warmup_bars:
        quality.append("DATA_GAP")
    price_range = candle.high - candle.low
    body = abs(candle.close - candle.open)
    lower = min(candle.open, candle.close) - candle.low
    upper = candle.high - max(candle.open, candle.close)
    denominator = price_range if price_range > 0 else 1.0
    prior_volumes = [
        item.volume for item in session_candles[-(policy.volume_lookback + 1) : -1]
    ]
    baseline = median(prior_volumes) if len(prior_volumes) == policy.volume_lookback else None
    rvol = candle.volume / baseline if baseline is not None and baseline > 0 else None
    points = macd_series(
        [item.close for item in indicator_candles],
        policy.fast_period,
        policy.slow_period,
        policy.signal_period,
    )
    point = points[-1]
    h1 = points[-2].histogram if len(points) >= 2 else None
    h2 = points[-3].histogram if len(points) >= 3 else None
    delta = (
        10_000.0 * (point.histogram - h1) / candle.close
        if point.histogram is not None and h1 is not None and candle.close > 0
        else None
    )
    latest_cross = next(
        (index for index in range(len(points) - 1, -1, -1) if points[index].cross_up), None
    )
    cross_age = len(points) - 1 - latest_cross if latest_cross is not None else None
    if point.histogram is None or h1 is None or h2 is None:
        quality.append("INDICATOR_NOT_READY")
    if len(indicator_candles) < policy.warmup_bars:
        quality.append("WARMUP")
    return BarFeatures(
        bar=candle,
        bar_end=_aware(candle.timestamp) + timedelta(minutes=1),
        body=body,
        price_range=price_range,
        lower_wick=lower,
        upper_wick=upper,
        body_ratio=body / denominator if price_range > 0 else 0.0,
        lower_ratio=lower / denominator if price_range > 0 else 0.0,
        upper_ratio=upper / denominator if price_range > 0 else 0.0,
        close_location=(candle.close - candle.low) / denominator if price_range > 0 else 0.0,
        baseline_volume=baseline,
        rvol_1m=rvol,
        macd=point.macd,
        signal=point.signal,
        histogram=point.histogram,
        h1=h1,
        h2=h2,
        delta_bps=delta,
        latest_cross_age=cross_age,
        warmup_count=len(indicator_candles),
        warmup_seed_count=len(seed),
        session_bar_count=len(session_candles),
        ready=not quality,
        quality_reasons=tuple(dict.fromkeys(quality)),
    )


def evaluate_entry(mode: str, features: BarFeatures, config: Optional[SignalConfig] = None) -> dict:
    policy = config or SignalConfig()
    if mode not in EXPERIMENT_ENTRY_MODES:
        raise ValueError(f"unsupported experiment entry mode: {mode}")
    rejection_checks = {
        "green": features.bar.close > features.bar.open,
        "lower_wick_dominant": features.lower_wick >= policy.lower_upper_wick_ratio * features.upper_wick,
        "lower_range_ratio": features.lower_ratio >= policy.minimum_lower_range_ratio,
        "body_range_ratio": features.body_ratio >= policy.minimum_body_range_ratio,
        "rvol_1m": features.rvol_1m is not None and features.rvol_1m >= policy.minimum_rvol_1m,
    }
    rejection = features.price_range > 0 and all(rejection_checks.values())
    h0, h1, h2 = features.histogram, features.h1, features.h2
    common = features.ready and rejection and h0 is not None and h1 is not None and h2 is not None
    early_checks = {
        "histogram_negative": h0 is not None and h0 < 0,
        "two_step_improvement": h2 is not None and h1 is not None and h0 is not None and h2 < h1 < h0,
        "positive_normalized_slope": features.delta_bps is not None and features.delta_bps > 0,
        "minimum_normalized_slope": features.delta_bps is not None and features.delta_bps >= policy.early_min_delta_bps,
    }
    fresh_checks = {
        "histogram_positive": h0 is not None and h0 > 0,
        "histogram_expanding": h0 is not None and h1 is not None and h0 > h1,
        "prior_not_declining": h1 is not None and h2 is not None and h1 >= h2,
        "fresh_cross": features.latest_cross_age is not None
        and 0 <= features.latest_cross_age <= policy.fresh_cross_max_age_bars,
    }
    checks = early_checks if mode in {MACD_EARLY, MACD_EARLY_PRICE_CONFIRM} else fresh_checks
    raw = common and all(checks.values())
    if features.quality_reasons:
        reason = features.quality_reasons[0]
    elif not rejection:
        reason = "CANDLE_REJECTION_FAILED" if rejection_checks["rvol_1m"] else "RVOL_LOW"
    elif not raw:
        reason = "MACD_NOT_IMPROVING" if mode != MACD_FRESH_CONFIRMED else "CROSS_TOO_OLD"
    else:
        reason = "EARLY_CONVERGENCE" if mode != MACD_FRESH_CONFIRMED else "FRESH_MACD_CONFIRMED"
    return {
        "strategy": mode,
        "ready": features.ready,
        "raw_qualified": raw,
        "actionable": raw,
        "reason": reason,
        "rejection": rejection,
        "rejection_checks": rejection_checks,
        "mode_checks": checks,
        "features": features.to_dict(),
    }


def evaluate_shared_exit(features: BarFeatures, config: Optional[SignalConfig] = None) -> dict:
    policy = config or SignalConfig()
    bearish = features.bar.close < features.bar.open
    upper_rejection = (
        features.upper_wick >= 1.50 * features.lower_wick
        and features.upper_ratio >= 0.20
        and features.body_ratio >= 0.25
    )
    strong_body = features.body_ratio >= 0.60 and features.close_location <= 0.25
    candle_ok = bearish and (upper_rejection or strong_body) and features.price_range > 0
    h2, h1, h0 = features.h2, features.h1, features.histogram
    values_ready = h2 is not None and h1 is not None and h0 is not None
    positive_shrink = values_ready and h1 > h0 > 0
    material = bool(
        positive_shrink and (h1 - h0) >= policy.exit_material_contraction_fraction * h1
    )
    consecutive = bool(values_ready and h2 > h1 > h0 > 0)
    cross_down = bool(values_ready and h1 >= 0 and h0 < 0)
    zero_touch = bool(values_ready and h1 > 0 and h0 == 0)
    macd_ok = material or consecutive or cross_down or zero_touch
    reason = None
    for passed, label in (
        (cross_down, "MOMENTUM_CROSS_DOWN"),
        (zero_touch, "MOMENTUM_ZERO_TOUCH"),
        (material, "MOMENTUM_CONTRACTION_MATERIAL"),
        (consecutive, "MOMENTUM_CONTRACTION_CONSECUTIVE"),
    ):
        if passed:
            reason = label
            break
    actionable = features.ready and candle_ok and macd_ok
    contraction = (h1 - h0) / h1 if values_ready and h1 > 0 else None
    decline_bps = 10_000.0 * (h1 - h0) / features.bar.close if values_ready else None
    return {
        "version": EXIT_POLICY_VERSION,
        "priority_version": PRIORITY_VERSION,
        "ready": features.ready,
        "raw_qualified": candle_ok and macd_ok,
        "actionable": actionable,
        "reason": reason if actionable else (
            "EXIT_DATA_NOT_READY" if not features.ready else
            "EXIT_CANDLE_FAILED" if not candle_ok else "EXIT_MACD_FAILED"
        ),
        "candle": {
            "bearish": bearish,
            "upper_rejection": upper_rejection,
            "strong_bearish_body": strong_body,
            "qualified": candle_ok,
            "upper_ratio": features.upper_ratio,
            "body_ratio": features.body_ratio,
            "close_location": features.close_location,
        },
        "macd": {
            "h2": h2,
            "h1": h1,
            "h0": h0,
            "positive_shrink": positive_shrink,
            "material_contraction": material,
            "consecutive_contraction": consecutive,
            "cross_down": cross_down,
            "zero_touch": zero_touch,
            "contraction_fraction": contraction,
            "normalized_decline_bps": decline_bps,
            "qualified": macd_ok,
        },
        "volume": {
            "mode": "ANNOTATE_ONLY",
            "rvol_1m": features.rvol_1m,
            "elevated_participation": features.rvol_1m is not None
            and features.rvol_1m >= policy.participation_rvol_min,
            "unknown": features.rvol_1m is None,
        },
        "features": features.to_dict(),
    }


class SetupState(str, Enum):
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass
class PriceConfirmationSetup:
    setup_id: str
    instrument_key: str
    bar_id: str
    available_at: datetime
    bar_end: datetime
    setup_high: Decimal
    setup_low: Decimal
    tick_size: Decimal
    expiry: datetime
    state: SetupState = SetupState.ARMED
    reason: Optional[str] = None

    @classmethod
    def arm(
        cls,
        setup_id: str,
        instrument_key: str,
        features: BarFeatures,
        available_at: datetime,
        tick_size: float,
        config: Optional[SignalConfig] = None,
    ) -> PriceConfirmationSetup:
        policy = config or SignalConfig()
        try:
            tick = Decimal(str(tick_size))
            high = Decimal(str(features.bar.high))
            low = Decimal(str(features.bar.low))
        except InvalidOperation as error:
            raise ValueError("invalid instrument tick size") from error
        if not tick.is_finite() or tick <= 0:
            raise ValueError("invalid instrument tick size")
        end = features.bar_end
        return cls(
            setup_id,
            instrument_key,
            features.bar_id,
            _aware(available_at),
            end,
            high,
            low,
            tick,
            end + timedelta(minutes=policy.confirmation_window_bars),
        )

    @property
    def trigger_price(self) -> Decimal:
        return self.setup_high + self.tick_size

    def on_quote(self, price: float, event_time: datetime) -> str:
        if self.state != SetupState.ARMED:
            return self.state.value
        moment = _aware(event_time)
        value = Decimal(str(price))
        if moment >= self.expiry:
            return self.cancel(SetupState.EXPIRED, "SETUP_EXPIRED")
        if moment <= self.available_at:
            return self.state.value
        if value <= self.setup_low:
            return self.cancel(SetupState.INVALIDATED, "SETUP_LOW_BREACHED")
        if value >= self.trigger_price:
            self.state = SetupState.TRIGGERED
        return self.state.value

    def on_bar(self, features: BarFeatures) -> str:
        if self.state != SetupState.ARMED or features.bar_id == self.bar_id:
            return self.state.value
        if features.bar_end >= self.expiry:
            return self.cancel(SetupState.EXPIRED, "SETUP_EXPIRED")
        low = Decimal(str(features.bar.low))
        high = Decimal(str(features.bar.high))
        if low <= self.setup_low and high >= self.trigger_price:
            return self.cancel(SetupState.CANCELLED, "AMBIGUOUS_BAR")
        if low <= self.setup_low:
            return self.cancel(SetupState.INVALIDATED, "SETUP_LOW_BREACHED")
        if (
            features.histogram is not None
            and features.h1 is not None
            and features.histogram < features.h1
        ):
            return self.cancel(SetupState.INVALIDATED, "MACD_WORSENED")
        return self.state.value

    def cancel(self, state: SetupState, reason: str) -> str:
        self.state = state
        self.reason = reason
        return self.state.value

    def to_dict(self) -> dict:
        return {
            "setup_id": self.setup_id,
            "instrument_key": self.instrument_key,
            "bar_id": self.bar_id,
            "available_at": self.available_at.isoformat(),
            "bar_end": self.bar_end.isoformat(),
            "setup_high": float(self.setup_high),
            "setup_low": float(self.setup_low),
            "tick_size": float(self.tick_size),
            "trigger_price": float(self.trigger_price),
            "expiry": self.expiry.isoformat(),
            "state": self.state.value,
            "reason": self.reason,
        }


def _valid_candle(candle: Candle) -> bool:
    values = (candle.open, candle.high, candle.low, candle.close, float(candle.volume))
    return (
        all(isfinite(value) for value in values)
        and min(candle.open, candle.high, candle.low, candle.close) > 0
        and candle.volume >= 0
        and candle.high >= max(candle.open, candle.close, candle.low)
        and candle.low <= min(candle.open, candle.close, candle.high)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
