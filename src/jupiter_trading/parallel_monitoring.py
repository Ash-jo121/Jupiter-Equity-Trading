from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from math import floor
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from .accounts import PaperAccountManager
from .candidate_queue import CandidateQueue
from .candle_signals import (
    MACD_EARLY,
    MACD_EARLY_PRICE_CONFIRM,
    MACD_FRESH_CONFIRMED,
    PriceConfirmationSetup,
    SetupState,
    SignalConfig,
    evaluate_entry,
    evaluate_shared_exit,
)
from .domain import Order, OrderType, Product, Quote, Side, Validity
from .execution_queue import ExecutionIntent, ExecutionQueue, intent_timestamp
from .feature_engine import FeatureSnapshot, SharedFeatureEngine
from .market_data import SharedCandleCache, SharedQuoteCache, UpstoxMarketData
from .monitoring_manager import STRATEGIES, MonitoringManager
from .research_store import ResearchStore
from .survey import SharedSurveyCache, SurveyInstrument
from .trade_rules import ExitPolicy, RatchetExit, breakeven_pct, cost_model

STRATEGY_MODES = {
    "A": MACD_EARLY,
    "B": MACD_EARLY_PRICE_CONFIRM,
    "C": MACD_FRESH_CONFIRMED,
}


def build_monitoring_strategy_run(
    session: dict,
    strategy: str,
    accounts: PaperAccountManager,
    store: ResearchStore,
) -> dict:
    """Project one V2 strategy portfolio into the existing run-report contract."""

    if strategy not in STRATEGIES:
        raise ValueError("unknown strategy")
    session_id = session["id"]
    config = session.get("config", {})
    stored_portfolio = next(
        (
            item
            for item in session.get("paper_portfolios", [])
            if item.get("strategy") == strategy
        ),
        {},
    )
    account_id = stored_portfolio.get("account_id") or (
        f"{config.get('account_prefix', 'parallel')}-"
        f"{session.get('session_date')}-{strategy.lower()}"
    )
    broker = accounts.get(account_id)
    portfolio = broker.snapshot()
    executions = broker.strategy_executions(f"{session_id}:{strategy}")
    intents = [
        intent
        for intent in store.execution_intents(session_id, limit=1000)
        if intent.get("strategy") == strategy
    ]
    intents_by_order = {
        intent.get("order", {}).get("id"): intent
        for intent in intents
        if intent.get("order", {}).get("id")
    }
    symbols = {
        item.get("instrument_key"): item.get("symbol")
        for item in session.get("candidate_queue", {}).get("candidates", [])
        if item.get("instrument_key") and item.get("symbol")
    }
    symbols.update(
        {
            intent.get("instrument_key"): intent.get("symbol")
            for intent in intents
            if intent.get("instrument_key") and intent.get("symbol")
        }
    )
    symbols.update(
        {
            item.get("instrument_key"): item.get("symbol")
            for item in session.get("open_positions", [])
            if item.get("instrument_key") and item.get("symbol")
        }
    )
    fills = [
        {
            **fill,
            "symbol": symbols.get(fill["instrument_key"], fill["instrument_key"]),
        }
        for fill in executions["fills"]
    ]
    enriched_positions = [
        {
            **position,
            "symbol": symbols.get(
                position["instrument_key"], position["instrument_key"]
            ),
        }
        for position in portfolio["positions"]
    ]
    portfolio = {**portfolio, "positions": enriched_positions}
    events = []
    for fill in fills:
        intent = intents_by_order.get(fill.get("order_id"), {})
        metadata = intent.get("metadata", {})
        event = {
            "type": "ENTRY_FILLED" if fill["side"] == "BUY" else "EXIT_FILLED",
            "timestamp": fill["timestamp"],
            "instrument_key": fill["instrument_key"],
            "symbol": fill["symbol"],
            "reason": intent.get("reason", "PAPER_FILL"),
            "observed_price": intent.get("observed_price", fill["price"]),
            "entry_price": fill["price"],
        }
        if fill["side"] == "BUY":
            event["entry_signal"] = {
                "reason": intent.get("reason", "PAPER_FILL"),
                "observed_price": intent.get("observed_price", fill["price"]),
                "market_alignment": "RECORDED_NOT_GATED",
                "structural_stop": metadata.get("structural_stop"),
                "intent": metadata,
            }
        else:
            event["exit_state"] = metadata.get("exit_state")
        events.append(event)
    events.sort(key=lambda item: item["timestamp"])
    gross_pnl = float(portfolio["realized_pnl"]) + float(
        portfolio["unrealized_pnl"]
    )
    fees = sum(fill["fees"] for fill in fills)
    initial_cash = float(portfolio["initial_cash"])
    exit_policy = ExitPolicy()
    duration_seconds = 375 * 60
    return {
        "id": f"{session_id}:{strategy}",
        "experiment_id": session_id,
        "session_id": session_id,
        "variant_label": strategy,
        "shared_config_hash": "parallel-monitoring-v2",
        "status": session.get("status", "DRAFT"),
        "started_at": session.get("started_at"),
        "finished_at": session.get("finished_at"),
        "stop_reason": None,
        "config": {
            "account_id": account_id,
            "signal_strategy": STRATEGY_MODES[strategy],
            "duration_seconds": duration_seconds,
            "poll_interval_seconds": config.get("poll_interval_seconds", 5),
            "rescan_interval_seconds": config.get("survey_interval_seconds", 285),
            "max_positions": config.get("max_positions_per_strategy", 2),
            "allocation_per_position": config.get("allocation_per_position", 25_000),
            "candidate_limit": config.get("slot_count", 10),
            "minimum_score": config.get("minimum_score", 0.15),
            "minimum_relative_volume": config.get("minimum_relative_volume", 1.2),
            "entry_momentum_pct": 0.1,
            "reversal_pct": 0.1,
            "hard_stop_pct": 0.35,
            "universe_name": "NIFTY 100",
            "universe_size": 100,
            "entry_mode": "THREE_BAR",
            "exit_mode": "RATCHET",
            "entry_bars": 3,
            "require_nifty_confirmation": False,
            "entry_cost_multiple": 1.0,
            "entry_noise_multiple": 2.0,
            "entry_timeframe_seconds": 60,
            "survive_stop_multiple": exit_policy.survive_stop_multiple,
            "lock_multiple": exit_policy.lock_multiple,
            "ride_multiple": exit_policy.ride_multiple,
            "min_gap_multiple": exit_policy.min_gap_multiple,
            "trail_window": exit_policy.trail_window,
            "fast_trail_window": exit_policy.fast_trail_window,
            "volume_decay_ratio": exit_policy.volume_decay_ratio,
            "confirmation_samples": exit_policy.confirmation_samples,
            "time_stop_seconds": exit_policy.time_stop_seconds,
            "market_code": "NSE",
            "market_timezone": config.get("market_timezone", "Asia/Kolkata"),
            "currency": "INR",
            "broker_provider": "INTERNAL_PAPER",
            "benchmark_instrument_key": config.get(
                "benchmark_instrument_key", "NSE_INDEX|Nifty 50"
            ),
            "benchmark_symbol": "NIFTY 50",
            "execution_segment": "NSE_EQ",
            "data_transport": "UPSTOX_WEBSOCKET_V3",
        },
        "cost_model": cost_model(
            float(config.get("allocation_per_position", 25_000)),
            broker.fee_schedule,
            broker.slippage_bps,
            Product.INTRADAY,
        ),
        "decision_counts": [],
        "initial_equity": initial_cash,
        "scan_count": int(session.get("scan_count", 0)),
        "poll_count": int(
            session.get("data_transport", {}).get("stream_quotes_processed", 0)
        ),
        "candidates": list(
            session.get("candidate_queue", {}).get("candidates", [])
        ),
        "open_positions": [
            item
            for item in session.get("open_positions", [])
            if item.get("strategy") == strategy
        ],
        "completed_instruments": sorted(
            {fill["instrument_key"] for fill in fills if fill["side"] == "SELL"}
        ),
        "pending_setups": [],
        "pending_entry_intents": [],
        "pending_signal_exits": [],
        "ratchet_states": {},
        "events": events,
        "monitoring": [],
        "monitoring_count": 0,
        "errors": list(session.get("errors", [])),
        "warnings": [],
        "market_status": session.get("market_status", "UNKNOWN"),
        "fills": fills,
        "metrics": {
            "gross_pnl": round(gross_pnl, 2),
            "fees": round(fees, 2),
            "net_pnl": round(gross_pnl - fees, 2),
        },
        "portfolio": portfolio,
        "session_pnl": round(float(portfolio["equity"]) - initial_cash, 2),
    }


@dataclass(frozen=True)
class ParallelMonitoringConfig:
    session_id: str
    session_date: str
    account_prefix: str = "parallel"
    slot_count: int = 10
    candidate_pool_size: int = 40
    lease_seconds: float = 300.0
    cooldown_seconds: float = 600.0
    poll_interval_seconds: float = 5.0
    stream_stale_seconds: float = 15.0
    feature_refresh_seconds: float = 1.0
    survey_interval_seconds: float = 285.0
    minimum_score: float = 0.15
    minimum_relative_volume: float = 1.2
    initial_cash: float = 1_000_000.0
    allocation_per_position: float = 25_000.0
    max_positions_per_strategy: int = 2
    instrument_tick_size: float = 0.05
    benchmark_instrument_key: str = "NSE_INDEX|Nifty 50"
    market_timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        if self.slot_count != 10:
            raise ValueError("parallel monitoring V2 requires exactly ten slots")
        if self.candidate_pool_size < self.slot_count:
            raise ValueError("candidate pool must be larger than the slot pool")
        if min(
            self.lease_seconds,
            self.poll_interval_seconds,
            self.stream_stale_seconds,
            self.feature_refresh_seconds,
            self.survey_interval_seconds,
            self.minimum_relative_volume,
            self.initial_cash,
            self.allocation_per_position,
            self.max_positions_per_strategy,
        ) <= 0:
            raise ValueError("parallel monitoring settings must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown cannot be negative")


class ParallelMonitoringEngine:
    """Ranked-candidate monitoring with ten reusable, multi-strategy workers.

    A single coordinator owns mutable state. Each logical slot receives one
    shared feature snapshot and fans it into A/B/C, while portfolios, setups,
    positions and fills remain isolated per strategy.
    """

    def __init__(
        self,
        config: ParallelMonitoringConfig,
        instruments: Iterable[SurveyInstrument],
        market_data: UpstoxMarketData,
        accounts: PaperAccountManager,
        store: ResearchStore,
        survey_cache: Optional[SharedSurveyCache] = None,
        quote_cache: Optional[SharedQuoteCache] = None,
        candle_cache: Optional[SharedCandleCache] = None,
        market_status_refresh: Optional[Callable[[], str]] = None,
        stream_status: Optional[Callable[[], dict]] = None,
        stream_quote_handler: Optional[Callable[[Quote], list]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config
        self.instruments = list(instruments)
        self.market_data = market_data
        self.accounts = accounts
        self.store = store
        self.survey_cache = survey_cache or SharedSurveyCache(
            ttl_seconds=config.survey_interval_seconds
        )
        self.quote_cache = quote_cache or SharedQuoteCache()
        self.candle_cache = candle_cache or SharedCandleCache()
        self.market_status_refresh = market_status_refresh
        self.stream_status = stream_status
        self.stream_quote_handler = stream_quote_handler or self.accounts.on_quote
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._scan_thread: Optional[Thread] = None
        self._feature_thread: Optional[Thread] = None
        self._fallback_thread: Optional[Thread] = None
        self._accept_scans = False
        self._accept_market_data = False
        self._next_scan_at = 0.0
        self._next_market_status_at = 0.0
        self._market_status = "UNKNOWN"
        self._status = "DRAFT"
        self._started_at: Optional[str] = None
        self._finished_at: Optional[str] = None
        self._errors: list[str] = []
        self._scan_count = 0
        self._poll_count = 0
        self._stream_quote_count = 0
        self._stream_drop_count = 0
        self._rest_fallback_count = 0
        self._last_stream_quote_at: Optional[datetime] = None
        self._stream_seen_at: dict[str, datetime] = {}
        self._stream_queue: Queue[Quote] = Queue(maxsize=5_000)
        self._feature_results: Queue[tuple] = Queue(maxsize=100)
        self._fallback_results: Queue[tuple] = Queue(maxsize=10)
        self._last_quotes: dict[str, Quote] = {}
        self._last_features: dict[str, dict] = {}
        self._setups: dict[str, PriceConfirmationSetup] = {}
        self._setup_stops: dict[str, float] = {}
        self._entry_intents: dict[tuple[str, str], dict] = {}
        self._pending_entries: set[tuple[str, str]] = set()
        self._open: dict[tuple[str, str], dict] = {}
        self._exits: dict[tuple[str, str], RatchetExit] = {}
        self._pending_exits: set[tuple[str, str]] = set()
        self._signal_exit_latches: dict[tuple[str, str], dict] = {}
        self._traded_symbols: set[str] = set()
        self._symbols = {item.instrument_key: item.symbol for item in self.instruments}

        self.candidates = CandidateQueue(config.candidate_pool_size)
        self.manager = MonitoringManager(
            self.candidates,
            config.slot_count,
            config.lease_seconds,
            config.cooldown_seconds,
            self._record,
        )
        self.signal_config = SignalConfig()
        self.feature_engine = SharedFeatureEngine(
            market_data,
            self.candle_cache,
            self.signal_config,
            config.market_timezone,
            max_symbols=config.candidate_pool_size + config.slot_count,
        )
        self._ensure_accounts()
        self.execution = ExecutionQueue(
            store, config.session_id, self._execute_intent, self._record
        )
        queued = store.execution_intents(config.session_id, status="QUEUED", limit=100)
        self._pending_entries.update(
            (payload["strategy"], payload["instrument_key"])
            for payload in queued
            if payload.get("side") == "BUY"
        )
        self._pending_exits.update(
            (payload["strategy"], payload["instrument_key"])
            for payload in queued
            if payload.get("side") == "SELL"
        )
        self._restore()

    def account_id(self, strategy: str) -> str:
        return f"{self.config.account_prefix}-{self.config.session_date}-{strategy.lower()}"

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            self._stop.clear()
            self._accept_scans = True
            self._accept_market_data = True
            self._status = "RUNNING"
            self._started_at = self._started_at or self._now().isoformat()
            self._finished_at = None
            self._thread = Thread(
                target=self._run,
                name=f"parallel-monitoring-{self.config.session_date}",
                daemon=True,
            )
            self._persist()
            self._thread.start()
            return self.snapshot()

    def stop(self, reason: str = "STOPPED", liquidate: bool = True) -> dict:
        self._stop.set()
        self._accept_scans = False
        self._accept_market_data = False
        thread = self._thread
        if thread and thread.is_alive() and thread is not current_thread():
            thread.join(timeout=max(10.0, self.config.poll_interval_seconds * 3))
        if liquidate:
            self._liquidate(reason)
        with self._lock:
            self._status = "COMPLETED" if reason in {"SESSION_END", "STOPPED"} else reason
            self._finished_at = self._now().isoformat()
            self._persist()
            return self.snapshot()

    def refresh_candidates(self, rows: Iterable[dict], at: Optional[datetime] = None) -> list[dict]:
        """Install a survey snapshot. Public for deterministic integration tests."""

        clock = at or self._now()
        eligible = [
            row
            for row in rows
            if row.get("eligible")
            and row.get("volume_confirmed")
            and float(row.get("momentum_score", 0)) >= self.config.minimum_score
            and float(row.get("recent_15m_change_pct", 0)) > 0
        ]
        candidates = self.candidates.refresh(eligible, clock)
        payload = [item.to_dict() for item in candidates]
        self.store.save_monitoring_rankings(
            self.config.session_id, self.candidates.version, payload
        )
        self._record(
            "CANDIDATE_RANK_UPDATED",
            {
                "ranking_version": self.candidates.version,
                "eligible": len(payload),
                "candidates": [item["symbol"] for item in payload],
            },
        )
        self.manager.rebalance(clock)
        self._persist()
        return payload

    def process_cycle(
        self,
        quotes: dict[str, Quote],
        features: Optional[dict[str, FeatureSnapshot]] = None,
        clock: Optional[datetime] = None,
    ) -> None:
        """Process one quote cycle; supplied features make the core easy to test."""

        now = clock or self._now()
        self._poll_count += 1
        self.manager.rebalance(now)
        for quote in quotes.values():
            self._last_quotes[quote.instrument_key] = quote
            self.accounts.on_quote(quote)
            self._process_quote_exits(quote)
            self._process_price_confirmation(quote)

        self._refresh_features(now, features)
        self._emit_ready_entries(quotes)
        self._drain_execution()
        self.manager.rebalance(now)
        self._persist()

    def enqueue_stream_quote(self, quote: Quote) -> bool:
        """Queue a WebSocket quote without mutating strategy state on the SDK thread."""

        if self._status != "RUNNING":
            return False
        monitored = self.manager.assigned_keys()
        with self._lock:
            held = {key for _, key in self._open}
        if quote.instrument_key not in monitored | held:
            return False
        try:
            self._stream_queue.put_nowait(quote)
        except Full:
            # Prefer a recent tick over an old queued tick during an exceptional
            # burst. Normal NIFTY 100 traffic stays far below this bound.
            try:
                self._stream_queue.get_nowait()
            except Empty:
                pass
            self._stream_drop_count += 1
            try:
                self._stream_queue.put_nowait(quote)
            except Full:
                self._stream_drop_count += 1
                return False
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.config.session_id,
                "session_date": self.config.session_date,
                "status": self._status,
                "architecture": "PARALLEL_MONITORING_V2",
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "scan_count": self._scan_count,
                "poll_count": self._poll_count,
                "data_transport": self._transport_snapshot(),
                "market_status": self._market_status,
                "errors": list(self._errors[-20:]),
                "config": asdict(self.config),
                "monitoring": self.manager.snapshot(self._now()),
                "candidate_queue": {
                    "ranking_version": self.candidates.version,
                    "updated_at": (
                        self.candidates.updated_at.isoformat()
                        if self.candidates.updated_at
                        else None
                    ),
                    "candidates": self.candidates.snapshot(),
                },
                "paper_portfolios": self.portfolios(),
                "open_positions": [dict(value) for value in self._open.values()],
                "setups": [value.to_dict() for value in self._setups.values()],
                "execution_queue": self.execution.snapshot(),
                "last_features": list(self._last_features.values()),
            }

    def slots(self) -> list[dict]:
        return self.manager.snapshot(self._now())["slots"]

    def slot(self, slot_id: int) -> Optional[dict]:
        return next((slot for slot in self.slots() if slot["slot_id"] == slot_id), None)

    def symbol(self, identifier: str) -> dict:
        instrument_key = identifier
        if identifier not in self._symbols:
            instrument_key = next(
                (
                    key
                    for key, symbol in self._symbols.items()
                    if symbol.upper() == identifier.upper()
                ),
                identifier,
            )
        slot = self.manager.slot_for(instrument_key)
        return {
            "instrument_key": instrument_key,
            "symbol": self._symbols.get(instrument_key, instrument_key),
            "slot": slot.to_dict(self._now()) if slot else None,
            "candidate": (
                self.candidates.get(instrument_key).to_dict()
                if self.candidates.get(instrument_key)
                else None
            ),
            "feature": self._last_features.get(instrument_key),
            "positions": [
                value
                for (strategy, key), value in self._open.items()
                if key == instrument_key
            ],
            "setup": (
                self._setups[instrument_key].to_dict()
                if instrument_key in self._setups
                else None
            ),
        }

    def portfolios(self) -> list[dict]:
        values = []
        for strategy in STRATEGIES:
            snapshot = self.accounts.get(self.account_id(strategy)).snapshot()
            values.append(
                {
                    "strategy": strategy,
                    "entry_mode": STRATEGY_MODES[strategy],
                    "account_id": self.account_id(strategy),
                    **snapshot,
                }
            )
        return values

    def _run(self) -> None:
        self._next_scan_at = 0.0
        next_feature_at = 0.0
        next_fallback_at = 0.0
        next_persist_at = 0.0
        while not self._stop.is_set():
            started = monotonic()
            try:
                clock = self._now()
                if started >= self._next_scan_at:
                    self._start_scan()
                    self._next_scan_at = started + self.config.survey_interval_seconds
                if self.market_status_refresh and started >= self._next_market_status_at:
                    if not self._refresh_market_status_from_stream(clock):
                        self._refresh_market_status()
                    self._next_market_status_at = started + 60.0

                self._drain_stream_quotes()
                self._drain_feature_results()
                self._drain_fallback_results()
                if started >= next_feature_at:
                    self._start_feature_refresh()
                    next_feature_at = started + self.config.feature_refresh_seconds

                keys = sorted(
                    self.manager.assigned_keys() | {key for _, key in self._open}
                )
                fallback_keys = self._fallback_keys(keys, clock)
                if fallback_keys and started >= next_fallback_at:
                    self._start_fallback_poll(fallback_keys, clock)
                    next_fallback_at = started + self.config.poll_interval_seconds

                if started >= next_persist_at:
                    self.manager.rebalance(clock)
                    self._drain_execution()
                    self._persist()
                    next_persist_at = started + min(
                        5.0, self.config.poll_interval_seconds
                    )
            except Exception as error:  # noqa: BLE001 - the next poll can recover
                self._errors.append(str(error)[:500])
                self._record("WORKER_ERROR", {"message": str(error)[:500]})
                self._persist()
            delay = max(
                0.01,
                min(0.25, self.config.feature_refresh_seconds)
                - (monotonic() - started),
            )
            self._stop.wait(delay)

    def _drain_stream_quotes(self, limit: int = 1_000) -> int:
        processed = 0
        for _ in range(limit):
            try:
                quote = self._stream_queue.get_nowait()
            except Empty:
                break
            previous = self._last_quotes.get(quote.instrument_key)
            if previous and quote.timestamp < previous.timestamp:
                continue
            self._last_quotes[quote.instrument_key] = quote
            self.stream_quote_handler(quote)
            self._process_quote_exits(quote)
            self._process_price_confirmation(quote)
            self._emit_ready_entries({quote.instrument_key: quote})
            self._drain_execution()
            self._stream_quote_count += 1
            self._last_stream_quote_at = self._now()
            self._stream_seen_at[quote.instrument_key] = self._last_stream_quote_at
            processed += 1
        return processed

    def _refresh_features(
        self,
        clock: datetime,
        supplied: Optional[dict[str, FeatureSnapshot]] = None,
    ) -> None:
        feature_map = supplied or {}
        for key in self.manager.assigned_keys():
            snapshot = feature_map.get(key)
            if snapshot is None and supplied is None:
                try:
                    snapshot = self.feature_engine.snapshot(key, clock)
                except Exception as error:  # noqa: BLE001 - isolate one provider read
                    self._record(
                        "DATA_UNAVAILABLE",
                        {"message": str(error)[:300], "stage": "ONE_MINUTE_FEATURES"},
                        instrument_key=key,
                    )
                    continue
            if snapshot:
                self._process_feature(snapshot)

    def _start_feature_refresh(self) -> None:
        if self._feature_thread and self._feature_thread.is_alive():
            return
        keys = sorted(self.manager.assigned_keys())
        if not keys:
            return
        self._feature_thread = Thread(
            target=self._collect_features,
            args=(keys,),
            name=f"parallel-features-{self.config.session_date}",
            daemon=True,
        )
        self._feature_thread.start()

    def _collect_features(self, keys: list[str]) -> None:
        for key in keys:
            if not self._accept_market_data:
                return
            try:
                # A refresh can span a minute boundary when the provider is
                # slow, so each symbol receives a current admission clock.
                snapshot = self.feature_engine.snapshot(key, self._now())
                if snapshot:
                    self._feature_results.put(("SNAPSHOT", snapshot), timeout=1)
            except Exception as error:  # noqa: BLE001 - report on coordinator thread
                try:
                    self._feature_results.put(
                        ("ERROR", key, str(error)[:300]), timeout=1
                    )
                except Full:
                    return

    def _drain_feature_results(self, limit: int = 100) -> int:
        processed = 0
        for _ in range(limit):
            try:
                result = self._feature_results.get_nowait()
            except Empty:
                break
            if result[0] == "SNAPSHOT":
                snapshot = result[1]
                if snapshot.instrument_key in self.manager.assigned_keys():
                    self._process_feature(snapshot)
            else:
                _, key, message = result
                self._record(
                    "DATA_UNAVAILABLE",
                    {"message": message, "stage": "ONE_MINUTE_FEATURES"},
                    instrument_key=key,
                )
            processed += 1
        return processed

    def _start_fallback_poll(self, keys: list[str], requested_at: datetime) -> None:
        if self._fallback_thread and self._fallback_thread.is_alive():
            return
        self._fallback_thread = Thread(
            target=self._collect_fallback_quotes,
            args=(list(keys), requested_at),
            name=f"parallel-rest-fallback-{self.config.session_date}",
            daemon=True,
        )
        self._fallback_thread.start()

    def _collect_fallback_quotes(
        self, keys: list[str], requested_at: datetime
    ) -> None:
        try:
            quotes = self.quote_cache.get(
                self.market_data,
                keys,
                max_age_seconds=self.config.poll_interval_seconds,
            )
            result = ("QUOTES", quotes, requested_at)
        except Exception as error:  # noqa: BLE001 - report on coordinator thread
            result = ("ERROR", str(error)[:300])
        if not self._accept_market_data:
            return
        try:
            self._fallback_results.put(result, timeout=1)
        except Full:
            return

    def _drain_fallback_results(self, limit: int = 10) -> int:
        processed = 0
        for _ in range(limit):
            try:
                result = self._fallback_results.get_nowait()
            except Empty:
                break
            if result[0] == "QUOTES":
                _, quotes, requested_at = result
                active = self.manager.assigned_keys() | {key for _, key in self._open}
                quotes = {
                    key: quote
                    for key, quote in quotes.items()
                    if key in active
                    and (
                        key not in self._stream_seen_at
                        or self._stream_seen_at[key] <= requested_at
                    )
                }
                if quotes:
                    self.process_cycle(quotes, features={}, clock=self._now())
                self._rest_fallback_count += 1
            else:
                self._record(
                    "DATA_UNAVAILABLE",
                    {"message": result[1], "stage": "REST_QUOTE_FALLBACK"},
                )
            processed += 1
        return processed

    def _drain_execution(self) -> None:
        for intent in self.execution.drain():
            self._apply_execution_result(intent)

    def _stream_is_healthy(self, clock: Optional[datetime] = None) -> bool:
        if not self.stream_status:
            return False
        try:
            status = self.stream_status()
        except Exception:  # noqa: BLE001 - status failure activates REST fallback
            return False
        if status.get("state") != "connected" or not status.get("last_message_at"):
            return False
        try:
            last_message = datetime.fromisoformat(status["last_message_at"])
        except (TypeError, ValueError):
            return False
        if last_message.tzinfo is None:
            last_message = last_message.replace(tzinfo=timezone.utc)
        now = clock or self._now()
        return (now - last_message).total_seconds() <= self.config.stream_stale_seconds

    def _fallback_keys(self, keys: list[str], clock: datetime) -> list[str]:
        if not keys:
            return []
        if not self._stream_is_healthy(clock):
            return keys
        return [
            key
            for key in keys
            if key not in self._stream_seen_at
            or (clock - self._stream_seen_at[key]).total_seconds()
            > self.config.stream_stale_seconds
        ]

    def _transport_snapshot(self) -> dict:
        status = None
        if self.stream_status:
            try:
                status = self.stream_status()
            except Exception as error:  # noqa: BLE001 - diagnostics must stay available
                status = {"state": "error", "last_error": str(error)[:300]}
        clock = self._now()
        active_keys = sorted(
            self.manager.assigned_keys() | {key for _, key in self._open}
        )
        stale_keys = self._fallback_keys(active_keys, clock)
        stream_healthy = self._stream_is_healthy(clock)
        return {
            "primary": "UPSTOX_WEBSOCKET_V3",
            "active": (
                "REST_FALLBACK"
                if not stream_healthy
                else "HYBRID"
                if stale_keys
                else "WEBSOCKET"
            ),
            "stream": status,
            "fallback_symbols": stale_keys,
            "stream_quotes_processed": self._stream_quote_count,
            "stream_quotes_dropped": self._stream_drop_count,
            "stream_queue_depth": self._stream_queue.qsize(),
            "last_stream_quote_at": (
                self._last_stream_quote_at.isoformat()
                if self._last_stream_quote_at
                else None
            ),
            "rest_fallback_polls": self._rest_fallback_count,
        }

    def _refresh_market_status(self) -> None:
        try:
            status = self.market_status_refresh() if self.market_status_refresh else "UNKNOWN"
            if status != self._market_status:
                self._market_status = status
                self._record("MARKET_STATUS", {"status": status})
        except Exception as error:  # noqa: BLE001 - execution must fail closed
            self._market_status = "UNKNOWN"
            self.accounts.update_market_status({"NSE_EQ": "UNKNOWN"})
            self._record(
                "DATA_UNAVAILABLE",
                {"stage": "MARKET_STATUS", "message": str(error)[:300]},
            )

    def _refresh_market_status_from_stream(self, clock: datetime) -> bool:
        if not self.stream_status or not self._stream_is_healthy(clock):
            return False
        try:
            statuses = self.stream_status().get("market_statuses", {})
        except Exception:  # noqa: BLE001 - REST status remains the fallback
            return False
        status = statuses.get("NSE_EQ")
        if not status:
            return False
        if status != self._market_status:
            self._market_status = status
            self._record("MARKET_STATUS", {"status": status, "source": "WEBSOCKET"})
        return True

    def _start_scan(self) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._scan_thread = Thread(
            target=self._scan_once,
            name=f"parallel-scanner-{self.config.session_date}",
            daemon=True,
        )
        self._scan_thread.start()

    def _scan_once(self) -> None:
        try:
            result = self.survey_cache.run(
                self.market_data,
                self.instruments,
                self.config.minimum_relative_volume,
                context_instrument_key=self.config.benchmark_instrument_key,
            )
            if not self._accept_scans:
                return
            self._scan_count += 1
            if result.get("analyzed", 0) > 0:
                self.refresh_candidates(result["results"], self._now())
            if result.get("failures"):
                self._record(
                    "DATA_UNAVAILABLE",
                    {
                        "stage": "SURVEY",
                        "requested": result.get("requested"),
                        "analyzed": result.get("analyzed"),
                        "failures": result["failures"][:20],
                    },
                )
            self._persist()
        except Exception as error:  # noqa: BLE001 - retain last good ranking
            if not self._accept_scans:
                return
            self._errors.append(str(error)[:500])
            self._record(
                "DATA_UNAVAILABLE", {"stage": "SURVEY", "message": str(error)[:500]}
            )

    def _process_feature(self, snapshot: FeatureSnapshot) -> None:
        key = snapshot.instrument_key
        self._last_features[key] = snapshot.to_dict()
        feature_limit = self.config.candidate_pool_size + self.config.slot_count
        while len(self._last_features) > feature_limit:
            protected = self.manager.assigned_keys() | {
                open_key for _, open_key in self._open
            }
            removable = next(
                (stored for stored in self._last_features if stored not in protected),
                None,
            )
            if removable is None:
                break
            self._last_features.pop(removable, None)
        candidate = self.candidates.get(key)
        symbol = candidate.symbol if candidate else self._symbols.get(key, key)
        setup = self._setups.get(key)
        if setup and setup.state == SetupState.ARMED:
            before = setup.state
            setup.on_bar(snapshot.features)
            if setup.state != before:
                self._record(
                    "SETUP_CANCELLED", setup.to_dict(), instrument_key=key, strategy="B"
                )
                self.manager.update_strategy(key, "B", "WATCHING", setup.reason)
                self._setup_stops.pop(key, None)
                self._maybe_start_cooldown(key, setup.reason or "SETUP_CANCELLED")

        evaluations = {
            strategy: evaluate_entry(mode, snapshot.features, self.signal_config)
            for strategy, mode in STRATEGY_MODES.items()
        }
        shared_exit = (
            evaluate_shared_exit(snapshot.features, self.signal_config)
            if any(open_key == key for _, open_key in self._open)
            else None
        )
        near_signal = bool(candidate) and any(
            self._near_signal(value) for value in evaluations.values()
        )
        self.manager.mark_near_signal(key, near_signal, snapshot.available_at)
        structural_stop = min(item.low for item in snapshot.candles[-3:])

        for strategy, evaluation in evaluations.items():
            self._record(
                "SIGNAL_EVALUATED",
                {
                    "symbol": symbol,
                    "bar_id": snapshot.bar_id,
                    "shared_feature_bar_id": snapshot.features.bar_id,
                    "signal": evaluation,
                    "stale": snapshot.stale,
                },
                instrument_key=key,
                strategy=strategy,
            )
            position_key = (strategy, key)
            if position_key in self._open:
                exit_signal = dict(shared_exit or {})
                entered_at = datetime.fromisoformat(self._open[position_key]["entered_at"])
                if (
                    exit_signal["actionable"]
                    and entered_at < snapshot.features.bar_end
                    and not snapshot.stale
                ):
                    self._signal_exit_latches.setdefault(
                        position_key,
                        {
                            "decision_id": snapshot.bar_id,
                            "decision_at": snapshot.available_at,
                            "reason": exit_signal["reason"],
                            "diagnostics": exit_signal,
                        },
                    )
                    self._record(
                        "SIGNAL_EMITTED",
                        {
                            "side": "SELL",
                            "symbol": symbol,
                            "bar_id": snapshot.bar_id,
                            "signal": exit_signal,
                        },
                        instrument_key=key,
                        strategy=strategy,
                    )
                continue
            if position_key in self._entry_intents or position_key in self._pending_entries:
                continue
            if (
                strategy == "B"
                and setup
                and setup.state in {SetupState.ARMED, SetupState.TRIGGERED}
            ):
                continue
            if not candidate:
                self.manager.update_strategy(
                    key, strategy, "WATCHING", "NOT_IN_CURRENT_RANKING", snapshot.available_at
                )
                continue
            if snapshot.stale or not evaluation["actionable"]:
                self.manager.update_strategy(
                    key, strategy, "WATCHING", evaluation["reason"], snapshot.available_at
                )
                continue
            if strategy == "B":
                active = self._setups.get(key)
                if not active or active.state != SetupState.ARMED:
                    active = PriceConfirmationSetup.arm(
                        f"{self.config.session_id}:B:{key}:{snapshot.bar_id}",
                        key,
                        snapshot.features,
                        snapshot.available_at,
                        self.config.instrument_tick_size,
                        self.signal_config,
                    )
                    self._setups[key] = active
                    self._setup_stops[key] = structural_stop
                    self.manager.update_strategy(
                        key, "B", "ARMED", "PRICE_CONFIRMATION", snapshot.available_at
                    )
                    self._record(
                        "SETUP_ARMED",
                        {
                            **active.to_dict(),
                            "symbol": symbol,
                            "signal": evaluation,
                            "structural_stop": structural_stop,
                        },
                        instrument_key=key,
                        strategy="B",
                    )
            else:
                self._entry_intents[position_key] = {
                    "id": f"{self.config.session_id}:{strategy}:{key}:{snapshot.bar_id}:BUY",
                    "available_at": snapshot.available_at,
                    "expires_at": min(
                        snapshot.available_at
                        + timedelta(seconds=self.signal_config.market_intent_ttl_seconds),
                        snapshot.features.bar_end + timedelta(minutes=1),
                    ),
                    "structural_stop": structural_stop,
                    "signal": evaluation,
                    "bar_id": snapshot.bar_id,
                }
                self.manager.update_strategy(
                    key, strategy, "SIGNALLED", evaluation["reason"], snapshot.available_at
                )
                self._record(
                    "SIGNAL_EMITTED",
                    {
                        "symbol": symbol,
                        "bar_id": snapshot.bar_id,
                        "signal": evaluation,
                    },
                    instrument_key=key,
                    strategy=strategy,
                )

    def _process_price_confirmation(self, quote: Quote) -> None:
        setup = self._setups.get(quote.instrument_key)
        if not setup or setup.state != SetupState.ARMED:
            return
        previous = setup.state
        setup.on_quote(quote.last_price, quote.timestamp)
        if setup.state == previous:
            return
        key = quote.instrument_key
        if setup.state == SetupState.TRIGGERED:
            features = self._last_features.get(key, {}).get("features", {})
            self._entry_intents[("B", key)] = {
                "id": f"{setup.setup_id}:BUY",
                "available_at": setup.available_at,
                "expires_at": setup.expiry,
                "structural_stop": self._setup_stops.get(key, float(setup.setup_low)),
                "signal": features,
                "bar_id": setup.bar_id,
                "setup": setup.to_dict(),
            }
            self.manager.update_strategy(key, "B", "SIGNALLED", "BREAKOUT_CONFIRMED")
            self._record(
                "SIGNAL_EMITTED",
                {"symbol": self._symbols.get(key, key), "setup": setup.to_dict()},
                instrument_key=key,
                strategy="B",
            )
        else:
            self.manager.update_strategy(key, "B", "WATCHING", setup.reason)
            self._setup_stops.pop(key, None)
            self._record(
                "SETUP_CANCELLED",
                setup.to_dict(),
                instrument_key=key,
                strategy="B",
            )
            self._maybe_start_cooldown(key, setup.reason or "SETUP_CANCELLED")

    def _emit_ready_entries(self, quotes: dict[str, Quote]) -> None:
        for (strategy, key), signal in list(self._entry_intents.items()):
            quote = quotes.get(key)
            if not quote or quote.timestamp <= signal["available_at"]:
                continue
            if quote.timestamp >= signal["expires_at"]:
                self._entry_intents.pop((strategy, key), None)
                if strategy == "B" and key in self._setups:
                    self._setups[key].cancel(SetupState.EXPIRED, "ENTRY_INTENT_EXPIRED")
                    self._setup_stops.pop(key, None)
                self.manager.update_strategy(key, strategy, "WATCHING", "INTENT_EXPIRED")
                self._maybe_start_cooldown(key, "ENTRY_INTENT_EXPIRED")
                continue
            committed = self._open_count(strategy) + sum(
                1 for pending_strategy, _ in self._pending_entries if pending_strategy == strategy
            )
            if committed >= self.config.max_positions_per_strategy:
                self.manager.update_strategy(key, strategy, "WATCHING", "MAX_POSITIONS")
                continue
            quantity = floor(self.config.allocation_per_position / quote.last_price)
            if quantity <= 0 or signal["structural_stop"] >= quote.last_price:
                self._entry_intents.pop((strategy, key), None)
                if strategy == "B" and key in self._setups:
                    self._setups[key].cancel(SetupState.REJECTED, "INVALID_ENTRY_RISK")
                    self._setup_stops.pop(key, None)
                self.manager.update_strategy(key, strategy, "WATCHING", "INVALID_ENTRY_RISK")
                continue
            intent = ExecutionIntent(
                id=signal["id"],
                session_id=self.config.session_id,
                strategy=strategy,
                account_id=self.account_id(strategy),
                instrument_key=key,
                symbol=self._symbols.get(key, key),
                side="BUY",
                quantity=quantity,
                observed_price=quote.last_price,
                reason=STRATEGY_MODES[strategy],
                created_at=intent_timestamp(),
                metadata={
                    **signal,
                    "available_at": signal["available_at"].isoformat(),
                    "expires_at": signal["expires_at"].isoformat(),
                },
            )
            if self.execution.enqueue(intent):
                self._entry_intents.pop((strategy, key), None)
                self._pending_entries.add((strategy, key))
                self.manager.update_strategy(key, strategy, "ENTRY_PENDING", intent.reason)
            else:
                self._entry_intents.pop((strategy, key), None)
                if strategy == "B" and key in self._setups:
                    self._setups[key].cancel(
                        SetupState.CANCELLED, "DUPLICATE_INTENT_IGNORED"
                    )
                    self._setup_stops.pop(key, None)
                self.manager.update_strategy(
                    key, strategy, "WATCHING", "DUPLICATE_INTENT_IGNORED"
                )

    def _process_quote_exits(self, quote: Quote) -> None:
        for (strategy, key), rule in list(self._exits.items()):
            if key != quote.instrument_key or (strategy, key) in self._pending_exits:
                continue
            candidate = self.candidates.get(key)
            relative_volume = (
                candidate.payload.get("relative_volume") if candidate else None
            )
            diagnostics = rule.update(quote.last_price, quote.timestamp, relative_volume)
            state = self._open.get((strategy, key))
            if state:
                state["last_price"] = quote.last_price
                state["exit_state"] = diagnostics
            if diagnostics["reason"]:
                self._queue_exit(
                    strategy,
                    key,
                    quote,
                    diagnostics["reason"],
                    diagnostics,
                    f"quote-{quote.timestamp.isoformat()}",
                )
                continue
            latch = self._signal_exit_latches.get((strategy, key))
            if latch and quote.timestamp > latch["decision_at"]:
                self._queue_exit(
                    strategy,
                    key,
                    quote,
                    latch["reason"],
                    latch["diagnostics"],
                    latch["decision_id"],
                )

    def _queue_exit(
        self,
        strategy: str,
        key: str,
        quote: Optional[Quote],
        reason: str,
        diagnostics: dict,
        decision_id: str,
    ) -> None:
        position_key = (strategy, key)
        if position_key in self._pending_exits or not quote:
            return
        broker = self.accounts.get(self.account_id(strategy))
        position = broker.positions.get(key)
        if not position or position.quantity <= 0:
            return
        intent = ExecutionIntent(
            id=(
                f"{self.config.session_id}:{strategy}:{key}:{decision_id}:"
                f"{quote.timestamp.isoformat()}:SELL"
            ),
            session_id=self.config.session_id,
            strategy=strategy,
            account_id=self.account_id(strategy),
            instrument_key=key,
            symbol=self._symbols.get(key, key),
            side="SELL",
            quantity=position.quantity,
            observed_price=quote.last_price,
            reason=reason,
            created_at=intent_timestamp(),
            metadata={"exit_state": diagnostics},
        )
        if self.execution.enqueue(intent):
            self._pending_exits.add(position_key)
            self.manager.update_strategy(key, strategy, "EXIT_PENDING", reason)

    def _execute_intent(self, intent: ExecutionIntent) -> dict:
        broker = self.accounts.get(intent.account_id)
        quote = self._last_quotes.get(intent.instrument_key)
        if quote is None:
            raise ValueError("no current quote is available for execution")
        current = broker.quotes.get(intent.instrument_key)
        if current is None or current.timestamp != quote.timestamp:
            broker.on_quote(quote)
        side = Side.BUY if intent.side == "BUY" else Side.SELL
        order = broker.submit(
            Order(
                instrument_key=intent.instrument_key,
                side=side,
                quantity=intent.quantity,
                order_type=OrderType.MARKET,
                strategy_id=f"{self.config.session_id}:{intent.strategy}",
                product=Product.INTRADAY,
                validity=Validity.IOC,
                account_id=intent.account_id,
            )
        )
        return order.to_dict()

    def _apply_execution_result(self, intent: ExecutionIntent) -> None:
        key = intent.instrument_key
        position_key = (intent.strategy, key)
        broker = self.accounts.get(intent.account_id)
        if intent.side == "BUY":
            self._pending_entries.discard(position_key)
            if intent.status not in {"FILLED", "PARTIAL"}:
                if intent.strategy == "B" and key in self._setups:
                    self._setups[key].cancel(
                        SetupState.REJECTED, intent.error or "ENTRY_REJECTED"
                    )
                    self._setup_stops.pop(key, None)
                self.manager.update_strategy(key, intent.strategy, "WATCHING", intent.error)
                self._maybe_start_cooldown(key, intent.error or "ENTRY_REJECTED")
                return
            position = broker.positions.get(key)
            if not position or position.quantity <= 0:
                return
            notional = position.quantity * position.average_price
            floor_pct = breakeven_pct(
                notional, broker.fee_schedule, broker.slippage_bps, Product.INTRADAY
            )
            structural_stop = float(intent.metadata["structural_stop"])
            rule = RatchetExit(
                position.average_price,
                max(floor_pct, 0.01),
                self._last_quotes[key].timestamp,
                ExitPolicy(),
                structural_stop,
            )
            self._exits[position_key] = rule
            self._open[position_key] = {
                "strategy": intent.strategy,
                "instrument_key": key,
                "symbol": intent.symbol,
                "quantity": position.quantity,
                "entry_price": position.average_price,
                "entered_at": self._last_quotes[key].timestamp.isoformat(),
                "last_price": self._last_quotes[key].last_price,
                "cost_floor_pct": floor_pct,
                "structural_stop": structural_stop,
                "entry_intent_id": intent.id,
                "exit_state": rule.to_dict(),
            }
            self._traded_symbols.add(key)
            if intent.strategy == "B" and key in self._setups:
                self._setups[key].state = (
                    SetupState.FILLED
                    if intent.status == "FILLED"
                    else SetupState.PARTIAL
                )
                self._setup_stops.pop(key, None)
            self.manager.update_strategy(key, intent.strategy, "OPEN", intent.reason)
            return

        self._pending_exits.discard(position_key)
        position = broker.positions.get(key)
        if intent.status == "FILLED" and (not position or position.quantity <= 0):
            self._open.pop(position_key, None)
            self._exits.pop(position_key, None)
            self._signal_exit_latches.pop(position_key, None)
            self.manager.update_strategy(key, intent.strategy, "CLOSED", intent.reason)
            self._maybe_start_cooldown(key, intent.reason)
        elif intent.status == "PARTIAL" and position and position.quantity > 0:
            if position_key in self._open:
                self._open[position_key]["quantity"] = position.quantity
            self.manager.update_strategy(key, intent.strategy, "OPEN", "EXIT_PARTIAL")
        else:
            self.manager.update_strategy(key, intent.strategy, "OPEN", "EXIT_REJECTED")

    def _liquidate(self, reason: str) -> None:
        keys = {key for _, key in self._open}
        if not keys:
            return
        try:
            quotes = self.market_data.ltp(sorted(keys))
        except Exception:  # noqa: BLE001 - use the last observed quote
            quotes = {key: self._last_quotes[key] for key in keys if key in self._last_quotes}
        for (strategy, key), state in list(self._open.items()):
            self._queue_exit(
                strategy,
                key,
                quotes.get(key),
                reason,
                self._exits[(strategy, key)].to_dict(),
                f"liquidate-{reason}",
            )
        for intent in self.execution.drain(limit=100):
            self._apply_execution_result(intent)

    def _near_signal(self, evaluation: dict) -> bool:
        if evaluation.get("actionable"):
            return True
        rejection = evaluation.get("rejection_checks", {})
        mode = evaluation.get("mode_checks", {})
        checks = [*rejection.values(), *mode.values()]
        return bool(checks) and sum(bool(value) for value in checks) >= max(1, len(checks) - 1)

    def _open_count(self, strategy: str) -> int:
        return sum(1 for current, _ in self._open if current == strategy)

    def _has_active_setup(self, key: str) -> bool:
        setup = self._setups.get(key)
        return bool(setup and setup.state in {SetupState.ARMED, SetupState.TRIGGERED})

    def _maybe_start_cooldown(self, key: str, reason: str) -> None:
        if key not in self._traded_symbols:
            return
        if (
            any(open_key == key for _, open_key in self._open)
            or any(intent_key == key for _, intent_key in self._entry_intents)
            or any(pending_key == key for _, pending_key in self._pending_entries)
            or self._has_active_setup(key)
        ):
            return
        self.manager.start_cooldown(
            key, self._symbols.get(key, key), reason, self._now()
        )

    def _ensure_accounts(self) -> None:
        for strategy in STRATEGIES:
            account_id = self.account_id(strategy)
            try:
                self.accounts.get(account_id)
            except KeyError:
                self.accounts.create(
                    account_id,
                    f"Parallel monitoring strategy {strategy} · {self.config.session_date}",
                    self.config.initial_cash,
                )

    def _record(
        self,
        event_type: str,
        payload: dict,
        instrument_key: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        value = {"timestamp": self._now().isoformat(), **payload}
        self.store.add_monitoring_event(
            self.config.session_id, event_type, value, instrument_key, strategy
        )

    def _persist(self) -> None:
        snapshot = self.snapshot()
        self.store.save_monitoring_session(snapshot)
        for strategy in STRATEGIES:
            self.store.save_momentum_run(
                build_monitoring_strategy_run(
                    snapshot,
                    strategy,
                    self.accounts,
                    self.store,
                )
            )

    def _restore(self) -> None:
        saved = self.store.monitoring_session(self.config.session_id)
        if not saved:
            return
        self._started_at = saved.get("started_at")
        self._scan_count = int(saved.get("scan_count", 0))
        self._poll_count = int(saved.get("poll_count", 0))
        self._market_status = saved.get("market_status", "UNKNOWN")
        self._errors = list(saved.get("errors", []))[-20:]
        self.manager.restore(saved.get("monitoring", {}))
        for strategy in STRATEGIES:
            self._traded_symbols.update(
                fill.instrument_key
                for fill in self.accounts.get(self.account_id(strategy)).fills
            )
        # Indicator buffers are intentionally rebuilt from provider candles.
        # Open risk state is safe to restore because fills and positions live in
        # the durable paper brokers and RatchetExit has an explicit serializer.
        for state in saved.get("open_positions", []):
            strategy = state["strategy"]
            key = state["instrument_key"]
            broker_position = self.accounts.get(self.account_id(strategy)).positions.get(key)
            if not broker_position or broker_position.quantity <= 0:
                continue
            self._open[(strategy, key)] = dict(state)
            exit_state = state.get("exit_state")
            if exit_state:
                self._exits[(strategy, key)] = RatchetExit.from_dict(exit_state, ExitPolicy())
            self.manager.update_strategy(key, strategy, "OPEN", "RESTORED")
        self._reconcile_persistent_portfolios()
        self._record("SESSION_RESTORED", {"open_positions": len(self._open)})

    def _reconcile_persistent_portfolios(self) -> None:
        """Make broker fills authoritative across every possible crash boundary."""

        intents = self.store.execution_intents(self.config.session_id, limit=1000)
        latest_buy: dict[tuple[str, str], dict] = {}
        for intent in intents:
            if intent.get("side") == "BUY":
                latest_buy.setdefault(
                    (intent["strategy"], intent["instrument_key"]), intent
                )

        for strategy in STRATEGIES:
            broker = self.accounts.get(self.account_id(strategy))
            for key, position in broker.positions.items():
                position_key = (strategy, key)
                if position.quantity <= 0 or position_key in self._open:
                    continue
                intent = latest_buy.get(position_key)
                if not intent:
                    self._record(
                        "WORKER_ERROR",
                        {
                            "message": "persistent position has no matching execution intent",
                            "account_id": self.account_id(strategy),
                        },
                        instrument_key=key,
                        strategy=strategy,
                    )
                    continue
                notional = position.quantity * position.average_price
                floor_pct = max(
                    breakeven_pct(
                        notional,
                        broker.fee_schedule,
                        broker.slippage_bps,
                        Product.INTRADAY,
                    ),
                    0.01,
                )
                metadata = intent.get("metadata", {})
                structural_stop = float(
                    metadata.get("structural_stop")
                    or position.average_price * (1 - 2 * floor_pct / 100)
                )
                buy_fills = [
                    fill
                    for fill in broker.fills
                    if fill.instrument_key == key and fill.side == Side.BUY
                ]
                entered_at = (
                    buy_fills[-1].timestamp
                    if buy_fills
                    else datetime.fromisoformat(intent["created_at"])
                )
                rule = RatchetExit(
                    position.average_price,
                    floor_pct,
                    entered_at,
                    ExitPolicy(),
                    structural_stop,
                )
                self._exits[position_key] = rule
                self._open[position_key] = {
                    "strategy": strategy,
                    "instrument_key": key,
                    "symbol": self._symbols.get(key, intent.get("symbol", key)),
                    "quantity": position.quantity,
                    "entry_price": position.average_price,
                    "entered_at": entered_at.isoformat(),
                    "last_price": position.average_price,
                    "cost_floor_pct": floor_pct,
                    "structural_stop": structural_stop,
                    "entry_intent_id": intent["id"],
                    "exit_state": rule.to_dict(),
                }
                self.manager.update_strategy(key, strategy, "OPEN", "BROKER_RECONCILED")

        queued_keys = self._pending_entries | self._pending_exits
        accepted = {
            (intent["strategy"], intent["instrument_key"]): intent
            for intent in intents
            if intent.get("status") == "ACCEPTED"
        }
        for slot in self.manager.snapshot(self._now())["slots"]:
            key = slot.get("instrument_key")
            if not key:
                continue
            for strategy, state in slot.get("strategies", {}).items():
                position_key = (strategy, key)
                saved_state = state.get("state")
                if saved_state not in {
                    "ARMED",
                    "SIGNALLED",
                    "ENTRY_PENDING",
                    "EXIT_PENDING",
                }:
                    continue
                if position_key in queued_keys or position_key in self._open:
                    continue
                uncertain = accepted.get(position_key)
                side = uncertain.get("side") if uncertain else None
                next_state = "CLOSED" if side == "SELL" else "WATCHING"
                reason = (
                    "VOLATILE_SETUP_REBUILT"
                    if saved_state in {"ARMED", "SIGNALLED"}
                    else "ACCEPTED_INTENT_NOT_RETRIED"
                )
                self.manager.update_strategy(
                    key,
                    strategy,
                    next_state,
                    reason,
                )
                if next_state == "CLOSED":
                    self._maybe_start_cooldown(key, "ACCEPTED_EXIT_RECONCILED")
                self._record(
                    (
                        "SETUP_CANCELLED"
                        if saved_state in {"ARMED", "SIGNALLED"}
                        else "EXECUTION_INTENT_REJECTED"
                    ),
                    {
                        "reason": reason,
                        "intent_id": uncertain.get("id") if uncertain else None,
                    },
                    instrument_key=key,
                    strategy=strategy,
                )


class ParallelMonitoringService:
    """Small NSE-hours supervisor for deployment restarts and daily sessions."""

    def __init__(
        self,
        enabled: bool,
        engine_factory: Callable[[str], ParallelMonitoringEngine],
        trading_day_check: Callable[[str], bool],
        market_ready: Callable[[], bool],
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.enabled = enabled
        self.engine_factory = engine_factory
        self.trading_day_check = trading_day_check
        self.market_ready = market_ready
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._engine: Optional[ParallelMonitoringEngine] = None
        self._lock = RLock()
        self._last_error: Optional[str] = None

    @property
    def engine(self) -> Optional[ParallelMonitoringEngine]:
        with self._lock:
            return self._engine

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="parallel-monitoring-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        engine = self.engine
        if engine and engine.snapshot()["status"] == "RUNNING":
            engine.stop("DEPLOYMENT", liquidate=False)

    def status(self) -> dict:
        engine = self.engine
        return {
            "enabled": self.enabled,
            "active": bool(engine and engine.snapshot()["status"] == "RUNNING"),
            "running": bool(engine and engine.snapshot()["status"] == "RUNNING"),
            "session": engine.snapshot() if engine else None,
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        market_zone = ZoneInfo("Asia/Kolkata")
        while not self._stop.is_set():
            now = self._now().astimezone(market_zone)
            open_window = time(9, 15) <= now.time() < time(15, 30)
            engine = self.engine
            needs_session = engine is None or engine.config.session_date != now.date().isoformat()
            try:
                trading_day = self.trading_day_check(now.date().isoformat())
            except Exception as error:  # noqa: BLE001 - keep the supervisor alive
                self._last_error = str(error)[:500]
                self._stop.wait(15)
                continue
            if open_window and trading_day and needs_session:
                if self.market_ready():
                    try:
                        candidate = self.engine_factory(now.date().isoformat())
                        with self._lock:
                            self._engine = candidate
                        candidate.start()
                        self._last_error = None
                    except Exception as error:  # noqa: BLE001 - retry transient startup failures
                        # Retry on the next supervisor pass; engine events cannot
                        # be recorded before a session exists.
                        self._last_error = str(error)[:500]
            elif (
                engine
                and engine.snapshot()["status"] == "RUNNING"
                and now.time() >= time(15, 30)
            ):
                engine.stop("SESSION_END", liquidate=True)
            self._stop.wait(15)
