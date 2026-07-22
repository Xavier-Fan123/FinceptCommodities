"""LPG event intelligence, asset exposure, and baseline-aware alerting.

The module deliberately separates observed evidence from inferred trading
impact.  News rows remain the evidence of record; durable intelligence events
add a conservative geographic and commercial lens without pretending that
public discovery is live terminal, vessel, or Platts data.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import finite_number, json_safe, normalize_timestamp, utc_now


INTELLIGENCE_VERSION = "lpg-situation-v1"
SCENARIO_ENGINE_VERSION = "lpg-scenario-v1"


def _asset(
    asset_id: str,
    name: str,
    kind: str,
    region: str,
    lat: float,
    lon: float,
    aliases: Sequence[str],
    affected_series: Sequence[str],
    *,
    geo_precision: str = "port_area",
    description: str = "",
) -> Dict[str, Any]:
    return {
        "id": asset_id,
        "name": name,
        "kind": kind,
        "region": region,
        "latitude": lat,
        "longitude": lon,
        "aliases": tuple(aliases),
        "affected_series": tuple(affected_series),
        "geo_precision": geo_precision,
        "description": description,
    }


# Coordinates identify a waterway, city, hub, or port area.  They are not
# represented as exact berth/facility coordinates and the precision field is
# always exposed to clients.
ASSETS: Tuple[Dict[str, Any], ...] = (
    _asset("hormuz", "Strait of Hormuz", "chokepoint", "Middle East", 26.56, 56.25,
           ("strait of hormuz", "hormuz"),
           ("FOB_AG_PROPANE", "FOB_AG_BUTANE", "CP_PROPANE", "CP_BUTANE", "VLGC_AG_JAPAN"),
           geo_precision="waterway", description="Primary Arab Gulf LPG export chokepoint."),
    _asset("bab_el_mandeb", "Bab-el-Mandeb", "chokepoint", "Middle East", 12.58, 43.33,
           ("bab el-mandeb", "bab-el-mandeb", "babelmandeb", "red sea attacks"),
           ("VLGC_AG_JAPAN", "VLGC_USGC_NA"), geo_precision="waterway"),
    _asset("suez", "Suez Canal", "chokepoint", "Middle East", 30.45, 32.35,
           ("suez canal", "suez"), ("VLGC_AG_JAPAN", "VLGC_USGC_NA"),
           geo_precision="waterway"),
    _asset("red_sea", "Red Sea", "route_area", "Middle East", 20.0, 38.5,
           ("red sea",), ("VLGC_AG_JAPAN", "VLGC_USGC_NA"), geo_precision="sea_area"),
    _asset("malacca", "Strait of Malacca", "chokepoint", "Southeast Asia", 2.55, 101.45,
           ("strait of malacca", "malacca strait", "malacca"),
           ("VLGC_AG_JAPAN", "FEI_PROPANE", "CFR_NA_BUTANE"), geo_precision="waterway"),
    _asset("lombok", "Lombok Strait", "chokepoint", "Southeast Asia", -8.55, 115.75,
           ("lombok strait", "lombok"), ("VLGC_AG_JAPAN",), geo_precision="waterway"),
    _asset("panama", "Panama Canal", "chokepoint", "Americas", 9.08, -79.68,
           ("panama canal", "panama"), ("VLGC_USGC_NA",), geo_precision="waterway"),
    _asset("cape_good_hope", "Cape of Good Hope", "chokepoint", "Africa", -34.36, 18.47,
           ("cape of good hope", "cape route"), ("VLGC_USGC_NA", "VLGC_AG_JAPAN"),
           geo_precision="waterway"),
    _asset("ras_tanura", "Ras Tanura port area", "export_port", "Middle East", 26.64, 50.16,
           ("ras tanura",),
           ("CP_PROPANE", "CP_BUTANE", "FOB_AG_PROPANE", "FOB_AG_BUTANE", "VLGC_AG_JAPAN")),
    _asset("yanbu", "Yanbu port area", "export_port", "Middle East", 24.09, 38.05,
           ("yanbu",), ("CP_PROPANE", "CP_BUTANE", "FOB_AG_PROPANE", "FOB_AG_BUTANE")),
    _asset("ruwais", "Ruwais port area", "export_port", "Middle East", 24.13, 52.73,
           ("ruwais",), ("FOB_AG_PROPANE", "FOB_AG_BUTANE", "VLGC_AG_JAPAN")),
    _asset("mesaieed", "Mesaieed port area", "export_port", "Middle East", 24.99, 51.55,
           ("mesaieed", "mesaieed industrial city"),
           ("FOB_AG_PROPANE", "FOB_AG_BUTANE", "VLGC_AG_JAPAN")),
    _asset("saudi_arabia", "Saudi Arabia", "market_area", "Middle East", 24.0, 45.0,
           ("saudi arabia", "saudi aramco", "aramco"),
           ("CP_PROPANE", "CP_BUTANE", "FOB_AG_PROPANE", "FOB_AG_BUTANE"),
           geo_precision="country"),
    _asset("arab_gulf", "Arab Gulf", "market_area", "Middle East", 25.5, 52.0,
           ("arab gulf", "persian gulf", "middle east gulf"),
           ("FOB_AG_PROPANE", "FOB_AG_BUTANE", "VLGC_AG_JAPAN"), geo_precision="region"),
    _asset("singapore", "Singapore/Jurong port area", "trading_hub", "Southeast Asia", 1.25, 103.72,
           ("singapore", "jurong island"),
           ("CFR_SINGAPORE_LPG", "FOB_SINGAPORE_LPG", "FOB_SINGAPORE_CP_DIFF")),
    _asset("map_ta_phut", "Map Ta Phut port area", "import_port", "Southeast Asia", 12.67, 101.14,
           ("map ta phut",), ("PRESSURIZED_ASIA_PROPANE", "FEI_PROPANE")),
    _asset("philippines", "Philippines", "market_area", "Southeast Asia", 12.5, 122.0,
           ("philippines", "manila"), ("CFR_PHILIPPINES_LPG",), geo_precision="country"),
    _asset("east_china", "East China import region", "market_area", "North Asia", 30.4, 121.3,
           ("east china", "ningbo", "zhangjiagang", "jiangsu", "zhejiang"),
           ("CFR_EAST_CHINA_PROPANE", "CFR_EAST_CHINA_BUTANE", "FOB_EAST_CHINA_LPG"),
           geo_precision="region"),
    _asset("south_china", "South China import region", "market_area", "North Asia", 22.8, 113.4,
           ("south china", "guangdong", "dongguan"),
           ("CFR_SOUTH_CHINA_PROPANE", "CFR_SOUTH_CHINA_BUTANE"), geo_precision="region"),
    _asset("japan", "Japan LPG market", "market_area", "North Asia", 35.2, 139.4,
           ("japan", "chiba"),
           ("FEI_PROPANE", "CFR_NA_BUTANE", "CFR_JAPAN_PROPANE", "CFR_JAPAN_BUTANE", "MOPJ_NAPHTHA"),
           geo_precision="country"),
    _asset("south_korea", "South Korea LPG market", "market_area", "North Asia", 35.5, 128.0,
           ("south korea", "korea", "ulsan", "yeosu"),
           ("FEI_PROPANE", "CFR_NA_BUTANE"), geo_precision="country"),
    _asset("north_asia", "North Asia LPG market", "market_area", "North Asia", 32.0, 126.0,
           ("north asia", "northeast asia", "far east index"),
           ("FEI_PROPANE", "CFR_NA_BUTANE", "VLGC_AG_JAPAN", "VLGC_USGC_NA"),
           geo_precision="region"),
    _asset("mont_belvieu", "Mont Belvieu pricing hub", "pricing_hub", "North America", 29.85, -94.89,
           ("mont belvieu", "mt belvieu", "non-lst", "non lst"),
           ("MB_PROPANE_USD_MT", "MB_BUTANE_USD_MT"), geo_precision="hub_area"),
    _asset("us_gulf_coast", "US Gulf Coast export region", "export_region", "North America", 29.3, -94.8,
           ("us gulf coast", "u.s. gulf coast", "usgc", "houston ship channel", "galena park"),
           ("MB_PROPANE_USD_MT", "MB_BUTANE_USD_MT", "FOB_USGC_LPG_2222", "VLGC_USGC_NA"),
           geo_precision="region"),
    _asset("marcus_hook", "Marcus Hook port area", "export_port", "North America", 39.82, -75.42,
           ("marcus hook",), ("MB_PROPANE_USD_MT", "VLGC_USGC_NA")),
)

ASSET_BY_ID = {item["id"]: item for item in ASSETS}


ROUTES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "ag_north_asia_malacca",
        "name": "Arab Gulf → North Asia via Malacca",
        "asset_ids": ("ras_tanura", "hormuz", "malacca", "singapore", "north_asia"),
        "affected_series": ("VLGC_AG_JAPAN", "FOB_AG_PROPANE", "FEI_PROPANE", "CFR_NA_BUTANE"),
        "status": "reference_route",
    },
    {
        "id": "ag_north_asia_lombok",
        "name": "Arab Gulf → North Asia via Lombok",
        "asset_ids": ("ras_tanura", "hormuz", "lombok", "north_asia"),
        "affected_series": ("VLGC_AG_JAPAN", "FOB_AG_PROPANE", "FEI_PROPANE"),
        "status": "reference_alternative",
    },
    {
        "id": "usgc_north_asia_panama",
        "name": "US Gulf Coast → North Asia via Panama",
        "asset_ids": ("mont_belvieu", "us_gulf_coast", "panama", "north_asia"),
        "affected_series": ("VLGC_USGC_NA", "MB_PROPANE_USD_MT", "FEI_PROPANE"),
        "status": "reference_route",
    },
    {
        "id": "usgc_north_asia_cape",
        "name": "US Gulf Coast → North Asia via Cape of Good Hope",
        "asset_ids": ("us_gulf_coast", "cape_good_hope", "malacca", "north_asia"),
        "affected_series": ("VLGC_USGC_NA", "MB_PROPANE_USD_MT", "FEI_PROPANE"),
        "status": "reference_alternative",
    },
    {
        "id": "ag_europe_red_sea",
        "name": "Arab Gulf → Europe via Red Sea/Suez",
        "asset_ids": ("ras_tanura", "hormuz", "bab_el_mandeb", "red_sea", "suez"),
        "affected_series": ("FOB_AG_PROPANE", "FOB_AG_BUTANE", "VLGC_AG_JAPAN"),
        "status": "reference_route",
    },
)

ROUTE_BY_ID = {item["id"]: item for item in ROUTES}


def _scenario_parameter(
    key: str,
    label: str,
    unit: str,
    minimum: float,
    maximum: float,
    step: float,
    default: float,
    meaning: str,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "default": default,
        "meaning": meaning,
    }


_DURATION = _scenario_parameter(
    "duration_days", "Duration", "days", 1, 90, 1, 14,
    "Assumed duration of the hypothetical disruption or demand shock.",
)
_TRANSIT = _scenario_parameter(
    "extra_transit_days", "Additional transit", "days", 0, 45, 1, 14,
    "Reference voyage-time addition; not a live routing estimate.",
)


# These are decision prompts, not forecasts.  Each template deliberately uses
# only user-controlled assumptions and disclosed reference geography.  No
# freight, price, cargo-flow, or probability value is synthesized.
SCENARIO_TEMPLATES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "hormuz_closure",
        "name": "Strait of Hormuz closure",
        "category": "chokepoint",
        "premise": "Test a temporary constraint on LPG movements through the Strait of Hormuz.",
        "asset_ids": ("hormuz", "ras_tanura", "ruwais", "mesaieed", "arab_gulf"),
        "route_ids": ("ag_north_asia_malacca", "ag_north_asia_lombok", "ag_europe_red_sea"),
        "alternative_route_ids": (),
        "affected_series": (
            "FOB_AG_PROPANE", "FOB_AG_BUTANE", "CP_PROPANE", "CP_BUTANE",
            "VLGC_AG_JAPAN", "FEI_PROPANE", "CFR_NA_BUTANE",
        ),
        "parameters": (
            _scenario_parameter("shock_pct", "Transit capacity constrained", "%", 0, 100, 5, 100,
                                "Share of reference transit capacity assumed unavailable."),
            {**_DURATION, "default": 14},
            {**_TRANSIT, "default": 14},
        ),
        "mechanisms": (
            "Arab Gulf export optionality contracts",
            "Prompt cargo replacement and freight exposure increase",
            "North Asia buyers reassess USGC and regional alternatives",
        ),
        "commercial_questions": (
            "Which nominations and lifting windows are exposed?",
            "What replacement origin, vessel, and credit capacity is available?",
            "Which benchmark and freight clauses transmit the disruption into open positions?",
        ),
        "assumptions": ("All listed Arab Gulf reference routes share Hormuz exposure.",),
    },
    {
        "id": "panama_disruption",
        "name": "Panama Canal disruption",
        "category": "chokepoint",
        "premise": "Test reduced Panama transit availability for USGC-to-North Asia LPG movements.",
        "asset_ids": ("panama", "us_gulf_coast", "north_asia", "cape_good_hope"),
        "route_ids": ("usgc_north_asia_panama",),
        "alternative_route_ids": ("usgc_north_asia_cape",),
        "affected_series": ("VLGC_USGC_NA", "MB_PROPANE_USD_MT", "MB_BUTANE_USD_MT", "FEI_PROPANE"),
        "parameters": (
            _scenario_parameter("shock_pct", "Transit capacity constrained", "%", 0, 100, 5, 40,
                                "Share of reference Panama transit capacity assumed unavailable."),
            {**_DURATION, "default": 30},
            {**_TRANSIT, "default": 12},
        ),
        "mechanisms": (
            "USGC-to-Asia voyage duration and vessel utilization increase",
            "Cape routing becomes the reference alternative",
            "Arrival timing and replacement-cargo exposure widen",
        ),
        "commercial_questions": (
            "Which cargoes have canal slots or contractual routing flexibility?",
            "How much laycan and inventory buffer absorbs a Cape diversion?",
            "Which freight basis applies after rerouting?",
        ),
        "assumptions": ("Cape routing is shown as reference context, not a recommended live route.",),
    },
    {
        "id": "red_sea_avoidance",
        "name": "Red Sea avoidance",
        "category": "shipping_security",
        "premise": "Test sustained avoidance of the Red Sea and Suez reference corridor.",
        "asset_ids": ("bab_el_mandeb", "red_sea", "suez", "cape_good_hope"),
        "route_ids": ("ag_europe_red_sea",),
        "alternative_route_ids": (),
        "affected_series": ("VLGC_AG_JAPAN", "VLGC_USGC_NA", "FOB_AG_PROPANE", "FOB_AG_BUTANE"),
        "parameters": (
            _scenario_parameter("shock_pct", "Corridor avoidance", "%", 0, 100, 5, 80,
                                "Share of reference corridor movements assumed to avoid the area."),
            {**_DURATION, "default": 30},
            {**_TRANSIT, "default": 14},
        ),
        "mechanisms": (
            "Longer voyage cycles tighten effective vessel availability",
            "Insurance, bunker, and schedule exposure rise",
            "Atlantic and Middle East cargo sequencing may change",
        ),
        "commercial_questions": (
            "Which charters permit route deviation and cost pass-through?",
            "What is the delivery-window effect of avoidance?",
            "Where does replacement tonnage become the binding constraint?",
        ),
        "assumptions": ("No live security, insurance, or vessel-routing feed is used.",),
    },
    {
        "id": "saudi_loading_reduction",
        "name": "Saudi LPG loading reduction",
        "category": "supply",
        "premise": "Test a temporary reduction in Saudi LPG loading availability.",
        "asset_ids": ("saudi_arabia", "ras_tanura", "yanbu", "hormuz"),
        "route_ids": ("ag_north_asia_malacca", "ag_north_asia_lombok", "ag_europe_red_sea"),
        "alternative_route_ids": (),
        "affected_series": (
            "CP_PROPANE", "CP_BUTANE", "FOB_AG_PROPANE", "FOB_AG_BUTANE",
            "VLGC_AG_JAPAN", "FEI_PROPANE", "CFR_NA_BUTANE",
        ),
        "parameters": (
            _scenario_parameter("shock_pct", "Loading availability reduction", "%", 0, 100, 5, 25,
                                "Assumed reduction in loading availability; not an observed export volume."),
            {**_DURATION, "default": 30},
        ),
        "mechanisms": (
            "Arab Gulf term and spot cargo availability contracts",
            "Saudi CP-linked replacement economics gain importance",
            "North Asia and Southeast Asia buyers compete for alternatives",
        ),
        "commercial_questions": (
            "Which term nominations and tolerances are exposed?",
            "Can Qatar, UAE, USGC, or regional supply substitute within delivery windows?",
            "Which positions are CP-linked versus FEI-linked?",
        ),
        "assumptions": ("The shock is user-supplied and is not derived from Saudi loading data.",),
    },
    {
        "id": "usgc_export_outage",
        "name": "USGC export outage",
        "category": "supply",
        "premise": "Test reduced US Gulf Coast LPG export availability.",
        "asset_ids": ("us_gulf_coast", "mont_belvieu", "panama", "north_asia"),
        "route_ids": ("usgc_north_asia_panama", "usgc_north_asia_cape"),
        "alternative_route_ids": (),
        "affected_series": (
            "MB_PROPANE_USD_MT", "MB_BUTANE_USD_MT", "FOB_USGC_LPG_2222",
            "VLGC_USGC_NA", "FEI_PROPANE", "CFR_NA_BUTANE",
        ),
        "parameters": (
            _scenario_parameter("shock_pct", "Export availability reduction", "%", 0, 100, 5, 35,
                                "Assumed reduction in export availability; not a terminal throughput reading."),
            {**_DURATION, "default": 21},
        ),
        "mechanisms": (
            "USGC cargo availability and terminal sequencing tighten",
            "Mont Belvieu-to-Asia arbitrage exposure changes",
            "North Asia replacement demand may shift toward Arab Gulf supply",
        ),
        "commercial_questions": (
            "Which terminal, supplier, and loading-window concentrations exist?",
            "What inventory or domestic-market flexibility remains upstream?",
            "How do open freight and destination optionality respond?",
        ),
        "assumptions": ("No live USGC terminal throughput or inventory model is applied.",),
    },
    {
        "id": "north_asia_demand_surge",
        "name": "North Asia demand surge",
        "category": "demand",
        "premise": "Test a temporary increase in North Asia LPG buying requirements.",
        "asset_ids": ("north_asia", "east_china", "south_china", "japan", "south_korea"),
        "route_ids": ("ag_north_asia_malacca", "ag_north_asia_lombok", "usgc_north_asia_panama", "usgc_north_asia_cape"),
        "alternative_route_ids": (),
        "affected_series": (
            "FEI_PROPANE", "CFR_NA_BUTANE", "CFR_EAST_CHINA_PROPANE",
            "CFR_SOUTH_CHINA_PROPANE", "VLGC_AG_JAPAN", "VLGC_USGC_NA",
        ),
        "parameters": (
            _scenario_parameter("shock_pct", "Demand increase", "%", 0, 60, 5, 15,
                                "Assumed demand increase; not an observed import or PDH run-rate change."),
            {**_DURATION, "default": 30},
        ),
        "mechanisms": (
            "Prompt North Asia cargo requirements increase",
            "Arab Gulf and USGC origin competition intensifies",
            "Freight, FEI, CP, and feedstock substitution exposure interact",
        ),
        "commercial_questions": (
            "Is the demand change driven by PDH, heating, inventory, or supply replacement?",
            "Which destination and timing windows concentrate prompt exposure?",
            "Can naphtha, olefin, or regional LPG substitution absorb the shock?",
        ),
        "assumptions": ("Demand composition and substitution are not modeled without user data.",),
    },
)

SCENARIO_BY_ID = {item["id"]: item for item in SCENARIO_TEMPLATES}


def scenario_catalog() -> Dict[str, Any]:
    """Return the disclosed, non-predictive scenario template contract."""
    return json_safe({
        "version": SCENARIO_ENGINE_VERSION,
        "state": "hypothetical_not_forecast",
        "templates": SCENARIO_TEMPLATES,
        "guardrails": [
            "Stress index is a deterministic assumption index, not a probability or price forecast.",
            "Reference assets and corridors are not live terminal or satellite AIS observations.",
            "No missing licensed assessment, freight, flow, or inventory value is synthesized.",
        ],
    })


def run_scenario(scenario_id: str, parameters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate one scenario with a deliberately simple and inspectable index."""
    template = SCENARIO_BY_ID.get(str(scenario_id or "").strip())
    if template is None:
        raise ValueError(f"unsupported LPG scenario: {scenario_id}")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping):
        raise ValueError("scenario inputs must be an object")
    schema = {item["key"]: item for item in template["parameters"]}
    unknown = sorted(set(str(key) for key in parameters) - set(schema))
    if unknown:
        raise ValueError(f"unsupported scenario input(s): {', '.join(unknown)}")
    inputs: Dict[str, float] = {}
    for key, spec in schema.items():
        raw = parameters.get(key, spec["default"])
        if isinstance(raw, bool):
            raise ValueError(f"scenario input '{key}' must be numeric")
        value = finite_number(raw)
        if value is None:
            value = float(spec["default"])
        if value < float(spec["minimum"]) or value > float(spec["maximum"]):
            raise ValueError(
                f"scenario input '{key}' must be between {spec['minimum']} and {spec['maximum']}"
            )
        inputs[key] = value

    shock_component = min(1.0, inputs["shock_pct"] / 100.0)
    duration_component = min(1.0, inputs["duration_days"] / 30.0)
    components = {
        "assumed_shock": round(shock_component * 100, 1),
        "duration": round(duration_component * 100, 1),
    }
    if "extra_transit_days" in inputs:
        transit_component = min(1.0, inputs["extra_transit_days"] / 30.0)
        components["additional_transit"] = round(transit_component * 100, 1)
        score = round(100 * (
            0.50 * shock_component + 0.25 * duration_component + 0.25 * transit_component
        ))
        formula = "round(100 * (0.50*shock_pct/100 + 0.25*min(duration_days/30,1) + 0.25*min(extra_transit_days/30,1)))"
    else:
        score = round(100 * (0.65 * shock_component + 0.35 * duration_component))
        formula = "round(100 * (0.65*shock_pct/100 + 0.35*min(duration_days/30,1)))"
    score = max(0, min(100, score))
    band = "extreme" if score >= 75 else "severe" if score >= 50 else "material" if score >= 25 else "contained"

    asset_ids = tuple(template["asset_ids"])
    route_ids = tuple(template["route_ids"])
    alternative_ids = tuple(template["alternative_route_ids"])
    return json_safe({
        "version": SCENARIO_ENGINE_VERSION,
        "scenario_state": "hypothetical_not_forecast",
        "scenario_id": template["id"],
        "name": template["name"],
        "category": template["category"],
        "premise": template["premise"],
        "inputs": inputs,
        "input_schema": template["parameters"],
        "stress_index": {
            "score": score,
            "band": band,
            "scale": "0-100 relative assumption stress",
            "meaning": "A transparent comparison aid only; it is not likelihood, VaR, price, freight, or volume impact.",
            "components": components,
            "formula": formula,
        },
        "asset_ids": asset_ids,
        "assets": [ASSET_BY_ID[item] for item in asset_ids if item in ASSET_BY_ID],
        "route_ids": route_ids,
        "routes": [ROUTE_BY_ID[item] for item in route_ids if item in ROUTE_BY_ID],
        "alternative_route_ids": alternative_ids,
        "alternative_routes": [ROUTE_BY_ID[item] for item in alternative_ids if item in ROUTE_BY_ID],
        "affected_series": template["affected_series"],
        "mechanisms": template["mechanisms"],
        "commercial_questions": template["commercial_questions"],
        "assumptions": template["assumptions"],
        "data_gaps": [
            "No live satellite AIS or vessel schedule is included.",
            "No authoritative terminal throughput or operating-status feed is included.",
            "No supply-demand elasticity, substitution, inventory, freight, or price-response model is included.",
        ],
        "generated_at": utc_now(),
    })


EVENT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("policy_sanctions", ("sanction", "tariff", "export ban", "trade restriction", "embargo")),
    ("weather_disruption", ("typhoon", "cyclone", "hurricane", "storm", "fog", "weather disruption")),
    ("terminal_disruption", ("terminal outage", "port closure", "loading delay", "force majeure",
                              "plant shutdown", "unplanned shutdown", "maintenance delay", "fire", "explosion")),
    ("shipping_disruption", ("shipping attack", "vessel seizure", "canal delay", "transit delay",
                              "freight surge", "reroute", "diversion", "blockade", "chokepoint")),
    ("supply_change", ("supply cut", "output cut", "production cut", "export halt", "export cut",
                       "production increase", "output increase", "cargo cancellation")),
    ("demand_change", ("buying tender", "import demand", "petchem demand", "cracker demand",
                       "heating demand", "stockbuild", "restocking")),
    ("pricing_signal", ("contract price", "saudi cp", "price increase", "price cut", "premium",
                        "discount", "assessment")),
)

EVENT_LABELS = {
    "policy_sanctions": "Policy / sanctions",
    "weather_disruption": "Weather disruption",
    "terminal_disruption": "Terminal / plant disruption",
    "shipping_disruption": "Shipping / chokepoint disruption",
    "supply_change": "Supply change",
    "demand_change": "Demand change",
    "pricing_signal": "Pricing signal",
    "market_development": "Market development",
}

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _list(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = [value]
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = []
    return [str(item).strip() for item in value if str(item).strip()]


def _text(article: Mapping[str, Any]) -> str:
    metadata = _mapping(article.get("metadata") or article.get("metadata_json"))
    raw = _mapping(metadata.get("raw"))
    pieces: List[str] = []
    for value in (
        article.get("headline"), article.get("title"), article.get("summary"), article.get("body"),
        article.get("region"), article.get("product"), article.get("topic"), article.get("tags"),
        metadata.get("regions"), metadata.get("products"), metadata.get("drivers"),
        raw.get("title"), raw.get("description"), raw.get("summary"),
    ):
        if isinstance(value, (list, tuple, set)):
            pieces.extend(str(item) for item in value)
        elif value not in (None, ""):
            pieces.append(str(value))
    return " ".join(pieces).lower()


def _contains(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(phrase.lower()).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _match_assets(text: str) -> List[Dict[str, Any]]:
    matched = []
    for asset in ASSETS:
        if any(_contains(text, alias) for alias in asset["aliases"]):
            matched.append(asset)
    # Prefer the more precise named location when a broad market area matched
    # only because both appeared in the same sentence.
    matched.sort(key=lambda item: (
        {"export_port": 0, "import_port": 0, "chokepoint": 1, "pricing_hub": 1,
         "trading_hub": 1, "route_area": 2, "export_region": 2, "market_area": 3}.get(item["kind"], 4),
        item["name"],
    ))
    return matched


def _event_type(text: str) -> str:
    for event_type, terms in EVENT_RULES:
        if any(_contains(text, term) for term in terms):
            return event_type
    return "market_development"


def _parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _source_boundary(article: Mapping[str, Any]) -> str:
    metadata = _mapping(article.get("metadata") or article.get("metadata_json"))
    return str(metadata.get("content_boundary") or "public_source")


def _event_key(group_key: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "_", str(group_key).lower()).strip("_")
    if clean:
        return clean[:96]
    return "event_" + hashlib.sha1(str(group_key).encode("utf-8", "replace")).hexdigest()[:20]


def _event_routes(asset_ids: Sequence[str]) -> List[Dict[str, Any]]:
    selected = set(asset_ids)
    return [route for route in ROUTES if selected.intersection(route["asset_ids"])]


def _impact(event_type: str, asset_ids: Sequence[str], route_ids: Sequence[str],
            direction: str) -> Dict[str, Any]:
    series = set()
    for asset_id in asset_ids:
        series.update(ASSET_BY_ID.get(asset_id, {}).get("affected_series") or ())
    for route_id in route_ids:
        series.update(ROUTE_BY_ID.get(route_id, {}).get("affected_series") or ())
    mechanisms = {
        "terminal_disruption": ["loading availability", "regional physical supply", "prompt differential"],
        "shipping_disruption": ["voyage duration", "freight", "arrival timing", "arbitrage netback"],
        "weather_disruption": ["port operability", "berthing/loading timing", "freight"],
        "policy_sanctions": ["trade-flow eligibility", "route substitution", "freight and basis risk"],
        "supply_change": ["export availability", "regional balance", "prompt structure"],
        "demand_change": ["import pull", "regional balance", "prompt structure"],
        "pricing_signal": ["benchmark repricing", "differential and spread transmission"],
        "market_development": ["market context; no mechanical causal link established"],
    }[event_type]
    if not series:
        series.update(("FEI_PROPANE", "CP_PROPANE", "MB_PROPANE_USD_MT", "VLGC_AG_JAPAN", "VLGC_USGC_NA"))
    signal = direction if direction in {"bullish", "bearish"} else "uncertain"
    return {
        "summary": f"{EVENT_LABELS[event_type]} with {signal} reported market direction; impact remains an inference until corroborated by price, freight, or operational data.",
        "direction": signal,
        "mechanisms": mechanisms,
        "affected_series": sorted(series),
        "interpretation_state": "inferred_not_official_assessment",
    }


def asset_catalog() -> List[Dict[str, Any]]:
    return [json_safe({key: value for key, value in item.items() if key != "aliases"}) for item in ASSETS]


def route_catalog() -> List[Dict[str, Any]]:
    rows = []
    for route in ROUTES:
        row = dict(route)
        row["points"] = [
            {
                "asset_id": asset_id,
                "name": ASSET_BY_ID[asset_id]["name"],
                "latitude": ASSET_BY_ID[asset_id]["latitude"],
                "longitude": ASSET_BY_ID[asset_id]["longitude"],
            }
            for asset_id in route["asset_ids"] if asset_id in ASSET_BY_ID
        ]
        rows.append(json_safe(row))
    return rows


def build_events(news_items: Iterable[Mapping[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Collapse entitled news evidence into conservative, durable LPG events."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for article in news_items:
        if str(article.get("entitlement_state") or "entitled") != "entitled":
            continue
        group_key = str(article.get("cluster_key") or article.get("article_key") or "").strip()
        if not group_key:
            continue
        groups[group_key].append(article)

    events: List[Dict[str, Any]] = []
    for group_key, evidence in groups.items():
        evidence = sorted(
            evidence,
            key=lambda row: (
                bool(row.get("is_breaking")), float(row.get("rank_score") or 0),
                float(row.get("relevance_score") or 0), str(row.get("published_at") or ""),
            ),
            reverse=True,
        )
        representative = evidence[0]
        combined = " ".join(_text(row) for row in evidence)
        matched_assets = _match_assets(combined)
        asset_ids = [item["id"] for item in matched_assets]
        routes = _event_routes(asset_ids)
        route_ids = [item["id"] for item in routes]
        event_type = _event_type(combined)
        sources = {
            str(row.get("source") or "").strip() for row in evidence if row.get("source")
        }
        cluster_sizes = []
        confirmed_by_adapter = False
        for row in evidence:
            metadata = _mapping(row.get("metadata") or row.get("metadata_json"))
            sources.update(_list(metadata.get("cluster_sources")))
            cluster_sizes.append(int(metadata.get("cluster_size") or 1))
            confirmed_by_adapter = confirmed_by_adapter or metadata.get("confirmation_state") == "confirmed"
        sources.discard("")
        official = any(_source_boundary(row) == "licensed_machine_readable" for row in evidence)
        confirmation = "confirmed" if official or confirmed_by_adapter or len(sources) >= 2 else "developing"
        evidence_count = max(len(evidence), max(cluster_sizes or [1]))
        source_count = len(sources)
        importance = str(representative.get("importance") or "low").lower()
        relevance = max(float(row.get("relevance_score") or 0) for row in evidence)
        breaking = any(bool(row.get("is_breaking")) for row in evidence)
        disruptive = event_type in {
            "terminal_disruption", "shipping_disruption", "weather_disruption", "policy_sanctions",
        }
        if breaking and confirmation == "confirmed" and disruptive:
            severity = "critical"
        elif importance == "high" or relevance >= 70:
            severity = "high"
        elif importance == "medium" or relevance >= 42:
            severity = "medium"
        else:
            severity = "low"

        confidence = 34 + min(source_count, 3) * 12 + min(evidence_count, 4) * 4
        confidence += min(max(int(representative.get("source_tier") or 0), 0), 5) * 3
        confidence += 10 if matched_assets else 0
        confidence += 10 if confirmation == "confirmed" else 0
        confidence += 8 if official else 0
        if confirmation == "developing":
            confidence = min(confidence, 74)
        confidence = max(0, min(100, confidence))
        confidence_label = "high" if confidence >= 80 else "medium" if confidence >= 55 else "low"

        data_gaps = []
        if not matched_assets:
            data_gaps.append("event_location_not_resolved")
        if source_count < 2:
            data_gaps.append("single_source_not_corroborated")
        if not official:
            data_gaps.append("public_discovery_not_licensed_platts_news")
        if event_type in {"terminal_disruption", "shipping_disruption", "weather_disruption"}:
            data_gaps.append("no_live_terminal_or_satellite_ais_confirmation")

        published = [_parse_time(row.get("published_at")) for row in evidence]
        published = [value for value in published if value is not None]
        first_seen = min(published) if published else current
        last_seen = max(published) if published else current
        active = (current - last_seen) <= timedelta(days=14)
        direction_values = [str(row.get("direction") or "neutral") for row in evidence]
        direction = next((item for item in direction_values if item in {"bullish", "bearish"}), "neutral")
        impact = _impact(event_type, asset_ids, route_ids, direction)
        primary = matched_assets[0] if matched_assets else None
        risk_score = min(100, round(
            SEVERITY_ORDER[severity] * 16 + confidence * 0.28 + min(source_count, 3) * 4
            + (8 if active else 0)
        ))
        evidence_rows = [
            {
                "article_key": row.get("article_key"),
                "headline": row.get("headline") or row.get("title"),
                "source": row.get("source"),
                "published_at": row.get("published_at"),
                "url": row.get("url"),
                "content_boundary": _source_boundary(row),
            }
            for row in evidence[:12]
        ]
        events.append(json_safe({
            "event_key": _event_key(group_key),
            "headline": representative.get("headline") or representative.get("title") or "Untitled LPG event",
            "event_type": event_type,
            "event_type_label": EVENT_LABELS[event_type],
            "severity": severity,
            "risk_score": risk_score,
            "confirmation_state": confirmation,
            "confidence_score": confidence,
            "confidence_label": confidence_label,
            "first_seen_at": first_seen.isoformat(),
            "last_seen_at": last_seen.isoformat(),
            "active": active,
            "latitude": primary.get("latitude") if primary else None,
            "longitude": primary.get("longitude") if primary else None,
            "location_name": primary.get("name") if primary else None,
            "geo_precision": primary.get("geo_precision") if primary else "unresolved",
            "region": primary.get("region") if primary else representative.get("region"),
            "direction": direction,
            "source_count": source_count,
            "evidence_count": evidence_count,
            "sources": sorted(sources),
            "asset_ids": asset_ids,
            "route_ids": route_ids,
            "products": sorted({str(row.get("product")) for row in evidence if row.get("product")}),
            "affected_series": impact["affected_series"],
            "impact": impact,
            "data_gaps": data_gaps,
            "evidence": evidence_rows,
            "metadata": {
                "intelligence_version": INTELLIGENCE_VERSION,
                "representative_article_key": representative.get("article_key"),
                "official_evidence_present": official,
                "observed_vs_inferred": {
                    "observed": "headline, source, publication time, matched named location",
                    "inferred": "event type, route exposure, affected series, market mechanism",
                },
            },
        }))

    events.sort(key=lambda row: (
        bool(row.get("active")), SEVERITY_ORDER.get(str(row.get("severity")), 0),
        int(row.get("risk_score") or 0), str(row.get("last_seen_at") or ""),
    ), reverse=True)
    return events


def baseline_alerts(events: Iterable[Mapping[str, Any]],
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Detect event surges against a 7-day baseline without hiding thin data."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    recent_start = current - timedelta(hours=2)
    baseline_start = current - timedelta(days=7)
    recent: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    baseline: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    eligible = 0
    for event in events:
        observed = _parse_time(event.get("last_seen_at"))
        if observed is None or observed < baseline_start:
            continue
        eligible += 1
        key = str(event.get("event_type") or "market_development")
        if observed >= recent_start:
            recent[key].append(event)
        elif observed < recent_start:
            baseline[key].append(event)
    alerts = []
    evaluated = []
    for key in sorted(set(recent) | set(baseline)):
        recent_count = len(recent[key])
        baseline_count = len(baseline[key])
        expected_two_hours = baseline_count / 83.0  # remaining 166 hours in the 7d window
        source_diversity = len({
            source for event in recent[key] for source in _list(event.get("sources"))
        })
        sufficient = baseline_count >= 3
        ratio = recent_count / max(expected_two_hours, 0.25) if sufficient else None
        state = "insufficient_history"
        if sufficient:
            state = "surge" if recent_count >= 2 and ratio >= 3 and source_diversity >= 2 else "normal"
        row = {
            "event_type": key,
            "event_type_label": EVENT_LABELS.get(key, key.replace("_", " ").title()),
            "recent_count": recent_count,
            "baseline_count": baseline_count,
            "expected_two_hours": round(expected_two_hours, 2),
            "ratio": round(ratio, 2) if ratio is not None else None,
            "source_diversity": source_diversity,
            "state": state,
        }
        evaluated.append(row)
        if state == "surge":
            alerts.append(row)
    return json_safe({
        "strategy": "rolling_2h_vs_prior_166h_source_diversity",
        "window_hours": 2,
        "baseline_days": 7,
        "eligible_events": eligible,
        "coverage_state": "ready" if any(row["state"] != "insufficient_history" for row in evaluated)
                          else "insufficient_history",
        "alerts": alerts,
        "evaluated": evaluated,
        "generated_at": current.isoformat(),
    })


def normalize_event_for_store(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize one derived event for the SQLite persistence contract."""
    item = dict(value)
    required = ("event_key", "headline", "event_type", "severity", "first_seen_at", "last_seen_at")
    if any(not str(item.get(key) or "").strip() for key in required):
        raise ValueError("event_key, headline, event_type, severity, first_seen_at, and last_seen_at are required")
    if item["severity"] not in SEVERITY_ORDER:
        raise ValueError("invalid intelligence event severity")
    if item.get("confirmation_state") not in {"confirmed", "developing"}:
        raise ValueError("invalid intelligence confirmation_state")
    return {
        "event_key": str(item["event_key"])[:128],
        "headline": str(item["headline"])[:1000],
        "event_type": str(item["event_type"])[:64],
        "severity": str(item["severity"]),
        "risk_score": max(0, min(100, int(item.get("risk_score") or 0))),
        "confirmation_state": str(item["confirmation_state"]),
        "confidence_score": max(0, min(100, int(item.get("confidence_score") or 0))),
        "first_seen_at": normalize_timestamp(item["first_seen_at"]) or str(item["first_seen_at"]),
        "last_seen_at": normalize_timestamp(item["last_seen_at"]) or str(item["last_seen_at"]),
        "latitude": float(item["latitude"]) if item.get("latitude") is not None else None,
        "longitude": float(item["longitude"]) if item.get("longitude") is not None else None,
        "location_name": str(item.get("location_name") or "")[:256] or None,
        "geo_precision": str(item.get("geo_precision") or "unresolved")[:64],
        "region": str(item.get("region") or "")[:128] or None,
        "direction": str(item.get("direction") or "neutral")[:32],
        "source_count": max(0, int(item.get("source_count") or 0)),
        "evidence_count": max(1, int(item.get("evidence_count") or 1)),
        "active": 1 if item.get("active", True) else 0,
        "sources_json": json.dumps(json_safe(item.get("sources") or []), ensure_ascii=True),
        "asset_ids_json": json.dumps(json_safe(item.get("asset_ids") or []), ensure_ascii=True),
        "route_ids_json": json.dumps(json_safe(item.get("route_ids") or []), ensure_ascii=True),
        "products_json": json.dumps(json_safe(item.get("products") or []), ensure_ascii=True),
        "affected_series_json": json.dumps(json_safe(item.get("affected_series") or []), ensure_ascii=True),
        "impact_json": json.dumps(json_safe(item.get("impact") or {}), ensure_ascii=True),
        "data_gaps_json": json.dumps(json_safe(item.get("data_gaps") or []), ensure_ascii=True),
        "evidence_json": json.dumps(json_safe(item.get("evidence") or []), ensure_ascii=True),
        "metadata_json": json.dumps(json_safe(item.get("metadata") or {}), ensure_ascii=True),
        "updated_at": utc_now(),
    }
