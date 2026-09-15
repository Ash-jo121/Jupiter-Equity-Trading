from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Event, RLock, Thread
from typing import Callable, Optional

from .alpaca import US_EASTERN, AlpacaRestClient
from .research_store import ResearchStore


@dataclass(frozen=True)
class UsSchedulerConfig:
    enabled: bool = False
    account_id: str = "alpaca-us"
    entry_mode: str = "MACD_FRESH_CONFIRMED"
    max_positions: int = 2
    allocation_per_position: float = 2_500.0
    candidate_limit: int = 10
    minimum_relative_volume: float = 1.2
    poll_interval_seconds: float = 5.0
    rescan_interval_seconds: float = 285.0
    tick_seconds: float = 30.0
    minimum_remaining_seconds: int = 300

    def __post_init__(self) -> None:
        if self.max_positions < 1 or self.candidate_limit < 1:
            raise ValueError("US scheduler position and candidate limits must be positive")
        if self.allocation_per_position <= 0:
            raise ValueError("US scheduler allocation must be positive")
        if self.tick_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise ValueError("US scheduler intervals must be positive")


class UsDailyScheduler:
    """Starts one Alpaca paper run whenever the US regular session is open.

    Alpaca's clock is the source of truth for holidays, early closes and US
    daylight-saving changes. A restart during an open session can still join
    the day as long as enough time remains to produce a meaningful run.
    """

    market_code = "US"

    def __init__(
        self,
        store: ResearchStore,
        client: Optional[AlpacaRestClient],
        launch: Callable[[str, int, UsSchedulerConfig], str],
        config: Optional[UsSchedulerConfig] = None,
    ) -> None:
        self.store = store
        self.client = client
        self._launch = launch
        self.config = config or UsSchedulerConfig()
        self._lock = RLock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._last_action: Optional[dict] = None
        self._last_clock: Optional[dict] = None

    def tick(self) -> list[dict]:
        if not self.config.enabled:
            return []
        if self.client is None:
            self._last_action = {
                "action": "WAITING_FOR_CREDENTIALS",
                "detail": "Alpaca paper credentials are not configured",
            }
            return [dict(self._last_action)]
        clock = self.client.clock()
        self._last_clock = clock
        timestamp = _parse(clock.get("timestamp"))
        if timestamp is None:
            raise ValueError("Alpaca clock did not include a timestamp")
        session_date = timestamp.astimezone(US_EASTERN).date().isoformat()
        plan = self.store.market_schedule_plan(self.market_code, session_date)
        if plan and plan.get("status") in {"LAUNCHED", "COMPLETED"}:
            return []
        if not clock.get("is_open"):
            return []
        next_close = _parse(clock.get("next_close"))
        if next_close is None:
            raise ValueError("Alpaca clock did not include the regular-session close")
        remaining = int((next_close - timestamp).total_seconds())
        if remaining < self.config.minimum_remaining_seconds:
            action = {
                "action": "SKIPPED",
                "session_date": session_date,
                "detail": "too little regular-session time remains",
            }
            self.store.save_market_schedule_plan(
                self.market_code,
                session_date,
                {**action, "market_code": self.market_code, "status": "SKIPPED"},
            )
            self._last_action = action
            return [action]
        try:
            runner_id = self._launch(session_date, remaining, self.config)
            action = {
                "action": "LAUNCHED",
                "session_date": session_date,
                "runner_id": runner_id,
                "duration_seconds": remaining,
            }
            self.store.save_market_schedule_plan(
                self.market_code,
                session_date,
                {**action, "market_code": self.market_code, "status": "LAUNCHED"},
            )
        except Exception as error:  # noqa: BLE001 - retry transient startup failures
            action = {
                "action": "WAITING_TO_LAUNCH",
                "session_date": session_date,
                "detail": str(error)[:300],
            }
        self._last_action = action
        return [action]

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._loop, name="us-daily-scheduler", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - scheduler survives provider errors
                self._last_action = {"action": "TICK_ERROR", "detail": str(error)[:300]}
            self._stop.wait(self.config.tick_seconds)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        now = datetime.now(US_EASTERN)
        session_date = now.date().isoformat()
        return {
            "market_code": self.market_code,
            "timezone": "America/New_York",
            "regular_session": "09:30-16:00 ET",
            "enabled": self.config.enabled,
            "configured": self.client is not None,
            "running": bool(self._thread and self._thread.is_alive()),
            "session_date": session_date,
            "strategy": self.config.entry_mode,
            "plan": self.store.market_schedule_plan(self.market_code, session_date),
            "clock": self._last_clock,
            "last_action": self._last_action,
        }


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
