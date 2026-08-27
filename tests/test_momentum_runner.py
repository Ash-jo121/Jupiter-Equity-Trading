from datetime import datetime, timedelta, timezone

from jupiter_trading.accounts import PaperAccountManager
from jupiter_trading.domain import Quote, Side
from jupiter_trading.market_data import Candle
from jupiter_trading.momentum_runner import (
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
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 99, 1000, 0)
            for index in range(4)
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
