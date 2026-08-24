from fastapi.testclient import TestClient

from jupiter_trading.api import create_app
from jupiter_trading.config import Settings
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits


def settings(database_path) -> Settings:
    return Settings(
        initial_cash=100_000,
        slippage_bps=0,
        database_path=str(database_path),
        upstox_access_token="token",
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )


def test_account_survives_restart_and_resets_only_explicitly(tmp_path) -> None:
    database = tmp_path / "paper.db"
    with TestClient(create_app(settings(database))) as client:
        client.post(
            "/orders",
            json={
                "instrument_key": "NSE_EQ|TEST",
                "side": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
            },
        )
        client.post("/paper/ticks", json={"instrument_key": "NSE_EQ|TEST", "last_price": 100})
        assert client.get("/portfolio").json()["cash"] == 99_000
        order_id = client.get("/orders").json()[0]["id"]
        events = client.get(f"/orders/{order_id}/events").json()
        assert [event["status"] for event in events] == ["OPEN", "FILLED"]

    with TestClient(create_app(settings(database))) as client:
        restored = client.get("/portfolio").json()
        assert restored["cash"] == 99_000
        assert restored["positions"][0]["quantity"] == 10
        assert len(client.get("/orders").json()) == 1
        assert len(client.get("/fills").json()) == 1

        denied = client.post("/paper/accounts/default/reset", json={"confirm": False})
        assert denied.status_code == 409
        reset = client.post(
            "/paper/accounts/default/reset",
            json={"confirm": True, "initial_cash": 250_000},
        )
        assert reset.json()["cash"] == 250_000
        assert reset.json()["positions"] == []
        assert client.get("/orders").json() == []


def test_named_accounts_are_isolated(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path / "paper.db"))) as client:
        created = client.post(
            "/paper/accounts",
            json={"id": "strategy-a", "name": "Strategy A", "initial_cash": 50_000},
        )
        assert created.status_code == 201

        client.post(
            "/orders",
            json={
                "account_id": "strategy-a",
                "instrument_key": "NSE_EQ|TEST",
                "side": "BUY",
                "quantity": 5,
                "order_type": "MARKET",
            },
        )
        client.post("/paper/ticks", json={"instrument_key": "NSE_EQ|TEST", "last_price": 100})

        assert client.get("/portfolio", params={"account_id": "strategy-a"}).json()["cash"] == 49_500
        assert client.get("/portfolio").json()["cash"] == 100_000


class FakeInstrumentSearch:
    def search(self, query, exchange, segment, records):
        return [
            {
                "instrument_key": "NSE_EQ|INE263A01024",
                "trading_symbol": query,
                "name": "BHARAT ELECTRONICS LIMITED",
                "exchange": exchange,
                "segment": "NSE_" + segment,
            }
        ][:records]


def test_instrument_search_endpoint(tmp_path) -> None:
    app = create_app(settings(tmp_path / "paper.db"), instrument_search=FakeInstrumentSearch())
    with TestClient(app) as client:
        response = client.get("/instruments/search", params={"q": "BEL"})

    assert response.status_code == 200
    assert response.json()[0]["instrument_key"] == "NSE_EQ|INE263A01024"
    assert response.json()[0]["trading_symbol"] == "BEL"
