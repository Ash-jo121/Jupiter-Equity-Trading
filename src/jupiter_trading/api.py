from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import Settings
from .domain import Order, OrderType, Quote, Side
from .market_data import MarketDataError, UpstoxMarketData
from .paper_broker import PaperBroker
from .repository import SQLiteRepository


class OrderRequest(BaseModel):
    instrument_key: str = Field(examples=["NSE_EQ|INE848E01016"])
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    strategy_id: Optional[str] = None


class QuoteRequest(BaseModel):
    instrument_key: str
    last_price: float = Field(gt=0)
    bid: Optional[float] = Field(default=None, gt=0)
    ask: Optional[float] = Field(default=None, gt=0)
    timestamp: Optional[datetime] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    repository = SQLiteRepository(settings.database_path)
    broker = PaperBroker(
        initial_cash=settings.initial_cash,
        slippage_bps=settings.slippage_bps,
        fee_schedule=settings.fee_schedule,
        risk_limits=settings.risk_limits,
        repository=repository,
    )
    app = FastAPI(
        title="Jupiter Paper Trading",
        version="0.1.0",
        description="Private, paper-only Indian equity strategy research API.",
    )
    app.state.settings = settings
    app.state.broker = broker

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "paper",
            "upstox_configured": bool(settings.upstox_access_token),
        }

    @app.get("/portfolio")
    def portfolio() -> dict:
        return broker.snapshot()

    @app.get("/orders")
    def list_orders() -> list:
        return [order.to_dict() for order in broker.orders.values()]

    @app.get("/fills")
    def list_fills() -> list:
        return [fill.to_dict() for fill in broker.fills]

    @app.post("/orders", status_code=201)
    def place_order(request: OrderRequest) -> dict:
        try:
            order = Order(**request.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error))
        return broker.submit(order).to_dict()

    @app.delete("/orders/{order_id}")
    def cancel_order(order_id: str) -> dict:
        try:
            return broker.cancel(order_id).to_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail="order not found")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.post("/paper/ticks")
    def ingest_tick(request: QuoteRequest) -> dict:
        quote = Quote(
            instrument_key=request.instrument_key,
            last_price=request.last_price,
            bid=request.bid,
            ask=request.ask,
            timestamp=request.timestamp or datetime.now().astimezone(),
        )
        fills = broker.on_quote(quote)
        return {"fills": [fill.to_dict() for fill in fills], "portfolio": broker.snapshot()}

    @app.put("/risk/kill-switch")
    def kill_switch(active: bool) -> dict:
        broker.set_kill_switch(active)
        return {"active": broker.kill_switch}

    @app.get("/market/ltp")
    def market_ltp(instrument_key: List[str] = Query()) -> dict:
        client = _upstox_client(settings)
        try:
            return {key: value.__dict__ for key, value in client.ltp(instrument_key).items()}
        except MarketDataError as error:
            raise HTTPException(status_code=502, detail=str(error))

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
            raise HTTPException(status_code=502, detail=str(error))

    return app


def _upstox_client(settings: Settings) -> UpstoxMarketData:
    try:
        return UpstoxMarketData(settings.upstox_access_token)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error))


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("jupiter_trading.api:app", host="127.0.0.1", port=8000, reload=True)

