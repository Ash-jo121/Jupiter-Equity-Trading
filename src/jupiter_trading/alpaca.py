from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from threading import RLock
from time import monotonic, sleep
from typing import Callable, Dict, Iterable, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .domain import (
    ChargeBreakdown,
    Fill,
    Order,
    OrderStatus,
    PaperAccount,
    Position,
    Product,
    Quote,
    Side,
    utc_now,
)
from .market_data import Candle, MarketDataError
from .paper_broker import FeeSchedule, RiskLimits

US_EASTERN = ZoneInfo("America/New_York")
US_SEGMENT = "US_EQ"


class AlpacaError(RuntimeError):
    pass


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AlpacaRestClient:
    """Small, paper-only Alpaca Trading and Market Data REST client.

    The trading URL is deliberately not configurable. This adapter can never
    send an order to Alpaca's live-trading host by an environment mistake.
    """

    trading_base_url = "https://paper-api.alpaca.markets"
    data_base_url = "https://data.alpaca.markets"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        data_feed: str = "iex",
        timeout: float = 15.0,
        transport: Optional[Callable[[Request, float], dict]] = None,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("Alpaca paper API credentials are not configured")
        if data_feed not in {"iex", "sip"}:
            raise ValueError("ALPACA_DATA_FEED must be 'iex' or 'sip'")
        self.api_key = api_key
        self.secret_key = secret_key
        self.data_feed = data_feed
        self.timeout = timeout
        self._transport = transport

    def account(self) -> dict:
        return self._request(self.trading_base_url, "/v2/account")

    def clock(self) -> dict:
        return self._request(self.trading_base_url, "/v2/clock")

    def calendar(self, start: date, end: date) -> list[dict]:
        query = urlencode({"start": start.isoformat(), "end": end.isoformat()})
        return self._request(self.trading_base_url, f"/v2/calendar?{query}")

    def positions(self) -> list[dict]:
        return self._request(self.trading_base_url, "/v2/positions")

    def orders(self, status: str = "open") -> list[dict]:
        query = urlencode({"status": status, "direction": "desc", "limit": 500})
        return self._request(self.trading_base_url, f"/v2/orders?{query}")

    def order(self, order_id: str) -> dict:
        return self._request(self.trading_base_url, f"/v2/orders/{order_id}")

    def submit_market_order(
        self, symbol: str, quantity: int, side: Side, client_order_id: str
    ) -> dict:
        return self._request(
            self.trading_base_url,
            "/v2/orders",
            method="POST",
            body={
                "symbol": symbol,
                "qty": str(quantity),
                "side": side.value.lower(),
                "type": "market",
                # The strategy runner expects each quote decision to resolve
                # immediately; IOC prevents a market order surviving after the
                # process has moved on to another signal.
                "time_in_force": "ioc",
                "client_order_id": client_order_id,
            },
        )

    def cancel_order(self, order_id: str) -> None:
        self._request(self.trading_base_url, f"/v2/orders/{order_id}", method="DELETE")

    def snapshots(self, symbols: Iterable[str]) -> dict:
        values = list(dict.fromkeys(symbols))
        if not values:
            return {}
        query = urlencode({"symbols": ",".join(values), "feed": self.data_feed})
        return self._request(self.data_base_url, f"/v2/stocks/snapshots?{query}")

    def stock_bars(
        self,
        symbols: Iterable[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        values = list(dict.fromkeys(symbols))
        combined: dict[str, list[dict]] = {symbol: [] for symbol in values}
        page_token: Optional[str] = None
        while values:
            params = {
                "symbols": ",".join(values),
                "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "limit": 10_000,
                "adjustment": "raw",
                "feed": self.data_feed,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            payload = self._request(
                self.data_base_url, "/v2/stocks/bars?" + urlencode(params)
            )
            for symbol, rows in (payload.get("bars") or {}).items():
                combined.setdefault(symbol, []).extend(rows)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return combined

    def _request(
        self,
        base_url: str,
        path: str,
        method: str = "GET",
        body: Optional[dict] = None,
    ):
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            base_url + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "User-Agent": "jupiter-equity-research/0.4",
            },
        )
        if self._transport:
            return self._transport(request, self.timeout)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("message")
            except Exception:  # noqa: BLE001 - preserve the original HTTP status
                detail = None
            raise AlpacaError(
                f"Alpaca returned HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise AlpacaError(f"Unable to reach Alpaca: {error}") from error


class AlpacaMarketData:
    """Alpaca data adapter with batch bar caching for the NASDAQ-100 scan."""

    def __init__(self, client: AlpacaRestClient) -> None:
        self.client = client
        self._lock = RLock()
        self._bar_cache: dict[tuple[str, int, str], tuple[int, List[Candle]]] = {}

    @staticmethod
    def instrument_key(symbol: str) -> str:
        return f"{US_SEGMENT}|{symbol.upper()}"

    @staticmethod
    def symbol(instrument_key: str) -> str:
        segment, separator, symbol = instrument_key.partition("|")
        if separator != "|" or segment != US_SEGMENT or not symbol:
            raise ValueError(f"unsupported US instrument key: {instrument_key}")
        return symbol.upper()

    def ltp(self, instrument_keys: Iterable[str]) -> Dict[str, Quote]:
        keys = list(dict.fromkeys(instrument_keys))
        symbols = [self.symbol(key) for key in keys]
        payload = self.client.snapshots(symbols)
        result: Dict[str, Quote] = {}
        for key, symbol in zip(keys, symbols):
            item = payload.get(symbol) or {}
            trade = item.get("latestTrade") or item.get("latest_trade") or {}
            quote = item.get("latestQuote") or item.get("latest_quote") or {}
            price = trade.get("p") or trade.get("price")
            if price is None:
                continue
            timestamp = _parse_timestamp(trade.get("t") or trade.get("timestamp")) or utc_now()
            result[key] = Quote(
                instrument_key=key,
                last_price=float(price),
                bid=float(quote["bp"]) if quote.get("bp") else None,
                ask=float(quote["ap"]) if quote.get("ap") else None,
                timestamp=timestamp,
            )
        return result

    def prefetch_intraday(
        self, instrument_keys: Iterable[str], unit: str = "minutes", interval: int = 5
    ) -> None:
        keys = list(dict.fromkeys(instrument_keys))
        if not keys:
            return
        if unit != "minutes" or interval not in {1, 5}:
            raise ValueError("Alpaca intraday prefetch supports 1-minute and 5-minute bars")
        now = datetime.now(timezone.utc)
        eastern_now = now.astimezone(US_EASTERN)
        session_open = datetime.combine(eastern_now.date(), time(9, 30), tzinfo=US_EASTERN)
        start = min(session_open, eastern_now).astimezone(timezone.utc)
        generation = int(now.timestamp() // (interval * 60))
        symbols = [self.symbol(key) for key in keys]
        missing = []
        with self._lock:
            for key, symbol in zip(keys, symbols):
                if (symbol, interval, eastern_now.date().isoformat()) not in self._bar_cache or (
                    self._bar_cache[(symbol, interval, eastern_now.date().isoformat())][0]
                    != generation
                ):
                    missing.append(symbol)
        if not missing:
            return
        rows_by_symbol = self.client.stock_bars(missing, f"{interval}Min", start, now)
        with self._lock:
            for symbol in missing:
                cache_key = (symbol, interval, eastern_now.date().isoformat())
                self._bar_cache[cache_key] = (
                    generation,
                    [self._candle(row) for row in rows_by_symbol.get(symbol, [])],
                )

    def intraday_candles(
        self, instrument_key: str, unit: str = "minutes", interval: int = 5
    ) -> List[Candle]:
        self.prefetch_intraday([instrument_key], unit, interval)
        symbol = self.symbol(instrument_key)
        session_date = datetime.now(US_EASTERN).date().isoformat()
        with self._lock:
            entry = self._bar_cache.get((symbol, interval, session_date))
            return list(entry[1]) if entry else []

    def historical_candles(
        self,
        instrument_key: str,
        unit: str,
        interval: int,
        to_date: date,
        from_date: Optional[date] = None,
    ) -> List[Candle]:
        if unit != "minutes":
            raise ValueError("Alpaca research history currently supports minute bars")
        symbol = self.symbol(instrument_key)
        start_day = from_date or to_date
        start = datetime.combine(start_day, time.min, tzinfo=US_EASTERN)
        end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=US_EASTERN)
        rows = self.client.stock_bars([symbol], f"{interval}Min", start, end)
        return [self._candle(row) for row in rows.get(symbol, [])]

    def market_status(self, _exchange: str = "NASDAQ") -> dict:
        clock = self.client.clock()
        return {
            "status": "NORMAL_OPEN" if clock.get("is_open") else "CLOSED",
            "timestamp": clock.get("timestamp"),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
            "source": "ALPACA_CLOCK",
        }

    @staticmethod
    def _candle(row: dict) -> Candle:
        timestamp = _parse_timestamp(row.get("t") or row.get("timestamp"))
        if timestamp is None:
            raise MarketDataError("Alpaca bar did not include a timestamp")
        return Candle(
            timestamp=timestamp,
            open=float(row.get("o", row.get("open"))),
            high=float(row.get("h", row.get("high"))),
            low=float(row.get("l", row.get("low"))),
            close=float(row.get("c", row.get("close"))),
            volume=int(row.get("v", row.get("volume", 0))),
            open_interest=0,
        )


def _zero_fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        delivery_brokerage_flat=0,
        intraday_brokerage_bps=0,
        intraday_brokerage_cap=0,
        transaction_bps=0,
        sebi_bps=0,
        delivery_stt_bps=0,
        intraday_sell_stt_bps=0,
        delivery_buy_stamp_bps=0,
        intraday_buy_stamp_bps=0,
        gst_percent=0,
        delivery_sell_dp_flat=0,
    )


class AlpacaPaperBroker:
    """Runner-facing broker backed by Alpaca's real paper-order endpoint."""

    def __init__(
        self,
        client: AlpacaRestClient,
        account_id: str = "alpaca-us",
        fill_timeout_seconds: float = 6.0,
    ) -> None:
        self.client = client
        remote = client.account()
        if remote.get("trading_blocked") or remote.get("account_blocked"):
            raise AlpacaError("Alpaca paper account is blocked from trading")
        equity = float(remote.get("equity") or remote.get("portfolio_value") or 0)
        cash = float(remote.get("cash") or 0)
        if equity <= 0:
            raise AlpacaError("Alpaca paper account returned no usable equity")
        self.account = PaperAccount(account_id, "Alpaca US paper account", equity)
        self.initial_cash = equity
        self.cash = cash
        self.slippage_bps = 0.0
        self.fee_schedule = _zero_fee_schedule()
        self.risk_limits = RiskLimits(
            allow_short=False,
            max_order_notional=max(equity, 1),
            max_position_notional=max(equity, 1),
            max_daily_loss=max(equity, 1),
        )
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []
        self.positions: Dict[str, Position] = {}
        self.quotes: Dict[str, Quote] = {}
        self.market_statuses: Dict[str, str] = {}
        self.kill_switch = False
        self.fill_timeout_seconds = fill_timeout_seconds
        self._lock = RLock()

    def assert_flat(self) -> None:
        positions = self.client.positions()
        orders = self.client.orders("open")
        if positions or orders:
            raise AlpacaError(
                "Alpaca paper account must be flat with no open orders before the automated run"
            )

    def prepare_session(self) -> None:
        """Reconcile overnight cash, then start with a clean local run ledger."""

        self.assert_flat()
        remote = self.client.account()
        equity = float(remote.get("equity") or remote.get("portfolio_value") or 0)
        cash = float(remote.get("cash") or 0)
        if equity <= 0:
            raise AlpacaError("Alpaca paper account returned no usable equity")
        with self._lock:
            self.account.initial_cash = equity
            self.initial_cash = equity
            self.cash = cash
            self.orders.clear()
            self.fills.clear()
            self.positions.clear()
            self.quotes.clear()

    def submit(self, order: Order) -> Order:
        with self._lock:
            if order.account_id != self.account.id:
                raise ValueError("order account does not match Alpaca paper account")
            if self.kill_switch:
                return self._reject(order, "kill switch is active")
            if self.market_statuses.get(US_SEGMENT) != "NORMAL_OPEN":
                return self._reject(order, "US market status does not allow execution")
            symbol = AlpacaMarketData.symbol(order.instrument_key)
            self.orders[order.id] = order
            try:
                remote = self.client.submit_market_order(
                    symbol, order.quantity, order.side, f"jup-{order.id}"
                )
                remote_id = remote["id"]
                order.provider_order_id = remote_id
                deadline = monotonic() + self.fill_timeout_seconds
                while remote.get("status") not in {
                    "filled",
                    "canceled",
                    "expired",
                    "rejected",
                } and monotonic() < deadline:
                    sleep(0.2)
                    remote = self.client.order(remote_id)
                if remote.get("status") not in {
                    "filled",
                    "canceled",
                    "expired",
                    "rejected",
                }:
                    self.client.cancel_order(remote_id)
                    remote = self.client.order(remote_id)
                self._apply_remote_order(order, remote)
                if order.filled_quantity:
                    account = self.client.account()
                    self.cash = float(account.get("cash") or self.cash)
            except (AlpacaError, KeyError, ValueError) as error:
                self._reject(order, str(error))
            return order

    def on_quote(self, quote: Quote) -> List[Fill]:
        with self._lock:
            self.quotes[quote.instrument_key] = quote
        return []

    def update_market_status(self, statuses: Mapping[str, str]) -> None:
        with self._lock:
            self.market_statuses.update(statuses)

    def set_kill_switch(self, active: bool) -> None:
        self.kill_switch = active

    def snapshot(self) -> dict:
        with self._lock:
            positions = []
            market_value = 0.0
            unrealized = 0.0
            realized = 0.0
            for position in self.positions.values():
                quote = self.quotes.get(position.instrument_key)
                last_price = quote.last_price if quote else position.average_price
                value = position.market_value(last_price)
                open_pnl = position.unrealized_pnl(last_price)
                market_value += value
                unrealized += open_pnl
                realized += position.realized_pnl
                positions.append(
                    {
                        "instrument_key": position.instrument_key,
                        "quantity": position.quantity,
                        "average_price": round(position.average_price, 4),
                        "last_price": last_price,
                        "market_value": round(value, 2),
                        "realized_pnl": round(position.realized_pnl, 2),
                        "unrealized_pnl": round(open_pnl, 2),
                    }
                )
            return {
                "account": self.account.to_dict(),
                "initial_cash": self.initial_cash,
                "cash": round(self.cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(self.cash + market_value, 2),
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "fees_paid": 0.0,
                "charges_paid": ChargeBreakdown().to_dict(),
                "kill_switch": self.kill_switch,
                "market_statuses": dict(self.market_statuses),
                "positions": positions,
                "provider": "ALPACA_PAPER",
                "currency": "USD",
            }

    def _apply_remote_order(self, order: Order, remote: dict) -> None:
        status = str(remote.get("status") or "rejected")
        quantity = int(float(remote.get("filled_qty") or 0))
        average = float(remote.get("filled_avg_price") or 0)
        status_map = {
            "filled": OrderStatus.FILLED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "canceled": OrderStatus.CANCELLED,
            "expired": OrderStatus.EXPIRED,
            "rejected": OrderStatus.REJECTED,
        }
        order.status = status_map.get(status, OrderStatus.OPEN)
        order.updated_at = _parse_timestamp(remote.get("updated_at")) or utc_now()
        order.filled_quantity = min(quantity, order.quantity)
        if order.filled_quantity and average > 0:
            order.average_filled_price = average
            order.filled_price = average
            order.filled_at = _parse_timestamp(remote.get("filled_at")) or order.updated_at
            fill = Fill(
                order_id=order.id,
                instrument_key=order.instrument_key,
                side=order.side,
                quantity=order.filled_quantity,
                price=average,
                fees=0,
                product=Product.INTRADAY,
                timestamp=order.filled_at,
            )
            self.fills.append(fill)
            self.positions.setdefault(order.instrument_key, Position(order.instrument_key)).apply(
                fill
            )
            self.cash += fill.gross_value if fill.side == Side.SELL else -fill.gross_value
        if order.status == OrderStatus.REJECTED:
            order.rejection_reason = remote.get("rejected_reason") or "Alpaca rejected the order"

    @staticmethod
    def _reject(order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason[:500]
        order.updated_at = utc_now()
        return order


class AlpacaAccountManager:
    """One account manager for Alpaca's single paper portfolio."""

    def __init__(self, broker: AlpacaPaperBroker) -> None:
        self.broker = broker

    def get(self, account_id: str = "alpaca-us") -> AlpacaPaperBroker:
        if account_id != self.broker.account.id:
            raise KeyError("Alpaca paper account not found")
        return self.broker

    def update_market_status(self, statuses: Mapping[str, str]) -> None:
        self.broker.update_market_status(statuses)


class AlpacaCoordinator:
    def __init__(self, accounts: AlpacaAccountManager) -> None:
        self.accounts = accounts

    def update_market_status(self, statuses: Mapping[str, str]) -> None:
        self.accounts.update_market_status(statuses)
