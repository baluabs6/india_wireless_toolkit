# india-wireless-toolkit

## About the Application

**india-wireless-toolkit** is a CLI-driven research toolkit for exploring
current issues in India's wireless/telecom sector: satcom spectrum delays,
the 6 GHz Wi-Fi vs 5G/6G band split, last-mile fibre duplication economics,
and regulatory activity from TRAI, DoT, and PIB.

It brings together four kinds of analysis under one command-line entry
point:

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

All results can be combined into a single shareable HTML report. The
toolkit ships with bundled dummy datasets, so every module runs fully
offline out of the box — useful in network-restricted environments where
live scraping or external APIs aren't reachable.

## About the Stack of the Application

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| CLI | `argparse` (`india_wireless_toolkit.cli`) |
| Data analysis | `pandas`-free numeric modeling in pure Python, `matplotlib` for static charts, `plotly` for the interactive dashboard/map |
| Web scraping | `requests` + `beautifulsoup4` for HTML scraping, `feedparser` for RSS |
| Storage | `sqlite3` (bundled, no server) for the news archive |
| Caching | `redis` (optional) for expensive simulation/lookup results, with automatic no-cache fallback |
| Configuration | `PyYAML` (`config.yaml`) for tunable parameters, `python-dotenv` + environment variables for secrets |
| Alerting | `smtplib`/email for SMTP alerts, `requests` for Slack incoming webhooks |
| Testing | `pytest` |
| Packaging | `setuptools` (`setup.py`), installable as an editable package with a `india-wireless-toolkit` console script |
| Containerization | `Docker` + `docker-compose` (toolkit container + Redis container) |
| Secret hygiene | `detect-secrets` via `pre-commit` |

No database server, message queue, or external cloud service is required
— Redis is the only optional infrastructure dependency, and everything
degrades gracefully without it.

## Architecture of the Application

```
                              ┌───────────────────────┐
                              │   config.yaml + .env   │
                              │  (config_loader.py)    │
                              └───────────┬─────────────┘
                                          │ CONFIG dict
                                          ▼
                        ┌─────────────────────────────────┐
                        │   india_wireless_toolkit.cli     │
                        │  (argparse command dispatcher)   │
                        └───┬───────┬───────┬───────┬───────┘
                            │       │       │       │
             ┌──────────────┘  ┌────┘  ┌────┘  ┌────┘
             ▼                 ▼       ▼        ▼
   ┌───────────────┐ ┌───────────────┐ ┌───────────┐ ┌────────────────┐
   │data_visualization│ │spectrum_    │ │infra_     │ │ news_scraper   │
   │  .py             │ │simulation.py│ │economics.py│ │  .py           │
   └────────┬──────────┘ └──────┬──────┘ └─────┬─────┘ └───────┬────────┘
            │                   │              │               │
            │ reads             │ cached via   │               │ HTML/RSS
            ▼                   ▼              │               ▼
   ┌──────────────┐    ┌────────────────┐      │      ┌──────────────────┐
   │  data/*.csv   │    │   db.py        │      │      │ TRAI / DoT / PIB │
   │ (dummy datasets)│  │ (Redis cache,  │      │      │  (live) or       │
   └──────────────┘    │  graceful      │      │      │ press_releases_  │
                        │  fallback)     │      │      │ sample.json      │
                        └────────────────┘      │      └─────────┬─────────┘
                                                 │                │ stores
                                                 │                ▼
                                                 │       ┌──────────────────┐
                                                 │       │ news_archive.db  │
                                                 │       │   (SQLite)       │
                                                 │       └─────────┬─────────┘
                                                 │                 │ alerts
                                                 │                 ▼
                                                 │       ┌──────────────────┐
                                                 │       │ Slack webhook /  │
                                                 │       │ SMTP email       │
                                                 │       └──────────────────┘
                            outputs from all modules
                                       │
                                       ▼
                          ┌─────────────────────────┐
                          │   report_generator.py    │
                          │  (combines charts, sim    │
                          │  results, news archive)   │
                          └────────────┬──────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │ reports/india_wireless_  │
                          │      report.html         │
                          └─────────────────────────┘
```

**Flow summary:**

1. `cli.py` parses the top-level command (`charts`, `spectrum`, `infra`,
   `news`, `report`, or `all`) and dispatches to the matching module's
   `main()`.
2. Every module reads shared settings from `config_loader.CONFIG`, which
   loads `config.yaml` and overlays secret-shaped values (SMTP
   credentials, webhook URLs) from environment variables / `.env`.
3. `data_visualization.py` and `infra_economics.py` read from the bundled
   CSV datasets in `data/`; `spectrum_simulation.py` runs numeric models
   directly, optionally caching expensive runs through `db.py`'s Redis
   layer.
4. `news_scraper.py` fetches TRAI/DoT/PIB press releases (live HTML, RSS,
   or the bundled dummy JSON when offline), deduplicates and persists
   them to `news_archive.db` (SQLite), and can fire Slack/email alerts
   for new matches.
5. Charts render to `charts/*.png`, the interactive dashboard/map render
   via Plotly, and `report_generator.py` stitches chart images,
   simulation output, and the news archive into one HTML file in
   `reports/`.
6. Docker Compose wires the toolkit container to an optional Redis
   container for caching; without Docker, the same modules run directly
   with `python -m india_wireless_toolkit.cli`.
