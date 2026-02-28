#!/bin/bash
# 依序部署 Portal、Survey、AD 到 Cloud Run，並設定 SERVICE_API_KEY 與 PORTAL_URL
# 使用方式：在終端機執行 ./deploy-all.sh
# 請先確認已登入 gcloud（gcloud auth login）且專案正確（gcloud config set project YOUR_PROJECT_ID）

set -e
PROJECTS_ROOT="/Users/chenweiliang/Projects"
REGION="asia-east1"

# 從 .env 讀取（若沒有則用預設）
ENV_FILE="$PROJECTS_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE" 2>/dev/null || true
fi
SERVICE_API_KEY="${SERVICE_API_KEY:-pKg0ICqr1Jy1udrVYZQArfE0w0YxOlyWGH355GPvSlY}"
PORTAL_URL="${PORTAL_URL:-https://real-estate-portal-334765337861.asia-east1.run.app}"

echo "使用 REGION=$REGION"
echo "PORTAL_URL=$PORTAL_URL"
echo "SERVICE_API_KEY 已設定（長度 ${#SERVICE_API_KEY}）"
echo ""

# 1. Portal
echo "========== 部署 Portal =========="
cd "$PROJECTS_ROOT/real-estate-portal"
gcloud run deploy real-estate-portal --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY" \
  --quiet
echo "Portal 部署完成"
echo ""

# 2. Survey
echo "========== 部署 Survey =========="
cd "$PROJECTS_ROOT/real-estate-survey"
gcloud run deploy real-estate-survey --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL" \
  --quiet
echo "Survey 部署完成"
echo ""

# 3. AD
echo "========== 部署 AD =========="
cd "$PROJECTS_ROOT/real-estate-ad"
gcloud run deploy real-estate-ad --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "SERVICE_API_KEY=$SERVICE_API_KEY,PORTAL_URL=$PORTAL_URL" \
  --quiet
echo "AD 部署完成"
echo ""

echo "========== 全部部署完成 =========="
echo "請到 Cloud Run 主控台確認三個服務的環境變數（必要時補上 FLASK_SECRET_KEY、GOOGLE_OAUTH_CLIENT_ID 等）。"
