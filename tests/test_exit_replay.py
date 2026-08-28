from datetime import datetime, timedelta, timezone

import pytest

from jupiter_trading.backtest import ExitReplayEngine, ReplayGates
from jupiter_trading.paper_broker import FeeSchedule
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.trade_rules import EntryPolicy, ExitPolicy

START = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)
# A clean run up from 1000 to 1012 followed by a fade back to 1004.
PRICES = [
    1000.0, 1000.2, 1000.6, 1001.5, 1003.0, 1005.0, 1007.0, 1009.0,
    1011.0, 1012.0, 1011.5, 1010.0, 1008.0, 1006.0, 1004.0, 1004.0,
]


def recorded_run(entry_index: int = 3, **overrides) -> dict:
    monitoring = [
        {
            "timestamp": (START + timedelta(seconds=5 * index)).isoformat(),
            "symbol": "TEST",
            "instrument_key": "NSE_EQ|TEST",
            "price": price,
            "relative_volume": 1.5 if index < 10 else 0.8,
            "nifty_window_change_pct": 0.05,
            "nifty_recent_15m_change_pct": 0.08,
            "decision": "ENTRY_FILLED" if index == entry_index else "WATCHING",
        }
        for index, price in enumerate(PRICES)
    ]
    return {
        "id": "recorded-run",
        "initial_equity": 100_000,
        "monitoring": monitoring,
        "config": {
            "allocation_per_position": 100_000,
            "max_positions": 2,
            "entry_bars": 3,
            "entry_momentum_pct": 0.05,
            "minimum_relative_volume": 1.2,
        },
        "fills": [],
        "events": [],
        **overrides,
    }


def engine(tmp_path) -> ExitReplayEngine:
    return ExitReplayEngine(
        ResearchStore(str(tmp_path / "research.db")), FeeSchedule()
    )


def test_exits_only_replays_the_recorded_entry_under_the_new_rule(tmp_path) -> None:
    report = engine(tmp_path).run(
        recorded_run(), ExitPolicy(time_stop_seconds=600), mode="EXITS_ONLY"
    )
    assert report["candidate"]["round_trips"] == 1
    trade = report["trades"][0]
    assert trade["entry_time"] == (START + timedelta(seconds=15)).isoformat()
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["net_pnl"] > 0
    assert report["mode"] == "EXITS_ONLY"


def test_the_replayed_stop_never_moves_down(tmp_path) -> None:
    report = engine(tmp_path).run(
        recorded_run(), ExitPolicy(time_stop_seconds=600), mode="EXITS_ONLY"
    )
    stops = [point["stop_price"] for point in report["trades"][0]["path"]]
    assert stops == sorted(stops)
    assert {point["phase"] for point in report["trades"][0]["path"]} >= {"LOCK", "RIDE"}


def test_full_mode_finds_its_own_entry_from_the_three_bar_rule(tmp_path) -> None:
    report = engine(tmp_path).run(
        recorded_run(entry_index=99),
        ExitPolicy(time_stop_seconds=600),
        entry_policy=EntryPolicy(bars=3, minimum_rise_pct=0.05),
        mode="FULL",
    )
    assert report["candidate"]["round_trips"] == 1
    # EXITS_ONLY would have taken nothing, because no row was flagged as an entry.
    assert report["trades"][0]["entry_check"]["triggered"]


def test_full_mode_respects_the_volume_gate(tmp_path) -> None:
    report = engine(tmp_path).run(
        recorded_run(entry_index=99),
        ExitPolicy(),
        mode="FULL",
        gates=ReplayGates(minimum_relative_volume=5.0, allocation_per_position=100_000),
    )
    assert report["candidate"]["round_trips"] == 0
    blocked = {row["decision"] for row in report["decision_counts"]}
    assert "RELATIVE_VOLUME_TOO_LOW" in blocked


def test_full_mode_can_opt_back_into_the_nifty_gate(tmp_path) -> None:
    source = recorded_run(entry_index=99)
    for row in source["monitoring"]:
        row["nifty_recent_15m_change_pct"] = -0.04
    report = engine(tmp_path).run(
        source,
        ExitPolicy(),
        mode="FULL",
        gates=ReplayGates(require_positive_nifty=True, allocation_per_position=100_000),
    )
    assert report["candidate"]["round_trips"] == 0
    blocked = {row["decision"] for row in report["decision_counts"]}
    assert "NIFTY_SHORT_TERM_NOT_POSITIVE" in blocked


def test_a_tight_time_stop_closes_a_trade_that_never_locks(tmp_path) -> None:
    flat = recorded_run()
    for row in flat["monitoring"]:
        row["price"] = 1000.0
    flat["monitoring"][3]["decision"] = "ENTRY_FILLED"
    report = engine(tmp_path).run(
        flat,
        ExitPolicy(time_stop_seconds=20),
        mode="EXITS_ONLY",
        gates=ReplayGates(allocation_per_position=50_000),
    )
    assert report["trades"][0]["exit_reason"] == "TIME_STOP"


def test_the_baseline_is_rebuilt_from_the_runs_own_fills(tmp_path) -> None:
    source = recorded_run()
    source["fills"] = [
        {"symbol": "TEST", "side": "BUY", "gross_value": 100_000.0, "fees": 25.0},
        {"symbol": "TEST", "side": "SELL", "gross_value": 100_300.0, "fees": 31.0},
    ]
    source["events"] = [
        {"type": "EXIT_FILLED", "symbol": "TEST", "reason": "MOMENTUM_REVERSAL"}
    ]
    report = engine(tmp_path).run(source, ExitPolicy(time_stop_seconds=600))
    assert report["baseline"]["net_pnl"] == pytest.approx(244.0)
    assert report["baseline"]["round_trips"] == 1
    assert report["delta"]["net_pnl"] == pytest.approx(
        report["candidate"]["net_pnl"] - 244.0
    )


def test_replayed_fills_are_charged_through_the_real_fee_model(tmp_path) -> None:
    report = engine(tmp_path).run(
        recorded_run(), ExitPolicy(time_stop_seconds=600), mode="EXITS_ONLY"
    )
    trade = report["trades"][0]
    assert trade["fees"] > 0
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["fees"])
    assert trade["cost_floor_pct"] > 0


def test_the_report_is_saved_for_later_comparison(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    report = ExitReplayEngine(store, FeeSchedule()).run(
        recorded_run(), ExitPolicy(time_stop_seconds=600)
    )
    saved = store.backtest(report["id"])
    assert saved["strategy_type"] == "EXIT_REPLAY"
    assert saved["source_run_id"] == "recorded-run"


def test_a_run_without_a_recorded_trace_cannot_be_replayed(tmp_path) -> None:
    with pytest.raises(ValueError, match="no monitoring trace"):
        engine(tmp_path).run({"id": "empty", "monitoring": []}, ExitPolicy())


def test_an_unknown_mode_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="EXITS_ONLY or FULL"):
        engine(tmp_path).run(recorded_run(), ExitPolicy(), mode="GUESS")
