"""
Tests for watch.py pure strategy functions:
  - safe_float
  - parse_direction
  - compute_strategy  (all branches for long and short positions)
  - _atr_pcts         (adaptive stop/T1/T2 from yesterday's range)
  - cmd_close / cmd_history (trade log)
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from watch import (
    safe_float, parse_direction, compute_strategy, _validate_positions,
    _atr_pcts, cmd_close, cmd_history,
    STOP_PCT, T1_PCT, T2_PCT,
)


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
    defaults = {
        "k_1min": None, "d_1min": None, "vwap_1min": None,
        "v6_score": None, "kd_signal": "none",
    }
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

    def test_add_signal_requires_fresh_gold_cross(self):
        # add 條件需要 kd_signal="gold_cross"，靜態 K>D 不夠
        pos = _long_pos(entry=100.0)
        ind = _ind(k_1min=70.0, d_1min=50.0, vwap_1min=100.5, v6_score=75.0,
                   kd_signal="gold_cross")
        _, urgency = compute_strategy(pos, price=101.0, ind=ind)
        assert urgency == "add"

    def test_add_signal_not_triggered_by_static_kd_gold(self):
        # 靜態 K>D（kd_signal="none"）不應觸發加碼，應改為 ok
        pos = _long_pos(entry=100.0)
        ind = _ind(k_1min=70.0, d_1min=50.0, vwap_1min=100.5, v6_score=75.0,
                   kd_signal="none")
        _, urgency = compute_strategy(pos, price=101.0, ind=ind)
        assert urgency == "ok"

    def test_ok_when_kd_gold_and_above_vwap_v6_ok(self):
        pos = _long_pos(entry=100.0)
        ind = _ind(k_1min=65.0, d_1min=50.0, vwap_1min=100.5, v6_score=62.0)
        _, urgency = compute_strategy(pos, price=100.8, ind=ind)
        assert urgency == "ok"

    def test_watch_when_no_signals(self):
        pos = _long_pos(entry=100.0)
        _, urgency = compute_strategy(pos, price=100.5, ind=_ind())
        assert urgency == "watch"

    def test_warn_when_loss_accelerating_with_fresh_kd_death(self):
        # warn 條件需要 kd_signal="death_cross"（剛形成死叉）
        pos = _long_pos(entry=100.0, stop_pct=0.03)  # stop=97.0
        ind = _ind(k_1min=20.0, d_1min=40.0, kd_signal="death_cross")
        _, urgency = compute_strategy(pos, price=98.4, ind=ind)  # -1.6% loss
        assert urgency == "warn"

    def test_warn_not_triggered_by_static_kd_death_loss(self):
        # 靜態 K<D（kd_signal="none"）虧損中不應觸發 warn，應為 watch（今日 4979 事件修正）
        pos = _long_pos(entry=100.0, stop_pct=0.03)  # stop=97.0
        ind = _ind(k_1min=20.0, d_1min=40.0, kd_signal="none")
        _, urgency = compute_strategy(pos, price=98.4, ind=ind)
        assert urgency == "watch"


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

    def test_add_signal_short_requires_fresh_death_cross(self):
        # 空單加碼需要 kd_signal="death_cross"
        pos = _short_pos(entry=100.0)
        ind = _ind(k_1min=20.0, d_1min=50.0, vwap_1min=99.5, v6_score=-25.0,
                   kd_signal="death_cross")
        _, urgency = compute_strategy(pos, price=99.0, ind=ind)
        assert urgency == "add"

    def test_add_signal_short_not_triggered_by_static_kd_death(self):
        # 靜態 K<D 不應觸發空單加碼
        pos = _short_pos(entry=100.0)
        ind = _ind(k_1min=20.0, d_1min=50.0, vwap_1min=99.5, v6_score=-25.0,
                   kd_signal="none")
        _, urgency = compute_strategy(pos, price=99.0, ind=ind)
        assert urgency == "ok"

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


# ── Guard & validation tests ──────────────────────────────────────────────────

class TestGuards:
    def test_entry_zero_returns_error(self):
        """entry=0 must not divide-by-zero; returns "error" urgency (not "stop")."""
        pos = {"direction": "long", "entry": 0, "stop": 0, "t1": 0, "t2": 0}
        _, urgency = compute_strategy(pos, price=100.0, ind=_ind())
        assert urgency == "error"

    def test_entry_none_returns_error(self):
        pos = {"direction": "long", "entry": None, "stop": None, "t1": None, "t2": None}
        _, urgency = compute_strategy(pos, price=100.0, ind=_ind())
        assert urgency == "error"

    def test_entry_zero_short_returns_error(self):
        pos = {"direction": "short", "entry": 0, "stop": 0, "t1": 0, "t2": 0}
        _, urgency = compute_strategy(pos, price=100.0, ind=_ind())
        assert urgency == "error"


class TestValidatePositions:
    def _valid_pos(self):
        return {"direction": "long", "entry": 100.0, "stop": 98.0, "t1": 102.0, "t2": 103.0}

    def test_valid_positions_pass_through(self):
        positions = {"2330": self._valid_pos()}
        result = _validate_positions(positions)
        assert "2330" in result

    def test_entry_none_dropped(self):
        pos = self._valid_pos()
        pos["entry"] = None
        result = _validate_positions({"2330": pos})
        assert "2330" not in result

    def test_entry_zero_dropped(self):
        pos = self._valid_pos()
        pos["entry"] = 0
        result = _validate_positions({"2330": pos})
        assert "2330" not in result

    def test_mixed_keeps_only_valid(self):
        bad = self._valid_pos()
        bad["stop"] = None
        good = self._valid_pos()
        result = _validate_positions({"BAD": bad, "GOOD": good})
        assert "BAD" not in result
        assert "GOOD" in result


# ── _atr_pcts ─────────────────────────────────────────────────────────────────

def _mock_api(y_high, y_low, y_close):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"y_high": y_high, "y_low": y_low, "y_close": y_close}
    return m


class TestAtrPcts:
    def test_high_volatility_hits_caps(self):
        # ATR = (700-660)/675 = 5.93% → all hit upper caps
        with patch("watch.requests.get", return_value=_mock_api(700, 660, 675)):
            stop, t1, t2 = _atr_pcts("4979")
        assert stop == pytest.approx(0.03)
        assert t1  == pytest.approx(0.05)
        assert t2  == pytest.approx(0.07)

    def test_low_volatility_hits_minimums(self):
        # ATR = 1.5/100.5 = 1.49% → all hit lower floors
        with patch("watch.requests.get", return_value=_mock_api(101.5, 100.0, 100.5)):
            stop, t1, t2 = _atr_pcts("2330")
        assert stop == pytest.approx(0.015)
        assert t1  == pytest.approx(0.02)
        assert t2  == pytest.approx(0.03)

    def test_medium_volatility_between_caps_and_floors(self):
        # ATR = 3.0/101.5 = 2.96% → stop=2.36%, T1=2.96%, T2=4.43%
        with patch("watch.requests.get", return_value=_mock_api(103.0, 100.0, 101.5)):
            stop, t1, t2 = _atr_pcts("2454")
        assert 0.015 < stop < 0.03   # 非預設值，也未觸頂
        assert 0.02  < t1  < 0.05
        assert 0.03  < t2  < 0.07
        assert stop == pytest.approx((3.0 / 101.5) * 0.8, rel=0.001)

    def test_api_failure_returns_defaults(self):
        with patch("watch.requests.get", side_effect=Exception("timeout")):
            stop, t1, t2 = _atr_pcts("9999")
        assert stop == STOP_PCT
        assert t1   == T1_PCT
        assert t2   == T2_PCT

    def test_missing_yesterday_data_returns_defaults(self):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"symbol": "9999"}  # y_high/y_low/y_close 缺失
        with patch("watch.requests.get", return_value=m):
            stop, t1, t2 = _atr_pcts("9999")
        assert stop == STOP_PCT
        assert t1   == T1_PCT
        assert t2   == T2_PCT

    def test_non_200_response_returns_defaults(self):
        m = MagicMock()
        m.status_code = 503
        with patch("watch.requests.get", return_value=m):
            stop, t1, t2 = _atr_pcts("9999")
        assert stop == STOP_PCT


# ── cmd_close & cmd_history ───────────────────────────────────────────────────

def _write_pos(path, sym, direction, entry):
    data = {sym: {"direction": direction, "entry": entry,
                  "stop": 0.0, "t1": 0.0, "t2": 0.0, "added_at": "2026-05-12 10:00:00"}}
    path.write_text(json.dumps(data))

def _write_trades(path, trades):
    path.write_text(json.dumps(trades))


class TestCmdClose:
    def test_long_profit_recorded(self, tmp_path, monkeypatch, capsys):
        pos_f = tmp_path / "positions.json"
        trd_f = tmp_path / "trades.json"
        _write_pos(pos_f, "2330", "long", 100.0)
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))
        monkeypatch.setattr("watch.TRADES_FILE",    str(trd_f))

        cmd_close(["2330", "102.0"])

        trades = json.loads(trd_f.read_text())
        assert len(trades) == 1
        assert trades[0]["pnl_pct"] == pytest.approx(2.0)
        assert trades[0]["pnl_pts"] == pytest.approx(2.0)
        assert "✅" in capsys.readouterr().out

    def test_long_loss_recorded(self, tmp_path, monkeypatch, capsys):
        pos_f = tmp_path / "positions.json"
        trd_f = tmp_path / "trades.json"
        _write_pos(pos_f, "2330", "long", 100.0)
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))
        monkeypatch.setattr("watch.TRADES_FILE",    str(trd_f))

        cmd_close(["2330", "98.0"])

        trades = json.loads(trd_f.read_text())
        assert trades[0]["pnl_pct"] == pytest.approx(-2.0)
        assert "❌" in capsys.readouterr().out

    def test_short_profit_recorded(self, tmp_path, monkeypatch):
        pos_f = tmp_path / "positions.json"
        trd_f = tmp_path / "trades.json"
        _write_pos(pos_f, "2454", "short", 100.0)
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))
        monkeypatch.setattr("watch.TRADES_FILE",    str(trd_f))

        cmd_close(["2454", "97.0"])

        trades = json.loads(trd_f.read_text())
        assert trades[0]["pnl_pct"] == pytest.approx(3.0)
        assert trades[0]["pnl_pts"] == pytest.approx(3.0)

    def test_short_loss_recorded(self, tmp_path, monkeypatch):
        pos_f = tmp_path / "positions.json"
        trd_f = tmp_path / "trades.json"
        _write_pos(pos_f, "2454", "short", 100.0)
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))
        monkeypatch.setattr("watch.TRADES_FILE",    str(trd_f))

        cmd_close(["2454", "103.0"])

        trades = json.loads(trd_f.read_text())
        assert trades[0]["pnl_pct"] == pytest.approx(-3.0)

    def test_position_removed_after_close(self, tmp_path, monkeypatch):
        pos_f = tmp_path / "positions.json"
        trd_f = tmp_path / "trades.json"
        _write_pos(pos_f, "2330", "long", 100.0)
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))
        monkeypatch.setattr("watch.TRADES_FILE",    str(trd_f))

        cmd_close(["2330", "102.0"])

        positions = json.loads(pos_f.read_text())
        assert "2330" not in positions

    def test_unknown_symbol_prints_error(self, tmp_path, monkeypatch, capsys):
        pos_f = tmp_path / "positions.json"
        pos_f.write_text("{}")
        monkeypatch.setattr("watch.POSITIONS_FILE", str(pos_f))

        cmd_close(["9999", "100.0"])

        assert "不在部位清單" in capsys.readouterr().out


class TestCmdHistory:
    def _make_trade(self, sym, direction, entry, exit_p, pnl_pts, pnl_pct):
        return {"symbol": sym, "direction": direction, "entry": entry,
                "exit": exit_p, "pnl_pts": pnl_pts, "pnl_pct": pnl_pct,
                "entry_time": "", "exit_time": "2026-05-12 11:00:00"}

    def test_win_rate_calculation(self, tmp_path, monkeypatch, capsys):
        # 3 wins, 2 losses → 60%
        trades = [
            self._make_trade("A", "long",  100, 102,  2.0,  2.0),
            self._make_trade("B", "long",  100,  99, -1.0, -1.0),
            self._make_trade("C", "short", 100,  97,  3.0,  3.0),
            self._make_trade("D", "long",  100,  98, -2.0, -2.0),
            self._make_trade("E", "long",  100, 101,  1.0,  1.0),
        ]
        trd_f = tmp_path / "trades.json"
        _write_trades(trd_f, trades)
        monkeypatch.setattr("watch.TRADES_FILE", str(trd_f))

        cmd_history([])
        out = capsys.readouterr().out
        assert "勝率:60%" in out

    def test_cumulative_pnl(self, tmp_path, monkeypatch, capsys):
        # +2 - 1 + 3 - 2 + 1 = +3%
        trades = [
            self._make_trade("A", "long",  100, 102,  2.0,  2.0),
            self._make_trade("B", "long",  100,  99, -1.0, -1.0),
            self._make_trade("C", "short", 100,  97,  3.0,  3.0),
            self._make_trade("D", "long",  100,  98, -2.0, -2.0),
            self._make_trade("E", "long",  100, 101,  1.0,  1.0),
        ]
        trd_f = tmp_path / "trades.json"
        _write_trades(trd_f, trades)
        monkeypatch.setattr("watch.TRADES_FILE", str(trd_f))

        cmd_history([])
        out = capsys.readouterr().out
        assert "+3.00%" in out

    def test_empty_history(self, tmp_path, monkeypatch, capsys):
        trd_f = tmp_path / "trades.json"
        _write_trades(trd_f, [])
        monkeypatch.setattr("watch.TRADES_FILE", str(trd_f))

        cmd_history([])
        assert "尚無交易記錄" in capsys.readouterr().out

    def test_n_argument_limits_shown(self, tmp_path, monkeypatch, capsys):
        # 5 筆記錄，只顯示最近 2 筆
        trades = [self._make_trade(str(i), "long", 100, 101, 1.0, 1.0) for i in range(5)]
        trd_f = tmp_path / "trades.json"
        _write_trades(trd_f, trades)
        monkeypatch.setattr("watch.TRADES_FILE", str(trd_f))

        cmd_history(["2"])
        out = capsys.readouterr().out
        assert "最近 2 筆" in out
