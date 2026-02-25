# 地理設施調查 v3.7 網頁

輸入**座標**或**地址**，依 v3.7 規則查詢範圍內**生活機能**與**嫌惡設施**。

## 需求

- Python 3.8+
- **goplaces** CLI（見下方安裝）
- **API 金鑰**：沿用 OpenClaw，讀取 `~/.openclaw/openclaw.json` 內 `skills.entries.goplaces.apiKey`

## 安裝 goplaces

```bash
# Homebrew（推薦）
brew install steipete/tap/goplaces

# 或 Go
go install github.com/steipete/goplaces/cmd/goplaces@latest
```

## 執行

```bash
cd 房仲工具/web
pip install -r requirements.txt
./run.sh
```

`run.sh` 會自動從 OpenClaw 設定讀取 API 金鑰並啟動。或手動：

```bash
python app.py
```

（後端會自動讀取 `~/.openclaw/openclaw.json` 的 goplaces apiKey，無需另設環境變數）

瀏覽器開啟：http://127.0.0.1:5000

## API

- `POST /api/search`  
  Body: `{ "address": "台東市中山路123號" }` 或 `{ "lat": 22.76, "lng": 121.15 }`  
  可選 `"radius_m": 350`（預設 350）  
  回傳：設施列表、生活機能／嫌惡／宗教區塊、評分、類別統計、覆蓋度警告。
