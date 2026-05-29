from fubon_neo.sdk import FubonSDK
import math
import os
import time
from dotenv import load_dotenv

load_dotenv()

ACCOUNT       = os.getenv("FUBON_ACCOUNT")
PASSWORD      = os.getenv("FUBON_PASSWORD")
CERT_PATH     = os.getenv("FUBON_CERT_PATH")
CERT_PASSWORD = os.getenv("FUBON_CERT_PASSWORD")
SYMBOL        = "2330"

entry_memory = {}
LONG_PRESSURE_THRESHOLD = 0.9
SHORT_PRESSURE_THRESHOLD = 1.2
ENTRY_CONFIRM_WINDOW_SECS = int(os.getenv("ENTRY_CONFIRM_WINDOW_SECS", "240"))
ENTRY_MEMORY_TTL_SECS = int(os.getenv("ENTRY_MEMORY_TTL_SECS", str(ENTRY_CONFIRM_WINDOW_SECS * 3)))
ENTRY_ZONE_PCT = float(os.getenv("ENTRY_ZONE_PCT", "0.003"))

# =============================================
# 🛠️ 核心修正：SDK 回傳 object，不是 dict
# 統一用這個函數取值，相容 dict / object / list
# =============================================
def _get(obj, *keys, default=None):
    """
    遞迴取值。支援：
      - dict: obj['key']
      - object: obj.key
      - list: 自動取第一筆再繼續
    用法: _get(quote, 'bids', 0, 'price')
    """
    cur = obj
    for key in keys:
        if cur is None:
            return default
        try:
            if isinstance(cur, list):
                cur = cur[key] if isinstance(key, int) and key < len(cur) else None
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = getattr(cur, key, None)
        except Exception:
            return default
    return cur if cur is not None else default


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_list(val):
    """把 SDK 可能回傳的 object/list 統一轉成 list of dict"""
    if val is None:
        return []
    if isinstance(val, list):
        result = []
        for item in val:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append({
                    'price': _to_float(getattr(item, 'price', None)),
                    'size':  int(getattr(item, 'size', 0) or 0),
                })
        return result
    return []


def _twse_tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _round_to_tick(price: float, direction: str) -> float:
    tick = _twse_tick_size(price)
    steps = price / tick
    if direction == "down":
        rounded = math.floor(steps) * tick
    elif direction == "up":
        rounded = math.ceil(steps) * tick
    else:
        rounded = round(steps) * tick
    return round(rounded, 2)


def _entry_zone_from_mid(mid_price: float) -> dict:
    width = max(mid_price * ENTRY_ZONE_PCT, _twse_tick_size(mid_price))
    return {
        "lower": _round_to_tick(mid_price - width, "down"),
        "upper": _round_to_tick(mid_price + width, "up"),
    }


def _prune_entry_memory(now: float) -> None:
    expired = []
    for sym, state in entry_memory.items():
        times = [state.get("short_time"), state.get("long_time")]
        latest = max((t for t in times if t is not None), default=None)
        if latest is None or now - latest > ENTRY_MEMORY_TTL_SECS:
            expired.append(sym)
    for sym in expired:
        entry_memory.pop(sym, None)


def _extract_stock_name(*sources):
    for key in (
        "name",
        "stock_name",
        "stockName",
        "symbol_name",
        "symbolName",
        "shortName",
        "companyName",
        "description",
    ):
        for source in sources:
            val = _get(source, key)
            if val:
                return str(val)
    return None

# =============================================
# 主要分析函數
# =============================================

def extract_analysis_data(ticker, quote):
    now = time.time()
    _prune_entry_memory(now)

    # --- 取 symbol ---
    symbol = (
        _get(ticker, 'symbol') or
        _get(quote,  'symbol') or
        "unknown"
    )
    name = _extract_stock_name(ticker, quote)

    # --- 從 quote 取五檔 ---
    bids_raw = _get(quote, 'bids') or _get(quote, 'bidPrices') or []
    asks_raw = _get(quote, 'asks') or _get(quote, 'askPrices') or []
    bids = _to_list(bids_raw)
    asks = _to_list(asks_raw)

    best_bid = _to_float(_get(bids, 0, 'price')) if bids else None
    best_ask = _to_float(_get(asks, 0, 'price')) if asks else None

    # --- 價格欄位（ticker 或 quote 二擇一）---
    last_price = (
        _to_float(_get(ticker, 'lastPrice')) or
        _to_float(_get(ticker, 'last_price')) or
        _to_float(_get(ticker, 'last')) or
        _to_float(_get(ticker, 'close')) or
        _to_float(_get(quote,  'lastPrice')) or
        _to_float(_get(quote,  'last'))
    )
    high_price = (
        _to_float(_get(ticker, 'highPrice')) or
        _to_float(_get(ticker, 'high')) or
        _to_float(_get(quote,  'highPrice'))
    )
    low_price = (
        _to_float(_get(ticker, 'lowPrice')) or
        _to_float(_get(ticker, 'low')) or
        _to_float(_get(quote,  'lowPrice'))
    )
    avg_price = (
        _to_float(_get(ticker, 'avgPrice')) or
        _to_float(_get(ticker, 'average')) or
        _to_float(_get(quote,  'avgPrice')) or
        _to_float(_get(quote,  'average'))
    )
    change_percent = (
        _to_float(_get(ticker, 'changePercent')) or
        _to_float(_get(quote,  'changePercent')) or
        0.0
    )

    # --- 成交量 ---
    trade_volume = (
        _to_float(_get(ticker, 'total', 'tradeVolume')) or
        _to_float(_get(quote,  'total', 'tradeVolume')) or
        _to_float(_get(ticker, 'tradeVolume')) or
        _to_float(_get(quote,  'tradeVolume')) or
        0
    )
    has_real_trade = (trade_volume or 0) > 0

    # =============================
    # 趨勢判斷
    # =============================
    trend = "neutral"
    if last_price and avg_price:
        diff_pct = (last_price - avg_price) / avg_price * 100
        if diff_pct > 0.3:
            trend = "up"
        elif diff_pct < -0.3:
            trend = "down"

    # =============================
    # 基礎數據
    # =============================
    mid_price = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None
    distance_from_mid = round(last_price - mid_price, 2) if (last_price and mid_price) else None
    distance_from_mid_pct = round((last_price - mid_price) / last_price * 100, 3) if (last_price and mid_price) else None
    vwap_distance = (
        round(((last_price - avg_price) / avg_price) * 100, 2)
        if (last_price and avg_price) else None
    )

    # =============================
    # 委買委賣力道
    # =============================
    bid_strength = sum(int(b.get('size', 0) or 0) for b in bids)
    ask_strength = sum(int(a.get('size', 0) or 0) for a in asks)

    dominance = (
        "buy"  if bid_strength > ask_strength else
        "sell" if ask_strength > bid_strength else
        "neutral"
    )
    pressure_ratio = round(ask_strength / bid_strength, 2) if bid_strength else None

    # =============================
    # 假突破
    # =============================
    NEAR_PCT     = 0.005  # 0.5%，相容不同價位股票
    is_near_high = (
        high_price is not None and last_price is not None and last_price > 0
        and 0 <= (high_price - last_price) / last_price <= NEAR_PCT
    )
    is_near_low = (
        low_price is not None and last_price is not None and last_price > 0
        and 0 <= (last_price - low_price) / last_price <= NEAR_PCT
    )

    trap_signal = None
    if is_near_high and dominance == "sell" and change_percent < 0.5:
        trap_signal = "bull_trap"
    elif is_near_low and dominance == "buy" and change_percent > -0.5:
        trap_signal = "bear_trap"

    # =============================
    # 反轉偵測
    # =============================
    reversal_signal = None
    if (trend == "down" and change_percent > 0.8 and
            last_price and mid_price and last_price > mid_price and
            dominance == "buy" and distance_from_mid_pct and distance_from_mid_pct > 0.1):
        reversal_signal = "bullish_reversal"
    elif (trend == "up" and change_percent < -0.8 and
            last_price and mid_price and last_price < mid_price and
            dominance == "sell" and distance_from_mid_pct and distance_from_mid_pct < -0.1):
        reversal_signal = "bearish_reversal"

    # =============================
    # 絕對禁令濾網
    # =============================
    is_limit_locked  = not bids or not asks
    is_over_7_percent = change_percent >= 7.0
    is_fomo_extreme   = vwap_distance is not None and vwap_distance >= 2.0

    # =============================
    # 決策
    # =============================
    decision = "observe"
    if not has_real_trade or is_limit_locked:
        decision = "no_trade"
    elif is_over_7_percent:
        decision = "avoid_long"
    elif is_fomo_extreme and dominance == "buy":
        decision = "avoid_long"
    elif trap_signal == "bull_trap":
        decision = "avoid_long"
    elif trap_signal == "bear_trap":
        decision = "avoid_short"
    elif reversal_signal == "bullish_reversal":
        decision = "long_possible"
    elif reversal_signal == "bearish_reversal":
        decision = "short_possible"
    elif dominance == "buy" and pressure_ratio and pressure_ratio < LONG_PRESSURE_THRESHOLD:
        decision = "long_possible" if trend == "up" else "observe"
    elif dominance == "sell" and pressure_ratio and pressure_ratio > SHORT_PRESSURE_THRESHOLD:
        decision = "short_possible" if trend == "down" else "observe"

    # =============================
    # 分數
    # =============================
    score = 50
    if change_percent > 0: score += 15
    elif change_percent < 0: score -= 15
    if trend == "up": score += 10
    elif trend == "down": score -= 10
    if dominance == "buy": score += 5
    elif dominance == "sell": score -= 5
    if pressure_ratio:
        if pressure_ratio > 1.5: score -= 10
        elif pressure_ratio < 0.7: score += 10
    if trap_signal == "bull_trap": score -= 15
    if trap_signal == "bear_trap": score += 15
    score = max(0, min(100, int(score)))

    # =============================
    # 進場訊號
    # =============================
    short_raw = (
        pressure_ratio and pressure_ratio > SHORT_PRESSURE_THRESHOLD and
        last_price and mid_price and last_price < mid_price and
        trend == "down" and reversal_signal is None and not is_limit_locked
    )
    long_raw = (
        pressure_ratio and pressure_ratio < LONG_PRESSURE_THRESHOLD and
        last_price and mid_price and last_price > mid_price and
        trend == "up" and reversal_signal is None and
        not is_limit_locked and not is_over_7_percent and not is_fomo_extreme
    )

    if symbol not in entry_memory:
        entry_memory[symbol] = {"short": 0, "long": 0, "short_time": None, "long_time": None}

    def update_trigger(side):
        time_key = f"{side}_time"
        if entry_memory[symbol][time_key] is not None and (now - entry_memory[symbol][time_key]) <= ENTRY_CONFIRM_WINDOW_SECS:
            entry_memory[symbol][side] += 1
        else:
            entry_memory[symbol][side] = 1
        entry_memory[symbol][time_key] = now

    if short_raw: update_trigger("short")
    else: entry_memory[symbol].update({"short": 0, "short_time": None})

    if long_raw: update_trigger("long")
    else: entry_memory[symbol].update({"long": 0, "long_time": None})

    short_trigger = entry_memory[symbol]["short"] >= 2
    long_trigger  = entry_memory[symbol]["long"]  >= 2

    short_reason = "賣壓顯著且跌破中值，順勢偏空" if short_trigger else None
    long_reason  = "買盤強勁且站上中值，順勢偏多" if long_trigger  else None

    if is_limit_locked:
        short_reason = long_reason = "⚠️ 五檔清空 (亮燈鎖死)，禁止任何建倉"
        short_trigger = long_trigger = False
    elif is_over_7_percent:
        long_reason  = "⚠️ 觸發 7% 死亡紅線禁令，鎖死做多"
        long_trigger = False
    elif is_fomo_extreme:
        long_reason  = f"⚠️ 乖離 VWAP 達 {vwap_distance}%，FOMO 禁令生效"
        long_trigger = False

    entry_signal = {
        "short_trigger": short_trigger,
        "short_reason":  short_reason,
        "long_trigger":  long_trigger,
        "long_reason":   long_reason,
    }

    # =============================
    # 盤型 / 出場 / 風控
    # =============================
    market_type = "range"
    if trap_signal:                 market_type = "trap"
    elif reversal_signal:           market_type = "reversal"
    elif trend in ["up","down"] and abs(change_percent) > 1: market_type = "trend"

    # grade 區分方向：A_long/A_short 避免 GPT 誤把強空訊號解讀為強多
    if score >= 75:    signal_grade = "A_long"
    elif score <= 25:  signal_grade = "A_short"
    elif score >= 60:  signal_grade = "B_long"
    elif score <= 40:  signal_grade = "B_short"
    else:              signal_grade = "C"

    entry_zone = {"lower": None, "upper": None}
    if mid_price and last_price:
        if decision in ["long_possible", "short_possible"]:
            entry_zone = _entry_zone_from_mid(mid_price)

    invalid_price = None
    dynamic_stop  = None
    if last_price:
        if decision in ["long_possible", "avoid_short"]:
            invalid_price = round(avg_price, 2) if avg_price else (round(low_price - 1, 2) if low_price else None)
            dynamic_stop  = round(last_price * 0.99, 2)
        elif decision in ["short_possible", "avoid_long"]:
            invalid_price = round(high_price + 1, 2) if high_price else None
            dynamic_stop  = round(last_price * 1.01, 2)

    risk_control = {
        "invalid_price": invalid_price,
        "dynamic_stop":  dynamic_stop,
        "vwap_distance": vwap_distance,
    }

    return {
        "symbol":   symbol,
        "name":     name,
        "decision": decision,
        "score":    score,

        "trend":       trend,
        "reversal":    reversal_signal,
        "trap":        trap_signal,
        "market_type": market_type,
        "signal_grade": signal_grade,

        "entry_signal": entry_signal,
        "entry_zone":   entry_zone,
        "risk_control": risk_control,

        "structure": {
            "dominance":      dominance,
            "pressure_ratio": pressure_ratio,
        },
        "price": {
            "last":           last_price,
            "change_percent": change_percent,
        },
    }

# =============================================
# 狀態管理
# =============================================

def reset_entry_memory(symbol: str | None = None) -> None:
    """清除 entry_memory 狀態，防止計數器在異常情況下累積錯誤。
    symbol=None 時清除所有標的；指定 symbol 則只清除該標的。
    """
    if symbol is None:
        entry_memory.clear()
    else:
        entry_memory.pop(symbol, None)
