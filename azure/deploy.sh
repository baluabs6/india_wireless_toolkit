#!/usr/bin/env bash
# azure/deploy.sh
#
# End-to-end deploy: provisions Azure resources (via main.bicep), builds
# both service images, pushes them to the new ACR, and points each Web
# App at its image. Idempotent — safe to re-run.
#
# Prerequisites: az cli (logged in: `az login`), docker.
#
# Usage:
#   RESOURCE_GROUP=india-wireless-rg APP_PREFIX=iwtoolkit LOCATION=centralindia ./azure/deploy.sh

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?Set RESOURCE_GROUP, e.g. RESOURCE_GROUP=india-wireless-rg}"
APP_PREFIX="${APP_PREFIX:?Set APP_PREFIX, e.g. APP_PREFIX=iwtoolkit (lowercase alphanumeric, globally-unique-ish)}"
LOCATION="${LOCATION:-centralindia}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Ensuring resource group '$RESOURCE_GROUP' exists in $LOCATION"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (azure/main.bicep)"
DEPLOY_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$REPO_ROOT/azure/main.bicep" \
  --parameters appNamePrefix="$APP_PREFIX" location="$LOCATION" \
  --query properties.outputs -o json)

ACR_LOGIN_SERVER=$(echo "$DEPLOY_OUTPUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['acrLoginServer']['value'])")
ANALYTICS_URL=$(echo "$DEPLOY_OUTPUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['analyticsApiUrl']['value'])")
NEWS_URL=$(echo "$DEPLOY_OUTPUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['newsApiUrl']['value'])")

echo "==> ACR: $ACR_LOGIN_SERVER"
az acr login --name "${ACR_LOGIN_SERVER%%.*}"

echo "==> Building + pushing analytics-api (Sanic) image"
docker build -f "$REPO_ROOT/Dockerfile.sanic" -t "$ACR_LOGIN_SERVER/india-wireless-analytics-api:latest" "$REPO_ROOT"
docker push "$ACR_LOGIN_SERVER/india-wireless-analytics-api:latest"

echo "==> Building + pushing news-api (Blacksheep) image"
docker build -f "$REPO_ROOT/Dockerfile.blacksheep" -t "$ACR_LOGIN_SERVER/india-wireless-news-api:latest" "$REPO_ROOT"
docker push "$ACR_LOGIN_SERVER/india-wireless-news-api:latest"

echo "==> Restarting Web Apps to pick up the freshly-pushed images"
az webapp restart --resource-group "$RESOURCE_GROUP" --name "${APP_PREFIX}-analytics-api" --output none
az webapp restart --resource-group "$RESOURCE_GROUP" --name "${APP_PREFIX}-news-api" --output none

cat <<EOF

==> Done.
    Analytics API (Sanic):    $ANALYTICS_URL
    News/Report API (Blacksheep): $NEWS_URL

Next steps:
  - Set alert secrets on the news API (optional):
      az webapp config appsettings set --resource-group "$RESOURCE_GROUP" \\
        --name "${APP_PREFIX}-news-api" \\
        --settings ALERT_WEBHOOK_URL="<slack webhook>" SMTP_HOST="<host>" \\
                   SMTP_USER="<user>" SMTP_PASSWORD="<password>" \\
                   SMTP_FROM_ADDR="<from>" SMTP_TO_ADDR="<to>"
  - Tail logs:
      az webapp log tail --resource-group "$RESOURCE_GROUP" --name "${APP_PREFIX}-analytics-api"
      az webapp log tail --resource-group "$RESOURCE_GROUP" --name "${APP_PREFIX}-news-api"
EOF
