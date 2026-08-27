from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import floor
from threading import Event, RLock, Thread
from time import monotonic
from typing import Dict, Iterable, Optional
from uuid import uuid4

from .accounts import PaperAccountManager
from .domain import Order, OrderType, Product, Quote, Side, Validity
from .market_data import UpstoxMarketData
from .research_store import ResearchStore
from .strategy_engine import MarketCoordinator
from .survey import MarketSurvey, SurveyInstrument

NIFTY50_INDEX_KEY = "NSE_INDEX|Nifty 50"


@dataclass(frozen=True)
class MomentumRunnerConfig:
    account_id: str
    duration_seconds: int = 300
    poll_interval_seconds: float = 5.0
    rescan_interval_seconds: float = 60.0
    max_positions: int = 2
    allocation_per_position: float = 25_000.0
    candidate_limit: int = 10
    minimum_score: float = 0.15
    minimum_relative_volume: float = 1.2
    entry_momentum_pct: float = 0.10
    reversal_pct: float = 0.10
    hard_stop_pct: float = 0.35

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id is required")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.poll_interval_seconds <= 0 or self.rescan_interval_seconds <= 0:
            raise ValueError("poll and rescan intervals must be positive")
        if self.max_positions <= 0 or self.candidate_limit <= 0:
            raise ValueError("position and candidate limits must be positive")
        if self.allocation_per_position <= 0:
            raise ValueError("allocation_per_position must be positive")
        if self.minimum_relative_volume < 1:
            raise ValueError("minimum_relative_volume must be at least 1x")
        if min(
            self.minimum_relative_volume,
            self.entry_momentum_pct,
            self.reversal_pct,
            self.hard_stop_pct,
        ) <= 0:
            raise ValueError("momentum and exit thresholds must be positive")


class MomentumReversalRunner:
    """Bounded paper session that discovers movers and exits trailing reversals."""

    def __init__(
        self,
        config: MomentumRunnerConfig,
        instruments: Iterable[SurveyInstrument],
        market_data: UpstoxMarketData,
        accounts: PaperAccountManager,
        coordinator: MarketCoordinator,
        runner_id: Optional[str] = None,
        store: Optional[ResearchStore] = None,
    ) -> None:
        self.id = runner_id or str(uuid4())
        self.config = config
        self.instruments = list(instruments)
        self.market_data = market_data
        self.accounts = accounts
        self.coordinator = coordinator
        self.store = store
        self._symbols = {item.instrument_key: item.symbol for item in self.instruments}
        self._lock = RLock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._status = "DRAFT"
        self._started_at: Optional[str] = None
        self._finished_at: Optional[str] = None
        self._scan_count = 0
        self._poll_count = 0
        self._candidates: Dict[str, dict] = {}
        self._history: Dict[str, deque[float]] = {}
        self._market_history: deque[float] = deque(maxlen=13)
        self._market_bases: dict[str, Optional[float]] = {
            "session_open": None,
            "recent_15m": None,
        }
        self._monitoring: list[dict] = []
        self._open: Dict[str, dict] = {}
        self._completed: set[str] = set()
        self._events: list[dict] = []
        self._errors: list[str] = []
        self._initial_equity = accounts.get(config.account_id).snapshot()["equity"]

    def start(self) -> dict:
        with self._lock:
            if self._status != "DRAFT":
                raise ValueError("runner has already been started")
            self._status = "RUNNING"
            self._started_at = self._now()
            self._record("STARTED", {"config": asdict(self.config)})
            self._thread = Thread(
                target=self._run,
                name=f"momentum-runner-{self.id[:8]}",
                daemon=True,
            )
            self._thread.start()
            self._persist()
            return self.snapshot()

    def stop(self) -> dict:
        self._stop.set()
        with self._lock:
            if self._status == "RUNNING":
                self._status = "STOPPING"
        self._persist()
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            broker = self.accounts.get(self.config.account_id)
            raw_portfolio = broker.snapshot()
            portfolio = {
                **raw_portfolio,
                "positions": [
                    {
                        **position,
                        "symbol": self._symbols.get(
                            position["instrument_key"], position["instrument_key"]
                        ),
                    }
                    for position in raw_portfolio["positions"]
                ],
            }
            order_ids = {
                order.id for order in broker.orders.values() if order.strategy_id == self.id
            }
            fills = [
                {
                    **fill.to_dict(),
                    "symbol": self._symbols.get(fill.instrument_key, fill.instrument_key),
                }
                for fill in broker.fills
                if fill.order_id in order_ids
            ]
            gross_pnl = sum(
                fill["gross_value"] if fill["side"] == Side.SELL.value else -fill["gross_value"]
                for fill in fills
            )
            run_fees = sum(fill["fees"] for fill in fills)
            return {
                "id": self.id,
                "status": self._status,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "config": asdict(self.config),
                "initial_equity": self._initial_equity,
                "scan_count": self._scan_count,
                "poll_count": self._poll_count,
                "candidates": list(self._candidates.values()),
                "open_positions": list(self._open.values()),
                "completed_instruments": sorted(self._completed),
                "events": list(self._events),
                "monitoring": list(self._monitoring),
                "errors": list(self._errors),
                "fills": fills,
                "metrics": {
                    "gross_pnl": round(gross_pnl, 2),
                    "fees": round(run_fees, 2),
                    "net_pnl": round(gross_pnl - run_fees, 2),
                },
                "portfolio": portfolio,
                "session_pnl": round(portfolio["equity"] - self._initial_equity, 2),
            }

    def _run(self) -> None:
        deadline = monotonic() + self.config.duration_seconds
        next_scan = 0.0
        try:
            while monotonic() < deadline and not self._stop.is_set():
                now = monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + self.config.rescan_interval_seconds
                self._poll()
                remaining = max(0.0, deadline - monotonic())
                self._stop.wait(min(self.config.poll_interval_seconds, remaining))
        except Exception as error:  # noqa: BLE001 - keeps cleanup and liquidation running
            self._error(error)
        finally:
            self._liquidate()
            with self._lock:
                self._status = "FAILED" if self._errors else "COMPLETED"
                self._finished_at = self._now()
                self._record("FINISHED", {"session_pnl": self.snapshot()["session_pnl"]})
                self._persist()

    def _scan(self) -> None:
        try:
            result = MarketSurvey(
                self.market_data, self.config.minimum_relative_volume
            ).run(self.instruments)
            candidates = [
                row
                for row in result["results"]
                if row["eligible"]
                and row["volume_confirmed"]
                and row["momentum_score"] >= self.config.minimum_score
                and row["recent_15m_change_pct"] > 0
            ][: self.config.candidate_limit]
            market_context_error = None
            try:
                nifty_candles = self.market_data.intraday_candles(
                    NIFTY50_INDEX_KEY, "minutes", 5
                )
                if nifty_candles:
                    self._market_bases = {
                        "session_open": nifty_candles[0].open,
                        "recent_15m": (
                            nifty_candles[-4].close
                            if len(nifty_candles) >= 4
                            else nifty_candles[0].close
                        ),
                    }
            except Exception as error:  # noqa: BLE001 - context must not stop trading
                market_context_error = str(error)[:200]
            with self._lock:
                self._scan_count += 1
                self._candidates = {row["instrument_key"]: row for row in candidates}
                self._record(
                    "SCAN",
                    {
                        "analyzed": result["analyzed"],
                        "failures": result["failures"],
                        "candidates": [row["symbol"] for row in candidates],
                        "low_volume_rejections": [
                            {
                                "symbol": row["symbol"],
                                "relative_volume": row["relative_volume"],
                            }
                            for row in result["results"]
                            if row["momentum_score"] >= self.config.minimum_score
                            and row["recent_15m_change_pct"] > 0
                            and not row["volume_confirmed"]
                        ][:10],
                        "market_context_error": market_context_error,
                    },
                )
            self._persist()
        except Exception as error:  # noqa: BLE001 - an individual rescan may recover
            self._error(error)

    def _poll(self) -> None:
        with self._lock:
            stock_keys = list(dict.fromkeys([*self._candidates, *self._open]))
            keys = [*stock_keys, NIFTY50_INDEX_KEY]
        if not keys:
            return
        try:
            quotes = self.market_data.ltp(keys)
        except Exception as error:  # noqa: BLE001 - an individual poll may recover
            self._error(error)
            return
        with self._lock:
            self._poll_count += 1
        market_quote = quotes.get(NIFTY50_INDEX_KEY)
        market_context = self._market_context(market_quote)
        observations = {}
        for key in stock_keys:
            quote = quotes.get(key)
            if not quote:
                continue
            self.coordinator.on_quote(quote)
            history = self._history.setdefault(quote.instrument_key, deque(maxlen=13))
            previous = history[-1] if history else None
            history.append(quote.last_price)
            candidate = self._candidates.get(key, {})
            observation = {
                "timestamp": quote.timestamp.isoformat(),
                "symbol": self._symbols.get(key, key),
                "instrument_key": key,
                "price": round(quote.last_price, 4),
                "previous_price": round(previous, 4) if previous else None,
                "sample_change_pct": round(_percent_change(quote.last_price, previous), 4),
                "window_start_price": round(history[0], 4),
                "window_change_pct": round(
                    _percent_change(quote.last_price, history[0]), 4
                ),
                "session_change_pct": candidate.get("session_change_pct"),
                "recent_15m_change_pct": candidate.get("recent_15m_change_pct"),
                "momentum_score": candidate.get("momentum_score"),
                "recent_volume": candidate.get("recent_volume"),
                "baseline_volume": candidate.get("baseline_volume"),
                "relative_volume": candidate.get("relative_volume"),
                "volume_signal": candidate.get("volume_signal"),
                **market_context,
                "decision": "WATCHING",
            }
            exit_reason = self._consider_exit(quote, previous)
            if exit_reason:
                observation["decision"] = f"EXIT_{exit_reason}"
            observations[key] = observation
            self._monitoring.append(observation)
        self._consider_entries(quotes, observations)
        self._persist()

    def _market_context(self, quote: Optional[Quote]) -> dict:
        if not quote:
            return {
                "nifty_price": None,
                "nifty_sample_change_pct": None,
                "nifty_window_change_pct": None,
                "nifty_session_change_pct": None,
                "nifty_recent_15m_change_pct": None,
            }
        previous = self._market_history[-1] if self._market_history else None
        self._market_history.append(quote.last_price)
        return {
            "nifty_price": round(quote.last_price, 4),
            "nifty_sample_change_pct": round(
                _percent_change(quote.last_price, previous), 4
            ),
            "nifty_window_change_pct": round(
                _percent_change(quote.last_price, self._market_history[0]), 4
            ),
            "nifty_session_change_pct": (
                round(
                    _percent_change(quote.last_price, self._market_bases["session_open"]),
                    4,
                )
                if self._market_bases["session_open"]
                else None
            ),
            "nifty_recent_15m_change_pct": (
                round(
                    _percent_change(quote.last_price, self._market_bases["recent_15m"]),
                    4,
                )
                if self._market_bases["recent_15m"]
                else None
            ),
        }

    def _consider_exit(self, quote: Quote, previous: Optional[float]) -> Optional[str]:
        state = self._open.get(quote.instrument_key)
        if not state:
            return None
        state["last_price"] = quote.last_price
        state["peak_price"] = max(state["peak_price"], quote.last_price)
        entry_price = state["entry_price"]
        drawdown = (state["peak_price"] / quote.last_price - 1) * 100
        pnl_pct = (quote.last_price / entry_price - 1) * 100
        reason = None
        if pnl_pct <= -self.config.hard_stop_pct:
            reason = "HARD_STOP"
        elif (
            previous is not None
            and quote.last_price < previous
            and state["peak_price"] > entry_price
            and drawdown >= self.config.reversal_pct
        ):
            reason = "MOMENTUM_REVERSAL"
        if reason:
            self._exit(quote, state, reason)
        return reason

    def _consider_entries(self, quotes: Dict[str, Quote], observations: dict) -> None:
        broker = self.accounts.get(self.config.account_id)
        open_count = sum(1 for position in broker.positions.values() if position.quantity > 0)
        slots = self.config.max_positions - open_count
        candidates = sorted(
            self._candidates.values(), key=lambda row: row["momentum_score"], reverse=True
        )
        for candidate in candidates:
            key = candidate["instrument_key"]
            observation = observations.get(key)
            if key in self._open:
                if observation:
                    observation["decision"] = "HOLDING_POSITION"
                continue
            if key in self._completed:
                if observation:
                    observation["decision"] = "ALREADY_TRADED"
                continue
            if slots <= 0:
                if observation:
                    observation["decision"] = "MAX_POSITIONS_REACHED"
                continue
            history = self._history.get(key)
            quote = quotes.get(key)
            if not history or len(history) < 3 or not quote:
                if observation:
                    observation["decision"] = "BUILDING_PRICE_HISTORY"
                continue
            if not candidate.get("volume_confirmed") or (
                candidate.get("relative_volume") is None
                or candidate["relative_volume"] < self.config.minimum_relative_volume
            ):
                if observation:
                    observation["decision"] = "RELATIVE_VOLUME_TOO_LOW"
                continue
            if not observation or (
                observation.get("nifty_window_change_pct") is None
                or observation["nifty_window_change_pct"] <= 0
                or observation.get("nifty_recent_15m_change_pct") is None
                or observation["nifty_recent_15m_change_pct"] <= 0
            ):
                if observation:
                    observation["decision"] = "NIFTY_SHORT_TERM_NOT_POSITIVE"
                continue
            window_change = (history[-1] / history[0] - 1) * 100
            if window_change < self.config.entry_momentum_pct:
                if observation:
                    observation["decision"] = "BELOW_ENTRY_THRESHOLD"
                continue
            if history[-1] <= history[-2]:
                if observation:
                    observation["decision"] = "LATEST_SAMPLE_NOT_RISING"
                continue
            quantity = floor(self.config.allocation_per_position / quote.last_price)
            if quantity <= 0:
                if observation:
                    observation["decision"] = "ALLOCATION_TOO_SMALL"
                continue
            entry_signal = {
                "reason": "UPWARD_MOVEMENT_CONFIRMED",
                "window_start_price": round(history[0], 4),
                "observed_price": round(quote.last_price, 4),
                "window_change_pct": round(window_change, 4),
                "entry_threshold_pct": self.config.entry_momentum_pct,
                "previous_price": round(history[-2], 4),
                "sample_change_pct": round(
                    _percent_change(history[-1], history[-2]), 4
                ),
                "session_change_pct": candidate["session_change_pct"],
                "recent_15m_change_pct": candidate["recent_15m_change_pct"],
                "momentum_score": candidate["momentum_score"],
                "recent_volume": candidate["recent_volume"],
                "baseline_volume": candidate["baseline_volume"],
                "relative_volume": candidate["relative_volume"],
                "minimum_relative_volume": self.config.minimum_relative_volume,
                "volume_signal": candidate["volume_signal"],
                "nifty_price": observation.get("nifty_price") if observation else None,
                "nifty_session_change_pct": (
                    observation.get("nifty_session_change_pct") if observation else None
                ),
                "nifty_recent_15m_change_pct": (
                    observation.get("nifty_recent_15m_change_pct")
                    if observation
                    else None
                ),
                "market_alignment": _market_alignment(observation),
            }
            order = self._submit(key, Side.BUY, quantity, quote)
            position = broker.positions.get(key)
            if order.filled_quantity <= 0 or not position or position.quantity <= 0:
                self._record(
                    "ENTRY_REJECTED",
                    {"symbol": candidate["symbol"], "order": order.to_dict()},
                )
                if observation:
                    observation["decision"] = "ENTRY_REJECTED"
                self._completed.add(key)
                continue
            self._open[key] = {
                "instrument_key": key,
                "symbol": candidate["symbol"],
                "quantity": position.quantity,
                "entry_price": position.average_price,
                "peak_price": quote.last_price,
                "last_price": quote.last_price,
                "entry_momentum_pct": round(window_change, 4),
            }
            self._record(
                "ENTRY_FILLED",
                {
                    "symbol": candidate["symbol"],
                    "observed_price": quote.last_price,
                    "entry_signal": entry_signal,
                    "order": order.to_dict(),
                },
            )
            if observation:
                observation["decision"] = "ENTRY_FILLED"
                observation["entry_signal"] = entry_signal
            slots -= 1

    def _exit(self, quote: Quote, state: dict, reason: str) -> None:
        broker = self.accounts.get(self.config.account_id)
        position = broker.positions.get(quote.instrument_key)
        if not position or position.quantity <= 0:
            self._open.pop(quote.instrument_key, None)
            return
        order = self._submit(quote.instrument_key, Side.SELL, position.quantity, quote)
        self._record(
            "EXIT_FILLED" if order.filled_quantity else "EXIT_REJECTED",
            {
                "symbol": state["symbol"],
                "reason": reason,
                "observed_price": quote.last_price,
                "order": order.to_dict(),
            },
        )
        if order.filled_quantity:
            self._open.pop(quote.instrument_key, None)
            self._completed.add(quote.instrument_key)

    def _liquidate(self) -> None:
        with self._lock:
            keys = list(self._open)
        if not keys:
            return
        try:
            quotes = self.market_data.ltp(keys)
        except Exception as error:  # noqa: BLE001 - use last observed prices below
            self._error(error)
            quotes = {}
        for key in keys:
            state = self._open.get(key)
            if not state:
                continue
            quote = quotes.get(key) or Quote(key, state["last_price"])
            self.coordinator.on_quote(quote)
            self._exit(quote, state, "SESSION_END")

    def _submit(self, key: str, side: Side, quantity: int, quote: Quote) -> Order:
        broker = self.accounts.get(self.config.account_id)
        self.coordinator.on_quote(quote)
        return broker.submit(
            Order(
                instrument_key=key,
                side=side,
                quantity=quantity,
                order_type=OrderType.MARKET,
                strategy_id=self.id,
                product=Product.INTRADAY,
                validity=Validity.IOC,
                account_id=self.config.account_id,
            )
        )

    def _record(self, event_type: str, payload: dict) -> None:
        self._events.append({"timestamp": self._now(), "type": event_type, **payload})

    def _error(self, error: Exception) -> None:
        with self._lock:
            message = str(error)[:500]
            self._errors.append(message)
            self._record("ERROR", {"message": message})

    def _persist(self) -> None:
        if self.store:
            self.store.save_momentum_run(self.snapshot())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _percent_change(current: float, base: Optional[float]) -> float:
    return (current / base - 1) * 100 if base else 0.0


def _market_alignment(observation: Optional[dict]) -> str:
    if not observation or observation.get("nifty_recent_15m_change_pct") is None:
        return "MARKET_CONTEXT_UNAVAILABLE"
    return (
        "WITH_BROAD_MARKET"
        if observation["nifty_recent_15m_change_pct"] >= 0
        else "AGAINST_BROAD_MARKET"
    )


class MomentumRunnerService:
    def __init__(self, store: Optional[ResearchStore] = None) -> None:
        self._runners: Dict[str, MomentumReversalRunner] = {}
        self._lock = RLock()
        self._store = store

    def add(self, runner: MomentumReversalRunner) -> dict:
        with self._lock:
            self._runners[runner.id] = runner
        return runner.start()

    def list(self) -> list[dict]:
        with self._lock:
            live = {runner.id: runner.snapshot() for runner in self._runners.values()}
        persisted = self._store.momentum_runs() if self._store else []
        combined = list(live.values()) + [item for item in persisted if item["id"] not in live]
        return sorted(combined, key=lambda item: item.get("started_at") or "", reverse=True)

    def get(self, runner_id: str) -> dict:
        with self._lock:
            runner = self._runners.get(runner_id)
        if runner:
            return runner.snapshot()
        persisted = self._store.momentum_run(runner_id) if self._store else None
        if persisted:
            return persisted
        raise KeyError(runner_id)

    def stop(self, runner_id: str) -> dict:
        with self._lock:
            return self._runners[runner_id].stop()

    def has_active(self, account_id: str) -> bool:
        with self._lock:
            return any(
                runner.config.account_id == account_id
                and runner.snapshot()["status"] in {"RUNNING", "STOPPING"}
                for runner in self._runners.values()
            )

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop()
