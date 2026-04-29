"""
Tests for the day-trading signal logic in test_analysis_data.py.

Covers:
  - Utility helpers: _get, _to_float, _to_list
  - extract_analysis_data: all decision branches, score, entry triggers
  - extract_investment_data: strong-buy and default paths
"""
import time
import pytest
import analysis as mod
from analysis import (
    _get,
    _to_float,
    _to_list,
    extract_analysis_data,
    extract_investment_data,
)


# ── Shared fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_entry_memory():
    """Reset global entry_memory between every test for isolation."""
    mod.entry_memory.clear()
    yield
    mod.entry_memory.clear()


def _ticker(
    symbol="2330",
    last=100.0,
    high=105.0,
    low=95.0,
    avg=100.0,
    change_pct=0.5,
    volume=10000,
) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": last,
        "highPrice": high,
        "lowPrice": low,
        "avgPrice": avg,
        "changePercent": change_pct,
        "total": {"tradeVolume": volume},
    }


def _quote(bid_price=99.5, bid_size=100, ask_price=100.5, ask_size=100) -> dict:
    return {
        "bids": [{"price": bid_price, "size": bid_size}],
        "asks": [{"price": ask_price, "size": ask_size}],
    }


# ── _get helper ────────────────────────────────────────────────────────────

class TestGetHelper:
    def test_dict_key_access(self):
        assert _get({"a": 1}, "a") == 1

    def test_object_attribute_access(self):
        class Obj:
            x = 42
        assert _get(Obj(), "x") == 42

    def test_nested_dict_access(self):
        assert _get({"total": {"tradeVolume": 500}}, "total", "tradeVolume") == 500

    def test_list_index_access(self):
        assert _get([10, 20, 30], 1) == 20

    def test_missing_key_returns_default(self):
        assert _get({"a": 1}, "b") is None
        assert _get({"a": 1}, "b", default=0) == 0

    def test_none_input_returns_default(self):
        assert _get(None, "key") is None

    def test_list_out_of_bounds_returns_default(self):
        assert _get([1, 2], 5) is None


# ── _to_float ──────────────────────────────────────────────────────────────

class TestToFloat:
    def test_numeric_string(self):
        assert _to_float("3.14") == pytest.approx(3.14)

    def test_integer(self):
        assert _to_float(42) == 42.0

    def test_none_returns_none(self):
        assert _to_float(None) is None

    def test_invalid_string_returns_none(self):
        assert _to_float("abc") is None

    def test_zero(self):
        assert _to_float(0) == 0.0


# ── _to_list ───────────────────────────────────────────────────────────────

class TestToList:
    def test_list_of_dicts_preserved(self):
        items = [{"price": 100, "size": 10}, {"price": 99, "size": 20}]
        result = _to_list(items)
        assert result == items

    def test_list_of_objects_converted(self):
        class Item:
            def __init__(self, price, size):
                self.price = price
                self.size = size
        result = _to_list([Item(100.0, 5)])
        assert result[0]["price"] == 100.0
        assert result[0]["size"] == 5

    def test_none_returns_empty_list(self):
        assert _to_list(None) == []

    def test_empty_list(self):
        assert _to_list([]) == []


# ── extract_analysis_data – decisions ─────────────────────────────────────

class TestExtractAnalysisDataDecision:
    def test_no_trade_when_volume_is_zero(self):
        t = _ticker(volume=0)
        q = _quote()
        result = extract_analysis_data(t, q)
        assert result["decision"] == "no_trade"

    def test_no_trade_when_no_bids(self):
        t = _ticker(volume=5000)
        q = {"bids": [], "asks": [{"price": 100.5, "size": 100}]}
        result = extract_analysis_data(t, q)
        assert result["decision"] == "no_trade"

    def test_no_trade_when_no_asks(self):
        t = _ticker(volume=5000)
        q = {"bids": [{"price": 99.5, "size": 100}], "asks": []}
        result = extract_analysis_data(t, q)
        assert result["decision"] == "no_trade"

    def test_avoid_long_over_7_percent(self):
        t = _ticker(last=100.0, avg=92.0, change_pct=8.0, volume=5000)
        q = _quote(bid_size=200, ask_size=100)
        result = extract_analysis_data(t, q)
        assert result["decision"] == "avoid_long"

    def test_avoid_long_fomo_extreme(self):
        # vwap_distance = (last - avg) / avg * 100 >= 2.0
        t = _ticker(last=104.0, avg=100.0, change_pct=4.0, volume=5000)
        # bid dominates to push toward "buy"
        q = _quote(bid_size=300, ask_size=100)
        result = extract_analysis_data(t, q)
        assert result["decision"] == "avoid_long"

    def test_bull_trap_detected(self):
        # is_near_high: (high - last) / last <= 0.005  → last ≈ high
        # dominance = "sell", change_pct < 0.5
        t = _ticker(last=100.0, high=100.3, low=95.0, avg=99.0, change_pct=0.3, volume=5000)
        q = _quote(bid_size=80, ask_size=200)  # sell dominance
        result = extract_analysis_data(t, q)
        assert result["trap"] == "bull_trap"
        assert result["decision"] == "avoid_long"

    def test_bear_trap_detected(self):
        # is_near_low: (last - low) / last <= 0.005  → last ≈ low
        # dominance = "buy", change_pct > -0.5
        t = _ticker(last=100.0, high=105.0, low=99.7, avg=101.0, change_pct=-0.2, volume=5000)
        q = _quote(bid_size=200, ask_size=80)  # buy dominance
        result = extract_analysis_data(t, q)
        assert result["trap"] == "bear_trap"
        assert result["decision"] == "avoid_short"

    def test_long_possible_via_buy_pressure(self):
        # dominance = "buy", pressure_ratio < 0.8, last > mid, trend = "up"
        # last=102.1, avg=101.5 → trend=up, vwap_dist=0.59% (no fomo)
        # bid: 200+300=500, ask: 100+80=180, pressure_ratio=0.36 < 0.8
        # mid = (101.5+102.5)/2 = 102.0, last=102.1 > 102.0
        t = _ticker(last=102.1, high=106.0, low=99.0, avg=101.5, change_pct=2.0, volume=5000)
        q = {
            "bids": [{"price": 101.5, "size": 200}, {"price": 101.0, "size": 300}],
            "asks": [{"price": 102.5, "size": 100}, {"price": 103.0, "size": 80}],
        }
        result = extract_analysis_data(t, q)
        assert result["decision"] == "long_possible"

    def test_short_possible_via_sell_pressure(self):
        # dominance = "sell", pressure_ratio > 1.2, last < mid, trend = "down"
        # last=97.9, avg=100.0 → trend=down
        # bid: 80=80, ask: 200+150=350, pressure_ratio=350/80=4.375 > 1.2
        # mid = (97.5+98.5)/2 = 98.0, last=97.9 < 98.0
        t = _ticker(last=97.9, high=100.0, low=95.0, avg=100.0, change_pct=-2.0, volume=5000)
        q = {
            "bids": [{"price": 97.5, "size": 80}],
            "asks": [{"price": 98.5, "size": 200}, {"price": 99.0, "size": 150}],
        }
        result = extract_analysis_data(t, q)
        assert result["decision"] == "short_possible"

    def test_observe_default_neutral_conditions(self):
        # equal bid/ask sizes → dominance neutral, pressure_ratio not significant
        t = _ticker(last=100.0, avg=100.0, change_pct=0.0, volume=5000)
        q = _quote(bid_size=100, ask_size=100)
        result = extract_analysis_data(t, q)
        assert result["decision"] == "observe"

    def test_bullish_reversal_signal(self):
        # trend=down, change_pct>1.2, last>mid, dominance=buy, distance_from_mid>1.5
        # last=102.0, avg=105.0 → trend=down (last < avg)
        # mid=(99.5+100.5)/2=100.0, distance=102-100=2 > 1.5
        t = _ticker(last=102.0, high=103.0, low=96.0, avg=105.0, change_pct=2.0, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 300}],
            "asks": [{"price": 100.5, "size": 100}],
        }
        result = extract_analysis_data(t, q)
        assert result["reversal"] == "bullish_reversal"
        assert result["decision"] == "long_possible"

    def test_bearish_reversal_signal(self):
        # trend=up, change_pct<-1.2, last<mid, dominance=sell, distance_from_mid<-1.5
        # last=98.0, avg=95.0 → trend=up (last > avg)
        # mid=(99.5+100.5)/2=100.0, distance=98-100=-2 < -1.5
        t = _ticker(last=98.0, high=101.0, low=97.0, avg=95.0, change_pct=-2.0, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 80}],
            "asks": [{"price": 100.5, "size": 300}],
        }
        result = extract_analysis_data(t, q)
        assert result["reversal"] == "bearish_reversal"
        assert result["decision"] == "short_possible"


# ── extract_analysis_data – score ─────────────────────────────────────────

class TestExtractAnalysisDataScore:
    def test_score_is_clamped_between_0_and_100(self):
        t = _ticker(last=100.0, avg=100.0, change_pct=0.0, volume=5000)
        q = _quote()
        result = extract_analysis_data(t, q)
        assert 0 <= result["score"] <= 100

    def test_score_higher_with_positive_change_and_uptrend(self):
        t_up = _ticker(last=102.0, avg=100.0, change_pct=2.0, volume=5000)
        t_down = _ticker(last=98.0, avg=100.0, change_pct=-2.0, volume=5000)
        q = _quote()
        score_up = extract_analysis_data(t_up, q)["score"]
        score_down = extract_analysis_data(t_down, q)["score"]
        assert score_up > score_down

    def test_low_pressure_ratio_increases_score(self):
        # Low pressure_ratio (<0.7) means strong buying; adds +10
        t = _ticker(last=100.0, avg=100.0, change_pct=0.5, volume=5000)
        q_strong_buy = {
            "bids": [{"price": 99.5, "size": 500}],
            "asks": [{"price": 100.5, "size": 100}],
        }
        q_neutral = _quote(bid_size=100, ask_size=100)
        s_buy = extract_analysis_data(t, q_strong_buy)["score"]
        s_neutral = extract_analysis_data(t, q_neutral)["score"]
        assert s_buy > s_neutral

    def test_bull_trap_decreases_score(self):
        # bull_trap subtracts 15
        t_trap = _ticker(last=100.0, high=100.3, low=95.0, avg=99.0, change_pct=0.3, volume=5000)
        t_normal = _ticker(last=100.0, high=105.0, low=95.0, avg=99.0, change_pct=0.3, volume=5000)
        q_sell = _quote(bid_size=80, ask_size=200)
        score_trap = extract_analysis_data(t_trap, q_sell)["score"]
        score_normal = extract_analysis_data(t_normal, q_sell)["score"]
        assert score_trap < score_normal


# ── extract_analysis_data – entry triggers ─────────────────────────────────

class TestExtractAnalysisDataEntryTriggers:
    """
    Trigger fires when the same condition appears >= 2 times within 60 seconds.
    """

    def _long_ticker_quote(self):
        t = _ticker(last=102.1, high=106.0, low=99.0, avg=101.5, change_pct=2.0, volume=5000)
        q = {
            "bids": [{"price": 101.5, "size": 200}, {"price": 101.0, "size": 300}],
            "asks": [{"price": 102.5, "size": 100}, {"price": 103.0, "size": 80}],
        }
        return t, q

    def _short_ticker_quote(self):
        t = _ticker(last=97.9, high=100.0, low=95.0, avg=100.0, change_pct=-2.0, volume=5000)
        q = {
            "bids": [{"price": 97.5, "size": 80}],
            "asks": [{"price": 98.5, "size": 200}, {"price": 99.0, "size": 150}],
        }
        return t, q

    def test_long_trigger_not_fired_after_one_call(self):
        t, q = self._long_ticker_quote()
        result = extract_analysis_data(t, q)
        assert result["entry_signal"]["long_trigger"] is False

    def test_long_trigger_fires_after_two_consecutive_calls(self):
        t, q = self._long_ticker_quote()
        extract_analysis_data(t, q)          # call 1 → counter = 1
        result = extract_analysis_data(t, q) # call 2 → counter = 2
        assert result["entry_signal"]["long_trigger"] is True
        assert result["entry_signal"]["long_reason"] is not None

    def test_short_trigger_fires_after_two_consecutive_calls(self):
        t, q = self._short_ticker_quote()
        extract_analysis_data(t, q)
        result = extract_analysis_data(t, q)
        assert result["entry_signal"]["short_trigger"] is True

    def test_counter_resets_when_condition_disappears(self):
        t_long, q_long = self._long_ticker_quote()
        extract_analysis_data(t_long, q_long)  # long counter = 1

        # Now use neutral conditions (no long_raw) → counter resets to 0
        t_neutral = _ticker(volume=5000)
        q_neutral = _quote()
        result = extract_analysis_data(t_neutral, q_neutral)
        assert result["entry_signal"]["long_trigger"] is False
        assert mod.entry_memory.get("2330", {}).get("long", 0) == 0

    def test_counter_resets_after_time_window_expires(self):
        t, q = self._long_ticker_quote()
        # Pre-seed with a stale timestamp (61s ago)
        stale = time.time() - 61
        mod.entry_memory["2330"] = {
            "short": 0, "long": 3,
            "short_time": None, "long_time": stale,
        }
        result = extract_analysis_data(t, q)
        # Counter was > 2 but time expired → resets to 1, trigger = False
        assert mod.entry_memory["2330"]["long"] == 1
        assert result["entry_signal"]["long_trigger"] is False

    def test_limit_locked_disables_both_triggers(self):
        t, q = self._long_ticker_quote()
        extract_analysis_data(t, q)
        # Now simulate limit-locked (no bids)
        t2, _ = self._long_ticker_quote()
        q_locked = {"bids": [], "asks": [{"price": 102.5, "size": 100}]}
        result = extract_analysis_data(t2, q_locked)
        assert result["entry_signal"]["short_trigger"] is False
        assert result["entry_signal"]["long_trigger"] is False

    def test_symbol_isolation(self):
        """Triggers for different symbols are tracked independently."""
        t_a = _ticker(symbol="2330", last=102.1, high=106.0, low=99.0,
                      avg=101.5, change_pct=2.0, volume=5000)
        t_b = _ticker(symbol="2454", last=102.1, high=106.0, low=99.0,
                      avg=101.5, change_pct=2.0, volume=5000)
        q = {
            "bids": [{"price": 101.5, "size": 200}, {"price": 101.0, "size": 300}],
            "asks": [{"price": 102.5, "size": 100}, {"price": 103.0, "size": 80}],
        }
        extract_analysis_data(t_a, q)
        extract_analysis_data(t_a, q)  # 2330 trigger fires
        result_b = extract_analysis_data(t_b, q)  # 2454 only 1 call
        assert result_b["entry_signal"]["long_trigger"] is False


# ── extract_analysis_data – output structure ──────────────────────────────

class TestExtractAnalysisDataOutput:
    def test_symbol_echoed_from_ticker(self):
        t = _ticker(symbol="3037")
        result = extract_analysis_data(t, _quote())
        assert result["symbol"] == "3037"

    def test_risk_control_contains_vwap_distance(self):
        t = _ticker(last=100.0, avg=100.0, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert "vwap_distance" in result["risk_control"]

    def test_market_type_trend_when_large_move(self):
        t = _ticker(last=102.0, avg=100.0, change_pct=2.5, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert result["market_type"] == "trend"

    def test_signal_grade_a_long_when_high_score(self):
        # Need score >= 75: +50 base, +15 (positive change), +10 (uptrend),
        # +5 (buy dominance) = 80
        t = _ticker(last=102.0, avg=100.0, change_pct=3.0, volume=5000)
        q = _quote(bid_size=200, ask_size=100)
        result = extract_analysis_data(t, q)
        assert result["signal_grade"] == "A_long"


# ── extract_investment_data ────────────────────────────────────────────────

class TestExtractInvestmentData:
    def _make_ticker(self, symbol="2330", last=110.0):
        class T:
            pass
        t = T()
        t.symbol = symbol
        t.lastPrice = last
        return t

    def test_strong_buy_when_all_conditions_met(self):
        # Bull alignment: last > ma5 > ma20 > ma60, growth yoy > 15, rsi in (50,75)
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 108.0, "ma20": 105.0, "ma60": 100.0, "rsi": 60.0, "yoy": 25.0}
        result = extract_investment_data(ticker, daily)
        assert result["decision"] == "strong_buy_candidate"
        assert result["signal_grade"] == "A"

    def test_investment_watch_when_no_bull_ma_alignment(self):
        # ma5 < ma20 → not bull
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 103.0, "ma20": 105.0, "ma60": 100.0, "rsi": 60.0, "yoy": 25.0}
        result = extract_investment_data(ticker, daily)
        assert result["decision"] == "investment_watch"
        assert result["signal_grade"] == "C"

    def test_investment_watch_when_rsi_too_high(self):
        # rsi >= 75 → fails condition
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 108.0, "ma20": 105.0, "ma60": 100.0, "rsi": 78.0, "yoy": 25.0}
        result = extract_investment_data(ticker, daily)
        assert result["decision"] == "investment_watch"

    def test_investment_watch_when_yoy_below_threshold(self):
        # yoy <= 15 → is_growth = False
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 108.0, "ma20": 105.0, "ma60": 100.0, "rsi": 60.0, "yoy": 10.0}
        result = extract_investment_data(ticker, daily)
        assert result["decision"] == "investment_watch"

    def test_always_returns_type_investment(self):
        ticker = self._make_ticker()
        daily = {"ma5": 90.0, "ma20": 85.0, "ma60": 80.0, "rsi": 55.0, "yoy": 20.0}
        result = extract_investment_data(ticker, daily)
        assert result["type"] == "INVESTMENT"

    def test_ma_status_bull(self):
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 108.0, "ma20": 105.0, "ma60": 100.0, "rsi": 60.0, "yoy": 25.0}
        result = extract_investment_data(ticker, daily)
        assert result["indicators"]["ma_status"] == "多頭排列"

    def test_ma_status_consolidating(self):
        ticker = self._make_ticker(last=110.0)
        daily = {"ma5": 103.0, "ma20": 105.0, "ma60": 100.0, "rsi": 60.0, "yoy": 25.0}
        result = extract_investment_data(ticker, daily)
        assert result["indicators"]["ma_status"] == "整理中"
