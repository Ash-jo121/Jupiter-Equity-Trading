from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import List, Optional
from zoneinfo import ZoneInfo

IST = timezone(timedelta(hours=5, minutes=30))


def _session_date(timestamp: str, timezone_name: str = "Asia/Kolkata") -> str:
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
    try:
        market_timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        market_timezone = IST
    return moment.astimezone(market_timezone).date().isoformat()


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
                CREATE TABLE IF NOT EXISTS entry_experiments (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    shared_config_hash TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS schedule_plans (
                    session_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS market_schedule_plans (
                    market_code TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (market_code, session_date)
                );
                CREATE TABLE IF NOT EXISTS daily_reports (
                    session_date TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS monitoring_sessions (
                    id TEXT PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS monitoring_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    instrument_key TEXT,
                    strategy TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS monitoring_rankings (
                    session_id TEXT NOT NULL,
                    ranking_version INTEGER NOT NULL,
                    instrument_key TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, ranking_version, instrument_key)
                );
                CREATE TABLE IF NOT EXISTS execution_intents (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_events
                    ON strategy_events(strategy_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_momentum_runs_account_updated
                    ON momentum_runs(account_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_entry_experiments_updated
                    ON entry_experiments(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_run
                    ON market_observations(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_observations_instrument_time
                    ON market_observations(instrument_key, timestamp);
                CREATE INDEX IF NOT EXISTS idx_observations_session
                    ON market_observations(session_date, symbol, timestamp);
                CREATE INDEX IF NOT EXISTS idx_monitoring_sessions_date
                    ON monitoring_sessions(session_date, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitoring_events_session
                    ON monitoring_events(session_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_monitoring_events_symbol
                    ON monitoring_events(session_id, instrument_key, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_monitoring_rankings_session
                    ON monitoring_rankings(session_id, ranking_version, rank);
                CREATE INDEX IF NOT EXISTS idx_execution_intents_session
                    ON execution_intents(session_id, updated_at DESC);
                """
            )

    def save_monitoring_session(self, payload: dict) -> None:
        """Persist the compact recoverable state of the V2 monitoring engine."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO monitoring_sessions (id, session_date, status, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_date = excluded.session_date,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    payload["id"],
                    payload["session_date"],
                    payload["status"],
                    json.dumps(payload),
                ),
            )

    def monitoring_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM monitoring_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def monitoring_session_for_date(self, session_date: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                """SELECT payload FROM monitoring_sessions
                   WHERE session_date = ? ORDER BY updated_at DESC LIMIT 1""",
                (session_date,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def monitoring_sessions(self, limit: int = 100) -> List[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._connection.execute(
                """SELECT payload FROM monitoring_sessions
                   ORDER BY session_date DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_monitoring_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
        instrument_key: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> int:
        event = {"type": event_type, **payload}
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT INTO monitoring_events
                   (session_id, event_type, instrument_key, strategy, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, event_type, instrument_key, strategy, json.dumps(event)),
            )
        return int(cursor.lastrowid)

    def monitoring_events(
        self,
        session_id: str,
        *,
        instrument_key: Optional[str] = None,
        strategy: Optional[str] = None,
        after_sequence: Optional[int] = None,
        limit: int = 200,
    ) -> List[dict]:
        limit = max(1, min(int(limit), 1000))
        clauses = ["session_id = ?"]
        values: list = [session_id]
        if instrument_key:
            clauses.append("instrument_key = ?")
            values.append(instrument_key)
        if strategy:
            clauses.append("strategy = ?")
            values.append(strategy)
        if after_sequence is not None:
            clauses.append("sequence > ?")
            values.append(after_sequence)
        values.append(limit)
        ascending = after_sequence is not None
        query = (
            "SELECT sequence, payload, created_at FROM monitoring_events WHERE "
            + " AND ".join(clauses)
            + (" ORDER BY sequence ASC LIMIT ?" if ascending else " ORDER BY sequence DESC LIMIT ?")
        )
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        events = [
            {"sequence": row[0], "created_at": row[2], **json.loads(row[1])}
            for row in rows
        ]
        return events if ascending else list(reversed(events))

    def save_monitoring_rankings(
        self, session_id: str, ranking_version: int, candidates: List[dict]
    ) -> None:
        rows = [
            (
                session_id,
                ranking_version,
                candidate["instrument_key"],
                candidate["rank"],
                json.dumps(candidate),
            )
            for candidate in candidates
        ]
        if not rows:
            return
        with self._lock, self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO monitoring_rankings
                   (session_id, ranking_version, instrument_key, rank, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )

    def monitoring_rankings(
        self, session_id: str, ranking_version: Optional[int] = None, limit: int = 100
    ) -> List[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if ranking_version is None:
                row = self._connection.execute(
                    "SELECT MAX(ranking_version) FROM monitoring_rankings WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                ranking_version = row[0] if row else None
            if ranking_version is None:
                return []
            rows = self._connection.execute(
                """SELECT payload FROM monitoring_rankings
                   WHERE session_id = ? AND ranking_version = ?
                   ORDER BY rank LIMIT ?""",
                (session_id, ranking_version, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_execution_intent(self, payload: dict) -> bool:
        """Insert/update an intent. Returns False when its id is already terminal.

        ACCEPTED is deliberately treated as terminal for retry purposes. If a
        process dies between broker submission and the final status write, a
        restart will not create a duplicate fill.
        """

        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT status FROM execution_intents WHERE id = ?", (payload["id"],)
            ).fetchone()
            if (
                existing
                and payload["status"] in {"CREATED", "QUEUED"}
                and existing[0]
                in {"ACCEPTED", "FILLED", "PARTIAL", "REJECTED", "CANCELLED"}
            ):
                return False
            self._connection.execute(
                """
                INSERT INTO execution_intents
                (id, session_id, strategy, instrument_key, side, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    payload["id"],
                    payload["session_id"],
                    payload["strategy"],
                    payload["instrument_key"],
                    payload["side"],
                    payload["status"],
                    json.dumps(payload),
                ),
            )
        return True

    def execution_intent(self, intent_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM execution_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def execution_intents(
        self, session_id: str, status: Optional[str] = None, limit: int = 200
    ) -> List[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if status:
                rows = self._connection.execute(
                    """SELECT payload FROM execution_intents
                       WHERE session_id = ? AND status = ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (session_id, status, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT payload FROM execution_intents
                       WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

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
                _session_date(
                    observation["timestamp"],
                    observation.get("market_timezone", "Asia/Kolkata"),
                ),
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

    def save_experiment(self, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO entry_experiments (id, status, shared_config_hash, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    shared_config_hash = excluded.shared_config_hash,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    payload["experiment_id"],
                    payload["status"],
                    payload["shared_config_hash"],
                    json.dumps(payload),
                ),
            )

    def experiments(self) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM entry_experiments ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def experiment(self, experiment_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM entry_experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set_credential(self, name: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO credentials (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (name, value),
            )

    def get_credential(self, name: str) -> Optional[tuple]:
        """The stored value and when it was last written, or None."""

        with self._lock:
            row = self._connection.execute(
                "SELECT value, updated_at FROM credentials WHERE name = ?", (name,)
            ).fetchone()
        return (row[0], row[1]) if row else None

    def save_schedule_plan(self, session_date: str, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO schedule_plans (session_date, payload) VALUES (?, ?)
                ON CONFLICT(session_date) DO UPDATE SET
                    payload = excluded.payload, updated_at = CURRENT_TIMESTAMP""",
                (session_date, json.dumps(payload)),
            )

    def save_market_schedule_plan(
        self, market_code: str, session_date: str, payload: dict
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO market_schedule_plans (market_code, session_date, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(market_code, session_date) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP""",
                (market_code, session_date, json.dumps(payload)),
            )

    def market_schedule_plan(self, market_code: str, session_date: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                """SELECT payload FROM market_schedule_plans
                WHERE market_code = ? AND session_date = ?""",
                (market_code, session_date),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def schedule_plan(self, session_date: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM schedule_plans WHERE session_date = ?", (session_date,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def schedule_plans(self, limit: int = 30) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM schedule_plans ORDER BY session_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_daily_report(self, session_date: str, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO daily_reports (session_date, payload) VALUES (?, ?)
                ON CONFLICT(session_date) DO UPDATE SET
                    payload = excluded.payload, updated_at = CURRENT_TIMESTAMP""",
                (session_date, json.dumps(payload)),
            )

    def daily_report(self, session_date: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM daily_reports WHERE session_date = ?", (session_date,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def daily_reports(self, limit: int = 60) -> List[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM daily_reports ORDER BY session_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
