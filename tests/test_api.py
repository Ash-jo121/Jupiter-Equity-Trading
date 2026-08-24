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
        fee_schedule=FeeSchedule(),
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
