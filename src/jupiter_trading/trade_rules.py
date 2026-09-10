from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from statistics import median
from typing import Deque, List, Optional, Sequence

from .domain import Product, Side
from .paper_broker import FeeSchedule

PHASE_ORDER = {"SURVIVE": 0, "LOCK": 1, "RIDE": 2}


def breakeven_pct(
    notional: float,
    fee_schedule: Optional[FeeSchedule] = None,
    slippage_bps: float = 0.0,
    product: Product = Product.INTRADAY,
) -> float:
    """Percentage move a round trip must clear before it earns a single rupee.

    Brokerage carries a flat floor, so this number shrinks as allocation grows.
    Every exit threshold is expressed as a multiple of it rather than as a
    hardcoded percentage, which keeps the rule honest when sizing changes.
    """

    if notional <= 0:
        raise ValueError("notional must be positive")
    schedule = fee_schedule or FeeSchedule()
    buy = schedule.calculate(notional, Side.BUY, product)
    sell = schedule.calculate(notional, Side.SELL, product)
    slippage = notional * slippage_bps / 10_000 * 2
    return (buy.total + sell.total + slippage) / notional * 100


def cost_model(
    notional: float,
    fee_schedule: Optional[FeeSchedule] = None,
    slippage_bps: float = 0.0,
    product: Product = Product.INTRADAY,
) -> dict:
    """The same computation broken out for display."""

    schedule = fee_schedule or FeeSchedule()
    buy = schedule.calculate(notional, Side.BUY, product)
    sell = schedule.calculate(notional, Side.SELL, product)
    slippage = notional * slippage_bps / 10_000 * 2
    return {
        "notional": round(notional, 2),
        "buy_fees": round(buy.total, 2),
        "sell_fees": round(sell.total, 2),
        "round_trip_fees": round(buy.total + sell.total, 2),
        "slippage": round(slippage, 2),
        "total_cost": round(buy.total + sell.total + slippage, 2),
        "breakeven_pct": round(
            breakeven_pct(notional, schedule, slippage_bps, product), 4
        ),
        "slippage_bps": slippage_bps,
        "product": product.value,
    }


class ExitPhase(str, Enum):
    SURVIVE = "SURVIVE"
    LOCK = "LOCK"
    RIDE = "RIDE"


@dataclass(frozen=True)
class ExitPolicy:
    """Thresholds for the ratcheting exit, all as multiples of the cost floor."""

    survive_stop_multiple: float = 2.0
    lock_multiple: float = 1.5
    ride_multiple: float = 3.0
    min_gap_multiple: float = 1.5
    trail_window: int = 24
    fast_trail_window: int = 6
    volume_decay_ratio: float = 1.0
    confirmation_samples: int = 2
    time_stop_seconds: float = 240.0

    def __post_init__(self) -> None:
        if min(
            self.survive_stop_multiple,
            self.lock_multiple,
            self.ride_multiple,
            self.min_gap_multiple,
            self.volume_decay_ratio,
            self.time_stop_seconds,
        ) <= 0:
            raise ValueError("exit policy multiples and time stop must be positive")
        if self.ride_multiple < self.lock_multiple:
            raise ValueError("ride_multiple cannot be below lock_multiple")
        if self.trail_window < 2 or self.fast_trail_window < 2:
            raise ValueError("trail windows must hold at least two samples")
        if self.fast_trail_window > self.trail_window:
            raise ValueError("fast_trail_window cannot exceed trail_window")
        if self.confirmation_samples < 1:
            raise ValueError("confirmation_samples must be at least one")


@dataclass(frozen=True)
class EntryPolicy:
    """The three-bar trigger: compare the first and last sample of a short window.

    A fifteen-second window is well inside the noise band, so the window size is
    not what makes the signal real - the threshold is. The bar an entry must
    clear is the largest of three claims:

    `minimum_rise_pct`    an absolute floor,
    `cost_floor_multiple` times the round-trip cost, so an entry is never taken
                          on a move smaller than what it costs to take it,
    `noise_multiple`      times the stock's own recent same-span movement, so a
                          jumpy name has to work harder than a quiet one.
    """

    bars: int = 3
    minimum_rise_pct: float = 0.10
    cost_floor_multiple: float = 1.0
    noise_multiple: float = 2.0

    def __post_init__(self) -> None:
        if self.bars < 2:
            raise ValueError("an entry window needs at least two bars")
        if self.minimum_rise_pct < 0:
            raise ValueError("minimum_rise_pct cannot be negative")
        if self.cost_floor_multiple < 0 or self.noise_multiple < 0:
            raise ValueError("entry multiples cannot be negative")

    def threshold(
        self, cost_floor_pct: Optional[float] = None, noise_pct: Optional[float] = None
    ) -> tuple:
        """The rise an entry must clear, and which claim set it."""

        claims = [("ABSOLUTE_FLOOR", self.minimum_rise_pct)]
        if cost_floor_pct is not None and self.cost_floor_multiple > 0:
            claims.append(("COST_FLOOR", self.cost_floor_multiple * cost_floor_pct))
        if noise_pct is not None and self.noise_multiple > 0:
            claims.append(("STOCK_NOISE", self.noise_multiple * noise_pct))
        source, value = max(claims, key=lambda claim: claim[1])
        return value, source


@dataclass
class EntryEvaluation:
    triggered: bool
    reason: str
    window: List[float]
    first_price: Optional[float] = None
    last_price: Optional[float] = None
    rise_pct: Optional[float] = None
    trigger_price: Optional[float] = None
    threshold_pct: Optional[float] = None
    threshold_source: Optional[str] = None
    noise_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "window": [round(price, 4) for price in self.window],
            "first_price": _rounded(self.first_price),
            "last_price": _rounded(self.last_price),
            "rise_pct": _rounded(self.rise_pct),
            "trigger_price": _rounded(self.trigger_price),
            "threshold_pct": _rounded(self.threshold_pct),
            "threshold_source": self.threshold_source,
            "noise_pct": _rounded(self.noise_pct),
        }


@dataclass
class Bar:
    """One OHLC bar built by bucketing raw ticks into a fixed wall-clock span."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    samples: int = 1

    def absorb(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.samples += 1


class BarAggregator:
    """Buckets a tick stream into fixed-duration OHLC bars.

    Feeding every raw sample - not just one per bucket - means a bar's high and
    low reflect real intrabar movement even though the source poll (typically
    every five seconds) is far faster than the bar. That matters for the entry
    rule: seeding its stop from a bar's close would understate risk whenever
    price wicked below the close and came back, so the stop is seeded from the
    low instead.

    Only fully closed bars are exposed through `completed_bars` - the bar still
    accumulating ticks cannot be used for a live decision, since its close is
    not yet known.
    """

    def __init__(self, bar_seconds: float, max_bars: int = 20) -> None:
        if bar_seconds <= 0:
            raise ValueError("bar_seconds must be positive")
        if max_bars < 2:
            raise ValueError("max_bars must hold at least two bars")
        self.bar_seconds = bar_seconds
        self._bars: Deque[Bar] = deque(maxlen=max_bars)
        self._current: Optional[Bar] = None
        self._bucket_start: Optional[float] = None

    def _bucket(self, timestamp: datetime) -> float:
        epoch = timestamp.timestamp()
        return epoch - (epoch % self.bar_seconds)

    def add(self, price: float, timestamp: datetime) -> Optional[Bar]:
        """Feed one tick. Returns the bar that just closed, if this tick opened a new one."""

        bucket = self._bucket(timestamp)
        closed = None
        if self._current is None:
            self._current = Bar(_aware(timestamp), price, price, price, price)
            self._bucket_start = bucket
        elif bucket != self._bucket_start:
            closed = self._current
            self._bars.append(closed)
            self._current = Bar(_aware(timestamp), price, price, price, price)
            self._bucket_start = bucket
        else:
            self._current.absorb(price)
        return closed

    @property
    def completed_bars(self) -> List[Bar]:
        return list(self._bars)

    @property
    def current_bar(self) -> Optional[Bar]:
        return self._current


def rolling_noise_pct(prices: Sequence[float], bars: int = 3) -> Optional[float]:
    """Typical absolute movement across the same span the entry rule measures.

    Median rather than mean, so one real move does not inflate the estimate of
    what ordinary movement looks like for this stock.
    """

    series = [price for price in prices if price > 0]
    span = bars - 1
    if len(series) < bars + 2:
        return None
    moves = [
        abs(series[index] / series[index - span] - 1) * 100
        for index in range(span, len(series))
    ]
    return median(moves) if moves else None


def evaluate_entry(
    prices: Sequence[float],
    policy: Optional[EntryPolicy] = None,
    cost_floor_pct: Optional[float] = None,
    lows: Optional[Sequence[float]] = None,
) -> EntryEvaluation:
    """Rising first-to-last over `bars` samples, with the window low as the stop seed.

    Flat and falling windows are the documented no-trade cases and are reported
    separately so the dashboard can show which of the two blocked an entry. The
    rise must clear `policy.threshold`, which scales with cost and with this
    stock's own recent movement rather than being a fixed percentage.

    `prices` are the closes the direction check compares. `lows` are each
    sample's own intrabar low, in the same order - pass a stock's bar lows
    alongside its bar closes so the stop seed reflects real wicks. When
    omitted, closes double as their own lows, which is the correct behaviour
    for a plain tick stream where no lower intrabar price was ever observed.
    """

    rule = policy or EntryPolicy()
    noise = rolling_noise_pct(prices, rule.bars)
    threshold, source = rule.threshold(cost_floor_pct, noise)
    window = list(prices)[-rule.bars :]
    low_window = list(lows)[-rule.bars :] if lows is not None else window
    if len(window) < rule.bars or len(low_window) < rule.bars:
        return EntryEvaluation(
            False,
            "BUILDING_PRICE_HISTORY",
            window,
            threshold_pct=threshold,
            threshold_source=source,
            noise_pct=noise,
        )
    first, last = window[0], window[-1]
    if first <= 0:
        return EntryEvaluation(False, "INVALID_PRICE", window)
    rise = (last / first - 1) * 100
    evaluation = EntryEvaluation(
        False, "", window, first, last, rise, min(low_window), threshold, source, noise
    )
    if rise == 0:
        evaluation.reason = "FLAT_NO_TRADE"
    elif rise < 0:
        evaluation.reason = "DOWNWARD_NO_TRADE"
    elif rise < threshold:
        evaluation.reason = "BELOW_ENTRY_THRESHOLD"
    else:
        evaluation.triggered = True
        evaluation.reason = "THREE_BAR_BREAKOUT" if rule.bars == 3 else "WINDOW_BREAKOUT"
    return evaluation


class RatchetExit:
    """Trailing exit whose stop only ever moves up.

    SURVIVE  a fixed stop `survive_stop_multiple` cost floors under entry, wide
             enough that ordinary quote noise cannot shake the trade out.
    LOCK     once ahead by `lock_multiple` floors the stop jumps to entry plus
             one floor, so the round trip can no longer finish at a loss.
    RIDE     once ahead by `ride_multiple` floors the stop trails the rolling
             window low, never sitting closer than `min_gap_multiple` floors
             below the last price.

    A breach in SURVIVE exits immediately because that stop is the risk limit.
    Breaches of the later, tighter stops need `confirmation_samples` consecutive
    prints, which costs a few seconds and ignores single-tick wicks.
    """

    def __init__(
        self,
        entry_price: float,
        cost_floor_pct: float,
        entered_at: datetime,
        policy: Optional[ExitPolicy] = None,
        structural_stop: Optional[float] = None,
    ) -> None:
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if cost_floor_pct <= 0:
            raise ValueError("cost_floor_pct must be positive")
        self.policy = policy or ExitPolicy()
        self.entry_price = entry_price
        self.cost_floor_pct = cost_floor_pct
        self.entered_at = _aware(entered_at)
        self.phase = ExitPhase.SURVIVE
        self.peak_price = entry_price
        self.last_price = entry_price
        self.breaches = 0
        self.samples = 0
        self.reason: Optional[str] = None
        self.trail_low: Optional[float] = None
        self.trail_window_used = 0
        self.volume_state = "UNKNOWN"
        self.seconds_held = 0.0
        self._prices: Deque[float] = deque(maxlen=self.policy.trail_window)
        self.max_risk_stop = entry_price * (
            1 - self.policy.survive_stop_multiple * cost_floor_pct / 100
        )
        self.noise_floor_stop = entry_price * (
            1 - self.policy.min_gap_multiple * cost_floor_pct / 100
        )
        self.breakeven_stop = entry_price * (1 + cost_floor_pct / 100)
        self.structural_stop = structural_stop
        self.stop_price = self.max_risk_stop
        self.stop_source = "MAX_RISK"
        if structural_stop is not None:
            clamped = max(self.max_risk_stop, min(structural_stop, self.noise_floor_stop))
            self.stop_price = clamped
            if clamped == structural_stop:
                self.stop_source = "STRUCTURAL"
            elif clamped == self.noise_floor_stop:
                self.stop_source = "STRUCTURAL_INSIDE_NOISE"
            else:
                self.stop_source = "STRUCTURAL_BEYOND_RISK"

    def update(
        self,
        price: float,
        timestamp: datetime,
        relative_volume: Optional[float] = None,
    ) -> dict:
        """Advance the stop for one observation and report why we stay or leave."""

        if price <= 0:
            raise ValueError("price must be positive")
        self.samples += 1
        self.last_price = price
        self._prices.append(price)
        self.peak_price = max(self.peak_price, price)
        self.seconds_held = max(
            0.0, (_aware(timestamp) - self.entered_at).total_seconds()
        )
        unrealized_pct = (price / self.entry_price - 1) * 100

        if unrealized_pct >= self.policy.ride_multiple * self.cost_floor_pct:
            self._advance(ExitPhase.RIDE)
        elif unrealized_pct >= self.policy.lock_multiple * self.cost_floor_pct:
            self._advance(ExitPhase.LOCK)

        if relative_volume is None:
            self.volume_state = "UNKNOWN"
        elif relative_volume < self.policy.volume_decay_ratio:
            self.volume_state = "DECAYED"
        else:
            self.volume_state = "PARTICIPATING"

        if PHASE_ORDER[self.phase.value] >= PHASE_ORDER[ExitPhase.LOCK.value]:
            self._raise_stop(self.breakeven_stop, "BREAKEVEN_LOCK")
        if self.phase is ExitPhase.RIDE:
            self.trail_window_used = (
                self.policy.fast_trail_window
                if self.volume_state == "DECAYED"
                else self.policy.trail_window
            )
            self.trail_low = min(list(self._prices)[-self.trail_window_used :])
            ceiling = price * (1 - self.policy.min_gap_multiple * self.cost_floor_pct / 100)
            self._raise_stop(min(self.trail_low, ceiling), "TRAILING")

        reason = None
        if price <= self.stop_price:
            self.breaches += 1
            if self.phase is ExitPhase.SURVIVE:
                reason = "HARD_STOP"
            elif self.breaches >= self.policy.confirmation_samples:
                reason = "BREAKEVEN_STOP" if self.phase is ExitPhase.LOCK else "TRAILING_STOP"
        else:
            self.breaches = 0
        if (
            reason is None
            and self.phase is ExitPhase.SURVIVE
            and self.seconds_held >= self.policy.time_stop_seconds
        ):
            reason = "TIME_STOP"
        self.reason = reason
        return self.to_dict()

    def _advance(self, phase: ExitPhase) -> None:
        if PHASE_ORDER[phase.value] > PHASE_ORDER[self.phase.value]:
            self.phase = phase

    def _raise_stop(self, candidate: float, source: str) -> None:
        if candidate > self.stop_price:
            self.stop_price = candidate
            self.stop_source = source

    @classmethod
    def from_dict(cls, value: dict, policy: Optional[ExitPolicy] = None) -> RatchetExit:
        """Restore every state field needed to continue a ratchet after restart."""

        rule = cls(
            float(value["entry_price"]),
            float(value["cost_floor_pct"]),
            datetime.fromisoformat(value["entered_at"]),
            policy,
            value.get("structural_stop"),
        )
        rule.phase = ExitPhase(value["phase"])
        rule.peak_price = float(value["peak_price"])
        rule.last_price = float(value["last_price"])
        rule.stop_price = float(value["stop_price"])
        rule.stop_source = value["stop_source"]
        rule.breaches = int(value.get("breaches", 0))
        rule.samples = int(value.get("samples", 0))
        rule.reason = value.get("reason")
        rule.trail_low = value.get("trail_low")
        rule.trail_window_used = int(value.get("trail_window", 0))
        rule.volume_state = value.get("volume_state", "UNKNOWN")
        rule.seconds_held = float(value.get("seconds_held", 0))
        rule._prices.extend(float(item) for item in value.get("price_window", []))
        return rule

    def to_dict(self) -> dict:
        unrealized_pct = (self.last_price / self.entry_price - 1) * 100
        return {
            "phase": self.phase.value,
            "reason": self.reason,
            "entry_price": round(self.entry_price, 4),
            "entered_at": self.entered_at.isoformat(),
            "last_price": round(self.last_price, 4),
            "peak_price": round(self.peak_price, 4),
            "stop_price": round(self.stop_price, 4),
            "stop_source": self.stop_source,
            "stop_distance_pct": round(
                (self.last_price / self.stop_price - 1) * 100, 4
            ),
            "unrealized_pct": round(unrealized_pct, 4),
            "net_of_cost_pct": round(unrealized_pct - self.cost_floor_pct, 4),
            "cost_floor_pct": round(self.cost_floor_pct, 4),
            "lock_at_pct": round(self.policy.lock_multiple * self.cost_floor_pct, 4),
            "ride_at_pct": round(self.policy.ride_multiple * self.cost_floor_pct, 4),
            "structural_stop": _rounded(self.structural_stop),
            "max_risk_stop": round(self.max_risk_stop, 4),
            "breakeven_stop": round(self.breakeven_stop, 4),
            "trail_low": _rounded(self.trail_low),
            "trail_window": self.trail_window_used,
            "volume_state": self.volume_state,
            "breaches": self.breaches,
            "confirmation_samples": self.policy.confirmation_samples,
            "seconds_held": round(self.seconds_held, 1),
            "time_stop_seconds": self.policy.time_stop_seconds,
            "samples": self.samples,
            "price_window": list(self._prices),
        }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rounded(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)
