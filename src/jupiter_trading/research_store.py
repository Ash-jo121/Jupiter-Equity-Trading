from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import List, Optional

IST = timezone(timedelta(hours=5, minutes=30))


def _session_date(timestamp: str) -> str:
    """The NSE trading day a UTC timestamp belongs to.

    Quotes are stored in UTC but a session is an Indian calendar day, so the
    date is taken in IST. Market hours (09:15-15:30 IST) never straddle a UTC
    midnight, but converting explicitly keeps the grouping correct regardless.
    """

    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST).date().isoformat()


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
                CREATE TABLE IF NOT EXISTS market_observations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    price REAL NOT NULL,
                    decision TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_events
                    ON strategy_events(strategy_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_momentum_runs_account_updated
                    ON momentum_runs(account_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_run
                    ON market_observations(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_observations_instrument_time
                    ON market_observations(instrument_key, timestamp);
                CREATE INDEX IF NOT EXISTS idx_observations_session
                    ON market_observations(session_date, symbol, timestamp);
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

    def add_observations(self, run_id: str, observations: List[dict]) -> int:
        """Append monitoring rows as queryable records rather than one growing blob.

        A run's trace is the raw material for every later backtest, so it is
        stored row per observation and indexed by instrument and session date.
        Rows are immutable once written, which is what makes the append cheap.
        """

        if not observations:
            return 0
        rows = [
            (
                run_id,
                _session_date(observation["timestamp"]),
                observation["instrument_key"],
                observation["symbol"],
                observation["timestamp"],
                observation["price"],
                observation.get("decision", "UNKNOWN"),
                json.dumps(observation),
            )
            for observation in observations
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                """INSERT INTO market_observations
                (run_id, session_date, instrument_key, symbol, timestamp, price,
                 decision, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def observations(
        self,
        run_id: Optional[str] = None,
        session_date: Optional[str] = None,
        symbol: Optional[str] = None,
        instrument_key: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[dict]:
        clauses, values = [], []
        for column, value in (
            ("run_id", run_id),
            ("session_date", session_date),
            ("symbol", symbol),
            ("instrument_key", instrument_key),
        ):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        query = "SELECT payload FROM market_observations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        if limit:
            query += " LIMIT ?"
            values.append(limit)
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return [json.loads(row[0]) for row in rows]

    def observation_count(self, run_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM market_observations WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row[0] if row else 0

    def observed_sessions(self) -> List[dict]:
        """Which days have recorded ticks, and how much of each - the backtest menu."""

        with self._lock:
            rows = self._connection.execute(
                """SELECT session_date, COUNT(*) AS observations,
                          COUNT(DISTINCT symbol) AS symbols,
                          COUNT(DISTINCT run_id) AS runs,
                          MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
                   FROM market_observations
                   GROUP BY session_date ORDER BY session_date DESC"""
            ).fetchall()
        return [
            {
                "session_date": row[0],
                "observations": row[1],
                "symbols": row[2],
                "runs": row[3],
                "first_seen": row[4],
                "last_seen": row[5],
            }
            for row in rows
        ]

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
