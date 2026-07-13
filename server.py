"""Fincept Commodities — local dashboard server.

A lightweight, dependency-free (stdlib http.server) local web server that
presents commodity data and analytics in the browser. Fully local: it talks
only to free public data APIs and never to any login/backend service.

Run:
    python server.py [port]           (default 8848; opens the browser)
    python server.py 9000 --no-browser

API:
    /api/overview[?sector=energy][&fresh=1]
    /api/commodity/{id}                       all panels, fetched in parallel
    /api/commodity/{id}/{history|curve|seasonality|cot|inventory|balance}
    /api/spreads[?fresh=1]                    crack / crush / WTI-Brent / gold-silver
    /api/energy-chemicals                     energy/petrochemical product map
    /api/energy-chemicals/product/{id}        product context and trade lens
    /api/news[?topic=energy][&product=wti]    public news sources + watch briefs
    /api/lpg/*                                local licensed/public LPG workspace

Caching: network fetches are cached per (kind, commodity) with TTLs matched to
how often the data actually changes, plus a stale-while-revalidate window —
within it, stale data is served instantly while one background thread
refreshes. `fresh=1` bypasses the cache (the ↻ button).
"""

import json
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sources  # noqa: E402
import energy_chemicals  # noqa: E402
from analytics.config import list_commodities, list_sectors, get_spec  # noqa: E402
from analytics.term_structure import TermStructureAnalyzer, calendar_spreads  # noqa: E402
from analytics.seasonality import SeasonalityAnalyzer  # noqa: E402
from analytics.positioning import COTAnalyzer  # noqa: E402
from analytics.risk import CommodityRiskAnalyzer  # noqa: E402
from analytics.spreads import SpreadAnalyzer  # noqa: E402
from analytics.inventory import InventoryAnalyzer  # noqa: E402
from lpg import LpgService  # noqa: E402
from lpg.exporting import to_csv, to_xlsx  # noqa: E402
from lpg.refresh_jobs import RefreshBusy, RefreshJobManager  # noqa: E402
from lpg.workflow import LpgRefreshWorkflow  # noqa: E402

# TTLs matched to source update frequency; swr = extra window where stale data
# is served immediately while one background thread refreshes it.
OVERVIEW_TTL, OVERVIEW_SWR = 60, 600
HISTORY_TTL, HISTORY_SWR = 300, 600
CURVE_TTL, CURVE_SWR = 300, 600
SEASON_TTL, SEASON_SWR = 86400, 86400    # 10y monthly stats change ~monthly
COT_TTL, COT_SWR = 21600, 86400          # COT is published weekly (Fri)
INV_TTL, INV_SWR = 21600, 86400          # EIA inventories are weekly (Wed/Thu)
NEWS_TTL, NEWS_SWR = 900, 1800           # public RSS/news search

_CACHE = {}          # key -> (fetched_at, value)
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS = {}      # key -> Lock, so concurrent misses coalesce into one fetch
_REFRESHING = set()  # keys with an in-flight background refresh

_LPG_RUNTIME = None
_LPG_RUNTIME_LOCK = threading.Lock()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


def _key_lock(key):
    with _CACHE_LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = _KEY_LOCKS[key] = threading.Lock()
        return lock


def _store(key, value):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def _refresh_async(key, fn):
    with _CACHE_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def run():
        try:
            with _key_lock(key):
                _store(key, fn())
        except Exception:  # noqa: BLE001 — keep serving the stale value
            pass
        finally:
            with _CACHE_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=run, daemon=True).start()


def cached(key, ttl, fn, swr=0, fresh=False):
    """Coalesced TTL cache with stale-while-revalidate.

    Age < ttl: cached value. ttl <= age < ttl+swr: stale value immediately,
    one background refresh. Otherwise: blocking fetch (one per key; concurrent
    callers wait and reuse it). fresh=True forces a blocking refetch.
    """
    if not fresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit:
            age = time.time() - hit[0]
            if age < ttl:
                return hit[1]
            if age < ttl + swr:
                _refresh_async(key, fn)
                return hit[1]
    with _key_lock(key):
        if not fresh:
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        value = fn()
        _store(key, value)
        return value


# ---------------- cached data fetchers (network layer) ----------------

def hist_1y(cid, fresh=False):
    return cached(f"h1y:{cid}", HISTORY_TTL,
                  lambda: sources.history(cid, "1y", "1d"),
                  swr=HISTORY_SWR, fresh=fresh)


def hist_long(cid, fresh=False):
    # SeasonalityAnalyzer caps its window at 10 years, so 10y monthly is enough.
    return cached(f"h10y:{cid}", SEASON_TTL,
                  lambda: sources.history(cid, "10y", "1mo"),
                  swr=SEASON_SWR, fresh=fresh)


# ---------------- panel builders ----------------

def panel_history(cid, fresh=False):
    spec = get_spec(cid)
    out = {
        "id": spec["id"], "name": spec["name"], "sector": spec["sector"],
        "exchange": spec["exchange"], "quote_unit": spec["quote_unit"],
        "contract": {"size": spec["contract_size"], "size_unit": spec["size_unit"]},
        "updated": int(time.time()),
    }
    try:
        hist = hist_1y(spec["id"], fresh)
    except Exception as exc:  # noqa: BLE001
        hist, out["history_error"] = [], str(exc)
    out["history"] = hist if isinstance(hist, list) else []
    try:
        if len(out["history"]) > 20:
            out["risk"] = CommodityRiskAnalyzer().analyze(out["history"])
        else:
            out["risk"] = {"success": False, "error": "insufficient history"}
    except Exception as exc:  # noqa: BLE001
        out["risk"] = {"success": False, "error": str(exc)}
    return out


def panel_curve(cid, fresh=False):
    spec = get_spec(cid)
    out = {"id": cid, "quote_unit": spec["quote_unit"] if spec else None,
           "updated": int(time.time())}
    try:
        c = cached(f"curve:{cid}", CURVE_TTL, lambda: sources.curve(cid),
                   swr=CURVE_SWR, fresh=fresh)
    except Exception as exc:  # noqa: BLE001
        out["curve"] = []
        out["term_structure"] = {"success": False, "error": str(exc)}
        out["calendar_spreads"] = {"success": False, "error": str(exc)}
        return out
    out["curve"] = c or []
    no_curve = {"success": False, "error": "no curve data"}
    out["term_structure"] = TermStructureAnalyzer().analyze(c) if c else no_curve
    # Calendar spreads ride on the same curve fetch (no extra network call).
    out["calendar_spreads"] = calendar_spreads(c) if c else dict(no_curve)
    return out


def panel_balance(cid, fresh=False):
    """US crude supply/demand balance (WTI). Reuses InventoryAnalyzer per
    EIA series so each component gets its level, weekly change, streak and
    5-year band; Cushing also carries a seasonal-band chart."""
    out = {"id": cid, "updated": int(time.time())}
    if cid != "wti":
        out["balance"] = {"success": False,
                          "error": "crude balance is US/WTI-specific"}
        return out
    try:
        data = cached("balance:wti", INV_TTL, sources.eia_crude_balance,
                      swr=INV_SWR, fresh=fresh)
    except Exception as exc:  # noqa: BLE001
        out["balance"] = {"success": False, "error": str(exc)}
        return out
    if isinstance(data, dict) and data.get("error"):
        out["balance"] = {"success": False, "error": data["error"]}
        return out

    analyzer = InventoryAnalyzer()
    components, cushing = [], None
    for c in sources.crude_balance_components():
        recs = data.get(c["series"]) if isinstance(data, dict) else None
        comp = {k: c[k] for k in ("id", "label", "short", "unit", "kind",
                                  "bullish", "seasonal")}
        if c.get("note"):
            comp["note"] = c["note"]
        if isinstance(recs, list) and len(recs) >= 2:
            an = analyzer.analyze(recs, unit=c["unit"], band_years=5)
            if an.get("success"):
                comp["current"] = an.get("current_level")
                comp["last_change"] = an.get("last_change")
                comp["streak"] = an.get("streak")
                comp["as_of"] = an.get("as_of")
                band = an.get("five_year_band") or {}
                comp["vs_avg_pct"] = band.get("vs_avg_pct")
                comp["position"] = band.get("position")
                comp["zscore"] = band.get("zscore")
                if c.get("chart"):
                    cushing = {"label": c["label"], "unit": c["unit"],
                               "five_year_band": an.get("five_year_band"),
                               "seasonal_chart": an.get("seasonal_chart")}
            else:
                comp["error"] = an.get("error")
        else:
            comp["error"] = "no data"
        components.append(comp)

    as_of = max((c["as_of"] for c in components if c.get("as_of")), default=None)
    out["balance"] = {"success": True, "as_of": as_of,
                      "components": components, "cushing": cushing}
    return out


def panel_seasonality(cid, fresh=False):
    out = {"id": cid, "updated": int(time.time())}
    try:
        long_h = hist_long(cid, fresh)
        src = long_h if isinstance(long_h, list) and long_h else hist_1y(cid, fresh)
        out["seasonality"] = SeasonalityAnalyzer().analyze(src)
    except Exception as exc:  # noqa: BLE001
        out["seasonality"] = {"success": False, "error": str(exc)}
    return out


def panel_cot(cid, fresh=False):
    spec = get_spec(cid)
    out = {"id": cid, "updated": int(time.time())}
    if not spec.get("cftc_code"):
        out["positioning"] = {"success": False,
                              "error": "no COT mapping for this commodity"}
        return out
    try:
        recs = cached(f"cot:{cid}", COT_TTL, lambda: sources.cot(cid),
                      swr=COT_SWR, fresh=fresh)
        if isinstance(recs, list) and recs:
            out["positioning"] = COTAnalyzer().analyze(recs)
        else:
            msg = recs.get("error") if isinstance(recs, dict) else "no COT data"
            out["positioning"] = {"success": False, "error": msg}
    except Exception as exc:  # noqa: BLE001
        out["positioning"] = {"success": False, "error": str(exc)}
    return out


def panel_inventory(cid, fresh=False):
    out = {"id": cid, "updated": int(time.time())}
    ses = sources.inventory_spec(cid)
    if not ses:
        out["inventory"] = {"success": False,
                            "error": "no EIA series for this commodity"}
        return out
    try:
        recs = cached(f"inv:{cid}", INV_TTL, lambda: sources.eia_inventory(cid),
                      swr=INV_SWR, fresh=fresh)
        if isinstance(recs, list) and recs:
            inv = InventoryAnalyzer().analyze(recs, unit=ses["unit"])
            if inv.get("success"):
                inv["series_label"] = ses["label"]
                inv["source_series"] = ses["series"]
            out["inventory"] = inv
        else:
            msg = recs.get("error") if isinstance(recs, dict) else "no EIA data"
            out["inventory"] = {"success": False, "error": msg}
    except Exception as exc:  # noqa: BLE001
        out["inventory"] = {"success": False, "error": str(exc)}
    return out


_PANELS = {"history": panel_history, "curve": panel_curve,
           "seasonality": panel_seasonality, "cot": panel_cot,
           "inventory": panel_inventory, "balance": panel_balance}


def build_detail(cid, fresh=False):
    """All panels for one commodity, fetched in parallel (legacy aggregate shape)."""
    spec = get_spec(cid)
    with ThreadPoolExecutor(max_workers=len(_PANELS)) as pool:
        futures = {name: pool.submit(fn, spec["id"], fresh)
                   for name, fn in _PANELS.items()}
        parts = {}
        for name, fut in futures.items():
            try:
                parts[name] = fut.result()
            except Exception as exc:  # noqa: BLE001
                parts[name] = {"error": str(exc)}

    out = {
        "id": spec["id"], "name": spec["name"], "sector": spec["sector"],
        "exchange": spec["exchange"], "quote_unit": spec["quote_unit"],
        "contract": {"size": spec["contract_size"], "size_unit": spec["size_unit"]},
        "updated": int(time.time()),
    }
    fail = lambda part, what: {"success": False,                  # noqa: E731
                               "error": part.get("error", f"{what} failed")}
    out["history"] = parts["history"].get("history", [])
    out["risk"] = parts["history"].get("risk", fail(parts["history"], "history"))
    out["curve"] = parts["curve"].get("curve", [])
    out["term_structure"] = parts["curve"].get(
        "term_structure", fail(parts["curve"], "curve"))
    out["calendar_spreads"] = parts["curve"].get(
        "calendar_spreads", fail(parts["curve"], "calendar_spreads"))
    out["balance"] = parts["balance"].get("balance", fail(parts["balance"], "balance"))
    out["seasonality"] = parts["seasonality"].get(
        "seasonality", fail(parts["seasonality"], "seasonality"))
    out["positioning"] = parts["cot"].get(
        "positioning", fail(parts["cot"], "positioning"))
    out["inventory"] = parts["inventory"].get(
        "inventory", fail(parts["inventory"], "inventory"))
    return out


# ---------------- overview ----------------

def build_overview_rows():
    items = list_commodities()
    quotes = sources.batch_quotes([i["id"] for i in items])
    qmap = {q["id"]: q for q in quotes}
    rows = []
    for it in items:
        q = qmap.get(it["id"])
        rows.append({
            "id": it["id"], "name": it["name"], "sector": it["sector"],
            "exchange": it["exchange"], "quote_unit": it["quote_unit"],
            "price": q["price"] if q else None,
            "change": q["change"] if q else None,
            "change_percent": q["change_percent"] if q else None,
            "volume": q["volume"] if q else None,
        })
    return {"updated": int(time.time()), "sectors": list_sectors(), "rows": rows}


def overview_payload(sector=None, fresh=False):
    """Sector views are filtered from one cached all-commodities fetch."""
    if sector and sector not in set(list_sectors()):
        return {"error": f"unknown sector '{sector}'", "sectors": list_sectors()}
    data = cached("ov", OVERVIEW_TTL, build_overview_rows,
                  swr=OVERVIEW_SWR, fresh=fresh)
    rows = data["rows"]
    if sector:
        rows = [r for r in rows if r["sector"] == sector]
    return dict(data, rows=rows, count=len(rows))


# ---------------- spreads ----------------

_SPREAD_DEFS = [
    {"key": "crack_321", "title": "WTI 3-2-1 Crack Spread", "unit": "USD/bbl",
     "legs": ["wti", "rbob", "heating_oil"],
     "note": "Refining margin: 3 bbl WTI → 2 bbl gasoline + 1 bbl distillate"},
    {"key": "brent_crack_321", "title": "Brent 3-2-1 Crack Spread", "unit": "USD/bbl",
     "legs": ["brent", "rbob", "heating_oil"],
     "note": "Refining margin off the global benchmark: 3 Brent → 2 gasoline + 1 distillate"},
    {"key": "gasoline_crack", "title": "Gasoline Crack (RBOB − WTI)", "unit": "USD/bbl",
     "legs": ["wti", "rbob"],
     "note": "Single-cut margin: RBOB×42 − WTI. The gasoline pull on the barrel"},
    {"key": "distillate_crack", "title": "Distillate Crack (ULSD − WTI)", "unit": "USD/bbl",
     "legs": ["wti", "heating_oil"],
     "note": "Single-cut margin: ULSD×42 − WTI. The diesel pull on the barrel"},
    {"key": "wti_brent", "title": "WTI − Brent", "unit": "USD/bbl",
     "legs": ["wti", "brent"],
     "note": "US inland light-sweet vs waterborne global benchmark; ≈ export arb"},
    {"key": "gold_silver", "title": "Gold / Silver Ratio", "unit": "ratio",
     "legs": ["gold", "silver"],
     "note": "Ounces of silver per ounce of gold"},
    {"key": "board_crush", "title": "Soybean Board Crush", "unit": "USD/bu",
     "legs": ["soybeans", "soybean_meal", "soybean_oil"],
     "note": "Processing margin: beans → meal + oil"},
]


def build_spreads(fresh=False):
    ids = sorted({leg for s in _SPREAD_DEFS for leg in s["legs"]})
    hists, errors = {}, {}
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        futures = {cid: pool.submit(hist_1y, cid, fresh) for cid in ids}
        for cid, fut in futures.items():
            try:
                h = fut.result()
                if isinstance(h, list) and h:
                    hists[cid] = h
                else:
                    errors[cid] = "no history"
            except Exception as exc:  # noqa: BLE001
                errors[cid] = str(exc)

    analyzer = SpreadAnalyzer()
    out = []
    for sdef in _SPREAD_DEFS:
        missing = [leg for leg in sdef["legs"] if leg not in hists]
        if missing:
            result = {"success": False,
                      "error": f"missing history: {', '.join(missing)}"}
        elif sdef["key"] == "crack_321":
            result = analyzer.crack_spread_series(
                hists["wti"], hists["rbob"], hists["heating_oil"])
        elif sdef["key"] == "brent_crack_321":
            result = analyzer.crack_spread_series(
                hists["brent"], hists["rbob"], hists["heating_oil"])
        elif sdef["key"] == "gasoline_crack":
            result = analyzer.single_crack_series(hists["wti"], hists["rbob"], "RBOB")
        elif sdef["key"] == "distillate_crack":
            result = analyzer.single_crack_series(hists["wti"], hists["heating_oil"], "ULSD")
        elif sdef["key"] == "wti_brent":
            result = analyzer.diff_spread(hists["wti"], hists["brent"],
                                          "WTI", "Brent")
        elif sdef["key"] == "gold_silver":
            result = analyzer.ratio_spread(hists["gold"], hists["silver"],
                                           "Gold", "Silver")
        else:
            result = analyzer.crush_spread_series(
                hists["soybeans"], hists["soybean_meal"], hists["soybean_oil"])
        out.append({"key": sdef["key"], "title": sdef["title"],
                    "unit": sdef["unit"], "note": sdef["note"],
                    "result": result})
    return {"updated": int(time.time()), "spreads": out,
            "fetch_errors": errors or None}


# ---------------- http ----------------

def _is_fresh(query):
    return (query.get("fresh") or [""])[0] in ("1", "true", "yes")


def _lpg_runtime():
    """Build the LPG database/workflow only when its API is first used."""
    global _LPG_RUNTIME
    if _LPG_RUNTIME is None:
        with _LPG_RUNTIME_LOCK:
            if _LPG_RUNTIME is None:
                service = LpgService()
                workflow = LpgRefreshWorkflow(service=service)
                jobs = RefreshJobManager(workflow.refresh)
                _LPG_RUNTIME = (service, workflow, jobs)
    return _LPG_RUNTIME


def _query_value(query, name, default=None):
    values = query.get(name)
    if not values:
        return default
    value = str(values[0]).strip()
    return value if value else default


def _query_int(query, name, default=None, minimum=0, maximum=5000):
    raw = _query_value(query, name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"query parameter '{name}' must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"query parameter '{name}' must be between {minimum} and {maximum}"
        )
    return value


def _query_fields(query, names):
    return {
        name: value
        for name in names
        if (value := _query_value(query, name)) is not None
    }


def _entitlement_filter(query, name="entitlement"):
    value = _query_value(query, name)
    if value in (None, "all"):
        return None
    allowed = {"entitled", "unentitled", "pending_review", "retired", "error"}
    if value not in allowed:
        raise ValueError(f"query parameter '{name}' has an invalid entitlement state")
    return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code, body, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, rel):
        path = os.path.join(HERE, *rel.split("/"))
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as fh:
            self._send(200, fh.read(), _CONTENT_TYPES.get(ext, "text/plain"))

    def _lpg_error(self, exc):
        if isinstance(exc, RefreshBusy):
            return self._json({"error": str(exc), "job": exc.job}, 409)
        if isinstance(exc, PermissionError):
            return self._json({"error": str(exc)}, 403)
        if isinstance(exc, KeyError):
            message = exc.args[0] if exc.args else str(exc)
            return self._json({"error": str(message)}, 404)
        if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
            return self._json({"error": str(exc)}, 400)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return self._json({"error": str(exc) or "internal server error"}, 500)

    def _lpg_get(self, path, query):
        try:
            service, _workflow, jobs = _lpg_runtime()

            if path == "/api/lpg/summary":
                return self._json(service.summary(as_of=_query_value(query, "as_of")))

            if path == "/api/lpg/series":
                filters = _query_fields(query, (
                    "product", "market", "region", "source", "canonical_key",
                    "quote_kind", "q",
                ))
                entitlement = _entitlement_filter(query, "entitlement_state")
                if entitlement:
                    filters["entitlement_state"] = entitlement
                active = _query_value(query, "active")
                if active is not None:
                    if active.lower() not in {"0", "1", "true", "false", "yes", "no"}:
                        raise ValueError("query parameter 'active' must be a boolean")
                    filters["active"] = active
                limit = _query_int(query, "limit", minimum=1, maximum=5000)
                offset = _query_int(query, "offset", minimum=0, maximum=10_000_000)
                if limit is not None:
                    filters["limit"] = limit
                if offset is not None:
                    filters["offset"] = offset
                return self._json(service.list_series(filters))

            series_prefix = "/api/lpg/series/"
            history_suffix = "/history"
            if path.startswith(series_prefix) and path.endswith(history_suffix):
                raw_id = path[len(series_prefix):-len(history_suffix)].strip("/")
                if not raw_id or "/" in raw_id:
                    raise KeyError("unknown LPG series route")
                series_id = unquote(raw_id)
                end = _query_value(query, "end") or _query_value(query, "as_of")
                limit = _query_int(query, "limit", minimum=1, maximum=5000)
                payload = service.series_history(
                    series_id,
                    start=_query_value(query, "start"),
                    end=end,
                    limit=limit,
                    bate=_query_value(query, "bate"),
                )
                return self._json(payload)

            if path == "/api/lpg/curves":
                return self._json(service.curves(
                    as_of=_query_value(query, "as_of"),
                    series_id=_query_value(query, "series_id"),
                ))

            if path == "/api/lpg/spreads":
                window = _query_int(
                    query, "window", default=252, minimum=2, maximum=5000,
                )
                return self._json(service.spreads(
                    as_of=_query_value(query, "as_of"), window=window,
                ))

            if path == "/api/lpg/news":
                filters = _query_fields(query, (
                    "topic", "product", "region", "source", "direction",
                    "importance", "start", "q",
                ))
                end = _query_value(query, "end") or _query_value(query, "as_of")
                if end:
                    filters["end"] = end
                limit = _query_int(query, "limit", minimum=1, maximum=5000)
                offset = _query_int(query, "offset", minimum=0, maximum=10_000_000)
                if limit is not None:
                    filters["limit"] = limit
                if offset is not None:
                    filters["offset"] = offset
                return self._json(service.news(filters))

            if path == "/api/lpg/explorer":
                dataset = _query_value(query, "dataset", "observations")
                filters = _query_fields(query, (
                    "q", "name", "series_id", "start", "end", "as_of",
                ))
                entitlement = _entitlement_filter(query)
                if entitlement:
                    filters["entitlement"] = entitlement
                limit = _query_int(query, "limit", minimum=1, maximum=5000)
                offset = _query_int(query, "offset", minimum=0, maximum=10_000_000)
                if limit is not None:
                    filters["limit"] = limit
                if offset is not None:
                    filters["offset"] = offset
                return self._json(service.explorer(dataset=dataset, **filters))

            if path == "/api/lpg/status":
                job_limit = _query_int(
                    query, "job_limit", default=20, minimum=1, maximum=50,
                )
                payload = service.status()
                payload["jobs"] = jobs.list(job_limit)
                payload["active_job"] = jobs.active()
                return self._json(payload)

            refresh_prefix = "/api/lpg/refresh/"
            if path.startswith(refresh_prefix):
                raw_id = path[len(refresh_prefix):].strip("/")
                if not raw_id or "/" in raw_id:
                    raise KeyError("unknown LPG refresh job")
                job = jobs.get(unquote(raw_id))
                if job is None:
                    raise KeyError(f"unknown LPG refresh job: {unquote(raw_id)}")
                return self._json({"job": job})

            if path == "/api/lpg/export":
                view = _query_value(query, "view", "cockpit").lower()
                allowed_views = {
                    "cockpit", "curves", "history", "moc", "news",
                    "explorer", "status",
                }
                if view not in allowed_views:
                    raise ValueError(f"unsupported LPG export view: {view}")
                export_format = _query_value(query, "format", "csv").lower()
                if export_format not in {"csv", "xlsx"}:
                    raise ValueError("export format must be csv or xlsx")
                filters = _query_fields(query, (
                    "as_of", "series_id", "dataset", "q", "name", "start",
                    "end", "bate", "topic", "product", "region", "source",
                    "direction", "importance",
                ))
                entitlement = _entitlement_filter(query)
                if entitlement:
                    filters["entitlement"] = entitlement
                filters["limit"] = _query_int(
                    query, "limit", default=5000, minimum=1, maximum=5000,
                )
                filters["offset"] = _query_int(
                    query, "offset", default=0, minimum=0, maximum=10_000_000,
                )
                rows = service.export_rows(view, **filters)
                filename = f"fincept-lpg-{view}.{export_format}"
                headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
                if export_format == "xlsx":
                    body = to_xlsx(rows, sheet_name=f"LPG {view.title()}")
                    ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                else:
                    body = to_csv(rows)
                    ctype = "text/csv; charset=utf-8"
                return self._send(200, body, ctype, headers=headers)

            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - local API boundary
            return self._lpg_error(exc)

    def _read_json_body(self, maximum=65_536):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length, 10)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > maximum:
            raise ValueError(f"JSON request body must be at most {maximum} bytes")
        if length == 0:
            return {}
        try:
            body = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("JSON request body must be UTF-8") from exc
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object")
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        try:
            if path in ("/", "/index.html"):
                return self._file("web/index.html")
            if path in ("/style.css", "/app.js"):
                return self._file("web/" + path.lstrip("/"))
            if path in ("/ENERGY.md", "/README.md"):
                return self._file(path.lstrip("/"))
            if path.startswith("/api/lpg/"):
                return self._lpg_get(path, query)
            if path == "/api/overview":
                sector = (query.get("sector") or [None])[0]
                return self._json(overview_payload(sector, _is_fresh(query)))
            if path == "/api/spreads":
                return self._json(build_spreads(_is_fresh(query)))
            if path == "/api/energy-chemicals":
                return self._json(energy_chemicals.energy_hub_payload())
            if path.startswith("/api/energy-chemicals/product/"):
                product = unquote(path[len("/api/energy-chemicals/product/"):]).strip("/")
                payload = energy_chemicals.product_context(product)
                return self._json(payload, 200 if payload.get("success") else 404)
            if path == "/api/news":
                topic = (query.get("topic") or ["energy"])[0]
                product = (query.get("product") or [None])[0]
                try:
                    limit = int((query.get("limit") or ["40"])[0])
                except ValueError:
                    limit = 40
                key = f"news:{topic}:{product or ''}:{max(5, min(limit, 80))}"
                return self._json(cached(
                    key, NEWS_TTL,
                    lambda: energy_chemicals.news_payload(topic, product, limit),
                    swr=NEWS_SWR, fresh=_is_fresh(query)))
            if path.startswith("/api/commodity/"):
                rest = unquote(path[len("/api/commodity/"):]).strip("/")
                cid, _, panel = rest.partition("/")
                spec = get_spec(cid)
                if not spec:
                    return self._json({"error": f"unknown commodity '{cid}'"}, 404)
                if not panel:
                    return self._json(build_detail(spec["id"], _is_fresh(query)))
                fn = _PANELS.get(panel)
                if fn is None:
                    return self._json({"error": f"unknown panel '{panel}'"}, 404)
                return self._json(fn(spec["id"], _is_fresh(query)))
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/lpg/refresh":
            return self._json({"error": "not found"}, 404)
        try:
            payload = self._read_json_body()
            scope = payload.get("scope", "all")
            if not isinstance(scope, str):
                raise ValueError("refresh scope must be a string")
            _service, _workflow, jobs = _lpg_runtime()
            job = jobs.start(scope.strip().lower() or "all")
            return self._json({"job": job}, 202)
        except Exception as exc:  # noqa: BLE001 - local API boundary
            return self._lpg_error(exc)


def _warm_up():
    """Pay the yfinance import + first overview fetch at startup, not on the
    first browser request."""
    try:
        overview_payload()
    except Exception:  # noqa: BLE001
        pass


def main():
    port = 8848
    args = [a for a in sys.argv[1:] if a != "--no-browser"]
    if args:
        try:
            port = int(args[0])
        except ValueError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Fincept Commodities — local dashboard running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=_warm_up, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
