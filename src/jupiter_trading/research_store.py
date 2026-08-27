from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import List, Optional


class ResearchStore:
    """SQLite persistence for strategy definitions, events, and backtest reports."""

    def __init__(self, path: str) -> None:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._lock = RLock()
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS strategy_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS backtest_reports (
                    id TEXT PRIMARY KEY,
                    strategy_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS momentum_runs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_events
                    ON strategy_events(strategy_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_momentum_runs_account_updated
                    ON momentum_runs(account_id, updated_at DESC);
                """
            )

    def save_strategy(self, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO strategies (id, status, payload) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (payload["id"], payload["status"], json.dumps(payload)),
            )

    def strategy(self, strategy_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def strategies(self) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategies ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_strategy_event(self, strategy_id: str, event_type: str, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO strategy_events (strategy_id, event_type, payload)
                VALUES (?, ?, ?)""",
                (strategy_id, event_type, json.dumps({"type": event_type, **payload})),
            )

    def strategy_events(self, strategy_id: str) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT payload FROM strategy_events
                WHERE strategy_id = ? ORDER BY sequence""",
                (strategy_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_backtest(self, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO backtest_reports (id, strategy_type, payload)
                VALUES (?, ?, ?)""",
                (payload["id"], payload["strategy_type"], json.dumps(payload)),
            )

    def backtests(self) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM backtest_reports ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def backtest(self, report_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM backtest_reports WHERE id = ?", (report_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_momentum_run(self, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO momentum_runs (id, account_id, status, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    account_id = excluded.account_id,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    payload["id"],
                    payload["config"]["account_id"],
                    payload["status"],
                    json.dumps(payload),
                ),
            )

    def momentum_runs(self, account_id: Optional[str] = None) -> List[dict]:
        with self._lock:
            if account_id:
                rows = self._connection.execute(
                    """SELECT payload FROM momentum_runs
                    WHERE account_id = ? ORDER BY updated_at DESC, rowid DESC""",
                    (account_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM momentum_runs ORDER BY updated_at DESC, rowid DESC"
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def momentum_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM momentum_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None
