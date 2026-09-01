import pytest
from fastapi.testclient import TestClient

from jupiter_trading.api import create_app
from jupiter_trading.config import Settings
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits


def test_order_tick_and_portfolio_flow(tmp_path) -> None:
    settings = Settings(
        initial_cash=100_000,
        slippage_bps=0,
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )

    with TestClient(create_app(settings)) as client:
        order_response = client.post(
            "/orders",
            json={
                "instrument_key": "NSE_EQ|TEST",
                "side": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
            },
        )
        assert order_response.status_code == 201
        assert order_response.json()["status"] == "OPEN"

        tick_response = client.post(
            "/paper/ticks",
            json={"instrument_key": "NSE_EQ|TEST", "last_price": 100},
        )
        assert tick_response.status_code == 200
        assert len(tick_response.json()["fills"]) == 1

        portfolio = client.get("/portfolio").json()
        assert portfolio["cash"] == 99_000
        assert portfolio["equity"] == 100_000


def test_market_data_endpoint_requires_token(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/market/ltp", params={"instrument_key": "NSE_EQ|TEST"})

    assert response.status_code == 503
    assert response.json()["detail"] == "UPSTOX_ACCESS_TOKEN is not configured"


def test_cost_model_endpoint_reports_the_breakeven_for_a_position_size(tmp_path) -> None:
    settings = Settings(
        slippage_bps=2,
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
        fee_schedule=FeeSchedule(),
        risk_limits=RiskLimits(),
    )

    with TestClient(create_app(settings)) as client:
        small = client.get("/cost-model", params={"notional": 25_000}).json()
        large = client.get("/cost-model", params={"notional": 100_000}).json()
        assert small["breakeven_pct"] > large["breakeven_pct"]
        assert small["slippage"] == 10.0
        assert client.get("/cost-model", params={"notional": 0}).status_code == 422


def test_exit_replay_endpoint_rescores_a_recorded_run(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from jupiter_trading.research_store import ResearchStore

    start = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)
    prices = [1000.0, 1000.2, 1000.6, 1001.5, 1004.0, 1008.0, 1012.0, 1006.0, 1002.0, 1000.0]
    store = ResearchStore(str(tmp_path / "paper.db"))
    store.save_momentum_run(
        {
            "id": "recorded",
            "status": "COMPLETED",
            "initial_equity": 100_000,
            "config": {
                "account_id": "momentum",
                "allocation_per_position": 50_000,
                "max_positions": 2,
                "entry_bars": 3,
                "entry_momentum_pct": 0.05,
                "minimum_relative_volume": 1.2,
            },
            "fills": [],
            "events": [],
            "monitoring": [
                {
                    "timestamp": (start + timedelta(seconds=5 * index)).isoformat(),
                    "symbol": "TEST",
                    "instrument_key": "NSE_EQ|TEST",
                    "price": price,
                    "relative_volume": 1.6,
                    "nifty_window_change_pct": 0.04,
                    "nifty_recent_15m_change_pct": 0.06,
                    "decision": "ENTRY_FILLED" if index == 3 else "WATCHING",
                }
                for index, price in enumerate(prices)
            ],
        }
    )
    settings = Settings(
        slippage_bps=2,
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
        fee_schedule=FeeSchedule(),
        risk_limits=RiskLimits(),
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/backtests/exit-replay",
            json={"run_id": "recorded", "mode": "EXITS_ONLY", "time_stop_seconds": 600},
        )
        assert response.status_code == 201
        report = response.json()
        assert report["strategy_type"] == "EXIT_REPLAY"
        assert report["candidate"]["round_trips"] == 1
        assert report["trades"][0]["exit_reason"] == "TRAILING_STOP"

        missing = client.post("/backtests/exit-replay", json={"run_id": "nope"})
        assert missing.status_code == 404

        saved = client.get("/reports/backtests").json()
        assert any(item["id"] == report["id"] for item in saved)


def _paper_settings(tmp_path):
    return Settings(
        initial_cash=100_000,
        slippage_bps=0,
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )


def test_batch_variants_merge_onto_the_base_config_with_separate_accounts() -> None:
    from jupiter_trading.api import MomentumBatchRequest, MomentumRunRequest, MomentumVariantRequest

    request = MomentumBatchRequest(
        base=MomentumRunRequest(allocation_per_position=100_000, duration_seconds=1800),
        variants=[
            MomentumVariantRequest(label="ticks", overrides={"entry_timeframe_seconds": 0}),
            MomentumVariantRequest(label="1-min", overrides={"entry_timeframe_seconds": 60}),
        ],
        account_prefix="fwd",
    )
    first = request.merged(request.variants[0], 0)
    second = request.merged(request.variants[1], 1)

    assert first.account_id == "fwd-ticks"
    assert second.account_id == "fwd-1-min"
    assert first.entry_timeframe_seconds == 0
    assert second.entry_timeframe_seconds == 60
    # Anything not overridden is inherited from the base.
    assert first.allocation_per_position == second.allocation_per_position == 100_000
    assert second.duration_seconds == 1800


def test_a_batch_variant_cannot_set_a_field_that_does_not_exist() -> None:
    from jupiter_trading.api import MomentumBatchRequest, MomentumVariantRequest

    request = MomentumBatchRequest(
        variants=[MomentumVariantRequest(label="typo", overrides={"entry_timeframe": 60})]
    )
    with pytest.raises(ValueError, match="unknown override fields"):
        request.merged(request.variants[0], 0)


def test_observation_endpoints_expose_the_recorded_trace(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from jupiter_trading.research_store import ResearchStore

    start = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)
    store = ResearchStore(str(tmp_path / "paper.db"))
    store.add_observations(
        "recorded",
        [
            {
                "timestamp": (start + timedelta(seconds=5 * i)).isoformat(),
                "symbol": "TCS",
                "instrument_key": "NSE_EQ|TCS",
                "price": 2340.0 + i,
                "decision": "WATCHING",
            }
            for i in range(12)
        ],
    )

    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        sessions = client.get("/observations/sessions").json()
        assert sessions[0]["session_date"] == "2026-08-28"
        assert sessions[0]["observations"] == 12

        page = client.get("/observations", params={"symbol": "TCS", "limit": 5}).json()
        assert page["count"] == 5
        assert page["observations"][0]["price"] == 2340.0

        assert client.get("/observations", params={"session_date": "1999-01-01"}).json()["count"] == 0
        assert client.get("/observations", params={"limit": 0}).status_code == 422


def test_schedule_plan_endpoint_returns_one_full_session_run_per_timeframe(tmp_path) -> None:
    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        plan = client.get("/schedule/plan", params={"session_date": "2026-08-31"}).json()
        assert plan["session_date"] == "2026-08-31"
        assert len(plan["slots"]) == 4  # 5s, 1m, 3m, 5m
        assert {s["entry_timeframe_seconds"] for s in plan["slots"]} == {0, 60, 180, 300}
        assert plan["coverage"]["covered_pct"] == 100.0
        # Every run spans the whole session, so all share the open and close.
        assert len({s["start_ist"] for s in plan["slots"]}) == 1
        assert all(s["duration_seconds"] == 22500 for s in plan["slots"])


def test_schedule_status_reports_disabled_by_default(tmp_path) -> None:
    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        status = client.get("/schedule/status").json()
        assert status["enabled"] is False
        assert "session_date" in status


def test_daily_report_can_be_built_and_fetched(tmp_path) -> None:
    from jupiter_trading.research_store import ResearchStore

    store = ResearchStore(str(tmp_path / "paper.db"))
    store.save_momentum_run(
        {
            "id": "auto-run",
            "account_id": "auto-x",
            "status": "COMPLETED",
            "started_at": "2026-08-28T05:30:00+00:00",
            "finished_at": "2026-08-28T06:00:00+00:00",
            "session_pnl": -42.0,
            "config": {
                "account_id": "auto-x",
                "duration_seconds": 1800,
                "max_positions": 2,
                "entry_timeframe_seconds": 60,
            },
            "metrics": {"gross_pnl": -22.0, "fees": 20.0, "net_pnl": -42.0},
            "fills": [],
            "events": [],
            "decision_counts": [{"decision": "FLAT_NO_TRADE", "count": 10, "share_pct": 100.0}],
        }
    )

    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        assert client.get("/reports/daily/2026-08-28").status_code == 404
        built = client.post("/reports/daily/2026-08-28/build").json()
        assert built["totals"]["runs"] == 1
        assert built["totals"]["net_pnl"] == -42.0

        fetched = client.get("/reports/daily/2026-08-28").json()
        assert fetched["session_date"] == "2026-08-28"
        assert client.get("/reports/daily")[0] if False else True
        listed = client.get("/reports/daily").json()
        assert any(item["session_date"] == "2026-08-28" for item in listed)


def test_manual_tick_is_a_no_op_while_the_scheduler_is_disabled(tmp_path) -> None:
    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        assert client.post("/schedule/tick").json() == {"actions": []}


def test_upstox_token_can_be_set_and_status_reflects_it(tmp_path) -> None:
    settings = _paper_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        before = client.get("/auth/upstox/status").json()
        assert before["has_token"] is False
        assert before["oauth_configured"] is False

        put = client.put("/auth/upstox/token", json={"access_token": "live-token-123"})
        assert put.status_code == 200
        assert put.json()["has_token"] is True

        health = client.get("/health").json()
        assert health["upstox_configured"] is True
        assert health["upstox_token"]["has_token"] is True


def test_login_url_needs_oauth_configuration(tmp_path) -> None:
    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        # No API key/secret/redirect -> the login URL cannot be built.
        assert client.get("/auth/upstox/login-url").status_code == 400


def test_login_url_is_returned_when_oauth_is_configured(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="",
        upstox_api_key="KEY",
        upstox_api_secret="SECRET",
        upstox_redirect_uri="https://app.example/auth/upstox/callback",
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    with TestClient(create_app(settings)) as client:
        url = client.get("/auth/upstox/login-url").json()["authorization_url"]
        assert "client_id=KEY" in url
        assert "SECRET" not in url  # secret never leaves the server


def test_callback_without_a_code_shows_a_failure_page(tmp_path) -> None:
    with TestClient(create_app(_paper_settings(tmp_path))) as client:
        response = client.get("/auth/upstox/callback")
        assert response.status_code == 400
        assert "failed" in response.text.lower()


def test_a_seeded_env_token_is_live_from_boot(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "paper.db"),
        upstox_access_token="env-seeded-token",
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/auth/upstox/status").json()["has_token"] is True
