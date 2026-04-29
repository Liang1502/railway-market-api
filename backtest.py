"""
scanner_v7 策略回測引擎

設計原則（按重要性排序）:
1. 嚴格無 look-ahead
   - 指標全數以 rolling / shift(1) 計算（只吃過去）
   - 進場固定為 T+1 開盤（不是 T 收盤）
   - 月營收 YoY 套用 15 日揭露時滯（台股月營收約每月 10 日公布）
2. 費用區分 swing vs day-trade（當沖證交稅減半）
3. 時間停損 max_hold_days 強制平倉
4. 波動性過濾 + grade_stock 完全沿用 scanner_v7 規則
5. Parquet 快取歷史 K 線，第二次執行 < 10 秒

使用方式:
    python backtest.py                      # 全市場，1.5 年
    python backtest.py --symbols 2330,2454  # 指定標的
    python backtest.py --hold 10            # 覆寫 max_hold_days
    python backtest.py --no-cache           # 強制重抓 K 線

輸出到 backtest_results/<timestamp>/:
    trades.csv          # 所有交易明細
    summary.json        # 總體統計
    equity_curve.csv    # 日 PnL 曲線
    report.md           # 人類可讀摘要
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace as dc_replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests as _requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fubon_neo.sdk import FubonSDK

# 沿用 scanner_v7 的設定、工具、評級邏輯
from scanner_v7 import (
    CFG as SCAN_CFG,
    FUBON_ID, FUBON_PWD, FUBON_CERT_PATH, FUBON_CERT_PWD,
    FINMIND_TOKEN,
    HTTP,
    fetch_all_symbols,
    fetch_daily_kbars,
    detect_volume_unit,
    grade_stock,
    compute_score,
    safe_float,
    v as _v,
    _call_fubon_api,
    _classify_error,
    GUARD,
)


# ==========================================
# 📝 Logging
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("backtest.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("backtest")


# ==========================================
# 🔧 回測設定
# ==========================================
@dataclass(frozen=True)
class BacktestConfig:
    period_days: int = 548           # 1.5 年
    warmup_days: int = 60            # 指標暖機（MA60 / RSI14 / ATR14 全數收斂）

    # 進出場 V7.2 分段收割
    stop_loss: float = 0.05         # 基礎停損比例（ATR不足時退回此值）
    take_profit_1: float = 0.08     # V7.2: 第一停利（50% 出場）
    take_profit_2: float = 0.15     # V7.2: 第二停利（另 50% 出場）
    atr_stop_mult: float = 1.5      # V7.2: ATR 動態停損乘數（≥ stop_loss）
    gap_skip: float = 0.03          # V7.2: T+1 開盤跳空 > 3% 則跳過（追高風險大）
    max_hold_days: int = 20

    # 費用（2.8 折手續費實算）
    # swing    = 0.1425% × 0.28 × 2 (雙邊手續費) + 0.3% (賣方證交稅)  = 0.3798% ≈ 0.38%
    # daytrade = 0.1425% × 0.28 × 2 (雙邊手續費) + 0.15% (當沖稅減半) = 0.2298% ≈ 0.23%
    cost_swing: float = 0.0038
    cost_daytrade: float = 0.0023

    # 資金管理（Fixed Fractional: 每檔固定金額）
    capital_per_trade: float = 100_000
    starting_capital: float = 3_000_000
    max_concurrent: int = 20       # 最大同時持倉（超過則略過新訊號）

    # 進場品質門檻
    min_score: int = 15            # V7.2: 17 分制中 ≥15 為門檻
    yoy_max: float = 500.0         # 上限：超過視為基期失真（與 scanner_v7.CFG.yoy_max 同步）

    # YoY 時滯
    revenue_lag_days: int = 15

    # 執行（K 線下載沿用 scanner_v7 的全域節流，所以 workers 設小即可）
    fubon_workers: int = 2
    data_cache_dir: str = "backtest_cache"


BT_CFG = BacktestConfig()


# ==========================================
# 🗂️ K 線快取（Parquet）
# ==========================================
def get_cache_path(symbol: str) -> Path:
    d = Path(BT_CFG.data_cache_dir)
    d.mkdir(exist_ok=True)
    return d / f"{symbol}.parquet"


MIN_USABLE_BARS = 80           # cache / 抓回後最少要有這麼多筆才算可用
FUGLE_MAX_RANGE_DAYS = 350     # Fugle 強制 < 365 天，留 15 天緩衝


def _fetch_kbars_range(sdk, symbol: str, start: datetime, end: datetime) -> list[dict]:
    """單一區間（≤ FUGLE_MAX_RANGE_DAYS）抓 Fugle K 線。沿用 scanner_v7 的節流。"""
    rest = sdk.marketdata.rest_client.stock
    params = {
        "symbol": symbol,
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
    }
    try:
        res = _call_fubon_api(
            lambda: rest.historical.candles(**params),
            symbol=symbol,
        )
    except Exception as e:
        kind = _classify_error(e)
        if kind == "not_found":
            return []
        log.debug(f"{symbol} kbar range {params['from']}→{params['to']} 失敗: {e}")
        return []

    kbars = getattr(res, "data", None)
    if kbars is None:
        if isinstance(res, list):
            kbars = res
        elif isinstance(res, dict):
            kbars = res.get("data", res.get("candles", []))
        else:
            kbars = []
    if not isinstance(kbars, list):
        return []

    out = []
    for c in kbars:
        close = safe_float(_v(c, "close"))
        date_ = _v(c, "date")
        if close is None or date_ is None:
            continue
        open_ = safe_float(_v(c, "open")) or close
        high = safe_float(_v(c, "high")) or close
        low = safe_float(_v(c, "low")) or close
        volume = safe_float(_v(c, "volume")) or 0
        out.append({
            "date": date_, "open": open_, "close": close,
            "high": high, "low": low, "volume": volume,
        })
    return out


def fetch_kbars_chunked(sdk, symbol: str, days: int) -> list[dict]:
    """把 days 切成 ≤ FUGLE_MAX_RANGE_DAYS 的多段抓取後拼接、去重、排序。"""
    today = datetime.now()
    earliest = today - timedelta(days=days)
    chunks: list[tuple[datetime, datetime]] = []
    cursor_end = today
    while cursor_end > earliest:
        cursor_start = max(earliest, cursor_end - timedelta(days=FUGLE_MAX_RANGE_DAYS))
        chunks.append((cursor_start, cursor_end))
        cursor_end = cursor_start - timedelta(days=1)

    seen: set = set()
    merged: list[dict] = []
    for start, end in chunks:
        bars = _fetch_kbars_range(sdk, symbol, start, end)
        if not bars:
            # 該段空就跳過（可能是早期未上市）
            continue
        for b in bars:
            d = b["date"]
            if d in seen:
                continue
            seen.add(d)
            merged.append(b)

    merged.sort(key=lambda x: x["date"])
    return merged


def load_kbars_cached(sdk, symbol: str, days: int, use_cache: bool = True) -> pd.DataFrame:
    cache = get_cache_path(symbol)
    min_start = datetime.now() - timedelta(days=days)

    if use_cache and cache.exists():
        try:
            df = pd.read_parquet(cache)
            df["date"] = pd.to_datetime(df["date"])
            if len(df) >= MIN_USABLE_BARS and df["date"].min() <= min_start + timedelta(days=60):
                return df
            log.debug(
                f"{symbol} cache 過短（rows={len(df)}），重抓"
            )
        except Exception:
            pass

    # 分段抓取（自動處理 Fugle 1 年限制）
    kbars = fetch_kbars_chunked(sdk, symbol, days)
    if not kbars:
        return pd.DataFrame()
    df = pd.DataFrame(kbars)
    df["date"] = pd.to_datetime(df["date"])
    if "open" not in df.columns:
        df["open"] = df["close"]
    if len(df) >= MIN_USABLE_BARS:
        try:
            df.to_parquet(cache, index=False)
        except Exception:
            log.debug(f"{symbol} parquet save failed")
    return df


# ==========================================
# 📊 指標計算（全 DataFrame 版；與 scanner_v7 邏輯一致）
# ==========================================
def calculate_indicators_full(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("date").reset_index(drop=True)
    for c in ("open", "close", "high", "low"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df["MA5"] = df["close"].rolling(SCAN_CFG.ma_short).mean()
    df["MA20"] = df["close"].rolling(SCAN_CFG.ma_mid).mean()
    df["MA60"] = df["close"].rolling(SCAN_CFG.ma_long).mean()

    # Wilder RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / SCAN_CFG.rsi_period, adjust=False,
                        min_periods=SCAN_CFG.rsi_period).mean()
    avg_loss = loss.ewm(alpha=1 / SCAN_CFG.rsi_period, adjust=False,
                        min_periods=SCAN_CFG.rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["RSI_slope"] = df["RSI"].diff().rolling(3).mean()

    # ATR
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1 / SCAN_CFG.atr_period, adjust=False,
                       min_periods=SCAN_CFG.atr_period).mean()

    # 波動率
    df["range_pct"] = (df["high"] - df["low"]) / df["close"]
    df["volatility"] = df["range_pct"].rolling(SCAN_CFG.ma_mid).mean()

    # 量能、前高
    df["vol_ma20"] = df["volume"].rolling(SCAN_CFG.ma_mid).mean()
    df["high20"] = df["high"].shift(1).rolling(SCAN_CFG.ma_mid).max()

    # MA20 斜率（5 日前 vs 今日，與 scanner_v7 邏輯一致）
    df["MA20_slope"] = (df["MA20"] - df["MA20"].shift(5)) / df["MA20"].shift(5)

    # 黃金交叉（近 N 日任一天發生）
    cross = (df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1))
    df["recent_cross"] = cross.rolling(SCAN_CFG.cross_lookback).apply(
        lambda x: x.any(), raw=True
    ).fillna(0).astype(bool)

    # V7.2: 近期突破（最近 breakout_lookback 日任一天 close > high20）
    cross_hi = df["close"] > df["high20"]
    df["broke_high20_recent"] = cross_hi.rolling(SCAN_CFG.breakout_lookback).apply(
        lambda x: bool(x.any()), raw=True
    ).fillna(False).astype(bool)

    # V7.2: MA5 回測確認（收盤在 MA5 以上且距離 ≤ ma5_pullback_max）
    dist_from_ma5 = (df["close"] - df["MA5"]) / df["MA5"].replace(0, np.nan)
    df["near_ma5_pullback"] = (
        (df["close"] >= df["MA5"]) & (dist_from_ma5 <= SCAN_CFG.ma5_pullback_max)
    ).fillna(False)

    # V7.2: ATR/price 比率
    df["atr_price_ratio"] = df["ATR"] / df["close"].replace(0, np.nan)

    return df


def row_to_ind(row: pd.Series) -> dict:
    """把 DataFrame 一列轉成 grade_stock / compute_score 吃的 ind dict。"""
    return {
        "ma5": safe_float(row.get("MA5")),
        "ma20": safe_float(row.get("MA20")),
        "ma60": safe_float(row.get("MA60")),
        "rsi": safe_float(row.get("RSI")),
        "rsi_slope": safe_float(row.get("RSI_slope")) or 0.0,
        "ma20_slope": safe_float(row.get("MA20_slope")) or 0.0,
        "atr": safe_float(row.get("ATR")),
        "volatility": safe_float(row.get("volatility")),
        "vol_today": safe_float(row.get("volume")),
        "vol_ma20": safe_float(row.get("vol_ma20")),
        "high20": safe_float(row.get("high20")),
        "close": safe_float(row.get("close")),
        "recent_cross": bool(row.get("recent_cross", False)),
        "broke_high20_recent": bool(row.get("broke_high20_recent", False)),
        "near_ma5_pullback": bool(row.get("near_ma5_pullback", False)),
        "atr_price_ratio": safe_float(row.get("atr_price_ratio")),
    }


# ==========================================
# 💰 YoY 時間序列（嚴格套用揭露時滯）
# ==========================================
def _load_local_revenues() -> dict[str, list[dict]]:
    """優先從 backtest_cache/revenues.parquet 讀取（build_revenue_cache.py 產出）。

    回傳 schema：{stock_id: [{date, revenue}, ...]}，date 統一成月底（YYYY-MM-DD）。
    回傳空 dict 表示快取不存在或格式異常，呼叫端應退回 FinMind 路徑。
    """
    p = Path("backtest_cache") / "revenues.parquet"
    if not p.exists():
        return {}
    try:
        df = pd.read_parquet(p)
    except Exception as e:
        log.warning(f"讀取本地營收快取失敗：{e}")
        return {}
    if df.empty:
        return {}

    grouped: dict[str, list[dict]] = {}
    for _, r in df.iterrows():
        sid = str(r["stock_id"])
        y, m = int(r["year"]), int(r["month"])
        date_str = f"{y}-{m:02d}-01"
        rev = float(r["revenue"]) if pd.notna(r["revenue"]) else 0.0
        # 直接帶入快取已算好的 yoy_pct，省去回測時重算需要去年同月資料的問題
        yoy_pre = float(r["yoy_pct"]) if pd.notna(r.get("yoy_pct", float("nan"))) else None
        grouped.setdefault(sid, []).append({"date": date_str, "revenue": rev, "yoy_pct": yoy_pre})
    for items in grouped.values():
        items.sort(key=lambda x: x["date"])

    log.info(f"✅ 從本地快取載入 {len(grouped):,} 檔營收（共 {len(df):,} 筆月度）")
    return grouped


def fetch_all_revenues() -> dict[str, list[dict]]:
    """先讀本地 parquet 快取（推薦），失敗才退 FinMind 批次。"""
    local = _load_local_revenues()
    if local:
        return local

    log.info("本地營收快取不存在，回退 FinMind 批次（免費版可能 400）...")
    today = datetime.now()
    # 分 5 段避開 FinMind 單次上限
    anchors = [730, 584, 438, 292, 146, 0]
    rows = []
    aborted_400 = False
    for i in range(len(anchors) - 1):
        if aborted_400:
            break
        start = (today - timedelta(days=anchors[i])).strftime("%Y-%m-%d")
        end = (today - timedelta(days=anchors[i + 1])).strftime("%Y-%m-%d")
        params = {
            "dataset": "TaiwanStockMonthRevenue",
            "start_date": start, "end_date": end,
        }
        if FINMIND_TOKEN:
            params["token"] = FINMIND_TOKEN
        try:
            res = HTTP.get("https://api.finmindtrade.com/api/v4/data",
                           params=params, timeout=60)
            res.raise_for_status()
            chunk = res.json().get("data", [])
            log.info(f"  {start}→{end}: {len(chunk):,} 筆")
            rows.extend(chunk)
            time.sleep(0.8)
        except _requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code == 400:
                log.warning(
                    "FinMind 400：免費版 token 不支援批次月營收，回測將以 YoY=None 放行"
                )
                aborted_400 = True
            else:
                log.warning(f"營收區段 {start} HTTP {code}：{e}")
        except Exception as e:
            log.warning(f"營收區段 {start} 抓取失敗：{e}")

    # 去重
    seen = set()
    uniq = []
    for r in rows:
        k = (r.get("stock_id"), r.get("date"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    log.info(f"去重後 {len(uniq):,} 筆")

    grouped: dict[str, list[dict]] = {}
    for r in uniq:
        sid = r.get("stock_id")
        if sid:
            grouped.setdefault(sid, []).append(r)
    for items in grouped.values():
        items.sort(key=lambda x: x.get("date", ""))
    return grouped


def build_yoy_schedule(rows: list[dict], lag_days: int) -> list[tuple[datetime, Optional[float]]]:
    """
    回傳 [(release_date, yoy), ...] 排序 list。
    release_date = 月份 + lag_days（揭露時滯）。

    優先使用快取已算好的 yoy_pct（由 build_revenue_cache.py 對 MOPS 計算，
    含 2023 年比較基期，無需本地有去年同月資料）。
    若 yoy_pct 為 None 才嘗試自行計算（向前找同月去年資料）。
    """
    if not rows:
        return []
    schedule: list[tuple[datetime, Optional[float]]] = []
    for i, cur in enumerate(rows):
        cur_date = cur.get("date", "")
        if len(cur_date) < 7:
            continue
        # 優先用快取的預算值
        yoy = cur.get("yoy_pct")
        if yoy is None:
            yr = int(cur_date[:4])
            mo = cur_date[5:7]
            prev = next(
                (x for x in rows[:i] if x.get("date", "").startswith(f"{yr - 1}-{mo}")),
                None,
            )
            if prev:
                try:
                    rev_now = float(cur.get("revenue", 0) or 0)
                    rev_prev = float(prev.get("revenue", 0) or 0)
                    yoy = ((rev_now - rev_prev) / rev_prev * 100) if rev_prev else None
                except Exception:
                    yoy = None
        if yoy is None:
            continue
        release = pd.to_datetime(cur_date) + timedelta(days=lag_days)
        schedule.append((release, float(yoy)))
    schedule.sort(key=lambda x: x[0])
    return schedule


def yoy_at_date(schedule: list, target: pd.Timestamp) -> Optional[float]:
    """回傳 target 當日可用的最新 YoY。Binary search。"""
    if not schedule:
        return None
    dates = [s[0] for s in schedule]
    idx = bisect.bisect_right(dates, target)
    if idx == 0:
        return None
    return schedule[idx - 1][1]


# ==========================================
# 📊 大盤 TAIEX 濾網（使用 0050 作代理）
# ==========================================
def load_taiex_filter(cache_dir: str) -> Optional[pd.DataFrame]:
    """從 backtest_cache/0050.parquet 讀取 0050 K 線並計算 MA20/MA60。"""
    p = Path(cache_dir) / "0050.parquet"
    if not p.exists():
        log.warning("0050.parquet 不存在，TAIEX 濾網停用")
        return None
    try:
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["MA20"] = df["close"].rolling(20).mean()
        df["MA60"] = df["close"].rolling(60).mean()
        log.info(f"✅ TAIEX 濾網（0050）載入：{len(df)} 根 K 棒")
        return df
    except Exception as e:
        log.warning(f"TAIEX 濾網載入失敗：{e}，停用")
        return None


def build_taiex_schedule(taiex_df: pd.DataFrame) -> list:
    """回傳 [(date, is_bullish), ...] 排序 list，供 bisect 快速查詢。"""
    df = taiex_df.copy()
    bullish = (
        (df["close"] > df["MA20"]) & (df["MA20"] > df["MA60"])
    ).fillna(False)
    return [(pd.Timestamp(d), bool(b)) for d, b in zip(df["date"], bullish)]


def is_taiex_bullish(schedule: list, date: pd.Timestamp) -> bool:
    """查詢 date 當日或之前最近一個交易日的 TAIEX 多空狀態。"""
    if not schedule:
        return True  # 無資料預設放行
    dates = [s[0] for s in schedule]
    idx = bisect.bisect_right(dates, date)
    if idx == 0:
        return True  # 比最早資料還早，預設放行
    return schedule[idx - 1][1]


# ==========================================
# 🎯 單筆交易模擬
# ==========================================
@dataclass
class Trade:
    symbol: str
    score: int
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    hold_days: int
    gross_return: float
    net_return: float
    cost: float
    exit_reason: str
    is_daytrade: bool
    yoy: Optional[float]
    industry: str = ""


def simulate_trade(df: pd.DataFrame, entry_idx: int, signal_date: str,
                   symbol: str, score: int, yoy: Optional[float],
                   config: BacktestConfig,
                   atr: Optional[float] = None) -> Optional[Trade]:
    """
    V7.2: 分段停利（50%@TP1, 50%@TP2）+ ATR 動態停損 + 跳空跳過。

    df 必須含 open/high/low/close，entry_idx 為 T+1（進場當日）。
    回傳 None 表示跳空跳過或資料不足，不進場。
    """
    if entry_idx <= 0 or entry_idx >= len(df):
        return None

    prev_close = float(df.iloc[entry_idx - 1]["close"])
    entry_row = df.iloc[entry_idx]
    entry_price = float(entry_row["open"])
    if not entry_price or entry_price <= 0:
        return None

    # 跳空跳過：T+1 開盤比前收高超過 gap_skip% → 不追
    if config.gap_skip > 0 and prev_close > 0 and entry_price > prev_close * (1 + config.gap_skip):
        return None

    # ATR 動態停損：取 max(stop_loss, atr_stop_mult × ATR/entry)
    if atr and atr > 0:
        atr_stop_frac = config.atr_stop_mult * atr / entry_price
        stop_dist = max(config.stop_loss, atr_stop_frac)
    else:
        stop_dist = config.stop_loss

    stop_price = entry_price * (1 - stop_dist)
    tp1_price = entry_price * (1 + config.take_profit_1)
    tp2_price = entry_price * (1 + config.take_profit_2)

    # 狀態：tp1_return = None 表示 phase 1；有值表示已達 TP1，進入 phase 2
    tp1_return: Optional[float] = None
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross: Optional[float] = None

    for hold_day in range(config.max_hold_days):
        day_idx = entry_idx + hold_day
        if day_idx >= len(df):
            day_idx = len(df) - 1
            close_p = float(df.iloc[day_idx]["close"])
            half2 = (close_p - entry_price) / entry_price
            gross = (0.5 * tp1_return + 0.5 * half2) if tp1_return is not None else half2
            exit_idx, exit_price, exit_reason = day_idx, close_p, "data_end"
            break

        day = df.iloc[day_idx]
        day_open = float(day["open"])
        day_high = float(day["high"])
        day_low = float(day["low"])

        # 當前有效停損與停利目標
        cur_stop = stop_price if tp1_return is None else entry_price  # phase2: stop=BE
        cur_tp = tp1_price if tp1_return is None else tp2_price

        if hold_day == 0:
            # 進場當日：不判斷跳空，只看盤中觸及（當沖）
            if day_low <= cur_stop:
                half2 = (cur_stop - entry_price) / entry_price
                gross = (0.5 * tp1_return + 0.5 * half2) if tp1_return is not None else half2
                exit_idx, exit_price, exit_reason = day_idx, cur_stop, "stop_loss_sameday"
                break
            if day_high >= cur_tp:
                if tp1_return is None:
                    tp1_return = (tp1_price - entry_price) / entry_price
                    if day_high >= tp2_price:
                        half2 = (tp2_price - entry_price) / entry_price
                        gross = 0.5 * tp1_return + 0.5 * half2
                        exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit_sameday"
                        break
                    continue  # TP1 hit, continue to phase 2
                else:
                    half2 = (tp2_price - entry_price) / entry_price
                    gross = 0.5 * tp1_return + 0.5 * half2
                    exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit_sameday"
                    break
        else:
            # 後續日：先檢查開盤跳空
            if day_open <= cur_stop:
                half2 = (day_open - entry_price) / entry_price
                gross = (0.5 * tp1_return + 0.5 * half2) if tp1_return is not None else half2
                exit_idx, exit_price, exit_reason = day_idx, day_open, "stop_loss_gap"
                break
            if day_open >= cur_tp:
                if tp1_return is None:
                    tp1_return = (day_open - entry_price) / entry_price
                    if day_open >= tp2_price:
                        half2 = (tp2_price - entry_price) / entry_price
                        gross = 0.5 * tp1_return + 0.5 * half2
                        exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit_gap"
                        break
                    continue  # TP1 gap hit, phase 2
                else:
                    half2 = (tp2_price - entry_price) / entry_price
                    gross = 0.5 * tp1_return + 0.5 * half2
                    exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit_gap"
                    break
            # 盤中觸及（保守：同日若停損停利都觸及，先停損）
            if day_low <= cur_stop:
                half2 = (cur_stop - entry_price) / entry_price
                gross = (0.5 * tp1_return + 0.5 * half2) if tp1_return is not None else half2
                exit_idx, exit_price, exit_reason = day_idx, cur_stop, "stop_loss"
                break
            if day_high >= cur_tp:
                if tp1_return is None:
                    tp1_return = (tp1_price - entry_price) / entry_price
                    if day_high >= tp2_price:
                        half2 = (tp2_price - entry_price) / entry_price
                        gross = 0.5 * tp1_return + 0.5 * half2
                        exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit"
                        break
                    continue  # TP1 hit, phase 2
                else:
                    half2 = (tp2_price - entry_price) / entry_price
                    gross = 0.5 * tp1_return + 0.5 * half2
                    exit_idx, exit_price, exit_reason = day_idx, tp2_price, "take_profit"
                    break
    else:
        # 時間停損（持滿 max_hold_days 仍未出場）
        day_idx = min(entry_idx + config.max_hold_days - 1, len(df) - 1)
        close_p = float(df.iloc[day_idx]["close"])
        half2 = (close_p - entry_price) / entry_price
        gross = (0.5 * tp1_return + 0.5 * half2) if tp1_return is not None else half2
        exit_idx, exit_price, exit_reason = day_idx, close_p, "time_stop"

    hold_days = exit_idx - entry_idx + 1
    is_daytrade = (hold_days == 1 and exit_reason.endswith("sameday"))
    cost = config.cost_daytrade if is_daytrade else config.cost_swing
    net = gross - cost

    return Trade(
        symbol=symbol,
        score=score,
        signal_date=signal_date,
        entry_date=df.iloc[entry_idx]["date"].strftime("%Y-%m-%d"),
        exit_date=df.iloc[exit_idx]["date"].strftime("%Y-%m-%d"),
        entry_price=round(entry_price, 2),
        exit_price=round(exit_price, 2),
        hold_days=hold_days,
        gross_return=round(gross, 4),
        net_return=round(net, 4),
        cost=cost,
        exit_reason=exit_reason,
        is_daytrade=is_daytrade,
        yoy=yoy,
    )


# ==========================================
# 🔁 單檔回測
# ==========================================
def backtest_symbol(df: pd.DataFrame, symbol: str,
                    yoy_schedule: list, vol_unit: str,
                    config: BacktestConfig,
                    diag: Optional[dict] = None,
                    taiex_schedule: Optional[list] = None) -> list[Trade]:
    """diag: 若提供，會累計各種 skip 原因到此 dict（多執行緒不安全，僅單緒用）。"""
    def bump(key):
        if diag is not None:
            diag[key] = diag.get(key, 0) + 1

    if df.empty or len(df) < SCAN_CFG.min_kbars + 2:
        bump("too_short_df")
        return []

    df_ind = calculate_indicators_full(df)

    trades: list[Trade] = []
    backtest_start = pd.Timestamp(datetime.now() - timedelta(days=config.period_days))

    # 從 warmup_days 之後開始掃（避開指標暖機期的假訊號）
    start_idx = max(config.warmup_days, SCAN_CFG.min_kbars)
    if start_idx >= len(df_ind) - 1:
        bump("warmup_exhausts_df")
        return []

    for i in range(start_idx, len(df_ind) - 1):
        row = df_ind.iloc[i]
        signal_date = row["date"]
        if signal_date < backtest_start:
            bump("before_period")
            continue
        bump("evaluated")

        # V7.2: TAIEX 大盤濾網（只在多頭市場做多）
        if taiex_schedule is not None:
            if not is_taiex_bullish(taiex_schedule, signal_date):
                bump("taiex_bearish")
                continue

        # 均量（過去 20 日）
        recent_vols = df_ind.iloc[i - SCAN_CFG.ma_mid + 1:i + 1]["volume"]
        recent_vols = recent_vols[recent_vols > 0]
        if len(recent_vols) == 0:
            bump("no_volume")
            continue
        avg_vol = recent_vols.mean()
        avg_lots = avg_vol / 1000 if vol_unit == "share" else avg_vol
        if avg_lots < SCAN_CFG.min_avg_lots:
            bump("low_volume")
            continue

        # YoY (with lag)；超過上限視為基期失真，當作無資料處理
        yoy = yoy_at_date(yoy_schedule, signal_date)
        if yoy is not None and yoy > config.yoy_max:
            bump("yoy_too_high")
            yoy = None

        ind = row_to_ind(row)
        price = ind["close"]
        if not price:
            bump("no_price")
            continue

        grade, reasons = grade_stock(ind, price, yoy)
        if grade == "A":
            bump("grade_A")
        elif grade == "B":
            bump("grade_B")
            continue
        else:
            bump("grade_C")
            continue
        score = compute_score(ind, price, yoy, reasons)

        if score < config.min_score:
            bump("low_score")
            continue

        # 進場 T+1 open（傳入 ATR 供動態停損計算）
        atr_val = safe_float(row.get("ATR"))
        trade = simulate_trade(
            df_ind, i + 1,
            signal_date.strftime("%Y-%m-%d"),
            symbol, score, yoy, config, atr_val,
        )
        if trade:
            bump("trade_entered")
            trades.append(trade)
        else:
            bump("entry_skipped")  # 跳空或資料不足

    return trades


# ==========================================
# 🏦 持倉限制（portfolio level）
# ==========================================
def apply_position_limit(trades: list[Trade], max_concurrent: int) -> list[Trade]:
    """按進場日期排序後貪婪接受，確保任一時點同時持倉不超過 max_concurrent。

    同日有多筆訊號時，優先取 score 高的。
    """
    if not trades:
        return trades
    sorted_t = sorted(trades, key=lambda t: (t.entry_date, -t.score))
    open_exits: list[pd.Timestamp] = []
    accepted: list[Trade] = []
    for trade in sorted_t:
        entry_dt = pd.to_datetime(trade.entry_date)
        # 移除已平倉部位
        open_exits = [ed for ed in open_exits if ed >= entry_dt]
        if len(open_exits) < max_concurrent:
            accepted.append(trade)
            open_exits.append(pd.to_datetime(trade.exit_date))
    return accepted


# ==========================================
# 📈 分析統計
# ==========================================
def compute_max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return float(dd.min())


def compute_max_concurrent(trades_df: pd.DataFrame) -> int:
    if trades_df.empty:
        return 0
    events = []
    for _, r in trades_df.iterrows():
        events.append((pd.to_datetime(r["entry_date"]), +1))
        events.append((pd.to_datetime(r["exit_date"]) + timedelta(days=1), -1))
    events.sort()
    cur = 0
    mx = 0
    for _, delta in events:
        cur += delta
        mx = max(mx, cur)
    return mx


def build_equity_curve(trades_df: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """以每筆交易的 exit_date 計算累計 PnL（元）。"""
    if trades_df.empty:
        return pd.DataFrame(columns=["date", "daily_pnl", "cum_pnl", "equity"])
    df = trades_df.copy()
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df["pnl_dollar"] = df["net_return"] * config.capital_per_trade
    daily = df.groupby("exit_date")["pnl_dollar"].sum().reset_index()
    daily.columns = ["date", "daily_pnl"]
    daily["cum_pnl"] = daily["daily_pnl"].cumsum()
    daily["equity"] = daily["cum_pnl"] + config.starting_capital
    return daily


def analyze(trades: list[Trade], config: BacktestConfig) -> dict:
    if not trades:
        return {"total_trades": 0, "note": "no_trades"}

    df = pd.DataFrame([asdict(t) for t in trades])
    total = len(df)
    wins = (df["net_return"] > 0).sum()
    win_rate = wins / total

    winners = df[df["net_return"] > 0]["net_return"]
    losers = df[df["net_return"] <= 0]["net_return"]

    avg_win = float(winners.mean()) if len(winners) else 0.0
    avg_loss = float(losers.mean()) if len(losers) else 0.0
    profit_factor = (
        float(winners.sum() / abs(losers.sum())) if len(losers) and losers.sum() != 0 else float("inf")
    )

    equity_curve = build_equity_curve(df, config)
    if not equity_curve.empty:
        equity_curve["equity"] = equity_curve["cum_pnl"] + config.starting_capital
        mdd = compute_max_drawdown(equity_curve["equity"])
        total_pnl = float(equity_curve["cum_pnl"].iloc[-1])
        period_years = config.period_days / 365
        # 年化以初始資金計
        annual_return = (1 + total_pnl / config.starting_capital) ** (1 / period_years) - 1
        # 夏普（日報酬近似）
        if len(equity_curve) > 1:
            daily_returns = equity_curve["daily_pnl"] / config.starting_capital
            sharpe = (
                float(daily_returns.mean() / daily_returns.std() * math.sqrt(252))
                if daily_returns.std() > 0 else 0.0
            )
        else:
            sharpe = 0.0
    else:
        mdd = 0.0
        total_pnl = 0.0
        annual_return = 0.0
        sharpe = 0.0

    max_concurrent = compute_max_concurrent(df)

    # 分桶分析
    exit_breakdown = df["exit_reason"].value_counts().to_dict()
    daytrade_ratio = float(df["is_daytrade"].mean())

    # 依 score 分桶
    df["score_bucket"] = pd.cut(
        df["score"], bins=[-1, 8, 11, 20],
        labels=["low(≤8)", "mid(9-11)", "high(≥12)"]
    )
    by_score = df.groupby("score_bucket", observed=True).agg(
        count=("net_return", "size"),
        win_rate=("net_return", lambda x: (x > 0).mean()),
        avg_return=("net_return", "mean"),
    ).reset_index()
    by_score["score_bucket"] = by_score["score_bucket"].astype(str)

    # 依產業分類
    by_industry: list[dict] = []
    if "industry" in df.columns:
        ind_g = df.groupby("industry", observed=True).agg(
            count=("net_return", "size"),
            win_rate=("net_return", lambda x: round((x > 0).mean(), 4)),
            avg_return=("net_return", lambda x: round(x.mean(), 4)),
            profit_factor=("net_return", lambda x: (
                round(x[x > 0].sum() / abs(x[x <= 0].sum()), 2)
                if x[x <= 0].sum() != 0 else None
            )),
        ).reset_index().sort_values("avg_return", ascending=False)
        by_industry = ind_g.to_dict(orient="records")

    return {
        "period_days": config.period_days,
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win * 100, 2),
        "avg_loss_pct": round(avg_loss * 100, 2),
        "avg_return_pct": round(float(df["net_return"].mean()) * 100, 2),
        "median_return_pct": round(float(df["net_return"].median()) * 100, 2),
        "expectancy_pct": round(float(df["net_return"].mean()) * 100, 2),
        "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else None,
        "total_pnl": round(total_pnl, 0),
        "annual_return_pct": round(annual_return * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_concurrent_positions": int(max_concurrent),
        "daytrade_ratio": round(daytrade_ratio, 3),
        "avg_hold_days": round(float(df["hold_days"].mean()), 1),
        "exit_breakdown": exit_breakdown,
        "by_score_bucket": by_score.to_dict(orient="records"),
        "by_industry": by_industry,
    }


# ==========================================
# 📄 輸出
# ==========================================
def write_report(trades: list[Trade], summary: dict, config: BacktestConfig,
                 out_dir: Path) -> None:
    df = pd.DataFrame([asdict(t) for t in trades])
    df.to_csv(out_dir / "trades.csv", index=False, encoding="utf-8-sig")

    (out_dir / "summary.json").write_text(
        json.dumps({"config": asdict(config), "summary": summary},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    equity = build_equity_curve(df, config)
    equity.to_csv(out_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")

    # Markdown 摘要
    lines = [
        f"# 回測報告 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- 回測期間: {config.period_days} 天",
        f"- 進出場: T+1 open / SL max({config.stop_loss*100:.0f}%,{config.atr_stop_mult}×ATR) "
        f"/ TP1 {config.take_profit_1*100:.0f}%(50%) + TP2 {config.take_profit_2*100:.0f}%(50%) "
        f"/ Max Hold {config.max_hold_days} 天",
        f"- 費用假設: swing {config.cost_swing*100:.2f}% / daytrade {config.cost_daytrade*100:.2f}%",
        f"- 資金: 每檔 {config.capital_per_trade:,.0f} / 上限同時 {config.max_concurrent} 檔",
        "",
        "## 績效總表",
        "",
        f"- 交易筆數: **{summary.get('total_trades', 0)}**",
        f"- 勝率: **{summary.get('win_rate', 0)*100:.1f}%**",
        f"- 平均獲利: {summary.get('avg_win_pct', 0):.2f}%",
        f"- 平均虧損: {summary.get('avg_loss_pct', 0):.2f}%",
        f"- 期望值/筆: **{summary.get('expectancy_pct', 0):.2f}%**",
        f"- 獲利因子: {summary.get('profit_factor', 'N/A')}",
        f"- 累積 PnL: **{summary.get('total_pnl', 0):,.0f}** 元",
        f"- 年化報酬: **{summary.get('annual_return_pct', 0):.2f}%**",
        f"- 最大回撤: **{summary.get('max_drawdown_pct', 0):.2f}%**",
        f"- Sharpe: {summary.get('sharpe_ratio', 0):.2f}",
        f"- 最大同時持倉: {summary.get('max_concurrent_positions', 0)} 檔",
        f"- 當沖比例: {summary.get('daytrade_ratio', 0)*100:.1f}%",
        f"- 平均持倉天數: {summary.get('avg_hold_days', 0)}",
        "",
        "## 出場原因分佈",
        "",
    ]
    for k, v in (summary.get("exit_breakdown") or {}).items():
        lines.append(f"- {k}: {v}")

    lines += ["", "## 依 Score 分桶", ""]
    for bucket in summary.get("by_score_bucket", []):
        lines.append(
            f"- {bucket['score_bucket']}: 筆數 {bucket['count']}, "
            f"勝率 {bucket['win_rate']*100:.1f}%, "
            f"平均 {bucket['avg_return']*100:.2f}%"
        )

    by_industry = summary.get("by_industry", [])
    if by_industry:
        lines += ["", "## 依產業分類（依平均報酬排序）", "",
                  "| 產業 | 筆數 | 勝率 | 平均報酬 | 獲利因子 |",
                  "|------|------|------|---------|---------|"]
        for row in by_industry:
            pf = row.get("profit_factor")
            pf_str = f"{pf:.2f}" if pf is not None else "∞"
            lines.append(
                f"| {row['industry']} | {row['count']} | "
                f"{row['win_rate']*100:.1f}% | "
                f"{row['avg_return']*100:+.2f}% | {pf_str} |"
            )

    lines += [
        "",
        "## 免責",
        "",
        "本回測存在固定未來未知偏差：參數選擇、survivorship（使用當下的上市清單回推 1.5 年、"
        "已下市股票不在樣本）、FinMind 歷史資料品質、滑價未模擬。實盤績效應打折看待。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ==========================================
# 🚀 主程式
# ==========================================
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default="",
                    help="逗號分隔，不給則跑全市場")
    ap.add_argument("--hold", type=int, default=None,
                    help="覆寫 max_hold_days")
    ap.add_argument("--days", type=int, default=None,
                    help="覆寫 period_days")
    ap.add_argument("--min-score", type=int, default=None,
                    help="覆寫 min_score（進場分數門檻）")
    ap.add_argument("--no-cache", action="store_true",
                    help="強制重抓 K 線")
    # scan config overrides（用於敏感度分析，覆寫 scanner_v7.ScanConfig）
    ap.add_argument("--rsi-high", type=float, default=None,
                    help="覆寫 rsi_healthy_high（預設 65）")
    ap.add_argument("--breakout-lookback", type=int, default=None,
                    help="覆寫 breakout_lookback（預設 3）")
    ap.add_argument("--pullback-pct", type=float, default=None,
                    help="覆寫 ma5_pullback_max（預設 0.05）")
    ap.add_argument("--sectors", type=str, default="",
                    help="只跑指定產業（逗號分隔，需符合 stock_info 的 industry_category）")
    ap.add_argument("--no-taiex", action="store_true",
                    help="停用 TAIEX 大盤濾網，在所有市場環境下建倉")
    ap.add_argument("--tp1", type=float, default=None,
                    help="覆寫 take_profit_1（預設 0.08）")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="覆寫 stop_loss（預設 0.05）")
    return ap.parse_args()


def main():
    args = parse_args()
    global BT_CFG, SCAN_CFG

    # ── BacktestConfig overrides ──────────────────────────────────────
    overrides = {}
    if args.hold:       overrides["max_hold_days"] = args.hold
    if args.days:       overrides["period_days"] = args.days
    if args.min_score:  overrides["min_score"] = args.min_score
    if args.tp1 is not None:        overrides["take_profit_1"] = args.tp1
    if args.stop_loss is not None:  overrides["stop_loss"] = args.stop_loss
    if overrides:
        BT_CFG = BacktestConfig(**{**asdict(BT_CFG), **overrides})

    # ── ScanConfig overrides（敏感度分析用）─────────────────────────
    scan_overrides = {}
    if args.rsi_high is not None:
        scan_overrides["rsi_healthy_high"] = args.rsi_high
    if args.breakout_lookback is not None:
        scan_overrides["breakout_lookback"] = args.breakout_lookback
    if args.pullback_pct is not None:
        scan_overrides["ma5_pullback_max"] = args.pullback_pct
    if scan_overrides:
        import scanner_v7 as _sv7
        new_scan_cfg = dc_replace(_sv7.CFG, **scan_overrides)
        _sv7.CFG = new_scan_cfg   # grade_stock / compute_score 使用此全域變數
        SCAN_CFG = new_scan_cfg   # calculate_indicators_full / row_to_ind 使用此模組變數
        log.info(f"ScanConfig 覆寫: {scan_overrides}")

    log.info("=" * 60)
    log.info("Backtest Config:")
    for k, v in asdict(BT_CFG).items():
        log.info(f"  {k}: {v}")
    log.info(f"ScanConfig key params: rsi_healthy_high={SCAN_CFG.rsi_healthy_high}, "
             f"breakout_lookback={SCAN_CFG.breakout_lookback}, "
             f"ma5_pullback_max={SCAN_CFG.ma5_pullback_max}")
    log.info("=" * 60)

    # 登入富邦
    if not (FUBON_ID and FUBON_PWD):
        log.error("缺少 FUBON_ID / FUBON_PWD 環境變數")
        return
    log.info("登入富邦 API...")
    sdk = FubonSDK()
    try:
        sdk.login(FUBON_ID, FUBON_PWD, FUBON_CERT_PATH, FUBON_CERT_PWD)
    except TypeError:
        sdk.login(FUBON_ID)
    sdk.init_realtime()

    vol_unit = detect_volume_unit(sdk)

    # 標的
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = fetch_all_symbols()
    log.info(f"標的數: {len(symbols)}")

    # V7.2: TAIEX 大盤濾網
    if args.no_taiex:
        taiex_schedule = None
        log.info("TAIEX 濾網已停用（--no-taiex）")
    else:
        taiex_df = load_taiex_filter(BT_CFG.data_cache_dir)
        taiex_schedule = build_taiex_schedule(taiex_df) if taiex_df is not None else None
        if taiex_schedule:
            bullish_days = sum(1 for _, b in taiex_schedule if b)
            log.info(f"TAIEX 濾網：{len(taiex_schedule)} 日，其中多頭 {bullish_days} 日")
        else:
            log.warning("TAIEX 濾網停用，將在所有市場環境下建倉")

    # 營收批次
    revenues_by_symbol = fetch_all_revenues()

    # 產業分類
    industry_map: dict[str, str] = {}
    _info_path = Path(BT_CFG.data_cache_dir) / "stock_info.parquet"
    if _info_path.exists():
        try:
            _info = pd.read_parquet(_info_path)
            industry_map = dict(zip(_info["stock_id"].astype(str), _info["industry_category"]))
            log.info(f"✅ 產業分類載入：{len(industry_map)} 檔")
        except Exception as e:
            log.warning(f"產業分類載入失敗：{e}")
    else:
        log.warning("未找到 backtest_cache/stock_info.parquet，請先執行產業資料下載")

    # 產業白名單過濾
    if args.sectors:
        whitelist = {s.strip() for s in args.sectors.split(",") if s.strip()}
        before = len(symbols)
        symbols = [s for s in symbols if industry_map.get(s, "") in whitelist]
        log.info(f"產業白名單 {whitelist}：{before} → {len(symbols)} 檔")

    # 載入 K 線（併發）
    total_kbar_days = BT_CFG.period_days + BT_CFG.warmup_days + 30
    log.info(f"下載 K 線（{total_kbar_days} 天, 併發 {BT_CFG.fubon_workers}）...")
    kbars_by_symbol: dict[str, pd.DataFrame] = {}

    def _load(s):
        return s, load_kbars_cached(sdk, s, total_kbar_days, use_cache=not args.no_cache)

    with ThreadPoolExecutor(max_workers=BT_CFG.fubon_workers) as pool:
        futures = [pool.submit(_load, s) for s in symbols]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                sym, df = fut.result()
                if not df.empty:
                    kbars_by_symbol[sym] = df
            except Exception:
                log.exception("K 線載入失敗")
            if i % 200 == 0:
                log.info(f"  K 線進度 {i}/{len(symbols)}")

    log.info(f"成功載入 {len(kbars_by_symbol)} 檔 K 線")

    # 回測主迴圈
    log.info("開始回測...")
    all_trades: list[Trade] = []
    diag: dict = {}
    t_start = time.time()
    for i, (sym, df) in enumerate(kbars_by_symbol.items(), 1):
        yoy_rows = revenues_by_symbol.get(sym, [])
        yoy_sched = build_yoy_schedule(yoy_rows, BT_CFG.revenue_lag_days)
        trades = backtest_symbol(df, sym, yoy_sched, vol_unit, BT_CFG, diag=diag,
                                 taiex_schedule=taiex_schedule)
        all_trades.extend(trades)
        if i % 200 == 0:
            elapsed = time.time() - t_start
            log.info(
                f"  回測進度 {i}/{len(kbars_by_symbol)}, "
                f"累計訊號 {len(all_trades)}, 耗時 {elapsed:.0f}s"
            )

    # 填入產業分類
    for t in all_trades:
        t.industry = industry_map.get(t.symbol, "未分類")

    log.info(f"回測完成（套用持倉限制前）：共 {len(all_trades)} 筆訊號")
    log.info("📊 篩選漏斗診斷：")
    for k in ("too_short_df", "warmup_exhausts_df", "before_period", "evaluated",
              "taiex_bearish", "no_volume", "low_volume", "no_price", "yoy_too_high",
              "grade_C", "grade_B", "grade_A", "low_score",
              "entry_skipped", "trade_entered"):
        log.info(f"  {k}: {diag.get(k, 0):,}")

    # 套用 portfolio 層級持倉上限
    all_trades = apply_position_limit(all_trades, BT_CFG.max_concurrent)
    log.info(f"套用持倉限制（≤{BT_CFG.max_concurrent}）後：{len(all_trades)} 筆交易")

    # 分析
    summary = analyze(all_trades, BT_CFG)
    log.info("=" * 60)
    log.info("回測摘要：")
    for k, v in summary.items():
        if isinstance(v, (dict, list)):
            continue
        log.info(f"  {k}: {v}")
    log.info("=" * 60)

    # 落地
    out_root = Path("backtest_results") / datetime.now().strftime("%Y%m%d_%H%M")
    out_root.mkdir(parents=True, exist_ok=True)
    write_report(all_trades, summary, BT_CFG, out_root)
    log.info(f"結果輸出：{out_root}")


if __name__ == "__main__":
    main()
