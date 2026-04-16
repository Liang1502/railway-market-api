from fastapi import FastAPI
from typing import Dict, Set

app = FastAPI()

market_data: Dict[str, dict] = {}
wishlist: Set[str] = set() # 🌟 新增：雲端許願池

# =============================
# 📥 接收資料（本機會打這裡）
# =============================
@app.post("/update")
def update_data(data: dict):
    symbol = data.get("symbol")
    if symbol:
        market_data[symbol] = data
        # 🌟 如果這檔股票原本在許願池裡，代表本機已經算好上傳了，將它移出名單
        if symbol in wishlist:
            wishlist.remove(symbol)
    return {"status": "ok"}

# =============================
# 📊 單檔分析（GPT 呼叫這裡）
# =============================
@app.get("/analysis-input/{symbol}")
def get_analysis(symbol: str):
    if symbol in market_data:
        return market_data[symbol]
    else:
        # 🌟 GPT 點了一首沒聽過的歌，加入許願池
        wishlist.add(symbol)
        return {
            "status": "pending", 
            "error": "data_missing",
            "message": f"📡 標的 {symbol} 不在監控名單內。已加入雲端許願池，請等待 3 秒後再問一次！"
        }

# =============================
# 📥 讓 Mac 領取任務（新增）
# =============================
@app.get("/wishlist")
def get_wishlist():
    return {"wishlist": list(wishlist)}

# =============================
# 🔍 掃描市場
# =============================
@app.get("/scan")
def scan_market():
    short_list = []
    long_list = []

    for data in market_data.values():
        if not data.get("decision"):
            continue

        score = data.get("score", 50)
        decision = data.get("decision")
        entry = data.get("entry_signal", {})

        short_trigger = entry.get("short_trigger", False)
        long_trigger = entry.get("long_trigger", False)

        if decision in ["short_possible", "avoid_long"] or short_trigger:
            short_list.append(data)
        if decision in ["long_possible", "avoid_short"] or long_trigger:
            long_list.append(data)

    short_list = sorted(short_list, key=lambda x: x["score"])
    long_list = sorted(long_list, key=lambda x: x["score"], reverse=True)

    return {
        "short_count": len(short_list),
        "long_count": len(long_list),
        "top_short": short_list[:3],
        "top_long": long_list[:3]
    }
