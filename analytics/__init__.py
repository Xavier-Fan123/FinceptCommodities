"""Commodities Analytics Module

Deep commodity analytics for Fincept Terminal: term structure, processing
spreads, seasonality, COT positioning, inventory, risk, hedging, and index
construction across energy, metals, and agriculture.

All analyzers are pure computation over inline JSON-shaped data; network
fetching is isolated in data_adapter.py. CLI entry point: cli.py.
"""

from .config import (
    CommoditySector,
    Constants,
    COMMODITY_REGISTRY,
    MONTH_CODES,
    get_spec,
    resolve_id,
    list_commodities,
    list_sectors,
)
from .term_structure import TermStructureAnalyzer
from .spreads import SpreadAnalyzer
from .seasonality import SeasonalityAnalyzer
from .positioning import COTAnalyzer
from .inventory import InventoryAnalyzer
from .risk import CommodityRiskAnalyzer
from .hedging import HedgeAnalyzer
from .index_analytics import CommodityIndexBuilder

__all__ = [
    "CommoditySector", "Constants", "COMMODITY_REGISTRY", "MONTH_CODES",
    "get_spec", "resolve_id", "list_commodities", "list_sectors",
    "TermStructureAnalyzer", "SpreadAnalyzer", "SeasonalityAnalyzer",
    "COTAnalyzer", "InventoryAnalyzer", "CommodityRiskAnalyzer",
    "HedgeAnalyzer", "CommodityIndexBuilder",
]
