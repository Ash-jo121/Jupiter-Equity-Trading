from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, RLock, Thread
from typing import Callable, List, Optional

from .research_store import ResearchStore
from .schedule import (
    IST,
    DailyPlan,
    ScheduledSlot,
    build_daily_plan,
    is_trading_day,
    now_ist,
)

# How long after a slot's planned end we wait before compiling the day's report,
# so the last run has finished writing its final snapshot.
REPORT_DELAY_SECONDS = 180


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    entry_timeframes: tuple = (0, 60, 180, 300)
    max_positions: int = 5
    reentry_cooldown_seconds: float = 900.0  # 15 min before a stock can re-enter
    account_prefix: str = "auto"
    allocation_per_position: float = 100_000.0
    initial_cash: float = 600_000.0  # must cover max_positions x allocation
    tick_seconds: float = 30.0
    catch_up_grace_seconds: float = 1800.0  # a full-session run is worth joining late

    def __post_init__(self) -> None:
        if not self.entry_timeframes:
            raise ValueError("at least one entry timeframe is required")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least one")
        if self.allocation_per_position <= 0:
            raise ValueError("allocation_per_position must be positive")
        if self.reentry_cooldown_seconds < 0:
            raise ValueError("reentry_cooldown_seconds cannot be negative")
        if self.initial_cash < self.max_positions * self.allocation_per_position:
            raise ValueError("initial_cash must cover max_positions x allocation")
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")


class DailyScheduler:
    """Launches one full-session run per entry timeframe at the open, reports at close.

    The decision logic lives in `tick`, which is a pure function of the current
    time and the persisted plan - so it can be unit tested without threads or a
    real clock. `start` merely calls `tick` on an interval in a daemon thread.

    A slot is launched once, when the session opens (or within a grace window if
    the process was down at 09:15). State is persisted per slot, so a restart
    mid-session resumes without relaunching or skipping.
    """

    def __init__(
        self,
        store: ResearchStore,
        launch: Callable[[ScheduledSlot, SchedulerConfig], str],
        build_report: Callable[[str], dict],
        config: Optional[SchedulerConfig] = None,
        market_ready: Optional[Callable[[], bool]] = None,
        trading_day_check: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.store = store
        self._launch = launch
        self._build_report = build_report
        self.config = config or SchedulerConfig()
        self._market_ready = market_ready or (lambda: True)
        self._trading_day_check = trading_day_check or is_trading_day
        self._lock = RLock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._last_actions: List[dict] = []

    # -- plan management ----------------------------------------------------

    def plan_for(self, session_date: str, create: bool = False) -> Optional[DailyPlan]:
        stored = self.store.schedule_plan(session_date)
        if stored:
            return DailyPlan.from_dict(stored)
        if not create:
            return None
        plan = build_daily_plan(
            session_date,
            timeframes=self.config.entry_timeframes,
            max_positions=self.config.max_positions,
            account_prefix=self.config.account_prefix,
        )
        self.store.save_schedule_plan(session_date, plan.to_dict())
        return plan

    def _save(self, plan: DailyPlan) -> None:
        self.store.save_schedule_plan(plan.session_date, plan.to_dict())

    # -- the testable core --------------------------------------------------

    def tick(self, now: Optional[datetime] = None) -> List[dict]:
        """Advance the schedule for the current moment. Returns actions taken."""

        moment = now or now_ist()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)
        session_date = moment.astimezone(IST).date().isoformat()
        actions: List[dict] = []
        if not self.config.enabled:
            return actions
        if not self._trading_day_check(session_date):
            return actions

        with self._lock:
            plan = self.plan_for(session_date, create=True)
            if plan is None:
                return actions
            changed = False
            for slot in plan.slots:
                if slot.status != "PENDING":
                    continue
                start = datetime.fromisoformat(slot.start_ist)
                if moment < start:
                    continue
                # Too late to be meaningful - the run would barely overlap its window.
                if moment > start + timedelta(seconds=self.config.catch_up_grace_seconds):
                    slot.status = "SKIPPED"
                    slot.detail = "missed launch window"
                    changed = True
                    actions.append({"slot": slot.index, "action": "SKIPPED"})
                    continue
                if not self._market_ready():
                    slot.status = "SKIPPED"
                    slot.detail = "market data unavailable"
                    changed = True
                    actions.append({"slot": slot.index, "action": "SKIPPED_NO_MARKET"})
                    continue
                try:
                    slot.runner_id = self._launch(slot, self.config)
                    slot.status = "LAUNCHED"
                    actions.append(
                        {"slot": slot.index, "action": "LAUNCHED", "runner_id": slot.runner_id}
                    )
                except Exception as error:  # noqa: BLE001 - one bad arm must not stop the rest
                    slot.status = "FAILED"
                    slot.detail = str(error)[:300]
                    actions.append(
                        {"slot": slot.index, "action": "FAILED", "error": slot.detail}
                    )
                changed = True
            if changed:
                self._save(plan)

            report_action = self._maybe_report(plan, moment)
            if report_action:
                actions.append(report_action)

        self._last_actions = actions
        return actions

    def _maybe_report(self, plan: DailyPlan, moment: datetime) -> Optional[dict]:
        """Build the day's report once every slot has finished and settled."""

        if not plan.slots:
            return None
        if self.store.daily_report(plan.session_date):
            return None
        launched = [slot for slot in plan.slots if slot.status == "LAUNCHED"]
        pending = [slot for slot in plan.slots if slot.status == "PENDING"]
        if pending:
            return None  # some slot has not even started yet
        last_end = max(
            datetime.fromisoformat(slot.end_ist) for slot in plan.slots
        )
        if launched and moment < last_end + timedelta(seconds=REPORT_DELAY_SECONDS):
            return None  # let the final run settle before summarising
        try:
            self._build_report(plan.session_date)
            return {"action": "REPORT_BUILT", "session_date": plan.session_date}
        except Exception as error:  # noqa: BLE001 - a report failure must not kill the loop
            return {"action": "REPORT_FAILED", "error": str(error)[:300]}

    # -- thread lifecycle ---------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._loop, name="daily-scheduler", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - the loop must outlive any tick
                self._last_actions = [{"action": "TICK_ERROR", "error": str(error)[:300]}]
            self._stop.wait(self.config.tick_seconds)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        session_date = now_ist().date().isoformat()
        plan = self.plan_for(session_date, create=False)
        return {
            "enabled": self.config.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "session_date": session_date,
            "is_trading_day": self._trading_day_check(session_date),
            "entry_timeframes": list(self.config.entry_timeframes),
            "max_positions": self.config.max_positions,
            "reentry_cooldown_seconds": self.config.reentry_cooldown_seconds,
            "plan": plan.to_dict() if plan else None,
            "report_ready": bool(self.store.daily_report(session_date)),
            "last_actions": list(self._last_actions),
        }
