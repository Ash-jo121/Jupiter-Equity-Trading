from datetime import datetime, timedelta, timezone

from jupiter_trading.feature_engine import SharedFeatureEngine
from jupiter_trading.market_data import Candle


def test_warmup_transient_failure_retries_after_backoff():
    clock = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)

    class Cache:
        calls = 0

        def warmup(self, *_):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary provider outage")
            return {"candles": [Candle(clock - timedelta(days=1), 99, 101, 98, 100, 10, 0)]}

    cache = Cache()
    engine = SharedFeatureEngine(object(), cache)
    assert engine._warmup("TEST", clock) == []
    assert engine._warmup("TEST", clock + timedelta(seconds=30)) == []
    assert cache.calls == 1
    assert engine._warmup("TEST", clock + timedelta(seconds=61))
    assert engine._warmup("TEST", clock + timedelta(seconds=120))
    assert cache.calls == 2


def test_warmup_precedes_live_fetch_and_latency_includes_computation(monkeypatch):
    minute = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
    elapsed = [0.0]
    order = []
    monkeypatch.setattr("jupiter_trading.feature_engine.monotonic", lambda: elapsed[0])

    class Cache:
        def warmup(self, *_):
            order.append("warmup")
            elapsed[0] += 61
            return {"candles": []}

        def get(self, _market, _key, clock, _grace):
            order.append("live")
            assert clock == minute + timedelta(seconds=64)
            elapsed[0] += 1
            return {
                "candles": [Candle(minute, 99, 101, 98, 100, 10, 0)],
                "received_at": minute + timedelta(seconds=65),
                "requested_at": clock,
                "request_seconds": 1,
                "cache_hit": False,
            }

    from jupiter_trading.feature_engine import build_features

    def slow_build(*args):
        elapsed[0] += 11
        return build_features(*args)

    monkeypatch.setattr("jupiter_trading.feature_engine.build_features", slow_build)
    snapshot = SharedFeatureEngine(object(), Cache()).snapshot("TEST", minute + timedelta(seconds=3))
    assert order == ["warmup", "live"]
    assert snapshot.features.bar.timestamp == minute
    assert snapshot.available_at == minute + timedelta(seconds=76)
    assert snapshot.stale
    assert snapshot.to_dict()["feature_processing_seconds"] == 11
