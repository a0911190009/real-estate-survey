#!/bin/bash
# 使用 OpenClaw 的 API 金鑰啟動地理設施調查網頁（v3.7）
cd "$(dirname "$0")"

# 從 ~/.openclaw/openclaw.json 讀取 goplaces apiKey 並 export，供 goplaces CLI 使用
OC_JSON="${HOME}/.openclaw/openclaw.json"
if [ -f "$OC_JSON" ]; then
  export GOOGLE_PLACES_API_KEY=$(python3 -c "
import json
try:
    with open('$OC_JSON') as f:
        d = json.load(f)
    print(d.get('skills', {}).get('entries', {}).get('goplaces', {}).get('apiKey', '') or '')
except Exception:
    print('')
")
fi

if [ -z "$GOOGLE_PLACES_API_KEY" ]; then
  echo "⚠️  未找到 API 金鑰，請確認 ~/.openclaw/openclaw.json 內 skills.entries.goplaces.apiKey 已設定"
fi

echo "啟動網頁：http://127.0.0.1:5000"
exec python3 app.py
