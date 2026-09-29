from datetime import datetime, timedelta, timezone

from jupiter_trading.accounts import PaperAccountManager
from jupiter_trading.candidate_queue import CandidateQueue
from jupiter_trading.candle_signals import BarFeatures
from jupiter_trading.domain import Quote
from jupiter_trading.execution_queue import ExecutionIntent, ExecutionQueue
from jupiter_trading.feature_engine import FeatureSnapshot
from jupiter_trading.market_data import Candle
from jupiter_trading.monitoring_manager import MonitoringManager
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits
from jupiter_trading.parallel_monitoring import (
    ParallelMonitoringConfig,
    ParallelMonitoringEngine,
)
from jupiter_trading.repository import SQLiteRepository
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.survey import SurveyInstrument


def _rows(count=15):
    return [
        {
            "instrument_key": f"NSE_EQ|{index:03d}",
            "symbol": f"STOCK{index}",
            "momentum_score": 10 - index / 10,
            "eligible": True,
            "volume_confirmed": True,
            "recent_15m_change_pct": 1.0,
        }
        for index in range(count)
    ]


def test_candidate_queue_is_ranked_bounded_and_replaced():
    queue = CandidateQueue(max_candidates=12)
    first = queue.refresh(_rows(20))
    assert len(first) == 12
    assert [item.rank for item in first] == list(range(1, 13))
    assert queue.version == 1

    second = queue.refresh(list(reversed(_rows(3))))
    assert len(second) == 3
    assert queue.version == 2
    assert queue.snapshot()[0]["symbol"] == "STOCK0"


def test_monitoring_manager_keeps_exactly_ten_reusable_slots():
    clock = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
    queue = CandidateQueue(max_candidates=20)
    queue.refresh(_rows(15), clock)
    manager = MonitoringManager(queue, slot_count=10, lease_seconds=300, cooldown_seconds=600)

    manager.rebalance(clock)
    slots = manager.snapshot(clock)["slots"]
    assert len(slots) == 10
    assert len({slot["instrument_key"] for slot in slots}) == 10

    manager.update_strategy(slots[0]["instrument_key"], "A", "OPEN", "FILLED", clock)
    protected_key = slots[0]["instrument_key"]
    manager.rebalance(clock + timedelta(seconds=301))
    rotated = manager.snapshot(clock + timedelta(seconds=301))["slots"]
    assert len(rotated) == 10
    assert manager.slot_for(protected_key) is not None
    assert manager.slot_for(protected_key).lease_expires_at > clock + timedelta(seconds=301)


def test_cooldown_requires_time_and_a_fresh_ranking_before_reentry():
    clock = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
    queue = CandidateQueue(max_candidates=10)
    queue.refresh(_rows(1), clock)
    manager = MonitoringManager(queue, slot_count=1, lease_seconds=60, cooldown_seconds=600)
    manager.rebalance(clock)
    key = manager.snapshot(clock)["slots"][0]["instrument_key"]

    manager.start_cooldown(key, "STOCK0", "EXIT", clock)
    manager.rebalance(clock + timedelta(seconds=601))
    assert manager.snapshot(clock + timedelta(seconds=601))["slots"][0]["instrument_key"] is None

    queue.refresh(_rows(1), clock + timedelta(seconds=602))
    manager.rebalance(clock + timedelta(seconds=602))
    assert manager.snapshot(clock + timedelta(seconds=602))["slots"][0]["instrument_key"] == key


def test_execution_intent_is_persistent_and_exactly_once(tmp_path):
    store = ResearchStore(str(tmp_path / "research.db"))
    calls = []

    def execute(intent):
        calls.append(intent.id)
        return {"filled_quantity": intent.quantity, "status": "FILLED"}

    queue = ExecutionQueue(store, "session-1", execute)
    intent = ExecutionIntent(
        id="session-1:A:NSE_EQ|001:bar:BUY",
        session_id="session-1",
        strategy="A",
        account_id="account-a",
        instrument_key="NSE_EQ|001",
        symbol="STOCK1",
        side="BUY",
        quantity=2,
        observed_price=100.0,
        reason="TEST",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    assert queue.enqueue(intent) is True
    completed = queue.drain()
    assert completed[0].status == "FILLED"
    assert store.execution_intent(intent.id)["status"] == "FILLED"

    restarted = ExecutionQueue(store, "session-1", execute)
    assert restarted.enqueue(intent) is False
    assert restarted.drain() == []
    assert calls == [intent.id]


def test_monitoring_state_and_events_round_trip(tmp_path):
    store = ResearchStore(str(tmp_path / "research.db"))
    state = {
        "id": "NSE:2026-09-30:parallel-v2",
        "session_date": "2026-09-30",
        "status": "RUNNING",
        "monitoring": {"slots": []},
    }
    store.save_monitoring_session(state)
    sequence = store.add_monitoring_event(
        state["id"],
        "SLOT_ASSIGNED",
        {"slot_id": 1},
        instrument_key="NSE_EQ|001",
        strategy="A",
    )

    assert store.monitoring_session(state["id"]) == state
    events = store.monitoring_events(state["id"], instrument_key="NSE_EQ|001")
    assert events[0]["sequence"] == sequence
    assert events[0]["type"] == "SLOT_ASSIGNED"


def test_one_feature_object_fans_out_to_three_isolated_portfolios(tmp_path, monkeypatch):
    database = str(tmp_path / "paper.db")
    accounts = PaperAccountManager(
        SQLiteRepository(database),
        initial_cash=1_000_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0, tax_bps=0),
        risk_limits=RiskLimits(),
    )
    accounts.update_market_status({"NSE_EQ": "NORMAL_OPEN"})
    store = ResearchStore(database)
    engine = ParallelMonitoringEngine(
        ParallelMonitoringConfig(
            session_id="NSE:2026-09-30:parallel-v2",
            session_date="2026-09-30",
        ),
        [SurveyInstrument("STOCK0", "NSE_EQ|000")],
        object(),
        accounts,
        store,
    )
    bar_start = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
    engine.refresh_candidates(_rows(1), bar_start)
    candles = tuple(
        Candle(
            bar_start + timedelta(minutes=index),
            99 + index,
            101 + index,
            98 + index,
            100 + index,
            10_000,
            0,
        )
        for index in range(3)
    )
    features = BarFeatures(
        bar=candles[-1],
        bar_end=candles[-1].timestamp + timedelta(minutes=1),
        body=1,
        price_range=3,
        lower_wick=1,
        upper_wick=1,
        body_ratio=1 / 3,
        lower_ratio=1 / 3,
        upper_ratio=1 / 3,
        close_location=2 / 3,
        baseline_volume=5_000,
        rvol_1m=2,
        macd=1,
        signal=0.5,
        histogram=0.5,
        h1=0.25,
        h2=0.1,
        delta_bps=1,
        latest_cross_age=0,
        warmup_count=100,
        warmup_seed_count=97,
        session_bar_count=3,
        ready=True,
        quality_reasons=(),
    )
    available_at = features.bar_end + timedelta(seconds=2)
    snapshot = FeatureSnapshot(
        "NSE_EQ|000",
        features,
        candles,
        available_at,
        available_at,
        False,
        False,
    )
    feature_ids = []

    def qualify(_mode, shared, _config):
        feature_ids.append(id(shared))
        return {
            "strategy": _mode,
            "actionable": True,
            "reason": "TEST_SIGNAL",
            "rejection_checks": {"candle": True},
            "mode_checks": {"macd": True},
            "features": shared.to_dict(),
        }

    monkeypatch.setattr("jupiter_trading.parallel_monitoring.evaluate_entry", qualify)
    quote = Quote("NSE_EQ|000", 103, timestamp=available_at + timedelta(seconds=1))
    engine.process_cycle({"NSE_EQ|000": quote}, {"NSE_EQ|000": snapshot}, quote.timestamp)

    assert len(feature_ids) == 3
    assert len(set(feature_ids)) == 1
    assert accounts.get(engine.account_id("A")).positions["NSE_EQ|000"].quantity > 0
    assert accounts.get(engine.account_id("C")).positions["NSE_EQ|000"].quantity > 0
    assert accounts.get(engine.account_id("B")).positions.get("NSE_EQ|000") is None

    restored = ParallelMonitoringEngine(
        engine.config,
        [SurveyInstrument("STOCK0", "NSE_EQ|000")],
        object(),
        accounts,
        store,
    )
    assert len(restored.snapshot()["open_positions"]) == 2
    assert {item["strategy"] for item in restored.snapshot()["open_positions"]} == {
        "A",
        "C",
    }
