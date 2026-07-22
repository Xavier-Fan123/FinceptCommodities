# Fincept Commodities — Local Dashboard

A fast, **localhost-only** commodities dashboard with a dedicated LPG trader
workspace. Public screens use Yahoo Finance, CFTC, EIA, and RSS feeds. The LPG
workspace can additionally ingest licensed S&P Global Commodity Insights data
through the official Excel Add-in and, when separately entitled, a contracted
machine-readable news API. Licensed files remain in a Git-ignored private data
directory and are never served outside `127.0.0.1`.

## Run

Double-click **`start.bat`**, or from a terminal:

```
"%LOCALAPPDATA%\com.fincept.terminal\venv-numpy2\Scripts\python.exe" server.py
```

It opens `http://127.0.0.1:8848/` automatically. Pass a port to change it:
`start.bat 9000`. Stop with Ctrl+C.

Install the declared dependencies when not using the bundled FinceptTerminal
Python environment:

```powershell
python -m pip install -r requirements.txt
```

## What you get

**LPG Trader Cockpit** — a first-class eight-view workspace for an Asia-focused
LPG trader:

- **Cockpit** — entitled FEI/CP/Mont Belvieu/freight assessments, source
  freshness, FEI-CP, P/B, FEI-MOPJ, and freight-adjusted arb signals.
- **Situation Map** — durable evidence-backed LPG events on a dependency-free
  reference map, with named terminals/chokepoints, reference trade corridors,
  source corroboration, baseline-aware alerts, inferred benchmark exposure,
  and explicit AIS/terminal/news intelligence gaps.
- **Curves & Spreads** — forward curves and derived spreads with unit checks.
- **History & Seasonality** — long history, statistics, monthly returns,
  seasonal bands, BATE selection, and correction counts.
- **MOC & Fundamentals** — entitlement-aware eWindow/MOC and dataset rows.
- **News** — a live LPG newsroom with relevance/freshness ranking, confirmed
  vs developing breaking rules, event clustering and related coverage,
  source-health telemetry, and clearly labelled licensed/public boundaries.
- **Data Explorer** — filtered series, observations, curves, revisions,
  candidates, news, spreads, and ingestion runs.
- **Data Status** — the complete entitlement matrix, source/session health,
  refresh jobs, coverage dates, and failure reasons.

Price data is never silently substituted. Derived spreads require compatible
currency/UOM. Daily legs must share an effective date; FEI-CP uses explicit
contract-month alignment. A stale/missing leg blocks the result instead of
combining mismatched assessments.

**Overview** — all 23 commodities grouped by sector (Energy, Precious/Base
Metals, Grains, Softs, Livestock) with live last price, change, %change, and
volume. Click a column header to sort (▲▼; third click restores sector
grouping), type in the filter box to narrow, click a row to drill in. Quotes
auto-refresh every minute while the tab is visible.

**Detail (per commodity)** — deep-linkable (`#/wti`, `#/natgas`, …; the
browser Back button works). Six panels render independently as their data
arrives — a slow CFTC response never blocks the price chart. All charts have
hover readouts. Energy contracts also get an **Energy Chain Context** panel
with benchmarks, drivers, key trading questions, related products, and a
direct jump into relevant news.

- **Price · 1Y** — daily close area chart
- **Futures Curve** — term structure with contango/backwardation tag,
  front→back spread, annualized roll yield and front→12m slope
- **Calendar Spreads** — the prompt M1→M2 time spread plus a 1/2/3/6/12-month
  ladder with annualized roll, read straight off the live curve: the trader's
  view of tightness (and it surfaces the natural-gas seasonal sawtooth too)
- **Risk · 1Y** — total return, annualized vol, VaR/CVaR 95%, max drawdown,
  skew, plus a 1y drawdown chart
- **Seasonality** — average return by calendar month with win rates on hover
  and a statistical-significance note
- **Seasonal Price Bands** — EIA-style 10y monthly min/max/avg band with the
  current year overlaid
- **COT Positioning** — managed-money vs commercials net, 1-week change,
  open interest, COT index (0–100, 3-year percentile) with extreme flag,
  52-week net-position chart, and a spec-vs-hedger divergence note
- **Inventories · EIA Weekly** (energy only: wti, natgas, rbob, heating_oil) —
  current stocks vs the 5-year weekly seasonal band, weekly build/draw with
  streak, and position vs the 5-year average
- **Crude Balance · EIA Weekly** (wti) — the full US crude balance: Cushing
  stocks (with their own 5-year band), SPR, production, refinery runs and
  utilization, and crude imports/exports, each coloured by its crude-price read

**Spreads tab** — live relative-value monitors computed from the same cached
histories: the refining suite — **WTI** and **Brent 3-2-1 cracks** plus the
single-cut **gasoline (RBOB−WTI)** and **distillate (ULSD−WTI)** cracks — then
**WTI−Brent**, the **gold/silver ratio**, and the **soybean board crush**, each
with 1y chart, percentile, z-score, and mean-reversion half-life.

**Energy Hub** — product-map coverage for crude, NGL/LPG, LNG, refined
products, olefins, aromatics, polymers, fertilizers, and carbon/power proxies.
Live futures stay linked to the market screen; physical products show their
proxy screen, trade lens, benchmarks, and monitoring questions.

**News** — a no-key news workspace for energy and chemicals. It aggregates
public sources where available (Google News RSS, EIA RSS, Yahoo Finance for
screen contracts), ranks headlines by market relevance, tags affected products,
and shows watch briefs when live feeds are unavailable.

**Energy primer** — `ENERGY.md` (footer link) explains the energy complex
through the dashboard's own panels: benchmarks, term structure and roll
yield, seasonality drivers, crack spreads, COT anatomy, the EIA calendar,
and risk character.

Charts are drawn as inline SVG (no external libraries), so everything renders
locally and instantly.

## How it works

```
server.py        stdlib http.server (no Flask) + layered TTL cache
sources.py       compact fetchers — yfinance (quotes/history/curve) + CFTC (COT)
energy_chemicals.py  Energy Hub product map + key-less news aggregation
lpg/             private SQLite store, catalog, analytics, Excel staging,
                 news/event intelligence, refresh jobs, exports, workflow, and CLI
analytics/       the tested commodity analytics (term structure, risk,
                 seasonality, positioning, spreads, …), self-contained
web/             index.html · style.css · app.js · situation.js/css (dashboard UI)
scripts/         official Add-in refresh and Windows Task Scheduler helpers
ENERGY.md        energy-commodities primer (served at /ENERGY.md)
start.bat        launcher (points at the venv Python)
```

API (all JSON, all local):

```
/api/overview[?sector=energy]
/api/commodity/{id}                          all panels, fetched in parallel
/api/commodity/{id}/{history|curve|seasonality|cot|inventory|balance}
/api/spreads
/api/energy-chemicals
/api/energy-chemicals/product/{id}
/api/news[?topic=energy][&product=wti][&limit=50]
/api/lpg/summary[?as_of=YYYY-MM-DD]
/api/lpg/series
/api/lpg/series/{id}/history
/api/lpg/curves
/api/lpg/spreads
/api/lpg/situation[?severity=high][&event_type=shipping_disruption][&region=Middle%20East]
/api/lpg/scenarios
/api/lpg/vessel-intelligence[?fleet_group=equinor_reference][&vessel_id=imo-...]
/api/lpg/vessels
/api/lpg/vessels/{id}/port-calls
/api/lpg/news[?q=...][&region=asia][&importance=high][&fresh=1]
/api/lpg/explorer
/api/lpg/status
/api/lpg/refresh/{job_id}
/api/lpg/export?view=...&format=csv|xlsx
```

`POST /api/lpg/refresh` starts a single-flight asynchronous job. Current-data
scopes are `asia`, `overnight`, `news`, and `all`; the isolated dataset scopes
are `history`, `curves`, and `moc`. A second refresh receives HTTP 409 with the
active job rather than starting another Excel instance.

`POST /api/lpg/scenarios/run` evaluates one of the six bounded LPG stress
templates (`hormuz_closure`, `panama_disruption`, `red_sea_avoidance`,
`saudi_loading_reduction`, `usgc_export_outage`, or
`north_asia_demand_surge`). Inputs and the stress-index formula are returned
with every result. The index is a relative assumption aid: it is not a
probability, price, freight, cargo-flow, VaR, or P&L forecast.

## Licensed LPG setup

The workflow uses the signed-in official Excel Add-in session; it never stores
an interactive username or password.

```powershell
# Build private, isolated-query workbooks (Git ignored)
python -m lpg.cli build --scope all

# Open one workbook once and sign in from the S&P Global Energy ribbon.
# Then run discovery + refresh + atomic staging + SQLite import:
python -m lpg.cli refresh --scope asia
python -m lpg.cli refresh --scope overnight

# Backfill history in deterministic per-symbol/per-year workbooks.
# Start with one known entitled symbol; add more --symbol values as needed.
python -m lpg.cli refresh-history --start-year 2026 --end-year 2026 --scope asia --symbol PMAAV00 --batch-size 1 --timeout 600

# Query official FC CurveData independently from the current-price workbooks
python -m lpg.cli refresh-curves --scope asia --curve CN3HO --batch-size 1 --timeout 600

# Query Platts eWindow/MOC independently
python -m lpg.cli refresh-moc --scope asia --timeout 600

# Inspect the entitlement/source matrix
python -m lpg.cli status
```

Private artifacts are under `data/private/platts/` and
`data/private/lpg.sqlite`. Each symbol/curve is queried independently, so one
unentitled code cannot invalidate an entitled batch. A failed/expired Excel
session does not replace the last-good staging file or observations.
The History page refreshes the currently selected entitled symbol instead of
launching every catalog symbol. Longer Excel jobs remain tracked in the
background, and a stable Add-in formula/session error is written to dataset
status and returned promptly instead of appearing as an empty chart.
Generated workbooks use manual calculation and the refresh runner dispatches
each isolated Add-in formula once; this prevents volatile async UDFs from
repeating completed requests. Current prices, history, forward curves, eWindow,
and news are separate entitlements, and unavailable datasets stay visibly empty
rather than being silently estimated. When official FC points are absent, the
Curve view may show an explicitly labelled non-official prompt structure
calculated only from the entitled HM1/HM2/HM3 price series; it never presents
that structure as Platts CurveData.

Yearly backfill workbooks are resumable and can also be built without opening
Excel:

```powershell
python -m lpg.cli build-backfill --start-year 2021 --end-year 2026 --scope all --batch-size 1
```

Review scheduler configuration without changing Windows:

```powershell
powershell -File scripts\Install-LpgScheduledTasks.ps1 -DryRun
```

Install only after the dry run is correct. The tasks run for the interactive
user at 08:00 (overnight/US legs) and 17:30 Singapore time (Asia close). If
Excel is open, the refresh defers without taking focus; the trigger retries
every 15 minutes for up to two hours. After the first usable refresh that day,
later retry triggers exit without reopening Excel. Excel itself starts as a
normal visible desktop window long enough for the official Add-in to restore
its remembered sign-in, then the runner minimizes and automates it. If a manual
refresh is started from an Administrator Terminal, the workbook runner
automatically re-dispatches at the interactive user's Limited integrity so the
per-user S&P ribbon and session remain available.

```powershell
powershell -File scripts\Install-LpgScheduledTasks.ps1 -Install
```

Machine-readable Platts news is a separate entitlement from Excel. Copy
`.env.example` to a private `.env` and populate only the endpoint/OAuth fields
supplied with the API contract. When it is not configured, the system reports
the gap and uses public LPG news; it does not scrape Platts Connect.

The public newsroom concurrently checks focused Google News discovery queries,
rotating GDELT DOC 2.0 regional discovery, Saudi Aramco, EIA and NHC feeds. It
persists per-source latency/error/last-success health and filters consumer LPG
noise before storing a headline. GDELT queries are deliberately rotated rather
than burst concurrently to respect its public service rate guidance. Public
feeds have no production SLA and never masquerade as licensed Platts content.
Set `LPG_NEWS_FEEDS_JSON` only for additional RSS/Atom feeds you are permitted
to ingest.

Every successful News refresh also rebuilds the durable `intelligence_events`
layer. Events inherit only entitled-to-display local news evidence. Named
locations are matched conservatively against a curated LPG asset registry;
unresolved events stay off-map. Route and benchmark exposure is explicitly
labelled as inferred context, not an official assessment. The built-in map has
no external tile or JavaScript dependency, and reference corridors never
masquerade as live vessel tracks. Satellite AIS and authoritative terminal
operating status remain visible intelligence gaps until separately entitled
feeds are configured.

The Situation Map includes an interactive Scenario Engine. A scenario uses
only user-entered duration, shock, and where relevant additional-transit
assumptions. It highlights exposed reference assets and corridors, links the
affected benchmarks to currently entitled rows, and surfaces commercial
questions, calculation components, assumptions, and missing data. It never
fills an unavailable market row or presents a calculated market outcome.

Vessel Intelligence keeps three capabilities separate: historical port calls,
continuous AIS tracks, and timestamped current positions. Import a permitted
CSV snapshot with:

```powershell
python -m lpg.cli import-vessels C:\path\to\port_calls.csv --fleet-group reference_fleet
```

The import is keyed and idempotent, records the source-file SHA-256 and source
health, preserves naive timestamps as `source_timezone_unverified`, and
recomputes draught-change operation signals as inference. Port-call coordinates
are displayed as historical triangles and never inserted into
`vessel_positions`. A marker can be labelled live only when a configured
provider supplies an explicit timestamped position; the current freshness rule
is live up to one hour, recent up to 24 hours, and stale thereafter. Snapshot
access does not establish production API or redistribution entitlement. Raw
provider evidence stays in the private SQLite audit record; browser/API read
models expose only normalized fields, provenance, and the evidence hash.

While the local server is running, the persisted LPG news snapshot refreshes in
the background every two minutes. The visible News tab checks every 60 seconds
and follows an active refresh job every 10 seconds. A failed source is isolated;
last-good headlines remain available and its error is shown in Source Health.

Append `&fresh=1` (or `?fresh=1`) to bypass the cache — the ↻ button does
this for the active view.

Caching is matched to how often each source actually changes: quotes 60 s,
history/curve 5 min, seasonality 24 h, COT and EIA inventories 6 h — each
with a stale-while-revalidate window (stale data is served instantly while
one background thread refreshes), per-key request coalescing, and a startup
warm-up so the first page paint hits a warm cache.

Public data sources:
- **Yahoo Finance** (key-less) — continuous-contract quotes/history and dated
  contracts (e.g. `CLN26.NYM`) for building the futures curve
- **Public news discovery/feeds** (key-less) - focused Google News discovery,
  rotating GDELT DOC 2.0 queries, Saudi Aramco, EIA and NHC for the LPG
  newsroom; Google News RSS, Yahoo Finance RSS and EIA for the broader energy
  workspace. Publisher copyright remains with the publisher; the LPG store
  keeps attributed metadata/link/available feed summary, not scraped articles.
- **CFTC public reporting API** (key-less) — Commitments of Traders
  positioning (disaggregated report; contract codes verified against the live
  dataset, including Brent `06765T` and Henry Hub `023651`)
- **EIA API v2** (free key) — weekly energy inventories: crude ex-SPR
  `WCESTUS1`, total gasoline `WGTSTUS1`, distillates `WDISTUS1`, Lower-48
  working gas `NW2_EPG0_SWO_R48_BCF`; plus the WTI crude balance from the
  weekly supply route (`petroleum/sum/sndw`): Cushing stocks, SPR, production,
  refinery runs, utilization, and crude imports/exports

Optional licensed sources:

- **S&P Global Energy Excel Add-in** — entitled LPG assessments, history,
  corrections, forward curves, discovery metadata, and eWindow datasets.
- **S&P Global machine-readable news API** — only when a separate API contract
  and client credentials are configured locally.

## Notes

- **Python dependency:** the tool needs a Python with `yfinance`, `pandas`,
  `numpy`, and `scipy`. `start.bat` uses the FinceptTerminal venv because those
  packages are already installed there. To decouple entirely, create your own
  venv (`python -m venv env && env\Scripts\pip install yfinance pandas numpy
  scipy`) and edit the `PY=` line in `start.bat`.
- **Not every panel exists for every commodity** — anything missing shows a
  per-panel "unavailable" with a retry button; other panels still load.
- **EIA key:** the inventory panels read the API key from the `EIA_API_KEY`
  environment variable, falling back to `eia_api_key.txt` in this folder
  (one line, the key). Keep that file private — don't share or commit it.
  Free registration: eia.gov/opendata.
- **Proxy handling:** market data and news fetchers deliberately bypass
  process-level `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` settings. This keeps
  the dashboard working on machines where those variables point to a dead local
  proxy such as `127.0.0.1:9`.
- **Private/licensed data:** `.env`, `data/private/`, and `data/exports/` are
  excluded from Git. Do not remove these rules or publish SQLite/workbook
  contents, entitlement results, news bodies, tokens, or credentials.
- This folder is independent of `C:\Program Files\FinceptTerminal` — it keeps
  working even if that app is uninstalled (as long as the chosen Python has
  the packages above).
