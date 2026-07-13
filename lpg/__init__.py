"""LPG market-data storage and analytics for FinceptCommodities.

The package is intentionally stdlib-only.  Paid-source extraction lives at the
edge of the application; this module accepts normalized records, preserves
their provenance, and exposes JSON-safe read models to the HTTP server.
"""

from .catalog import ASIA_LPG_CANDIDATES, DEFAULT_SPREADS
from .models import (
    CurvePointInput,
    DatasetRowInput,
    NewsInput,
    ObservationInput,
    SeriesInput,
)
from .service import LpgService
from .store import DEFAULT_DB_PATH, LpgStore

__all__ = [
    "ASIA_LPG_CANDIDATES",
    "DEFAULT_DB_PATH",
    "DEFAULT_SPREADS",
    "CurvePointInput",
    "DatasetRowInput",
    "LpgService",
    "LpgStore",
    "NewsInput",
    "ObservationInput",
    "SeriesInput",
]
