from jupiter_trading.daily_report import DailyReportBuilder
from jupiter_trading.research_store import ResearchStore


def run_payload(run_id, started, timeframe, net, trips, account, fills=None, exits=None):
    return {
        "id": run_id,
        "account_id": account,
        "status": "COMPLETED",
        "started_at": started,
        "finished_at": started,
        "session_pnl": net,
        "config": {
            "account_id": account,
            "duration_seconds": 1800,
            "max_positions": 2,
            "entry_timeframe_seconds": timeframe,
        },
        "metrics": {"gross_pnl": net + 20, "fees": 20.0, "net_pnl": net},
        "fills": fills or [],
        "events": exits or [],
        "decision_counts": [
            {"decision": "FLAT_NO_TRADE", "count": 100, "share_pct": 50.0},
            {"decision": "ENTRY_FILLED", "count": trips, "share_pct": 1.0},
        ],
        "monitoring_count": 200,
        "scan_count": 5,
        "poll_count": 300,
        "errors": [],
    }


def store_with_two_runs(tmp_path):
    store = ResearchStore(str(tmp_path / "report.db"))
    # Two runs on the same IST day, one profitable arm and one losing arm.
    store.save_momentum_run(
        {
            **run_payload(
                "run-a",
                "2026-08-28T05:00:00+00:00",
                0,
                150.0,
                1,
                "auto-a",
                fills=[{"symbol": "TCS", "side": "SELL", "gross_value": 1, "fees": 1}],
                exits=[
                    {
                        "type": "EXIT_FILLED",
                        "symbol": "TCS",
                        "reason": "TRAILING_STOP",
                        "exit_state": {"net_of_cost_pct": 0.4},
                    }
                ],
            ),
            "config": {
                "account_id": "auto-a",
                "duration_seconds": 1800,
                "max_positions": 2,
                "entry_timeframe_seconds": 0,
            },
        }
    )
    store.save_momentum_run(
        {
            **run_payload(
                "run-b",
                "2026-08-28T06:00:00+00:00",
                60,
                -90.0,
                1,
                "auto-b",
                fills=[{"symbol": "INFY", "side": "SELL", "gross_value": 1, "fees": 1}],
                exits=[
                    {
                        "type": "EXIT_FILLED",
                        "symbol": "INFY",
                        "reason": "HARD_STOP",
                        "exit_state": {"net_of_cost_pct": -0.5},
                    }
                ],
            ),
            "config": {
                "account_id": "auto-b",
                "duration_seconds": 1800,
                "max_positions": 4,
                "entry_timeframe_seconds": 60,
            },
        }
    )
    return store


def test_report_totals_sum_the_day(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    report = DailyReportBuilder(store).build("2026-08-28")
    totals = report["totals"]
    assert totals["runs"] == 2
    assert totals["net_pnl"] == 60.0  # 150 + (-90)
    assert totals["round_trips"] == 2
    assert totals["wins"] == 1
    assert totals["win_rate_pct"] == 50.0


def test_report_identifies_the_best_and_worst_arm(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    report = DailyReportBuilder(store).build("2026-08-28")
    assert report["best_run"]["net_pnl"] == 150.0
    assert report["worst_run"]["net_pnl"] == -90.0


def test_report_rolls_up_by_each_config_dimension(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    report = DailyReportBuilder(store).build("2026-08-28")
    by_timeframe = {row["key"]: row["net_pnl"] for row in report["by_timeframe"]}
    assert by_timeframe[0] == 150.0
    assert by_timeframe[60] == -90.0
    assert len(report["by_positions"]) == 2


def test_report_aggregates_decision_histograms(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    report = DailyReportBuilder(store).build("2026-08-28")
    flat = next(row for row in report["decision_totals"] if row["decision"] == "FLAT_NO_TRADE")
    assert flat["count"] == 200  # 100 from each run


def test_report_only_counts_runs_from_that_session(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    store.save_momentum_run(
        run_payload("run-c", "2026-08-27T05:00:00+00:00", 0, 999.0, 1, "auto-c")
    )
    report = DailyReportBuilder(store).build("2026-08-28")
    assert report["totals"]["runs"] == 2
    assert all(row["run_id"] != "run-c" for row in report["runs"])


def test_report_is_stored_and_can_be_read_back(tmp_path) -> None:
    store = store_with_two_runs(tmp_path)
    DailyReportBuilder(store).build("2026-08-28")
    saved = store.daily_report("2026-08-28")
    assert saved["session_date"] == "2026-08-28"
    assert saved["totals"]["runs"] == 2
    assert store.daily_reports()[0]["session_date"] == "2026-08-28"


def test_a_day_with_no_runs_reports_zeroes(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "report.db"))
    report = DailyReportBuilder(store).build("2026-08-28")
    assert report["totals"]["runs"] == 0
    assert report["best_run"] is None
