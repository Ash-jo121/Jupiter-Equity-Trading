from datetime import datetime

import pytest

from jupiter_trading.automation import DailyScheduler, SchedulerConfig
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.schedule import (
    ENTRY_TIMEFRAMES,
    build_daily_plan,
    coverage_summary,
    is_trading_day,
)

MONDAY = "2026-08-31"
SATURDAY = "2026-08-29"


def test_the_default_plan_is_one_run_per_entry_timeframe() -> None:
    plan = build_daily_plan(MONDAY)
    assert len(plan.slots) == len(ENTRY_TIMEFRAMES)
    assert {slot.entry_timeframe_seconds for slot in plan.slots} == set(ENTRY_TIMEFRAMES)
    assert all(slot.max_positions == 5 for slot in plan.slots)


def test_every_run_spans_the_whole_session() -> None:
    plan = build_daily_plan(MONDAY)
    open_at = datetime.fromisoformat(f"{MONDAY}T09:15:00+05:30")
    close_at = datetime.fromisoformat(f"{MONDAY}T15:30:00+05:30")
    for slot in plan.slots:
        assert datetime.fromisoformat(slot.start_ist) == open_at
        assert datetime.fromisoformat(slot.end_ist) == close_at
        assert slot.duration_seconds == int((close_at - open_at).total_seconds())


def test_full_session_runs_cover_the_entire_day() -> None:
    coverage = coverage_summary(build_daily_plan(MONDAY).slots)
    assert coverage["covered_pct"] == 100.0
    assert coverage["largest_gap_seconds"] == 0
    assert coverage["concurrent_peak"] == len(ENTRY_TIMEFRAMES)


def test_each_arm_gets_its_own_account() -> None:
    accounts = [slot.account_id for slot in build_daily_plan(MONDAY).slots]
    assert len(set(accounts)) == len(accounts)


def test_a_custom_set_of_timeframes_is_honoured() -> None:
    plan = build_daily_plan(MONDAY, timeframes=(0, 60), max_positions=3)
    assert len(plan.slots) == 2
    assert all(slot.max_positions == 3 for slot in plan.slots)


def test_duplicate_timeframes_collapse() -> None:
    plan = build_daily_plan(MONDAY, timeframes=(60, 60, 300))
    assert [slot.entry_timeframe_seconds for slot in plan.slots] == [60, 300]


def test_weekends_are_not_trading_days() -> None:
    assert is_trading_day(MONDAY) is True
    assert is_trading_day(SATURDAY) is False


def test_an_empty_timeframe_list_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_daily_plan(MONDAY, timeframes=())


# -- scheduler -----------------------------------------------------------------


def _scheduler(tmp_path, **config):
    store = ResearchStore(str(tmp_path / "sched.db"))
    launched, reports = [], []

    def launch(slot, cfg):
        launched.append(slot)
        return f"runner-{slot.index}"

    def build_report(session_date):
        store.save_daily_report(session_date, {"session_date": session_date})
        reports.append(session_date)
        return {"session_date": session_date}

    scheduler = DailyScheduler(
        store, launch, build_report, SchedulerConfig(enabled=True, **config)
    )
    return scheduler, store, launched, reports


def _at(day: str, hour: int, minute: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00+05:30")


def test_all_arms_launch_at_the_open(tmp_path) -> None:
    scheduler, _, launched, _ = _scheduler(tmp_path)
    scheduler.tick(_at(MONDAY, 9, 0))  # before open
    assert launched == []
    scheduler.tick(_at(MONDAY, 9, 16))  # just after open
    assert len(launched) == len(ENTRY_TIMEFRAMES)
    assert {slot.entry_timeframe_seconds for slot in launched} == set(ENTRY_TIMEFRAMES)


def test_the_cooldown_and_positions_reach_the_launch(tmp_path) -> None:
    scheduler, _, launched, _ = _scheduler(
        tmp_path, max_positions=5, reentry_cooldown_seconds=900
    )
    scheduler.tick(_at(MONDAY, 9, 16))
    assert scheduler.config.reentry_cooldown_seconds == 900
    assert all(slot.max_positions == 5 for slot in launched)


def test_a_full_day_launches_all_arms_then_reports_after_close(tmp_path) -> None:
    scheduler, store, launched, reports = _scheduler(tmp_path)
    scheduler.tick(_at(MONDAY, 9, 16))
    assert len(launched) == len(ENTRY_TIMEFRAMES)
    assert reports == []  # runs still going
    scheduler.tick(_at(MONDAY, 15, 34))  # a few minutes past close
    assert reports == [MONDAY]
    assert store.daily_report(MONDAY) is not None


def test_arms_are_not_relaunched_after_a_restart(tmp_path) -> None:
    scheduler, store, launched, _ = _scheduler(tmp_path)
    scheduler.tick(_at(MONDAY, 9, 16))
    assert len(launched) == len(ENTRY_TIMEFRAMES)
    resumed, _, relaunched, _ = _scheduler(tmp_path)
    resumed.store = store
    resumed.tick(_at(MONDAY, 9, 30))
    assert relaunched == []


def test_arms_missed_past_the_grace_window_are_skipped(tmp_path) -> None:
    scheduler, store, launched, _ = _scheduler(
        tmp_path, catch_up_grace_seconds=1800
    )
    scheduler.tick(_at(MONDAY, 11, 0))  # over an hour after open
    assert launched == []
    plan = store.schedule_plan(MONDAY)
    assert {slot["status"] for slot in plan["slots"]} == {"SKIPPED"}


def test_no_launch_without_market_data(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "sched.db"))
    launched = []
    scheduler = DailyScheduler(
        store,
        lambda slot, cfg: launched.append(slot) or "x",
        lambda date: {},
        SchedulerConfig(enabled=True),
        market_ready=lambda: False,
    )
    scheduler.tick(_at(MONDAY, 9, 16))
    assert launched == []
    plan = store.schedule_plan(MONDAY)
    assert all(slot["detail"] == "market data unavailable" for slot in plan["slots"])


def test_a_disabled_scheduler_does_nothing(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "sched.db"))
    scheduler = DailyScheduler(
        store, lambda slot, cfg: "x", lambda date: {}, SchedulerConfig(enabled=False)
    )
    assert scheduler.tick(_at(MONDAY, 10, 0)) == []


def test_weekends_are_ignored(tmp_path) -> None:
    scheduler, _store, launched, _ = _scheduler(tmp_path)
    assert scheduler.tick(_at(SATURDAY, 9, 16)) == []
    assert launched == []


def test_exchange_holidays_are_ignored(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "sched.db"))
    launched = []
    scheduler = DailyScheduler(
        store,
        lambda slot, cfg: launched.append(slot) or "x",
        lambda date: {},
        SchedulerConfig(enabled=True),
        trading_day_check=lambda day: day != "2026-09-14",
    )

    assert scheduler.tick(_at("2026-09-14", 9, 16)) == []
    assert launched == []
    assert store.schedule_plan("2026-09-14") is None


def test_one_failing_arm_does_not_stop_the_others(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "sched.db"))
    launched = []

    def launch(slot, cfg):
        if slot.index == 0:
            raise RuntimeError("account clash")
        launched.append(slot)
        return f"runner-{slot.index}"

    scheduler = DailyScheduler(
        store, launch, lambda date: {}, SchedulerConfig(enabled=True)
    )
    scheduler.tick(_at(MONDAY, 9, 16))
    plan = store.schedule_plan(MONDAY)
    assert plan["slots"][0]["status"] == "FAILED"
    assert len(launched) == len(ENTRY_TIMEFRAMES) - 1


def test_initial_cash_must_cover_the_positions() -> None:
    with pytest.raises(ValueError, match="initial_cash must cover"):
        SchedulerConfig(max_positions=5, allocation_per_position=100_000, initial_cash=200_000)
