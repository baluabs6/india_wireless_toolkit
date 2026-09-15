# india-wireless-toolkit

## About the Application

**india-wireless-toolkit** is a research toolkit for exploring current
issues in India's wireless/telecom sector: satcom spectrum delays, the
6 GHz Wi-Fi vs 5G/6G band split, last-mile fibre duplication economics,
and regulatory activity from TRAI, DoT, and PIB.

The core analysis logic is exposed as two independently-deployable web
APIs (plus the original CLI, still available for local/offline use):

- **Analytics API** (Sanic) — charts, spectrum simulation, and
  infrastructure economics as JSON/image endpoints.
- **News/Report API** (Blacksheep) — the regulatory-news scraper/archive,
  keyword-trend charts, Slack/email alerting, and the combined HTML
  report.

Splitting the two lets them be built, deployed, and scaled independently
— the analytics side is CPU-bound and read-heavy, while the news side is
I/O-bound (scraping, SQLite, SMTP/webhooks) and benefits from separate
resource limits and restart cycles.

- **Data visualization** — static charts (broadband split, teledensity
  gap, 6 GHz split, public Wi-Fi gap), a subscriber-trend time series, an
  interactive Plotly dashboard with a 6 GHz "what-if" slider, and a
  state-wise bubble map.
- **Spectrum simulation** — channel-capacity modeling, a log-distance
  path-loss/SINR model, Monte Carlo AP-placement interference simulation,
  illustrative 6G terahertz-band modeling, and custom "what-if" MHz
  scenarios.
- **Infrastructure economics** — duplicate vs. shared-neutral last-mile
  cost modeling, NPV/IRR analysis, sensitivity/tornado charts, a
  three-way FTTH vs. FWA vs. Satellite comparison, and break-even
  ISP-count calculations.
- **Regulatory news tracking** — a TRAI/DoT/PIB press-release scraper
  with a persistent archive, RSS feed support, keyword-trend charts, and
  Slack/email alerting for new matches.

Both APIs ship with bundled dummy datasets, so every endpoint works fully
offline out of the box — useful in network-restricted environments where
live scraping or external APIs aren't reachable.

## About the Stack of the Application

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Analytics API | **Sanic** (async web framework) — `india_wireless_toolkit/services/sanic_api/` |
| News/Report API | **Blacksheep** (ASGI web framework) + `uvicorn` — `india_wireless_toolkit/services/blacksheep_api/` |
| CLI (still available) | `argparse` (`india_wireless_toolkit.cli`) |
| Data analysis | `matplotlib` for static charts, `plotly` for the interactive dashboard/map |
| Web scraping | `requests` + `beautifulsoup4` for HTML scraping, `feedparser` for RSS |
| Storage | `sqlite3` (bundled, no server) for the news archive — mounted on Azure File Share in production |
| Caching | `redis` — local Redis for dev, **Azure Cache for Redis** (TLS) in production, with automatic no-cache fallback |
| Configuration | `PyYAML` (`config.yaml`) for tunable parameters, `python-dotenv` + environment variables for secrets |
| Alerting | `smtplib`/email for SMTP alerts, `requests` for Slack incoming webhooks |
| Testing | `pytest` |
| Packaging | `setuptools` (`setup.py`) for the CLI; each API ships as its own Docker image |
| Cloud platform | **Microsoft Azure** — Azure Web Apps for Containers (one per service), Azure Container Registry, Azure Cache for Redis, Azure Storage (File Share) |
| Infrastructure as Code | Bicep (`azure/main.bicep`) |
| CI/CD | GitHub Actions (`.github/workflows/azure-deploy.yml`) — builds both images, pushes to ACR, restarts the Web Apps |
| Containerization | `Docker` (`Dockerfile.sanic`, `Dockerfile.blacksheep`, `Dockerfile.cli`) + `docker-compose` for local dev |
| Secret hygiene | `detect-secrets` via `pre-commit` |

No self-managed database server or message queue is required — Redis is
the only stateful dependency, and it's a managed Azure service in
production (local container for dev).

## Architecture of the Application

```
                              ┌───────────────────────┐
                              │   config.yaml + .env /  │
                              │   Azure App Settings    │
                              │  (config_loader.py)     │
                              └───────────┬─────────────┘
                                          │ CONFIG dict
                    ┌─────────────────────┴─────────────────────┐
                    ▼                                           ▼
      ┌───────────────────────────┐               ┌───────────────────────────┐
      │   Analytics API (Sanic)    │               │  News/Report API           │
      │   Azure Web App #1         │               │  (Blacksheep + uvicorn)    │
      │   services/sanic_api/      │               │  Azure Web App #2          │
      │                            │               │  services/blacksheep_api/  │
      │  /charts/*                 │               │  /news/*                  │
      │  /spectrum/*               │               │  /report                  │
      │  /infra/*                  │               │  /report/generate          │
      └──────┬──────────┬──────────┘               └──────┬──────────┬─────────┘
             │          │                                  │          │
             │ reads    │ cached via                       │ HTML/RSS │ stores
             ▼          ▼                                  ▼          ▼
   ┌──────────────┐ ┌──────────────────┐        ┌──────────────────┐ ┌──────────────────┐
   │ data/*.csv    │ │ Azure Cache for   │        │ TRAI / DoT / PIB │ │ news_archive.db  │
   │ (dummy data)  │ │ Redis (TLS)       │        │ (live) or        │ │ (SQLite, on      │
   └──────────────┘ │ db.py             │        │ press_releases_  │ │ Azure File Share) │
                     └──────────────────┘         │ sample.json      │ └────────┬─────────┘
                                                    └──────────────────┘          │ alerts
                                                                                   ▼
                                                                          ┌──────────────────┐
                                                                          │ Slack webhook /  │
                                                                          │ SMTP email       │
                                                                          └──────────────────┘
                              charts + report output
                                       │
                                       ▼
                          ┌─────────────────────────┐
                          │  Azure Storage File      │
                          │  Share: /mnt/toolkit-data │
                          │  (charts/, reports/,      │
                          │   news_archive.db)         │
                          │  — mounted into BOTH        │
                          │    Web Apps                │
                          └─────────────────────────┘

                     ┌───────────────────────────────────┐
                     │  Azure Container Registry (ACR)     │
                     │  india-wireless-analytics-api:latest │
                     │  india-wireless-news-api:latest      │
                     └───────────────────────────────────┘
```

**Flow summary:**

1. Each API loads shared settings from `config_loader.CONFIG`, which reads
   `config.yaml` and overlays secret-shaped values (SMTP credentials,
   Redis password, webhook URL) from environment variables — set via
   `.env` locally or Azure App Settings in production.
2. The **Analytics API** (Sanic) reads the bundled CSV datasets in
   `data/` for charts and infra-economics, and runs spectrum simulations
   directly, caching expensive Monte Carlo runs through `db.py`'s Redis
   layer (Azure Cache for Redis in production, TLS on port 6380).
3. The **News/Report API** (Blacksheep) fetches TRAI/DoT/PIB press
   releases (live HTML, RSS, or the bundled dummy JSON when offline),
   deduplicates and persists them to `news_archive.db` (SQLite), and can
   fire Slack/email alerts for new matches. It also stitches chart
   images, simulation output, and the news archive into one HTML report.
4. Both services write charts/reports/the SQLite archive to
   `/mnt/toolkit-data`, an **Azure File Share** mounted into both Web
   Apps — this is what survives container restarts and redeploys, since
   the container filesystem itself is ephemeral.
5. Both service images are built from their own Dockerfile
   (`Dockerfile.sanic`, `Dockerfile.blacksheep`), pushed to **Azure
   Container Registry**, and deployed as two separate **Azure Web Apps
   for Containers** — independently scalable and restartable. GitHub
   Actions (`.github/workflows/azure-deploy.yml`) automates the
   build/push/restart cycle on every push to `main`.
6. Locally, `docker compose up --build` runs both services plus a local
   Redis container (`REDIS_SSL=false`) so development doesn't require any
   Azure resources — the same code path just switches to
   `REDIS_SSL=true` and a managed Redis endpoint in Azure.
