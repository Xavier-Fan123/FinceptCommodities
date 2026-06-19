"""Commodities Analytics Configuration

Canonical commodity registry with cross-source symbol mappings:
- CME product codes (settlement curves via nymex/comex/grain scripts)
- Yahoo Finance continuous-contract tickers (history via yfinance_data.py)
- World Bank Pink Sheet keys (long-term monthly via world_bank_commodity_data.py)
- CFTC COT market names/codes (positioning via cftc_data.py)
"""

from enum import Enum
from typing import Any, Dict, List, Optional


class CommoditySector(str, Enum):
    ENERGY = "energy"
    PRECIOUS_METALS = "precious_metals"
    BASE_METALS = "base_metals"
    GRAINS = "grains"
    SOFTS = "softs"
    LIVESTOCK = "livestock"


class Constants:
    DAYS_IN_YEAR = 365
    TRADING_DAYS_IN_YEAR = 252
    GALLONS_PER_BARREL = 42
    EWMA_LAMBDA = 0.94
    COT_INDEX_WEEKS = 156          # 3 years of weekly COT reports
    DEFAULT_RISK_FREE_RATE = 0.04
    DEFAULT_STORAGE_COST = 0.02    # annualized, fraction of spot
    DEFAULT_HEAT_RATE = 7.0        # MMBtu per MWh for spark spread


# Futures month codes (CME convention)
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
MONTH_NAMES = {v: k for k, v in MONTH_CODES.items()}


# Canonical commodity registry.
# Fields:
#   name          display name
#   sector        CommoditySector value
#   exchange      primary listing exchange
#   cme_code      CME Group product code for settlements API (None = not on CME API)
#   yf_ticker     Yahoo Finance continuous front-month ticker
#   wb_key        world_bank_commodity_data.COMMODITY_INDICATORS key (None = not covered)
#   cftc_name     cftc_data cot_codes friendly name (None = not in curated list)
#   cftc_code     CFTC contract market code (for direct lookups)
#   contract_size / size_unit   quantity per contract
#   quote_unit    price quotation unit
#   tick_size     minimum price fluctuation (in quote_unit)
#   months        listed contract month codes
COMMODITY_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ---------------- Energy ----------------
    "wti": {
        "name": "WTI Crude Oil", "sector": "energy", "exchange": "NYMEX",
        "cme_code": "CL", "yf_ticker": "CL=F", "wb_key": "crude_oil_wti",
        "cftc_name": "crude_oil", "cftc_code": "067651",
        "contract_size": 1000, "size_unit": "barrels",
        "quote_unit": "USD/bbl", "tick_size": 0.01,
        "months": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    },
    "brent": {
        "name": "Brent Crude Oil", "sector": "energy", "exchange": "NYMEX/ICE",
        "cme_code": "BZ", "yf_ticker": "BZ=F", "wb_key": "crude_oil_brent",
        "cftc_name": "brent", "cftc_code": "06765T",  # BRENT LAST DAY - NYMEX
        "contract_size": 1000, "size_unit": "barrels",
        "quote_unit": "USD/bbl", "tick_size": 0.01,
        "months": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    },
    "natgas": {
        "name": "Henry Hub Natural Gas", "sector": "energy", "exchange": "NYMEX",
        "cme_code": "NG", "yf_ticker": "NG=F", "wb_key": "natural_gas_us",
        "cftc_name": "natural_gas", "cftc_code": "023651",  # NAT GAS NYME
        "contract_size": 10000, "size_unit": "MMBtu",
        "quote_unit": "USD/MMBtu", "tick_size": 0.001,
        "months": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    },
    "rbob": {
        "name": "RBOB Gasoline", "sector": "energy", "exchange": "NYMEX",
        "cme_code": "RB", "yf_ticker": "RB=F", "wb_key": None,
        "cftc_name": "gasoline", "cftc_code": "111659",
        "contract_size": 42000, "size_unit": "gallons",
        "quote_unit": "USD/gal", "tick_size": 0.0001,
        "months": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    },
    "heating_oil": {
        "name": "NY Harbor ULSD (Heating Oil)", "sector": "energy", "exchange": "NYMEX",
        "cme_code": "HO", "yf_ticker": "HO=F", "wb_key": None,
        "cftc_name": "heating_oil", "cftc_code": "022651",
        "contract_size": 42000, "size_unit": "gallons",
        "quote_unit": "USD/gal", "tick_size": 0.0001,
        "months": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    },
    # ---------------- Precious metals ----------------
    "gold": {
        "name": "Gold", "sector": "precious_metals", "exchange": "COMEX",
        "cme_code": "GC", "yf_ticker": "GC=F", "wb_key": "gold",
        "cftc_name": "gold", "cftc_code": "088691",
        "contract_size": 100, "size_unit": "troy oz",
        "quote_unit": "USD/oz", "tick_size": 0.10,
        "months": ["G", "J", "M", "Q", "V", "Z"],
    },
    "silver": {
        "name": "Silver", "sector": "precious_metals", "exchange": "COMEX",
        "cme_code": "SI", "yf_ticker": "SI=F", "wb_key": "silver",
        "cftc_name": "silver", "cftc_code": "084691",
        "contract_size": 5000, "size_unit": "troy oz",
        "quote_unit": "USD/oz", "tick_size": 0.005,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "platinum": {
        "name": "Platinum", "sector": "precious_metals", "exchange": "NYMEX",
        "cme_code": "PL", "yf_ticker": "PL=F", "wb_key": "platinum",
        "cftc_name": "platinum", "cftc_code": "076651",
        "contract_size": 50, "size_unit": "troy oz",
        "quote_unit": "USD/oz", "tick_size": 0.10,
        "months": ["F", "J", "N", "V"],
    },
    "palladium": {
        "name": "Palladium", "sector": "precious_metals", "exchange": "NYMEX",
        "cme_code": "PA", "yf_ticker": "PA=F", "wb_key": None,
        "cftc_name": "palladium", "cftc_code": "075651",
        "contract_size": 100, "size_unit": "troy oz",
        "quote_unit": "USD/oz", "tick_size": 0.10,
        "months": ["H", "M", "U", "Z"],
    },
    # ---------------- Base metals ----------------
    "copper": {
        "name": "Copper", "sector": "base_metals", "exchange": "COMEX",
        "cme_code": "HG", "yf_ticker": "HG=F", "wb_key": "copper",
        "cftc_name": "copper", "cftc_code": "085692",
        "contract_size": 25000, "size_unit": "pounds",
        "quote_unit": "USD/lb", "tick_size": 0.0005,
        "months": ["H", "K", "N", "U", "Z"],
    },
    # ---------------- Grains & oilseeds ----------------
    "corn": {
        "name": "Corn", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZC", "yf_ticker": "ZC=F", "wb_key": "corn",
        "cftc_name": "corn", "cftc_code": "002602",
        "contract_size": 5000, "size_unit": "bushels",
        "quote_unit": "cents/bu", "tick_size": 0.25,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "wheat": {
        "name": "Chicago SRW Wheat", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZW", "yf_ticker": "ZW=F", "wb_key": "wheat",
        "cftc_name": "wheat", "cftc_code": "001602",
        "contract_size": 5000, "size_unit": "bushels",
        "quote_unit": "cents/bu", "tick_size": 0.25,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "soybeans": {
        "name": "Soybeans", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZS", "yf_ticker": "ZS=F", "wb_key": "soybean",
        "cftc_name": "soybeans", "cftc_code": "005602",
        "contract_size": 5000, "size_unit": "bushels",
        "quote_unit": "cents/bu", "tick_size": 0.25,
        "months": ["F", "H", "K", "N", "Q", "U", "X"],
    },
    "soybean_oil": {
        "name": "Soybean Oil", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZL", "yf_ticker": "ZL=F", "wb_key": "soybeantoil",
        "cftc_name": None, "cftc_code": "007601",
        "contract_size": 60000, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.01,
        "months": ["F", "H", "K", "N", "Q", "U", "V", "Z"],
    },
    "soybean_meal": {
        "name": "Soybean Meal", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZM", "yf_ticker": "ZM=F", "wb_key": "soybeanmeal",
        "cftc_name": None, "cftc_code": "026603",
        "contract_size": 100, "size_unit": "short tons",
        "quote_unit": "USD/short ton", "tick_size": 0.10,
        "months": ["F", "H", "K", "N", "Q", "U", "V", "Z"],
    },
    "oats": {
        "name": "Oats", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZO", "yf_ticker": "ZO=F", "wb_key": None,
        "cftc_name": None, "cftc_code": "004603",
        "contract_size": 5000, "size_unit": "bushels",
        "quote_unit": "cents/bu", "tick_size": 0.25,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "rough_rice": {
        "name": "Rough Rice", "sector": "grains", "exchange": "CBOT",
        "cme_code": "ZR", "yf_ticker": "ZR=F", "wb_key": "rice",
        "cftc_name": None, "cftc_code": "039601",
        "contract_size": 2000, "size_unit": "hundredweight",
        "quote_unit": "USD/cwt", "tick_size": 0.005,
        "months": ["F", "H", "K", "N", "U", "X"],
    },
    # ---------------- Softs (ICE US — not on CME settlements API) ----------------
    "sugar": {
        "name": "Sugar No. 11", "sector": "softs", "exchange": "ICE",
        "cme_code": None, "yf_ticker": "SB=F", "wb_key": "sugar",
        "cftc_name": "sugar", "cftc_code": "080732",
        "contract_size": 112000, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.01,
        "months": ["H", "K", "N", "V"],
    },
    "coffee": {
        "name": "Coffee C (Arabica)", "sector": "softs", "exchange": "ICE",
        "cme_code": None, "yf_ticker": "KC=F", "wb_key": "coffee_arabica",
        "cftc_name": "coffee", "cftc_code": "083731",
        "contract_size": 37500, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.05,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "cocoa": {
        "name": "Cocoa", "sector": "softs", "exchange": "ICE",
        "cme_code": None, "yf_ticker": "CC=F", "wb_key": "cocoa",
        "cftc_name": "cocoa", "cftc_code": "073732",
        "contract_size": 10, "size_unit": "metric tons",
        "quote_unit": "USD/MT", "tick_size": 1.0,
        "months": ["H", "K", "N", "U", "Z"],
    },
    "cotton": {
        "name": "Cotton No. 2", "sector": "softs", "exchange": "ICE",
        "cme_code": None, "yf_ticker": "CT=F", "wb_key": "cotton",
        "cftc_name": "cotton", "cftc_code": "033661",
        "contract_size": 50000, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.01,
        "months": ["H", "K", "N", "V", "Z"],
    },
    # ---------------- Livestock ----------------
    "live_cattle": {
        "name": "Live Cattle", "sector": "livestock", "exchange": "CME",
        "cme_code": "LE", "yf_ticker": "LE=F", "wb_key": None,
        "cftc_name": "live_cattle", "cftc_code": "057642",
        "contract_size": 40000, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.025,
        "months": ["G", "J", "M", "Q", "V", "Z"],
    },
    "lean_hogs": {
        "name": "Lean Hogs", "sector": "livestock", "exchange": "CME",
        "cme_code": "HE", "yf_ticker": "HE=F", "wb_key": None,
        "cftc_name": "lean_hogs", "cftc_code": "054642",
        "contract_size": 40000, "size_unit": "pounds",
        "quote_unit": "cents/lb", "tick_size": 0.025,
        "months": ["G", "J", "K", "M", "N", "Q", "V", "Z"],
    },
}


# Common aliases → canonical id (lowercase lookup)
_ALIASES: Dict[str, str] = {
    "crude": "wti", "crude_oil": "wti", "oil": "wti", "cl": "wti", "cl=f": "wti",
    "bz": "brent", "bz=f": "brent",
    "gas": "natgas", "natural_gas": "natgas", "ng": "natgas", "ng=f": "natgas",
    "gasoline": "rbob", "rb": "rbob", "rb=f": "rbob",
    "ho": "heating_oil", "ho=f": "heating_oil", "ulsd": "heating_oil", "diesel": "heating_oil",
    "gc": "gold", "gc=f": "gold", "xau": "gold",
    "si": "silver", "si=f": "silver", "xag": "silver",
    "hg": "copper", "hg=f": "copper",
    "pl": "platinum", "pl=f": "platinum",
    "pa": "palladium", "pa=f": "palladium",
    "zc": "corn", "zc=f": "corn", "maize": "corn",
    "zw": "wheat", "zw=f": "wheat",
    "zs": "soybeans", "zs=f": "soybeans", "soybean": "soybeans", "beans": "soybeans",
    "zl": "soybean_oil", "zl=f": "soybean_oil", "bean_oil": "soybean_oil",
    "zm": "soybean_meal", "zm=f": "soybean_meal", "meal": "soybean_meal",
    "zo": "oats", "zo=f": "oats",
    "zr": "rough_rice", "zr=f": "rough_rice", "rice": "rough_rice",
    "sb": "sugar", "sb=f": "sugar",
    "kc": "coffee", "kc=f": "coffee",
    "cc": "cocoa", "cc=f": "cocoa",
    "ct": "cotton", "ct=f": "cotton",
    "le": "live_cattle", "le=f": "live_cattle", "cattle": "live_cattle",
    "he": "lean_hogs", "he=f": "lean_hogs", "hogs": "lean_hogs",
}


def resolve_id(identifier: str) -> Optional[str]:
    """Resolve a commodity id, alias, CME code, or Yahoo ticker to a canonical id."""
    if not identifier:
        return None
    key = identifier.strip().lower()
    if key in COMMODITY_REGISTRY:
        return key
    return _ALIASES.get(key)


def get_spec(identifier: str) -> Optional[Dict[str, Any]]:
    """Get the registry spec for a commodity (accepts aliases). Includes its canonical id."""
    cid = resolve_id(identifier)
    if cid is None:
        return None
    spec = dict(COMMODITY_REGISTRY[cid])
    spec["id"] = cid
    return spec


def list_commodities(sector: Optional[str] = None) -> List[Dict[str, Any]]:
    """List registry entries, optionally filtered by sector (accepts enum value or name)."""
    if sector:
        sector_key = sector.strip().lower()
        valid = {s.value for s in CommoditySector}
        if sector_key not in valid:
            return []
        return [dict(spec, id=cid) for cid, spec in COMMODITY_REGISTRY.items()
                if spec["sector"] == sector_key]
    return [dict(spec, id=cid) for cid, spec in COMMODITY_REGISTRY.items()]


def list_sectors() -> List[str]:
    return [s.value for s in CommoditySector]
