from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from .research_store import IST, ResearchStore, _session_date


def _run_session_date(run: dict) -> Optional[str]:
    started = run.get("started_at")
    return _session_date(started) if started else None


def _config_key(config: dict) -> str:
    """A stable label for the config an arm ran, for grouping and comparison."""

    minutes = int(config.get("duration_seconds", 0)) // 60
    timeframe = int(config.get("entry_timeframe_seconds", 0) or 0)
    frame = {0: "5s", 60: "1m", 180: "3m", 300: "5m"}.get(timeframe, f"{timeframe}s")
    strategy = config.get("signal_strategy", "MOMENTUM_REVERSAL")
    strategy_label = strategy.lower().replace("_", "-")
    return f"{strategy_label}-{minutes}m-{config.get('max_positions', '?')}pos-{frame}"


def _run_summary(run: dict) -> dict:
    """One row of the report: how one arm's run went."""

    config = run.get("config", {})
    metrics = run.get("metrics", {})
    fills = run.get("fills", [])
    exit_events = [
        event for event in run.get("events", []) if event.get("type") == "EXIT_FILLED"
    ]
    symbols_traded = sorted({fill["symbol"] for fill in fills})
    exit_reasons: dict = {}
    for event in exit_events:
        reason = event.get("reason", "UNKNOWN")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    round_trips = len(exit_events)
    wins = sum(
        1
        for event in exit_events
        if (event.get("exit_state") or {}).get("net_of_cost_pct", 0) > 0
    )
    return {
        "run_id": run.get("id"),
        "experiment_id": run.get("experiment_id") or config.get("experiment_id"),
        "variant_label": run.get("variant_label") or config.get("variant_label"),
        "shared_config_hash": run.get("shared_config_hash") or config.get("shared_config_hash"),
        "account_id": config.get("account_id"),
        "config_key": _config_key(config),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": config.get("duration_seconds"),
        "max_positions": config.get("max_positions"),
        "entry_timeframe_seconds": config.get("entry_timeframe_seconds", 0),
        "signal_strategy": config.get("signal_strategy", "MOMENTUM_REVERSAL"),
        "net_pnl": run.get("session_pnl", metrics.get("net_pnl", 0.0)),
        "gross_pnl": metrics.get("gross_pnl", 0.0),
        "fees": metrics.get("fees", 0.0),
        "round_trips": round_trips,
        "wins": wins,
        "win_rate_pct": round(wins / round_trips * 100, 1) if round_trips else 0.0,
        "symbols_traded": symbols_traded,
        "exit_reasons": exit_reasons,
        "monitoring_count": run.get("monitoring_count", 0),
        "scan_count": run.get("scan_count", 0),
        "poll_count": run.get("poll_count", 0),
        "errors": run.get("errors", []),
    }


def _decision_totals(runs: List[dict]) -> List[dict]:
    """Aggregate decision histograms across every run in the day."""

    totals: dict = {}
    for run in runs:
        for row in run.get("decision_counts", []) or []:
            totals[row["decision"]] = totals.get(row["decision"], 0) + row["count"]
    grand = sum(totals.values())
    return [
        {
            "decision": decision,
            "count": count,
            "share_pct": round(count / grand * 100, 2) if grand else 0.0,
        }
        for decision, count in sorted(totals.items(), key=lambda item: -item[1])
    ]


def _by_config(summaries: List[dict], dimension: str) -> List[dict]:
    """Roll up net P&L and trades along one config dimension."""

    groups: dict = {}
    for summary in summaries:
        key = summary[dimension]
        bucket = groups.setdefault(
            key, {"key": key, "runs": 0, "net_pnl": 0.0, "round_trips": 0, "wins": 0}
        )
        bucket["runs"] += 1
        bucket["net_pnl"] += summary["net_pnl"]
        bucket["round_trips"] += summary["round_trips"]
        bucket["wins"] += summary["wins"]
    rows = []
    for bucket in groups.values():
        trips = bucket["round_trips"]
        rows.append(
            {
                **bucket,
                "net_pnl": round(bucket["net_pnl"], 2),
                "win_rate_pct": round(bucket["wins"] / trips * 100, 1) if trips else 0.0,
            }
        )
    return sorted(rows, key=lambda row: str(row["key"]))


class DailyReportBuilder:
    """Aggregates a day's automated runs into one stored report."""

    def __init__(self, store: ResearchStore) -> None:
        self.store = store

    def build(self, session_date: str, persist: bool = True) -> dict:
        runs = [
            run
            for run in self.store.momentum_runs()
            if _run_session_date(run) == session_date
        ]
        summaries = [_run_summary(run) for run in runs]
        summaries.sort(key=lambda row: row.get("started_at") or "")

        completed = [row for row in summaries if row["status"] in {"COMPLETED", "FAILED"}]
        total_net = round(sum(row["net_pnl"] for row in summaries), 2)
        total_trips = sum(row["round_trips"] for row in summaries)
        total_wins = sum(row["wins"] for row in summaries)
        total_fees = round(sum(row["fees"] for row in summaries), 2)
        traded = [row for row in summaries if row["round_trips"] > 0]

        best = max(summaries, key=lambda row: row["net_pnl"], default=None)
        worst = min(summaries, key=lambda row: row["net_pnl"], default=None)

        plan = self.store.schedule_plan(session_date)
        observations = self.store.observations(session_date=session_date, limit=1)
        session_meta = next(
            (
                item
                for item in self.store.observed_sessions()
                if item["session_date"] == session_date
            ),
            None,
        )

        report = {
            "session_date": session_date,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runs": summaries,
            "totals": {
                "runs": len(summaries),
                "completed": len(completed),
                "runs_that_traded": len(traded),
                "net_pnl": total_net,
                "fees": total_fees,
                "round_trips": total_trips,
                "wins": total_wins,
                "win_rate_pct": round(total_wins / total_trips * 100, 1)
                if total_trips
                else 0.0,
                "observations": (session_meta or {}).get("observations", 0),
                "symbols_observed": (session_meta or {}).get("symbols", 0),
            },
            "best_run": best,
            "worst_run": worst,
            "by_duration": _by_config(summaries, "duration_seconds"),
            "by_positions": _by_config(summaries, "max_positions"),
            "by_timeframe": _by_config(summaries, "entry_timeframe_seconds"),
            "by_strategy": _by_config(summaries, "signal_strategy"),
            "decision_totals": _decision_totals(runs),
            "coverage": (plan or {}).get("coverage"),
            "plan_slots": len((plan or {}).get("slots", [])),
            "has_observations": bool(observations),
        }
        if persist:
            self.store.save_daily_report(session_date, report)
        return report

    @staticmethod
    def _now_ist() -> str:
        return datetime.now(IST).isoformat()
