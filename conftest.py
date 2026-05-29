"""
Root-level conftest: patch unavailable external deps before any module import.
This file is loaded by pytest before test collection, so all sys.modules
assignments take effect before analysis.py or backtest.py are imported.
"""
import os
import sys
from unittest.mock import MagicMock

# Ensure project root is on sys.path so test files can import project modules.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# main.py / uploader.py 要求 API_SECRET_TOKEN 必須存在（取消 fallback 後的硬要求）
os.environ.setdefault("API_SECRET_TOKEN", "test_token")

# ── fubon_neo ──────────────────────────────────────────────────────────────
_fubon = MagicMock()
sys.modules.setdefault("fubon_neo", _fubon)
sys.modules.setdefault("fubon_neo.sdk", _fubon)

# ── google.generativeai ────────────────────────────────────────────────────
_google = MagicMock()
sys.modules.setdefault("google", _google)
sys.modules.setdefault("google.generativeai", _google)

# ── scanner_v7 (backtest.py imports CFG and helpers from here) ─────────────
class _ScanCfg:
    ma_short = 5
    ma_mid = 20
    ma_long = 60
    rsi_period = 14
    atr_period = 14
    cross_lookback = 5
    breakout_lookback = 3
    ma5_pullback_max = 0.05
    min_kbars = 60
    min_avg_lots = 100
    rsi_healthy_high = 65


def _safe_float(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _v(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


_sv7 = MagicMock()
_sv7.CFG = _ScanCfg()
_sv7.FUBON_ID = ""
_sv7.FUBON_PWD = ""
_sv7.FUBON_CERT_PATH = ""
_sv7.FUBON_CERT_PWD = ""
_sv7.FINMIND_TOKEN = ""
_sv7.HTTP = MagicMock()
_sv7.fetch_all_symbols = MagicMock(return_value=[])
_sv7.fetch_daily_kbars = MagicMock(return_value=[])
_sv7.detect_volume_unit = MagicMock(return_value="lot")
_sv7.grade_stock = MagicMock(return_value=("A", []))
_sv7.compute_score = MagicMock(return_value=15)
_sv7.safe_float = _safe_float
_sv7.v = _v
_sv7._call_fubon_api = MagicMock()
_sv7._classify_error = MagicMock(return_value="unknown")
_sv7.GUARD = MagicMock()
sys.modules.setdefault("scanner_v7", _sv7)
