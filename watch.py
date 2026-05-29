#!/usr/bin/env python3
"""
watch.py — 當沖部位追蹤 + 即時策略提示

新增部位:
  python watch.py add 2330 buy 550          多單，停損預設 -2%
  python watch.py add 2330 buy 550 539      多單，自訂停損 539
  python watch.py add 2454 sell 120         空單

更新停損/停利:
  python watch.py update 2330 stop 545
  python watch.py update 2330 t1 562
  python watch.py update 2330 t2 567

出場並記錄損益:
  python watch.py close 2330 556         出場價 556，計算損益並寫入 trades.json

移除部位（不記錄）:
  python watch.py remove 2330

查看交易記錄:
  python watch.py history                最近 10 筆
  python watch.py history 20             最近 20 筆

查看清單:
  python watch.py list

啟動監看 (預設每 6 秒更新):
  python watch.py
  python watch.py --interval 5
"""
import sys
import os
import time
import json
import logging
import tempfile
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

POSITIONS_FILE   = os.path.join(os.path.dirname(__file__), "positions.json")
TRADES_FILE      = os.path.join(os.path.dirname(__file__), "trades.json")
DEFAULT_INTERVAL = 6

_BASE      = os.getenv("RAILWAY_API_URL", "http://127.0.0.1:8000")
TG_TOKEN   = os.getenv("TG_BOT_TOKEN",  "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID",    "")

STOP_PCT = 0.02
T1_PCT   = 0.02
T2_PCT   = 0.03
SHORT_ADD_V6_THRESHOLD = -60

def _atr_pcts(sym: str) -> tuple:
    """查 API 取 ATR/true range 波幅，計算自適應 stop/T1/T2 比例。
    若缺少前收資料則退回昨日 high-low；若無資料則回傳預設值。
    """
    try:
        r = requests.get(f"{_BASE}/analysis-input/{sym}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            api_atr = safe_float(d.get("atr_pct"))
            if api_atr is not None and api_atr > 0:
                atr = api_atr / 100 if api_atr > 1 else api_atr
                stop = max(0.015, min(0.03,  atr * 0.8))
                t1   = max(0.02,  min(0.05,  atr * 1.0))
                t2   = max(0.03,  min(0.07,  atr * 1.5))
                return stop, t1, t2

            y_high  = safe_float(d.get("y_high"))
            y_low   = safe_float(d.get("y_low"))
            y_close = safe_float(d.get("y_close"))
            y_prev_close = safe_float(
                d.get("y_prev_close") or d.get("prev_close") or d.get("previous_close")
            )
            if y_high is not None and y_low is not None and y_close and y_close > 0:
                if y_prev_close and y_prev_close > 0:
                    tr = max(
                        y_high - y_low,
                        abs(y_high - y_prev_close),
                        abs(y_low - y_prev_close),
                    )
                else:
                    tr = y_high - y_low
                atr = tr / y_close
                stop = max(0.015, min(0.03,  atr * 0.8))
                t1   = max(0.02,  min(0.05,  atr * 1.0))
                t2   = max(0.03,  min(0.07,  atr * 1.5))
                return stop, t1, t2
    except Exception as e:
        logging.warning("_atr_pcts %s: %s", sym, e)
    return STOP_PCT, T1_PCT, T2_PCT

DATA_STALE_WARN  = 60   # 超過幾秒顯示黃色警告
DATA_STALE_ERROR = 120  # 超過幾秒顯示紅色警告

# 已發過警報的 key（symbol_urgency_stop），避免重複發送
_alerted: set = set()

# ─── ANSI ────────────────────────────────────────────────
def _c(code, s): return f"\033[{code}m{s}\033[0m"
def green(s):    return _c("32", s)
def red(s):      return _c("31", s)
def yellow(s):   return _c("33", s)
def cyan(s):     return _c("36", s)
def bold(s):     return _c("1",  s)
def dim(s):      return _c("2",  s)
def bg_red(s):   return _c("41;97", s)
def bg_green(s): return _c("42;30", s)

def score_color(v6):
    if v6 is None:
        return "?"
    s = f"{v6:+.0f}"
    return green(s) if v6 >= 60 else (red(s) if v6 <= 0 else yellow(s))

# ─── positions.json ──────────────────────────────────────
def load_positions() -> dict:
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WARN] 讀取 positions.json 失敗: {e}")
        return {}

def save_positions(data: dict):
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POSITIONS_FILE)

def load_trades() -> list:
    try:
        with open(TRADES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[WARN] 讀取 trades.json 失敗: {e}")
        return []

def save_trades(trades: list):
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TRADES_FILE)

def _validate_positions(positions: dict) -> dict:
    """Drop positions whose required numeric fields are missing or zero."""
    valid = {}
    for sym, pos in positions.items():
        if not isinstance(pos, dict):
            print(f"[WARN] {sym} 部位資料格式錯誤（非 dict），已跳過: {pos}")
            continue
        required = ("entry", "stop", "t1", "t2")
        if all(pos.get(f) not in (None, 0, "") for f in required):
            valid[sym] = pos
        else:
            print(f"[WARN] {sym} 部位資料損毀，已跳過: {pos}")
    return valid

# ─── helpers ─────────────────────────────────────────────
def safe_float(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (ValueError, TypeError):
        return None

def parse_direction(s: str) -> str:
    s = s.lower()
    if s in ("buy", "b", "long", "多"):
        return "long"
    if s in ("sell", "s", "short", "空"):
        return "short"
    raise ValueError(f"無效方向 '{s}'，請用 buy / sell")

def data_age_secs(ind: dict):
    """回傳資料距現在幾秒（UTC），無 _server_ts 則回傳 None"""
    ts_str = ind.get("_server_ts")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        # 相容舊版可能寫入的 naive datetime
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()
    except Exception:
        return None

# ─── Telegram 警報 ───────────────────────────────────────
def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception:
        pass

def check_alerts(positions: dict, results: dict):
    for sym, pos in positions.items():
        ind   = results.get(sym) or {}
        if ind.get("_stale"):
            continue
        price = safe_float(
            ind.get("score_price") or ind.get("current_price") or ind.get("last")
        )
        if price is None:
            continue

        _, urgency = compute_strategy(pos, price, ind)
        if urgency not in ("stop", "profit"):
            continue

        threshold = pos["stop"] if urgency == "stop" else pos["t2"]
        alert_key = f"{sym}_{urgency}_{threshold}"
        if alert_key in _alerted:
            continue
        _alerted.add(alert_key)

        dir_label = "多↑" if pos["direction"] == "long" else "空↓"
        if urgency == "stop":
            msg = (
                f"🔴 停損警報！\n"
                f"{sym} {dir_label}  進場: {pos['entry']}  現價: {price}\n"
                f"停損: {pos['stop']}  已觸及，請立即出場！"
            )
        else:
            msg = (
                f"🟢 停利訊號！\n"
                f"{sym} {dir_label}  進場: {pos['entry']}  現價: {price}\n"
                f"已達停利 T2: {pos['t2']}，建議獲利了結"
            )

        print('\a', end='', flush=True)
        send_telegram(msg)

# ─── 資料抓取 ─────────────────────────────────────────────
def fetch_symbol(symbol: str) -> dict:
    try:
        r = requests.get(f"{_BASE}/analysis-input/{symbol}", timeout=12)
        if r.status_code == 200:
            d = r.json()
            d.setdefault("symbol", symbol)
            return d
    except Exception:
        pass
    return {"symbol": symbol, "status": "pending"}

def fetch_all(symbols: list) -> dict:
    try:
        r = requests.get(
            f"{_BASE}/analysis-batch",
            params={"symbols": ",".join(symbols)},
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 8)) as ex:
        futs = {ex.submit(fetch_symbol, s): s for s in symbols}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    return results

# ─── 策略引擎 ─────────────────────────────────────────────
def compute_strategy(pos: dict, price: float, ind: dict) -> tuple:
    """
    urgency: "stop" | "profit" | "add" | "warn" | "ok" | "watch"
    """
    direction = pos["direction"]
    entry     = safe_float(pos.get("entry"))
    stop      = safe_float(pos.get("stop"))
    t1        = safe_float(pos.get("t1"))
    t2        = safe_float(pos.get("t2"))

    if not entry or stop is None or t1 is None or t2 is None:
        return "⚠️ 部位價位資料異常，請確認 entry/stop/t1/t2", "error"

    k         = safe_float(ind.get("k_1min"))
    d         = safe_float(ind.get("d_1min"))
    vwap      = safe_float(ind.get("vwap_1min"))
    v6        = safe_float(ind.get("v6_score"))
    kd_signal = ind.get("kd_signal", "none")

    # 區分「剛形成交叉」（強訊號）與「維持方向」（弱訊號）
    kd_gold_fresh  = kd_signal == "gold_cross"
    kd_death_fresh = kd_signal == "death_cross"
    kd_gold        = kd_gold_fresh  or (k is not None and d is not None and k > d)
    kd_death       = kd_death_fresh or (k is not None and d is not None and k < d)
    above_vwap     = vwap is not None and price > vwap
    below_vwap     = vwap is not None and price < vwap

    if direction == "long":
        gain_pct = (price - entry) / entry * 100

        if price <= stop:
            return "🔴 已觸停損，立即出場！", "stop"
        if price >= t2:
            return "🟢 達停利 T2，建議出場獲利了結", "profit"
        if price >= t1:
            if kd_death:
                return "🟠 達 T1 且 KD 轉弱，建議減碼或移停損至成本", "warn"
            return f"🟡 達停利 T1，建議移停損至成本 {entry:.2f}", "warn"
        # 加碼條件：需要 KD 剛形成金叉（非靜態偏多）+ VWAP 站上 + V6 強
        if gain_pct > 0.5 and kd_gold_fresh and above_vwap and v6 is not None and v6 >= 70:
            return "🔵 動能齊備（V6 KD金叉 VWAP 全多），可考慮加碼", "add"
        if gain_pct < -1.5 and kd_death_fresh:
            return "🟠 虧損加速 + KD 剛死叉，注意是否觸及停損！", "warn"
        if kd_death_fresh and below_vwap and gain_pct < 0.5:
            return "🟠 KD 剛死叉 + 跌破 VWAP，考慮提前減碼", "warn"
        if kd_gold and above_vwap and v6 is not None and v6 >= 60:
            return "🟢 多方強勢，可續抱", "ok"
        return "⚪ 持倉觀察", "watch"

    else:  # short
        gain_pct = (entry - price) / entry * 100

        if price >= stop:
            return "🔴 已觸停損，立即出場！", "stop"
        if price <= t2:
            return "🟢 達停利 T2，建議出場獲利了結", "profit"
        if price <= t1:
            if kd_gold:
                return "🟠 達 T1 且 KD 轉強，建議減碼或移停損至成本", "warn"
            return f"🟡 達停利 T1，建議移停損至成本 {entry:.2f}", "warn"
        # 加碼條件：需要 KD 剛形成死叉（非靜態偏空）+ VWAP 跌破 + V6 強空
        if gain_pct > 0.5 and kd_death_fresh and below_vwap and v6 is not None and v6 <= SHORT_ADD_V6_THRESHOLD:
            return "🔵 空方動能齊備（V6 KD死叉 VWAP 全空），可考慮加碼", "add"
        if gain_pct < -1.5 and kd_gold_fresh:
            return "🟠 虧損加速 + KD 剛金叉，注意是否觸及停損！", "warn"
        if kd_gold_fresh and above_vwap:
            return "🟠 KD 剛金叉 + 站上 VWAP，考慮提前減碼", "warn"
        if kd_death and below_vwap and v6 is not None and v6 <= -10:
            return "🟢 空方強勢，可續抱", "ok"
        return "⚪ 持倉觀察", "watch"

def colorize_advice(advice: str, urgency: str) -> str:
    if urgency == "stop":   return bg_red(f" {advice} ")
    if urgency == "profit": return bg_green(f" {advice} ")
    if urgency == "add":    return cyan(bold(advice))
    if urgency == "warn":   return yellow(advice)
    if urgency == "ok":     return green(advice)
    return dim(advice)

# ─── 渲染單一部位 ──────────────────────────────────────────
def render_position(sym: str, pos: dict, ind: dict) -> list:
    direction = pos["direction"]
    entry     = safe_float(pos.get("entry"))
    stop      = safe_float(pos.get("stop"))
    t1        = safe_float(pos.get("t1"))
    t2        = safe_float(pos.get("t2"))
    dir_label = green("多 ↑") if direction == "long" else red("空 ↓")

    if not entry:
        return [red(f"  {bold(sym):<6} ⚠️ 進場價異常，請用 update 修正"), ""]

    price = safe_float(
        ind.get("score_price") or ind.get("current_price") or ind.get("last")
    )

    # 資料尚未就緒或伺服器只回了過期快取
    if ind.get("status") == "pending" or ind.get("_stale") or price is None:
        stale_note = ""
        if ind.get("_stale"):
            age = safe_float(ind.get("_age_secs"))
            stale_note = yellow(f"⚠️ 快取過期 {int(age)}s，等待 uploader 重抓...") if age else yellow("⚠️ 快取過期，等待 uploader 重抓...")
        return [
            f"  {bold(sym):<6} {dir_label}  進場: {entry:.2f}  {stale_note or yellow('⏳ 等待 uploader 回應...')}",
            "",
        ]

    v6    = safe_float(ind.get("v6_score"))
    k     = safe_float(ind.get("k_1min"))
    d_val = safe_float(ind.get("d_1min"))
    vwap  = safe_float(ind.get("vwap_1min"))
    vol_r = safe_float(ind.get("volume_ratio"))
    tags  = (ind.get("v6_tags") or "")[:40]

    # 資料時效
    age = data_age_secs(ind)
    if age is None:
        freshness = ""
    elif age > DATA_STALE_ERROR:
        freshness = red(f" ⚠️ 資料已 {int(age)}s 未更新！")
    elif age > DATA_STALE_WARN:
        freshness = yellow(f" ⏱ {int(age)}s 前")
    else:
        freshness = dim(f" {int(age)}s 前")

    # 損益計算
    if direction == "long":
        gain_pct = (price - entry) / entry * 100
        to_stop  = (price - stop)  / entry * 100
        to_t1    = (t1 - price)    / entry * 100
        to_t2    = (t2 - price)    / entry * 100
    else:
        gain_pct = (entry - price) / entry * 100
        to_stop  = (stop - price)  / entry * 100
        to_t1    = (price - t1)    / entry * 100
        to_t2    = (price - t2)    / entry * 100

    gain_col  = green(f"{gain_pct:+.2f}%") if gain_pct >= 0 else red(f"{gain_pct:+.2f}%")
    stop_label = red(f"{stop:.2f}") if to_stop < 0.5 else f"{stop:.2f}"
    t1_label   = green(f"{t1:.2f}") if to_t1 <= 0 else f"{t1:.2f}"
    t2_label   = green(f"{t2:.2f}") if to_t2 <= 0 else f"{t2:.2f}"

    # 策略
    advice, urgency = compute_strategy(pos, price, ind)
    advice_col = colorize_advice(advice, urgency)

    # KD
    if k is not None and d_val is not None:
        arrow  = green("↑") if k > d_val else red("↓")
        kd_col = f"K:{k:.0f}/D:{d_val:.0f}{arrow}"
    else:
        kd_col = "KD: ?"

    # VWAP
    if vwap and price:
        pos_sym  = green("▲") if price > vwap else red("▼")
        vwap_col = f"VWAP:{vwap:.2f}{pos_sym}"
    else:
        vwap_col = "VWAP: ?"

    v6_col  = f"V6:{score_color(v6)}"
    vol_col = f"量:{vol_r:.1f}x" if vol_r else "量: ?"

    hint_stop = dim(f"距停損 {to_stop:+.1f}%") if to_stop >= 0 else red(f"已穿停損 {to_stop:.1f}%")
    hint_t1   = dim(f"距T1 {to_t1:+.1f}%")    if to_t1 > 0  else green(f"已達T1 {-to_t1:.1f}%↑")

    return [
        (
            f"  {bold(sym):<6} {dir_label}  "
            f"進:{entry:.2f}  現:{bold(f'{price:.2f}')}  損益:{gain_col}  "
            f"停損:{stop_label}  T1:{t1_label}  T2:{t2_label}{freshness}"
        ),
        (
            f"          {v6_col}  {kd_col}  {vwap_col}  {vol_col}  "
            f"{hint_stop}  {hint_t1}  {dim(tags)}"
        ),
        f"          {advice_col}",
        "",
    ]

# ─── 主顯示 ───────────────────────────────────────────────
def print_monitor(positions: dict, results: dict, interval: int):
    now = datetime.now().strftime("%H:%M:%S")
    os.system("clear")
    print(
        f"{bold(cyan('🎯 當沖追蹤儀'))}  {now}  |  "
        f"{len(positions)} 部位  |  每 {interval}s 更新  |  Ctrl+C 停止"
    )
    print("─" * 80)

    if not positions:
        print(dim("  尚無部位。用 python watch.py add <代號> <buy|sell> <進場價> 新增。"))
    else:
        for sym, pos in positions.items():
            ind = results.get(sym) or {"status": "pending"}
            for line in render_position(sym, pos, ind):
                print(line)

    print("─" * 80)

# ─── 子指令 ───────────────────────────────────────────────
def cmd_add(args):
    if len(args) < 3:
        print("用法: python watch.py add <代號> <buy|sell> <進場價> [停損價]")
        return

    sym       = args[0].strip()
    direction = parse_direction(args[1])
    entry     = float(args[2])

    stop_pct, t1_pct, t2_pct = _atr_pcts(sym)

    if direction == "long":
        default_stop = round(entry * (1 - stop_pct), 2)
        t1           = round(entry * (1 + t1_pct), 2)
        t2           = round(entry * (1 + t2_pct), 2)
    else:
        default_stop = round(entry * (1 + stop_pct), 2)
        t1           = round(entry * (1 - t1_pct), 2)
        t2           = round(entry * (1 - t2_pct), 2)

    stop = float(args[3]) if len(args) >= 4 else default_stop

    positions = load_positions()
    positions[sym] = {
        "direction": direction,
        "entry":     entry,
        "stop":      stop,
        "t1":        t1,
        "t2":        t2,
        "added_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_positions(positions)

    # 同步推進 server 的 watch_set，讓 uploader 訂閱清單立刻納入此標的
    try:
        requests.get(f"{_BASE}/analysis-batch", params={"symbols": sym}, timeout=5)
    except Exception as e:
        print(f"[WARN] 同步 watch_set 失敗（不影響本地部位）: {e}")

    dir_label = "多↑" if direction == "long" else "空↓"
    atr_hint = "" if stop_pct == STOP_PCT else f"  (ATR自適應: 停損{stop_pct*100:.1f}% T1{t1_pct*100:.1f}% T2{t2_pct*100:.1f}%)"
    print(f"✅ 已新增  {sym} {dir_label}  進場:{entry}  停損:{stop}  T1:{t1}  T2:{t2}{atr_hint}")

def cmd_update(args):
    if len(args) < 3:
        print("用法: python watch.py update <代號> <stop|t1|t2> <新價位>")
        print("例:   python watch.py update 2330 stop 545")
        return

    sym   = args[0].strip()
    field = args[1].lower()
    if field not in ("stop", "t1", "t2"):
        print("欄位必須是 stop、t1 或 t2")
        return

    try:
        value = float(args[2])
    except ValueError:
        print("價位必須是數字")
        return

    positions = load_positions()
    if sym not in positions:
        print(f"{sym} 不在部位清單中")
        return

    old_val = positions[sym][field]
    positions[sym][field] = value
    save_positions(positions)
    print(f"✅ {sym} {field} 更新：{old_val} → {value}")

def cmd_remove(args):
    if not args:
        print("用法: python watch.py remove <代號>")
        return
    positions = load_positions()
    removed = [s for s in args if s in positions]
    for s in args:
        positions.pop(s, None)
    save_positions(positions)
    print(f"🗑️  已移除: {' '.join(removed)}" if removed else "（代號不在清單中）")

def cmd_close(args):
    if len(args) < 2:
        print("用法: python watch.py close <代號> <出場價>")
        return
    sym = args[0].strip()
    try:
        exit_price = float(args[1])
    except ValueError:
        print("出場價必須是數字")
        return

    positions = load_positions()
    if sym not in positions:
        print(f"{sym} 不在部位清單中")
        return

    pos       = positions[sym]
    entry     = safe_float(pos.get("entry"))
    direction = pos.get("direction")

    if entry is None:
        print(f"[ERROR] {sym} 部位缺少進場價，無法計算損益")
        return

    if direction == "long":
        pnl_pct = (exit_price - entry) / entry * 100
        pnl_pts = exit_price - entry
    else:
        pnl_pct = (entry - exit_price) / entry * 100
        pnl_pts = entry - exit_price

    trade = {
        "symbol":     sym,
        "direction":  direction,
        "entry":      entry,
        "exit":       exit_price,
        "pnl_pts":    round(pnl_pts, 2),
        "pnl_pct":    round(pnl_pct, 2),
        "entry_time": pos.get("added_at", ""),
        "exit_time":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)

    positions.pop(sym)
    save_positions(positions)

    mark      = "✅" if pnl_pct >= 0 else "❌"
    dir_label = "多↑" if direction == "long" else "空↓"
    print(f"{mark} {sym} {dir_label}  進:{entry}  出:{exit_price}  "
          f"損益:{pnl_pts:+.2f}點 ({pnl_pct:+.2f}%)")
    print("📝 已記錄至 trades.json")

def cmd_history(args):
    trades = load_trades()
    if not trades:
        print("尚無交易記錄。")
        return

    n = 10
    if args and args[0].isdigit():
        n = int(args[0])
    recent = trades[-n:]

    wins      = sum(1 for t in recent if t.get("pnl_pct", 0) >= 0)
    total_pnl = sum(t.get("pnl_pct", 0) for t in recent)
    win_rate  = wins / len(recent) * 100

    print(f"📊 最近 {len(recent)} 筆交易  勝率:{win_rate:.0f}%  累積損益:{total_pnl:+.2f}%")
    print("─" * 70)
    for t in recent:
        pnl_pct   = t.get("pnl_pct", 0)
        pnl_pts   = t.get("pnl_pts", 0)
        direction = t.get("direction", "long")
        mark      = "✅" if pnl_pct >= 0 else "❌"
        dir_label = "多↑" if direction == "long" else "空↓"
        print(f"  {mark} {t.get('symbol', '?'):<6} {dir_label}  "
              f"進:{t.get('entry', '?')}  出:{t.get('exit', '?')}  "
              f"{pnl_pts:+.2f}點 ({pnl_pct:+.2f}%)  "
              f"{t.get('exit_time', '')}")

def cmd_list():
    positions = load_positions()
    if not positions:
        print("目前無追蹤部位。")
        return
    print(f"📋 部位清單 ({len(positions)} 筆)")
    for sym, pos in positions.items():
        dir_label = "多↑" if pos["direction"] == "long" else "空↓"
        print(
            f"  {sym}  {dir_label}  進場:{pos['entry']}  "
            f"停損:{pos['stop']}  T1:{pos['t1']}  T2:{pos['t2']}"
        )

def cmd_monitor(interval: int):
    positions = load_positions()
    if not positions:
        print("尚無追蹤部位。用 python watch.py add 2330 buy 550 新增。")
        sys.exit(0)

    symbols = list(positions.keys())
    print(f"🚀 監看 {len(symbols)} 部位: {' '.join(symbols)}  (每 {interval}s，Ctrl+C 停止)")
    try:
        while True:
            positions = _validate_positions(load_positions())
            symbols   = list(positions.keys())
            results   = fetch_all(symbols) if symbols else {}
            check_alerts(positions, results)
            print_monitor(positions, results, interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\n✅ 已停止監看。")

# ─── main ─────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    if not args:
        cmd_monitor(DEFAULT_INTERVAL)
        return

    subcmd = args[0]
    if subcmd in ("help", "--help", "-h"):
        print(__doc__.strip())
        return
    if subcmd == "add":
        cmd_add(args[1:])
        return
    if subcmd == "update":
        cmd_update(args[1:])
        return
    if subcmd == "remove":
        cmd_remove(args[1:])
        return
    if subcmd == "close":
        cmd_close(args[1:])
        return
    if subcmd in ("history", "hist"):
        cmd_history(args[1:])
        return
    if subcmd in ("list", "ls"):
        cmd_list()
        return

    interval = DEFAULT_INTERVAL
    i = 0
    while i < len(args):
        if args[i] in ("--interval", "-i") and i + 1 < len(args):
            try:
                interval = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    cmd_monitor(interval)

if __name__ == "__main__":
    main()
