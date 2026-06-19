# Fincept Commodities — Local Dashboard

A fast, **fully local** commodities dashboard. It runs in your browser, talks
only to **free public data APIs** (Yahoo Finance + CFTC), and has **no login,
no backend, and no dependency on the FinceptTerminal app**. Built to give the
commodity views a clean, snappy presentation without the heavy Qt/Chromium app.

## Run

Double-click **`start.bat`**, or from a terminal:

```
"%LOCALAPPDATA%\com.fincept.terminal\venv-numpy2\Scripts\python.exe" server.py
```

It opens `http://127.0.0.1:8848/` automatically. Pass a port to change it:
`start.bat 9000`. Stop with Ctrl+C.

## What you get

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

**Spreads tab** — live relative-value monitors computed from the same cached
histories: **3-2-1 crack spread** (refining margin), **WTI−Brent**,
**gold/silver ratio**, and the **soybean board crush**, each with 1y chart,
percentile, z-score, and mean-reversion half-life.

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
analytics/       the tested commodity analytics (term structure, risk,
                 seasonality, positioning, spreads, …), self-contained
web/             index.html · style.css · app.js (the dashboard UI)
ENERGY.md        energy-commodities primer (served at /ENERGY.md)
start.bat        launcher (points at the venv Python)
```

API (all JSON, all local):

```
/api/overview[?sector=energy]
/api/commodity/{id}                          all panels, fetched in parallel
/api/commodity/{id}/{history|curve|seasonality|cot|inventory}
/api/spreads
/api/energy-chemicals
/api/energy-chemicals/product/{id}
/api/news[?topic=energy][&product=wti][&limit=50]
```

Append `&fresh=1` (or `?fresh=1`) to bypass the cache — the ↻ button does
this for the active view.

Caching is matched to how often each source actually changes: quotes 60 s,
history/curve 5 min, seasonality 24 h, COT and EIA inventories 6 h — each
with a stale-while-revalidate window (stale data is served instantly while
one background thread refreshes), per-key request coalescing, and a startup
warm-up so the first page paint hits a warm cache.

Data sources, all free:
- **Yahoo Finance** (key-less) — continuous-contract quotes/history and dated
  contracts (e.g. `CLN26.NYM`) for building the futures curve
- **Public news feeds** (key-less) - Google News RSS, Yahoo Finance RSS, and
  EIA Today in Energy RSS for the energy and petrochemicals news workspace
- **CFTC public reporting API** (key-less) — Commitments of Traders
  positioning (disaggregated report; contract codes verified against the live
  dataset, including Brent `06765T` and Henry Hub `023651`)
- **EIA API v2** (free key) — weekly energy inventories: crude ex-SPR
  `WCESTUS1`, total gasoline `WGTSTUS1`, distillates `WDISTUS1`, Lower-48
  working gas `NW2_EPG0_SWO_R48_BCF`

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
- This folder is independent of `C:\Program Files\FinceptTerminal` — it keeps
  working even if that app is uninstalled (as long as the chosen Python has
  the packages above).
