from datetime import datetime, timedelta, timezone

from jupiter_trading.accounts import PaperAccountManager
from jupiter_trading.domain import Quote, Side
from jupiter_trading.market_data import Candle
from jupiter_trading.momentum_runner import (
    NIFTY50_INDEX_KEY,
    MomentumReversalRunner,
    MomentumRunnerConfig,
    MomentumRunnerService,
)
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits
from jupiter_trading.repository import InMemoryRepository
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.strategy_engine import MarketCoordinator, StrategyService
from jupiter_trading.survey import SurveyInstrument


class RisingThenReversingMarket:
    def __init__(self) -> None:
        self.prices = iter([100.0, 100.0, 100.03, 100.06, 100.12, 100.06])

    def ltp(self, instrument_keys) -> dict:
        price = next(self.prices)
        return {key: Quote(key, price) for key in instrument_keys}

    def intraday_candles(self, instrument_key, unit, interval) -> list:
        now = datetime.now(timezone.utc)
        return [
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 99, volume, 0)
            for index, volume in enumerate([100, 100, 100, 200, 200, 200, 200])
        ]


class FallingNiftyMarket(RisingThenReversingMarket):
    def __init__(self) -> None:
        self.stock_prices = iter([100.0, 100.0, 100.05, 100.10])
        self.nifty_prices = iter([200.0, 199.9, 199.8])

    def ltp(self, instrument_keys) -> dict:
        keys = list(instrument_keys)
        stock_price = next(self.stock_prices) if "NSE_EQ|TEST" in keys else None
        return {
            key: Quote(
                key,
                next(self.nifty_prices) if key == NIFTY50_INDEX_KEY else stock_price,
            )
            for key in keys
        }


class LowVolumeMarket(RisingThenReversingMarket):
    def intraday_candles(self, instrument_key, unit, interval) -> list:
        now = datetime.now(timezone.utc)
        return [
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 99, volume, 0)
            for index, volume in enumerate([200, 200, 200, 100, 100, 100, 100])
        ]


def test_runner_buys_momentum_and_sells_reversal(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(
            delivery_brokerage_flat=0,
            intraday_brokerage_bps=0,
            delivery_sell_dp_flat=0,
        ),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            duration_seconds=300,
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            reversal_pct=0.04,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RisingThenReversingMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="runner-test",
    )

    runner._scan()
    for _ in range(5):
        runner._poll()

    broker = accounts.get()
    orders = [order for order in broker.orders.values() if order.strategy_id == runner.id]
    assert [order.side for order in orders] == [Side.BUY, Side.SELL]
    assert broker.positions["NSE_EQ|TEST"].quantity == 0
    assert runner.snapshot()["completed_instruments"] == ["NSE_EQ|TEST"]
    exit_event = next(
        event for event in runner.snapshot()["events"] if event["type"] == "EXIT_FILLED"
    )
    assert exit_event["reason"] == "MOMENTUM_REVERSAL"
    entry_event = next(
        event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED"
    )
    assert entry_event["entry_signal"]["reason"] == "UPWARD_MOVEMENT_CONFIRMED"
    assert entry_event["entry_signal"]["window_change_pct"] >= 0.02
    monitoring = runner.snapshot()["monitoring"]
    assert len(monitoring) == 5
    assert monitoring[0]["decision"] == "BUILDING_PRICE_HISTORY"
    assert any(item["decision"] == "ENTRY_FILLED" for item in monitoring)
    assert all(item["nifty_price"] is not None for item in monitoring)


def test_completed_run_history_survives_service_restart(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    payload = {
        "id": "saved-run",
        "status": "COMPLETED",
        "started_at": "2026-08-27T09:00:00+00:00",
        "config": {"account_id": "momentum"},
        "fills": [{"symbol": "CIPLA"}],
        "session_pnl": -12.5,
    }
    store.save_momentum_run(payload)

    restored = MomentumRunnerService(ResearchStore(str(tmp_path / "research.db")))

    assert restored.get("saved-run")["fills"][0]["symbol"] == "CIPLA"
    assert restored.list()[0]["session_pnl"] == -12.5


def test_falling_nifty_blocks_an_otherwise_valid_entry(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default", entry_momentum_pct=0.02),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=FallingNiftyMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
    )

    runner._scan()
    for _ in range(3):
        runner._poll()

    assert not accounts.get().orders
    assert runner.snapshot()["monitoring"][-1]["decision"] == (
        "NIFTY_SHORT_TERM_NOT_POSITIVE"
    )


def test_low_relative_volume_is_rejected_during_scan(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default"),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=LowVolumeMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
    )

    runner._scan()

    snapshot = runner.snapshot()
    assert snapshot["candidates"] == []
    scan = next(event for event in snapshot["events"] if event["type"] == "SCAN")
    assert scan["low_volume_rejections"] == [
        {"symbol": "TEST", "relative_volume": 0.5}
    ]
