from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .domain import Order, OrderType, Product, Quote, Side
from .market_data import Candle
from .paper_broker import FeeSchedule, PaperBroker, RiskLimits
from .research_store import ResearchStore
from .strategy_engine import ProfitTargetLeg, ProfitTargetRule


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
