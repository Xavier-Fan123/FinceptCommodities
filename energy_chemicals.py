"""Energy and petrochemicals intelligence for the local dashboard.

The main commodity registry intentionally tracks liquid exchange contracts.
This module adds the wider physical energy / chemicals map without pretending
that every product has a clean, free, live futures screen. News is fetched from
public RSS endpoints with only the standard library.
"""

from __future__ import annotations

import email.utils
import html
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener
import xml.etree.ElementTree as ET


PRODUCT_GROUPS = [
    {"id": "crude_feedstocks", "name": "Crude and feedstocks"},
    {"id": "gas_lng_ngl", "name": "Gas, LNG and NGLs"},
    {"id": "refined_products", "name": "Refined products"},
    {"id": "olefins_aromatics", "name": "Olefins and aromatics"},
    {"id": "polymers_fertilizers", "name": "Polymers and fertilizers"},
    {"id": "carbon_power", "name": "Carbon and power proxies"},
]


PRODUCTS: List[Dict[str, Any]] = [
    {
        "id": "wti",
        "name": "WTI crude",
        "group": "crude_feedstocks",
        "dashboard_id": "wti",
        "coverage": "live future",
        "screen": "NYMEX CL",
        "unit": "USD/bbl",
        "role": "US inland light-sweet crude benchmark and refinery feedstock proxy.",
        "drivers": ["Cushing stocks", "US shale supply", "refinery runs", "export arb"],
        "signals": ["WTI-Brent spread", "EIA crude draw/build", "CFTC managed money"],
        "proxies": ["brent", "rbob", "heating_oil"],
        "news_terms": ["WTI", "Cushing", "crude oil", "refinery runs"],
    },
    {
        "id": "brent",
        "name": "Brent crude",
        "group": "crude_feedstocks",
        "dashboard_id": "brent",
        "coverage": "live future",
        "screen": "NYMEX BZ / ICE Brent proxy",
        "unit": "USD/bbl",
        "role": "Waterborne global crude benchmark for Atlantic Basin and seaborne trade.",
        "drivers": ["OPEC+", "North Sea supply", "geopolitics", "tanker flows"],
        "signals": ["prompt spread", "Brent-Dubai arb", "floating storage"],
        "proxies": ["wti", "heating_oil"],
        "news_terms": ["Brent", "OPEC", "crude oil", "oil exports"],
    },
    {
        "id": "naphtha",
        "name": "Naphtha",
        "group": "crude_feedstocks",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "Asia/Europe physical assessments",
        "unit": "USD/MT",
        "role": "Steam-cracker feedstock and gasoline blending component.",
        "drivers": ["crude cracks", "cracker margins", "gasoline blending", "LPG substitution"],
        "signals": ["naphtha-Brent", "ethylene margin", "Asia-Europe arb"],
        "proxies": ["brent", "wti", "rbob"],
        "news_terms": ["naphtha", "steam cracker", "petrochemical feedstock"],
    },
    {
        "id": "condensate",
        "name": "Condensate",
        "group": "crude_feedstocks",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "regional condensate differentials",
        "unit": "USD/bbl",
        "role": "Light hydrocarbon feed for splitters, crackers and blending.",
        "drivers": ["gas production", "splitter demand", "Middle East exports"],
        "signals": ["condensate-naphtha", "splitter run cuts", "light-end oversupply"],
        "proxies": ["brent", "natgas", "naphtha"],
        "news_terms": ["condensate", "splitter", "naphtha"],
    },
    {
        "id": "natgas",
        "name": "Henry Hub gas",
        "group": "gas_lng_ngl",
        "dashboard_id": "natgas",
        "coverage": "live future",
        "screen": "NYMEX NG",
        "unit": "USD/MMBtu",
        "role": "US gas benchmark for power, heating, LNG feedgas and fertilizer costs.",
        "drivers": ["weather", "storage", "LNG feedgas", "associated gas"],
        "signals": ["EIA storage surprise", "winter strip", "Mar-Apr spread"],
        "proxies": ["lng_jkm", "ammonia_urea"],
        "news_terms": ["Henry Hub", "natural gas", "gas storage", "LNG feedgas"],
    },
    {
        "id": "lng_jkm",
        "name": "LNG JKM",
        "group": "gas_lng_ngl",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "JKM physical / swaps",
        "unit": "USD/MMBtu",
        "role": "Northeast Asia spot LNG benchmark and marginal gas price for Asia.",
        "drivers": ["Asian weather", "Europe storage", "shipping", "plant outages"],
        "signals": ["JKM-TTF", "JKM-Henry netback", "LNG freight"],
        "proxies": ["natgas", "brent"],
        "news_terms": ["LNG", "JKM", "Japan Korea Marker", "gas cargo"],
    },
    {
        "id": "propane_lpg",
        "name": "Propane / LPG",
        "group": "gas_lng_ngl",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "Mont Belvieu / FEI / CP",
        "unit": "USD/MT or USD/gal",
        "role": "Heating fuel, petrochemical feedstock and PDH propylene input.",
        "drivers": ["US exports", "PDH margins", "winter demand", "Saudi CP"],
        "signals": ["propane-naphtha", "FEI-MB arb", "VLGC freight"],
        "proxies": ["natgas", "brent", "naphtha"],
        "news_terms": ["propane", "LPG", "PDH", "VLGC", "Saudi CP"],
    },
    {
        "id": "butane_lpg",
        "name": "Butane / LPG",
        "group": "gas_lng_ngl",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "Mont Belvieu / FEI / CP",
        "unit": "USD/MT or USD/gal",
        "role": "Gasoline blending, LPG pool and petrochemical feedstock.",
        "drivers": ["gasoline RVP season", "cracker demand", "exports", "Saudi CP"],
        "signals": ["butane-gasoline blend value", "butane-propane spread"],
        "proxies": ["rbob", "brent", "propane_lpg"],
        "news_terms": ["butane", "LPG", "gasoline blending", "Saudi CP"],
    },
    {
        "id": "rbob",
        "name": "RBOB gasoline",
        "group": "refined_products",
        "dashboard_id": "rbob",
        "coverage": "live future",
        "screen": "NYMEX RB",
        "unit": "USD/gal",
        "role": "US gasoline blendstock benchmark and summer driving-demand proxy.",
        "drivers": ["driving season", "refinery outages", "RVP switch", "stocks"],
        "signals": ["gasoline crack", "EIA gasoline draw/build", "blend economics"],
        "proxies": ["wti", "brent", "butane_lpg"],
        "news_terms": ["gasoline", "RBOB", "refinery outage", "driving season"],
    },
    {
        "id": "heating_oil",
        "name": "ULSD / diesel",
        "group": "refined_products",
        "dashboard_id": "heating_oil",
        "coverage": "live future",
        "screen": "NYMEX HO",
        "unit": "USD/gal",
        "role": "Diesel/distillate benchmark for freight, industry and heating demand.",
        "drivers": ["distillate stocks", "freight demand", "winter heating", "refinery yields"],
        "signals": ["diesel crack", "EIA distillate draw/build", "gasoil arb"],
        "proxies": ["wti", "brent"],
        "news_terms": ["diesel", "ULSD", "distillate", "heating oil", "gasoil"],
    },
    {
        "id": "jet_fuel",
        "name": "Jet fuel",
        "group": "refined_products",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "regional jet fuel differentials",
        "unit": "USD/bbl or USD/gal",
        "role": "Aviation demand marker and middle-distillate yield competitor.",
        "drivers": ["air travel", "refinery kerosene yields", "Asia exports"],
        "signals": ["jet regrade vs diesel", "airport demand", "refinery run mode"],
        "proxies": ["heating_oil", "brent"],
        "news_terms": ["jet fuel", "aviation fuel", "kerosene", "air travel demand"],
    },
    {
        "id": "fuel_oil",
        "name": "Fuel oil",
        "group": "refined_products",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "HSFO / VLSFO physical and swaps",
        "unit": "USD/MT",
        "role": "Marine fuel and refinery residue value marker.",
        "drivers": ["shipping demand", "refinery residue", "sanctions flows", "scrubber economics"],
        "signals": ["VLSFO-HSFO", "fuel oil crack", "bunker demand"],
        "proxies": ["brent", "heating_oil"],
        "news_terms": ["fuel oil", "VLSFO", "HSFO", "bunker fuel", "shipping fuel"],
    },
    {
        "id": "ethylene",
        "name": "Ethylene",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "regional ethylene spot/contract",
        "unit": "USD/MT",
        "role": "Core olefin for polyethylene, MEG, PVC chain and cracker margin.",
        "drivers": ["cracker operating rates", "feedstock slate", "PE demand", "plant outages"],
        "signals": ["ethylene-naphtha margin", "ethylene-ethane margin", "cracker run cuts"],
        "proxies": ["naphtha", "propane_lpg", "natgas"],
        "news_terms": ["ethylene", "steam cracker", "polyethylene", "MEG"],
    },
    {
        "id": "propylene",
        "name": "Propylene",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "polymer-grade propylene",
        "unit": "USD/MT",
        "role": "Input for polypropylene, acrylonitrile and propylene oxide.",
        "drivers": ["PDH margins", "FCC operating rates", "PP demand", "turnarounds"],
        "signals": ["propylene-propane PDH spread", "PP margin", "FCC run changes"],
        "proxies": ["propane_lpg", "rbob", "brent"],
        "news_terms": ["propylene", "PDH", "polypropylene", "FCC"],
    },
    {
        "id": "butadiene",
        "name": "Butadiene",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "Asia/Europe/US butadiene spot",
        "unit": "USD/MT",
        "role": "Synthetic rubber feedstock tied to auto, tire and cracker C4 output.",
        "drivers": ["cracker severity", "tire demand", "C4 extraction", "plant outages"],
        "signals": ["BD-naphtha", "rubber chain margins", "auto demand"],
        "proxies": ["naphtha", "brent"],
        "news_terms": ["butadiene", "synthetic rubber", "C4", "tire demand"],
    },
    {
        "id": "benzene",
        "name": "Benzene",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "benzene spot / contract",
        "unit": "USD/MT",
        "role": "Aromatics building block for styrene, phenol and nylon chain.",
        "drivers": ["reformer output", "styrene margins", "gasoline aromatics", "imports"],
        "signals": ["benzene-naphtha", "styrene-benzene", "US-Asia arb"],
        "proxies": ["rbob", "naphtha", "brent"],
        "news_terms": ["benzene", "styrene", "aromatics", "reformer"],
    },
    {
        "id": "paraxylene",
        "name": "Paraxylene / PTA",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "PX / PTA Asia physical",
        "unit": "USD/MT",
        "role": "Polyester chain feedstock for PTA, PET and fibers.",
        "drivers": ["polyester demand", "reformer output", "China PTA runs", "naphtha"],
        "signals": ["PX-naphtha", "PTA-PX", "polyester inventory"],
        "proxies": ["naphtha", "brent"],
        "news_terms": ["paraxylene", "PX", "PTA", "polyester"],
    },
    {
        "id": "methanol",
        "name": "Methanol",
        "group": "olefins_aromatics",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "China coastal / CFR China / US Gulf",
        "unit": "USD/MT",
        "role": "Gas/coal-derived chemical feedstock for MTO, formaldehyde and MTBE.",
        "drivers": ["coal prices", "gas costs", "MTO margins", "plant outages"],
        "signals": ["methanol-olefin spread", "China port stocks", "coal-to-chemical runs"],
        "proxies": ["natgas", "brent", "butane_lpg"],
        "news_terms": ["methanol", "MTO", "coal chemical", "MTBE"],
    },
    {
        "id": "polyethylene",
        "name": "Polyethylene",
        "group": "polymers_fertilizers",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "HDPE/LLDPE/LDPE regional physical",
        "unit": "USD/MT",
        "role": "Largest ethylene derivative; packaging and film demand marker.",
        "drivers": ["ethylene cost", "converter demand", "exports", "new capacity"],
        "signals": ["PE-ethylene margin", "China port inventory", "US export netback"],
        "proxies": ["ethylene", "naphtha", "natgas"],
        "news_terms": ["polyethylene", "HDPE", "LLDPE", "PE exports"],
    },
    {
        "id": "polypropylene",
        "name": "Polypropylene",
        "group": "polymers_fertilizers",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "PP regional physical",
        "unit": "USD/MT",
        "role": "Propylene derivative used in packaging, fiber and autos.",
        "drivers": ["propylene cost", "PDH economics", "consumer goods demand", "capacity"],
        "signals": ["PP-propylene margin", "PDH spread", "China raffia demand"],
        "proxies": ["propylene", "propane_lpg", "rbob"],
        "news_terms": ["polypropylene", "PP", "raffia", "PDH"],
    },
    {
        "id": "pvc",
        "name": "PVC",
        "group": "polymers_fertilizers",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "PVC regional physical",
        "unit": "USD/MT",
        "role": "Construction-linked polymer tied to ethylene, chlorine and carbide routes.",
        "drivers": ["construction", "chlor-alkali", "coal/carbide cost", "exports"],
        "signals": ["PVC-ethylene spread", "caustic soda co-product", "housing indicators"],
        "proxies": ["ethylene", "natgas"],
        "news_terms": ["PVC", "vinyl", "chlor-alkali", "construction demand"],
    },
    {
        "id": "ammonia_urea",
        "name": "Ammonia / urea",
        "group": "polymers_fertilizers",
        "dashboard_id": None,
        "coverage": "physical proxy",
        "screen": "urea / ammonia physical",
        "unit": "USD/MT",
        "role": "Gas-intensive fertilizer chain and marginal gas-demand sink.",
        "drivers": ["gas cost", "crop demand", "export policy", "plant outages"],
        "signals": ["urea-gas margin", "Middle East exports", "India tenders"],
        "proxies": ["natgas", "corn"],
        "news_terms": ["urea", "ammonia", "fertilizer", "natural gas cost"],
    },
    {
        "id": "eua_carbon",
        "name": "EU carbon allowance",
        "group": "carbon_power",
        "dashboard_id": None,
        "coverage": "external proxy",
        "screen": "EUA futures / EU ETS",
        "unit": "EUR/MT CO2",
        "role": "Carbon cost for power, refining and European industrial margins.",
        "drivers": ["policy", "power burn", "industrial output", "allowance supply"],
        "signals": ["clean dark/spark spreads", "EUA-gas", "auction volumes"],
        "proxies": ["natgas", "brent"],
        "news_terms": ["EU ETS", "carbon allowance", "EUA", "emissions trading"],
    },
]


GROUP_LENSES: Dict[str, Dict[str, Any]] = {
    "crude_feedstocks": {
        "trade_lens": "Feedstock value, refinery demand and regional arbitrage.",
        "benchmarks": ["Brent", "WTI", "Dubai/Oman", "naphtha cracks"],
        "watch_questions": [
            "Are prompt crude spreads confirming inventory tightness?",
            "Are refiners raising runs or cutting because product cracks are weak?",
            "Is the regional arbitrage open after freight and quality adjustments?",
        ],
        "risk_flags": ["OPEC+ policy", "sanctions", "pipeline/storage constraints"],
    },
    "gas_lng_ngl": {
        "trade_lens": "Weather, storage, export pull and petrochemical feed switching.",
        "benchmarks": ["Henry Hub", "JKM", "TTF", "Mont Belvieu", "Saudi CP"],
        "watch_questions": [
            "Is storage trajectory above or below the five-year band?",
            "Are LNG netbacks pulling feedgas away from domestic demand?",
            "Is LPG cheap enough versus naphtha to alter cracker slate?",
        ],
        "risk_flags": ["weather model shift", "LNG outage", "freight spike", "PDH run cuts"],
    },
    "refined_products": {
        "trade_lens": "Refinery margin, product stocks, yield choice and demand seasonality.",
        "benchmarks": ["RBOB", "ULSD", "gasoil", "jet regrade", "fuel oil cracks"],
        "watch_questions": [
            "Are cracks paying refiners to run harder?",
            "Are product draws broad-based or only one barrel?",
            "Is refinery maintenance tightening prompt supply?",
        ],
        "risk_flags": ["refinery outage", "RVP transition", "hurricane", "freight disruption"],
    },
    "olefins_aromatics": {
        "trade_lens": "Cracker/reformer margins, feedstock slate and derivative demand.",
        "benchmarks": ["ethylene", "propylene", "benzene", "PX", "naphtha"],
        "watch_questions": [
            "Are derivative margins strong enough to keep crackers running?",
            "Is LPG/naphtha economics changing olefin yields?",
            "Are Asian operating rates responding to inventory pressure?",
        ],
        "risk_flags": ["cracker outage", "new capacity", "China demand", "feedstock switch"],
    },
    "polymers_fertilizers": {
        "trade_lens": "Derivative demand, inventory pressure and cost pass-through.",
        "benchmarks": ["PE", "PP", "PVC", "urea", "ammonia"],
        "watch_questions": [
            "Are converters restocking or only buying hand-to-mouth?",
            "Can producers pass feedstock cost into polymer prices?",
            "Are export tenders clearing marginal supply?",
        ],
        "risk_flags": ["new capacity", "export policy", "construction demand", "gas cost"],
    },
    "carbon_power": {
        "trade_lens": "Policy cost, fuel switching and industrial operating leverage.",
        "benchmarks": ["EUA", "clean spark", "clean dark", "power forwards"],
        "watch_questions": [
            "Is carbon strengthening because power burn changed or policy changed?",
            "Are industrial margins absorbing allowance costs?",
            "Is gas-to-coal switching setting the marginal carbon bid?",
        ],
        "risk_flags": ["auction supply", "policy revision", "industrial slowdown"],
    },
}


PRODUCT_LENSES: Dict[str, Dict[str, Any]] = {
    "wti": {
        "trade_lens": "Cushing bottleneck and US export arb.",
        "benchmarks": ["CL prompt spread", "WTI-Brent", "Cushing stocks"],
        "watch_questions": ["Is Cushing drawing fast enough to support the front spread?"],
    },
    "brent": {
        "trade_lens": "Waterborne balance and geopolitical risk premium.",
        "benchmarks": ["Brent prompt spread", "Brent-Dubai", "OPEC+ supply"],
        "watch_questions": ["Is the prompt spread moving before flat price?"],
    },
    "natgas": {
        "trade_lens": "Weather, storage and LNG feedgas call on supply.",
        "benchmarks": ["Henry Hub winter strip", "EIA storage", "LNG feedgas"],
        "watch_questions": ["Is storage tracking to comfortable end-season levels?"],
    },
    "propane_lpg": {
        "trade_lens": "Export arb, PDH economics and naphtha substitution.",
        "benchmarks": ["Mont Belvieu", "FEI", "Saudi CP", "VLGC freight"],
        "watch_questions": ["Is FEI strong enough to pull US barrels after freight?"],
    },
    "butane_lpg": {
        "trade_lens": "Gasoline blend value versus LPG export value.",
        "benchmarks": ["normal butane", "isobutane", "RBOB blend value"],
        "watch_questions": ["Is seasonal RVP limiting butane blend demand?"],
    },
    "naphtha": {
        "trade_lens": "Cracker feed cost and gasoline blend component.",
        "benchmarks": ["Japan naphtha", "CFR Asia", "naphtha-Brent"],
        "watch_questions": ["Is naphtha discounted enough to win against LPG in crackers?"],
    },
    "ethylene": {
        "trade_lens": "Steam-cracker margin and PE demand pulse.",
        "benchmarks": ["ethylene-naphtha", "ethylene-ethane", "PE margin"],
        "watch_questions": ["Are ethylene margins forcing cracker rate cuts?"],
    },
    "propylene": {
        "trade_lens": "PDH margin, FCC output and PP pull.",
        "benchmarks": ["propylene-propane", "PP-propylene", "FCC rates"],
        "watch_questions": ["Are PDH units in or out of the money?"],
    },
    "benzene": {
        "trade_lens": "Aromatics tightness through styrene and gasoline blend value.",
        "benchmarks": ["benzene-naphtha", "styrene-benzene", "reformer economics"],
        "watch_questions": ["Is benzene being pulled by styrene demand or gasoline blending?"],
    },
    "ammonia_urea": {
        "trade_lens": "Gas-cost floor, tender demand and export policy.",
        "benchmarks": ["urea FOB Middle East", "ammonia", "Henry Hub", "TTF"],
        "watch_questions": ["Are gas prices forcing marginal fertilizer supply offline?"],
    },
}


def _coverage_score(product: Dict[str, Any]) -> int:
    coverage = product.get("coverage", "")
    if "live" in coverage:
        return 3
    if "physical" in coverage:
        return 2
    return 1


def _enriched_product(product: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(product)
    group_lens = GROUP_LENSES.get(base["group"], {})
    product_lens = PRODUCT_LENSES.get(base["id"], {})
    merged = dict(group_lens)
    for key, value in product_lens.items():
        if key in ("benchmarks", "watch_questions", "risk_flags"):
            merged[key] = list(dict.fromkeys(value + group_lens.get(key, [])))
        else:
            merged[key] = value
    base["trade_lens"] = merged.get("trade_lens", "")
    base["benchmarks"] = merged.get("benchmarks", [])
    base["watch_questions"] = merged.get("watch_questions", [])
    base["risk_flags"] = merged.get("risk_flags", [])
    base["coverage_score"] = _coverage_score(base)
    base["action"] = "open_live_screen" if base.get("dashboard_id") else "track_proxy_news"
    return base


def _products() -> List[Dict[str, Any]]:
    return [_enriched_product(product) for product in PRODUCTS]


FLOW_LINKS = [
    {"from": "wti", "to": "naphtha", "label": "distillation"},
    {"from": "brent", "to": "naphtha", "label": "waterborne feed"},
    {"from": "wti", "to": "rbob", "label": "refining"},
    {"from": "wti", "to": "heating_oil", "label": "refining"},
    {"from": "heating_oil", "to": "jet_fuel", "label": "middle distillates"},
    {"from": "brent", "to": "fuel_oil", "label": "residue"},
    {"from": "natgas", "to": "lng_jkm", "label": "liquefaction"},
    {"from": "natgas", "to": "ammonia_urea", "label": "gas cost"},
    {"from": "propane_lpg", "to": "propylene", "label": "PDH"},
    {"from": "naphtha", "to": "ethylene", "label": "steam cracking"},
    {"from": "naphtha", "to": "propylene", "label": "steam cracking"},
    {"from": "ethylene", "to": "polyethylene", "label": "polymerization"},
    {"from": "propylene", "to": "polypropylene", "label": "polymerization"},
    {"from": "ethylene", "to": "pvc", "label": "vinyl chain"},
    {"from": "benzene", "to": "polyethylene", "label": "styrene/packaging proxy"},
    {"from": "paraxylene", "to": "polyethylene", "label": "polyester demand proxy"},
]


EVENT_CALENDAR = [
    {
        "cadence": "weekly",
        "day": "Tue",
        "name": "API petroleum stocks",
        "time": "16:30 ET",
        "markets": ["wti", "rbob", "heating_oil"],
        "use": "Early stock survey; market trades the surprise into EIA.",
    },
    {
        "cadence": "weekly",
        "day": "Wed",
        "name": "EIA Weekly Petroleum Status Report",
        "time": "10:30 ET",
        "markets": ["wti", "rbob", "heating_oil", "jet_fuel"],
        "use": "Crude, Cushing, products, refinery runs, imports and exports.",
    },
    {
        "cadence": "weekly",
        "day": "Thu",
        "name": "EIA Natural Gas Storage",
        "time": "10:30 ET",
        "markets": ["natgas", "lng_jkm", "ammonia_urea"],
        "use": "Injection/withdrawal against weather-normal expectations.",
    },
    {
        "cadence": "weekly",
        "day": "Fri",
        "name": "CFTC Commitments of Traders",
        "time": "15:30 ET",
        "markets": ["wti", "brent", "natgas", "rbob", "heating_oil"],
        "use": "Managed-money crowding, hedger flow and open-interest changes.",
    },
    {
        "cadence": "monthly",
        "day": "monthly",
        "name": "OPEC MOMR / IEA OMR / EIA STEO",
        "time": "varies",
        "markets": ["brent", "wti", "lng_jkm", "naphtha"],
        "use": "Demand balances, non-OPEC supply, refinery runs and stocks.",
    },
]


NEWS_TOPICS = [
    {"id": "energy", "name": "Energy complex"},
    {"id": "crude", "name": "Crude oil"},
    {"id": "refined_products", "name": "Refined products"},
    {"id": "natgas_lng", "name": "Natural gas / LNG"},
    {"id": "lpg_ngl", "name": "LPG / NGL"},
    {"id": "petrochemicals", "name": "Petrochemicals"},
    {"id": "shipping_policy", "name": "Shipping / policy"},
]


_TOPIC_QUERIES = {
    "energy": '("crude oil" OR "natural gas" OR LNG OR petrochemical) market when:14d',
    "crude": 'WTI Brent crude oil OPEC refinery inventories when:14d',
    "refined_products": 'gasoline diesel jet fuel refinery crack spread stocks when:14d',
    "natgas_lng": 'natural gas LNG Henry Hub storage JKM feedgas when:14d',
    "lpg_ngl": 'LPG propane butane NGL PDH naphtha Asia when:14d',
    "petrochemicals": 'petrochemical ethylene propylene polyethylene naphtha aromatics when:14d',
    "shipping_policy": 'oil tanker LNG freight sanctions energy shipping policy when:14d',
}


_OFFICIAL_FEEDS = [
    {
        "id": "eia_today",
        "source": "EIA Today in Energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "topics": ["energy", "crude", "refined_products", "natgas_lng"],
    },
]

_YAHOO_TICKERS = {
    "wti": "CL=F",
    "brent": "BZ=F",
    "natgas": "NG=F",
    "rbob": "RB=F",
    "heating_oil": "HO=F",
    "crude": "CL=F",
    "refined_products": "RB=F",
    "natgas_lng": "NG=F",
    "energy": "CL=F",
}

_DIRECT_OPENER = build_opener(ProxyHandler({}))


_BULLISH_TERMS = [
    "draw", "drawdown", "outage", "shutdown", "sanction", "attack", "disruption",
    "tight", "shortage", "cut", "curb", "hurricane", "freeze", "heatwave",
    "cold snap", "export halt", "strike", "unplanned",
]
_BEARISH_TERMS = [
    "build", "stockpile", "surplus", "glut", "weak demand", "demand falls",
    "restart", "resume", "output rises", "production rises", "warm weather",
    "mild weather", "oversupply", "run cuts",
]
_PRIORITY_TERMS = [
    "opec", "eia", "iea", "inventory", "storage", "refinery", "outage", "sanction",
    "lng", "shipping", "freight", "petrochemical", "ethylene", "propylene", "lpg",
]


def _product_by_id() -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in _products()}


def _google_news_url(query: str) -> str:
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return "https://news.google.com/rss/search?" + urlencode(params)


def _text_of(node: Optional[ET.Element], default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return html.unescape(node.text).strip()


def _strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _parse_date(raw: str) -> Optional[int]:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        pass
    try:
        raw = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _fetch_xml(url: str, timeout: int = 12) -> ET.Element:
    req = Request(
        url,
        headers={
            "User-Agent": "FinceptCommoditiesLocal/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with _DIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - fixed public feeds
        return ET.fromstring(resp.read())


def _entry_children(entry: ET.Element) -> Dict[str, ET.Element]:
    children = {}
    for child in list(entry):
        key = child.tag.rsplit("}", 1)[-1].lower()
        children[key] = child
    return children


def _first_child(children: Dict[str, ET.Element], *names: str) -> Optional[ET.Element]:
    for name in names:
        node = children.get(name)
        if node is not None:
            return node
    return None


def _parse_feed(url: str, source_name: str, feed_id: str) -> List[Dict[str, Any]]:
    root = _fetch_xml(url)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{*}item")
    is_atom = False
    if not entries:
        entries = root.findall(".//{*}entry")
        is_atom = True

    articles = []
    for entry in entries:
        kids = _entry_children(entry)
        title = _text_of(kids.get("title"))
        if not title:
            continue
        if is_atom:
            link = ""
            link_node = kids.get("link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
            summary = _strip_html(_text_of(_first_child(kids, "summary", "content")))
            raw_date = _text_of(_first_child(kids, "updated", "published"))
        else:
            link = _text_of(kids.get("link"))
            summary = _strip_html(_text_of(kids.get("description")))
            raw_date = _text_of(_first_child(kids, "pubdate", "date"))

        pub_ts = _parse_date(raw_date) or int(time.time())
        source = source_name
        source_node = kids.get("source")
        if source_node is not None and _text_of(source_node):
            source = _text_of(source_node)
        articles.append({
            "title": _strip_html(title),
            "summary": summary[:420],
            "url": link,
            "source": source,
            "feed_id": feed_id,
            "published": pub_ts,
            "published_iso": datetime.fromtimestamp(pub_ts, timezone.utc).isoformat(),
        })
    return articles


def _fetch_yahoo_news(symbol: str, limit: int = 12) -> List[Dict[str, Any]]:
    query = urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    return _parse_feed(
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?{query}",
        "Yahoo Finance",
        f"yahoo_{symbol}",
    )[:limit]


def _tag_products(article: Dict[str, Any]) -> List[str]:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    matches = []
    for product in PRODUCTS:
        terms = [product["name"], product["id"]] + product.get("news_terms", [])
        for term in terms:
            if term and term.lower() in text:
                matches.append(product["id"])
                break
    return matches[:8]


def _score_article(article: Dict[str, Any]) -> Dict[str, Any]:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    bull = sum(1 for term in _BULLISH_TERMS if term in text)
    bear = sum(1 for term in _BEARISH_TERMS if term in text)
    matched = [term for term in _PRIORITY_TERMS if term in text]
    score = bull - bear
    if score >= 2:
        bias = "bullish"
    elif score <= -2:
        bias = "bearish"
    else:
        bias = "neutral"
    priority = len(matched)
    priority += 2 if bias != "neutral" else 0
    if priority >= 5:
        priority_label = "high"
    elif priority >= 2:
        priority_label = "medium"
    else:
        priority_label = "low"
    return {
        "market_bias": bias,
        "bias_score": score,
        "priority": priority,
        "priority_label": priority_label,
        "matched_terms": matched[:8],
    }


def _feed_defs(topic: str, product_id: Optional[str]) -> List[Dict[str, str]]:
    topic = topic if topic in _TOPIC_QUERIES else "energy"
    query = _TOPIC_QUERIES[topic]
    product = _product_by_id().get(product_id or "")
    if product:
        terms = " OR ".join(f'"{t}"' for t in product.get("news_terms", [])[:5])
        query = f"({terms}) market when:14d" if terms else query
    feeds = [{
        "id": f"google_{product_id or topic}",
        "source": "Google News",
        "url": _google_news_url(query),
    }]
    for feed in _OFFICIAL_FEEDS:
        if topic in feed["topics"] and not product:
            feeds.append({"id": feed["id"], "source": feed["source"], "url": feed["url"]})
    return feeds


def _dedupe_articles(articles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for article in articles:
        title_key = re.sub(r"[^a-z0-9]+", "", article.get("title", "").lower())[:90]
        url_key = article.get("url") or title_key
        key = title_key or url_key
        if key in seen:
            continue
        seen.add(key)
        article["products"] = _tag_products(article)
        article.update(_score_article(article))
        out.append(article)
    out.sort(key=lambda a: (a.get("priority", 0), a.get("published", 0)), reverse=True)
    return out


def _brief_products(topic: str, product_id: Optional[str]) -> List[Dict[str, Any]]:
    products = _products()
    if product_id:
        return [p for p in products if p["id"] == product_id]
    if topic == "crude":
        groups = {"crude_feedstocks"}
    elif topic == "refined_products":
        groups = {"refined_products"}
    elif topic == "natgas_lng":
        ids = {"natgas", "lng_jkm", "ammonia_urea"}
        return [p for p in products if p["id"] in ids]
    elif topic == "lpg_ngl":
        ids = {"propane_lpg", "butane_lpg", "propylene", "naphtha"}
        return [p for p in products if p["id"] in ids]
    elif topic == "petrochemicals":
        groups = {"olefins_aromatics", "polymers_fertilizers"}
    elif topic == "shipping_policy":
        ids = {"brent", "lng_jkm", "propane_lpg", "fuel_oil", "eua_carbon"}
        return [p for p in products if p["id"] in ids]
    else:
        ids = {"wti", "brent", "natgas", "rbob", "heating_oil", "propane_lpg"}
        return [p for p in products if p["id"] in ids]
    return [p for p in products if p["group"] in groups]


def _watch_briefs(topic: str, product_id: Optional[str]) -> List[Dict[str, Any]]:
    briefs = []
    for product in _brief_products(topic, product_id)[:8]:
        questions = product.get("watch_questions") or []
        signals = product.get("signals") or []
        title = f"{product['name']}: {product.get('trade_lens') or product['role']}"
        summary_parts = []
        if signals:
            summary_parts.append("Signals: " + ", ".join(signals[:3]) + ".")
        if questions:
            summary_parts.append("Question: " + questions[0])
        if product.get("risk_flags"):
            summary_parts.append("Risk: " + ", ".join(product["risk_flags"][:3]) + ".")
        briefs.append({
            "title": title,
            "summary": " ".join(summary_parts),
            "url": "",
            "source": "Fincept watch brief",
            "feed_id": "watch_brief",
            "published": int(time.time()),
            "published_iso": datetime.now(timezone.utc).isoformat(),
            "products": [product["id"]],
            "market_bias": "watch",
            "bias_score": 0,
            "priority": product.get("coverage_score", 1),
            "priority_label": "watch",
            "matched_terms": product.get("benchmarks", [])[:5],
            "is_brief": True,
        })
    return briefs


def energy_hub_payload() -> Dict[str, Any]:
    by_group = {g["id"]: [] for g in PRODUCT_GROUPS}
    products = _products()
    for product in products:
        by_group.setdefault(product["group"], []).append(product)
    groups = [dict(group, products=by_group.get(group["id"], [])) for group in PRODUCT_GROUPS]
    live = [p for p in products if p.get("dashboard_id")]
    physical = [p for p in products if not p.get("dashboard_id")]
    return {
        "updated": int(time.time()),
        "groups": groups,
        "products": products,
        "flows": FLOW_LINKS,
        "calendar": EVENT_CALENDAR,
        "topics": NEWS_TOPICS,
        "coverage": {
            "live_contracts": len(live),
            "physical_products": len(physical),
            "total_products": len(PRODUCTS),
            "note": "Live futures stay in the main market grid; physical chemicals use proxy and news coverage.",
        },
    }


def product_context(product_id: str) -> Dict[str, Any]:
    pid = (product_id or "").strip().lower()
    products = _product_by_id()
    product = products.get(pid)
    if product is None:
        for candidate in _products():
            if candidate.get("dashboard_id") == pid:
                product = candidate
                break
    if product is None:
        return {"success": False, "error": f"unknown energy product '{product_id}'"}

    related_edges = [
        edge for edge in FLOW_LINKS
        if edge["from"] == product["id"] or edge["to"] == product["id"]
    ]
    related_ids = set(product.get("proxies", []))
    for edge in related_edges:
        related_ids.add(edge["from"])
        related_ids.add(edge["to"])
    related_ids.discard(product["id"])
    related = [products[rid] for rid in related_ids if rid in products]
    group = next((g for g in PRODUCT_GROUPS if g["id"] == product["group"]), None)
    return {
        "success": True,
        "updated": int(time.time()),
        "product": product,
        "group": group,
        "related": related,
        "flows": related_edges,
        "news_topic": _topic_for_product(product),
    }


def _topic_for_product(product: Dict[str, Any]) -> str:
    group = product.get("group")
    if group == "crude_feedstocks":
        return "crude"
    if group == "gas_lng_ngl":
        return "natgas_lng" if product["id"] in ("natgas", "lng_jkm") else "lpg_ngl"
    if group == "refined_products":
        return "refined_products"
    if group in ("olefins_aromatics", "polymers_fertilizers"):
        return "petrochemicals"
    return "energy"


def news_payload(topic: str = "energy", product: Optional[str] = None,
                 limit: int = 40) -> Dict[str, Any]:
    topic = topic if topic in _TOPIC_QUERIES else "energy"
    product_id = (product or "").strip().lower() or None
    if product_id and product_id not in _product_by_id():
        product_id = None
    feed_defs = _feed_defs(topic, product_id)
    all_articles: List[Dict[str, Any]] = []
    sources = []
    yahoo_key = product_id or topic
    yahoo_symbol = _YAHOO_TICKERS.get(yahoo_key)
    if yahoo_symbol:
        try:
            articles = _fetch_yahoo_news(yahoo_symbol)
            all_articles.extend(articles)
            sources.append({"id": f"yahoo_{yahoo_symbol}", "source": "Yahoo Finance",
                            "ok": True, "count": len(articles)})
        except Exception as exc:  # noqa: BLE001
            sources.append({"id": f"yahoo_{yahoo_symbol}", "source": "Yahoo Finance",
                            "ok": False, "error": str(exc)})
    for feed in feed_defs:
        try:
            articles = _parse_feed(feed["url"], feed["source"], feed["id"])
            all_articles.extend(articles)
            sources.append({"id": feed["id"], "source": feed["source"],
                            "ok": True, "count": len(articles)})
        except Exception as exc:  # noqa: BLE001 - surface per-feed failure
            sources.append({"id": feed["id"], "source": feed["source"],
                            "ok": False, "error": str(exc)})
    deduped = _dedupe_articles(all_articles)
    limit = max(5, min(int(limit or 40), 80))
    briefs = _watch_briefs(topic, product_id)
    ok_sources = sum(1 for source in sources if source.get("ok"))
    return {
        "success": True,
        "updated": int(time.time()),
        "topic": topic,
        "product": product_id,
        "topics": NEWS_TOPICS,
        "articles": deduped[:limit],
        "briefs": briefs,
        "count": min(len(deduped), limit),
        "available": len(deduped),
        "sources": sources,
        "source_status": {
            "ok": ok_sources,
            "failed": len(sources) - ok_sources,
            "degraded": ok_sources == 0,
        },
    }
