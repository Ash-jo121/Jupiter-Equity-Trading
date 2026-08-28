from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .accounts import PaperAccountManager
from .backtest import BacktestEngine
from .config import Settings
from .domain import DepthLevel, Order, OrderType, Product, Quote, Side, Validity
from .instrument_search import InstrumentSearchError, UpstoxInstrumentSearch
from .market_data import MarketDataError, UpstoxMarketData
from .market_stream import UpstoxMarketStream
from .momentum_runner import (
    MomentumReversalRunner,
    MomentumRunnerConfig,
    MomentumRunnerService,
)
from .repository import SQLiteRepository
from .research_store import ResearchStore
from .strategy_engine import (
    MarketCoordinator,
    ProfitTargetLeg,
    StrategyDefinition,
    StrategyService,
    StrategyStatus,
)
from .survey import MarketSurvey, SurveyInstrument
from .universe import Nifty50Universe, Nifty100Universe, UniverseError


class OrderRequest(BaseModel):
    instrument_key: str = Field(examples=["NSE_EQ|INE848E01016"])
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    strategy_id: Optional[str] = None
    product: Product = Product.DELIVERY
    validity: Validity = Validity.DAY
    account_id: str = "default"


class ModifyOrderRequest(BaseModel):
    quantity: Optional[int] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    validity: Optional[Validity] = None


class DepthRequest(BaseModel):
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)


class QuoteRequest(BaseModel):
    instrument_key: str
    last_price: float = Field(gt=0)
    bid: Optional[float] = Field(default=None, gt=0)
    ask: Optional[float] = Field(default=None, gt=0)
    bids: List[DepthRequest] = Field(default_factory=list)
    asks: List[DepthRequest] = Field(default_factory=list)
    timestamp: Optional[datetime] = None


class AccountCreateRequest(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=100)
    initial_cash: float = Field(gt=0)


class AccountResetRequest(BaseModel):
    confirm: bool
    initial_cash: Optional[float] = Field(default=None, gt=0)


class StreamRequest(BaseModel):
    instrument_keys: List[str] = Field(min_length=1)
    mode: Literal["ltpc", "full"] = "full"


class StrategyLegRequest(BaseModel):
    instrument_key: str
    symbol: str
    quantity: int = Field(default=1, gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    profit_target_pct: float = Field(default=0.5, gt=0)
    stop_loss_pct: float = Field(default=0.35, gt=0)
    absolute_profit_target: Optional[float] = Field(default=None, gt=0)


class StrategyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    account_id: str = "default"
    legs: List[StrategyLegRequest] = Field(min_length=1)


class SurveyInstrumentRequest(BaseModel):
    symbol: str
    instrument_key: str


class SurveyRequest(BaseModel):
    instruments: List[SurveyInstrumentRequest] = Field(min_length=1, max_length=50)


class MomentumRunRequest(BaseModel):
    account_id: str = Field(
        default="momentum", pattern=r"^[a-zA-Z0-9_-]{1,40}$"
    )
    initial_cash: float = Field(default=100_000, gt=0)
    duration_seconds: int = Field(default=300, ge=30, le=3_600)
    poll_interval_seconds: float = Field(default=5, ge=1, le=60)
    rescan_interval_seconds: float = Field(default=60, ge=15, le=600)
    max_positions: int = Field(default=2, ge=1, le=10)
    allocation_per_position: float = Field(default=25_000, gt=0)
    candidate_limit: int = Field(default=10, ge=1, le=50)
    minimum_score: float = Field(default=0.15, ge=0)
    minimum_relative_volume: float = Field(default=1.2, ge=1)
    entry_momentum_pct: float = Field(default=0.10, gt=0)
    reversal_pct: float = Field(default=0.10, gt=0)
    hard_stop_pct: float = Field(default=0.35, gt=0)


class BacktestRequest(BaseModel):
    instrument_key: str
    symbol: str
    from_date: date
    to_date: date
    unit: Literal["minutes", "hours", "days", "weeks", "months"] = "days"
    interval: int = Field(default=1, gt=0)
    quantity: int = Field(default=1, gt=0)
    initial_cash: float = Field(default=100_000, gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    profit_target_pct: float = Field(default=0.5, gt=0)
    stop_loss_pct: float = Field(default=0.35, gt=0)
    absolute_profit_target: Optional[float] = Field(default=None, gt=0)


def create_app(
    settings: Optional[Settings] = None,
    instrument_search: Optional[UpstoxInstrumentSearch] = None,
    market_data_client: Optional[UpstoxMarketData] = None,
) -> FastAPI:
    settings = settings or Settings()
    repository = SQLiteRepository(settings.database_path)
    accounts = PaperAccountManager(
        repository=repository,
        initial_cash=settings.initial_cash,
        slippage_bps=settings.slippage_bps,
        fee_schedule=settings.fee_schedule,
        risk_limits=settings.risk_limits,
    )
    research_store = ResearchStore(settings.database_path)
    try:
        accounts.get("momentum")
    except KeyError:
        accounts.create("momentum", "Momentum research account", 100_000)
    strategies = StrategyService(accounts, research_store)
    coordinator = MarketCoordinator(accounts, strategies)
    market_stream = UpstoxMarketStream(
        settings.upstox_access_token,
        coordinator.on_quote,
        coordinator.update_market_status,
    )
    search_client = instrument_search or UpstoxInstrumentSearch(settings.upstox_access_token)
    backtests = BacktestEngine(research_store, settings.fee_schedule)
    nifty50 = Nifty50Universe()
    nifty100 = Nifty100Universe()
    momentum_runners = MomentumRunnerService(research_store)

    def market_data() -> UpstoxMarketData:
        return market_data_client or _upstox_client(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configured_keys = (
            list(settings.upstox_stream_instruments)
            if settings.upstox_stream_auto_start
            else []
        )
        strategy_keys = [
            leg["instrument_key"]
            for strategy in strategies.list()
            if strategy["status"] == StrategyStatus.RUNNING.value
            for leg in strategy["legs"]
        ]
        startup_keys = list(dict.fromkeys(configured_keys + strategy_keys))
        if startup_keys:
            market_stream.start(
                startup_keys,
                "full" if strategy_keys else settings.upstox_stream_mode,
            )
        try:
            yield
        finally:
            momentum_runners.stop_all()
            market_stream.stop()

    app = FastAPI(
        title="Jupiter Paper Trading",
        version="0.2.0",
        description="Private, paper-only Indian equity strategy research API.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.broker = accounts.get()
    app.state.market_stream = market_stream
    app.state.instrument_search = search_client
    app.state.strategies = strategies
    app.state.research_store = research_store
    app.state.backtests = backtests
    app.state.momentum_runners = momentum_runners

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "paper",
            "upstox_configured": bool(settings.upstox_access_token),
            "paper_accounts": len(accounts.list()),
            "strategies": len(strategies.list()),
        }

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("http://localhost:3000/")

    @app.get("/paper/accounts")
    def list_accounts() -> list:
        return accounts.list()

    @app.post("/paper/accounts", status_code=201)
    def create_account(request: AccountCreateRequest) -> dict:
        try:
            return accounts.create(request.id, request.name, request.initial_cash).snapshot()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/paper/accounts/{account_id}")
    def get_account(account_id: str) -> dict:
        return _broker(accounts, account_id).snapshot()

    @app.post("/paper/accounts/{account_id}/reset")
    def reset_account(account_id: str, request: AccountResetRequest) -> dict:
        if not request.confirm:
            raise HTTPException(status_code=409, detail="reset requires confirm=true")
        try:
            broker = accounts.reset(account_id, request.initial_cash)
            if account_id == "default":
                app.state.broker = broker
            return broker.snapshot()
        except KeyError as error:
            raise HTTPException(status_code=404, detail="paper account not found") from error

    @app.get("/portfolio")
    def portfolio(account_id: str = "default") -> dict:
        return _broker(accounts, account_id).snapshot()

    @app.get("/orders")
    def list_orders(account_id: str = "default") -> list:
        return [order.to_dict() for order in _broker(accounts, account_id).orders.values()]

    @app.get("/fills")
    def list_fills(account_id: str = "default") -> list:
        return [fill.to_dict() for fill in _broker(accounts, account_id).fills]

    @app.get("/orders/{order_id}")
    def get_order(order_id: str, account_id: str = "default") -> dict:
        try:
            return _broker(accounts, account_id).orders[order_id].to_dict()
        except KeyError as error:
            raise HTTPException(status_code=404, detail="order not found") from error

    @app.get("/orders/{order_id}/events")
    def get_order_events(order_id: str, account_id: str = "default") -> list:
        broker = _broker(accounts, account_id)
        if order_id not in broker.orders:
            raise HTTPException(status_code=404, detail="order not found")
        return repository.load_order_events(account_id, order_id)

    @app.post("/orders", status_code=201)
    def place_order(request: OrderRequest) -> dict:
        broker = _broker(accounts, request.account_id)
        try:
            order = Order(**request.model_dump())
            return broker.submit(order).to_dict()
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.patch("/orders/{order_id}")
    def modify_order(
        order_id: str, request: ModifyOrderRequest, account_id: str = "default"
    ) -> dict:
        try:
            return _broker(accounts, account_id).modify(
                order_id,
                quantity=request.quantity,
                limit_price=request.limit_price,
                trigger_price=request.trigger_price,
                validity=request.validity,
            ).to_dict()
        except KeyError as error:
            raise HTTPException(status_code=404, detail="order not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/orders/{order_id}")
    def cancel_order(order_id: str, account_id: str = "default") -> dict:
        try:
            return _broker(accounts, account_id).cancel(order_id).to_dict()
        except KeyError as error:
            raise HTTPException(status_code=404, detail="order not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/paper/ticks")
    def ingest_tick(request: QuoteRequest, account_id: str = "default") -> dict:
        quote = Quote(
            instrument_key=request.instrument_key,
            last_price=request.last_price,
            bid=request.bid,
            ask=request.ask,
            bids=tuple(DepthLevel(item.price, item.quantity) for item in request.bids),
            asks=tuple(DepthLevel(item.price, item.quantity) for item in request.asks),
            timestamp=request.timestamp or datetime.now().astimezone(),
        )
        fills = coordinator.on_quote(quote)
        account_fills = [
            fill for fill in fills if _broker(accounts, account_id).orders.get(fill.order_id)
        ]
        return {
            "fills": [fill.to_dict() for fill in account_fills],
            "portfolio": _broker(accounts, account_id).snapshot(),
        }

    @app.put("/risk/kill-switch")
    def kill_switch(active: bool, account_id: str = "default") -> dict:
        broker = _broker(accounts, account_id)
        broker.set_kill_switch(active)
        return {"active": broker.kill_switch, "account_id": account_id}

    @app.get("/instruments/search")
    def search_instruments(
        q: str = Query(min_length=1, max_length=50),
        exchange: Literal["NSE", "BSE", "MCX", "ALL"] = "NSE",
        segment: str = "EQ",
        limit: int = Query(default=20, ge=1, le=30),
    ) -> list:
        try:
            return search_client.search(q, exchange, segment, limit)
        except InstrumentSearchError as error:
            status = 503 if "not configured" in str(error) else 502
            raise HTTPException(status_code=status, detail=str(error)) from error

    @app.post("/market/survey")
    def survey_market(request: SurveyRequest) -> dict:
        try:
            survey = MarketSurvey(market_data())
            return survey.run(
                [SurveyInstrument(item.symbol, item.instrument_key) for item in request.instruments]
            )
        except (MarketDataError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/universes/nifty50")
    def nifty50_universe() -> dict:
        try:
            constituents = nifty50.constituents()
            return {
                "name": "NIFTY 50",
                "source": Nifty50Universe.url,
                "count": len(constituents),
                "constituents": constituents,
            }
        except UniverseError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/universes/nifty100")
    def nifty100_universe() -> dict:
        try:
            constituents = nifty100.constituents()
            return {
                "name": Nifty100Universe.name,
                "source": Nifty100Universe.url,
                "count": len(constituents),
                "constituents": constituents,
            }
        except UniverseError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/market/status")
    def exchange_status(exchange: str = "NSE") -> dict:
        try:
            status = market_data().market_status(exchange)
            if exchange == "NSE":
                coordinator.update_market_status({"NSE_EQ": status["status"]})
            return status
        except MarketDataError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.post("/momentum-runners", status_code=202)
    def start_momentum_runner(request: MomentumRunRequest) -> dict:
        account_id = request.account_id
        try:
            accounts.get(account_id)
        except KeyError:
            accounts.create(account_id, "Momentum reversal paper session", request.initial_cash)
        if momentum_runners.has_active(account_id):
            raise HTTPException(
                status_code=409,
                detail="a momentum run is already active for this paper account",
            )
        try:
            constituents = nifty100.constituents()
            runner = MomentumReversalRunner(
                config=MomentumRunnerConfig(
                    account_id=account_id,
                    duration_seconds=request.duration_seconds,
                    poll_interval_seconds=request.poll_interval_seconds,
                    rescan_interval_seconds=request.rescan_interval_seconds,
                    max_positions=request.max_positions,
                    allocation_per_position=request.allocation_per_position,
                    candidate_limit=request.candidate_limit,
                    minimum_score=request.minimum_score,
                    minimum_relative_volume=request.minimum_relative_volume,
                    entry_momentum_pct=request.entry_momentum_pct,
                    reversal_pct=request.reversal_pct,
                    hard_stop_pct=request.hard_stop_pct,
                    universe_name=Nifty100Universe.name,
                    universe_size=Nifty100Universe.expected_count,
                ),
                instruments=[
                    SurveyInstrument(item["symbol"], item["instrument_key"])
                    for item in constituents
                ],
                market_data=market_data(),
                accounts=accounts,
                coordinator=coordinator,
                store=research_store,
            )
            return momentum_runners.add(runner)
        except (UniverseError, MarketDataError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/momentum-runners")
    def list_momentum_runners() -> list:
        return momentum_runners.list()

    @app.get("/momentum-runners/{runner_id}")
    def get_momentum_runner(runner_id: str) -> dict:
        try:
            return momentum_runners.get(runner_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="momentum runner not found") from error

    @app.post("/momentum-runners/{runner_id}/stop")
    def stop_momentum_runner(runner_id: str) -> dict:
        try:
            return momentum_runners.stop(runner_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="momentum runner not found") from error

    @app.get("/strategies")
    def list_strategies() -> list:
        return strategies.list()

    @app.post("/strategies", status_code=201)
    def create_strategy(request: StrategyCreateRequest) -> dict:
        try:
            definition = StrategyDefinition(
                name=request.name,
                account_id=request.account_id,
                legs=[ProfitTargetLeg(**leg.model_dump()) for leg in request.legs],
            )
            return strategies.create(definition)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="paper account not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/strategies/{strategy_id}")
    def get_strategy(strategy_id: str) -> dict:
        try:
            return strategies.get(strategy_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy not found") from error

    @app.get("/strategies/{strategy_id}/events")
    def strategy_events(strategy_id: str) -> list:
        try:
            return strategies.events(strategy_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy not found") from error

    @app.post("/strategies/{strategy_id}/{action}")
    def change_strategy_status(
        strategy_id: str, action: Literal["start", "pause", "stop"]
    ) -> dict:
        status = {
            "start": StrategyStatus.RUNNING,
            "pause": StrategyStatus.PAUSED,
            "stop": StrategyStatus.STOPPED,
        }[action]
        try:
            return strategies.set_status(strategy_id, status)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/backtests", status_code=201)
    def run_backtest(request: BacktestRequest) -> dict:
        if request.from_date > request.to_date:
            raise HTTPException(status_code=422, detail="from_date cannot be after to_date")
        try:
            candles = market_data().historical_candles(
                request.instrument_key,
                request.unit,
                request.interval,
                request.to_date,
                request.from_date,
            )
            return backtests.run(
                request.instrument_key,
                request.symbol,
                candles,
                request.quantity,
                request.initial_cash,
                request.entry_price,
                request.profit_target_pct,
                request.stop_loss_pct,
                request.absolute_profit_target,
            )
        except (MarketDataError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/reports/backtests")
    def backtest_reports() -> list:
        return research_store.backtests()

    @app.get("/reports/backtests/{report_id}")
    def backtest_report(report_id: str) -> dict:
        report = research_store.backtest(report_id)
        if not report:
            raise HTTPException(status_code=404, detail="backtest report not found")
        return report

    @app.get("/dashboard/summary")
    def dashboard_summary(account_id: str = "default") -> dict:
        broker = _broker(accounts, account_id)
        return {
            "accounts": accounts.list(),
            "portfolio": broker.snapshot(),
            "orders": [order.to_dict() for order in broker.orders.values()],
            "fills": [fill.to_dict() for fill in broker.fills],
            "strategies": strategies.list(),
            "backtests": research_store.backtests(),
            "momentum_runners": momentum_runners.list(),
            "stream": market_stream.status(),
        }

    @app.get("/market/ltp")
    def market_ltp(instrument_key: List[str] = Query()) -> dict:
        client = market_data()
        try:
            return {key: value.__dict__ for key, value in client.ltp(instrument_key).items()}
        except MarketDataError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/market/stream/status")
    def market_stream_status() -> dict:
        return market_stream.status()

    @app.post("/market/stream/start", status_code=202)
    def start_market_stream(request: StreamRequest) -> dict:
        try:
            return market_stream.start(request.instrument_keys, request.mode)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/market/stream")
    def stop_market_data_stream() -> dict:
        return market_stream.stop()

    @app.get("/market/candles")
    def market_candles(
        instrument_key: str,
        unit: str,
        interval: int,
        to_date: date,
        from_date: Optional[date] = None,
    ) -> list:
        client = market_data()
        try:
            candles = client.historical_candles(
                instrument_key, unit, interval, to_date, from_date
            )
            return [candle.to_dict() for candle in candles]
        except (MarketDataError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return app


def _broker(accounts: PaperAccountManager, account_id: str):
    try:
        return accounts.get(account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="paper account not found") from error


def _upstox_client(settings: Settings) -> UpstoxMarketData:
    try:
        return UpstoxMarketData(settings.upstox_access_token)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("jupiter_trading.api:app", host="127.0.0.1", port=8000, reload=True)
