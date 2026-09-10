from __future__ import annotations

import csv
import io
from typing import Iterable, List


def build_comparison(experiment: dict, runs: Iterable[dict]) -> dict:
    rows = [_variant_metrics(run) for run in runs]
    hashes = {run.get("shared_config_hash") for run in runs}
    complete = (
        len(rows) == 3
        and len(hashes) == 1
        and None not in hashes
        and all(row["status"] == "COMPLETED" for row in rows)
    )
    return {
        "schema_version": "entry-comparison.v1",
        "experiment_id": experiment["experiment_id"],
        "session_id": experiment.get("session_id"),
        "shared_config_hash": experiment.get("shared_config_hash"),
        "comparison_complete": complete,
        "comparison_status": "COMPLETE" if complete else "INCOMPLETE",
        "resolved_config": experiment.get("resolved_config"),
        "variants": sorted(rows, key=lambda row: row.get("variant_label") or ""),
        "interpretation": (
            "Paper-research comparison of entry timing under one shared exit policy; "
            "it is not evidence of profitability or a trading recommendation."
        ),
    }


def export_csv(kind: str, runs: List[dict]) -> str:
    builders = {
        "trades": _trade_rows,
        "signals": _signal_rows,
        "exits": _exit_rows,
        "equity": _equity_rows,
    }
    if kind not in builders:
        raise ValueError("export kind must be trades, signals, exits, or equity")
    rows = builders[kind](runs)
    columns = sorted({key for row in rows for key in row}) or ["experiment_id"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _variant_metrics(run: dict) -> dict:
    fills = run.get("fills", [])
    events = run.get("events", [])
    entries = [event for event in events if event.get("type") == "ENTRY_FILLED"]
    exits = [event for event in events if event.get("type") == "EXIT_FILLED"]
    setup_events = [event for event in events if event.get("type", "").startswith("SETUP_")]
    decisions = [event for event in events if event.get("type") == "ENTRY_SIGNAL_EVALUATED"]
    net = float(run.get("session_pnl", 0.0))
    starting = float(run.get("initial_equity") or run.get("portfolio", {}).get("initial_cash", 0))
    trade_results = []
    for event in exits:
        state = event.get("exit_state") or {}
        trade_results.append(float(state.get("net_of_cost_pct") or 0.0))
    wins = sum(value > 0 for value in trade_results)
    losses = sum(value < 0 for value in trade_results)
    gross_wins = sum(value for value in trade_results if value > 0)
    gross_losses = abs(sum(value for value in trade_results if value < 0))
    return {
        "run_id": run.get("id"),
        "variant_label": run.get("variant_label") or run.get("config", {}).get("variant_label"),
        "entry_mode": run.get("config", {}).get("signal_strategy"),
        "status": run.get("status"),
        "starting_capital": starting,
        "ending_equity": run.get("portfolio", {}).get("equity"),
        "net_pnl": net,
        "return_fraction": net / starting if starting else None,
        "fees": run.get("metrics", {}).get("fees", 0.0),
        "evaluated_bars": len(decisions),
        "raw_qualified_signals": sum(
            bool(event.get("signal", {}).get("raw_qualified")) for event in decisions
        ),
        "setups": sum(event.get("type") == "SETUP_ARMED" for event in setup_events),
        "setup_transitions": len(setup_events),
        "filled_trades": len(entries),
        "closed_round_trips": len(exits),
        "open_trades": len(run.get("open_positions", [])),
        "wins": wins,
        "losses": losses,
        "flats": len(trade_results) - wins - losses,
        "win_rate": wins / len(trade_results) if trade_results else None,
        "expectancy_pct": sum(trade_results) / len(trade_results) if trade_results else None,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "profit_factor_status": "INFINITY" if gross_wins and not gross_losses else "FINITE",
        "fill_count": len(fills),
    }


def _base(run: dict) -> dict:
    return {
        "experiment_id": run.get("experiment_id"),
        "run_id": run.get("id"),
        "variant": run.get("variant_label"),
        "entry_mode": run.get("config", {}).get("signal_strategy"),
    }


def _trade_rows(runs: List[dict]) -> List[dict]:
    return [
        {
            **_base(run),
            **{key: fill.get(key) for key in ("id", "order_id", "symbol", "side", "quantity", "price", "fees", "timestamp")},
        }
        for run in runs
        for fill in run.get("fills", [])
    ]


def _signal_rows(runs: List[dict]) -> List[dict]:
    return [
        {
            **_base(run),
            "timestamp": event.get("timestamp"),
            "event_type": event.get("type"),
            "symbol": event.get("symbol"),
            "reason": (event.get("signal") or {}).get("reason") or event.get("reason"),
            "raw_qualified": (event.get("signal") or {}).get("raw_qualified"),
        }
        for run in runs
        for event in run.get("events", [])
        if event.get("type") in {"ENTRY_SIGNAL_EVALUATED", "SETUP_ARMED", "SETUP_TRANSITION"}
    ]


def _exit_rows(runs: List[dict]) -> List[dict]:
    return [
        {
            **_base(run),
            "timestamp": event.get("timestamp"),
            "event_type": event.get("type"),
            "symbol": event.get("symbol"),
            "reason": event.get("reason") or (event.get("signal") or {}).get("reason"),
            "residual_quantity": event.get("residual_quantity"),
        }
        for run in runs
        for event in run.get("events", [])
        if event.get("type")
        in {"EXIT_SIGNAL_EVALUATED", "MOMENTUM_EXIT_LATCHED", "EXIT_FILLED", "EXIT_PARTIAL", "EXIT_REJECTED"}
    ]


def _equity_rows(runs: List[dict]) -> List[dict]:
    return [
        {
            **_base(run),
            "timestamp": run.get("finished_at") or run.get("started_at"),
            "starting_equity": run.get("initial_equity"),
            "ending_equity": run.get("portfolio", {}).get("equity"),
            "net_pnl": run.get("session_pnl"),
            "status": run.get("status"),
        }
        for run in runs
    ]

