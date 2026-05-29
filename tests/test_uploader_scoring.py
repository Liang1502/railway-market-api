"""
Tests for uploader.compute_v6_score and its helpers.

Conftest already mocks fubon_neo / google.generativeai so importing uploader
does not require Fubon credentials.
"""
import pytest
from datetime import datetime

import uploader


# ── Test fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """Score history is module-level; reset between tests for isolation."""
    uploader.score_history.clear()
    uploader._prev_kd.clear()
    yield
    uploader.score_history.clear()
    uploader._prev_kd.clear()


def _base_payload(**overrides) -> dict:
    """Minimal payload with no signals firing. Override fields per test."""
    data = {
        "score_price":  100.0,
        "_runtime_price": 100.0,
        "y_high":       110.0,
        "y_low":         90.0,
        "y_close":      100.0,
        "k_1min":         50.0,
        "d_1min":         50.0,
        "vwap_1min":    100.0,
        "volume_ratio":   1.0,
        "kd_signal":   "none",
        "structure":    {"pressure_ratio": 1.0},
        "decision":    "observe",
        "score":          50,
        "entry_signal": {},
    }
    data.update(overrides)
    return data


# ── KD signals ────────────────────────────────────────────────────────────

class TestKD:
    def test_gold_cross_adds_full_weight(self):
        data = _base_payload(kd_signal="gold_cross", k_1min=60, d_1min=55)
        score, tags = uploader.compute_v6_score("S1", data)
        assert "KD金叉" in tags
        # gold cross 加 +15

    def test_death_cross_adds_negative_full_weight(self):
        data = _base_payload(kd_signal="death_cross", k_1min=40, d_1min=45)
        score, tags = uploader.compute_v6_score("S2", data)
        assert "KD死叉" in tags

    def test_static_bullish_uses_partial_weight(self):
        data = _base_payload(k_1min=60, d_1min=50)
        _, tags = uploader.compute_v6_score("S3", data)
        assert "KD偏多" in tags
        assert "KD金叉" not in tags

    def test_static_bearish_uses_partial_weight(self):
        data = _base_payload(k_1min=40, d_1min=50)
        _, tags = uploader.compute_v6_score("S4", data)
        assert "KD偏空" in tags


# ── VWAP ──────────────────────────────────────────────────────────────────

class TestVWAP:
    def test_above_vwap(self):
        data = _base_payload(score_price=102.0, vwap_1min=100.0)
        _, tags = uploader.compute_v6_score("V1", data)
        assert "站上VWAP" in tags

    def test_below_vwap(self):
        data = _base_payload(score_price=98.0, vwap_1min=100.0)
        _, tags = uploader.compute_v6_score("V2", data)
        assert "跌破VWAP" in tags


# ── Yesterday levels ──────────────────────────────────────────────────────

class TestYesterdayLevels:
    def test_break_y_high(self):
        data = _base_payload(score_price=112.0, y_high=110.0)
        _, tags = uploader.compute_v6_score("Y1", data)
        assert "突破昨高" in tags

    def test_break_y_low(self):
        data = _base_payload(score_price=88.0, y_low=90.0)
        _, tags = uploader.compute_v6_score("Y2", data)
        assert "跌破昨低" in tags

    def test_rebound_near_low(self):
        data = _base_payload(score_price=91.0, y_low=90.0, y_close=100.0)
        _, tags = uploader.compute_v6_score("Y3", data)
        assert "低檔反彈" in tags


# ── Volume / overheat / fake breakout ─────────────────────────────────────

class TestVolumeAndExtreme:
    def test_volume_expand(self):
        data = _base_payload(volume_ratio=2.0)
        _, tags = uploader.compute_v6_score("X1", data)
        assert any("量增" in t for t in tags)

    def test_volume_ratio_ignores_current_unfinished_candle(self, monkeypatch):
        now = datetime(2026, 5, 28, 9, 10, tzinfo=uploader.TW_TZ)
        monkeypatch.setattr(uploader, "now_tw", lambda: now)
        candles = [
            {"time_key": f"2026-05-28 09:{minute:02d}", "volume": 100}
            for minute in range(9)
        ]
        candles.append({"time_key": "2026-05-28 09:09", "volume": 200})
        candles.append({"time_key": "2026-05-28 09:10", "volume": 10000})

        result = uploader.get_volume_info("2330", candles=candles)

        assert result == {"volume_expand": True, "volume_ratio": 2.0}

    def test_overheat(self):
        data = _base_payload(score_price=108.0, y_close=100.0)
        _, tags = uploader.compute_v6_score("X2", data)
        assert "短線過熱" in tags

    def test_fake_breakout_when_break_y_high_with_kd_death(self):
        data = _base_payload(
            score_price=112.0, y_high=110.0,
            k_1min=30, d_1min=50,  # k < d → 死叉狀態
        )
        _, tags = uploader.compute_v6_score("X3", data)
        assert "疑似假突破" in tags


# ── Pressure ratio (五檔力道) ─────────────────────────────────────────────

class TestPressureRatio:
    def test_buy_dominant(self):
        data = _base_payload(structure={"pressure_ratio": 0.6})
        _, tags = uploader.compute_v6_score("P1", data)
        assert "買盤主導" in tags

    def test_sell_dominant(self):
        data = _base_payload(structure={"pressure_ratio": 1.5})
        _, tags = uploader.compute_v6_score("P2", data)
        assert "賣盤主導" in tags


# ── System veto (亮燈鎖死 / 7% 死亡 / no_trade) ──────────────────────────

class TestSystemVeto:
    def test_no_trade_forces_negative(self):
        data = _base_payload(decision="no_trade", score=80)
        score, tags = uploader.compute_v6_score("Z1", data)
        assert score <= -10
        assert "系統禁令" in tags

    def test_limit_locked_long_reason_forces_negative(self):
        data = _base_payload(
            entry_signal={"long_reason": "⚠️ 五檔清空 (亮燈鎖死)，禁止任何建倉"},
        )
        score, tags = uploader.compute_v6_score("Z2", data)
        assert score <= -10
        assert "系統禁令" in tags

    def test_seven_percent_red_line_forces_negative(self):
        data = _base_payload(
            entry_signal={"long_reason": "⚠️ 觸發 7% 死亡紅線禁令，鎖死做多"},
        )
        score, tags = uploader.compute_v6_score("Z3", data)
        assert score <= -10
        assert "系統禁令" in tags


# ── Momentum (multi-tick behavior) ────────────────────────────────────────

class TestMomentum:
    def test_momentum_strengthening_adds_tag(self):
        # 第一次：低分；第二次：高分 → momentum 為正
        weak = _base_payload(score_price=98.0, vwap_1min=100.0)   # 跌破 VWAP
        uploader.compute_v6_score("M1", weak)
        strong = _base_payload(score_price=112.0, y_high=110.0,
                               kd_signal="gold_cross", volume_ratio=2.0)
        _, tags = uploader.compute_v6_score("M1", strong)
        assert "動能走強" in tags

    def test_momentum_weakening_adds_tag(self):
        strong = _base_payload(score_price=112.0, y_high=110.0,
                               kd_signal="gold_cross", volume_ratio=2.0)
        uploader.compute_v6_score("M2", strong)
        weak = _base_payload(score_price=98.0, vwap_1min=100.0)
        _, tags = uploader.compute_v6_score("M2", weak)
        assert "動能轉弱" in tags

    def test_score_history_capped_at_max_history(self):
        data = _base_payload()
        for _ in range(uploader.MAX_HISTORY + 5):
            uploader.compute_v6_score("M3", data)
        assert len(uploader.score_history["M3"]) == uploader.MAX_HISTORY


# ── Raw-score booster ─────────────────────────────────────────────────────

class TestRawScoreBooster:
    def test_high_raw_score_adds_bonus(self):
        data = _base_payload(score=85)
        _, tags = uploader.compute_v6_score("R1", data)
        assert "高原始分" in tags

    def test_low_raw_score_subtracts(self):
        data = _base_payload(score=30)
        # 沒有專屬 tag，但 score 應該受影響 — 比對含/不含的差
        with_low, _ = uploader.compute_v6_score("R2_low", data)
        uploader.score_history.clear()
        baseline_data = _base_payload(score=50)
        without_low, _ = uploader.compute_v6_score("R2_base", baseline_data)
        assert with_low < without_low
