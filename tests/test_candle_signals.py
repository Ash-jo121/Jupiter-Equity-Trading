from datetime import datetime, timedelta, timezone

import pytest

from jupiter_trading.candle_signals import (
    MACD_EARLY,
    MACD_EARLY_PRICE_CONFIRM,
    MACD_FRESH_CONFIRMED,
    PriceConfirmationSetup,
    SetupState,
    SignalConfig,
    build_features,
    evaluate_entry,
    evaluate_shared_exit,
)
from jupiter_trading.indicators import ema, macd_series
from jupiter_trading.market_data import Candle

START = datetime(2026, 9, 10, 3, 45, tzinfo=timezone.utc)


def _candle(index, close=100.0, volume=100, ohlc=None):
    if ohlc is None:
        open_price, high, low = close - 0.2, close + 0.2, close - 0.5
    else:
        open_price, high, low, close = ohlc
    return Candle(
        START + timedelta(minutes=index),
        open_price,
        high,
        low,
        close,
        volume,
        0,
    )


def _features(histograms, *, ohlc=(100.0, 101.0, 99.0, 100.5), volume=200):
    rows = [_candle(index, 100 + index * 0.01) for index in range(99)]
    rows.append(_candle(99, volume=volume, ohlc=ohlc))
    feature = build_features(rows)
    return feature.__class__(
        **{
            **feature.__dict__,
            "h2": histograms[0],
            "h1": histograms[1],
            "histogram": histograms[2],
            "delta_bps": 10_000 * (histograms[2] - histograms[1]) / feature.bar.close,
            "latest_cross_age": 0 if histograms[1] <= 0 < histograms[2] else None,
            "ready": True,
            "quality_reasons": (),
            "rvol_1m": 2.0,
        }
    )


def test_ema_uses_arithmetic_mean_seed_and_keeps_leading_values_undefined() -> None:
    assert ema([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    points = macd_series([float(value) for value in range(1, 40)])
    assert points[0].histogram is None
    assert next(point for point in points if point.histogram is not None).histogram is not None


def test_early_entry_requires_negative_two_step_improvement() -> None:
    passed = evaluate_entry(MACD_EARLY, _features([-0.30, -0.20, -0.09]))
    crossed = evaluate_entry(MACD_EARLY, _features([-0.20, -0.10, 0.01]))
    equal = evaluate_entry(MACD_EARLY, _features([-0.20, -0.10, -0.10]))

    assert passed["raw_qualified"] is True
    assert crossed["raw_qualified"] is False
    assert equal["raw_qualified"] is False


def test_price_confirmation_arms_from_early_signal_and_uses_later_quote() -> None:
    features = _features([-0.30, -0.20, -0.09])
    assert evaluate_entry(MACD_EARLY_PRICE_CONFIRM, features)["raw_qualified"] is True
    available = features.bar_end + timedelta(seconds=2)
    setup = PriceConfirmationSetup.arm(
        "setup-1", "NSE_EQ|TEST", features, available, 0.05
    )

    assert setup.on_quote(101.15, available) == SetupState.ARMED.value
    assert setup.on_quote(101.15, available + timedelta(seconds=1)) == SetupState.TRIGGERED.value


def test_price_confirmation_expiry_and_low_boundaries_are_inclusive() -> None:
    features = _features([-0.30, -0.20, -0.09])
    setup = PriceConfirmationSetup.arm(
        "setup-1", "NSE_EQ|TEST", features, features.bar_end, 0.05
    )
    assert setup.on_quote(99.0, features.bar_end + timedelta(seconds=1)) == "INVALIDATED"

    setup = PriceConfirmationSetup.arm(
        "setup-2", "NSE_EQ|TEST", features, features.bar_end, 0.05
    )
    assert setup.on_quote(101.2, setup.expiry) == "EXPIRED"


def test_price_confirmation_cancels_an_unordered_both_touch_bar() -> None:
    features = _features([-0.30, -0.20, -0.09])
    setup = PriceConfirmationSetup.arm(
        "setup-1", "NSE_EQ|TEST", features, features.bar_end, 0.05
    )
    ambiguous = features.__class__(
        **{
            **features.__dict__,
            "bar": _candle(100, ohlc=(100.0, 101.2, 98.9, 100.5)),
            "bar_end": features.bar_end + timedelta(minutes=1),
        }
    )

    assert setup.on_bar(ambiguous) == "CANCELLED"
    assert setup.reason == "AMBIGUOUS_BAR"


def test_fresh_confirmed_accepts_cross_ages_zero_one_two_only() -> None:
    base = _features([-0.01, 0.01, 0.02])
    for age in (0, 1, 2):
        features = base.__class__(**{**base.__dict__, "latest_cross_age": age})
        assert evaluate_entry(MACD_FRESH_CONFIRMED, features)["raw_qualified"] is True
    stale = base.__class__(**{**base.__dict__, "latest_cross_age": 3})
    assert evaluate_entry(MACD_FRESH_CONFIRMED, stale)["raw_qualified"] is False


@pytest.mark.parametrize(
    ("histograms", "reason"),
    [
        ((0.50, 0.625, 0.50), "MOMENTUM_CONTRACTION_MATERIAL"),
        ((0.10, 0.099, 0.098), "MOMENTUM_CONTRACTION_CONSECUTIVE"),
        ((0.12, 0.10, -0.01), "MOMENTUM_CROSS_DOWN"),
        ((0.12, 0.10, 0.0), "MOMENTUM_ZERO_TOUCH"),
    ],
)
def test_shared_exit_macd_branches(histograms, reason) -> None:
    # Red, strong-body candle: body/range=.75 and close location=.125.
    features = _features(histograms, ohlc=(101.0, 101.0, 99.0, 99.25))
    result = evaluate_shared_exit(features)
    assert result["actionable"] is True
    assert result["reason"] == reason


def test_shared_exit_requires_bearish_candle_and_volume_is_annotation_only() -> None:
    green = _features((0.50, 0.625, 0.50))
    assert evaluate_shared_exit(green)["actionable"] is False

    bearish = _features((0.50, 0.625, 0.50), ohlc=(101.0, 101.0, 99.0, 99.25))
    unknown = bearish.__class__(**{**bearish.__dict__, "rvol_1m": None})
    assert evaluate_shared_exit(unknown)["actionable"] is True
    assert evaluate_shared_exit(unknown)["volume"]["unknown"] is True


def test_v1_configuration_rejects_changed_fixed_windows() -> None:
    with pytest.raises(ValueError, match="confirmation"):
        SignalConfig(confirmation_window_bars=3)
