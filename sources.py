"""Self-contained commodity data fetchers for the local dashboard.

Uses only free public APIs:
  - Yahoo Finance (yfinance) for quotes, history, and dated-contract curves
  - CFTC public reporting API for Commitments of Traders positioning
  - EIA API v2 for weekly energy inventories (free key: eia.gov/opendata;
    read from EIA_API_KEY env var or eia_api_key.txt next to this file)

No dependency on the FinceptTerminal application or its login/backend — this
runs entirely locally against public data.
"""

import contextlib
import io
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from analytics.config import get_spec, MONTH_CODES

# Yahoo Finance exchange suffix for dated contracts (e.g. CLN26.NYM)
_YF_SUFFIX = {"NYMEX": "NYM", "NYMEX/ICE": "NYM", "COMEX": "CMX",
              "CBOT": "CBT", "CME": "CME", "ICE": "NYB"}

# CFTC Socrata dataset resource ids
_CFTC_RES = {"disaggregated": "kh3c-gbw2", "legacy": "jun7-fc8e"}

_INV_MONTH = {v: k for k, v in MONTH_CODES.items()}
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
_HTTP = requests.Session()
_HTTP.trust_env = False


@contextlib.contextmanager
def _direct_network_env():
    """Run public-data clients without inheriting a broken local proxy.

    This machine can have HTTP(S)_PROXY/ALL_PROXY pointed at 127.0.0.1:9,
    which makes yfinance/curl fail before reaching Yahoo. The dashboard talks
    only to public data APIs, so direct egress is the correct default here.
    """
    saved = {key: os.environ.pop(key) for key in _PROXY_ENV_KEYS if key in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _f(value: Any) -> Optional[float]:
    try:
        f = float(value)
        return f if f == f else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def _yf():
    with _direct_network_env():
        import yfinance as yf
    return yf


def _download(tickers, period="5d", interval="1d"):
    yf = _yf()
    buf = io.StringIO()
    with _direct_network_env(), contextlib.redirect_stdout(buf):  # silence yfinance progress noise
        return yf.download(tickers, period=period, interval=interval,
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=True)


def _hist_for(data, ticker):
    """Extract one ticker's frame from a (possibly multi-ticker) download."""
    if data is None or getattr(data, "empty", True):
        return None
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0).unique().tolist()
        if ticker in level0:
            h = data[ticker]
        else:
            try:
                h = data.xs(ticker, axis=1, level=1)
            except KeyError:
                return None
    else:
        h = data
    h = h.dropna(how="all")
    return h if not h.empty else None


def batch_quotes(ids: List[str]) -> List[Dict[str, Any]]:
    specs = {}
    for c in ids:
        s = get_spec(c)
        if s and s.get("yf_ticker"):
            specs[s["yf_ticker"]] = s
    if not specs:
        return []
    data = _download(list(specs.keys()), period="5d")
    out = []
    for tkr, spec in specs.items():
        h = _hist_for(data, tkr)
        if h is None or "Close" not in h:
            continue
        close = _f(h["Close"].iloc[-1])
        if close is None:
            continue
        prev = _f(h["Close"].iloc[-2]) if len(h) >= 2 else close
        prev = prev if prev else close
        chg = close - prev
        vol = h["Volume"].iloc[-1] if "Volume" in h else None
        out.append({
            "id": spec["id"], "name": spec["name"], "sector": spec["sector"],
            "exchange": spec["exchange"], "quote_unit": spec["quote_unit"],
            "price": round(close, 4), "change": round(chg, 4),
            "change_percent": round(chg / prev * 100, 2) if prev else 0.0,
            "volume": int(vol) if _f(vol) is not None else 0,
        })
    return out


def history(commodity: str, period: str = "1y",
            interval: str = "1d") -> Any:
    spec = get_spec(commodity)
    if not spec:
        return {"error": "unknown commodity"}
    yf = _yf()
    buf = io.StringIO()
    with _direct_network_env(), contextlib.redirect_stdout(buf):
        h = yf.Ticker(spec["yf_ticker"]).history(period=period, interval=interval)
    if h is None or h.empty:
        return []
    recs = []
    for idx, row in h.iterrows():
        close = _f(row.get("Close"))
        if close is None:
            continue
        recs.append({
            "timestamp": int(idx.timestamp()),
            "date": idx.strftime("%Y-%m-%d"),
            "open": _f(row.get("Open")), "high": _f(row.get("High")),
            "low": _f(row.get("Low")), "close": close,
            "volume": int(row["Volume"]) if _f(row.get("Volume")) is not None else 0,
        })
    return recs


def curve(commodity: str, max_contracts: int = 10) -> List[Dict[str, Any]]:
    """Futures curve from Yahoo dated contracts (CME settlements are bot-blocked)."""
    spec = get_spec(commodity)
    if not spec:
        return []
    suffix = _YF_SUFFIX.get(spec["exchange"])
    root = spec.get("cme_code") or (spec.get("yf_ticker") or "").replace("=F", "")
    if not suffix or not root:
        return []
    now = datetime.now()
    listed = sorted(MONTH_CODES[m] for m in spec["months"])
    tickers, tmap = [], {}
    y, m = now.year, now.month
    while len(tickers) < max_contracts and y <= now.year + 3:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if m in listed:
            t = f"{root}{_INV_MONTH[m]}{str(y)[-2:]}.{suffix}"
            tickers.append(t)
            tmap[t] = f"{y}-{m:02d}"
    if not tickers:
        return []
    data = _download(tickers, period="5d")
    contracts = []
    for t in tickers:
        h = _hist_for(data, t)
        if h is None or "Close" not in h:
            continue
        px = _f(h["Close"].iloc[-1])
        if px and px > 0:
            contracts.append({"expiry": tmap[t], "month_label": t, "price": round(px, 4)})
    contracts.sort(key=lambda c: c["expiry"])
    return contracts


# ---------------- EIA weekly inventories ----------------

_EIA_BASE = "https://api.eia.gov/v2"

# Weekly inventory series for the energy complex (series ids verified against
# the live API). scale converts API units to the display unit.
_EIA_SERIES: Dict[str, Dict[str, Any]] = {
    "wti": {"route": "petroleum/stoc/wstk", "series": "WCESTUS1",
            "scale": 0.001, "unit": "MMbbl",
            "label": "US crude stocks ex-SPR"},
    "rbob": {"route": "petroleum/stoc/wstk", "series": "WGTSTUS1",
             "scale": 0.001, "unit": "MMbbl",
             "label": "US total gasoline stocks"},
    "heating_oil": {"route": "petroleum/stoc/wstk", "series": "WDISTUS1",
                    "scale": 0.001, "unit": "MMbbl",
                    "label": "US distillate stocks"},
    "natgas": {"route": "natural-gas/stor/wkly", "series": "NW2_EPG0_SWO_R48_BCF",
               "scale": 1.0, "unit": "Bcf",
               "label": "Lower 48 working gas in storage"},
}


def eia_key() -> Optional[str]:
    key = os.environ.get("EIA_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "eia_api_key.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def inventory_spec(commodity: str) -> Optional[Dict[str, Any]]:
    spec = get_spec(commodity)
    return _EIA_SERIES.get(spec["id"]) if spec else None


def eia_inventory(commodity: str, weeks: int = 340) -> Any:
    """Weekly inventory levels from the EIA API v2, oldest-first, scaled to
    display units (MMbbl / Bcf). ~340 weeks covers the 5y band + buffer."""
    ses = inventory_spec(commodity)
    if not ses:
        return {"error": "no EIA series for this commodity"}
    key = eia_key()
    if not key:
        return {"error": "no EIA API key (set EIA_API_KEY or eia_api_key.txt)"}
    url = f"{_EIA_BASE}/{ses['route']}/data/"
    params = {
        "api_key": key, "frequency": "weekly", "data[0]": "value",
        "facets[series][]": ses["series"],
        "sort[0][column]": "period", "sort[0][direction]": "desc",
        "length": weeks,
    }
    r = _HTTP.get(url, params=params, timeout=30,
                  headers={"User-Agent": "FinceptCommoditiesLocal/1.0",
                           "Accept": "application/json"})
    r.raise_for_status()
    rows = (r.json().get("response") or {}).get("data") or []
    out = []
    for row in rows:
        v = _f(row.get("value"))
        if v is None or not row.get("period"):
            continue
        out.append({"date": str(row["period"]), "value": v * ses["scale"]})
    out.reverse()  # API returns newest first
    return out


# ---------------- EIA weekly crude balance (US petroleum supply) ----------------

# The Weekly Supply Estimates route (petroleum/sum/sndw) carries the whole US
# crude balance in one dataset, so the entire panel is a single request.
# Series ids verified live (2026-06): stocks in MBBL -> MMbbl (x0.001),
# flows in MBBL/D -> Mb/d (x0.001), utilization already in %.
_EIA_SUPPLY_ROUTE = "petroleum/sum/sndw"

_CRUDE_BALANCE: List[Dict[str, Any]] = [
    {"id": "cushing", "series": "W_EPC0_SAX_YCUOK_MBBL",
     "label": "Cushing crude stocks", "short": "Cushing",
     "kind": "stock", "scale": 0.001, "unit": "MMbbl",
     "seasonal": True, "bullish": "low", "chart": True,
     "note": "WTI delivery hub — low/falling Cushing tightens the front spread"},
    {"id": "spr", "series": "WCSSTUS1",
     "label": "Strategic Petroleum Reserve", "short": "SPR",
     "kind": "stock", "scale": 0.001, "unit": "MMbbl",
     "seasonal": False, "bullish": None},
    {"id": "production", "series": "WCRFPUS2",
     "label": "US crude production", "short": "Production",
     "kind": "flow", "scale": 0.001, "unit": "Mb/d",
     "seasonal": False, "bullish": "low"},
    {"id": "runs", "series": "WCRRIUS2",
     "label": "Refiner crude runs", "short": "Refinery runs",
     "kind": "flow", "scale": 0.001, "unit": "Mb/d",
     "seasonal": True, "bullish": "high"},
    {"id": "utilization", "series": "WPULEUS3",
     "label": "Refinery utilization", "short": "Refinery util",
     "kind": "pct", "scale": 1.0, "unit": "%",
     "seasonal": True, "bullish": "high"},
    {"id": "exports", "series": "WCREXUS2",
     "label": "US crude exports", "short": "Exports",
     "kind": "flow", "scale": 0.001, "unit": "Mb/d",
     "seasonal": False, "bullish": "high"},
    {"id": "imports", "series": "WCEIMUS2",
     "label": "US crude imports", "short": "Imports",
     "kind": "flow", "scale": 0.001, "unit": "Mb/d",
     "seasonal": False, "bullish": "low"},
]


def crude_balance_components() -> List[Dict[str, Any]]:
    """Static metadata for the US crude-balance components (no network)."""
    return [dict(c) for c in _CRUDE_BALANCE]


def eia_crude_balance(weeks: int = 280) -> Any:
    """US crude supply/demand balance from the EIA weekly supply route.

    One request fetches every component (Cushing, SPR, production, runs,
    utilization, exports, imports). Returns {series_id: [{date, value}]},
    oldest-first and scaled to display units (~280 weeks covers a 5y band).
    """
    key = eia_key()
    if not key:
        return {"error": "no EIA API key (set EIA_API_KEY or eia_api_key.txt)"}
    series_ids = [c["series"] for c in _CRUDE_BALANCE]
    scale_by = {c["series"]: c["scale"] for c in _CRUDE_BALANCE}
    start = (datetime.now() - timedelta(weeks=weeks + 8)).strftime("%Y-%m-%d")
    url = f"{_EIA_BASE}/{_EIA_SUPPLY_ROUTE}/data/"
    params = {
        "api_key": key, "frequency": "weekly", "data[0]": "value",
        "facets[series][]": series_ids, "start": start,
        "sort[0][column]": "period", "sort[0][direction]": "desc",
        "length": 5000,
    }
    r = _HTTP.get(url, params=params, timeout=30,
                  headers={"User-Agent": "FinceptCommoditiesLocal/1.0",
                           "Accept": "application/json"})
    r.raise_for_status()
    rows = (r.json().get("response") or {}).get("data") or []
    by_series: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in series_ids}
    for row in rows:
        sid = row.get("series")
        v = _f(row.get("value"))
        if sid in by_series and v is not None and row.get("period"):
            by_series[sid].append({"date": str(row["period"]),
                                    "value": v * scale_by[sid]})
    for sid in by_series:
        by_series[sid].reverse()  # API returns newest first
    return by_series


def cot(commodity: str, weeks: int = 156,
        report_type: str = "disaggregated") -> Any:
    """COT records from the CFTC public reporting API (exact contract code)."""
    spec = get_spec(commodity)
    if not spec:
        return {"error": "unknown commodity"}
    code = spec.get("cftc_code")
    if not code:
        return {"error": "no COT mapping for this commodity"}
    res = _CFTC_RES.get(report_type, "kh3c-gbw2")
    start = (datetime.now() - timedelta(weeks=weeks + 8)).strftime("%Y-%m-%d")
    where = (f"cftc_contract_market_code='{code}' "
             f"AND report_date_as_yyyy_mm_dd > '{start}'")
    url = f"https://publicreporting.cftc.gov/resource/{res}.json"
    params = {"$where": where,
              "$order": "report_date_as_yyyy_mm_dd ASC", "$limit": 400}
    r = _HTTP.get(url, params=params, timeout=30,
                  headers={"User-Agent": "FinceptCommoditiesLocal/1.0",
                           "Accept": "application/json"})
    r.raise_for_status()
    return r.json()
