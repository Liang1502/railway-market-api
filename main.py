from fastapi import FastAPI, HTTPException, Header
from typing import Dict, Set
from datetime import datetime
import asyncio
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = FastAPI()

market_data: Dict[str, dict] = {}
wishlist:    Set[str] = set()
watch_set:   Set[str] = set()   # watch.py 請求的標的，永久追蹤不清除

MY_SECRET_TOKEN = os.getenv("API_SECRET_TOKEN", "ChiaChun_Super_Secret_888")

STALE_SECS = 90   # 超過此秒數視為過期，重新觸發 wishlist

def cache_age_secs(data: dict) -> float:
    ts_str = data.get("_server_ts", "")
    try:
        return (datetime.utcnow() - datetime.fromisoformat(ts_str)).total_seconds()
    except Exception:
        return 9999

def is_stale(data: dict) -> bool:
    return cache_age_secs(data) > STALE_SECS

# =============================
# 📥 接收資料（uploader.py 打這裡）
# =============================
@app.post("/update")
def update_data(data: dict, authorization: str = Header(None)):
    if authorization != f"Bearer {MY_SECRET_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    symbol = data.get("symbol")
    if symbol:
        data["_server_ts"] = datetime.utcnow().isoformat()
        market_data[symbol] = data
        wishlist.discard(symbol)
    return {"status": "ok"}

# =============================
# 📊 單檔查詢（長輪詢，最多等 10 秒）
# =============================
@app.get("/analysis-input/{symbol}")
async def get_analysis(symbol: str):
    cached = market_data.get(symbol)
    if cached and not is_stale(cached):
        return cached

    if cached:
        old_ts = cached.get("_server_ts", "")
        wishlist.add(symbol)
        for _ in range(10):
            await asyncio.sleep(1)
            fresh = market_data.get(symbol)
            if fresh and fresh.get("_server_ts", "") != old_ts and not is_stale(fresh):
                wishlist.discard(symbol)
                return fresh
        stale_data = dict(cached)
        stale_data["_stale"] = True
        stale_data["_age_secs"] = int(cache_age_secs(cached))
        return stale_data

    wishlist.add(symbol)

    for _ in range(10):
        await asyncio.sleep(1)
        if symbol in market_data:
            wishlist.discard(symbol)
            return market_data[symbol]

    return {
        "status":  "pending",
        "symbol":  symbol,
        "message": f"標的 {symbol} 尚無資料，請確認 uploader.py 是否運作中"
    }

# =============================
# 📊 批次查詢（watch.py 使用，最多等 5 秒）
# =============================
@app.get("/analysis-batch")
async def get_analysis_batch(symbols: str):
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise HTTPException(status_code=400, detail="symbols 不可為空")

    # 所有請求的標的自動加入持久追蹤清單
    for sym in symbol_list:
        watch_set.add(sym)

    result = {}
    pending = []
    stale_pending = {}
    for sym in symbol_list:
        if sym in market_data:
            if is_stale(market_data[sym]):
                wishlist.add(sym)
                pending.append(sym)
                stale_pending[sym] = market_data[sym].get("_server_ts", "")
            else:
                result[sym] = market_data[sym]
        else:
            wishlist.add(sym)
            pending.append(sym)

    for _ in range(5):
        if not pending:
            break
        await asyncio.sleep(1)
        still = []
        for sym in pending:
            data = market_data.get(sym)
            old_ts = stale_pending.get(sym)
            if data and (old_ts is None or data.get("_server_ts", "") != old_ts):
                result[sym] = data
                wishlist.discard(sym)
            else:
                still.append(sym)
        pending = still

    for sym in pending:
        if sym in market_data:
            data = dict(market_data[sym])
            data["_stale"] = True
            data["_age_secs"] = int(cache_age_secs(market_data[sym]))
            result[sym] = data
        else:
            result[sym] = {
                "symbol": sym,
                "status": "pending",
                "message": "uploader 尚未回應"
            }
    return result

# =============================
# 📋 許願池（一次性任務）
# =============================
@app.get("/wishlist")
def get_wishlist():
    return {"wishlist": list(wishlist)}

# =============================
# 👁 持久追蹤清單（uploader.py 定期拉取）
# =============================
@app.get("/watch-list")
def get_watch_list():
    return {"symbols": list(watch_set)}

@app.delete("/watch-list/{symbol}")
def remove_from_watch_list(symbol: str, authorization: str = Header(None)):
    if authorization != f"Bearer {MY_SECRET_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    watch_set.discard(symbol)
    return {"status": "ok", "remaining": list(watch_set)}

# =============================
# 🔍 市場掃描（radar.py 使用）
# =============================
@app.get("/scan")
def scan_market():
    short_list = []
    long_list  = []

    for data in market_data.values():
        if not data.get("decision"):
            continue

        decision      = data.get("decision", "")
        entry         = data.get("entry_signal", {})
        short_trigger = entry.get("short_trigger", False)
        long_trigger  = entry.get("long_trigger",  False)

        if decision == "short_possible" or short_trigger:
            short_list.append(data)
        if decision == "long_possible" or long_trigger:
            long_list.append(data)

    short_list.sort(key=lambda x: x.get("score", 50))
    long_list.sort( key=lambda x: x.get("score", 50), reverse=True)

    return {
        "short_count": len(short_list),
        "long_count":  len(long_list),
        "top_short":   short_list[:3],
        "top_long":    long_list[:3],
    }

# =============================
# 🏥 健康檢查
# =============================
@app.get("/health")
def health():
    return {
        "status":          "ok",
        "symbols_cached":  len(market_data),
        "watch_set_count": len(watch_set),
    }
