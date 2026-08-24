from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .accounts import PaperAccountManager
from .config import Settings
from .domain import DepthLevel, Order, OrderType, Product, Quote, Side, Validity
from .instrument_search import InstrumentSearchError, UpstoxInstrumentSearch
from .market_data import MarketDataError, UpstoxMarketData
from .market_stream import UpstoxMarketStream
from .repository import SQLiteRepository


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


def create_app(
    settings: Optional[Settings] = None,
    instrument_search: Optional[UpstoxInstrumentSearch] = None,
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
    market_stream = UpstoxMarketStream(
        settings.upstox_access_token,
        accounts.on_quote,
        accounts.update_market_status,
    )
    search_client = instrument_search or UpstoxInstrumentSearch(settings.upstox_access_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.upstox_stream_auto_start and settings.upstox_stream_instruments:
            market_stream.start(settings.upstox_stream_instruments, settings.upstox_stream_mode)
        try:
            yield
        finally:
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

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "paper",
            "upstox_configured": bool(settings.upstox_access_token),
            "paper_accounts": len(accounts.list()),
        }

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
        fills = accounts.on_quote(quote)
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

    @app.get("/market/ltp")
    def market_ltp(instrument_key: List[str] = Query()) -> dict:
        client = _upstox_client(settings)
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
        client = _upstox_client(settings)
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
