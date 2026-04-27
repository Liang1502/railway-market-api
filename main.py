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
wishlist: Set[str] = set()

MY_SECRET_TOKEN = os.getenv("API_SECRET_TOKEN", "ChiaChun_Super_Secret_888")

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
    if symbol in market_data:
        return market_data[symbol]

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

    result = {}
    pending = []
    for sym in symbol_list:
        if sym in market_data:
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
            if sym in market_data:
                result[sym] = market_data[sym]
                wishlist.discard(sym)
            else:
                still.append(sym)
        pending = still

    for sym in pending:
        result[sym] = {
            "symbol": sym,
            "status": "pending",
            "message": "uploader 尚未回應"
        }
    return result

# =============================
# 📋 許願池（uploader.py 領取任務）
# =============================
@app.get("/wishlist")
def get_wishlist():
    return {"wishlist": list(wishlist)}

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

        if decision in ("short_possible", "avoid_long") or short_trigger:
            short_list.append(data)
        if decision in ("long_possible", "avoid_short") or long_trigger:
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
    return {"status": "ok", "symbols_cached": len(market_data)}
