# ===============================
# 🔥 V6.3 穩健合併版
# V4.6 穩健架構 + V6.3 評分/進出場邏輯
# ===============================

import time
import requests
import glob
import csv
import threading
import json
import traceback
from datetime import datetime, timedelta, timezone
from collections import deque

import os
from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK
from analysis import extract_analysis_data

load_dotenv()

# ===============================
# 配置區
# ===============================
_BASE           = os.getenv("RAILWAY_API_URL", "https://web-production-641b.up.railway.app")
API_URL         = f"{_BASE}/update"
WISHLIST_URL    = f"{_BASE}/wishlist"
WATCH_LIST_URL  = f"{_BASE}/watch-list"
MY_SECRET_TOKEN = os.getenv("API_SECRET_TOKEN", "ChiaChun_Super_Secret_888")
MY_HOLDINGS    = ["1815", "2481", "3324", "5351", "6789", "5386", "2493", "6568"]

TW_TZ = timezone(timedelta(hours=8))

# 節流控制
REST_CALL_COOLDOWN_PER_SYMBOL = 30
GLOBAL_REST_MIN_INTERVAL      = 1.5
PRICE_ONLY_COOLDOWN_PER_SYMBOL = 3
WISHLIST_UPLOAD_COOLDOWN      = 60
HEARTBEAT_INTERVAL            = 120
YDAY_REFRESH_COOLDOWN         = 60
MAX_ERROR_COUNT               = 3

_rate_limited_until = 0  # Fugle 429 全域退避時間戳

# 診斷開關
DIAG_MODE = False

# 評分權重
WEIGHTS = {
    "kd_gold_cross":  15,
    "kd_death_cross": -15,
    "above_vwap":     20,
    "below_vwap":     -20,
    "break_y_high":   30,
    "break_y_low":    -20,
    "rebound_near_low": 10,
    "volume_expand":  20,
    "overheat":       -15,
    "fake_break":     -25,
    "trend_up":       15,
    "trend_down":      -10,
}

# V6.4 進出場參數（模擬追蹤，不真實下單）
MAX_HISTORY      = 5      # 評分歷史保留幾筆
MAX_LOSS_STREAK  = 3      # 連續虧損幾次後停止進場
ENTRY_THRESHOLD  = 60     # V6 分數超過此值才建立進場計畫
MOMENTUM_MIN     = 15     # 動能至少需達此值才執行進場計畫
STOP_LOSS_RATIO  = 0.98   # 基礎停損比例
TRAIL_TRIGGER_1  = 0.02   # 獲利 2% 啟動第一段移動停損
TRAIL_RATIO_1    = 0.995  # 第一段移動停損比例
TRAIL_TRIGGER_2  = 0.03   # 獲利 3% 啟動第二段移動停損
TRAIL_RATIO_2    = 0.997  # 第二段移動停損比例（更緊）

# ===============================
# 核心工具
# ===============================
def v(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, list):
        return v(obj[0], key, default) if obj else default
    try:
        res = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
        if res is None and key == "last":
            res = v(obj, "last_price", default)
        return res
    except Exception:
        return default

def now_tw():
    return datetime.now(TW_TZ)

def today_tw_str():
    return now_tw().strftime("%Y-%m-%d")

def is_market_hours():
    now = now_tw()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 900 <= t <= 1335

def safe_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None

def set_payload_price(data, price, source=None):
    price = safe_float(price)
    if price is None or price <= 0:
        return False

    data["current_price"] = price
    data["last"] = price
    data["_runtime_price"] = price
    data["score_price"] = price
    if source:
        data["_price_source"] = source
        data["_price_ts"] = now_tw().isoformat()

    if isinstance(data.get("price"), dict):
        data["price"]["last"] = price
    else:
        data["price"] = {"last": price}
    return True

def sync_vwap_risk(data, price, vwap):
    price = safe_float(price)
    vwap = safe_float(vwap)
    if not price or not vwap:
        return

    distance = round(((price - vwap) / vwap) * 100, 2)
    risk = data.setdefault("risk_control", {})
    risk["vwap_distance"] = distance

    if distance >= 2.0 and (data.get("structure") or {}).get("dominance") == "buy":
        data["decision"] = "avoid_long"
        entry = data.setdefault("entry_signal", {})
        entry["long_trigger"] = False
        entry["long_reason"] = f"⚠️ 乖離 VWAP 達 {distance}%，FOMO 禁令生效"

def parse_date_like(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.astimezone(TW_TZ) if val.tzinfo else val.replace(tzinfo=TW_TZ)
    s = str(val).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            return d.replace(tzinfo=TW_TZ)
        except Exception:
            pass
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.astimezone(TW_TZ) if dt.tzinfo else dt.replace(tzinfo=TW_TZ)
    except Exception:
        return None

def normalize_symbol(sym):
    if sym is None:
        return None
    return str(sym).strip()

# ===============================
# 初始化
# ===============================
print("1. 正在登入富邦 API...")
sdk = FubonSDK()
sdk.login(
    os.getenv("FUBON_ACCOUNT"),
    os.getenv("FUBON_PASSWORD"),
    os.getenv("FUBON_CERT_PATH"),
    os.getenv("FUBON_CERT_PASSWORD"),
)
print("2. 登入成功！正在初始化即時行情模組...")
sdk.init_realtime()

rest_stock = sdk.marketdata.rest_client.stock
ws_stock   = sdk.marketdata.websocket_client.stock

last_sent          = {}
last_rest_call     = {}
last_price_push    = {}
last_global_call   = 0
subscribed_symbols = set()
lock               = threading.Lock()

yesterday_cache    = {}
yday_refresh_time  = {}
error_count        = {}
score_board        = {}

# V6.4 狀態
score_history      = {}   # {symbol: deque([score, ...])}
positions          = {}   # {symbol: {"entry": price, "stop": price}}
entry_plan         = {}   # {symbol: {"base": price, "step": 0}}  分批進場計畫
trade_log          = []   # [{"symbol": ..., "pnl": ...}]
_prev_kd           = {}   # {symbol: (prev_k, prev_d)}，用於偵測真實交叉事件

# ===============================
# 診斷
# ===============================
def diag_quote_format():
    symbol = MY_HOLDINGS[0] if MY_HOLDINGS else "2330"
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"[DIAG] ticker / quote 欄位診斷（{symbol}）")
    print(sep)
    try:
        ticker = rest_stock.intraday.ticker(symbol=symbol)
        print(f"\n  ticker 型態: {type(ticker).__name__}")
        items = ticker.items() if isinstance(ticker, dict) else [
            (a, getattr(ticker, a, None)) for a in dir(ticker) if not a.startswith("_")
        ]
        for k, val in items:
            print(f"    ticker.{k} = {val}")
    except Exception as e:
        print(f"  ticker 失敗: {e}")
    try:
        quote = rest_stock.intraday.quote(symbol=symbol)
        print(f"\n  quote 型態: {type(quote).__name__}")
        items = quote.items() if isinstance(quote, dict) else [
            (a, getattr(quote, a, None)) for a in dir(quote) if not a.startswith("_")
        ]
        for k, val in items:
            if k in ("bids", "asks") and isinstance(val, list):
                print(f"    quote.{k} = 共{len(val)}筆，第一筆: {val[0] if val else '空'}")
            else:
                print(f"    quote.{k} = {val}")
    except Exception as e:
        print(f"  quote 失敗: {e}")
    print(f"{sep}\n")

if DIAG_MODE:
    diag_quote_format()

# ===============================
# 解析 candle list（穩健版，相容 SDK object）
# ===============================
def extract_candle_list(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ["data", "candles", "items", "result", "body", "ohlc", "bars"]:
            val = res.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                inner = extract_candle_list(val)
                if inner:
                    return inner
        for val in res.values():
            if isinstance(val, list) and val:
                return val
    for attr in ["data", "candles", "items", "result", "body", "ohlc", "bars"]:
        val = getattr(res, attr, None)
        if isinstance(val, list):
            return val
    return []

def normalize_candle(c):
    if c is None:
        return None
    date_val = (
        v(c, "date") or v(c, "datetime") or
        v(c, "time") or v(c, "ts") or v(c, "timestamp")
    )
    dt      = parse_date_like(date_val)
    high_   = safe_float(v(c, "high"))
    low_    = safe_float(v(c, "low"))
    close_  = safe_float(v(c, "close"))
    volume_ = safe_float(v(c, "volume")) or 0

    if high_ is None or low_ is None or close_ is None:
        return None

    return {
        "date_obj": dt,
        "date_str": dt.strftime("%Y-%m-%d") if dt else None,
        "time_key": dt.strftime("%Y-%m-%d %H:%M") if dt else None,
        "open":   safe_float(v(c, "open")),
        "high":   high_,
        "low":    low_,
        "close":  close_,
        "volume": volume_,
    }

# ===============================
# 抓 1 分 K
# ===============================
def fetch_candles(symbol, count=100):
    try:
        try:
            res = rest_stock.intraday.candles(symbol=symbol, timeframe="1", limit=count)
        except TypeError:
            res = rest_stock.intraday.candles(symbol=symbol, timeframe="1", count=count)

        raw_data = extract_candle_list(res)
        if not raw_data:
            return []

        candles = []
        for c in raw_data:
            nc = normalize_candle(c)
            if nc:
                candles.append(nc)

        candles.sort(key=lambda x: (
            x["date_obj"] is None,
            x["date_obj"] or datetime.min.replace(tzinfo=TW_TZ)
        ))
        return candles
    except Exception as e:
        print(f"[ERROR] fetch_candles {symbol}: {e}")
        return []

# ===============================
# 即時成交價
# ===============================
def get_current_price(*sources):
    # 優先抓真實成交欄位；ticker 有時會給到收盤/參考價，所以 quote 也要一起看。
    for key in ["last", "last_price", "lastPrice", "trade_price", "tradePrice",
                "current_price", "currentPrice", "price", "match_price", "matchPrice"]:
        for source in sources:
            val = safe_float(v(source, key))
            if val and val > 0:
                return val
    # close 僅在盤中當備援，盤後不用（盤後 close 可能是昨日值）
    if is_market_hours():
        for key in ["close"]:
            for source in sources:
                val = safe_float(v(source, key))
                if val and val > 0:
                    return val
    return None

def get_today_close(symbol):
    """
    盤後專用：從 historical.candles 抓今日收盤價。
    盤中不需要用這個，用 get_current_price 即可。
    """
    try:
        today = today_tw_str()
        for fn in [
            lambda: rest_stock.historical.candles(symbol=symbol, limit=5),
            lambda: rest_stock.historical.candles(symbol=symbol, count=5),
        ]:
            try:
                res = fn()
                raw = extract_candle_list(res)
                if not raw:
                    continue
                for c in reversed(raw):
                    nc = normalize_candle(c)
                    if nc and nc["date_str"] == today:
                        return nc["close"]
            except Exception:
                continue
    except Exception as e:
        print(f"[ERROR] get_today_close {symbol}: {e}")
    return None

# ===============================
# KD（避免重複灌同分鐘假K）
# ===============================
def get_kd_1min(symbol, current_price=None, candles=None):
    if candles is None:
        candles = fetch_candles(symbol, count=100)

    if len(candles) < 9:
        print(f"[KD] {symbol} K棒不足 ({len(candles)} 根)")
        return None, None

    if current_price is not None:
        now_min  = now_tw().strftime("%Y-%m-%d %H:%M")
        last_min = candles[-1]["time_key"]
        if last_min != now_min:
            candles.append({
                "high":     current_price,
                "low":      current_price,
                "close":    current_price,
                "date_obj": now_tw(),
                "date_str": today_tw_str(),
                "time_key": now_min,
                "volume":   0,
            })

    rsv_list = []
    for i in range(len(candles)):
        if i < 8:
            rsv_list.append(50.0)
            continue
        window = candles[i - 8:i + 1]
        h9     = max(c["high"]  for c in window)
        l9     = min(c["low"]   for c in window)
        curr_c = candles[i]["close"]
        rsv    = (curr_c - l9) / (h9 - l9) * 100 if h9 != l9 else 50.0
        rsv_list.append(rsv)

    k_v, d_v = 50.0, 50.0
    for rsv in rsv_list:
        k_v = (1 / 3) * rsv + (2 / 3) * k_v
        d_v = (1 / 3) * k_v + (2 / 3) * d_v

    return round(k_v, 2), round(d_v, 2)

# ===============================
# VWAP（用 1 分 K 自算）
# ===============================
def get_vwap_1min(symbol, current_price=None, candles=None):
    if candles is None:
        candles = fetch_candles(symbol, count=30)
    if not candles:
        return None

    if current_price is not None:
        now_min = now_tw().strftime("%Y-%m-%d %H:%M")
        if candles[-1]["time_key"] != now_min:
            candles.append({
                "high":     current_price,
                "low":      current_price,
                "close":    current_price,
                "date_obj": now_tw(),
                "date_str": today_tw_str(),
                "time_key": now_min,
                "volume":   0,
            })

    total_pv = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in candles)
    total_v  = sum(c["volume"] for c in candles)

    if total_v <= 0:
        closes = [c["close"] for c in candles[-5:]]
        return round(sum(closes) / len(closes), 4) if closes else None

    return round(total_pv / total_v, 4)

# ===============================
# 量能評估
# ===============================
def get_volume_info(symbol, candles=None):
    if candles is None:
        candles = fetch_candles(symbol, count=30)
    if len(candles) < 10:
        return {"volume_expand": False, "volume_ratio": None}

    vols     = [c["volume"] for c in candles]
    last_vol = vols[-1]
    avg_vol  = sum(vols[-10:-1]) / 9 if len(vols) >= 10 else None

    if not avg_vol or avg_vol <= 0:
        return {"volume_expand": False, "volume_ratio": None}

    ratio = last_vol / avg_vol
    return {"volume_expand": ratio >= 1.8, "volume_ratio": round(ratio, 2)}

# ===============================
# 昨日高低點（跨日快取 + refresh cooldown）
# ===============================
def get_yesterday_levels(symbol, force_refresh=False):
    try:
        now_ts = time.time()
        cache  = yesterday_cache.get(symbol)

        if cache and not force_refresh:
            if cache.get("cached_date") == today_tw_str():
                return cache

        if force_refresh:
            if now_ts - yday_refresh_time.get(symbol, 0) < YDAY_REFRESH_COOLDOWN:
                return cache if cache else None

        raw = []
        for fn in [
            lambda: rest_stock.historical.candles(symbol=symbol, limit=10),
            lambda: rest_stock.historical.candles(symbol=symbol, count=10),
            lambda: rest_stock.historical.candles(symbol=symbol),
        ]:
            try:
                res = fn()
                raw = extract_candle_list(res)
                if raw:
                    break
            except Exception:
                continue

        if not raw:
            return cache if cache else None

        candles = [nc for c in raw for nc in [normalize_candle(c)] if nc]
        if not candles:
            return cache if cache else None

        unique_by_date = {}
        for c in candles:
            if c["date_str"]:
                unique_by_date[c["date_str"]] = c
        candles = sorted(
            unique_by_date.values(),
            key=lambda x: (x["date_obj"] is None, x["date_obj"] or datetime.min.replace(tzinfo=TW_TZ))
        )

        today_str = today_tw_str()
        past_days = [c for c in candles if c["date_str"] and c["date_str"] < today_str]
        y_data    = past_days[-1] if past_days else (candles[-2] if len(candles) >= 2 else candles[-1])

        result = {
            "y_high":      y_data["high"],
            "y_low":       y_data["low"],
            "y_close":     y_data["close"],
            "y_open":      y_data["open"],
            "y_date":      y_data["date_str"],
            "cached_date": today_tw_str(),
        }

        yesterday_cache[symbol]   = result
        yday_refresh_time[symbol] = now_ts

        print(
            f"[YDAY] {symbol} 使用日期={result['y_date']} "
            f"open={result['y_open']} high={result['y_high']} "
            f"low={result['y_low']} close={result['y_close']}"
        )
        return result

    except Exception as e:
        print(f"[ERROR] get_yesterday_levels {symbol}: {e}")
        return yesterday_cache.get(symbol)

# ===============================
# 價格合理性驗證
# ===============================
def validate_market_data(symbol, curr_p, y_data):
    if curr_p is None:
        return False, "current_price_missing"
    if not y_data:
        return True, "yesterday_missing_but_allowed"
    y_close = safe_float(y_data.get("y_close"))
    if y_close is None or y_close <= 0:
        return True, "yesterday_close_invalid_but_allowed"
    gap_ratio = abs(curr_p - y_close) / y_close
    if gap_ratio > 0.30:  # 30%，相容漲跌停 + 隔週差距
        return False, f"price_mismatch curr={curr_p} y_close={y_close} ratio={gap_ratio:.2%}"
    return True, "ok"

# ===============================
# V6.3 評分系統
# ===============================
def update_score_history(symbol, score):
    if symbol not in score_history:
        score_history[symbol] = deque(maxlen=MAX_HISTORY)
    score_history[symbol].append(score)

def momentum_score(symbol):
    """評分動能：最新分數 - 最舊分數，正代表持續走強"""
    h = score_history.get(symbol)
    if not h or len(h) < 2:
        return 0
    return (h[-1] - h[0]) * 1.5

def is_fake_break(price, y_data, k_v, d_v):
    """假突破：突破昨高但 KD 死叉，可能是誘多"""
    if not all([price, y_data, k_v, d_v]):
        return False
    y_high = safe_float(y_data.get("y_high"))
    if y_high and price > y_high and k_v < d_v:
        return True
    return False

def market_type(price, vwap):
    """盤型判斷：趨勢盤 or 盤整盤"""
    if not price or not vwap:
        return "UNK"
    return "TREND" if abs(price - vwap) / vwap > 0.01 else "RANGE"

def compute_v6_score(symbol, data):
    # 修正問題 A：momentum 含本次分數（先算 base，塞入歷史，再加 momentum）
    # 修正問題 B：評分用 score_price，語義獨立於 current_price
    price     = safe_float(data.get("score_price") or data.get("_runtime_price"))
    y_high    = safe_float(data.get("y_high"))
    y_low     = safe_float(data.get("y_low"))
    y_close   = safe_float(data.get("y_close"))
    k_v       = safe_float(data.get("k_1min"))
    d_v       = safe_float(data.get("d_1min"))
    vwap      = safe_float(data.get("vwap_1min"))
    vol_ratio = safe_float(data.get("volume_ratio"))

    base_score = 0
    tags = []

    # KD：區分「剛形成交叉」（動態事件）與「維持方向」（靜態狀態）
    kd_signal = data.get("kd_signal", "none")
    if k_v is not None and d_v is not None:
        if kd_signal == "gold_cross":
            base_score += WEIGHTS["kd_gold_cross"]      # +15 剛形成金叉
            tags.append("KD金叉")
        elif kd_signal == "death_cross":
            base_score += WEIGHTS["kd_death_cross"]     # -15 剛形成死叉
            tags.append("KD死叉")
        elif k_v > d_v:
            base_score += WEIGHTS["kd_gold_cross"] // 3  # +5 維持多方
            tags.append("KD偏多")
        else:
            base_score += WEIGHTS["kd_death_cross"] // 3  # -5 維持空方
            tags.append("KD偏空")

    # VWAP
    if price and vwap:
        if price > vwap:
            base_score += WEIGHTS["above_vwap"]
            tags.append("站上VWAP")
        else:
            base_score += WEIGHTS["below_vwap"]
            tags.append("跌破VWAP")

    # 昨高昨低
    if price and y_high and price > y_high:
        base_score += WEIGHTS["break_y_high"]
        tags.append("突破昨高")
    if price and y_low:
        if price < y_low:
            base_score += WEIGHTS["break_y_low"]
            tags.append("跌破昨低")
        elif y_close and (price - y_low) / y_close < 0.02:
            base_score += WEIGHTS["rebound_near_low"]
            tags.append("低檔反彈")

    # 量能
    if vol_ratio and vol_ratio >= 1.8:
        base_score += WEIGHTS["volume_expand"]
        tags.append(f"量增{vol_ratio}倍")

    # 過熱
    if price and y_close and (price - y_close) / y_close > 0.07:
        base_score += WEIGHTS["overheat"]
        tags.append("短線過熱")

    # 假突破
    if is_fake_break(price, {"y_high": y_high}, k_v, d_v):
        base_score += WEIGHTS["fake_break"]
        tags.append("疑似假突破")

    # 主力判斷（pressure_ratio = ask_strength / bid_strength）
    pressure = safe_float((data.get("structure") or {}).get("pressure_ratio"))
    if pressure is not None:
        if pressure <= 0.8:
            base_score += 10
            tags.append("買盤主導")
        elif pressure >= 1.2:
            base_score -= 8
            tags.append("賣盤主導")

    # 原始系統 decision 輔助
    decision  = str(data.get("decision", "")).lower()
    raw_score = safe_float(data.get("score"))
    if "long_possible" in decision or "avoid_short" in decision:
        base_score += WEIGHTS["trend_up"]
        tags.append("系統偏多")
    elif "short_possible" in decision or "avoid_long" in decision:
        base_score += WEIGHTS["trend_down"]
        tags.append("系統偏弱")

    if raw_score is not None:
        if raw_score >= 80:
            base_score += 10
            tags.append("高原始分")
        elif raw_score <= 40:
            base_score -= 5

    # 問題 A 修正：先把 base_score 塞入歷史，再算含本次的 momentum
    update_score_history(symbol, base_score)
    mom = momentum_score(symbol)
    score = base_score + mom
    if mom > 5:
        tags.append("動能走強")
    elif mom < -5:
        tags.append("動能轉弱")

    # 系統否決：亮燈鎖死或無成交量時 V6 強制壓至負值，避免其他指標救回
    long_reason = str((data.get("entry_signal") or {}).get("long_reason") or "")
    if "no_trade" in str(data.get("decision", "")) or "亮燈" in long_reason or "7% 死亡" in long_reason:
        score = min(score, -10)
        if "系統禁令" not in tags:
            tags.append("系統禁令")

    return round(score, 2), tags

# ===============================
# V6.4 進出場（模擬追蹤，不真實下單）
# ===============================
def can_enter():
    """連續虧損超過門檻就停止進場"""
    losses = 0
    for t in reversed(trade_log):
        if t["pnl"] < 0:
            losses += 1
        else:
            break
    return losses < MAX_LOSS_STREAK

def try_entry(symbol, price, v6_score):
    """
    V6.4 分批進場：
    - 分數 > ENTRY_THRESHOLD 且動能 >= MOMENTUM_MIN → 建立計畫
    - 計畫建立後等待 price <= vwap 時進場（拉回確認）
    """
    if symbol in positions:
        return
    if not can_enter():
        return

    mom = momentum_score(symbol)

    if v6_score > ENTRY_THRESHOLD and mom >= MOMENTUM_MIN:
        if symbol not in entry_plan:
            entry_plan[symbol] = {"base": price, "step": 0, "created": time.time()}
            print(f"🧭 [模擬計畫] {symbol} V6:{v6_score} 動能:{mom:.1f}")

def execute_entry(symbol, price, vwap, v6_score=None):
    """
    執行分批進場計畫：
    step 0 → 等待 price <= vwap*1.002（拉回確認）
    step 1 → price 再次站上 vwap → 進場第一批
    step 2 → 獲利 1% → 加碼第二批（記錄，不真實加碼）
    step 3 → 獲利 2% → 計畫完成
    """
    if symbol not in entry_plan:
        return
    if symbol in positions and entry_plan.get(symbol, {}).get("step", 0) < 2:
        return

    plan = entry_plan[symbol]

    # 過期判斷：超過 10 分鐘沒完成 → 放棄計畫
    if time.time() - plan.get("created", 0) > 600:
        print(f"⏱️ [計畫逾時] {symbol} 超過 10 分鐘未完成，放棄")
        entry_plan.pop(symbol, None)
        return

    if plan["step"] == 0:
        if vwap and price <= vwap * 1.002:
            plan["step"] = 1

    elif plan["step"] == 1:
        if symbol not in positions and vwap and price > vwap:
            mom = momentum_score(symbol)
            if v6_score is None or v6_score <= ENTRY_THRESHOLD or mom < MOMENTUM_MIN:
                print(
                    f"🛑 [取消進場] {symbol} "
                    f"V6:{v6_score} 動能:{mom:.1f} 未達門檻"
                )
                entry_plan.pop(symbol, None)
                return
            positions[symbol] = {
                "entry": price,
                "stop":  price * STOP_LOSS_RATIO,
                "size":  1,
            }
            plan["step"] = 2
            print(f"🚀 [模擬進場] {symbol} 第一批 價:{price}")

    elif plan["step"] == 2:
        pos = positions.get(symbol)
        if pos and price > pos["entry"] * 1.01:
            # 加碼：更新加權平均成本
            old_entry = pos["entry"]
            old_size  = pos.get("size", 1)
            new_size  = old_size + 1
            new_entry = (old_entry * old_size + price) / new_size
            pos["entry"] = round(new_entry, 4)
            pos["size"]  = new_size
            # 停損也隨平均成本更新
            pos["stop"]  = max(pos["stop"], new_entry * STOP_LOSS_RATIO)
            plan["step"] = 3
            print(f"🚀 [模擬加碼] {symbol} 第二批 價:{price} 新均成本:{new_entry:.2f}")

    elif plan["step"] == 3:
        pos = positions.get(symbol)
        if pos and price > pos["entry"] * 1.02:
            entry_plan.pop(symbol, None)

def update_trailing_stop(symbol, price):
    """
    V6.4 分段移動停損：
    獲利 2% → 啟動第一段（緊縮到 price * 0.995）
    獲利 3% → 啟動第二段（更緊，price * 0.997）
    """
    if symbol not in positions:
        return
    pos = positions[symbol]
    gain = (price - pos["entry"]) / pos["entry"]

    if gain > TRAIL_TRIGGER_2:
        pos["stop"] = max(pos["stop"], price * TRAIL_RATIO_2)
    elif gain > TRAIL_TRIGGER_1:
        pos["stop"] = max(pos["stop"], price * TRAIL_RATIO_1)

def try_exit(symbol, price, v6_score):
    if symbol not in positions:
        return
    pos = positions[symbol]
    if price < pos["stop"] or v6_score < 0:
        pnl = price - pos["entry"]
        trade_log.append({"symbol": symbol, "pnl": pnl, "time": today_tw_str()})
        positions.pop(symbol)
        entry_plan.pop(symbol, None)
        print(f"📉 [模擬出場] {symbol} 價:{price} PnL:{pnl:+.2f}")


# ===============================
# 建立分析資料
# ===============================
def build_payload(symbol, current_price_hint=None):
    global _rate_limited_until

    wait = _rate_limited_until - time.time()
    if wait > 0:
        raise ValueError(f"{symbol} 暫緩呼叫 API（429 退避中，剩 {wait:.0f}s）")

    try:
        ticker = rest_stock.intraday.ticker(symbol=symbol)
        quote  = rest_stock.intraday.quote(symbol=symbol)
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            _rate_limited_until = time.time() + 60
            raise ValueError(f"{symbol} Fugle 429，退避 60s: {e}")
        raise

    curr_p = safe_float(current_price_hint)
    if not curr_p or curr_p <= 0:
        curr_p = get_current_price(ticker, quote)

    # 共用 candles（一次抓取，供 KD / VWAP / 量能 / 價格補正共用）
    try:
        shared_candles = fetch_candles(symbol, count=100)
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            _rate_limited_until = time.time() + 60
            raise ValueError(f"{symbol} Fugle 429，退避 60s: {e}")
        shared_candles = []

    # 補正 1：ticker 無法取得時，從最新 1 分 K 收盤價取得（最可靠的備援）
    if curr_p is None and shared_candles:
        _c = safe_float(shared_candles[-1].get("close"))
        if _c and _c > 0:
            curr_p = _c
            print(f"[補正] {symbol} 從最新1分K取得即時價: {curr_p}")

    # 補正 2：盤後用今日歷史收盤
    if curr_p is None and not is_market_hours():
        curr_p = get_today_close(symbol)
        if curr_p:
            print(f"[補正] {symbol} 使用今日歷史收盤價 {curr_p}")

    y_data = get_yesterday_levels(symbol)

    # 補正 3：最後手段，用最近交易日收盤價
    if curr_p is None and y_data:
        curr_p = y_data.get("y_close")
        if curr_p:
            print(f"[補正] {symbol} 使用最近交易日 {y_data.get('y_date')} 收盤價 {curr_p}")

    ok, reason = validate_market_data(symbol, curr_p, y_data)

    if not ok:
        print(f"[WARN] {symbol} 資料驗證失敗，重抓昨日資料：{reason}")
        y_data = get_yesterday_levels(symbol, force_refresh=True)
        ok, reason = validate_market_data(symbol, curr_p, y_data)

    if not ok:
        raise ValueError(f"{symbol} 資料異常，停止上傳：{reason}")

    # 共用 candles 傳入，避免重複 API 呼叫
    k_v, d_v  = get_kd_1min(symbol, current_price=curr_p, candles=shared_candles)
    vol_info  = get_volume_info(symbol, candles=shared_candles)

    # 盤後 VWAP 不可靠（分K資料可能是殘留或空的），直接用 None
    # 盤中才計算真實 VWAP
    if is_market_hours():
        vwap_1m = get_vwap_1min(symbol, current_price=curr_p, candles=shared_candles)
    else:
        vwap_1m = None

    data = extract_analysis_data(ticker, quote)
    data["symbol"] = symbol

    # 對外現價一律使用已驗證 curr_p，避免 extract_analysis_data 裡的 close/舊欄位蓋過即時價。
    if curr_p is not None:
        set_payload_price(data, curr_p)

    # 評分系統用獨立欄位，不污染原始分析
    data["_runtime_price"] = curr_p

    if y_data:
        data.update({k: vv for k, vv in y_data.items() if k != "cached_date"})

    kd_sig = "none"
    if k_v is not None and d_v is not None:
        with lock:
            prev = _prev_kd.get(symbol)
            if prev is not None:
                pk, pd = prev
                if pk < pd and k_v >= d_v:
                    kd_sig = "gold_cross"
                elif pk > pd and k_v <= d_v:
                    kd_sig = "death_cross"
            _prev_kd[symbol] = (k_v, d_v)

    data.update({
        "k_1min":        k_v,
        "d_1min":        d_v,
        "kd_signal":     kd_sig,
        "vwap_1min":     vwap_1m,
        "volume_ratio":  vol_info.get("volume_ratio"),
        "volume_expand": vol_info.get("volume_expand"),
    })
    sync_vwap_risk(data, curr_p, vwap_1m)

    # score_price 必須在 compute_v6_score 之前設定，否則函數內取到 None
    data["score_price"] = curr_p

    # V6.3 評分
    v6_score, v6_tags = compute_v6_score(symbol, data)
    data["v6_score"] = v6_score
    data["v6_tags"]  = " | ".join(v6_tags[:6]) if v6_tags else ""

    # V6.4 模擬進出場
    if curr_p:
        vwap_for_entry = vwap_1m
        update_trailing_stop(symbol, curr_p)
        try_exit(symbol, curr_p, v6_score)
        try_entry(symbol, curr_p, v6_score)
        execute_entry(symbol, curr_p, vwap_for_entry, v6_score)

    print(
        f"[BUILD] {symbol} curr={curr_p} "
        f"y_close={data.get('y_close')} "
        f"K={k_v} D={d_v} kd={kd_sig} "
        f"VWAP={vwap_1m} V6={v6_score} "
        f"tags={data['v6_tags']}"
    )
    return data

# ===============================
# 雲端傳輸
# ===============================
def send_to_cloud(data):
    headers = {"Authorization": f"Bearer {MY_SECRET_TOKEN}"}
    try:
        response = requests.post(API_URL, json=data, headers=headers, timeout=5)
        if response.status_code == 200:
            print(
                f"✅ {data['symbol']} "
                f"K:{data.get('k_1min')} D:{data.get('d_1min')} {data.get('kd_signal')} "
                f"V6:{data.get('v6_score')}"
            )
        else:
            print(f"❌ [雲端] {response.status_code} - {response.text[:150]}")
    except Exception as e:
        print(f"❌ [雲端] 連線失敗: {e}")

def send_price_only_update(symbol, price):
    now_ts = time.time()
    if now_ts - last_price_push.get(symbol, 0) < PRICE_ONLY_COOLDOWN_PER_SYMBOL:
        return

    with lock:
        base = score_board.get(symbol)
        if not base:
            return

        data = dict(base)
        if isinstance(base.get("price"), dict):
            data["price"] = dict(base["price"])

        old_price = safe_float(data.get("score_price") or data.get("current_price") or data.get("last"))
        new_price = safe_float(price)
        if old_price == new_price:
            return
        if not set_payload_price(data, new_price, source="websocket_trade"):
            return

        # 價格快速更新不重算 KD，重置為 none 避免 GPT 誤讀過期交叉事件
        data["kd_signal"] = "none"

        score_board[symbol] = data

    send_to_cloud(data)
    last_price_push[symbol] = now_ts
    print(f"⚡ [價格更新] {symbol} {old_price} → {new_price}")

# ===============================
# 排行榜輸出
# ===============================
def print_score_board():
    while True:
        try:
            with lock:
                # 過濾掉 skipped 的股票，v6_score 為 None 的也排除
                items = sorted(
                    [(s, d) for s, d in score_board.items()
                     if not d.get("skipped") and d.get("v6_score") is not None],
                    key=lambda x: x[1].get("v6_score", -9999),
                    reverse=True
                )

            if len(items) < 3:
                time.sleep(10)
                continue  # 資料太少，等更多股票跑完再印

            if items:
                print("\n🔥 強勢榜 TOP 5")
                for sym, d in items[:5]:
                    print(
                        f"  {sym}  V6:{d.get('v6_score')}  "
                        f"價:{d.get('_runtime_price') or d.get('current_price')}  "
                        f"KD:{d.get('k_1min')}/{d.get('d_1min')}  "
                        f"{d.get('v6_tags','')}"
                    )

                rebound = [(s, d) for s, d in items if (d.get("v6_score") or -9999) > 0]
                print("\n⚡ 反彈榜 TOP 3")
                for sym, d in rebound[:3]:
                    print(f"  {sym}  V6:{d.get('v6_score')}  {d.get('v6_tags','')}")

                abnormal = [
                    (s, d) for s, d in items
                    if d.get("volume_expand") or "突破昨高" in (d.get("v6_tags") or "")
                ]
                print("\n🚨 異常榜 TOP 3")
                for sym, d in abnormal[:3]:
                    print(
                        f"  {sym}  V6:{d.get('v6_score')}  "
                        f"量比:{d.get('volume_ratio')}  "
                        f"{d.get('v6_tags','')}"
                    )

                if positions:
                    print("\n📋 模擬持倉")
                    for sym, pos in positions.items():
                        curr = (score_board.get(sym) or {}).get("score_price") or "?"
                        gain = ((curr - pos["entry"]) / pos["entry"] * 100) if isinstance(curr, float) else 0
                        print(f"  {sym} 進場:{pos['entry']} 停損:{pos['stop']:.2f} 現價:{curr} 損益:{gain:+.1f}%")

                if entry_plan:
                    print("\n🧭 進場計畫")
                    for sym, plan in entry_plan.items():
                        curr = (score_board.get(sym) or {}).get("score_price") or "?"
                        print(f"  {sym} Step:{plan['step']} 基準:{plan['base']} 現價:{curr}")

        except Exception as e:
            print(f"[ERROR] print_score_board: {e}")

        # 盤中 10 秒刷新，盤後 60 秒刷新（資料不會變，不需要頻繁刷）
        time.sleep(10 if is_market_hours() else 60)

# ===============================
# 訊息處理
# ===============================
def handle_message(message):
    global last_global_call
    try:
        msg_data = json.loads(message)
        raw = msg_data.get("data")
        if raw is None:
            return

        item = raw[0] if isinstance(raw, list) else raw
        code = normalize_symbol(
            item.get("symbol") if isinstance(item, dict) else getattr(item, "symbol", None)
        )
        if not code:
            return

        now_ts = time.time()
        trade_price = get_current_price(item)
        if now_ts - last_rest_call.get(code, 0) < REST_CALL_COOLDOWN_PER_SYMBOL:
            send_price_only_update(code, trade_price)
            return
        if now_ts - last_global_call < GLOBAL_REST_MIN_INTERVAL:
            send_price_only_update(code, trade_price)
            return

        last_global_call     = now_ts
        last_rest_call[code] = now_ts

        data = build_payload(code, current_price_hint=trade_price)

        with lock:
            score_board[code] = data

        k_v   = data.get("k_1min")
        k_int = int(k_v) if isinstance(k_v, (int, float)) else -1
        key   = (
            f"{data.get('score')}-{data.get('decision')}-"
            f"{data.get('v6_score')}-{k_int}-"
            f"{round((data.get('_runtime_price') or data.get('current_price') or 0), 2)}"
        )

        if last_sent.get(code, {}).get("key") != key:
            send_to_cloud(data)
            with lock:
                last_sent[code] = {"key": key, "time": now_ts}

        error_count.pop(code, None)

    except Exception as e:
        print(f"[ERROR] handle_message: {e}")
        traceback.print_exc()

# ===============================
# WebSocket 連線 + 自動重連
# ===============================
def reconnect_websocket():
    try:
        print("[系統] WebSocket 斷線，正在重新連線...")
        ws_stock.connect()
        time.sleep(2)
        with lock:
            syms = list(subscribed_symbols)
        if syms:
            ws_stock.subscribe({"channel": "trades", "symbols": syms})
            print(f"[系統] 重新訂閱完成：{len(syms)} 檔")
    except Exception as e:
        print(f"[ERROR] 重新連線失敗: {e}")

ws_stock.on("connect",    lambda: print("✅ [系統] 伺服器連線成功！"))
ws_stock.on("disconnect", lambda: reconnect_websocket())
ws_stock.on("message",    handle_message)
ws_stock.connect()

# ===============================
# 背景訂閱管理
# ===============================
def update_subscriptions():
    global subscribed_symbols
    while True:
        try:
            local_symbols = set(normalize_symbol(x) for x in MY_HOLDINGS if normalize_symbol(x))

            for file_path in glob.glob("*.csv"):
                try:
                    with open(file_path, mode="r", encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            if "商品" in row and row["商品"]:
                                local_symbols.add(normalize_symbol(row["商品"]))
                except Exception:
                    pass

            # 許願池（一次性任務）
            res = requests.get(WISHLIST_URL, timeout=3)
            cloud_symbols = (
                set(normalize_symbol(x) for x in res.json().get("wishlist", []) if normalize_symbol(x))
                if res.status_code == 200 else set()
            )

            # 持久追蹤清單（watch.py 請求的標的，長期維持）
            res2 = requests.get(WATCH_LIST_URL, timeout=3)
            watch_symbols = (
                set(normalize_symbol(x) for x in res2.json().get("symbols", []) if normalize_symbol(x))
                if res2.status_code == 200 else set()
            )

            all_needed  = local_symbols | cloud_symbols | watch_symbols
            new_symbols = all_needed - subscribed_symbols

            if new_symbols:
                print(f"[訂閱] 新增: {new_symbols}")
                try:
                    ws_stock.subscribe({"channel": "trades", "symbols": list(new_symbols)})
                    with lock:
                        subscribed_symbols.update(new_symbols)
                except Exception as sub_e:
                    if "closed" in str(sub_e).lower():
                        reconnect_websocket()
                    else:
                        print(f"[ERROR] 訂閱失敗: {sub_e}")

            # 許願池 + 持久清單：主動上傳（cooldown 30s）
            for sym in cloud_symbols | watch_symbols:
                try:
                    if time.time() - last_sent.get(sym, {}).get("time", 0) < WISHLIST_UPLOAD_COOLDOWN:
                        continue
                    d_data = build_payload(sym)
                    with lock:
                        score_board[sym] = d_data
                    send_to_cloud(d_data)
                    tag = "許願池" if sym in cloud_symbols else "追蹤清單"
                    print(f"[{tag}] {sym} 上傳完成")
                    with lock:
                        last_sent[sym] = {"key": "watchlist", "time": time.time()}
                    time.sleep(1)
                except Exception as e:
                    print(f"[追蹤] {sym} 上傳失敗: {e}")

        except Exception as e:
            print(f"[ERROR] update_subscriptions: {e}")

        time.sleep(10)

# ===============================
# 啟動背景執行緒
# ===============================
threading.Thread(target=update_subscriptions, daemon=True).start()
threading.Thread(target=print_score_board, daemon=True).start()

print("🚀 V6.3 穩健合併版啟動成功！正在監聽市場...")

# ===============================
# 盤中時段判斷（函數定義在檔案頂部）
# ===============================

# ===============================
# 心跳迴圈（含連續失敗保護）
# ===============================
try:
    while True:
        current_time = time.time()

        # 盤後：只跑一輪讓所有股票有初始資料，之後休眠
        if not is_market_hours():
            # 若 score_board 裡還沒有資料，先跑一輪
            with lock:
                missing = [s for s in subscribed_symbols if s not in score_board]
            if missing:
                # 逐一補齊沒有資料的股票
                for sym in missing:
                    # 連續失敗超過 3 次就標記跳過，不再重試
                    if error_count.get(sym, 0) >= MAX_ERROR_COUNT:
                        with lock:
                            score_board[sym] = {"v6_score": None, "skipped": True}
                        continue
                    try:
                        d_data = build_payload(sym)
                        with lock:
                            score_board[sym] = d_data
                        send_to_cloud(d_data)
                        k_v = d_data.get("k_1min")
                        last_sent[sym] = {
                            "key":  f"init-{int(k_v or 0)}",
                            "time": time.time()
                        }
                        error_count.pop(sym, None)
                        time.sleep(1)
                    except Exception as e:
                        print(f"[ERROR] 盤後初始化 {sym}: {e}")
                        error_count[sym] = error_count.get(sym, 0) + 1
                        if error_count[sym] >= MAX_ERROR_COUNT:
                            print(f"[SKIP] {sym} 連續失敗 {error_count[sym]} 次，略過")
                            with lock:
                                score_board[sym] = {"v6_score": None, "skipped": True}
            else:
                time.sleep(60)
            continue

        with lock:
            targets = list(subscribed_symbols)

        for sym in targets:
            last_info = last_sent.get(sym, {"key": "", "time": 0})
            if current_time - last_info["time"] <= HEARTBEAT_INTERVAL:
                continue

            try:
                d_data = build_payload(sym)
                with lock:
                    score_board[sym] = d_data
                send_to_cloud(d_data)

                k_v = d_data.get("k_1min")
                last_sent[sym] = {
                    "key":  f"hb-{d_data.get('v6_score')}-{int(k_v or 0)}-{round((d_data.get('_runtime_price') or d_data.get('current_price') or 0), 2)}",
                    "time": time.time()
                }
                error_count.pop(sym, None)
                time.sleep(1.5)

            except Exception as e:
                print(f"[ERROR] 心跳 {sym}: {e}")
                error_count[sym] = error_count.get(sym, 0) + 1
                if error_count[sym] >= MAX_ERROR_COUNT:
                    print(f"[WARN] {sym} 連續失敗 {error_count[sym]} 次，暫停監控")
                    with lock:
                        subscribed_symbols.discard(sym)
                        score_board.pop(sym, None)
                    error_count.pop(sym, None)

        time.sleep(1)

except KeyboardInterrupt:
    print("\n[系統] 手動停止...")
    ws_stock.disconnect()
    print("[系統] 已安全關閉。")
