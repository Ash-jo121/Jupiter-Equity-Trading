from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import Event, RLock, Thread
from typing import Callable, List, Optional

from .research_store import ResearchStore
from .schedule import (
    ENTRY_VARIANTS,
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
MIN_CONTINUATION_SECONDS = 30


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    entry_variants: tuple = ENTRY_VARIANTS
    max_positions: int = 2
    reentry_cooldown_seconds: float = 0.0
    account_prefix: str = "auto"
    allocation_per_position: float = 25_000.0
    initial_cash: float = 1_000_000.0
    tick_seconds: float = 30.0
    catch_up_grace_seconds: float = 1800.0  # a full-session run is worth joining late

    def __post_init__(self) -> None:
        if not self.entry_variants:
            raise ValueError("at least one entry variant is required")
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
    """Launches the three-entry experiment at the open and reports at close.

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
            plan = DailyPlan.from_dict(stored)
            # Plans can be created before a deployment changes the configured
            # experiment arms. Until anything has launched, the saved plan is
            # only a draft, so make it match the current scheduler settings.
            # Once a slot has moved past PENDING it is execution history and
            # must never be rewritten.
            configured_modes = [mode for _label, mode in self.config.entry_variants]
            stored_modes = [slot.entry_mode for slot in plan.slots]
            plan_is_draft = all(slot.status == "PENDING" for slot in plan.slots)
            configuration_changed = (
                stored_modes != configured_modes
                or any(slot.max_positions != self.config.max_positions for slot in plan.slots)
                or plan.account_prefix != self.config.account_prefix
            )
            if create and plan_is_draft and configuration_changed:
                plan = build_daily_plan(
                    session_date,
                    variants=self.config.entry_variants,
                    max_positions=self.config.max_positions,
                    account_prefix=self.config.account_prefix,
                )
                self._save(plan)
            return plan
        if not create:
            return None
        plan = build_daily_plan(
            session_date,
            variants=self.config.entry_variants,
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
                if slot.status == "LAUNCHED":
                    if self._continue_interrupted_slot(slot, moment, actions):
                        changed = True
                    continue
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
                    # Opening status and token refreshes can lag briefly. Keep
                    # the slot pending so the next scheduler tick can retry it
                    # until the normal catch-up grace window expires.
                    actions.append({"slot": slot.index, "action": "WAITING_FOR_MARKET"})
                    continue
                try:
                    slot.runner_id = self._launch(slot, self.config)
                    slot.runner_ids = [slot.runner_id]
                    slot.status = "LAUNCHED"
                    actions.append(
                        {"slot": slot.index, "action": "LAUNCHED", "runner_id": slot.runner_id}
                    )
                except Exception as error:  # noqa: BLE001 - one bad arm must not stop the rest
                    slot.status = "FAILED"
                    slot.detail = str(error)[:300]
                    actions.append({"slot": slot.index, "action": "FAILED", "error": slot.detail})
                changed = True
            if changed:
                self._save(plan)

            report_action = self._maybe_report(plan, moment)
            if report_action:
                actions.append(report_action)

        self._last_actions = actions
        return actions

    def _continue_interrupted_slot(
        self, slot: ScheduledSlot, moment: datetime, actions: List[dict]
    ) -> bool:
        """Start a new segment when a deployment ended today's live segment.

        Runner indicator/candle state is intentionally not reconstructed. The
        persisted segment remains immutable and a fresh runner uses the same
        paper account, preserving cash and realised P&L while rebuilding its
        signals from provider candles. This is a continuation in reporting,
        not a claim that in-memory state survived the process restart.
        """

        if not slot.runner_id:
            return False
        previous = self.store.momentum_run(slot.runner_id)
        if not previous:
            return False
        status = previous.get("status")
        stop_reason = previous.get("stop_reason")
        recoverable = stop_reason == "DEPLOYMENT" or (
            stop_reason is None and status == "COMPLETED"
        )
        if status not in {"COMPLETED", "FAILED", "INCOMPLETE"} or not recoverable:
            return False

        end = datetime.fromisoformat(slot.end_ist)
        remaining_seconds = int((end - moment).total_seconds())
        if remaining_seconds < MIN_CONTINUATION_SECONDS:
            return False
        if not self._market_ready():
            actions.append(
                {"slot": slot.index, "action": "WAITING_TO_CONTINUE", "runner_id": slot.runner_id}
            )
            return False

        prior_id = slot.runner_id
        continuation_number = slot.continuation_count + 1
        continuation = replace(
            slot,
            duration_seconds=remaining_seconds,
            continuation_count=continuation_number,
        )
        try:
            next_id = self._launch(continuation, self.config)
        except Exception as error:  # noqa: BLE001 - retry on the next scheduler tick
            slot.detail = f"continuation pending: {str(error)[:260]}"
            actions.append(
                {
                    "slot": slot.index,
                    "action": "CONTINUATION_FAILED",
                    "runner_id": prior_id,
                    "error": str(error)[:300],
                }
            )
            return True

        history = list(slot.runner_ids)
        if prior_id not in history:
            history.append(prior_id)
        history.append(next_id)
        slot.runner_ids = history
        slot.runner_id = next_id
        slot.continuation_count = continuation_number
        slot.detail = f"continued after deployment with {remaining_seconds}s remaining"
        actions.append(
            {
                "slot": slot.index,
                "action": "CONTINUED",
                "previous_runner_id": prior_id,
                "runner_id": next_id,
                "remaining_seconds": remaining_seconds,
            }
        )
        return True

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
        last_end = max(datetime.fromisoformat(slot.end_ist) for slot in plan.slots)
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
            "entry_variants": [
                {"label": label, "entry_mode": mode}
                for label, mode in self.config.entry_variants
            ],
            "max_positions": self.config.max_positions,
            "reentry_cooldown_seconds": self.config.reentry_cooldown_seconds,
            "plan": plan.to_dict() if plan else None,
            "report_ready": bool(self.store.daily_report(session_date)),
            "last_actions": list(self._last_actions),
        }
