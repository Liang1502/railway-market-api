"""
Tests for watch.py pure strategy functions:
  - safe_float
  - parse_direction
  - compute_strategy  (all branches for long and short positions)
"""
import pytest
from watch import safe_float, parse_direction, compute_strategy


# ── safe_float ────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_integer(self):
        assert safe_float(42) == 42.0

    def test_float_string(self):
        assert safe_float("3.14") == pytest.approx(3.14)

    def test_none_returns_none(self):
        assert safe_float(None) is None

    def test_empty_string_returns_none(self):
        assert safe_float("") is None

    def test_invalid_string_returns_none(self):
        assert safe_float("abc") is None

    def test_zero(self):
        assert safe_float(0) == 0.0


# ── parse_direction ───────────────────────────────────────────────────────

class TestParseDirection:
    @pytest.mark.parametrize("s", ["buy", "b", "long", "多"])
    def test_long_aliases(self, s):
        assert parse_direction(s) == "long"

    @pytest.mark.parametrize("s", ["sell", "s", "short", "空"])
    def test_short_aliases(self, s):
        assert parse_direction(s) == "short"

    def test_case_insensitive(self):
        assert parse_direction("BUY") == "long"
        assert parse_direction("SELL") == "short"

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_direction("unknown")


# ── compute_strategy – shared helpers ────────────────────────────────────

def _long_pos(entry: float = 100.0, stop_pct: float = 0.02,
              t1_pct: float = 0.02, t2_pct: float = 0.03) -> dict:
    return {
        "direction": "long",
        "entry": entry,
        "stop": round(entry * (1 - stop_pct), 2),
        "t1": round(entry * (1 + t1_pct), 2),
        "t2": round(entry * (1 + t2_pct), 2),
    }


def _short_pos(entry: float = 100.0, stop_pct: float = 0.02,
               t1_pct: float = 0.02, t2_pct: float = 0.03) -> dict:
    return {
        "direction": "short",
        "entry": entry,
        "stop": round(entry * (1 + stop_pct), 2),
        "t1": round(entry * (1 - t1_pct), 2),
        "t2": round(entry * (1 - t2_pct), 2),
    }


def _ind(**kwargs) -> dict:
    """Build a minimal indicator dict; all optional KD/VWAP fields default to None."""
    defaults = {"k_1min": None, "d_1min": None, "vwap_1min": None, "v6_score": None}
    defaults.update(kwargs)
    return defaults


# ── compute_strategy – long positions ─────────────────────────────────────

class TestComputeStrategyLong:
    def test_stop_loss_hit(self):
        pos = _long_pos(entry=100.0)   # stop = 98.0
        _, urgency = compute_strategy(pos, price=97.0, ind=_ind())
        assert urgency == "stop"

    def test_take_profit_t2_hit(self):
        pos = _long_pos(entry=100.0)   # t2 = 103.0
        _, urgency = compute_strategy(pos, price=104.0, ind=_ind())
        assert urgency == "profit"

    def test_t1_hit_with_kd_death_cross_warns(self):
        pos = _long_pos(entry=100.0)   # t1 = 102.0
        ind = _ind(k_1min=30.0, d_1min=50.0)  # k < d → death cross
        _, urgency = compute_strategy(pos, price=102.5, ind=ind)
        assert urgency == "warn"

    def test_t1_hit_without_kd_warns(self):
        pos = _long_pos(entry=100.0)   # t1 = 102.0
        _, urgency = compute_strategy(pos, price=102.5, ind=_ind())
        assert urgency == "warn"

    def test_add_signal_when_momentum_aligned(self):
        # gain > 0.5%, kd_gold, above_vwap, v6 >= 70
        pos = _long_pos(entry=100.0)
        ind = _ind(k_1min=70.0, d_1min=50.0, vwap_1min=100.5, v6_score=75.0)
        _, urgency = compute_strategy(pos, price=101.0, ind=ind)
        assert urgency == "add"

    def test_ok_when_kd_gold_and_above_vwap_v6_ok(self):
        pos = _long_pos(entry=100.0)
        ind = _ind(k_1min=65.0, d_1min=50.0, vwap_1min=100.5, v6_score=62.0)
        _, urgency = compute_strategy(pos, price=100.8, ind=ind)
        assert urgency == "ok"

    def test_watch_when_no_signals(self):
        pos = _long_pos(entry=100.0)
        _, urgency = compute_strategy(pos, price=100.5, ind=_ind())
        assert urgency == "watch"

    def test_warn_when_loss_accelerating_with_kd_death(self):
        # gain_pct < -1.5 and kd_death; use stop_pct=0.03 so stop=97 < price=98.4
        pos = _long_pos(entry=100.0, stop_pct=0.03)  # stop=97.0
        ind = _ind(k_1min=20.0, d_1min=40.0)         # k < d → death cross
        _, urgency = compute_strategy(pos, price=98.4, ind=ind)  # -1.6% loss
        assert urgency == "warn"


# ── compute_strategy – short positions ───────────────────────────────────

class TestComputeStrategyShort:
    def test_stop_loss_hit(self):
        pos = _short_pos(entry=100.0)  # stop = 102.0
        _, urgency = compute_strategy(pos, price=103.0, ind=_ind())
        assert urgency == "stop"

    def test_take_profit_t2_hit(self):
        pos = _short_pos(entry=100.0)  # t2 = 97.0
        _, urgency = compute_strategy(pos, price=96.0, ind=_ind())
        assert urgency == "profit"

    def test_t1_hit_with_kd_gold_cross_warns(self):
        pos = _short_pos(entry=100.0)  # t1 = 98.0
        ind = _ind(k_1min=60.0, d_1min=40.0)  # k > d → gold cross
        _, urgency = compute_strategy(pos, price=97.5, ind=ind)
        assert urgency == "warn"

    def test_t1_hit_without_kd_warns(self):
        pos = _short_pos(entry=100.0)  # t1 = 98.0
        _, urgency = compute_strategy(pos, price=97.5, ind=_ind())
        assert urgency == "warn"

    def test_add_signal_when_short_momentum_aligned(self):
        # gain > 0.5%, kd_death, below_vwap, v6 <= -20
        pos = _short_pos(entry=100.0)
        ind = _ind(k_1min=20.0, d_1min=50.0, vwap_1min=99.5, v6_score=-25.0)
        _, urgency = compute_strategy(pos, price=99.0, ind=ind)
        assert urgency == "add"

    def test_ok_when_short_momentum_ok(self):
        # price=99.0 strictly below vwap=99.5 → below_vwap True
        pos = _short_pos(entry=100.0)
        ind = _ind(k_1min=20.0, d_1min=50.0, vwap_1min=99.5, v6_score=-12.0)
        _, urgency = compute_strategy(pos, price=99.0, ind=ind)
        assert urgency == "ok"

    def test_watch_when_no_signals(self):
        pos = _short_pos(entry=100.0)
        _, urgency = compute_strategy(pos, price=99.5, ind=_ind())
        assert urgency == "watch"
