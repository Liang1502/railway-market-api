from fastapi import FastAPI
from typing import Dict

app = FastAPI()

# 🔥 用來存市場資料（記憶體）
market_data: Dict[str, dict] = {}

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

        if not data.get("analysis_ready"):
            continue

        score = data.get("score", 50)
        decision = data.get("decision")
        signal = data.get("signal", {})

        # 空方
        if decision in ["short_possible", "avoid_long"] or signal.get("fake_breakout_risk"):
            short_list.append(data)

        # 多方
        if decision in ["long_possible", "avoid_short"] or signal.get("fake_breakdown_risk"):
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
