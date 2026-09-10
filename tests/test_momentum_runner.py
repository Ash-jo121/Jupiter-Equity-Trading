from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from jupiter_trading.accounts import PaperAccountManager
from jupiter_trading.domain import Quote, Side
from jupiter_trading.market_data import Candle
from jupiter_trading.momentum_runner import (
    NIFTY50_INDEX_KEY,
    MomentumReversalRunner,
    MomentumRunnerConfig,
    MomentumRunnerService,
)
from jupiter_trading.paper_broker import FeeSchedule, RiskLimits
from jupiter_trading.repository import InMemoryRepository
from jupiter_trading.research_store import ResearchStore
from jupiter_trading.strategy_engine import MarketCoordinator, StrategyService
from jupiter_trading.survey import SharedSurveyCache, SurveyInstrument


class RisingThenReversingMarket:
    def __init__(self) -> None:
        self.prices = iter([100.0, 100.0, 100.03, 100.06, 100.12, 100.06])

    def ltp(self, instrument_keys) -> dict:
        price = next(self.prices)
        return {key: Quote(key, price) for key in instrument_keys}

    def intraday_candles(self, instrument_key, unit, interval) -> list:
        now = datetime.now(timezone.utc)
        return [
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 99, volume, 0)
            for index, volume in enumerate([100, 100, 100, 200, 200, 200, 200])
        ]


class FallingNiftyMarket(RisingThenReversingMarket):
    def __init__(self) -> None:
        self.stock_prices = iter([100.0, 100.0, 100.05, 100.10])
        self.nifty_prices = iter([200.0, 199.9, 199.8])

    def ltp(self, instrument_keys) -> dict:
        keys = list(instrument_keys)
        stock_price = next(self.stock_prices) if "NSE_EQ|TEST" in keys else None
        return {
            key: Quote(
                key,
                next(self.nifty_prices) if key == NIFTY50_INDEX_KEY else stock_price,
            )
            for key in keys
        }


class LowVolumeMarket(RisingThenReversingMarket):
    def intraday_candles(self, instrument_key, unit, interval) -> list:
        now = datetime.now(timezone.utc)
        return [
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 99, volume, 0)
            for index, volume in enumerate([200, 200, 200, 100, 100, 100, 100])
        ]


class CountingSurveyMarket:
    def __init__(self) -> None:
        self.ltp_calls = 0
        self.candle_calls = 0

    def ltp(self, instrument_keys) -> dict:
        self.ltp_calls += 1
        return {key: Quote(key, 100.0) for key in instrument_keys}

    def intraday_candles(self, instrument_key, unit, interval) -> list:
        self.candle_calls += 1
        now = datetime.now(timezone.utc)
        return [
            Candle(now + timedelta(minutes=index * 5), 99, 101, 98, 100, volume, 0)
            for index, volume in enumerate([100, 100, 100, 200, 200, 200, 200])
        ]


def test_shared_survey_cache_reuses_one_market_snapshot() -> None:
    market = CountingSurveyMarket()
    cache = SharedSurveyCache(ttl_seconds=300)
    instruments = [SurveyInstrument("TEST", "NSE_EQ|TEST")]

    first = cache.run(market, instruments, 1.2)
    second = cache.run(market, instruments, 1.2)

    assert market.ltp_calls == 1
    assert market.candle_calls == 1
    assert first["shared_cache"]["hit"] is False
    assert second["shared_cache"]["hit"] is True


def test_shared_survey_cache_includes_one_shared_nifty_context_read() -> None:
    market = CountingSurveyMarket()
    cache = SharedSurveyCache(ttl_seconds=300)
    instruments = [SurveyInstrument("TEST", "NSE_EQ|TEST")]

    first = cache.run(market, instruments, 1.2, context_instrument_key=NIFTY50_INDEX_KEY)
    second = cache.run(market, instruments, 1.2, context_instrument_key=NIFTY50_INDEX_KEY)

    assert market.ltp_calls == 1
    assert market.candle_calls == 2  # one stock plus one shared NIFTY baseline
    assert first["market_context"] == second["market_context"]
    assert second["shared_cache"]["hit"] is True


def test_background_survey_does_not_block_live_quote_polling(tmp_path) -> None:
    class BlockingSurveyMarket(CountingSurveyMarket):
        def __init__(self) -> None:
            super().__init__()
            self.candle_started = Event()
            self.release_candle = Event()

        def intraday_candles(self, instrument_key, unit, interval):
            if instrument_key == "NSE_EQ|TEST":
                self.candle_started.set()
                assert self.release_candle.wait(timeout=2)
            return super().intraday_candles(instrument_key, unit, interval)

    market = BlockingSurveyMarket()
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    store = ResearchStore(str(tmp_path / "research.db"))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default"),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=market,
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, StrategyService(accounts, store)),
        survey_cache=SharedSurveyCache(ttl_seconds=300),
    )
    runner._status = "RUNNING"
    runner._accept_background_scans = True

    runner._request_scan()
    assert market.candle_started.wait(timeout=1)
    runner._poll()

    assert runner.snapshot()["poll_count"] == 1
    market.release_candle.set()
    runner._scan_thread.join(timeout=2)
    assert runner.snapshot()["scan_count"] == 1


def test_legacy_reversal_mode_buys_momentum_and_sells_reversal(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(
            delivery_brokerage_flat=0,
            intraday_brokerage_bps=0,
            delivery_sell_dp_flat=0,
        ),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            duration_seconds=300,
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            reversal_pct=0.04,
            entry_mode="ROLLING_WINDOW",
            exit_mode="REVERSAL",
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RisingThenReversingMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="runner-test",
    )

    runner._scan()
    for _ in range(5):
        runner._poll()

    broker = accounts.get()
    orders = [order for order in broker.orders.values() if order.strategy_id == runner.id]
    assert [order.side for order in orders] == [Side.BUY, Side.SELL]
    assert broker.positions["NSE_EQ|TEST"].quantity == 0
    assert runner.snapshot()["completed_instruments"] == ["NSE_EQ|TEST"]
    exit_event = next(
        event for event in runner.snapshot()["events"] if event["type"] == "EXIT_FILLED"
    )
    assert exit_event["reason"] == "MOMENTUM_REVERSAL"
    entry_event = next(
        event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED"
    )
    assert entry_event["entry_signal"]["reason"] == "UPWARD_MOVEMENT_CONFIRMED"
    assert entry_event["entry_signal"]["window_change_pct"] >= 0.02
    monitoring = runner.snapshot()["monitoring"]
    assert len(monitoring) == 5
    assert monitoring[0]["decision"] == "BUILDING_PRICE_HISTORY"
    assert any(item["decision"] == "ENTRY_FILLED" for item in monitoring)
    assert all(item["nifty_price"] is not None for item in monitoring)


def test_completed_run_history_survives_service_restart(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    payload = {
        "id": "saved-run",
        "status": "COMPLETED",
        "started_at": "2026-08-27T09:00:00+00:00",
        "config": {"account_id": "momentum"},
        "fills": [{"symbol": "CIPLA"}],
        "session_pnl": -12.5,
    }
    store.save_momentum_run(payload)

    restored = MomentumRunnerService(ResearchStore(str(tmp_path / "research.db")))

    assert restored.get("saved-run")["fills"][0]["symbol"] == "CIPLA"
    assert restored.list()[0]["session_pnl"] == -12.5


def test_falling_nifty_blocks_an_entry_only_when_confirmation_is_required(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            entry_momentum_pct=0.02,
            require_nifty_confirmation=True,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=FallingNiftyMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
    )

    runner._scan()
    for _ in range(3):
        runner._poll()

    assert not accounts.get().orders
    assert runner.snapshot()["monitoring"][-1]["decision"] == ("NIFTY_SHORT_TERM_NOT_POSITIVE")


def test_low_relative_volume_is_rejected_during_scan(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default"),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=LowVolumeMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
    )

    runner._scan()

    snapshot = runner.snapshot()
    assert snapshot["candidates"] == []
    scan = next(event for event in snapshot["events"] if event["type"] == "SCAN")
    assert scan["low_volume_rejections"] == [{"symbol": "TEST", "relative_volume": 0.5}]


class RatchetMarket(RisingThenReversingMarket):
    """A rise that actually clears the round-trip cost, then gives it back."""

    def __init__(self) -> None:
        self.prices = iter([100.0, 100.0, 100.0, 100.4, 101.0, 101.3, 99.5, 99.4])


def test_ratchet_mode_rides_the_move_and_exits_on_the_trailing_stop(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(
            delivery_brokerage_flat=0,
            intraday_brokerage_bps=0,
            delivery_sell_dp_flat=0,
        ),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            duration_seconds=300,
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="ratchet-test",
    )

    runner._scan()
    for _ in range(6):
        runner._poll()

    snapshot = runner.snapshot()
    entry = next(event for event in snapshot["events"] if event["type"] == "ENTRY_FILLED")
    exit_event = next(event for event in snapshot["events"] if event["type"] == "EXIT_FILLED")
    assert entry["entry_signal"]["reason"] == "THREE_BAR_BREAKOUT"
    assert entry["entry_signal"]["structural_stop"] == 100.0
    assert entry["entry_signal"]["entry_threshold_source"] in {
        "ABSOLUTE_FLOOR",
        "COST_FLOOR",
        "STOCK_NOISE",
    }
    assert (
        entry["entry_signal"]["window_change_pct"] >= entry["entry_signal"]["entry_threshold_pct"]
    )
    assert entry["entry_signal"]["cost_floor_pct"] > 0
    assert exit_event["reason"] == "TRAILING_STOP"
    assert exit_event["exit_state"]["phase"] == "RIDE"
    assert accounts.get().positions["NSE_EQ|TEST"].quantity == 0


def test_a_ratchet_run_records_the_evidence_needed_to_tune_it(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=2,
        fee_schedule=FeeSchedule(),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=25_000,
            entry_momentum_pct=0.02,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="evidence-test",
    )

    runner._scan()
    for _ in range(6):
        runner._poll()

    snapshot = runner.snapshot()
    assert snapshot["cost_model"]["breakeven_pct"] == pytest.approx(0.2643, abs=0.001)
    assert all("entry_check" in row for row in snapshot["monitoring"])
    assert {row["decision"] for row in snapshot["decision_counts"]} == {
        row["decision"] for row in snapshot["monitoring"]
    }
    held = [row for row in snapshot["monitoring"] if row["exit"]]
    assert held, "an open position should record its exit state on every check"
    assert all(row["exit"]["stop_price"] > 0 for row in held)
    stops = [row["exit"]["stop_price"] for row in held]
    assert stops == sorted(stops)


def test_the_three_bar_verdict_is_recorded_even_when_another_gate_blocks(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))

    class FlatMarket(RisingThenReversingMarket):
        def __init__(self) -> None:
            self.prices = iter([100.0] * 8)

    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default", allocation_per_position=1_000),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=FlatMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="flat-test",
    )

    runner._scan()
    for _ in range(5):
        runner._poll()

    monitoring = runner.snapshot()["monitoring"]
    # The gates run cheapest first, so a blocking gate can mask the entry verdict.
    # Every observation still carries the three-bar reading it would have used.
    settled = [row for row in monitoring if len(row["entry_check"]["window"]) == 3]
    assert settled
    assert all(row["entry_check"]["reason"] == "FLAT_NO_TRADE" for row in settled)
    assert all(not row["entry_check"]["triggered"] for row in monitoring)
    assert not runner.snapshot()["open_positions"]


def test_a_small_negative_nifty_print_does_not_block_an_entry_by_default(tmp_path) -> None:
    """NIFTY is recorded as context but does not change today's entry strategy."""

    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(account_id="default", entry_momentum_pct=0.02),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=FallingNiftyMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="nifty-context-only",
    )

    runner._scan()
    for _ in range(3):
        runner._poll()

    snapshot = runner.snapshot()
    assert any(order.side == Side.BUY for order in accounts.get().orders.values())
    assert "NIFTY_SHORT_TERM_NOT_POSITIVE" not in {
        row["decision"] for row in snapshot["monitoring"]
    }
    assert all(row["nifty_price"] is not None for row in snapshot["monitoring"])
    assert snapshot["monitoring"][-1]["nifty_recent_15m_change_pct"] is not None


def test_a_zero_cost_configuration_still_gets_a_usable_ladder(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            minimum_cost_floor_pct=0.02,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="zero-cost",
    )

    runner._scan()
    for _ in range(6):
        runner._poll()

    entry = next(event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED")
    assert entry["entry_signal"]["cost_floor_pct"] == 0.02


class TimedTicksMarket(RisingThenReversingMarket):
    """Plays back a fixed (price, timestamp) tape, as a forward test would see live."""

    def __init__(self, ticks):
        self._ticks = iter(ticks)

    def ltp(self, keys):
        price, timestamp = next(self._ticks)
        return {key: Quote(key, price, timestamp=timestamp) for key in keys}


def _minute_forward_test_ticks(base):
    """Thirteen 5-second polls: a clean intraminute wick, then a real 3-minute rise.

    A bar only closes once a tick from the *next* bucket arrives, so a bar's
    close is whatever its last tick happened to be, not a tidy round number:

    Minute 0 (04:30:00-04:30:59): opens 100.0, wicks to 99.6, closes at 100.0
        when the 04:31:05 tick starts the next bucket. The wick is the low,
        which the close alone would hide.
    Minute 1 (04:31:00-04:31:59): opens 100.2, closes 100.6.
    Minute 2 (04:32:00-04:32:59): opens 100.8, closes 101.2 - closed by the
        04:33:02 tick, which itself opens minute 3.

    The three completed bars [100.0, 100.6, 101.2] first become available
    together on the poll that delivers the 04:33:02 tick - the 12th poll. A
    15-second, tick-based THREE_BAR check would have fired inside minute 0
    already, on the very first bounce back up from the wick.
    """

    ticks = [
        (100.0, base + timedelta(seconds=0)),
        (99.9, base + timedelta(seconds=5)),
        (99.6, base + timedelta(seconds=10)),  # the wick this test is built around
        (99.8, base + timedelta(seconds=15)),
        (100.0, base + timedelta(seconds=55)),  # last tick of minute 0
        (100.2, base + timedelta(seconds=65)),  # opens minute 1, closes minute 0 at 100.0
        (100.4, base + timedelta(seconds=90)),
        (100.6, base + timedelta(seconds=115)),  # last tick of minute 1
        (100.8, base + timedelta(seconds=125)),  # opens minute 2, closes minute 1 at 100.6
        (101.0, base + timedelta(seconds=150)),
        (101.2, base + timedelta(seconds=170)),  # last tick of minute 2
        (101.4, base + timedelta(seconds=182)),  # opens minute 3, closes minute 2 at 101.2
        (101.3, base + timedelta(seconds=185)),
    ]
    # A momentum runner's periodic _scan() calls MarketSurvey, which itself
    # makes one ltp() call - so the first tick off this tape is consumed before
    # the poll loop ever sees it. Duplicating it keeps every timestamp above
    # aligned to the poll index the comments describe.
    return [ticks[0]] + ticks


def test_a_one_minute_entry_timeframe_waits_for_full_bars_not_five_second_ticks(
    tmp_path,
) -> None:
    base = datetime(2026, 8, 28, 4, 30, 0, tzinfo=timezone.utc)
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.05,
            entry_timeframe_seconds=60.0,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=TimedTicksMarket(_minute_forward_test_ticks(base)),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="timeframe-test",
    )

    runner._scan()
    decisions = []
    for _ in range(13):
        runner._poll()
        decisions.append(runner.snapshot()["monitoring"][-1]["decision"])

    # No entry inside minute 0, even though price recovers to a local high by
    # tick 4 - a tick-based THREE_BAR check would have looked rising there.
    assert "ENTRY_FILLED" not in decisions[:4]

    # The entry fires on the 12th poll, the first one where three full
    # 1-minute bars are available, not on any of the five-second ticks before it.
    assert decisions[11] == "ENTRY_FILLED"
    assert "ENTRY_FILLED" not in decisions[:11]
    assert decisions[12] == "ALREADY_TRADED"

    entry = next(event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED")
    signal = entry["entry_signal"]
    # Direction is close-to-close across three completed 1-minute bars.
    assert signal["entry_check"]["window"] == pytest.approx([100.0, 100.6, 101.2])
    # But the stop seed is the LOW across those bars - the 99.6 wick inside the
    # first bar, invisible to a close-only reading and to the old tick-based rule.
    assert signal["structural_stop"] == pytest.approx(99.6)
    assert signal["entry_threshold_source"] in {"ABSOLUTE_FLOOR", "COST_FLOOR", "STOCK_NOISE"}


def test_a_new_bar_does_not_re_fire_the_same_entry_on_every_five_second_poll(
    tmp_path,
) -> None:
    base = datetime(2026, 8, 28, 4, 30, 0, tzinfo=timezone.utc)
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.05,
            entry_timeframe_seconds=60.0,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=TimedTicksMarket(_minute_forward_test_ticks(base)),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="no-refire-test",
    )

    runner._scan()
    for _ in range(13):
        runner._poll()

    fills = [event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED"]
    assert len(fills) == 1


def test_with_the_timeframe_off_bar_mode_reduces_to_the_original_tick_rule(
    tmp_path,
) -> None:
    """entry_timeframe_seconds=0 is the documented escape hatch back to 5s ticks."""

    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "research.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="timeframe-off-test",
    )

    runner._scan()
    for _ in range(6):
        runner._poll()

    entry = next(event for event in runner.snapshot()["events"] if event["type"] == "ENTRY_FILLED")
    assert entry["entry_signal"]["structural_stop"] == 100.0


def test_entry_timeframe_seconds_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        MomentumRunnerConfig(account_id="default", entry_timeframe_seconds=-1)


def test_a_runs_trace_is_streamed_into_its_own_table_not_the_run_blob(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default", allocation_per_position=1_000, entry_momentum_pct=0.02
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, StrategyService(accounts, store)),
        runner_id="stream-test",
        store=store,
    )

    runner._scan()
    for _ in range(6):
        runner._poll()
    runner._persist()

    assert store.observation_count("stream-test") == 6
    persisted = store.momentum_run("stream-test")
    # The blob carries the summary and a count, not thousands of rows.
    assert persisted["monitoring"] == []
    assert persisted["monitoring_count"] == 6
    assert persisted["monitoring_stored"] is True


def test_observations_are_appended_once_even_across_repeated_persists(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default", allocation_per_position=1_000, entry_momentum_pct=0.02
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, StrategyService(accounts, store)),
        runner_id="idempotent-test",
        store=store,
    )

    runner._scan()
    for _ in range(4):
        runner._poll()
    for _ in range(3):
        runner._persist()

    assert store.observation_count("idempotent-test") == 4


def test_a_finished_runs_trace_is_read_back_from_the_observation_table(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default", allocation_per_position=1_000, entry_momentum_pct=0.02
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=RatchetMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, StrategyService(accounts, store)),
        runner_id="hydrate-test",
        store=store,
    )
    runner._scan()
    for _ in range(5):
        runner._poll()
    runner._persist()

    # A fresh service has no live runner, so it must rebuild the trace from SQLite.
    restored = MomentumRunnerService(store)
    detail = restored.get("hydrate-test")
    assert len(detail["monitoring"]) == 5
    assert detail["monitoring_count"] == 5
    assert all("entry_check" in row for row in detail["monitoring"])

    summaries = restored.list()
    assert summaries[0]["monitoring"] == []
    assert summaries[0]["monitoring_count"] == 5
    assert restored.get("hydrate-test", include_monitoring=False)["monitoring"] == []


def test_one_account_allows_one_run_but_other_accounts_run_alongside(tmp_path) -> None:
    store = ResearchStore(str(tmp_path / "research.db"))
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    accounts.create("variant-b", "Second arm", 100_000)
    service = MomentumRunnerService(store)

    def build(runner_id, account_id):
        return MomentumReversalRunner(
            config=MomentumRunnerConfig(account_id=account_id, allocation_per_position=1_000),
            instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
            market_data=RatchetMarket(),
            accounts=accounts,
            coordinator=MarketCoordinator(accounts, StrategyService(accounts, store)),
            runner_id=runner_id,
            store=store,
        )

    first, second = build("arm-a", "default"), build("arm-b", "variant-b")
    for runner in (first, second):
        runner._status = "RUNNING"
        service._runners[runner.id] = runner

    assert service.has_active("default") is True
    assert service.has_active("variant-b") is True
    assert service.has_active("untouched") is False
    assert len(service.active()) == 2


class ReenterMarket(RisingThenReversingMarket):
    """Rise, exit on the fade, then rise again - a full-session re-entry setup.

    Emits stepped 5-second timestamps so the wall clock actually advances between
    polls; a tight test loop otherwise stamps every quote at the same instant and
    no cooldown could ever elapse. The first tick is consumed by the opening scan.
    """

    def __init__(self):
        base = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)
        prices = [
            100.0,  # consumed by _scan()
            100.0,
            100.0,
            100.0,
            100.4,
            101.0,
            101.3,
            99.5,  # entry then exit
            99.5,
            99.5,
            100.4,
            101.2,
            101.5,  # second rise, later
        ]
        self._ticks = iter(
            (price, base + timedelta(seconds=5 * i)) for i, price in enumerate(prices)
        )

    def ltp(self, keys):
        price, timestamp = next(self._ticks)
        return {key: Quote(key, price, timestamp=timestamp) for key in keys}


def test_a_cooldown_lets_a_full_session_run_re_enter_a_stock(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "r.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            entry_cost_multiple=0,  # isolate cooldown from the entry-bar scaling
            entry_noise_multiple=0,
            confirmation_samples=1,
            reentry_cooldown_seconds=10,  # exit near t=30s, re-entry near t=50s
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=ReenterMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="cooldown-test",
    )
    runner._scan()
    for _ in range(12):
        runner._poll()

    entries = [e for e in runner.snapshot()["events"] if e["type"] == "ENTRY_FILLED"]
    assert len(entries) >= 2, "the stock should be tradeable again after the cooldown"


def test_without_a_cooldown_a_stock_is_traded_at_most_once(tmp_path) -> None:
    accounts = PaperAccountManager(
        repository=InMemoryRepository(),
        initial_cash=100_000,
        slippage_bps=0,
        fee_schedule=FeeSchedule(brokerage_bps=0),
        risk_limits=RiskLimits(),
    )
    strategies = StrategyService(accounts, ResearchStore(str(tmp_path / "r.db")))
    runner = MomentumReversalRunner(
        config=MomentumRunnerConfig(
            account_id="default",
            allocation_per_position=1_000,
            entry_momentum_pct=0.02,
            confirmation_samples=1,
        ),
        instruments=[SurveyInstrument("TEST", "NSE_EQ|TEST")],
        market_data=ReenterMarket(),
        accounts=accounts,
        coordinator=MarketCoordinator(accounts, strategies),
        runner_id="no-cooldown-test",
    )
    runner._scan()
    for _ in range(12):
        runner._poll()

    entries = [e for e in runner.snapshot()["events"] if e["type"] == "ENTRY_FILLED"]
    assert len(entries) == 1
    decisions = {row["decision"] for row in runner.snapshot()["monitoring"]}
    assert "ALREADY_TRADED" in decisions
