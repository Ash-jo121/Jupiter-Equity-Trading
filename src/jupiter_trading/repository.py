from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import List, Protocol

from .domain import Fill, Order


class TradingRepository(Protocol):
    def save_order(self, order: Order) -> None: ...

    def save_fill(self, fill: Fill) -> None: ...


class InMemoryRepository:
    def __init__(self) -> None:
        self.orders = {}
        self.fills = []

    def save_order(self, order: Order) -> None:
        self.orders[order.id] = order.to_dict()

    def save_fill(self, fill: Fill) -> None:
        self.fills.append(fill.to_dict())


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
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def save_order(self, order: Order) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO orders (id, status, payload) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (order.id, order.status.value, json.dumps(order.to_dict())),
            )

    def save_fill(self, fill: Fill) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO fills (id, order_id, payload) VALUES (?, ?, ?)",
                (fill.id, fill.order_id, json.dumps(fill.to_dict())),
            )

    def rows(self, table: str) -> List[dict]:
        if table not in {"orders", "fills"}:
            raise ValueError("unsupported table")
        with self._lock:
            records = self._connection.execute(
                f"SELECT payload FROM {table} ORDER BY rowid"
            ).fetchall()
        return [json.loads(record[0]) for record in records]
