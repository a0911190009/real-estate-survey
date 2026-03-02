#!/bin/bash
# 依序部署 Portal、Survey、AD 到 Cloud Run，並設定 SERVICE_API_KEY 與 PORTAL_URL
# 使用方式：在終端機執行 ./deploy-all.sh
# 請先確認已登入 gcloud（gcloud auth login）且專案正確（gcloud config set project YOUR_PROJECT_ID）

set -e
PROJECTS_ROOT="/Users/chenweiliang/Projects"
REGION="asia-east1"
DEPLOY_TAG="v$(date +%Y%m%d%H%M)"
echo "部署標籤：$DEPLOY_TAG"

# 從 .env 讀取（若沒有則用預設）
ENV_FILE="$PROJECTS_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE" 2>/dev/null || true
  set +a
fi
if [ -z "${SERVICE_API_KEY}" ]; then
  echo "❌ 錯誤：SERVICE_API_KEY 未設定。請在 .env 中設定或 export SERVICE_API_KEY=..." >&2
  exit 1
fi
PORTAL_URL="${PORTAL_URL:-https://real-estate-portal-334765337861.asia-east1.run.app}"
SURVEY_URL="${SURVEY_URL:-https://real-estate-survey-334765337861.asia-east1.run.app}"
AD_URL="${AD_URL:-https://real-estate-ad-334765337861.asia-east1.run.app}"
# 若 .env 把 URL 設成空字串，仍用預設，避免 Portal 進不去 Survey/AD
[ -z "$SURVEY_URL" ] && SURVEY_URL="https://real-estate-survey-334765337861.asia-east1.run.app"
[ -z "$AD_URL" ] && AD_URL="https://real-estate-ad-334765337861.asia-east1.run.app"
[ -z "$PORTAL_URL" ] && PORTAL_URL="https://real-estate-portal-334765337861.asia-east1.run.app"
LIBRARY_URL="${LIBRARY_URL:-}"
[ -z "$LIBRARY_URL" ] && LIBRARY_URL="https://real-estate-library-334765337861.asia-east1.run.app"
FREE_LIBRARY_LIMIT="${FREE_LIBRARY_LIMIT:-3}"
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"
FLASK_SECRET_KEY="${FLASK_SECRET_KEY:-}"
FREE_POINTS="${FREE_POINTS:-10}"
GOOGLE_API_KEY="${GOOGLE_API_KEY:-}"
GOOGLE_MAPS_API_KEY="${GOOGLE_MAPS_API_KEY:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
GROK_API_KEY="${GROK_API_KEY:-}"
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}"
TONGYI_API_KEY="${TONGYI_API_KEY:-}"
SURVEY_API_URL="${SURVEY_API_URL:-$SURVEY_URL}"
GCS_BUCKET="${GCS_BUCKET:-}"
PORTAL_TITLE="${PORTAL_TITLE:-房仲 AI 工具平台}"
# 還原版 Portal：物件庫在 GCS 的前綴（可選，預設 properties）
PROPERTIES_GCS_PREFIX="${PROPERTIES_GCS_PREFIX:-properties}"
NEWEBPAY_MERCHANT_ID="${NEWEBPAY_MERCHANT_ID:-}"
NEWEBPAY_HASH_KEY="${NEWEBPAY_HASH_KEY:-}"
NEWEBPAY_HASH_IV="${NEWEBPAY_HASH_IV:-}"
NEWEBPAY_SANDBOX="${NEWEBPAY_SANDBOX:-0}"

if [ -z "$GCS_BUCKET" ]; then
  echo "⚠️  警告：GCS_BUCKET 未設定。歷史與物件資料將存在容器內，每次改版部署後都會清空。"
  echo "    若需永久保留歷史，請在 .env 中設定 GCS_BUCKET（例如：real-estate-survey-data-0393195862）。"
  echo ""
fi
# 還原版 Portal 在生產環境必須設定 FLASK_SECRET_KEY
if [ -z "$FLASK_SECRET_KEY" ]; then
  echo "⚠️  警告：FLASK_SECRET_KEY 未設定。還原版 Portal 在生產環境會拒絕啟動，請在 .env 設定後再部署。"
  echo ""
fi

echo "使用 REGION=$REGION"
echo "PORTAL_URL=$PORTAL_URL"
echo "SURVEY_URL=$SURVEY_URL"
echo "AD_URL=$AD_URL"
echo "LIBRARY_URL=$LIBRARY_URL"
echo "SERVICE_API_KEY 已設定（長度 ${#SERVICE_API_KEY}）"
echo ""

# 1. Portal（點數制：FREE_POINTS=新用戶預設點數，PROPERTIES_GCS_PREFIX=物件庫在 GCS 前綴）
echo "========== 部署 Portal =========="
cd "$PROJECTS_ROOT/real-estate-portal"
gcloud run deploy real-estate-portal --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL,SURVEY_URL=$SURVEY_URL,AD_URL=$AD_URL,LIBRARY_URL=$LIBRARY_URL,GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID,ADMIN_EMAILS=$ADMIN_EMAILS,FLASK_SECRET_KEY=$FLASK_SECRET_KEY,FREE_POINTS=$FREE_POINTS,FREE_LIBRARY_LIMIT=$FREE_LIBRARY_LIMIT,GCS_BUCKET=$GCS_BUCKET,PORTAL_TITLE=$PORTAL_TITLE,PROPERTIES_GCS_PREFIX=$PROPERTIES_GCS_PREFIX,NEWEBPAY_MERCHANT_ID=$NEWEBPAY_MERCHANT_ID,NEWEBPAY_HASH_KEY=$NEWEBPAY_HASH_KEY,NEWEBPAY_HASH_IV=$NEWEBPAY_HASH_IV,NEWEBPAY_SANDBOX=$NEWEBPAY_SANDBOX" \
  --quiet
echo "Portal 部署完成"
echo ""

# 2. Survey
echo "========== 部署 Survey =========="
cd "$PROJECTS_ROOT/real-estate-survey"
gcloud run deploy real-estate-survey --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL,GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID,ADMIN_EMAILS=$ADMIN_EMAILS,FLASK_SECRET_KEY=$FLASK_SECRET_KEY,GOOGLE_API_KEY=$GOOGLE_API_KEY,GOOGLE_MAPS_API_KEY=$GOOGLE_MAPS_API_KEY,GCS_BUCKET=$GCS_BUCKET" \
  --quiet
echo "Survey 部署完成"
echo ""

# 3. AD
echo "========== 部署 AD =========="
cd "$PROJECTS_ROOT/real-estate-ad"
gcloud run deploy real-estate-ad --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL,GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID,ADMIN_EMAILS=$ADMIN_EMAILS,FLASK_SECRET_KEY=$FLASK_SECRET_KEY,GOOGLE_API_KEY=$GOOGLE_API_KEY,OPENAI_API_KEY=$OPENAI_API_KEY,GROK_API_KEY=$GROK_API_KEY,DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY,ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY,DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY,TONGYI_API_KEY=$TONGYI_API_KEY,SURVEY_API_URL=$SURVEY_API_URL,GCS_BUCKET=$GCS_BUCKET,LIBRARY_URL=$LIBRARY_URL" \
  --quiet
echo "AD 部署完成"
echo ""

# 4. 物件庫
echo "========== 部署 real-estate-library =========="
cd "$PROJECTS_ROOT/real-estate-library"
gcloud run deploy real-estate-library --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL,FLASK_SECRET_KEY=$FLASK_SECRET_KEY,ADMIN_EMAILS=$ADMIN_EMAILS,GCS_BUCKET=$GCS_BUCKET" \
  --quiet
echo "real-estate-library 部署完成"
echo ""

echo "========== 全部部署完成（$DEPLOY_TAG） =========="
echo "請到 Cloud Run 主控台確認三個服務的環境變數（必要時補上 FLASK_SECRET_KEY、GOOGLE_OAUTH_CLIENT_ID 等）。"
if [ -z "$GCS_BUCKET" ]; then
  echo ""
  echo "⚠️  提醒：本次未設定 GCS_BUCKET，歷史與物件資料在下次改版後會清空。請在 .env 設定 GCS_BUCKET 後重新部署。"
fi
echo ""
echo "若需回滾，可使用以下指令："
echo "  gcloud run services update-traffic <SERVICE> --to-revisions=<REVISION>=100 --region $REGION"
echo "  或到 Cloud Run 主控台 → 修訂版本 → 管理流量"
