import time
import requests

# 👉 你的 Railway 雲端主機掃描端點
API_URL = "https://web-production-641b.up.railway.app/scan"

# 👉 掃描間隔（秒）
INTERVAL = 10

last_top = None  # ⭐ 記錄上一輪結果

def print_candidates(data):
    print("\n==============================")
    print("📡 盤中即時雷達掃描結果")
    print("==============================")

    # 🔻 空方
    short_list = data.get("top_short", [])
    if short_list:
        print("\n🔻 空方機會 (Top 3)：")
        for i, s in enumerate(short_list):
            tag = "⭐" if i == 0 else "  "
            print(f"{tag} {s['symbol']} ｜ 分數: {s['score']} ｜ 狀態: {s['decision']}")

    # 🔺 多方
    long_list = data.get("top_long", [])
    if long_list:
        print("\n🔺 多方機會 (Top 3)：")
        for i, s in enumerate(long_list):
            tag = "⭐" if i == 0 else "  "
            print(f"{tag} {s['symbol']} ｜ 分數: {s['score']} ｜ 狀態: {s['decision']}")

    print("\n==============================\n")

def main():
    global last_top

    print("🚀 啟動盤中監控儀表板（Ctrl+C 停止）")

    error_streak = 0
    while True:
        try:
            res = requests.get(API_URL, timeout=5)
            if res.status_code == 200:
                error_streak = 0
                data = res.json()

                current_top = str(data.get("top_short", [])[:1]) + str(data.get("top_long", [])[:1])
                if current_top != last_top:
                    print_candidates(data)
                    last_top = current_top
            else:
                error_streak += 1
                if error_streak >= 3:
                    print(f"[WARN] radar: API 連續 {error_streak} 次異常回應 {res.status_code}")

        except Exception as e:
            error_streak += 1
            if error_streak >= 3:
                print(f"[WARN] radar: 連線失敗 {error_streak} 次 — {e}")

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
