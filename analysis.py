from fubon_neo.sdk import FubonSDK
import os
import time
import json
import requests
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

ACCOUNT       = os.getenv("FUBON_ACCOUNT")
PASSWORD      = os.getenv("FUBON_PASSWORD")
CERT_PATH     = os.getenv("FUBON_CERT_PATH")
CERT_PASSWORD = os.getenv("FUBON_CERT_PASSWORD")
SYMBOL        = "2330"

entry_memory = {}

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

# =============================================
# 主要分析函數
# =============================================

def extract_analysis_data(ticker, quote):
    # --- 取 symbol ---
    symbol = (
        _get(ticker, 'symbol') or
        _get(quote,  'symbol') or
        "unknown"
    )

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
        if last_price > avg_price:
            trend = "up"
        elif last_price < avg_price:
            trend = "down"

    # =============================
    # 基礎數據
    # =============================
    mid_price = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None
    distance_from_mid = round(last_price - mid_price, 2) if (last_price and mid_price) else None
    vwap_distance = (
        round(((last_price - avg_price) / avg_price) * 100, 2)
        if (last_price and avg_price) else 0
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
    is_near_high = high_price and last_price and (high_price - last_price) / last_price <= NEAR_PCT
    is_near_low  = low_price  and last_price and (last_price - low_price)  / last_price <= NEAR_PCT

    trap_signal = None
    if is_near_high and dominance == "sell" and change_percent < 0.5:
        trap_signal = "bull_trap"
    elif is_near_low and dominance == "buy" and change_percent > -0.5:
        trap_signal = "bear_trap"

    # =============================
    # 反轉偵測
    # =============================
    reversal_signal = None
    if (trend == "down" and change_percent > 1.2 and
            last_price and mid_price and last_price > mid_price and
            dominance == "buy" and distance_from_mid and distance_from_mid > 1.5):
        reversal_signal = "bullish_reversal"
    elif (trend == "up" and change_percent < -1.2 and
            last_price and mid_price and last_price < mid_price and
            dominance == "sell" and distance_from_mid and distance_from_mid < -1.5):
        reversal_signal = "bearish_reversal"

    # =============================
    # 絕對禁令濾網
    # =============================
    is_limit_locked  = not bids or not asks
    is_over_7_percent = change_percent >= 7.0
    is_fomo_extreme   = vwap_distance >= 2.0

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
    elif dominance == "buy" and pressure_ratio and pressure_ratio < 0.8:
        decision = "long_possible" if trend == "up" else "observe"
    elif dominance == "sell" and pressure_ratio and pressure_ratio > 1.2:
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
        pressure_ratio and pressure_ratio > 1.3 and
        last_price and mid_price and last_price < mid_price and
        trend == "down" and reversal_signal is None and not is_limit_locked
    )
    long_raw = (
        pressure_ratio and pressure_ratio < 0.8 and
        last_price and mid_price and last_price > mid_price and
        trend == "up" and reversal_signal is None and
        not is_limit_locked and not is_over_7_percent and not is_fomo_extreme
    )

    if symbol not in entry_memory:
        entry_memory[symbol] = {"short": 0, "long": 0, "short_time": None, "long_time": None}

    now = time.time()
    TIME_WINDOW = 60

    def update_trigger(side):
        time_key = f"{side}_time"
        if entry_memory[symbol][time_key] and (now - entry_memory[symbol][time_key]) <= TIME_WINDOW:
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
    if reversal_signal:             market_type = "reversal"
    elif trend in ["up","down"] and abs(change_percent) > 1: market_type = "trend"
    elif trap_signal:               market_type = "trap"

    # grade 區分方向：A_long/A_short 避免 GPT 誤把強空訊號解讀為強多
    if score >= 75:    signal_grade = "A_long"
    elif score <= 25:  signal_grade = "A_short"
    elif score >= 60:  signal_grade = "B_long"
    elif score <= 40:  signal_grade = "B_short"
    else:              signal_grade = "C"

    entry_zone = {"lower": None, "upper": None}
    if mid_price and last_price:
        if decision in ["long_possible", "avoid_short", "short_possible", "avoid_long"]:
            entry_zone = {
                "lower": round(mid_price - 1, 2),
                "upper": round(mid_price + 1, 2)
            }

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

# ==========================================
# 波段投資診斷邏輯
# ==========================================
def extract_investment_data(ticker, daily_data):
    symbol     = _get(ticker, 'symbol') or "unknown"
    last_price = (
        _to_float(_get(ticker, 'lastPrice')) or
        _to_float(_get(ticker, 'last_price')) or
        _to_float(_get(ticker, 'last'))
    )

    ma5  = daily_data.get("ma5")
    ma20 = daily_data.get("ma20")
    ma60 = daily_data.get("ma60")
    rsi  = daily_data.get("rsi")
    yoy  = daily_data.get("yoy", 0)

    is_bull   = (last_price > ma5 > ma20 > ma60) if (last_price and ma5 and ma20 and ma60) else False
    is_growth = yoy > 15

    decision     = "investment_watch"
    signal_grade = "C"
    if is_bull and is_growth and rsi and 50 < rsi < 75:
        decision     = "strong_buy_candidate"
        signal_grade = "A"

    return {
        "type":         "INVESTMENT",
        "symbol":       symbol,
        "decision":     decision,
        "signal_grade": signal_grade,
        "indicators": {
            "price":     last_price,
            "ma_status": "多頭排列" if is_bull else "整理中",
            "rsi":       rsi,
            "yoy":       yoy,
        },
    }

# =============================================
# AI Telegram 報告 (防彈修正版)
# =============================================
def send_ai_telegram_report(analysis_result, gemini_key=None, tg_token=None, tg_chat_id=None):
    gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
    tg_token   = tg_token   or os.getenv("TG_BOT_TOKEN", "")
    tg_chat_id = tg_chat_id or os.getenv("TG_CHAT_ID", "")
    try:
        genai.configure(api_key=gemini_key)
        model_list   = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        correct_name = next((m for m in model_list if 'flash' in m), model_list[0])
        ai_model     = genai.GenerativeModel(correct_name)

        symbol        = analysis_result['symbol']
        is_investment = analysis_result.get("type") == "INVESTMENT"
        analysis_type_str = "「波段投資標的」" if is_investment else "「極短線當沖」"
        tone_instruction  = "專注於均線趨勢與基本面營收" if is_investment else "專注於五檔力道與價格乖離"

        prompt = f"""
        你是一位精準的台股交易專家。請針對這份{analysis_type_str}數據撰寫投資快報。
        {tone_instruction}，口吻需專業且帶點評分感。

        【數據內容】:
        {json.dumps(analysis_result, ensure_ascii=False)}

        【撰寫規範】:
        1. 標題：【{symbol}】今日診斷 / 評級：{analysis_result['signal_grade']}
        2. 分點說明：目前狀況、數據診斷、操作建議。
        3. 結尾附上 TradingView：https://www.tradingview.com/chart/?symbol=TWSE:{symbol}
        4. 請多用 Emoji 增加易讀性。
        """

        response    = ai_model.generate_content(prompt)
        report_text = response.text

        tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"

        # --- 第一次嘗試：帶 Markdown 格式 ---
        res = requests.post(tg_url, json={
            "chat_id":    tg_chat_id,
            "text":       report_text,
            "parse_mode": "Markdown"
        }, timeout=10)

        # --- 如果 Markdown 失敗 (通常是符號衝突)，嘗試純文字發送 ---
        if res.status_code != 200:
            print(f"⚠️ Markdown 格式失效 (錯誤 {res.status_code})，嘗試純文字降級發送...")
            res = requests.post(tg_url, json={
                "chat_id":    tg_chat_id,
                "text":       report_text
            }, timeout=10)

        # --- 最終檢查 ---
        if res.status_code == 200:
            print(f"✅ {symbol} 報告實質送達！(模型: {correct_name})")
        else:
            print(f"❌ Telegram API 拒絕發送：{res.text}")

    except Exception as e:
        print(f"❌ AI 報告崩潰: {e}")


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
