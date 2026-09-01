from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Sequence

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

# The one axis worth comparing head to head: 5s ticks, 1m, 3m, 5m entry bars.
# Everything else (full-session duration, position cap) is held fixed so each
# run differs by exactly this, over the identical universe and session.
ENTRY_TIMEFRAMES = (0, 60, 180, 300)
TIMEFRAME_LABELS = {0: "5s", 60: "1m", 180: "3m", 300: "5m"}


def timeframe_label(seconds: int) -> str:
    return TIMEFRAME_LABELS.get(seconds, f"{seconds}s")


@dataclass
class ScheduledSlot:
    """One full-session run: which entry timeframe it tests, and how it went."""

    index: int
    start_ist: str
    end_ist: str
    account_id: str
    label: str
    duration_seconds: int
    max_positions: int
    entry_timeframe_seconds: int
    status: str = "PENDING"  # PENDING -> LAUNCHED / FAILED / SKIPPED
    runner_id: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> ScheduledSlot:
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value[key] for key in known if key in value})


@dataclass
class DailyPlan:
    """A day's runs: one per entry timeframe, each spanning the whole session."""

    session_date: str
    account_prefix: str
    slots: List[ScheduledSlot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_date": self.session_date,
            "account_prefix": self.account_prefix,
            "slots": [slot.to_dict() for slot in self.slots],
            "coverage": coverage_summary(self.slots),
        }

    @classmethod
    def from_dict(cls, value: dict) -> DailyPlan:
        return cls(
            session_date=value["session_date"],
            account_prefix=value.get("account_prefix", "auto"),
            slots=[ScheduledSlot.from_dict(slot) for slot in value.get("slots", [])],
        )


def _ist_datetime(session_date: str, moment: time) -> datetime:
    return datetime.combine(date.fromisoformat(session_date), moment, tzinfo=IST)


def build_daily_plan(
    session_date: str,
    timeframes: Sequence[int] = ENTRY_TIMEFRAMES,
    max_positions: int = 5,
    account_prefix: str = "auto",
) -> DailyPlan:
    """One full-session run per entry timeframe.

    Every run opens at 09:15 and closes at 15:30, so the session is covered by
    construction with no staggering to engineer. The runs are identical but for
    their entry timeframe, which makes the day's four P&L numbers a clean,
    like-for-like comparison of that one axis. A re-entry cooldown (set on the
    scheduler, applied at launch) keeps each run trading through the day rather
    than exhausting the universe by mid-morning.
    """

    if not timeframes:
        raise ValueError("a plan needs at least one entry timeframe")
    if max_positions < 1:
        raise ValueError("max_positions must be at least one")
    open_at = _ist_datetime(session_date, SESSION_OPEN)
    close_at = _ist_datetime(session_date, SESSION_CLOSE)
    duration = int((close_at - open_at).total_seconds())
    if duration <= 0:
        raise ValueError("session close must be after session open")

    slots = []
    for index, timeframe in enumerate(dict.fromkeys(timeframes)):
        label = f"full-{max_positions}pos-{timeframe_label(timeframe)}"
        slots.append(
            ScheduledSlot(
                index=index,
                start_ist=open_at.isoformat(),
                end_ist=close_at.isoformat(),
                account_id=f"{account_prefix}-{session_date}-{timeframe_label(timeframe)}"[:40],
                label=label,
                duration_seconds=duration,
                max_positions=max_positions,
                entry_timeframe_seconds=int(timeframe),
            )
        )
    return DailyPlan(session_date=session_date, account_prefix=account_prefix, slots=slots)


def coverage_summary(slots: List[ScheduledSlot]) -> dict:
    """How much of the session the runs' active windows collectively cover.

    Full-session runs cover the whole day, so this is ~100% by design; it stays
    a real measure in case a run is launched late or a timeframe is dropped.
    """

    if not slots:
        return {"covered_pct": 0.0, "largest_gap_seconds": 0.0, "concurrent_peak": 0}
    session_date = slots[0].start_ist[:10]
    open_at = _ist_datetime(session_date, SESSION_OPEN)
    close_at = _ist_datetime(session_date, SESSION_CLOSE)
    session_seconds = (close_at - open_at).total_seconds()

    intervals = sorted(
        (
            max(open_at, datetime.fromisoformat(slot.start_ist)),
            min(close_at, datetime.fromisoformat(slot.end_ist)),
        )
        for slot in slots
    )
    covered = 0.0
    cursor = open_at
    largest_gap = 0.0
    for start, end in intervals:
        if start > cursor:
            largest_gap = max(largest_gap, (start - cursor).total_seconds())
            cursor = start
        if end > cursor:
            covered += (end - cursor).total_seconds()
            cursor = end
    if cursor < close_at:
        largest_gap = max(largest_gap, (close_at - cursor).total_seconds())

    return {
        "covered_pct": round(min(covered, session_seconds) / session_seconds * 100, 1),
        "largest_gap_seconds": round(largest_gap, 0),
        "concurrent_peak": len(slots),  # all runs are live for the whole session
        "session_open_ist": open_at.isoformat(),
        "session_close_ist": close_at.isoformat(),
    }


def is_trading_day(session_date: str) -> bool:
    """Weekday check. NSE trading holidays are not modelled - see the README."""

    return date.fromisoformat(session_date).weekday() < 5


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def now_ist() -> datetime:
    return datetime.now(IST)
