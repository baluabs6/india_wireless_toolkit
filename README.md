# india-wireless-toolkit (v2)

Scripts covering current issues in India's wireless/telecom sector:
satcom spectrum delays, the 6 GHz Wi-Fi vs 5G/6G band split, last-mile
fibre duplication economics, and tracking TRAI/DoT/PIB press releases —
now with Redis caching, dummy datasets, interactive dashboards, and more.

## Install

```bash
pip install -r requirements.txt
pip install -e .          # optional, for the `india-wireless-toolkit` CLI command
```

Redis is optional but recommended (used to cache slow simulations and news
archive lookups). Everything falls back gracefully to "no cache" mode if
Redis isn't running.

```bash
# Local Redis (Debian/Ubuntu)
sudo apt-get install redis-server && redis-server --daemonize yes

# Or via Docker (see docker-compose.yml below)
docker compose up -d redis
```

## Run

```bash
python -m india_wireless_toolkit.cli all                 # run everything
python -m india_wireless_toolkit.cli charts               # static charts -> ./charts/*.png
python -m india_wireless_toolkit.cli charts --dashboard    # + interactive Plotly dashboard
python -m india_wireless_toolkit.cli charts --map          # + state-wise bubble map
python -m india_wireless_toolkit.cli charts --all-features # + dashboard + map

python -m india_wireless_toolkit.cli spectrum                       # channel capacity + SINR + Monte Carlo + THz
python -m india_wireless_toolkit.cli spectrum --whatif-mhz 800       # custom "what-if" MHz allocation
python -m india_wireless_toolkit.cli spectrum --aps 30               # change AP density in stress tests
python -m india_wireless_toolkit.cli spectrum --skip-montecarlo      # faster run, skips Monte Carlo

python -m india_wireless_toolkit.cli infra                # base duplicate-vs-shared cost model
python -m india_wireless_toolkit.cli infra --npv           # + NPV/IRR analysis
python -m india_wireless_toolkit.cli infra --sensitivity   # + tornado/sensitivity chart
python -m india_wireless_toolkit.cli infra --compare       # + FTTH vs FWA vs Satellite comparison
python -m india_wireless_toolkit.cli infra --breakeven     # + break-even ISP count
python -m india_wireless_toolkit.cli infra --all-features  # run all of the above

python -m india_wireless_toolkit.cli news --dummy          # load bundled dummy dataset (works offline)
python -m india_wireless_toolkit.cli news                  # live scrape (needs real internet + non-blocked IP)
python -m india_wireless_toolkit.cli news --rss <url> ...   # scrape RSS feeds instead of HTML
python -m india_wireless_toolkit.cli news --trend           # keyword trend chart from the SQLite archive
python -m india_wireless_toolkit.cli news --alert           # send Slack/email alerts for new matches

python -m india_wireless_toolkit.cli report                 # combined HTML report (charts + sims + news)
```

Or, after `pip install -e .`:
```bash
india-wireless-toolkit all
```

## Docker

```bash
docker compose up --build     # runs Redis + the toolkit together
```

`docker-compose.yml` wires `REDIS_HOST=redis` automatically so the app talks
to the containerized Redis instance.

## Tests

```bash
pip install pytest
pytest tests/ -v
```

## Modules

| Module | What it does |
|---|---|
| `data_visualization.py` | Static charts (broadband split, teledensity gap, 6 GHz split, public Wi-Fi gap), a time-series subscriber trend chart, an interactive Plotly dashboard with a 6 GHz "what-if" slider, a state-wise bubble map, and a live-fetch hook for TRAI open data (needs real internet). |
| `spectrum_simulation.py` | Channel capacity modeling, a log-distance path-loss/SINR model, Monte Carlo AP-placement interference simulation (Redis-cached), illustrative 6G terahertz-band modeling, and a `--whatif-mhz` custom scenario mode. |
| `infra_economics.py` | Duplicate vs shared-neutral last-mile cost model, NPV/IRR analysis, sensitivity/tornado chart analysis, a three-way FTTH vs FWA vs Satellite comparison, and a break-even ISP-count calculator. |
| `news_scraper.py` | TRAI/DoT/PIB press-release scraper with SQLite storage + dedup, RSS feed support, Slack/email alerting, a keyword-trend chart, and a bundled dummy dataset so the whole pipeline works offline. |
| `report_generator.py` | Combines all charts, simulation output, and the news archive into one shareable HTML report. |
| `db.py` | Redis caching layer (`@cached` decorator + `RedisCache` class). Falls back to "no cache" automatically if Redis is unreachable or disabled in `config.yaml`. |
| `config_loader.py` | Loads `config.yaml` once and exposes it as `CONFIG`. |

## Dummy datasets (`data/`)

| File | Contents |
|---|---|
| `subscriber_trends.csv` | Monthly wireless/wireline broadband subscriber counts and teledensity, Jan 2024–Apr 2026. |
| `state_teledensity.csv` | 15 Indian states with urban/rural teledensity, 5G coverage %, and lat/lon for mapping. |
| `press_releases_sample.json` | 7 sample TRAI/DoT/PIB press releases for offline testing of the news pipeline. |
| `infra_cost_variables.csv` | Base/low/high estimates for each cost variable, used in sensitivity analysis. |

## Configuration (`config.yaml`)

All tunable parameters — Redis connection, spectrum MHz assumptions, infra
cost variables, news sources/keywords, alert webhooks — live in
`config.yaml`. Edit it instead of touching module code.

## Notes

- Figures are based on publicly reported TRAI/DoT figures and industry
  estimates as of 2026, or clearly-marked illustrative/dummy data — swap in
  real datasets for production use.
- `news_scraper.py`'s live HTML scraping may return 403s from government
  sites depending on your IP/network — use `--dummy` or `--rss` as
  reliable offline/alternative paths.
- The SINR/path-loss and THz models are simplified engineering
  approximations for illustration, not a substitute for a licensed RF
  propagation study.
