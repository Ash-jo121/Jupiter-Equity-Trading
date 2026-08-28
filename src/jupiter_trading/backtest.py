from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import floor
from typing import Dict, List, Optional
from uuid import uuid4

from .domain import Order, OrderType, Product, Quote, Side
from .market_data import Candle
from .paper_broker import FeeSchedule, PaperBroker, RiskLimits
from .research_store import ResearchStore
from .strategy_engine import ProfitTargetLeg, ProfitTargetRule
from .trade_rules import (
    BarAggregator,
    EntryPolicy,
    ExitPolicy,
    RatchetExit,
    breakeven_pct,
    evaluate_entry,
)


class BacktestEngine:
    """Close-price replay for the same profit-target/stop-loss rule used live."""

    def __init__(self, store: ResearchStore, fee_schedule: FeeSchedule) -> None:
        self.store = store
        self.fee_schedule = fee_schedule

    def run(
        self,
        instrument_key: str,
        symbol: str,
        candles: List[Candle],
        quantity: int,
        initial_cash: float,
        entry_price: Optional[float],
        profit_target_pct: float,
        stop_loss_pct: float,
        absolute_profit_target: Optional[float] = None,
    ) -> dict:
        if not candles:
            raise ValueError("backtest requires at least one candle")
        leg = ProfitTargetLeg(
            instrument_key=instrument_key,
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            absolute_profit_target=absolute_profit_target,
        )
        broker = PaperBroker(
            initial_cash=initial_cash,
            slippage_bps=0,
            fee_schedule=self.fee_schedule,
            risk_limits=RiskLimits(
                max_order_notional=max(initial_cash, 250_000),
                max_position_notional=max(initial_cash, 500_000),
                max_daily_loss=initial_cash,
            ),
        )
        equity_curve = []
        exit_reasons = []
        entered = False
        completed = False

        for candle in candles:
            quote = Quote(instrument_key, candle.close, timestamp=candle.timestamp)
            broker.on_quote(quote)
            position = broker.positions.get(instrument_key)
            current_quantity = position.quantity if position else 0
            if not entered and ProfitTargetRule.entry_reached(candle.close, entry_price):
                broker.submit(
                    Order(
                        instrument_key,
                        Side.BUY,
                        quantity,
                        OrderType.MARKET,
                        strategy_id="backtest-profit-target",
                        product=Product.DELIVERY,
                    )
                )
                entered = True
            elif entered and not completed and current_quantity > 0:
                reason = ProfitTargetRule.exit_reason(candle.close, position.average_price, leg)
                if reason:
                    broker.submit(
                        Order(
                            instrument_key,
                            Side.SELL,
                            current_quantity,
                            OrderType.MARKET,
                            strategy_id="backtest-profit-target",
                            product=Product.DELIVERY,
                        )
                    )
                    completed = True
                    exit_reasons.append(reason)
            snapshot = broker.snapshot()
            equity_curve.append(
                {"timestamp": candle.timestamp.isoformat(), "equity": snapshot["equity"]}
            )

        position = broker.positions.get(instrument_key)
        if position and position.quantity > 0:
            last = candles[-1]
            broker.on_quote(Quote(instrument_key, last.close, timestamp=last.timestamp))
            broker.submit(
                Order(
                    instrument_key,
                    Side.SELL,
                    position.quantity,
                    OrderType.MARKET,
                    strategy_id="backtest-profit-target",
                    product=Product.DELIVERY,
                )
            )
            exit_reasons.append("END_OF_DATA")
            equity_curve[-1]["equity"] = broker.snapshot()["equity"]

        snapshot = broker.snapshot()
        equities = [point["equity"] for point in equity_curve]
        peak = equities[0]
        max_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        buys = [fill for fill in broker.fills if fill.side == Side.BUY]
        sells = [fill for fill in broker.fills if fill.side == Side.SELL]
        gross_pnl = sum(fill.gross_value for fill in sells) - sum(
            fill.gross_value for fill in buys
        )
        net_pnl = snapshot["equity"] - initial_cash
        report = {
            "id": str(uuid4()),
            "strategy_type": "PROFIT_TARGET",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "instrument_key": instrument_key,
            "symbol": symbol,
            "parameters": {
                "quantity": quantity,
                "initial_cash": initial_cash,
                "entry_price": entry_price,
                "profit_target_pct": profit_target_pct,
                "stop_loss_pct": stop_loss_pct,
                "absolute_profit_target": absolute_profit_target,
                "execution_model": "candle_close",
            },
            "period": {
                "from": candles[0].timestamp.isoformat(),
                "to": candles[-1].timestamp.isoformat(),
                "candles": len(candles),
            },
            "metrics": {
                "initial_cash": initial_cash,
                "final_equity": snapshot["equity"],
                "gross_pnl": round(gross_pnl, 2),
                "net_pnl": round(net_pnl, 2),
                "return_pct": round(net_pnl / initial_cash * 100, 4),
                "fees": snapshot["fees_paid"],
                "max_drawdown_pct": round(max_drawdown, 4),
                "round_trips": min(len(buys), len(sells)),
                "win_rate_pct": 100.0 if net_pnl > 0 and sells else 0.0,
            },
            "exit_reasons": exit_reasons,
            "fills": [fill.to_dict() for fill in broker.fills],
            "equity_curve": equity_curve,
        }
        self.store.save_backtest(report)
        return report


@dataclass(frozen=True)
class ReplayGates:
    """The entry filters a FULL replay re-applies from the recorded observations."""

    minimum_relative_volume: float = 1.2
    require_positive_nifty: bool = False
    max_positions: int = 2
    allocation_per_position: float = 25_000.0
    one_trade_per_symbol: bool = True
    minimum_cost_floor_pct: float = 0.01
    entry_timeframe_seconds: float = 0.0


class ExitReplayEngine:
    """Replays a recorded run's five-second monitoring trace under another rule.

    EXITS_ONLY keeps exactly the entries the live run took and swaps only the
    exit rule, which isolates the exit change from everything else. FULL also
    re-evaluates the entry rule and the gates against the same observations, so
    a candidate strategy can be scored end to end without waiting for a session.

    Both paths route orders through a real PaperBroker, so replayed fees and
    slippage are the same numbers the live account would have paid.
    """

    def __init__(self, store: ResearchStore, fee_schedule: FeeSchedule) -> None:
        self.store = store
        self.fee_schedule = fee_schedule

    def run(
        self,
        source: dict,
        exit_policy: Optional[ExitPolicy] = None,
        entry_policy: Optional[EntryPolicy] = None,
        mode: str = "EXITS_ONLY",
        gates: Optional[ReplayGates] = None,
        slippage_bps: float = 2.0,
        persist: bool = True,
    ) -> dict:
        if mode not in {"EXITS_ONLY", "FULL"}:
            raise ValueError("mode must be EXITS_ONLY or FULL")
        observations = sorted(
            source.get("monitoring") or [],
            key=lambda row: (row["timestamp"], row["symbol"]),
        )
        if not observations:
            raise ValueError("this run recorded no monitoring trace to replay")
        config = source.get("config") or {}
        exits = exit_policy or ExitPolicy()
        entries = entry_policy or EntryPolicy(
            bars=config.get("entry_bars", 3),
            minimum_rise_pct=config.get("entry_momentum_pct", 0.10),
        )
        limits = gates or ReplayGates(
            minimum_relative_volume=config.get("minimum_relative_volume", 1.2),
            max_positions=config.get("max_positions", 2),
            allocation_per_position=config.get("allocation_per_position", 25_000.0),
        )
        initial_cash = float(
            source.get("initial_equity")
            or (source.get("portfolio") or {}).get("initial_cash")
            or 100_000
        )
        broker = PaperBroker(
            initial_cash=initial_cash,
            slippage_bps=slippage_bps,
            fee_schedule=self.fee_schedule,
            risk_limits=RiskLimits(
                max_order_notional=max(initial_cash, 250_000),
                max_position_notional=max(initial_cash, 500_000),
                max_daily_loss=initial_cash,
            ),
            account_id="exit-replay",
        )
        forced = {
            (row["symbol"], row["timestamp"])
            for row in observations
            if row.get("decision") == "ENTRY_FILLED"
        }

        history: Dict[str, deque] = {}
        aggregators: Dict[str, BarAggregator] = {}
        open_trades: Dict[str, dict] = {}
        traded: set = set()
        trades: List[dict] = []
        decisions: Dict[str, int] = {}

        def entry_series(symbol: str) -> tuple:
            """Closes and lows in the configured timeframe, as the runner sees them."""

            if limits.entry_timeframe_seconds > 0:
                bars = aggregators.get(symbol)
                completed = bars.completed_bars if bars else []
                return [bar.close for bar in completed], [bar.low for bar in completed]
            prices = list(history[symbol])
            return prices, prices

        def note(reason: str) -> None:
            decisions[reason] = decisions.get(reason, 0) + 1

        for row in observations:
            symbol, key, price = row["symbol"], row["instrument_key"], row["price"]
            timestamp = _parse(row["timestamp"])
            window = history.setdefault(
                symbol, deque(maxlen=max(13, entries.bars))
            )
            window.append(price)
            if limits.entry_timeframe_seconds > 0:
                aggregators.setdefault(
                    symbol,
                    BarAggregator(
                        limits.entry_timeframe_seconds, max(20, entries.bars + 5)
                    ),
                ).add(price, timestamp)
            quote = Quote(key, price, timestamp=timestamp)
            broker.on_quote(quote)

            trade = open_trades.get(symbol)
            if trade:
                state = trade["rule"].update(price, timestamp, row.get("relative_volume"))
                trade["path"].append(_path_point(row, state))
                note(f"EXIT_{state['reason']}" if state["reason"] else "HOLDING_POSITION")
                if state["reason"]:
                    self._close(broker, trade, quote, state["reason"], state, trades)
                    open_trades.pop(symbol, None)
                continue

            if limits.one_trade_per_symbol and symbol in traded:
                note("ALREADY_TRADED")
                continue
            if len(open_trades) >= limits.max_positions:
                note("MAX_POSITIONS_REACHED")
                continue

            entry_closes, entry_lows = entry_series(symbol)
            evaluation = evaluate_entry(
                entry_closes,
                entries,
                max(
                    breakeven_pct(
                        limits.allocation_per_position,
                        self.fee_schedule,
                        slippage_bps,
                        Product.INTRADAY,
                    ),
                    limits.minimum_cost_floor_pct,
                ),
                lows=entry_lows,
            )
            if mode == "EXITS_ONLY":
                if (symbol, row["timestamp"]) not in forced:
                    note("NOT_A_RECORDED_ENTRY")
                    continue
            else:
                reason = _gate_reason(row, limits)
                if reason:
                    note(reason)
                    continue
                if not evaluation.triggered:
                    note(evaluation.reason)
                    continue

            quantity = floor(limits.allocation_per_position / price)
            if quantity <= 0:
                note("ALLOCATION_TOO_SMALL")
                continue
            order = broker.submit(
                Order(
                    key,
                    Side.BUY,
                    quantity,
                    OrderType.MARKET,
                    strategy_id="exit-replay",
                    product=Product.INTRADAY,
                    account_id="exit-replay",
                )
            )
            position = broker.positions.get(key)
            if not order.filled_quantity or not position or position.quantity <= 0:
                note("ENTRY_REJECTED")
                continue
            notional = position.quantity * position.average_price
            floor_pct = max(
                breakeven_pct(notional, self.fee_schedule, slippage_bps, Product.INTRADAY),
                limits.minimum_cost_floor_pct,
            )
            structural = evaluation.trigger_price or min(entry_lows or window)
            rule = RatchetExit(
                position.average_price, floor_pct, timestamp, exits, structural
            )
            open_trades[symbol] = {
                "symbol": symbol,
                "instrument_key": key,
                "quantity": position.quantity,
                "entry_price": position.average_price,
                "entry_time": row["timestamp"],
                "observed_entry_price": price,
                "cost_floor_pct": round(floor_pct, 4),
                "structural_stop": round(structural, 4),
                "initial_stop": round(rule.stop_price, 4),
                "initial_stop_source": rule.stop_source,
                "entry_check": evaluation.to_dict(),
                "rule": rule,
                "path": [],
            }
            traded.add(symbol)
            note("ENTRY_FILLED")

        for symbol, trade in list(open_trades.items()):
            last = history[symbol][-1]
            quote = Quote(trade["instrument_key"], last)
            broker.on_quote(quote)
            self._close(broker, trade, quote, "END_OF_DATA", trade["rule"].to_dict(), trades)

        candidate = _summarise(trades, broker)
        baseline = _baseline(source)
        report = {
            "id": str(uuid4()),
            "strategy_type": "EXIT_REPLAY",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_run_id": source.get("id"),
            "mode": mode,
            "exit_policy": asdict(exits),
            "entry_policy": asdict(entries),
            "gates": asdict(limits),
            "slippage_bps": slippage_bps,
            "period": {
                "from": observations[0]["timestamp"],
                "to": observations[-1]["timestamp"],
                "observations": len(observations),
                "symbols": len({row["symbol"] for row in observations}),
            },
            "baseline": baseline,
            "candidate": candidate,
            "delta": {
                "net_pnl": round(candidate["net_pnl"] - baseline["net_pnl"], 2),
                "round_trips": candidate["round_trips"] - baseline["round_trips"],
                "fees": round(candidate["fees"] - baseline["fees"], 2),
            },
            "trades": trades,
            "decision_counts": [
                {"decision": decision, "count": count}
                for decision, count in sorted(decisions.items(), key=lambda item: -item[1])
            ],
        }
        if persist:
            self.store.save_backtest(report)
        return report

    def replay_session(
        self,
        session_date: str,
        exit_policy: Optional[ExitPolicy] = None,
        entry_policy: Optional[EntryPolicy] = None,
        gates: Optional[ReplayGates] = None,
        slippage_bps: float = 2.0,
        initial_cash: float = 100_000.0,
        symbols: Optional[List[str]] = None,
        persist: bool = True,
    ) -> dict:
        """Replay every tick recorded on one trading day, across all runs.

        A single run only sees the stocks it happened to shortlist while it was
        alive. Pooling a whole session's observations gives the rule far more
        signals to be judged on, which is the point of storing ticks per day
        rather than per run.
        """

        observations = self.store.observations(session_date=session_date)
        if symbols:
            wanted = set(symbols)
            observations = [row for row in observations if row["symbol"] in wanted]
        if not observations:
            raise ValueError(f"no observations recorded for {session_date}")
        # The same instrument can appear in several overlapping runs; keep one
        # tick per instrument per timestamp so a stock is not double counted.
        deduplicated = {}
        for row in observations:
            deduplicated[(row["instrument_key"], row["timestamp"])] = row
        source = {
            "id": f"session-{session_date}",
            "initial_equity": initial_cash,
            "monitoring": sorted(
                deduplicated.values(), key=lambda row: (row["timestamp"], row["symbol"])
            ),
            "config": {
                "allocation_per_position": (gates or ReplayGates()).allocation_per_position,
                "max_positions": (gates or ReplayGates()).max_positions,
            },
            "fills": [],
            "events": [],
        }
        report = self.run(
            source,
            exit_policy=exit_policy,
            entry_policy=entry_policy,
            mode="FULL",
            gates=gates,
            slippage_bps=slippage_bps,
            persist=False,
        )
        report["strategy_type"] = "SESSION_REPLAY"
        report["session_date"] = session_date
        report["source_run_id"] = None
        report["deduplicated_observations"] = len(source["monitoring"])
        report["raw_observations"] = len(observations)
        if persist:
            self.store.save_backtest(report)
        return report

    def _close(
        self,
        broker: PaperBroker,
        trade: dict,
        quote: Quote,
        reason: str,
        state: dict,
        trades: List[dict],
    ) -> None:
        position = broker.positions.get(trade["instrument_key"])
        quantity = position.quantity if position else 0
        if quantity > 0:
            broker.submit(
                Order(
                    trade["instrument_key"],
                    Side.SELL,
                    quantity,
                    OrderType.MARKET,
                    strategy_id="exit-replay",
                    product=Product.INTRADAY,
                    account_id="exit-replay",
                )
            )
        fills = [
            fill for fill in broker.fills if fill.instrument_key == trade["instrument_key"]
        ]
        buys = [fill for fill in fills if fill.side == Side.BUY]
        sells = [fill for fill in fills if fill.side == Side.SELL]
        gross = sum(fill.gross_value for fill in sells) - sum(
            fill.gross_value for fill in buys
        )
        fees = sum(fill.fees for fill in fills)
        record = {
            key: value for key, value in trade.items() if key not in {"rule", "path"}
        }
        record.update(
            {
                "exit_reason": reason,
                "exit_price": round(quote.last_price, 4),
                "exit_state": state,
                "gross_pnl": round(gross, 2),
                "fees": round(fees, 2),
                "net_pnl": round(gross - fees, 2),
                "seconds_held": state.get("seconds_held"),
                "samples": len(trade["path"]),
                "path": trade["path"],
            }
        )
        trades.append(record)


def _gate_reason(row: dict, limits: ReplayGates) -> Optional[str]:
    volume = row.get("relative_volume")
    if volume is None or volume < limits.minimum_relative_volume:
        return "RELATIVE_VOLUME_TOO_LOW"
    if limits.require_positive_nifty:
        rolling = row.get("nifty_window_change_pct")
        recent = row.get("nifty_recent_15m_change_pct")
        if rolling is None or rolling <= 0 or recent is None or recent <= 0:
            return "NIFTY_SHORT_TERM_NOT_POSITIVE"
    return None


def _path_point(row: dict, state: dict) -> dict:
    return {
        "timestamp": row["timestamp"],
        "price": row["price"],
        "stop_price": state["stop_price"],
        "phase": state["phase"],
        "stop_source": state["stop_source"],
        "unrealized_pct": state["unrealized_pct"],
        "net_of_cost_pct": state["net_of_cost_pct"],
        "relative_volume": row.get("relative_volume"),
        "volume_state": state["volume_state"],
    }


def _summarise(trades: List[dict], broker: PaperBroker) -> dict:
    wins = [trade for trade in trades if trade["net_pnl"] > 0]
    losses = [trade for trade in trades if trade["net_pnl"] <= 0]
    holds = [trade["seconds_held"] for trade in trades if trade["seconds_held"]]
    reasons: Dict[str, int] = {}
    for trade in trades:
        reasons[trade["exit_reason"]] = reasons.get(trade["exit_reason"], 0) + 1
    return {
        "net_pnl": round(sum(trade["net_pnl"] for trade in trades), 2),
        "gross_pnl": round(sum(trade["gross_pnl"] for trade in trades), 2),
        "fees": round(sum(trade["fees"] for trade in trades), 2),
        "round_trips": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "average_win": round(sum(t["net_pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "average_loss": round(
            sum(t["net_pnl"] for t in losses) / len(losses), 2
        ) if losses else 0.0,
        "best_trade": round(max((t["net_pnl"] for t in trades), default=0.0), 2),
        "worst_trade": round(min((t["net_pnl"] for t in trades), default=0.0), 2),
        "average_hold_seconds": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "exit_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda item: -item[1])
        ],
        "final_equity": broker.snapshot()["equity"],
    }


def _baseline(source: dict) -> dict:
    """What the recorded run actually achieved, rebuilt from its own fills."""

    grouped: Dict[str, List[dict]] = {}
    for fill in source.get("fills") or []:
        grouped.setdefault(fill["symbol"], []).append(fill)
    trades = []
    reasons = {
        event.get("symbol"): event.get("reason")
        for event in source.get("events") or []
        if event.get("type") == "EXIT_FILLED"
    }
    for symbol, fills in grouped.items():
        buys = [fill for fill in fills if fill["side"] == Side.BUY.value]
        sells = [fill for fill in fills if fill["side"] == Side.SELL.value]
        gross = sum(fill["gross_value"] for fill in sells) - sum(
            fill["gross_value"] for fill in buys
        )
        fees = sum(fill["fees"] for fill in fills)
        trades.append(
            {
                "symbol": symbol,
                "gross_pnl": round(gross, 2),
                "fees": round(fees, 2),
                "net_pnl": round(gross - fees, 2),
                "exit_reason": reasons.get(symbol, "OPEN"),
                "seconds_held": None,
            }
        )
    wins = [trade for trade in trades if trade["net_pnl"] > 0]
    counted: Dict[str, int] = {}
    for trade in trades:
        counted[trade["exit_reason"]] = counted.get(trade["exit_reason"], 0) + 1
    return {
        "net_pnl": round(sum(trade["net_pnl"] for trade in trades), 2),
        "gross_pnl": round(sum(trade["gross_pnl"] for trade in trades), 2),
        "fees": round(sum(trade["fees"] for trade in trades), 2),
        "round_trips": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "exit_reasons": [
            {"reason": reason, "count": count} for reason, count in counted.items()
        ],
        "trades": trades,
    }


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
