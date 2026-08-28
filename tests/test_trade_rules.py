from datetime import datetime, timedelta, timezone

import pytest

from jupiter_trading.domain import Product
from jupiter_trading.paper_broker import FeeSchedule
from jupiter_trading.trade_rules import (
    EntryPolicy,
    ExitPhase,
    ExitPolicy,
    RatchetExit,
    breakeven_pct,
    cost_model,
    evaluate_entry,
    rolling_noise_pct,
)

START = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)


def exit_rule(**overrides) -> RatchetExit:
    policy = ExitPolicy(**overrides)
    return RatchetExit(1_000.0, 0.1, START, policy, structural_stop=998.2)


def test_breakeven_shrinks_as_the_flat_brokerage_is_amortised() -> None:
    small = breakeven_pct(25_000, FeeSchedule(), 2.0, Product.INTRADAY)
    large = breakeven_pct(100_000, FeeSchedule(), 2.0, Product.INTRADAY)
    assert small == pytest.approx(0.2643, abs=0.001)
    assert large == pytest.approx(0.1226, abs=0.001)
    assert large < small


def test_cost_model_splits_the_charge_the_same_way_the_broker_does() -> None:
    model = cost_model(50_000, FeeSchedule(), 2.0, Product.INTRADAY)
    assert model["round_trip_fees"] == pytest.approx(
        model["buy_fees"] + model["sell_fees"]
    )
    assert model["total_cost"] == pytest.approx(
        model["round_trip_fees"] + model["slippage"]
    )
    assert model["slippage"] == pytest.approx(20.0)


def test_three_bar_entry_triggers_on_a_rise_and_seeds_the_stop_at_the_window_low() -> None:
    evaluation = evaluate_entry([99.0, 100.0, 100.5, 101.0], EntryPolicy(minimum_rise_pct=0.1))
    assert evaluation.triggered
    assert evaluation.reason == "THREE_BAR_BREAKOUT"
    assert evaluation.window == [100.0, 100.5, 101.0]
    assert evaluation.trigger_price == 100.0
    assert evaluation.rise_pct == pytest.approx(1.0)


@pytest.mark.parametrize(
    "prices,reason",
    [
        ([100.0, 100.0, 100.0], "FLAT_NO_TRADE"),
        ([100.0, 100.5, 99.0], "DOWNWARD_NO_TRADE"),
        ([100.0, 99.5, 99.0], "DOWNWARD_NO_TRADE"),
        ([100.0, 100.02, 100.05], "BELOW_ENTRY_THRESHOLD"),
        ([100.0, 100.5], "BUILDING_PRICE_HISTORY"),
    ],
)
def test_documented_no_trade_windows_are_reported_separately(prices, reason) -> None:
    evaluation = evaluate_entry(prices, EntryPolicy(minimum_rise_pct=0.1))
    assert not evaluation.triggered
    assert evaluation.reason == reason


def test_a_structural_stop_inside_the_noise_floor_is_widened() -> None:
    rule = RatchetExit(1_000.0, 0.1, START, ExitPolicy(), structural_stop=999.5)
    assert rule.stop_source == "STRUCTURAL_INSIDE_NOISE"
    assert rule.stop_price == pytest.approx(998.5)


def test_a_structural_stop_beyond_the_risk_limit_is_tightened() -> None:
    rule = RatchetExit(1_000.0, 0.1, START, ExitPolicy(), structural_stop=990.0)
    assert rule.stop_source == "STRUCTURAL_BEYOND_RISK"
    assert rule.stop_price == pytest.approx(998.0)


def test_a_structural_stop_between_the_bounds_is_kept() -> None:
    rule = exit_rule()
    assert rule.stop_source == "STRUCTURAL"
    assert rule.stop_price == pytest.approx(998.2)


def test_the_stop_climbs_through_survive_lock_and_ride_without_ever_falling() -> None:
    rule = exit_rule()
    stops = []
    for index, price in enumerate([1000.5, 1001.6, 1003.5, 1006.0, 1004.0, 1005.0]):
        state = rule.update(price, START + timedelta(seconds=5 * (index + 1)))
        stops.append(state["stop_price"])
    assert stops == sorted(stops)
    assert rule.phase is ExitPhase.RIDE
    assert rule.stop_price > rule.entry_price


def test_reaching_the_lock_threshold_puts_the_stop_above_the_cost_floor() -> None:
    rule = exit_rule()
    state = rule.update(1_001.6, START + timedelta(seconds=5))
    assert state["phase"] == "LOCK"
    assert state["stop_price"] == pytest.approx(1_001.0)
    assert state["stop_source"] == "BREAKEVEN_LOCK"


def test_a_breach_in_survive_exits_immediately_because_it_is_the_risk_limit() -> None:
    rule = exit_rule()
    state = rule.update(997.0, START + timedelta(seconds=5))
    assert state["reason"] == "HARD_STOP"
    assert state["breaches"] == 1


def test_a_locked_stop_needs_consecutive_prints_before_it_fires() -> None:
    rule = exit_rule(confirmation_samples=2)
    rule.update(1_001.6, START + timedelta(seconds=5))
    first = rule.update(1_000.5, START + timedelta(seconds=10))
    assert first["reason"] is None
    assert first["breaches"] == 1
    second = rule.update(1_000.4, START + timedelta(seconds=15))
    assert second["reason"] == "BREAKEVEN_STOP"


def test_a_single_wick_below_the_stop_does_not_end_the_trade() -> None:
    rule = exit_rule(confirmation_samples=2)
    rule.update(1_001.6, START + timedelta(seconds=5))
    assert rule.update(1_000.5, START + timedelta(seconds=10))["reason"] is None
    assert rule.update(1_002.0, START + timedelta(seconds=15))["reason"] is None
    assert rule.breaches == 0


def test_a_trade_that_never_reaches_lock_is_closed_by_the_time_stop() -> None:
    rule = exit_rule(time_stop_seconds=240)
    assert rule.update(1_000.4, START + timedelta(seconds=235))["reason"] is None
    state = rule.update(1_000.4, START + timedelta(seconds=245))
    assert state["reason"] == "TIME_STOP"
    assert state["phase"] == "SURVIVE"


def test_the_time_stop_stands_down_once_the_trade_is_locked() -> None:
    rule = exit_rule(time_stop_seconds=240)
    rule.update(1_001.6, START + timedelta(seconds=10))
    assert rule.update(1_001.6, START + timedelta(seconds=600))["reason"] is None


def test_decaying_volume_tightens_the_trailing_window() -> None:
    rule = exit_rule(trail_window=24, fast_trail_window=3, volume_decay_ratio=1.0)
    prices = [1001.6, 1003.5, 1004.0, 1005.0, 1006.0, 1007.0]
    for index, price in enumerate(prices):
        rule.update(price, START + timedelta(seconds=5 * (index + 1)), relative_volume=1.5)
    patient = rule.stop_price
    state = rule.update(
        1_008.0, START + timedelta(seconds=40), relative_volume=0.6
    )
    assert state["volume_state"] == "DECAYED"
    assert state["trail_window"] == 3
    assert state["stop_price"] > patient


def test_policies_reject_settings_that_cannot_hold_together() -> None:
    with pytest.raises(ValueError):
        ExitPolicy(ride_multiple=1.0, lock_multiple=2.0)
    with pytest.raises(ValueError):
        ExitPolicy(fast_trail_window=30, trail_window=10)
    with pytest.raises(ValueError):
        ExitPolicy(confirmation_samples=0)
    with pytest.raises(ValueError):
        EntryPolicy(bars=1)


# Measured from a real NIFTY 100 session: the median absolute move across a
# three-sample (15 second) window is about 0.015%, the 90th percentile 0.053%.
QUIET = [100.0, 100.005, 100.0, 100.008, 100.002, 100.006, 100.0]
JUMPY = [100.0, 100.08, 99.95, 100.09, 99.97, 100.06, 100.0]


def test_rolling_noise_measures_movement_over_the_same_span_as_the_rule() -> None:
    assert rolling_noise_pct(QUIET, bars=3) < rolling_noise_pct(JUMPY, bars=3)
    assert rolling_noise_pct([100.0, 100.1], bars=3) is None


def test_the_entry_bar_is_the_largest_of_its_three_claims() -> None:
    policy = EntryPolicy(minimum_rise_pct=0.10, cost_floor_multiple=1.0, noise_multiple=2.0)
    assert policy.threshold(None, None) == (0.10, "ABSOLUTE_FLOOR")
    assert policy.threshold(0.264, None) == (0.264, "COST_FLOOR")
    assert policy.threshold(0.05, 0.09) == (0.18, "STOCK_NOISE")


def test_an_entry_below_the_cost_of_taking_it_is_rejected() -> None:
    """A 0.10% rise at 25,000 does not cover the 0.264% round trip."""

    prices = QUIET[:-1] + [100.0, 100.05, 100.10]
    evaluation = evaluate_entry(prices, EntryPolicy(), cost_floor_pct=0.264)
    assert not evaluation.triggered
    assert evaluation.reason == "BELOW_ENTRY_THRESHOLD"
    assert evaluation.threshold_source == "COST_FLOOR"
    assert evaluation.threshold_pct == pytest.approx(0.264)


def test_the_same_rise_is_accepted_once_the_position_is_large_enough() -> None:
    """0.15% covers the round trip at 100,000 but not at 25,000."""

    prices = QUIET[:-1] + [100.0, 100.07, 100.15]
    small = evaluate_entry(prices, EntryPolicy(), cost_floor_pct=0.2643)
    large = evaluate_entry(prices, EntryPolicy(), cost_floor_pct=0.1226)
    assert not small.triggered and small.reason == "BELOW_ENTRY_THRESHOLD"
    assert large.triggered
    assert large.rise_pct == pytest.approx(0.15, abs=0.001)


def test_the_cost_floor_dominates_the_absolute_floor_at_every_real_size() -> None:
    """The old fixed 0.10% bar is below the round-trip cost at any allocation."""

    prices = QUIET[:-1] + [100.0, 100.05, 100.10]
    for floor in (0.1226, 0.2643):
        evaluation = evaluate_entry(prices, EntryPolicy(), cost_floor_pct=floor)
        assert not evaluation.triggered
        assert evaluation.threshold_source == "COST_FLOOR"


def test_a_jumpy_stock_has_to_clear_a_higher_bar_than_a_quiet_one() -> None:
    # A low absolute floor lets the noise claim be the binding one; with the
    # default 0.10% floor a real NIFTY 100 stock is never jumpy enough to bind.
    policy = EntryPolicy(minimum_rise_pct=0.01, cost_floor_multiple=0, noise_multiple=2.0)
    rise = [100.0, 100.02, 100.04]
    quiet = evaluate_entry(QUIET[:-1] + rise, policy)
    jumpy = evaluate_entry(JUMPY[:-1] + rise, policy)
    assert jumpy.threshold_pct > quiet.threshold_pct
    assert jumpy.threshold_source == "STOCK_NOISE"
    assert quiet.triggered and not jumpy.triggered


def test_the_scaling_can_be_turned_off_for_a_fixed_percentage_rule() -> None:
    policy = EntryPolicy(minimum_rise_pct=0.02, cost_floor_multiple=0, noise_multiple=0)
    evaluation = evaluate_entry(
        JUMPY[:-1] + [100.0, 100.02, 100.05], policy, cost_floor_pct=0.264
    )
    assert evaluation.triggered
    assert evaluation.threshold_source == "ABSOLUTE_FLOOR"


def test_entry_multiples_reject_negative_settings() -> None:
    with pytest.raises(ValueError):
        EntryPolicy(cost_floor_multiple=-1)
    with pytest.raises(ValueError):
        EntryPolicy(noise_multiple=-1)


# ---- Bar aggregation for a resampled entry timeframe -----------------------

def ts(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def test_a_bar_closes_only_once_its_wall_clock_bucket_ends() -> None:
    from jupiter_trading.trade_rules import BarAggregator

    agg = BarAggregator(bar_seconds=60)
    assert agg.add(100.0, ts(0)) is None
    assert agg.add(100.5, ts(30)) is None
    assert agg.add(99.5, ts(59)) is None
    closed = agg.add(101.0, ts(60))
    assert closed is not None
    assert closed.open == 100.0
    assert closed.high == 100.5
    assert closed.low == 99.5
    assert closed.close == 99.5
    assert closed.samples == 3
    assert agg.current_bar.open == 101.0


def test_completed_bars_never_include_the_bar_still_forming() -> None:
    from jupiter_trading.trade_rules import BarAggregator

    agg = BarAggregator(bar_seconds=60)
    # Ticks at 0/20/40s share the first minute; 60/80/100s open the second
    # minute, which has not yet closed because no tick past 120s has arrived.
    for second in (0, 20, 40, 60, 80, 100):
        agg.add(100.0, ts(second))
    assert len(agg.completed_bars) == 1
    assert agg.current_bar is not None
    assert agg.current_bar.samples == 3


def test_a_bars_low_captures_a_wick_the_close_alone_would_hide() -> None:
    from jupiter_trading.trade_rules import BarAggregator

    agg = BarAggregator(bar_seconds=60)
    agg.add(100.0, ts(0))
    agg.add(98.0, ts(20))   # dips well below the eventual close
    agg.add(100.2, ts(59))
    closed = agg.add(100.5, ts(60))
    assert closed.close == 100.2
    assert closed.low == 98.0
    assert closed.close != closed.low


def test_aggregator_rejects_a_non_positive_bar_span() -> None:
    from jupiter_trading.trade_rules import BarAggregator

    with pytest.raises(ValueError):
        BarAggregator(bar_seconds=0)


def test_older_bars_roll_off_once_the_cap_is_reached() -> None:
    from jupiter_trading.trade_rules import BarAggregator

    agg = BarAggregator(bar_seconds=1, max_bars=3)
    for second in range(6):
        agg.add(float(second), ts(second))
    assert len(agg.completed_bars) == 3


# ---- evaluate_entry with an honest, bar-sourced stop seed -------------------

def test_without_lows_the_close_still_doubles_as_its_own_low() -> None:
    """Unchanged tick-stream behaviour: no bar data means no lower price was seen."""

    evaluation = evaluate_entry([99.0, 100.0, 100.5, 101.0], EntryPolicy(minimum_rise_pct=0.1))
    assert evaluation.trigger_price == 100.0  # min of the closes themselves


def test_with_lows_the_stop_seed_reflects_the_real_intrabar_wick() -> None:
    closes = [100.0, 100.6, 101.2]
    lows = [99.7, 98.5, 100.9]   # bar 2 wicked well below its own close
    evaluation = evaluate_entry(closes, EntryPolicy(minimum_rise_pct=0.1), lows=lows)
    assert evaluation.triggered
    assert evaluation.trigger_price == 98.5
    assert evaluation.trigger_price < min(closes)


def test_a_short_lows_series_is_treated_as_still_building_history() -> None:
    evaluation = evaluate_entry([100.0, 101.0, 102.0], EntryPolicy(), lows=[99.0, 98.0])
    assert evaluation.reason == "BUILDING_PRICE_HISTORY"
