from fastapi import FastAPI, HTTPException, Header  # <--- 確保多了 Header
from typing import Dict, Set, List
import asyncio  # 🌟 新增這個，用來讓伺服器稍微等一下

app = FastAPI()

market_data: Dict[str, dict] = {}
wishlist: Set[str] = set()

MY_SECRET_TOKEN = "ChiaChun_Super_Secret_888"

# =============================
# 📥 接收資料（本機會打這裡）
# =============================
@app.post("/update")
def update_data(data: dict, authorization: str = Header(None)):
    # 🌟 檢查通關密語！如果不對，直接踢掉！
    if authorization != f"Bearer {MY_SECRET_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized: 滾開！")

    symbol = data.get("symbol")
    if symbol:
        market_data[symbol] = data
        # 🌟 如果這檔股票原本在許願池裡，代表本機已經算好上傳了，將它移出名單
        if symbol in wishlist:
            wishlist.remove(symbol)
    return {"status": "ok"}

# =============================
# 📊 單檔分析（GPT 呼叫這裡，升級長輪詢版）
# =============================
@app.get("/analysis-input/{symbol}")
async def get_analysis(symbol: str):  # 🌟 注意這裡加上了 async
    # 狀況一：資料已經在雲端了，光速秒答
    if symbol in market_data:
        return market_data[symbol]
    
    # 狀況二：全新標的，加入許願池
    wishlist.add(symbol)
    
    # 🌟 雲端開始憋氣等待 (最多等 10 秒)
    for _ in range(10):
        await asyncio.sleep(1) # 等待 1 秒
        if symbol in market_data:
            # 你的 Mac 成功把資料傳上來了！立刻回傳給 GPT
            if symbol in wishlist:
                wishlist.remove(symbol)
            return market_data[symbol]
            
    # 狀況三：等了 5 秒都沒拿到資料 (可能是你 Mac 上的雷達沒開)
    return {
        "status": "pending", 
        "error": "data_missing",
        "message": f"⚠️ 標的 {symbol} 不在名單內，且本機雷達未在 10 秒內回應。請確認 uploader.py 是否運作中！"
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
