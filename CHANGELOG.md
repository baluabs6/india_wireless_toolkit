# Changelog

## 2.1.0

Correctness, security and reliability release. **Results change** where earlier versions were wrong;
the reason for each change is listed so you can judge it.

### Model corrections (results change)

- **spectrum: free-space path loss had a 60 dB unit error.** The reference loss at 1 m used the
  kilometre/MHz constant with a distance in metres: 108 dB instead of 48 dB at 6 GHz. Every SINR was
  negative (about -12 dB at 2 m) and the Monte Carlo gave -20.1 dB (500 MHz) vs -20.4 dB (1200 MHz), i.e. no
  visible effect of spectrum. After the fix: ~47 dB at 2 m, and 6.8 dB (500 MHz) vs 14.8 dB (1200 MHz).
- **spectrum: channel counts** now account for the 20 MHz band-edge guard (500 MHz -> 24 x 20 MHz, 3 x 160;
  1200 MHz -> 59 x 20, 7 x 160), matching the real 6 GHz band plan.
- **spectrum: PHY rates** are derived from tone plans (80/160 MHz were 4.5% low: 720.6 / 1441.2 Mbps).
- **spectrum: THz atmospheric loss** was `0.05 * GHz` dB per metre (15 dB/m at 300 GHz). Replaced by a
  per-band absorption table in dB/km. Free-space loss uses exponent 2.0 and the takeaway text was rewritten.
- **spectrum: Monte Carlo** uses a local `random.Random(seed)` instead of reseeding the global RNG.
- **spectrum: `config.yaml` `spectrum:` values were never read.** They now drive scenarios, charts and API.
- **economics: lease fees were double-counted.** The shared model's cost was `capex + lease fees` (fees repay
  that capex) and omitted the neutral operator's opex. Total resource cost is now `capex + opex` of one
  network; fees are reported as a transfer. Default savings move from 53.8% to 80.0%.
- **economics: NPV** adds optional adoption ramp and the fibre's residual value (`include_terminal_value`,
  `fibre_asset_life_years`), removes the dead `annual_revenue_per_building_inr` default (now an optional override),
  and `irr()` falls back to bisection when Newton diverges.
- **economics: break-even** now requires the neutral operator's NPV >= 0 and that ISPs prefer leasing to
  building (default: 3 ISPs; each condition is reported separately).
- **economics: sensitivity/tornado** only varied the duplicate-model cost, so 3 of 6 bars were zero
  (including the access fee). It now takes a `metric` (`savings`, `neutral_npv`, `fwa_total_cost`, ...) and
  reports which metrics each variable drives.
- **economics: three-way comparison** adds an asset-life-adjusted cost alongside the cash totals.
- **data:** national statistics moved from plotting code to `data/national_stats.csv`; Delhi's rural
  teledensity is blank (unknown) instead of `0`.

### News pipeline

- Real publication dates (RSS fields, dates found in listing rows) instead of the scrape date; ISO dates so
  sorting is correct; `date_source` records `published` / `fetched`.
- Dedup on canonical URL + normalised title (tracking params, `www.`, http/https, trailing slash).
- Whole-word/phrase keyword matching (`"row"` no longer matches "growth"); relative, protocol-relative and
  `javascript:` links handled via `urljoin`.
- `origin` column (`live`, `rss`, `sample`); sample data is never mixed in silently, never alerted by default.
- Alerts only send items not yet alerted (`alerted_at`), batch Slack messages, escape Slack markup, and mark
  items only when every configured channel succeeded. Alert queries filter sample rows in SQL.
- `limit` validated everywhere (`LIMIT -1` used to return all rows). SQLite connections use timeouts.
- Existing archives are migrated in place (columns added, legacy rows labelled, ids re-keyed, duplicates collapsed).
- "Keyword trend" now includes a real monthly time series.

### Security

- API-key authentication (`API_KEY`, `API_REQUIRE_KEY`) and per-route rate limits on both APIs.
- SSRF: hostnames are resolved and must be public, redirects re-validated, timeouts and size caps
  (`feedparser` used to fetch with no timeout). Applies to RSS, live scraping and `fetch_live_trai_data`.
- The HTML report escapes all scraped content and only links http(s) URLs.
- Charts are built on `Figure` objects (no shared `pyplot` state) and written atomically; the APIs rebuild a
  chart only when its inputs changed. Agent runs are limited to 3 concurrent per process.
- Containers run as a non-root user with a `HEALTHCHECK`; Azure uses Key Vault references and managed
  identity (no ACR admin password, no plain-text Redis key), health checks, TLS 1.2+, FTPS off.

### Reliability

- Redis: failed connections are retried (was: disabled for the life of the process); `flush_all` only
  removes `iwt:*` keys (was `FLUSHDB`); cache keys are versioned; results have the same types on hit and miss.
- Stdout capture (`redirect_stdout`, not thread-safe) is gone: every analysis has a pure function that
  returns data, plus a `*_report` that prints it.
- Paths in `config.yaml` resolve against the config file's folder, not the working directory; nothing is
  created at import time. plotly is imported lazily (only the dashboard and map need it).
- CI runs tests and a secret scan before deploying, deploys immutable SHA tags and rolls back on a failed
  health check. `docker-compose` no longer bind-mounts a gitignored file (Docker created a directory).

### Agent

- Infra tools accept scenario parameters (ISPs, buildings, years, discount rate, costs).
- Truncated answers are flagged; hitting the turn limit asks the model for a summary of what it has.
- Each result carries `_ref` (`tool#n`) and the response lists `sources`; the system prompt asks for citations.
- Prompt caching on the system prompt and tool list; tool errors no longer leak file paths.

### Tests

116 tests (was ~10) including golden values (FSPL 48.0 dB at 1 m / 6 GHz), the SSRF cases, migration of a
v2.0 archive, alert state, and the agent loop against a fake client.

### Not done / known limits

- The Sanic and Blacksheep apps could not be started in the environment these changes were made in (the
  frameworks were not installable); only their shared logic (`security.py`, the toolkit modules) was executed.
  Their smoke tests run when the frameworks are installed.
- `azure/main.bicep` was edited without the Bicep compiler available; run `az bicep build` first.
- SSRF protection resolves DNS before connecting, so a DNS answer that changes in between is still a residual
  risk - keep an egress firewall rule as well. Rate limiting is per process.
- The PIB source URL in `config.yaml` is a detail-page template, not a listing; replace it with a real listing/RSS URL.
- Model limits remain: indoor log-distance path loss with fixed wall loss, co-channel interference only,
  PHY rates are peak single-stream, and the economics compare cost only (not service quality).
