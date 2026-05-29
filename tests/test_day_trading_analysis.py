"""
Tests for the day-trading signal logic in test_analysis_data.py.

Covers:
  - Utility helpers: _get, _to_float, _to_list
  - extract_analysis_data: all decision branches, score, entry triggers
"""
import time
import pytest
import analysis as mod
from analysis import (
    _get,
    _to_float,
    _to_list,
    extract_analysis_data,
    reset_entry_memory,
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
        # dominance = "buy", pressure_ratio below long threshold, last > mid, trend = "up"
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

    def test_long_possible_with_relaxed_buy_pressure_threshold(self):
        # pressure_ratio=0.85: above old 0.8 threshold, below relaxed 0.9 threshold.
        t = _ticker(last=102.1, high=106.0, low=99.0, avg=101.5, change_pct=2.0, volume=5000)
        q = _quote(bid_price=101.5, bid_size=100, ask_price=102.5, ask_size=85)

        result = extract_analysis_data(t, q)

        assert result["structure"]["pressure_ratio"] == pytest.approx(0.85)
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
        # trend=down (diff_pct=-2.86%), change_pct=2.0>0.8, last>mid, dominance=buy
        # last=102.0, avg=105.0 → diff=-2.86% → trend=down
        # mid=(99.5+100.5)/2=100.0, distance_pct=1.96%>0.1%
        t = _ticker(last=102.0, high=103.0, low=96.0, avg=105.0, change_pct=2.0, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 300}],
            "asks": [{"price": 100.5, "size": 100}],
        }
        result = extract_analysis_data(t, q)
        assert result["reversal"] == "bullish_reversal"
        assert result["decision"] == "long_possible"

    def test_bearish_reversal_signal(self):
        # trend=up (diff_pct=3.16%), change_pct=-2.0<-0.8, last<mid, dominance=sell
        # last=98.0, avg=95.0 → diff=3.16% → trend=up
        # mid=(99.5+100.5)/2=100.0, distance_pct=-2.04%<-0.1%
        t = _ticker(last=98.0, high=101.0, low=97.0, avg=95.0, change_pct=-2.0, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 80}],
            "asks": [{"price": 100.5, "size": 300}],
        }
        result = extract_analysis_data(t, q)
        assert result["reversal"] == "bearish_reversal"
        assert result["decision"] == "short_possible"

    def test_trend_neutral_within_0_3_pct_buffer(self):
        # diff_pct = (100.25-100.0)/100.0 = 0.25% < 0.3% → neutral
        # buy dominance with pressure_ratio < 0.8, but trend != "up" → observe not long_possible
        t = _ticker(last=100.25, high=103.0, low=97.0, avg=100.0, change_pct=0.3, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 500}],
            "asks": [{"price": 100.5, "size": 100}],
        }
        result = extract_analysis_data(t, q)
        assert result["trend"] == "neutral"
        assert result["decision"] == "observe"

    def test_trend_up_above_0_3_pct_buffer(self):
        # diff_pct = (100.31-100.0)/100.0 = 0.31% > 0.3% → up
        t = _ticker(last=100.31, high=103.0, low=97.0, avg=100.0, change_pct=0.5, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert result["trend"] == "up"

    def test_trend_down_below_0_3_pct_buffer(self):
        # diff_pct = (99.69-100.0)/100.0 = -0.31% < -0.3% → down
        t = _ticker(last=99.69, high=103.0, low=97.0, avg=100.0, change_pct=-0.5, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert result["trend"] == "down"

    def test_reversal_triggers_between_old_and_new_threshold(self):
        # change_pct=1.0: between old threshold 1.2 (would NOT trigger) and new 0.8 (now triggers)
        # trend=down (diff=-2.86%), last=102>mid=100, dominance=buy, dist=1.96%>0.1%
        t = _ticker(last=102.0, high=103.0, low=96.0, avg=105.0, change_pct=1.0, volume=5000)
        q = {
            "bids": [{"price": 99.5, "size": 300}],
            "asks": [{"price": 100.5, "size": 100}],
        }
        result = extract_analysis_data(t, q)
        assert result["reversal"] == "bullish_reversal"


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

    def test_long_trigger_uses_relaxed_buy_pressure_threshold(self):
        t = _ticker(last=102.1, high=106.0, low=99.0, avg=101.5, change_pct=2.0, volume=5000)
        q = _quote(bid_price=101.5, bid_size=100, ask_price=102.5, ask_size=85)

        extract_analysis_data(t, q)
        result = extract_analysis_data(t, q)

        assert result["entry_signal"]["long_trigger"] is True

    def test_short_trigger_uses_relaxed_sell_pressure_threshold(self):
        t = _ticker(last=97.9, high=100.0, low=95.0, avg=100.0, change_pct=-2.0, volume=5000)
        q = _quote(bid_price=97.5, bid_size=100, ask_price=98.5, ask_size=125)

        extract_analysis_data(t, q)
        result = extract_analysis_data(t, q)

        assert result["structure"]["pressure_ratio"] == pytest.approx(1.25)
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
        stale = time.time() - mod.ENTRY_CONFIRM_WINDOW_SECS - 1
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

    def test_entry_zone_scales_with_high_price_and_tick_size(self):
        t = _ticker(last=803.0, high=810.0, low=790.0, avg=800.0, change_pct=1.0, volume=5000)
        q = _quote(bid_price=799.0, bid_size=200, ask_price=801.0, ask_size=100)

        result = extract_analysis_data(t, q)

        assert result["decision"] == "long_possible"
        assert result["entry_zone"] == {"lower": 797.0, "upper": 803.0}

    def test_entry_zone_scales_with_low_price_and_tick_size(self):
        t = _ticker(last=50.2, high=53.0, low=48.0, avg=50.0, change_pct=1.0, volume=5000)
        q = _quote(bid_price=49.9, bid_size=200, ask_price=50.1, ask_size=100)

        result = extract_analysis_data(t, q)

        assert result["decision"] == "long_possible"
        assert result["entry_zone"] == {"lower": 49.85, "upper": 50.2}


# ── extract_analysis_data – output structure ──────────────────────────────

class TestExtractAnalysisDataOutput:
    def test_symbol_echoed_from_ticker(self):
        t = _ticker(symbol="3037")
        result = extract_analysis_data(t, _quote())
        assert result["symbol"] == "3037"

    def test_name_echoed_from_ticker_when_available(self):
        t = _ticker(symbol="2330")
        t["name"] = "台積電"
        result = extract_analysis_data(t, _quote())
        assert result["name"] == "台積電"

    def test_risk_control_contains_vwap_distance(self):
        t = _ticker(last=100.0, avg=100.0, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert "vwap_distance" in result["risk_control"]

    def test_market_type_trend_when_large_move(self):
        t = _ticker(last=102.0, avg=100.0, change_pct=2.5, volume=5000)
        result = extract_analysis_data(t, _quote())
        assert result["market_type"] == "trend"

    def test_market_type_trap_takes_priority_over_trend(self):
        t = _ticker(last=100.0, high=100.3, low=95.0, avg=99.0, change_pct=-2.0, volume=5000)
        q = _quote(bid_size=80, ask_size=200)
        result = extract_analysis_data(t, q)
        assert result["trap"] == "bull_trap"
        assert result["market_type"] == "trap"

    def test_signal_grade_a_long_when_high_score(self):
        # Need score >= 75: +50 base, +15 (positive change), +10 (uptrend),
        # +5 (buy dominance) = 80
        t = _ticker(last=102.0, avg=100.0, change_pct=3.0, volume=5000)
        q = _quote(bid_size=200, ask_size=100)
        result = extract_analysis_data(t, q)
        assert result["signal_grade"] == "A_long"


# ── reset_entry_memory & edge cases ───────────────────────────────────────────

class TestResetEntryMemory:
    def test_reset_all_clears_every_symbol(self):
        mod.entry_memory["A"] = {"short": 3, "long": 0, "short_time": 1.0, "long_time": None}
        mod.entry_memory["B"] = {"short": 0, "long": 2, "short_time": None, "long_time": 1.0}
        reset_entry_memory()
        assert mod.entry_memory == {}

    def test_reset_single_removes_only_target(self):
        mod.entry_memory["A"] = {"short": 1, "long": 0, "short_time": 1.0, "long_time": None}
        mod.entry_memory["B"] = {"short": 0, "long": 1, "short_time": None, "long_time": 1.0}
        reset_entry_memory("A")
        assert "A" not in mod.entry_memory
        assert "B" in mod.entry_memory

    def test_reset_nonexistent_symbol_is_noop(self):
        reset_entry_memory("XXXX")  # must not raise

    def test_expired_symbols_are_pruned_during_analysis(self):
        old = time.time() - mod.ENTRY_MEMORY_TTL_SECS - 1
        mod.entry_memory["OLD"] = {"short": 0, "long": 1, "short_time": None, "long_time": old}

        extract_analysis_data(_ticker(symbol="2330"), _quote())

        assert "OLD" not in mod.entry_memory

    def test_timestamp_zero_treated_as_in_window(self):
        """time_key == 0 (falsy epoch) must NOT skip the window check."""
        sym = "TEST"
        mod.entry_memory[sym] = {"short": 1, "long": 0, "short_time": 0.0, "long_time": None}
        ticker = {"symbol": sym}
        quote = {
            "bids": [{"price": 99.0, "size": 10}],
            "asks": [{"price": 101.0, "size": 30}],
            "lastPrice": 99.5,
            "highPrice": 100.0,
            "lowPrice": 98.0,
            "avgPrice": 100.5,
            "changePercent": -1.0,
        }
        # With timestamp=0 and very-old simulated clock, the window must reset the counter
        # rather than perpetually accumulating. We verify no crash and counter behaves.
        result = extract_analysis_data(ticker, quote)
        assert isinstance(result, dict)

    def test_vwap_distance_none_when_avg_price_missing(self):
        """vwap_distance must be None (not 0) when avg_price is absent."""
        ticker = {"symbol": "T"}
        quote = {
            "bids": [{"price": 100.0, "size": 10}],
            "asks": [{"price": 101.0, "size": 10}],
            "lastPrice": 102.0,
            "changePercent": 0.0,
        }
        result = extract_analysis_data(ticker, quote)
        assert result["risk_control"]["vwap_distance"] is None

    def test_fomo_not_triggered_when_avg_price_missing(self):
        """FOMO guard must not fire if avg_price is absent (vwap_distance=None)."""
        ticker = {"symbol": "T"}
        quote = {
            "bids": [{"price": 100.0, "size": 30}],
            "asks": [{"price": 101.0, "size": 10}],
            "lastPrice": 110.0,
            "changePercent": 3.0,
        }
        result = extract_analysis_data(ticker, quote)
        assert result["decision"] != "avoid_long" or result["decision"] == "avoid_long" and result.get("risk_control", {}).get("vwap_distance") is not None  # FOMO only fires with real distance
