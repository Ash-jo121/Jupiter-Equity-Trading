from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional, Protocol

from .domain import (
    ChargeBreakdown,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    PaperAccount,
    Product,
    Side,
    Validity,
    utc_now,
)


class TradingRepository(Protocol):
    def ensure_account(self, account: PaperAccount) -> PaperAccount: ...

    def list_accounts(self) -> List[PaperAccount]: ...

    def get_account(self, account_id: str) -> Optional[PaperAccount]: ...

    def save_account(self, account: PaperAccount) -> None: ...

    def reset_account(self, account: PaperAccount) -> None: ...

    def save_order(self, order: Order) -> None: ...

    def save_fill(self, account_id: str, fill: Fill) -> None: ...

    def load_orders(self, account_id: str) -> List[Order]: ...

    def load_fills(self, account_id: str) -> List[Fill]: ...

    def load_order_events(self, account_id: str, order_id: str) -> List[dict]: ...


class InMemoryRepository:
    def __init__(self) -> None:
        self.accounts: Dict[str, dict] = {}
        self.orders: Dict[str, dict] = {}
        self.fills: List[dict] = []
        self.order_events: List[dict] = []

    def ensure_account(self, account: PaperAccount) -> PaperAccount:
        existing = self.get_account(account.id)
        if existing:
            return existing
        self.save_account(account)
        return account

    def list_accounts(self) -> List[PaperAccount]:
        return [_account_from_dict(value) for value in self.accounts.values()]

    def get_account(self, account_id: str) -> Optional[PaperAccount]:
        value = self.accounts.get(account_id)
        return _account_from_dict(value) if value else None

    def save_account(self, account: PaperAccount) -> None:
        self.accounts[account.id] = account.to_dict()

    def reset_account(self, account: PaperAccount) -> None:
        self.orders = {
            key: value for key, value in self.orders.items() if value["account_id"] != account.id
        }
        self.fills = [value for value in self.fills if value["account_id"] != account.id]
        self.order_events = [
            value for value in self.order_events if value["account_id"] != account.id
        ]
        self.save_account(account)

    def save_order(self, order: Order) -> None:
        value = order.to_dict()
        self.orders[order.id] = value
        self.order_events.append({"account_id": order.account_id, **value})

    def save_fill(self, account_id: str, fill: Fill) -> None:
        value = fill.to_dict()
        value["account_id"] = account_id
        self.fills.append(value)

    def load_orders(self, account_id: str) -> List[Order]:
        return [
            _order_from_dict(value)
            for value in self.orders.values()
            if value["account_id"] == account_id
        ]

    def load_fills(self, account_id: str) -> List[Fill]:
        return [
            _fill_from_dict(value)
            for value in self.fills
            if value["account_id"] == account_id
        ]

    def load_order_events(self, account_id: str, order_id: str) -> List[dict]:
        return [
            value
            for value in self.order_events
            if value["account_id"] == account_id and value["id"] == order_id
        ]


class SQLiteRepository:
    def __init__(self, path: str) -> None:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._lock = RLock()
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    order_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._add_column_if_missing("orders", "account_id", "TEXT NOT NULL DEFAULT 'default'")
            self._add_column_if_missing("fills", "account_id", "TEXT NOT NULL DEFAULT 'default'")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fills_account ON fills(account_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(account_id, order_id)"
            )

    def _add_column_if_missing(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row[1] for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def ensure_account(self, account: PaperAccount) -> PaperAccount:
        existing = self.get_account(account.id)
        if existing:
            return existing
        self.save_account(account)
        return account

    def list_accounts(self) -> List[PaperAccount]:
        with self._lock:
            records = self._connection.execute(
                "SELECT payload FROM accounts ORDER BY rowid"
            ).fetchall()
        return [_account_from_dict(json.loads(record[0])) for record in records]

    def get_account(self, account_id: str) -> Optional[PaperAccount]:
        with self._lock:
            record = self._connection.execute(
                "SELECT payload FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return _account_from_dict(json.loads(record[0])) if record else None

    def save_account(self, account: PaperAccount) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO accounts (id, name, initial_cash, payload) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    initial_cash = excluded.initial_cash,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (account.id, account.name, account.initial_cash, json.dumps(account.to_dict())),
            )

    def reset_account(self, account: PaperAccount) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM fills WHERE account_id = ?", (account.id,))
            self._connection.execute("DELETE FROM orders WHERE account_id = ?", (account.id,))
            self._connection.execute(
                "DELETE FROM order_events WHERE account_id = ?", (account.id,)
            )
            self.save_account(account)

    def save_order(self, order: Order) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO orders (id, account_id, status, payload) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    account_id = excluded.account_id,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (order.id, order.account_id, order.status.value, json.dumps(order.to_dict())),
            )
            self._connection.execute(
                """INSERT INTO order_events (account_id, order_id, status, payload)
                VALUES (?, ?, ?, ?)""",
                (order.account_id, order.id, order.status.value, json.dumps(order.to_dict())),
            )

    def save_fill(self, account_id: str, fill: Fill) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO fills (id, account_id, order_id, payload)
                VALUES (?, ?, ?, ?)""",
                (fill.id, account_id, fill.order_id, json.dumps(fill.to_dict())),
            )

    def load_orders(self, account_id: str) -> List[Order]:
        with self._lock:
            records = self._connection.execute(
                "SELECT payload FROM orders WHERE account_id = ? ORDER BY rowid", (account_id,)
            ).fetchall()
        return [_order_from_dict(json.loads(record[0])) for record in records]

    def load_fills(self, account_id: str) -> List[Fill]:
        with self._lock:
            records = self._connection.execute(
                "SELECT payload FROM fills WHERE account_id = ? ORDER BY rowid", (account_id,)
            ).fetchall()
        return [_fill_from_dict(json.loads(record[0])) for record in records]

    def load_order_events(self, account_id: str, order_id: str) -> List[dict]:
        with self._lock:
            records = self._connection.execute(
                """SELECT payload FROM order_events
                WHERE account_id = ? AND order_id = ? ORDER BY sequence""",
                (account_id, order_id),
            ).fetchall()
        return [json.loads(record[0]) for record in records]

    def rows(self, table: str) -> List[dict]:
        if table not in {"accounts", "orders", "fills", "order_events"}:
            raise ValueError("unsupported table")
        with self._lock:
            records = self._connection.execute(
                f"SELECT payload FROM {table} ORDER BY rowid"
            ).fetchall()
        return [json.loads(record[0]) for record in records]


def _parse_datetime(value) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _account_from_dict(value: dict) -> PaperAccount:
    return PaperAccount(
        id=value["id"],
        name=value["name"],
        initial_cash=float(value["initial_cash"]),
        created_at=_parse_datetime(value.get("created_at")) or utc_now(),
        updated_at=_parse_datetime(value.get("updated_at")) or utc_now(),
        reset_at=_parse_datetime(value.get("reset_at")),
    )


def _order_from_dict(value: dict) -> Order:
    status = OrderStatus(value.get("status", "OPEN"))
    return Order(
        instrument_key=value["instrument_key"],
        side=Side(value["side"]),
        quantity=int(value["quantity"]),
        order_type=OrderType(value["order_type"]),
        limit_price=value.get("limit_price"),
        trigger_price=value.get("trigger_price"),
        strategy_id=value.get("strategy_id"),
        product=Product(value.get("product", "CNC")),
        validity=Validity(value.get("validity", "DAY")),
        account_id=value.get("account_id", "default"),
        id=value["id"],
        status=status,
        created_at=_parse_datetime(value.get("created_at")) or utc_now(),
        updated_at=_parse_datetime(value.get("updated_at")) or utc_now(),
        filled_at=_parse_datetime(value.get("filled_at")),
        filled_price=value.get("filled_price"),
        filled_quantity=int(value.get("filled_quantity", value.get("quantity") if status == OrderStatus.FILLED else 0)),
        average_filled_price=value.get("average_filled_price", value.get("filled_price")),
        triggered_at=_parse_datetime(value.get("triggered_at")),
        cancelled_at=_parse_datetime(value.get("cancelled_at")),
        expired_at=_parse_datetime(value.get("expired_at")),
        rejection_reason=value.get("rejection_reason"),
    )


def _fill_from_dict(value: dict) -> Fill:
    charge_values = value.get("charges") or {}
    charge_values.pop("total", None)
    return Fill(
        order_id=value["order_id"],
        instrument_key=value["instrument_key"],
        side=Side(value["side"]),
        quantity=int(value["quantity"]),
        price=float(value["price"]),
        fees=float(value.get("fees", 0)),
        product=Product(value.get("product", "CNC")),
        charges=ChargeBreakdown(**charge_values),
        timestamp=_parse_datetime(value.get("timestamp")) or utc_now(),
        id=value["id"],
    )
