from fastapi import FastAPI
from typing import Dict, Set

app = FastAPI()

# 🔥 用來存市場資料（記憶體）
market_data: Dict[str, dict] = {}

# 🔥 新增：用來儲存 TradingView 傳來的動態名單
dynamic_watchlist: Set[str] = {"2330"}  # 預設先放一檔台積電確保運作

# =============================
# 📥 接收資料（本機會打這裡）
# =============================
@app.post("/update")
def update_data(data: dict):
    symbol = data.get("symbol")
    if symbol:
        market_data[symbol] = data
    return {"status": "ok"}

# =============================
# 📊 單檔分析
# =============================
@app.get("/analysis-input/{symbol}")
def get_analysis(symbol: str):
    return market_data.get(symbol, {"error": "no data"})

# =============================
# 🔍 掃描市場
# =============================
@app.get("/scan")
def scan_market():
    short_list = []
    long_list = []

    for data in market_data.values():
        # 改用 decision 存在即可進入掃描
        if not data.get("decision"):
            continue

        score = data.get("score", 50)
        decision = data.get("decision")
        entry = data.get("entry_signal", {})

        short_trigger = entry.get("short_trigger", False)
        long_trigger = entry.get("long_trigger", False)

        # 空方
        if decision in ["short_possible", "avoid_long"] or short_trigger:
            short_list.append(data)

        # 多方
        if decision in ["long_possible", "avoid_short"] or long_trigger:
            long_list.append(data)

    # 排序
    short_list = sorted(short_list, key=lambda x: x["score"])
    long_list = sorted(long_list, key=lambda x: x["score"], reverse=True)

    return {
        "short_count": len(short_list),
        "long_count": len(long_list),
        "top_short": short_list[:3],
        "top_long": long_list[:3]
    }

# =============================
# 📡 接收 TradingView Webhook (新增)
# =============================
@app.post("/tv-webhook")
def receive_tv_alert(data: dict):
    symbol = data.get("symbol")
    if symbol:
        # 濾掉 TradingView 可能自帶的 TWSE: 或 TPEX: 前綴
        clean_symbol = symbol.replace("TWSE:", "").replace("TPEX:", "")
        dynamic_watchlist.add(clean_symbol)
        print(f"📥 TradingView 新增監控標的: {clean_symbol}")
    return {"status": "ok", "watchlist": list(dynamic_watchlist)}

# =============================
# 📋 讓本地端 uploader.py 索取最新名單 (新增)
# =============================
@app.get("/get-watchlist")
def get_watchlist():
    return {"symbols": list(dynamic_watchlist)}
