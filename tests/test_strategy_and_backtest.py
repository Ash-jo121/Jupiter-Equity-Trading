from datetime import datetime, timedelta, timezone

from jupiter_trading.accounts import PaperAccountManager
from jupiter_trading.backtest import BacktestEngine
from jupiter_trading.domain import OrderStatus, Quote, Side
from jupiter_trading.market_data import Candle
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits
from jupiter_trading.repository import InMemoryRepository
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.strategy_engine import (
    MarketCoordinator,
    ProfitTargetLeg,
    StrategyDefinition,
    StrategyService,
    StrategyStatus,
)


def account_manager() -> PaperAccountManager:
    return PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )


def test_profit_target_strategy_enters_and_exits(tmp_path) -> None:
    accounts = account_manager()
    store = ResearchStore(str(tmp_path / "research.db"))
    service = StrategyService(accounts, store)
    coordinator = MarketCoordinator(accounts, service)
    strategy = StrategyDefinition(
        name="BEL target",
        account_id="default",
        legs=[
            ProfitTargetLeg(
                "NSE_EQ|BEL",
                "BEL",
                quantity=2,
                entry_price=100,
                profit_target_pct=1,
                stop_loss_pct=0.5,
            )
        ],
    )
    service.create(strategy)
    service.set_status(strategy.id, StrategyStatus.RUNNING)

    coordinator.on_quote(Quote("NSE_EQ|BEL", 100))
    broker = accounts.get()
    assert broker.positions["NSE_EQ|BEL"].quantity == 2
    assert next(iter(broker.orders.values())).status == OrderStatus.FILLED

    coordinator.on_quote(Quote("NSE_EQ|BEL", 101))
    assert broker.positions["NSE_EQ|BEL"].quantity == 0
    assert [order.side for order in broker.orders.values()] == [Side.BUY, Side.SELL]
    assert [event["type"] for event in service.events(strategy.id)][-2:] == [
        "ENTRY",
        "PROFIT_TARGET",
    ]


def test_strategy_definition_survives_service_restart(tmp_path) -> None:
    accounts = account_manager()
    store = ResearchStore(str(tmp_path / "research.db"))
    first = StrategyService(accounts, store)
    strategy = StrategyDefinition(
        name="Persistent strategy",
        account_id="default",
        legs=[ProfitTargetLeg("NSE_EQ|TEST", "TEST")],
    )
    first.create(strategy)
    first.set_status(strategy.id, StrategyStatus.PAUSED)

    restored = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))

    assert restored.get(strategy.id)["status"] == "PAUSED"
    assert restored.get(strategy.id)["legs"][0]["symbol"] == "TEST"


def test_backtest_produces_persistent_report(tmp_path) -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = [
        Candle(now + timedelta(days=index), price, price, price, price, 1000, 0)
        for index, price in enumerate([100, 100.2, 101, 102])
    ]
    store = ResearchStore(str(tmp_path / "research.db"))
    engine = BacktestEngine(store, FeeSchedule(brokerage_bps=0))

    report = engine.run(
        "NSE_EQ|TEST",
        "TEST",
        candles,
        quantity=10,
        initial_cash=100_000,
        entry_price=100,
        profit_target_pct=1,
        stop_loss_pct=0.5,
    )

    assert report["metrics"]["net_pnl"] == 10
    assert report["metrics"]["round_trips"] == 1
    assert report["exit_reasons"] == ["PROFIT_TARGET"]
    assert store.backtest(report["id"])["symbol"] == "TEST"
