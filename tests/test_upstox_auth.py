from datetime import datetime, timedelta, timezone

import pytest

from jupiter_trading.research_store import IST, ResearchStore
from jupiter_trading.upstox_auth import (
    CREDENTIAL_NAME,
    UpstoxAuthError,
    UpstoxTokenStore,
    _token_valid_until,
)


def store(tmp_path):
    return ResearchStore(str(tmp_path / "auth.db"))


def test_an_env_token_seeds_the_store_on_first_boot(tmp_path) -> None:
    ts = UpstoxTokenStore(store(tmp_path), env_token="ENV")
    assert ts.current_token() == "ENV"


def test_a_stored_token_is_not_overwritten_by_the_env_on_later_boots(tmp_path) -> None:
    db = store(tmp_path)
    UpstoxTokenStore(db, env_token="ENV").set_token("FRESH")
    # A later boot with a (now stale) env var must not clobber the live token.
    assert UpstoxTokenStore(db, env_token="ENV").current_token() == "FRESH"


def test_the_token_survives_a_restart(tmp_path) -> None:
    db = store(tmp_path)
    UpstoxTokenStore(db).set_token("PERSISTED")
    assert UpstoxTokenStore(db).current_token() == "PERSISTED"


def test_setting_an_empty_token_is_rejected(tmp_path) -> None:
    with pytest.raises(UpstoxAuthError):
        UpstoxTokenStore(store(tmp_path)).set_token("")


def test_oauth_requires_full_app_credentials(tmp_path) -> None:
    partial = UpstoxTokenStore(store(tmp_path), api_key="k")
    assert partial.configured_for_oauth is False
    with pytest.raises(UpstoxAuthError):
        partial.authorization_url()


def test_the_authorization_url_carries_the_app_credentials(tmp_path) -> None:
    ts = UpstoxTokenStore(
        store(tmp_path), api_key="KEY", api_secret="SECRET", redirect_uri="https://app/cb"
    )
    url = ts.authorization_url()
    assert "client_id=KEY" in url
    assert "response_type=code" in url
    assert "redirect_uri=https%3A%2F%2Fapp%2Fcb" in url
    # The secret is never placed in the browser-facing URL.
    assert "SECRET" not in url


def test_exchange_requires_a_code(tmp_path) -> None:
    ts = UpstoxTokenStore(
        store(tmp_path), api_key="k", api_secret="s", redirect_uri="https://app/cb"
    )
    with pytest.raises(UpstoxAuthError):
        ts.exchange_code("")


def test_status_reports_absence_and_presence(tmp_path) -> None:
    ts = UpstoxTokenStore(store(tmp_path))
    absent = ts.status()
    assert absent["has_token"] is False
    assert absent["likely_valid"] is False
    ts.set_token("T")
    present = ts.status()
    assert present["has_token"] is True
    assert present["updated_at"] is not None


def test_token_expiry_is_the_next_0330_ist() -> None:
    # A token minted at 09:00 IST expires at 03:30 IST the following day.
    issued = datetime(2026, 8, 31, 9, 0, tzinfo=IST)
    expiry = _token_valid_until(issued)
    assert expiry == datetime(2026, 9, 1, 3, 30, tzinfo=IST)
    # One minted just after midnight (before 03:30) expires the same morning.
    issued_early = datetime(2026, 8, 31, 1, 0, tzinfo=IST)
    assert _token_valid_until(issued_early) == datetime(2026, 8, 31, 3, 30, tzinfo=IST)


def test_a_token_from_before_the_last_expiry_reads_as_invalid(tmp_path) -> None:
    db = store(tmp_path)
    ts = UpstoxTokenStore(db)
    # Backdate the stored timestamp two days so its 03:30 expiry is well past.
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    db.set_credential(CREDENTIAL_NAME, "OLD")
    with db._lock, db._connection:
        db._connection.execute(
            "UPDATE credentials SET updated_at = ? WHERE name = ?",
            (two_days_ago, CREDENTIAL_NAME),
        )
    status = ts.status()
    assert status["has_token"] is True
    assert status["likely_valid"] is False
