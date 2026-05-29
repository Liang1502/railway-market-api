# Railway Market API 專案說明書

## 專案目的

`railway-market-api` 是台股盤中當沖輔助工具，負責把本機富邦行情資料整理後上傳到 FastAPI 服務，提供單檔查詢、批次查詢、市場掃描、持久追蹤清單與部位監看所需資料。

此專案本身不下單，主要用途是：

- 盤中接收 `uploader.py` 送來的即時分析資料。
- 讓 GPT Actions、CLI 工具或其他客戶端查詢單檔與批次分析資料。
- 依多空條件產生即時市場掃描清單。
- 管理當沖部位追蹤、停損停利提示與交易紀錄。
- 定期排除不能現沖或處置中的標的。

## 系統架構

```text
富邦 SDK / 即時行情
        |
        v
uploader.py
        |
        v
FastAPI 服務 main.py
        |
        +--> /analysis-input/{symbol}  單檔分析資料
        +--> /analysis-batch           批次分析資料
        +--> /scan                     多空候選掃描
        +--> /watch-list               持久追蹤清單
        +--> /wishlist                 一次性資料請求池
        +--> /health                   健康檢查
        |
        +--> watch.py                  部位追蹤與警報
        +--> radar.py                  盤中雷達掃描
        +--> GPT Actions / 外部客戶端
```

## 主要檔案

| 檔案 | 說明 |
| --- | --- |
| `main.py` | FastAPI 入口，管理快取、查詢、掃描、追蹤清單與健康檢查。 |
| `uploader.py` | 登入富邦 SDK，取得行情、計算即時指標與 V6 評分，送到 `/update`。 |
| `analysis.py` | 從 ticker / quote 抽取五檔、價格、量能、結構與進場訊號。 |
| `watch.py` | 當沖部位管理與即時監看，支援新增、更新、移除、結算與歷史紀錄。 |
| `radar.py` | 定期呼叫 `/scan`，輸出多空候選清單。 |
| `disposition.py` | 從證交所與櫃買中心更新處置股 / 不適合現沖排除清單。 |
| `backtest.py` | 轉呼叫新版 `stock_scanner/run.py backtest`，保留舊指令相容性。 |
| `gpt_actions_openapi.json` | GPT Actions 使用的 OpenAPI 定義。 |
| `launchd/` | macOS launchd 自動啟動與排程設定。 |
| `scripts/` | 健康檢查與 log 清理腳本。 |
| `tests/` | 單元測試與策略邏輯測試。 |

## 執行環境

- Python 3
- FastAPI / Uvicorn
- 富邦證券 SDK `fubon_neo`
- 可連線到 Railway API 或本機 API
- macOS launchd 為選用，用於本機常駐 uploader、健康檢查與 log 清理

安裝 Python 套件：

```bash
pip install -r requirements.txt
```

注意：`requirements.txt` 中沒有包含富邦 SDK，需依富邦官方方式另外安裝 `fubon_neo`。

## 環境變數

請在 `.env` 或部署環境設定以下變數：

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `API_SECRET_TOKEN` | 是 | `/update` 與刪除 watch-list 時使用的 Bearer token。 |
| `RAILWAY_API_URL` | 視情境 | API base URL，預設 `http://127.0.0.1:8000`。 |
| `PORT` | 否 | FastAPI 啟動 port，預設 `8000`。 |
| `STALE_SECS` | 否 | 快取資料超過幾秒視為過期，預設 `180`。 |
| `DISPOSITION_REFRESH_SECS` | 否 | 處置股排除清單刷新間隔，預設 `3600`。 |
| `DISPOSITION_REFRESH_TIMEOUT` | 否 | 官方處置股 API timeout，預設 `8` 秒。 |
| `FUBON_ACCOUNT` | uploader 需要 | 富邦帳號。 |
| `FUBON_PASSWORD` | uploader 需要 | 富邦密碼。 |
| `FUBON_CERT_PATH` | uploader 需要 | 憑證路徑。 |
| `FUBON_CERT_PASSWORD` | uploader 需要 | 憑證密碼。 |
| `TG_BOT_TOKEN` | 選用 | Telegram Bot token，用於 watch / health check 告警。 |
| `TG_CHAT_ID` | 選用 | Telegram chat id。 |
| `STOCK_SCANNER_DIR` | backtest 選用 | 新版 stock scanner 專案路徑，預設 `/Users/chiachun/Desktop/stock_scanner`。 |

## 啟動方式

啟動 API：

```bash
make serve
```

或指定 port：

```bash
PORT=8000 python3 main.py
```

啟動行情上傳器：

```bash
make uploader
```

啟動部位監看：

```bash
make watch
```

執行測試：

```bash
make test
```

執行回測轉接：

```bash
make backtest
```

## API 端點

### `POST /update`

接收 `uploader.py` 上傳的單檔分析資料。

- 需要 header：`Authorization: Bearer <API_SECRET_TOKEN>`
- body 需包含 `symbol`
- 服務會補上 `_server_ts` 並寫入記憶體快取

### `GET /analysis-input/{symbol}`

查詢單一股票分析資料。

- 若快取新鮮，直接回傳。
- 若資料過期，會把 symbol 加入 `wishlist` 並最多等待 10 秒。
- 若仍無新資料，回傳 stale 標記或 pending 狀態。

### `GET /analysis-batch?symbols=2330,2454`

批次查詢多檔分析資料。

- 所有查詢 symbol 會加入持久追蹤清單 `watch_set`。
- 若資料缺失或過期，會加入 `wishlist` 等待 uploader 補資料。
- 最多等待 5 秒後回傳結果。

### `GET /wishlist`

回傳一次性待補資料清單，供 `uploader.py` 拉取並補送資料。

### `GET /watch-list`

回傳持久追蹤清單，供 `uploader.py` 定期訂閱或補抓。

### `DELETE /watch-list/{symbol}`

從持久追蹤清單移除 symbol。

- 需要 header：`Authorization: Bearer <API_SECRET_TOKEN>`

### `GET /scan`

掃描目前快取中的新鮮資料，回傳多空候選與觸發清單。

主要邏輯：

- 忽略過期資料。
- 忽略無 `decision` 的資料。
- 排除處置股 / 不能現沖標的。
- 空方候選依 `v6_score`、壓力比與空方進場條件篩選。
- 多方候選依多方進場條件與漲幅限制篩選。

### `GET /health`

健康檢查，回傳 API 狀態、快取檔數與追蹤清單數量。

## 部位追蹤操作

新增部位：

```bash
python3 watch.py add 2330 buy 550
python3 watch.py add 2454 sell 120
```

自訂停損：

```bash
python3 watch.py add 2330 buy 550 539
```

更新停損 / 停利：

```bash
python3 watch.py update 2330 stop 545
python3 watch.py update 2330 t1 562
python3 watch.py update 2330 t2 567
```

出場並記錄損益：

```bash
python3 watch.py close 2330 556
```

移除部位但不記錄損益：

```bash
python3 watch.py remove 2330
```

查看部位：

```bash
python3 watch.py list
```

查看交易紀錄：

```bash
python3 watch.py history
python3 watch.py history 20
```

指定監看刷新間隔：

```bash
python3 watch.py --interval 5
```

## 本機資料檔

| 檔案 | 說明 |
| --- | --- |
| `watch_set.json` | 持久追蹤 symbol 清單。 |
| `positions.json` | 目前追蹤中的部位。 |
| `trades.json` | 已結算交易紀錄。 |
| `daytrade_exclusions.json` | 不能現沖 / 處置股排除清單。 |
| `log/` | 本機 log 目錄。 |

## macOS launchd

`launchd/` 內提供三個設定：

- `com.chiachun.railway.uploader.plist`：常駐執行 `uploader.py`，異常時自動重啟。
- `com.chiachun.railway.health_check.plist`：每 5 分鐘檢查 `/health`，失敗時發 Telegram 通知。
- `com.chiachun.railway.rotate_logs.plist`：每天 04:00 清理舊 log。

使用前請確認 plist 內的 Python 路徑、專案路徑與 log 路徑符合目前機器設定。

## 測試

```bash
pytest -q
```

測試涵蓋重點包含：

- 當沖分析資料抽取。
- 回測引擎與轉接行為。
- 處置股排除清單。
- uploader 評分邏輯。
- watch 策略提示。
- API 快取新鮮度處理。

## 注意事項

- API 的 `market_data` 是記憶體快取，服務重啟後會清空。
- `watch_set.json`、`positions.json`、`trades.json` 是本機狀態檔，部署與備份時需自行保護。
- 本專案只提供分析與提醒，不保證交易績效，也不會自動下單。
- 台股處置股 / 不能現沖清單會嘗試從官方資料更新；更新失敗時會沿用本地檔案。
- 使用真實帳號與憑證時，請避免把 `.env`、憑證與 log 中的敏感資訊提交到版本庫。
