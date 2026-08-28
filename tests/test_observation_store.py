from datetime import datetime, timedelta, timezone

import pytest

from jupiter_trading.backtest import ExitReplayEngine, ReplayGates
from jupiter_trading.paper_broker import FeeSchedule
from jupiter_trading.research_store import ResearchStore, _session_date
from jupiter_trading.trade_rules import EntryPolicy, ExitPolicy

START = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)


def observation(index, price, symbol="TCS", run="run-a", decision="WATCHING"):
    return {
        "timestamp": (START + timedelta(seconds=5 * index)).isoformat(),
        "symbol": symbol,
        "instrument_key": f"NSE_EQ|{symbol}",
        "price": price,
        "decision": decision,
        "relative_volume": 1.6,
        "nifty_window_change_pct": 0.04,
        "nifty_recent_15m_change_pct": 0.06,
        "run": run,
    }


def test_a_nse_session_is_grouped_by_indian_calendar_day() -> None:
    # 09:15 IST open and 15:30 IST close are 03:45 and 10:00 UTC on the same day.
    assert _session_date("2026-08-28T03:45:00+00:00") == "2026-08-28"
    assert _session_date("2026-08-28T10:00:00+00:00") == "2026-08-28"
    # An evening UTC timestamp is already the next Indian day.
    assert _session_date("2026-08-28T19:30:00+00:00") == "2026-08-29"
    assert _session_date("2026-08-28T04:00:00") == "2026-08-28"


def test_observations_round_trip_and_stay_in_recorded_order(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    rows = [observation(index, 1000 + index) for index in range(5)]
    assert store.add_observations("run-a", rows) == 5

    stored = store.observations(run_id="run-a")
    assert [row["price"] for row in stored] == [1000, 1001, 1002, 1003, 1004]
    assert store.observation_count("run-a") == 5


def test_observations_can_be_filtered_by_symbol_and_day(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    store.add_observations("run-a", [observation(0, 1000, symbol="TCS")])
    store.add_observations("run-a", [observation(1, 500, symbol="INFY")])
    store.add_observations("run-b", [observation(2, 1002, symbol="TCS")])

    assert len(store.observations(symbol="TCS")) == 2
    assert len(store.observations(run_id="run-b")) == 1
    assert len(store.observations(session_date="2026-08-28")) == 3
    assert store.observations(session_date="1999-01-01") == []
    assert len(store.observations(instrument_key="NSE_EQ|INFY")) == 1


def test_a_limit_caps_how_much_of_a_trace_is_returned(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    store.add_observations("run-a", [observation(i, 1000 + i) for i in range(50)])
    assert len(store.observations(run_id="run-a", limit=10)) == 10


def test_an_empty_batch_writes_nothing(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    assert store.add_observations("run-a", []) == 0
    assert store.observed_sessions() == []


def test_observed_sessions_summarises_what_is_available_to_backtest(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    store.add_observations("run-a", [observation(i, 1000 + i, symbol="TCS") for i in range(3)])
    store.add_observations("run-b", [observation(i, 500 + i, symbol="INFY") for i in range(2)])

    sessions = store.observed_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_date"] == "2026-08-28"
    assert sessions[0]["observations"] == 5
    assert sessions[0]["symbols"] == 2
    assert sessions[0]["runs"] == 2


def test_a_whole_session_can_be_replayed_across_several_runs(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    rise = [1000.0, 1000.2, 1000.6, 1001.5, 1004.0, 1008.0, 1012.0, 1006.0, 1002.0, 1000.0]
    store.add_observations(
        "run-a", [observation(i, price, symbol="TCS") for i, price in enumerate(rise)]
    )
    store.add_observations(
        "run-b", [observation(i, price, symbol="INFY") for i, price in enumerate(rise)]
    )

    report = ExitReplayEngine(store, FeeSchedule()).replay_session(
        "2026-08-28",
        exit_policy=ExitPolicy(time_stop_seconds=600),
        entry_policy=EntryPolicy(bars=3, minimum_rise_pct=0.05, cost_floor_multiple=0),
        gates=ReplayGates(allocation_per_position=50_000, max_positions=2),
    )
    assert report["strategy_type"] == "SESSION_REPLAY"
    assert report["session_date"] == "2026-08-28"
    # Both stocks traded, which a single run's trace alone could not have shown.
    assert {trade["symbol"] for trade in report["trades"]} == {"TCS", "INFY"}


def test_the_same_instrument_seen_by_two_runs_is_not_counted_twice(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    rows = [observation(i, 1000.0 + i, symbol="TCS") for i in range(8)]
    store.add_observations("run-a", rows)
    store.add_observations("run-b", rows)  # overlapping runs watched the same stock

    report = ExitReplayEngine(store, FeeSchedule()).replay_session(
        "2026-08-28", gates=ReplayGates(allocation_per_position=50_000), persist=False
    )
    assert report["raw_observations"] == 16
    assert report["deduplicated_observations"] == 8


def test_replaying_a_day_with_no_recorded_ticks_is_rejected(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    with pytest.raises(ValueError, match="no observations recorded"):
        ExitReplayEngine(store, FeeSchedule()).replay_session("2026-01-01")
