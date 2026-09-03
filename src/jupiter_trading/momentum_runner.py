from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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
from .survey import MarketSurvey, SharedSurveyCache, SurveyInstrument
from .trade_rules import (
    BarAggregator,
    EntryPolicy,
    ExitPolicy,
    RatchetExit,
    breakeven_pct,
    cost_model,
    evaluate_entry,
)

NIFTY50_INDEX_KEY = "NSE_INDEX|Nifty 50"
ENTRY_MODES = frozenset({"THREE_BAR", "ROLLING_WINDOW"})
ACTIVE_RUN_STATUSES = frozenset({"RUNNING", "STOPPING"})
EXIT_MODES = frozenset({"RATCHET", "REVERSAL"})


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
    universe_name: str = "NIFTY 100"
    universe_size: int = 100
    entry_mode: str = "THREE_BAR"
    exit_mode: str = "RATCHET"
    entry_bars: int = 3
    require_nifty_confirmation: bool = True
    minimum_cost_floor_pct: float = 0.01
    entry_cost_multiple: float = 1.0
    entry_noise_multiple: float = 2.0
    entry_timeframe_seconds: float = 0.0
    reentry_cooldown_seconds: float = 0.0
    survive_stop_multiple: float = 2.0
    lock_multiple: float = 1.5
    ride_multiple: float = 3.0
    min_gap_multiple: float = 1.5
    trail_window: int = 24
    fast_trail_window: int = 6
    volume_decay_ratio: float = 1.0
    confirmation_samples: int = 2
    time_stop_seconds: float = 240.0

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
        if not self.universe_name or self.universe_size <= 0:
            raise ValueError("universe name and size are required")
        if self.minimum_relative_volume < 1:
            raise ValueError("minimum_relative_volume must be at least 1x")
        if min(
            self.minimum_relative_volume,
            self.entry_momentum_pct,
            self.reversal_pct,
            self.hard_stop_pct,
        ) <= 0:
            raise ValueError("momentum and exit thresholds must be positive")
        if self.minimum_cost_floor_pct <= 0:
            raise ValueError("minimum_cost_floor_pct must be positive")
        if self.entry_timeframe_seconds < 0:
            raise ValueError("entry_timeframe_seconds cannot be negative")
        if self.reentry_cooldown_seconds < 0:
            raise ValueError("reentry_cooldown_seconds cannot be negative")
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode must be one of {sorted(ENTRY_MODES)}")
        if self.exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode must be one of {sorted(EXIT_MODES)}")
        self.entry_policy()
        self.exit_policy()

    def entry_policy(self) -> EntryPolicy:
        return EntryPolicy(
            bars=self.entry_bars,
            minimum_rise_pct=self.entry_momentum_pct,
            cost_floor_multiple=self.entry_cost_multiple,
            noise_multiple=self.entry_noise_multiple,
        )

    def exit_policy(self) -> ExitPolicy:
        return ExitPolicy(
            survive_stop_multiple=self.survive_stop_multiple,
            lock_multiple=self.lock_multiple,
            ride_multiple=self.ride_multiple,
            min_gap_multiple=self.min_gap_multiple,
            trail_window=self.trail_window,
            fast_trail_window=self.fast_trail_window,
            volume_decay_ratio=self.volume_decay_ratio,
            confirmation_samples=self.confirmation_samples,
            time_stop_seconds=self.time_stop_seconds,
        )


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
        survey_cache: Optional[SharedSurveyCache] = None,
    ) -> None:
        self.id = runner_id or str(uuid4())
        self.config = config
        self.instruments = list(instruments)
        self.market_data = market_data
        self.accounts = accounts
        self.coordinator = coordinator
        self.store = store
        self.survey_cache = survey_cache
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
        self._bar_aggregators: Dict[str, BarAggregator] = {}
        self._market_history: deque[float] = deque(maxlen=13)
        self._market_bases: dict[str, Optional[float]] = {
            "session_open": None,
            "recent_15m": None,
        }
        self._monitoring: list[dict] = []
        self._open: Dict[str, dict] = {}
        self._exits: Dict[str, RatchetExit] = {}
        self._volume_state: Dict[str, dict] = {}
        self._completed: set[str] = set()
        self._cooldown_until: Dict[str, datetime] = {}
        self._events: list[dict] = []
        self._errors: list[str] = []
        self._data_health = {
            "status": "WAITING",
            "requested": len(self.instruments),
            "analyzed": 0,
            "failures": 0,
            "rate_limited": 0,
            "cache_hit": False,
            "cache_age_seconds": None,
        }
        self._dirty = False
        self._flushed_observations = 0
        self._history_size = max(13, config.entry_bars)
        self._bar_capacity = max(20, config.entry_bars + 5)
        broker = accounts.get(config.account_id)
        self._initial_equity = broker.snapshot()["equity"]
        self._cost_model = cost_model(
            config.allocation_per_position,
            broker.fee_schedule,
            broker.slippage_bps,
            Product.INTRADAY,
        )

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
                "cost_model": self._cost_model,
                "decision_counts": _decision_counts(self._monitoring),
                "initial_equity": self._initial_equity,
                "scan_count": self._scan_count,
                "poll_count": self._poll_count,
                "candidates": list(self._candidates.values()),
                "open_positions": list(self._open.values()),
                "completed_instruments": sorted(self._completed),
                "events": list(self._events),
                "monitoring": list(self._monitoring),
                "errors": list(self._errors),
                "data_health": dict(self._data_health),
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
            result = (
                self.survey_cache.run(
                    self.market_data,
                    self.instruments,
                    self.config.minimum_relative_volume,
                )
                if self.survey_cache
                else MarketSurvey(
                    self.market_data, self.config.minimum_relative_volume
                ).run(self.instruments)
            )
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
            requested = result.get("requested", len(self.instruments))
            analyzed = result["analyzed"]
            rate_limited = sum(
                "429" in failure.get("error", "")
                or "rate limited" in failure.get("error", "").lower()
                for failure in result["failures"]
            )
            data_status = (
                "UNAVAILABLE"
                if analyzed == 0
                else "DEGRADED" if analyzed < requested else "OK"
            )
            cache = result.get("shared_cache", {})
            with self._lock:
                self._scan_count += 1
                self._candidates = {row["instrument_key"]: row for row in candidates}
                # Held positions drop out of the candidate list, but the trailing
                # stop still wants their latest participation reading.
                self._volume_state = {
                    row["instrument_key"]: {
                        key: row[key]
                        for key in (
                            "relative_volume",
                            "recent_volume",
                            "baseline_volume",
                            "volume_signal",
                            "momentum_score",
                            "session_change_pct",
                            "recent_15m_change_pct",
                            "range_position_pct",
                        )
                    }
                    for row in result["results"]
                }
                self._data_health = {
                    "status": data_status,
                    "requested": requested,
                    "analyzed": analyzed,
                    "failures": len(result["failures"]),
                    "rate_limited": rate_limited,
                    "cache_hit": bool(cache.get("hit", False)),
                    "cache_age_seconds": cache.get("age_seconds"),
                }
                self._record(
                    "SCAN",
                    {
                        "requested": requested,
                        "analyzed": analyzed,
                        "data_status": data_status,
                        "rate_limited": rate_limited,
                        "shared_cache": cache,
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
            history = self._history.setdefault(
                quote.instrument_key, deque(maxlen=self._history_size)
            )
            previous = history[-1] if history else None
            history.append(quote.last_price)
            if self.config.entry_timeframe_seconds > 0:
                self._bar_aggregators.setdefault(
                    key, BarAggregator(self.config.entry_timeframe_seconds, self._bar_capacity)
                ).add(quote.last_price, quote.timestamp)
            stats = {**self._volume_state.get(key, {}), **self._candidates.get(key, {})}
            entry_closes, entry_lows = self._entry_series(key)
            entry_check = evaluate_entry(
                entry_closes,
                self.config.entry_policy(),
                self._expected_cost_floor_pct(),
                lows=entry_lows,
            )
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
                "session_change_pct": stats.get("session_change_pct"),
                "recent_15m_change_pct": stats.get("recent_15m_change_pct"),
                "momentum_score": stats.get("momentum_score"),
                "range_position_pct": stats.get("range_position_pct"),
                "recent_volume": stats.get("recent_volume"),
                "baseline_volume": stats.get("baseline_volume"),
                "relative_volume": stats.get("relative_volume"),
                "volume_signal": stats.get("volume_signal"),
                "entry_check": entry_check.to_dict(),
                "held": key in self._open,
                "exit": None,
                **market_context,
                "decision": "WATCHING",
            }
            exit_result = self._consider_exit(quote, previous, stats.get("relative_volume"))
            if exit_result:
                observation["exit"] = exit_result["state"]
                if exit_result["reason"]:
                    observation["decision"] = f"EXIT_{exit_result['reason']}"
            observations[key] = observation
            self._monitoring.append(observation)
        self._consider_entries(quotes, observations)
        self._persist(force=False)

    def _entry_series(self, key: str) -> tuple:
        """Closes and lows for the entry window, in the configured timeframe.

        With `entry_timeframe_seconds` at its default of zero, this is the raw
        five-second tick stream and closes double as their own lows - identical
        to the behaviour before bar aggregation existed. Set it above zero to
        run the three-bar check on resampled OHLC bars instead, with the stop
        seeded from each bar's real intrabar low rather than its close.
        """

        if self.config.entry_timeframe_seconds > 0:
            bars = self._bar_aggregators.get(key)
            completed = bars.completed_bars if bars else []
            return [bar.close for bar in completed], [bar.low for bar in completed]
        prices = list(self._history.get(key) or ())
        return prices, prices

    def _expected_cost_floor_pct(self) -> float:
        """Cost of a round trip at the configured size, before a fill is known."""

        return max(
            self._cost_model["breakeven_pct"], self.config.minimum_cost_floor_pct
        )

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

    def _consider_exit(
        self,
        quote: Quote,
        previous: Optional[float],
        relative_volume: Optional[float] = None,
    ) -> Optional[dict]:
        state = self._open.get(quote.instrument_key)
        if not state:
            return None
        state["last_price"] = quote.last_price
        state["peak_price"] = max(state["peak_price"], quote.last_price)
        result = (
            self._ratchet_exit(quote, state, relative_volume)
            if self.config.exit_mode == "RATCHET"
            else self._reversal_exit(quote, state, previous)
        )
        state.update(
            {
                key: result["state"].get(key)
                for key in ("phase", "stop_price", "stop_source", "unrealized_pct")
            }
        )
        if result["reason"]:
            self._exit(quote, state, result["reason"], result["state"])
        return result

    def _ratchet_exit(
        self, quote: Quote, state: dict, relative_volume: Optional[float]
    ) -> dict:
        rule = self._exits.get(quote.instrument_key)
        if not rule:
            return self._reversal_exit(quote, state, None)
        diagnostics = rule.update(quote.last_price, quote.timestamp, relative_volume)
        return {"reason": diagnostics["reason"], "state": diagnostics}

    def _reversal_exit(self, quote: Quote, state: dict, previous: Optional[float]) -> dict:
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
        cost_floor = state.get("cost_floor_pct") or 0.0
        return {
            "reason": reason,
            "state": {
                "phase": "REVERSAL",
                "reason": reason,
                "entry_price": round(entry_price, 4),
                "last_price": round(quote.last_price, 4),
                "peak_price": round(state["peak_price"], 4),
                "stop_price": round(entry_price * (1 - self.config.hard_stop_pct / 100), 4),
                "stop_source": "FIXED_HARD_STOP",
                "unrealized_pct": round(pnl_pct, 4),
                "net_of_cost_pct": round(pnl_pct - cost_floor, 4),
                "cost_floor_pct": round(cost_floor, 4),
                "drawdown_from_peak_pct": round(drawdown, 4),
                "reversal_pct": self.config.reversal_pct,
                "hard_stop_pct": self.config.hard_stop_pct,
            },
        }

    def _lock_out(self, key: str, quote: Optional[Quote]) -> None:
        """Bar a stock from re-entry after an exit or a rejected fill.

        With no cooldown configured this is permanent (the short-run default:
        trade each stock once). With a cooldown, the stock reopens once that many
        seconds have passed, so a full-session run keeps finding trades instead of
        exhausting the universe by mid-morning.
        """

        self._completed.add(key)
        if self.config.reentry_cooldown_seconds > 0 and quote is not None:
            self._cooldown_until[key] = quote.timestamp + timedelta(
                seconds=self.config.reentry_cooldown_seconds
            )

    def _cooldown_elapsed(self, key: str, quote: Optional[Quote]) -> bool:
        """True once a cooled-down stock may trade again; releases it if so."""

        if self.config.reentry_cooldown_seconds <= 0:
            return False  # permanent lockout
        ready_at = self._cooldown_until.get(key)
        if ready_at is None or quote is None or quote.timestamp < ready_at:
            return False
        self._completed.discard(key)
        self._cooldown_until.pop(key, None)
        return True

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
            quote = quotes.get(key)
            if key in self._open:
                if observation:
                    observation["decision"] = "HOLDING_POSITION"
                continue
            if key in self._completed and not self._cooldown_elapsed(key, quote):
                if observation:
                    observation["decision"] = (
                        "IN_COOLDOWN"
                        if self.config.reentry_cooldown_seconds > 0
                        else "ALREADY_TRADED"
                    )
                continue
            if slots <= 0:
                if observation:
                    observation["decision"] = "MAX_POSITIONS_REACHED"
                continue
            history = self._history.get(key)
            entry_closes, entry_lows = self._entry_series(key)
            ready_length = (
                len(entry_closes) if self.config.entry_mode == "THREE_BAR" else len(history or [])
            )
            if not history or ready_length < self.config.entry_bars or not quote:
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
            # The index moves an order of magnitude less than a single stock, so a
            # small negative NIFTY print is noise rather than a reason to stand
            # aside. It is recorded on every observation either way; set
            # require_nifty_confirmation to make it a gate again.
            if self.config.require_nifty_confirmation and (
                not observation
                or observation.get("nifty_window_change_pct") is None
                or observation["nifty_window_change_pct"] <= 0
                or observation.get("nifty_recent_15m_change_pct") is None
                or observation["nifty_recent_15m_change_pct"] <= 0
            ):
                if observation:
                    observation["decision"] = "NIFTY_SHORT_TERM_NOT_POSITIVE"
                continue
            evaluation = evaluate_entry(
                entry_closes,
                self.config.entry_policy(),
                self._expected_cost_floor_pct(),
                lows=entry_lows,
            )
            if self.config.entry_mode == "THREE_BAR":
                if not evaluation.triggered:
                    if observation:
                        observation["decision"] = evaluation.reason
                    continue
                window_change = evaluation.rise_pct
                structural_stop = evaluation.trigger_price
            else:
                window_change = (history[-1] / history[0] - 1) * 100
                if window_change < self.config.entry_momentum_pct:
                    if observation:
                        observation["decision"] = "BELOW_ENTRY_THRESHOLD"
                    continue
                if history[-1] <= history[-2]:
                    if observation:
                        observation["decision"] = "LATEST_SAMPLE_NOT_RISING"
                    continue
                structural_stop = min(history)
            quantity = floor(self.config.allocation_per_position / quote.last_price)
            if quantity <= 0:
                if observation:
                    observation["decision"] = "ALLOCATION_TOO_SMALL"
                continue
            entry_signal = {
                "reason": evaluation.reason if self.config.entry_mode == "THREE_BAR"
                else "UPWARD_MOVEMENT_CONFIRMED",
                "entry_mode": self.config.entry_mode,
                "entry_check": evaluation.to_dict(),
                "structural_stop": round(structural_stop, 4),
                "window_start_price": round(history[0], 4),
                "observed_price": round(quote.last_price, 4),
                "window_change_pct": round(window_change, 4),
                "entry_threshold_pct": evaluation.threshold_pct,
                "entry_threshold_source": evaluation.threshold_source,
                "stock_noise_pct": evaluation.noise_pct,
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
                self._lock_out(key, quote)
                continue
            notional = position.quantity * position.average_price
            cost_floor_pct = max(
                breakeven_pct(
                    notional, broker.fee_schedule, broker.slippage_bps, Product.INTRADAY
                ),
                self.config.minimum_cost_floor_pct,
            )
            rule = RatchetExit(
                position.average_price,
                cost_floor_pct,
                quote.timestamp,
                self.config.exit_policy(),
                structural_stop,
            )
            self._exits[key] = rule
            self._open[key] = {
                "instrument_key": key,
                "symbol": candidate["symbol"],
                "quantity": position.quantity,
                "entry_price": position.average_price,
                "peak_price": quote.last_price,
                "last_price": quote.last_price,
                "entry_momentum_pct": round(window_change, 4),
                "notional": round(notional, 2),
                "cost_floor_pct": round(cost_floor_pct, 4),
                "structural_stop": round(structural_stop, 4),
                "stop_price": round(rule.stop_price, 4),
                "stop_source": rule.stop_source,
                "phase": rule.phase.value,
                "unrealized_pct": 0.0,
            }
            entry_signal.update(
                {
                    "notional": round(notional, 2),
                    "cost_floor_pct": round(cost_floor_pct, 4),
                    "initial_stop": round(rule.stop_price, 4),
                    "initial_stop_source": rule.stop_source,
                    "risk_pct": round(
                        (position.average_price / rule.stop_price - 1) * 100, 4
                    ),
                    "lock_at_pct": round(cost_floor_pct * self.config.lock_multiple, 4),
                    "ride_at_pct": round(cost_floor_pct * self.config.ride_multiple, 4),
                }
            )
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

    def _exit(
        self,
        quote: Quote,
        state: dict,
        reason: str,
        diagnostics: Optional[dict] = None,
    ) -> None:
        broker = self.accounts.get(self.config.account_id)
        position = broker.positions.get(quote.instrument_key)
        if not position or position.quantity <= 0:
            self._open.pop(quote.instrument_key, None)
            self._exits.pop(quote.instrument_key, None)
            return
        order = self._submit(quote.instrument_key, Side.SELL, position.quantity, quote)
        self._record(
            "EXIT_FILLED" if order.filled_quantity else "EXIT_REJECTED",
            {
                "symbol": state["symbol"],
                "reason": reason,
                "observed_price": quote.last_price,
                "entry_price": state["entry_price"],
                "exit_state": diagnostics,
                "order": order.to_dict(),
            },
        )
        if order.filled_quantity:
            self._open.pop(quote.instrument_key, None)
            self._exits.pop(quote.instrument_key, None)
            self._lock_out(quote.instrument_key, quote)

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
            rule = self._exits.get(key)
            self._exit(quote, state, "SESSION_END", rule.to_dict() if rule else None)

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
        self._dirty = True

    def _error(self, error: Exception) -> None:
        with self._lock:
            message = str(error)[:500]
            self._errors.append(message)
            self._record("ERROR", {"message": message})

    def _flush_observations(self) -> None:
        """Append any monitoring rows not yet written, oldest first."""

        if not self.store:
            return
        with self._lock:
            pending = self._monitoring[self._flushed_observations :]
            if not pending:
                return
            batch = list(pending)
            self._flushed_observations += len(batch)
        self.store.add_observations(self.id, batch)

    def _persist(self, force: bool = True) -> None:
        """Persist the run summary, and stream observations into their own table.

        The trace is the raw material for later backtests, so every row is
        written as its own record rather than re-serialised inside the run
        payload - a growing blob rewritten each poll is quadratic, and a blob
        cannot be queried by symbol or by day.
        """

        if not self.store:
            return
        if not force and not self._dirty and self._poll_count % 12:
            return
        self._dirty = False
        self._flush_observations()
        self.store.save_momentum_run(self._persistable_snapshot())

    def _persistable_snapshot(self) -> dict:
        """The run summary without the trace, which now lives in its own table."""

        payload = self.snapshot()
        payload["monitoring_count"] = len(payload.get("monitoring") or [])
        payload["monitoring"] = []
        payload["monitoring_stored"] = True
        return payload

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _percent_change(current: float, base: Optional[float]) -> float:
    return (current / base - 1) * 100 if base else 0.0


def _without_trace(snapshot: dict) -> dict:
    monitoring = snapshot.get("monitoring") or []
    return {
        **snapshot,
        "monitoring": [],
        "monitoring_count": snapshot.get("monitoring_count", len(monitoring)),
    }


def _decision_counts(monitoring: list[dict]) -> list[dict]:
    """Which gate each observation stopped at, so a run explains its own inaction."""

    counts: Dict[str, int] = {}
    for observation in monitoring:
        decision = observation.get("decision", "UNKNOWN")
        counts[decision] = counts.get(decision, 0) + 1
    total = sum(counts.values())
    return [
        {
            "decision": decision,
            "count": count,
            "share_pct": round(count / total * 100, 2) if total else 0.0,
        }
        for decision, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


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
        """Run summaries only. A trace can run to thousands of rows, so callers
        that want one ask for that run by id."""

        with self._lock:
            live = {
                runner.id: _without_trace(runner.snapshot())
                for runner in self._runners.values()
            }
        persisted = self._store.momentum_runs() if self._store else []
        combined = list(live.values()) + [
            _without_trace(item) for item in persisted if item["id"] not in live
        ]
        return sorted(combined, key=lambda item: item.get("started_at") or "", reverse=True)

    def get(self, runner_id: str, include_monitoring: bool = True) -> dict:
        with self._lock:
            runner = self._runners.get(runner_id)
        snapshot = runner.snapshot() if runner else None
        if snapshot is None:
            snapshot = self._store.momentum_run(runner_id) if self._store else None
        if snapshot is None:
            raise KeyError(runner_id)
        if not include_monitoring:
            return _without_trace(snapshot)
        if not snapshot.get("monitoring") and self._store:
            # Runs recorded since observations moved into their own table keep an
            # empty list in the payload; older runs still carry theirs inline.
            snapshot["monitoring"] = self._store.observations(run_id=runner_id)
        snapshot["monitoring_count"] = len(snapshot.get("monitoring") or [])
        return snapshot

    def active(self) -> list[dict]:
        with self._lock:
            return [
                _without_trace(runner.snapshot())
                for runner in self._runners.values()
                if runner.snapshot()["status"] in ACTIVE_RUN_STATUSES
            ]

    def stop(self, runner_id: str) -> dict:
        with self._lock:
            return self._runners[runner_id].stop()

    def has_active(self, account_id: str) -> bool:
        """One run per paper account: concurrent runners would share cash and
        positions. Different accounts may run side by side."""

        with self._lock:
            return any(
                runner.config.account_id == account_id
                and runner.snapshot()["status"] in ACTIVE_RUN_STATUSES
                for runner in self._runners.values()
            )

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop()
