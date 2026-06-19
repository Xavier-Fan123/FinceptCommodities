"""CFTC Commitments-of-Traders positioning analytics.

Consumes COT records as returned by scripts/cftc_data.py (CFTC Socrata API,
legacy or disaggregated report formats) and computes net positioning, weekly
changes, the COT index (3-year percentile), and extreme flags per trader class.
"""

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from .common import ok, err, json_safe
    from .config import Constants
except ImportError:
    from common import ok, err, json_safe
    from config import Constants

_DATE_KEYS = ("report_date_as_yyyy_mm_dd", "report_date", "date")

# category -> (long-key predicate substring, short complement is implied by "short")
_CATEGORIES = {
    "noncommercial": "noncomm",
    "commercial": "comm",
    "nonreportable": "nonrept",
    "producer_merchant": "prod_merc",
    "swap_dealers": "swap",
    "managed_money": "m_money",
    "other_reportables": "other_rept",
}


def _find_pair(keys: List[str], pattern: str) -> Tuple[Optional[str], Optional[str]]:
    """Find the (long, short) column pair for a trader-category pattern."""
    def match(key: str, side: str) -> bool:
        k = key.lower()
        if pattern == "comm" and k.startswith("noncomm"):
            return False  # "commercial" must not match noncommercial columns
        return pattern in k and side in k and "spread" not in k and "pct" not in k \
            and "change" not in k and "traders" not in k and "conc" not in k
    long_key = next((k for k in keys if match(k, "long")), None)
    short_key = next((k for k in keys if match(k, "short")), None)
    return long_key, short_key


class COTAnalyzer:
    """Positioning analytics over weekly COT records."""

    def analyze(self, records: Any,
                index_weeks: int = Constants.COT_INDEX_WEEKS) -> Dict[str, Any]:
        """Full positioning report from a list of weekly COT record dicts."""
        if isinstance(records, dict):
            records = records.get("data") or records.get("records") or []
        if not isinstance(records, list) or not records:
            return err("positioning requires a non-empty list of COT records")
        records = [r for r in records if isinstance(r, dict)]
        if not records:
            return err("no usable COT records")

        keys = list(records[0].keys())
        date_key = next((k for k in _DATE_KEYS if k in records[0]), None)
        if date_key is None:
            return err(f"no report date field found in keys {keys[:12]}")

        df = pd.DataFrame(records)
        df["_dt"] = pd.to_datetime(df[date_key].astype(str), errors="coerce", format="mixed")
        df = df.dropna(subset=["_dt"]).sort_values("_dt")

        # Wildcard searches can return several contract markets (e.g. GOLD and
        # MICRO GOLD); restrict the analysis to the most frequent one.
        market = None
        markets_dropped = None
        for name_key in ("market_and_exchange_names", "contract_market_name", "market"):
            if name_key in df.columns:
                counts = df[name_key].value_counts()
                market = str(counts.index[0])
                if len(counts) > 1:
                    markets_dropped = [str(m) for m in counts.index[1:]]
                    df = df[df[name_key] == market]
                break
        df = df.drop_duplicates("_dt")
        if len(df) < 2:
            return err("need at least 2 weekly COT records")

        categories = {}
        for cat, pattern in _CATEGORIES.items():
            long_key, short_key = _find_pair(list(df.columns), pattern)
            if not long_key or not short_key:
                continue
            longs = pd.to_numeric(df[long_key], errors="coerce")
            shorts = pd.to_numeric(df[short_key], errors="coerce")
            net = (longs - shorts).dropna()
            if len(net) < 2:
                continue
            window = net.tail(index_weeks)
            lo, hi = float(window.min()), float(window.max())
            current = float(net.iloc[-1])
            cot_index = 100.0 * (current - lo) / (hi - lo) if hi > lo else 50.0
            dates = df["_dt"].loc[net.index]
            categories[cat] = {
                "long": float(longs.iloc[-1]),
                "short": float(shorts.iloc[-1]),
                "net": current,
                "net_change_1w": current - float(net.iloc[-2]),
                "net_change_4w": current - float(net.iloc[-5]) if len(net) >= 5 else None,
                "cot_index": cot_index,
                "cot_index_window_weeks": int(len(window)),
                "extreme": ("bullish_extreme" if cot_index >= 90 else
                            "bearish_extreme" if cot_index <= 10 else None),
                "net_series": [
                    {"date": d.strftime("%Y-%m-%d"), "value": json_safe(v)}
                    for d, v in zip(dates.tail(52), net.tail(52))
                ],
            }

        if not categories:
            return err("could not locate any long/short column pairs in COT records",
                       columns=keys[:20])

        # Speculator-vs-hedger divergence (use best available spec/hedger proxies)
        spec = categories.get("managed_money") or categories.get("noncommercial")
        hedger = categories.get("producer_merchant") or categories.get("commercial")
        divergence = None
        if spec and hedger and spec.get("net_change_4w") is not None \
                and hedger.get("net_change_4w") is not None:
            s4, h4 = spec["net_change_4w"], hedger["net_change_4w"]
            if s4 > 0 > h4:
                divergence = "specs adding longs while hedgers add shorts (trend-confirming)"
            elif s4 < 0 < h4:
                divergence = "specs reducing longs while hedgers cover (potential turn)"

        oi = None
        oi_key = next((k for k in df.columns if k.lower().startswith("open_interest")), None)
        if oi_key:
            oi_series = pd.to_numeric(df[oi_key], errors="coerce").dropna()
            if len(oi_series) >= 2:
                oi = {
                    "current": float(oi_series.iloc[-1]),
                    "change_1w": float(oi_series.iloc[-1] - oi_series.iloc[-2]),
                    "change_4w_pct": (float(oi_series.iloc[-1] / oi_series.iloc[-5] - 1) * 100
                                      if len(oi_series) >= 5 and oi_series.iloc[-5] else None),
                }

        return ok({
            "market": market,
            "other_markets_excluded": markets_dropped,
            "as_of": df["_dt"].iloc[-1].strftime("%Y-%m-%d"),
            "weeks_of_data": int(len(df)),
            "open_interest": oi,
            "categories": categories,
            "divergence_signal": divergence,
            "notes": "cot_index = 100*(net - min)/(max - min) over trailing window; "
                     ">=90 bullish extreme, <=10 bearish extreme",
        })
