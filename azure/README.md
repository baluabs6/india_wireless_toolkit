# Deploying to Azure

This deploys the toolkit as **two independent services**, each its own
Azure Web App for Containers:

| Service | Framework | Azure resource | Default port |
|---|---|---|---|
| Analytics API (charts, spectrum sim, infra economics) | Sanic | `<prefix>-analytics-api` | 8001 |
| News/Report API (scraper, alerts, HTML report) | Blacksheep | `<prefix>-news-api` | 8002 |

Shared infrastructure: one **Azure Container Registry**, one **Azure Cache
for Redis** (TLS), and one **Storage Account + Azure File Share** mounted
into both Web Apps at `/mnt/toolkit-data` for the SQLite news archive,
generated charts, and HTML report — container filesystems are ephemeral,
so this is what makes data survive restarts/redeploys.

## One-time setup

```bash
az login
az account set --subscription <subscription-id>
```

## Option A: one-shot script

```bash
RESOURCE_GROUP=india-wireless-rg APP_PREFIX=iwtoolkit LOCATION=centralindia \
  ./azure/deploy.sh
```

This creates the resource group (if needed), applies `main.bicep`, builds
both Docker images, pushes them to the new ACR, and restarts the Web Apps.
Re-running it is safe — it picks up code changes and redeploys.

## Option B: step by step

```bash
# 1. Provision infrastructure
az group create --name india-wireless-rg --location centralindia
az deployment group create \
  --resource-group india-wireless-rg \
  --template-file azure/main.bicep \
  --parameters appNamePrefix=iwtoolkit

# 2. Build & push images
ACR=$(az acr list --resource-group india-wireless-rg --query "[0].loginServer" -o tsv)
az acr login --name "${ACR%%.*}"
docker build -f Dockerfile.sanic -t "$ACR/india-wireless-analytics-api:latest" .
docker push "$ACR/india-wireless-analytics-api:latest"
docker build -f Dockerfile.blacksheep -t "$ACR/india-wireless-news-api:latest" .
docker push "$ACR/india-wireless-news-api:latest"

# 3. Restart the Web Apps so they pull the images
az webapp restart --resource-group india-wireless-rg --name iwtoolkit-analytics-api
az webapp restart --resource-group india-wireless-rg --name iwtoolkit-news-api
```

## Set alert secrets (optional)

Slack/email alerting on the news API needs its secrets set as App Settings
— **never** put these in `config.yaml` or the Bicep template:

```bash
az webapp config appsettings set \
  --resource-group india-wireless-rg --name iwtoolkit-news-api \
  --settings \
    ALERT_WEBHOOK_URL="https://hooks.slack.com/services/..." \
    SMTP_HOST="smtp.example.com" SMTP_PORT="587" \
    SMTP_USER="alerts@example.com" SMTP_PASSWORD="..." \
    SMTP_FROM_ADDR="alerts@example.com" SMTP_TO_ADDR="team@example.com"
```

These map to the same env vars `config_loader.py` reads locally via
`.env` — see `.env.example`.

## CI/CD (GitHub Actions)

`.github/workflows/azure-deploy.yml` rebuilds and redeploys both images on
every push to `main`. It needs these repo secrets:

| Secret | How to get it |
|---|---|
| `AZURE_CREDENTIALS` | `az ad sp create-for-rbac --name "iwtoolkit-gha" --role contributor --scopes /subscriptions/<sub-id>/resourceGroups/<rg> --sdk-auth` |
| `ACR_LOGIN_SERVER` | Bicep output `acrLoginServer`, or `az acr list -o table` |
| `AZURE_RESOURCE_GROUP` | e.g. `india-wireless-rg` |
| `AZURE_APP_PREFIX` | must match `appNamePrefix` used in `main.bicep`, e.g. `iwtoolkit` |

The workflow only builds/pushes app code — it doesn't re-run the Bicep
deployment, so infrastructure changes still go through `deploy.sh` or
Option B above.

## Verify

```bash
curl https://iwtoolkit-analytics-api.azurewebsites.net/health
curl https://iwtoolkit-news-api.azurewebsites.net/health
```

## Local development

`docker compose up --build` runs both services + a local (non-TLS) Redis
container, so you don't need any Azure resources to develop against —
`REDIS_SSL=false` locally, `REDIS_SSL=true` in Azure, same code path either
way (see `india_wireless_toolkit/db.py`).

## Cost notes

- App Service Plan `B1` and Redis `Basic C0` are the cheapest tiers meant
  for dev/test — for production traffic, move to `P1v3`+/`Standard`+.
- The Storage Account + File Share cost is minimal (pay for what's stored;
  charts/reports/SQLite archive here are tiny).
