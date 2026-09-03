from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .accounts import PaperAccountManager
from .automation import DailyScheduler, SchedulerConfig
from .backtest import BacktestEngine, ExitReplayEngine, ReplayGates
from .config import Settings
from .daily_report import DailyReportBuilder
from .domain import DepthLevel, Order, OrderType, Product, Quote, Side, Validity
from .holiday_calendar import HolidayCalendarError, NseHolidayCalendar
from .instrument_search import InstrumentSearchError, UpstoxInstrumentSearch
from .market_data import MarketDataError, SharedQuoteCache, UpstoxMarketData
from .market_stream import UpstoxMarketStream
from .momentum_runner import (
    MomentumReversalRunner,
    MomentumRunnerConfig,
    MomentumRunnerService,
)
from .repository import SQLiteRepository
from .research_store import ResearchStore
from .schedule import build_daily_plan
from .schedule import now_ist as schedule_now_ist
from .strategy_engine import (
    MarketCoordinator,
    ProfitTargetLeg,
    StrategyDefinition,
    StrategyService,
    StrategyStatus,
)
from .survey import MarketSurvey, SharedSurveyCache, SurveyInstrument
from .trade_rules import EntryPolicy, ExitPolicy, cost_model
from .universe import Nifty50Universe, Nifty100Universe, UniverseError
from .upstox_auth import UpstoxAuthError, UpstoxTokenStore


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
    duration_seconds: int = Field(default=300, ge=30, le=25_200)
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
    entry_mode: Literal["THREE_BAR", "ROLLING_WINDOW"] = "THREE_BAR"
    exit_mode: Literal["RATCHET", "REVERSAL"] = "RATCHET"
    entry_bars: int = Field(default=3, ge=2, le=12)
    require_nifty_confirmation: bool = False
    entry_cost_multiple: float = Field(default=1.0, ge=0)
    entry_noise_multiple: float = Field(default=2.0, ge=0)
    entry_timeframe_seconds: float = Field(default=0.0, ge=0, le=900)
    reentry_cooldown_seconds: float = Field(default=0.0, ge=0, le=7200)
    survive_stop_multiple: float = Field(default=2.0, gt=0)
    lock_multiple: float = Field(default=1.5, gt=0)
    ride_multiple: float = Field(default=3.0, gt=0)
    min_gap_multiple: float = Field(default=1.5, gt=0)
    trail_window: int = Field(default=24, ge=2, le=360)
    fast_trail_window: int = Field(default=6, ge=2, le=360)
    volume_decay_ratio: float = Field(default=1.0, gt=0)
    confirmation_samples: int = Field(default=2, ge=1, le=20)
    time_stop_seconds: float = Field(default=240.0, gt=0)


class MomentumVariantRequest(BaseModel):
    """One arm of a batch: a label plus any fields that differ from the base."""

    label: Optional[str] = Field(default=None, max_length=40)
    account_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    overrides: dict = Field(default_factory=dict)


class MomentumBatchRequest(BaseModel):
    """Several runs launched together, sharing a base config and differing per arm."""

    base: MomentumRunRequest = Field(default_factory=MomentumRunRequest)
    variants: List[MomentumVariantRequest] = Field(min_length=1, max_length=8)
    account_prefix: str = Field(default="momentum", pattern=r"^[a-zA-Z0-9_-]{1,24}$")

    def merged(self, variant: MomentumVariantRequest, index: int) -> MomentumRunRequest:
        """Base config with this arm's overrides applied, on its own account."""

        fields = self.base.model_dump()
        unknown = set(variant.overrides) - set(fields)
        if unknown:
            raise ValueError(f"unknown override fields: {', '.join(sorted(unknown))}")
        fields.update(variant.overrides)
        slug = variant.label or f"v{index + 1}"
        safe = "".join(char if char.isalnum() else "-" for char in slug.lower())[:20]
        fields["account_id"] = variant.account_id or f"{self.account_prefix}-{safe}"
        return MomentumRunRequest(**fields)


class UpstoxTokenRequest(BaseModel):
    """Set the live Upstox access token directly (when you already hold one)."""

    access_token: str = Field(min_length=10, max_length=4096)


class ExitReplayRequest(BaseModel):
    """Re-score a recorded run's monitoring trace under a different rule."""

    run_id: str
    mode: Literal["EXITS_ONLY", "FULL"] = "EXITS_ONLY"
    slippage_bps: float = Field(default=2.0, ge=0)
    entry_bars: Optional[int] = Field(default=None, ge=2, le=12)
    minimum_rise_pct: Optional[float] = Field(default=None, ge=0)
    entry_cost_multiple: float = Field(default=1.0, ge=0)
    entry_noise_multiple: float = Field(default=2.0, ge=0)
    survive_stop_multiple: float = Field(default=2.0, gt=0)
    lock_multiple: float = Field(default=1.5, gt=0)
    ride_multiple: float = Field(default=3.0, gt=0)
    min_gap_multiple: float = Field(default=1.5, gt=0)
    trail_window: int = Field(default=24, ge=2, le=360)
    fast_trail_window: int = Field(default=6, ge=2, le=360)
    volume_decay_ratio: float = Field(default=1.0, gt=0)
    confirmation_samples: int = Field(default=2, ge=1, le=20)
    time_stop_seconds: float = Field(default=240.0, gt=0)
    max_positions: Optional[int] = Field(default=None, ge=1, le=10)
    allocation_per_position: Optional[float] = Field(default=None, gt=0)
    minimum_relative_volume: Optional[float] = Field(default=None, ge=0)
    require_positive_nifty: bool = False


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
    holiday_calendar: Optional[NseHolidayCalendar] = None,
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
    token_store = UpstoxTokenStore(
        research_store,
        api_key=settings.upstox_api_key,
        api_secret=settings.upstox_api_secret,
        redirect_uri=settings.upstox_redirect_uri,
        env_token=settings.upstox_access_token,
        analytics_token=settings.upstox_analytics_token,
    )
    market_stream = UpstoxMarketStream(
        token_store.current_token(),
        coordinator.on_quote,
        coordinator.update_market_status,
    )
    search_client = instrument_search or UpstoxInstrumentSearch(token_store.current_token())
    backtests = BacktestEngine(research_store, settings.fee_schedule)
    exit_replays = ExitReplayEngine(research_store, settings.fee_schedule)
    nifty50 = Nifty50Universe()
    nifty100 = Nifty100Universe()
    momentum_runners = MomentumRunnerService(research_store)
    shared_survey_cache = SharedSurveyCache()
    shared_quote_cache = SharedQuoteCache()
    daily_reports_builder = DailyReportBuilder(research_store)
    nse_holidays = holiday_calendar or NseHolidayCalendar()

    def market_data() -> UpstoxMarketData:
        # Read the live token each call, so a morning re-auth reaches the next
        # run without a restart.
        if market_data_client is not None:
            return market_data_client
        try:
            return UpstoxMarketData(token_store.current_token())
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    def _scheduler_launch(slot, sched_config) -> str:
        # _launch_runner is defined further down; closures resolve at call time.
        request = MomentumRunRequest(
            account_id=slot.account_id,
            initial_cash=sched_config.initial_cash,
            duration_seconds=slot.duration_seconds,
            max_positions=slot.max_positions,
            entry_timeframe_seconds=slot.entry_timeframe_seconds,
            reentry_cooldown_seconds=sched_config.reentry_cooldown_seconds,
            allocation_per_position=sched_config.allocation_per_position,
            entry_mode="THREE_BAR",
            exit_mode="RATCHET",
            require_nifty_confirmation=False,
        )
        return _launch_runner(request, label=slot.label)["id"]

    scheduler = DailyScheduler(
        research_store,
        _scheduler_launch,
        lambda session_date: daily_reports_builder.build(session_date),
        SchedulerConfig(
            enabled=settings.scheduler_enabled,
            max_positions=settings.scheduler_max_positions,
            reentry_cooldown_seconds=settings.scheduler_cooldown_seconds,
            account_prefix=settings.scheduler_account_prefix,
            allocation_per_position=settings.scheduler_allocation,
            initial_cash=settings.scheduler_initial_cash,
        ),
        market_ready=lambda: token_store.status()["likely_valid"],
        trading_day_check=nse_holidays.is_trading_day,
    )

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
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            momentum_runners.stop_all()
            market_stream.stop()

    app = FastAPI(
        title="Jupiter Paper Trading",
        version="0.2.0",
        description="Private, paper-only Indian equity strategy research API.",
        lifespan=lifespan,
    )
    if settings.cors_allow_origins:
        # A split deploy (dashboard on one host, API on another) is cross-origin,
        # so the browser needs the API to name the dashboard's origin explicitly.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.broker = accounts.get()
    app.state.market_stream = market_stream
    app.state.instrument_search = search_client
    app.state.strategies = strategies
    app.state.research_store = research_store
    app.state.backtests = backtests
    app.state.exit_replays = exit_replays
    app.state.momentum_runners = momentum_runners
    app.state.shared_quote_cache = shared_quote_cache
    app.state.scheduler = scheduler
    app.state.token_store = token_store
    app.state.daily_reports = daily_reports_builder
    app.state.nse_holidays = nse_holidays

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "paper",
            "upstox_configured": bool(token_store.current_token()),
            "upstox_token": token_store.status(),
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

    @app.get("/cost-model")
    def position_cost_model(
        notional: float = Query(gt=0), product: Literal["INTRADAY", "DELIVERY"] = "INTRADAY"
    ) -> dict:
        """What a round trip costs at a given size, so sizing can be chosen honestly."""

        return cost_model(
            notional,
            settings.fee_schedule,
            settings.slippage_bps,
            Product.INTRADAY if product == "INTRADAY" else Product.DELIVERY,
        )

    @app.get("/market/status")
    def exchange_status(exchange: str = "NSE") -> dict:
        try:
            status = market_data().market_status(exchange)
            if exchange == "NSE":
                coordinator.update_market_status({"NSE_EQ": status["status"]})
            return status
        except MarketDataError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    def _launch_runner(request: MomentumRunRequest, label: Optional[str] = None) -> dict:
        """Provision the account if needed and start one configured run."""

        account_id = request.account_id
        try:
            accounts.get(account_id)
        except KeyError:
            accounts.create(
                account_id, label or "Momentum reversal paper session", request.initial_cash
            )
        if momentum_runners.has_active(account_id):
            raise ValueError(
                f"a momentum run is already active for paper account '{account_id}'"
            )
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
                entry_mode=request.entry_mode,
                exit_mode=request.exit_mode,
                entry_bars=request.entry_bars,
                require_nifty_confirmation=request.require_nifty_confirmation,
                entry_cost_multiple=request.entry_cost_multiple,
                entry_noise_multiple=request.entry_noise_multiple,
                entry_timeframe_seconds=request.entry_timeframe_seconds,
                reentry_cooldown_seconds=request.reentry_cooldown_seconds,
                survive_stop_multiple=request.survive_stop_multiple,
                lock_multiple=request.lock_multiple,
                ride_multiple=request.ride_multiple,
                min_gap_multiple=request.min_gap_multiple,
                trail_window=request.trail_window,
                fast_trail_window=request.fast_trail_window,
                volume_decay_ratio=request.volume_decay_ratio,
                confirmation_samples=request.confirmation_samples,
                time_stop_seconds=request.time_stop_seconds,
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
            survey_cache=shared_survey_cache,
            quote_cache=shared_quote_cache,
        )
        return momentum_runners.add(runner)

    @app.post("/momentum-runners", status_code=202)
    def start_momentum_runner(request: MomentumRunRequest) -> dict:
        try:
            return _launch_runner(request)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (UniverseError, MarketDataError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/momentum-runners/batch", status_code=202)
    def start_momentum_runner_batch(request: MomentumBatchRequest) -> dict:
        """Start several differently configured runs side by side.

        Each variant gets its own paper account, because concurrent runners
        sharing one account would compete for the same cash and positions and
        neither result would mean anything. Comparing configurations needs the
        accounts kept apart.
        """

        started, failed = [], []
        for index, variant in enumerate(request.variants):
            merged = request.merged(variant, index)
            try:
                started.append(
                    {"label": variant.label or merged.account_id, "run": _launch_runner(merged, variant.label)}
                )
            except (ValueError, UniverseError, MarketDataError) as error:
                failed.append({"label": variant.label or merged.account_id, "error": str(error)})
        if not started and failed:
            raise HTTPException(status_code=409, detail=failed[0]["error"])
        return {"started": started, "failed": failed}

    @app.get("/momentum-runners")
    def list_momentum_runners() -> list:
        return momentum_runners.list()

    @app.get("/momentum-runners/{runner_id}")
    def get_momentum_runner(runner_id: str, include_monitoring: bool = True) -> dict:
        try:
            return momentum_runners.get(runner_id, include_monitoring=include_monitoring)
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

    @app.get("/auth/upstox/status")
    def upstox_auth_status() -> dict:
        return token_store.status()

    @app.get("/auth/upstox/login-url")
    def upstox_login_url() -> dict:
        """The Upstox login link to open each morning to refresh the token."""

        try:
            return {"authorization_url": token_store.authorization_url()}
        except UpstoxAuthError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/auth/upstox/callback", include_in_schema=False)
    def upstox_callback(code: Optional[str] = None, error: Optional[str] = None):
        """Upstox redirects here after login; exchange the code for a token.

        Returns a tiny self-contained page so a morning re-auth ends with a
        human-readable confirmation in the browser, token already live.
        """

        if error:
            return HTMLResponse(_auth_page(False, error), status_code=400)
        if not code:
            return HTMLResponse(_auth_page(False, "no authorization code in redirect"), 400)
        try:
            status = token_store.exchange_code(code)
        except UpstoxAuthError as problem:
            return HTMLResponse(_auth_page(False, str(problem)), status_code=502)
        expiry = status["expires_at_ist"] or "the provider-reported expiry"
        return HTMLResponse(_auth_page(True, f"Token live, valid until {expiry}"))

    @app.put("/auth/upstox/token")
    def set_upstox_token(request: UpstoxTokenRequest) -> dict:
        token_store.set_token(request.access_token)
        return token_store.status()

    @app.get("/schedule/status")
    def schedule_status() -> dict:
        return scheduler.status()

    @app.get("/schedule/calendar")
    def schedule_calendar(year: Optional[int] = Query(default=None, ge=2000, le=2100)) -> dict:
        target_year = year or schedule_now_ist().year
        try:
            holidays = nse_holidays.holidays(target_year)
        except HolidayCalendarError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "year": target_year,
            "segment": NseHolidayCalendar.segment,
            "source": NseHolidayCalendar.url,
            "count": len(holidays),
            "holidays": holidays,
            "fallback_active": bool(nse_holidays.last_error),
            "warning": nse_holidays.last_error,
        }

    @app.get("/schedule/plan")
    def schedule_plan(session_date: Optional[str] = None) -> dict:
        """The stored plan for a day, building today's on first read."""

        target = session_date or schedule_now_ist().date().isoformat()
        plan = scheduler.plan_for(target, create=session_date is None)
        if plan is None:
            plan = build_daily_plan(
                target,
                max_positions=settings.scheduler_max_positions,
                account_prefix=settings.scheduler_account_prefix,
            )
        return plan.to_dict()

    @app.get("/schedule/plans")
    def schedule_plans(limit: int = Query(default=30, ge=1, le=365)) -> list:
        return research_store.schedule_plans(limit)

    @app.post("/schedule/tick")
    def schedule_tick() -> dict:
        """Advance the schedule now. Useful for ops and when a tick was missed."""

        return {"actions": scheduler.tick()}

    @app.get("/reports/daily")
    def daily_reports_list(limit: int = Query(default=60, ge=1, le=365)) -> list:
        return research_store.daily_reports(limit)

    @app.get("/reports/daily/{session_date}")
    def daily_report(session_date: str) -> dict:
        report = research_store.daily_report(session_date)
        if not report:
            raise HTTPException(status_code=404, detail="no report for that date")
        return report

    @app.post("/reports/daily/{session_date}/build", status_code=201)
    def build_daily_report(session_date: str) -> dict:
        """Compile (or recompile) the report for a day from its recorded runs."""

        return daily_reports_builder.build(session_date)

    @app.get("/observations/sessions")
    def observation_sessions() -> list:
        """Which trading days have recorded ticks, and how much of each."""

        return research_store.observed_sessions()

    @app.get("/observations")
    def list_observations(
        run_id: Optional[str] = None,
        session_date: Optional[str] = None,
        symbol: Optional[str] = None,
        instrument_key: Optional[str] = None,
        limit: int = Query(default=5_000, ge=1, le=200_000),
    ) -> dict:
        rows = research_store.observations(
            run_id=run_id,
            session_date=session_date,
            symbol=symbol,
            instrument_key=instrument_key,
            limit=limit,
        )
        return {"count": len(rows), "limit": limit, "observations": rows}

    @app.post("/backtests/exit-replay", status_code=201)
    def replay_exits(request: ExitReplayRequest) -> dict:
        try:
            source = momentum_runners.get(request.run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="momentum run not found") from error
        config = source.get("config") or {}
        try:
            return exit_replays.run(
                source,
                exit_policy=ExitPolicy(
                    survive_stop_multiple=request.survive_stop_multiple,
                    lock_multiple=request.lock_multiple,
                    ride_multiple=request.ride_multiple,
                    min_gap_multiple=request.min_gap_multiple,
                    trail_window=request.trail_window,
                    fast_trail_window=request.fast_trail_window,
                    volume_decay_ratio=request.volume_decay_ratio,
                    confirmation_samples=request.confirmation_samples,
                    time_stop_seconds=request.time_stop_seconds,
                ),
                entry_policy=EntryPolicy(
                    bars=request.entry_bars or config.get("entry_bars", 3),
                    minimum_rise_pct=(
                        request.minimum_rise_pct
                        if request.minimum_rise_pct is not None
                        else config.get("entry_momentum_pct", 0.10)
                    ),
                    cost_floor_multiple=request.entry_cost_multiple,
                    noise_multiple=request.entry_noise_multiple,
                ),
                mode=request.mode,
                gates=ReplayGates(
                    minimum_relative_volume=(
                        request.minimum_relative_volume
                        if request.minimum_relative_volume is not None
                        else config.get("minimum_relative_volume", 1.2)
                    ),
                    require_positive_nifty=request.require_positive_nifty,
                    max_positions=request.max_positions or config.get("max_positions", 2),
                    allocation_per_position=(
                        request.allocation_per_position
                        or config.get("allocation_per_position", 25_000.0)
                    ),
                ),
                slippage_bps=request.slippage_bps,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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


app = create_app()


def _auth_page(ok: bool, message: str) -> str:
    colour = "#176b4c" if ok else "#b64c3f"
    title = "Upstox token refreshed" if ok else "Token refresh failed"
    safe = message.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>Jupiter · Upstox</title>"
        "<body style='font-family:system-ui;max-width:520px;margin:80px auto;padding:0 20px'>"
        f"<h1 style='color:{colour};font-size:20px'>{title}</h1>"
        f"<p style='color:#444;line-height:1.5'>{safe}</p>"
        "<p style='color:#888;font-size:13px'>You can close this tab.</p></body>"
    )


def run() -> None:
    import os

    import uvicorn

    # Railway and other hosts inject $PORT and expect a bind on all interfaces.
    # Locally, with no PORT set, keep the friendly localhost + reload defaults.
    port = int(os.getenv("PORT", "8000"))
    hosted = "PORT" in os.environ
    uvicorn.run(
        "jupiter_trading.api:app",
        host="0.0.0.0" if hosted else "127.0.0.1",
        port=port,
        reload=not hosted,
    )
