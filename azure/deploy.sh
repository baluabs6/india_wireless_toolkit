#!/usr/bin/env bash
# azure/deploy.sh
#
# End-to-end deploy: provisions Azure resources (via main.bicep), builds both
# service images, pushes them to the new ACR tagged with the git commit SHA,
# points each Web App at that exact image, waits for /health, and ROLLS BACK
# to the previous image if the new one does not become healthy. Idempotent.
#
# Prerequisites: az cli (logged in: `az login`), docker, git, curl.
#
# Usage:
#   RESOURCE_GROUP=india-wireless-rg APP_PREFIX=iwtoolkit LOCATION=centralindia \
#   API_KEY=<long-random-string> ./azure/deploy.sh

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?Set RESOURCE_GROUP, e.g. RESOURCE_GROUP=india-wireless-rg}"
APP_PREFIX="${APP_PREFIX:?Set APP_PREFIX, e.g. APP_PREFIX=iwtoolkit (lowercase alphanumeric, globally-unique-ish)}"
API_KEY="${API_KEY:?Set API_KEY (clients send it as X-API-Key). Generate one with: openssl rand -hex 32}"
LOCATION="${LOCATION:-centralindia}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

echo "==> Ensuring resource group '$RESOURCE_GROUP' exists in $LOCATION"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (azure/main.bicep)"
DEPLOY_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$REPO_ROOT/azure/main.bicep" \
  --parameters appNamePrefix="$APP_PREFIX" location="$LOCATION" apiKey="$API_KEY" \
  --query properties.outputs -o json)

out() { echo "$DEPLOY_OUTPUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['$1']['value'])"; }
ACR_LOGIN_SERVER=$(out acrLoginServer)
ANALYTICS_URL=$(out analyticsApiUrl)
NEWS_URL=$(out newsApiUrl)

echo "==> ACR: $ACR_LOGIN_SERVER   image tag: $TAG"
az acr login --name "${ACR_LOGIN_SERVER%%.*}"

build_push() {  # dockerfile image
  docker build -f "$REPO_ROOT/$1" -t "$ACR_LOGIN_SERVER/$2:$TAG" -t "$ACR_LOGIN_SERVER/$2:latest" "$REPO_ROOT"
  docker push "$ACR_LOGIN_SERVER/$2:$TAG"
  docker push "$ACR_LOGIN_SERVER/$2:latest"
}
echo "==> Building + pushing images"
build_push Dockerfile.sanic india-wireless-analytics-api
build_push Dockerfile.blacksheep india-wireless-news-api

deploy_and_verify() {  # app-name image url
  local app="$1" image="$2" url="$3" previous
  previous=$(az webapp config show -g "$RESOURCE_GROUP" -n "$app" --query linuxFxVersion -o tsv)
  echo "==> $app: $previous  ->  DOCKER|$ACR_LOGIN_SERVER/$image:$TAG"
  az webapp config set -g "$RESOURCE_GROUP" -n "$app" \
    --linux-fx-version "DOCKER|$ACR_LOGIN_SERVER/$image:$TAG" --output none
  az webapp restart -g "$RESOURCE_GROUP" -n "$app" --output none
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 10 "$url/health" >/dev/null 2>&1; then
      echo "    healthy after $attempt check(s)"; return 0
    fi
    sleep 10
  done
  echo "!!  $app did not become healthy; rolling back to $previous" >&2
  az webapp config set -g "$RESOURCE_GROUP" -n "$app" --linux-fx-version "$previous" --output none
  az webapp restart -g "$RESOURCE_GROUP" -n "$app" --output none
  return 1
}

deploy_and_verify "${APP_PREFIX}-analytics-api" india-wireless-analytics-api "$ANALYTICS_URL"
deploy_and_verify "${APP_PREFIX}-news-api" india-wireless-news-api "$NEWS_URL"

cat <<EOF2

==> Done (image tag $TAG).
    Analytics API (Sanic):        $ANALYTICS_URL
    News/Report API (Blacksheep): $NEWS_URL

Call protected routes with:  curl -H "X-API-Key: \$API_KEY" $ANALYTICS_URL/infra/npv?n_isps=3

Next steps:
  - Set alert/agent secrets on the news API (optional; prefer Key Vault references in production):
      az webapp config appsettings set --resource-group "$RESOURCE_GROUP" \\
        --name "${APP_PREFIX}-news-api" \\
        --settings ALERT_WEBHOOK_URL="<slack webhook>" SMTP_HOST="<host>" \\
                   SMTP_USER="<user>" SMTP_PASSWORD="<password>" \\
                   SMTP_FROM_ADDR="<from>" SMTP_TO_ADDR="<to>" ANTHROPIC_API_KEY="<key>"
  - Logs and telemetry: Application Insights '${APP_PREFIX}-insights', or
      az webapp log tail --resource-group "$RESOURCE_GROUP" --name "${APP_PREFIX}-analytics-api"
EOF2
