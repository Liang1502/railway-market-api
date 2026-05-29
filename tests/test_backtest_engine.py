"""
Tests for backtest.py pure functions (no SDK / external API calls).

Covered:
  - build_yoy_schedule / yoy_at_date
  - build_taiex_schedule / is_taiex_bullish
  - simulate_trade  (gap-skip, stop-loss, take-profit, time-stop, ATR)
  - apply_position_limit
  - compute_max_drawdown / compute_max_concurrent
  - analyze
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from backtest import (
    BacktestConfig,
    Trade,
    analyze,
    apply_position_limit,
    build_taiex_schedule,
    build_yoy_schedule,
    compute_max_concurrent,
    compute_max_drawdown,
    is_taiex_bullish,
    simulate_trade,
    yoy_at_date,
)

# ── Helpers ────────────────────────────────────────────────────────────────

_DEFAULT_CFG = BacktestConfig()


def _build_df(ohlc_rows: list[tuple]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from (open, high, low, close) tuples."""
    n = len(ohlc_rows)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [r[0] for r in ohlc_rows],
            "high": [r[1] for r in ohlc_rows],
            "low": [r[2] for r in ohlc_rows],
            "close": [r[3] for r in ohlc_rows],
            "volume": [10_000] * n,
        }
    )


def _flat_df(n: int = 30, price: float = 100.0) -> pd.DataFrame:
    """Flat DataFrame: open=close=price, high=price+2, low=price-2."""
    return _build_df([(price, price + 2, price - 2, price)] * n)


def _make_trade(**overrides) -> Trade:
    defaults = dict(
        symbol="2330",
        score=15,
        signal_date="2024-01-04",
        entry_date="2024-01-05",
        exit_date="2024-01-12",
        entry_price=100.0,
        exit_price=108.0,
        hold_days=6,
        gross_return=0.08,
        net_return=0.0762,
        cost=0.0038,
        exit_reason="take_profit",
        is_daytrade=False,
        yoy=25.0,
        industry="電子",
    )
    defaults.update(overrides)
    return Trade(**defaults)


# ── build_yoy_schedule ────────────────────────────────────────────────────

class TestBuildYoySchedule:
    def test_empty_rows_returns_empty(self):
        assert build_yoy_schedule([], lag_days=15) == []

    def test_uses_cached_yoy_pct(self):
        rows = [{"date": "2024-01-01", "revenue": 1000, "yoy_pct": 25.0}]
        sched = build_yoy_schedule(rows, lag_days=0)
        assert len(sched) == 1
        assert sched[0][1] == pytest.approx(25.0)

    def test_applies_lag_days_to_release_date(self):
        rows = [{"date": "2024-01-01", "yoy_pct": 10.0}]
        sched = build_yoy_schedule(rows, lag_days=15)
        expected_release = pd.Timestamp("2024-01-01") + timedelta(days=15)
        assert sched[0][0] == expected_release

    def test_calculates_yoy_from_prior_year_when_no_cache(self):
        rows = [
            {"date": "2023-01-01", "revenue": 800},
            {"date": "2024-01-01", "revenue": 1000},
        ]
        sched = build_yoy_schedule(rows, lag_days=0)
        # 2023 row has no prior year → skipped; 2024 row computes from 2023
        assert len(sched) == 1
        assert sched[0][1] == pytest.approx(25.0)  # (1000-800)/800 * 100

    def test_skips_row_without_prior_year_data(self):
        # Only one row, yoy_pct=None, no previous year → skip
        rows = [{"date": "2023-06-01", "revenue": 500}]
        sched = build_yoy_schedule(rows, lag_days=0)
        assert sched == []

    def test_result_is_sorted_by_release_date(self):
        rows = [
            {"date": "2024-03-01", "yoy_pct": 10.0},
            {"date": "2024-01-01", "yoy_pct": 20.0},
        ]
        sched = build_yoy_schedule(rows, lag_days=0)
        dates = [s[0] for s in sched]
        assert dates == sorted(dates)


# ── yoy_at_date ───────────────────────────────────────────────────────────

class TestYoyAtDate:
    def _sched(self):
        return [
            (pd.Timestamp("2024-01-16"), 25.0),
            (pd.Timestamp("2024-03-16"), 10.0),
        ]

    def test_returns_none_for_empty_schedule(self):
        assert yoy_at_date([], pd.Timestamp("2024-06-01")) is None

    def test_returns_none_before_first_release(self):
        assert yoy_at_date(self._sched(), pd.Timestamp("2024-01-01")) is None

    def test_returns_yoy_on_exact_release_date(self):
        assert yoy_at_date(self._sched(), pd.Timestamp("2024-01-16")) == pytest.approx(25.0)

    def test_returns_first_yoy_between_releases(self):
        assert yoy_at_date(self._sched(), pd.Timestamp("2024-02-01")) == pytest.approx(25.0)

    def test_returns_latest_yoy_after_second_release(self):
        assert yoy_at_date(self._sched(), pd.Timestamp("2024-04-01")) == pytest.approx(10.0)


# ── TAIEX filter ──────────────────────────────────────────────────────────

class TestTaiexFilter:
    def _taiex_df(self, close: float, ma20: float, ma60: float) -> pd.DataFrame:
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        return pd.DataFrame({"date": dates, "close": [close] * 3,
                             "MA20": [ma20] * 3, "MA60": [ma60] * 3})

    def test_is_bullish_returns_true_when_schedule_empty(self):
        assert is_taiex_bullish([], pd.Timestamp("2024-01-10")) is True

    def test_is_bullish_when_close_above_both_mas(self):
        df = self._taiex_df(close=110.0, ma20=105.0, ma60=100.0)
        sched = build_taiex_schedule(df)
        assert is_taiex_bullish(sched, pd.Timestamp("2024-01-10")) is True

    def test_is_bearish_when_close_below_ma20(self):
        df = self._taiex_df(close=95.0, ma20=100.0, ma60=90.0)
        sched = build_taiex_schedule(df)
        assert is_taiex_bullish(sched, pd.Timestamp("2024-01-10")) is False

    def test_is_bullish_defaults_true_before_schedule_data(self):
        df = self._taiex_df(close=110.0, ma20=105.0, ma60=100.0)
        sched = build_taiex_schedule(df)
        # Query before any schedule entry
        very_early = pd.Timestamp("2020-01-01")
        assert is_taiex_bullish(sched, very_early) is True


# ── simulate_trade ────────────────────────────────────────────────────────

class TestSimulateTrade:
    """
    All tests use entry_idx=5 with a 30-row DataFrame so there is
    enough room for the default max_hold_days=20.
    prev_close = df.iloc[4]["close"] = 100.0 (flat rows).
    entry_price = df.iloc[5]["open"] (set per test).
    """

    def test_returns_none_for_zero_entry_idx(self):
        df = _flat_df()
        assert simulate_trade(df, 0, "2024-01-08", "T", 15, None, _DEFAULT_CFG) is None

    def test_returns_none_when_entry_idx_out_of_bounds(self):
        df = _flat_df(n=5)
        assert simulate_trade(df, 5, "2024-01-08", "T", 15, None, _DEFAULT_CFG) is None

    def test_gap_skip_returns_none(self):
        # open at entry = 104.5 → 4.5% above prev_close=100 → exceeds gap_skip=3%
        rows = [(100, 102, 98, 100)] * 5 + [(104.5, 106, 103, 104)] + [(100, 102, 98, 100)] * 24
        df = _build_df(rows)
        cfg = BacktestConfig(gap_skip=0.03)
        assert simulate_trade(df, 5, "2024-01-08", "T", 15, None, cfg) is None

    def test_stop_loss_same_day(self):
        # entry=100, stop=95, day0 low=90 → stop hit on entry day
        rows = [(100, 102, 98, 100)] * 5 + [(100, 101, 90, 95)] + [(100, 102, 98, 100)] * 24
        df = _build_df(rows)
        cfg = BacktestConfig(stop_loss=0.05, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t is not None
        assert t.exit_reason == "stop_loss_sameday"
        assert t.is_daytrade is True
        assert t.gross_return == pytest.approx(-0.05, abs=1e-6)
        assert t.cost == cfg.cost_daytrade

    def test_stop_loss_gap_down_next_day(self):
        # day0 fine, day1 opens below stop → stop_loss_gap
        rows = (
            [(100, 102, 98, 100)] * 5   # rows 0-4 (prev_close at 4 = 100)
            + [(100, 102, 98, 100)]     # row 5: entry day, all fine
            + [(90, 91, 89, 90)]        # row 6: opens below stop 95
            + [(100, 102, 98, 100)] * 23
        )
        df = _build_df(rows)
        cfg = BacktestConfig(stop_loss=0.05, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t is not None
        assert t.exit_reason == "stop_loss_gap"
        assert t.hold_days == 2
        assert t.is_daytrade is False

    def test_take_profit_both_levels_on_same_day(self):
        # high=120 on entry day: tp1=108, tp2=115 both hit
        rows = [(100, 102, 98, 100)] * 5 + [(100, 120, 98, 115)] + [(100, 102, 98, 100)] * 24
        df = _build_df(rows)
        cfg = BacktestConfig(take_profit_1=0.08, take_profit_2=0.15, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t is not None
        assert t.exit_reason == "take_profit_sameday"
        assert t.is_daytrade is True
        # gross = 0.5 * 0.08 + 0.5 * 0.15 = 0.115
        assert t.gross_return == pytest.approx(0.115, abs=1e-6)

    def test_take_profit_across_two_days(self):
        # day0: high=112 → tp1 hit (not tp2), advance to phase2
        # day1: open=101 (above phase-2 break-even stop=100), high=120 → tp2 hit
        rows = (
            [(100, 102, 98, 100)] * 5
            + [(100, 112, 98, 110)]   # row5: tp1 hit, phase2 begins
            + [(101, 120, 101, 115)]  # row6: open+low above BE=100, high reaches tp2
            + [(100, 102, 98, 100)] * 23
        )
        df = _build_df(rows)
        cfg = BacktestConfig(take_profit_1=0.08, take_profit_2=0.15, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t is not None
        assert t.exit_reason == "take_profit"
        assert t.hold_days == 2
        assert t.is_daytrade is False
        assert t.cost == cfg.cost_swing
        assert t.gross_return == pytest.approx(0.115, abs=1e-6)

    def test_time_stop_after_max_hold_days(self):
        # Use extreme stop/tp so nothing triggers; close stays at 100.
        cfg = BacktestConfig(max_hold_days=5, stop_loss=0.50,
                              take_profit_1=0.90, take_profit_2=1.80, gap_skip=0.0)
        df = _flat_df(n=30, price=100.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t is not None
        assert t.exit_reason == "time_stop"
        assert t.hold_days == 5
        assert t.gross_return == pytest.approx(0.0, abs=1e-6)

    def test_atr_dynamic_stop_widens_beyond_base(self):
        """With ATR=10 and mult=1.5: effective stop = max(0.05, 0.15) = 0.15 → price=85.
        Day0 low=90 → base stop (95) would fire, but ATR stop (85) does not.
        Day1 high=120 → both TPs hit, trade continues to take_profit instead.
        """
        rows = (
            [(100, 102, 98, 100)] * 5
            + [(100, 102, 90, 100)]   # row5: low=90, above ATR-stop=85
            + [(100, 120, 98, 115)]   # row6: tp1+tp2 hit
            + [(100, 102, 98, 100)] * 23
        )
        df = _build_df(rows)
        cfg = BacktestConfig(stop_loss=0.05, atr_stop_mult=1.5,
                              take_profit_1=0.08, take_profit_2=0.15, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg, atr=10.0)
        assert t is not None
        # With base stop (0.05) it would exit on day0; with ATR stop (0.15) it continues
        assert t.exit_reason != "stop_loss_sameday"

    def test_daytrade_cost_applied_for_sameday_exit(self):
        rows = [(100, 102, 98, 100)] * 5 + [(100, 101, 90, 95)] + [(100, 102, 98, 100)] * 24
        df = _build_df(rows)
        cfg = BacktestConfig(stop_loss=0.05, gap_skip=0.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t.is_daytrade is True
        assert t.cost == pytest.approx(cfg.cost_daytrade)
        assert t.net_return == pytest.approx(t.gross_return - cfg.cost_daytrade)

    def test_swing_cost_applied_for_multiday_exit(self):
        cfg = BacktestConfig(max_hold_days=3, stop_loss=0.50,
                              take_profit_1=0.90, take_profit_2=1.80, gap_skip=0.0)
        df = _flat_df(n=30, price=100.0)
        t = simulate_trade(df, 5, "2024-01-08", "TEST", 15, None, cfg)
        assert t.is_daytrade is False
        assert t.cost == pytest.approx(cfg.cost_swing)


# ── apply_position_limit ──────────────────────────────────────────────────

class TestApplyPositionLimit:
    def test_empty_list_returns_empty(self):
        assert apply_position_limit([], max_concurrent=5) == []

    def test_within_limit_all_trades_accepted(self):
        trades = [
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-10", score=15),
            _make_trade(entry_date="2024-01-15", exit_date="2024-01-20", score=14),
        ]
        result = apply_position_limit(trades, max_concurrent=2)
        assert len(result) == 2

    def test_exceeds_limit_excess_rejected(self):
        # 3 trades all entering same day, limit=2
        trades = [
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-15", score=10),
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-15", score=12),
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-15", score=15),
        ]
        result = apply_position_limit(trades, max_concurrent=2)
        assert len(result) == 2

    def test_higher_score_preferred_when_limit_exceeded(self):
        trades = [
            _make_trade(symbol="A", entry_date="2024-01-05", exit_date="2024-01-15", score=10),
            _make_trade(symbol="B", entry_date="2024-01-05", exit_date="2024-01-15", score=15),
            _make_trade(symbol="C", entry_date="2024-01-05", exit_date="2024-01-15", score=12),
        ]
        result = apply_position_limit(trades, max_concurrent=2)
        symbols = {t.symbol for t in result}
        assert "B" in symbols   # score=15 always included
        assert "C" in symbols   # score=12 second best
        assert "A" not in symbols

    def test_sequential_non_overlapping_trades_all_accepted(self):
        # Trade2 starts after Trade1 exits
        trades = [
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-08"),
            _make_trade(entry_date="2024-01-10", exit_date="2024-01-15"),
        ]
        result = apply_position_limit(trades, max_concurrent=1)
        assert len(result) == 2


# ── compute_max_drawdown ──────────────────────────────────────────────────

class TestComputeMaxDrawdown:
    def test_strictly_rising_equity_has_zero_drawdown(self):
        equity = pd.Series([1_000_000, 1_010_000, 1_020_000, 1_030_000])
        assert compute_max_drawdown(equity) == pytest.approx(0.0, abs=1e-6)

    def test_drawdown_calculation_correct(self):
        # Peak 1_050_000, trough 1_000_000 → MDD = (1M-1.05M)/1.05M ≈ -4.76%
        equity = pd.Series([1_000_000, 1_010_000, 1_050_000, 1_000_000])
        mdd = compute_max_drawdown(equity)
        expected = (1_000_000 - 1_050_000) / 1_050_000  # ≈ -0.04762
        assert mdd == pytest.approx(expected, rel=0.001)

    def test_drawdown_is_negative(self):
        equity = pd.Series([1_000_000, 900_000, 950_000])
        assert compute_max_drawdown(equity) < 0


# ── compute_max_concurrent ────────────────────────────────────────────────

class TestComputeMaxConcurrent:
    def test_empty_df_returns_zero(self):
        import pandas as pd
        assert compute_max_concurrent(pd.DataFrame()) == 0

    def test_non_overlapping_trades(self):
        from dataclasses import asdict
        trades = [
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-08"),
            _make_trade(entry_date="2024-01-10", exit_date="2024-01-15"),
        ]
        df = pd.DataFrame([dict(entry_date=t.entry_date, exit_date=t.exit_date) for t in trades])
        assert compute_max_concurrent(df) == 1

    def test_overlapping_trades(self):
        trades = [
            _make_trade(entry_date="2024-01-05", exit_date="2024-01-12"),
            _make_trade(entry_date="2024-01-08", exit_date="2024-01-15"),
            _make_trade(entry_date="2024-01-20", exit_date="2024-01-25"),
        ]
        df = pd.DataFrame([dict(entry_date=t.entry_date, exit_date=t.exit_date) for t in trades])
        assert compute_max_concurrent(df) == 2


# ── analyze ───────────────────────────────────────────────────────────────

class TestAnalyze:
    def test_no_trades_returns_no_trades_note(self):
        result = analyze([], _DEFAULT_CFG)
        assert result["total_trades"] == 0
        assert "note" in result

    def test_win_rate_calculation(self):
        trades = [
            _make_trade(net_return=0.10),  # win
            _make_trade(net_return=0.08),  # win
            _make_trade(net_return=-0.05), # loss
            _make_trade(net_return=-0.04), # loss
        ]
        result = analyze(trades, _DEFAULT_CFG)
        assert result["win_rate"] == pytest.approx(0.50)
        assert result["total_trades"] == 4

    def test_daytrade_ratio_correct(self):
        trades = [
            _make_trade(is_daytrade=True),
            _make_trade(is_daytrade=True),
            _make_trade(is_daytrade=False),
            _make_trade(is_daytrade=False),
        ]
        result = analyze(trades, _DEFAULT_CFG)
        assert result["daytrade_ratio"] == pytest.approx(0.50)

    def test_exit_breakdown_present(self):
        trades = [
            _make_trade(exit_reason="take_profit"),
            _make_trade(exit_reason="take_profit"),
            _make_trade(exit_reason="stop_loss"),
        ]
        result = analyze(trades, _DEFAULT_CFG)
        breakdown = result["exit_breakdown"]
        assert breakdown.get("take_profit") == 2
        assert breakdown.get("stop_loss") == 1

    def test_profit_factor_ratio(self):
        trades = [
            _make_trade(net_return=0.10),   # gross wins = 0.10
            _make_trade(net_return=-0.05),  # gross losses = 0.05
        ]
        result = analyze(trades, _DEFAULT_CFG)
        # profit_factor = 0.10 / 0.05 = 2.0
        assert result["profit_factor"] == pytest.approx(2.0, rel=0.01)

    def test_avg_return_matches_mean(self):
        trades = [
            _make_trade(net_return=0.10),
            _make_trade(net_return=0.06),
            _make_trade(net_return=-0.04),
        ]
        result = analyze(trades, _DEFAULT_CFG)
        expected_avg = (0.10 + 0.06 - 0.04) / 3 * 100
        assert result["avg_return_pct"] == pytest.approx(expected_avg, rel=0.01)

    def test_max_drawdown_is_computed_from_equity_curve(self):
        cfg = BacktestConfig(starting_capital=1_000_000, capital_per_trade=100_000)
        trades = [
            _make_trade(net_return=0.10),
            _make_trade(net_return=-0.20),
        ]

        result = analyze(trades, cfg)

        # Equity: 1,010,000 -> 990,000, drawdown = -20,000 / 1,010,000.
        assert result["max_drawdown_pct"] == pytest.approx(-1.9802, rel=0.001)

    def test_result_contains_all_key_fields(self):
        trades = [_make_trade()]
        result = analyze(trades, _DEFAULT_CFG)
        required = {
            "total_trades", "win_rate", "avg_win_pct", "avg_loss_pct",
            "profit_factor", "total_pnl", "annual_return_pct",
            "max_drawdown_pct", "sharpe_ratio", "daytrade_ratio",
            "avg_hold_days", "exit_breakdown", "by_score_bucket",
        }
        assert required.issubset(result.keys())
