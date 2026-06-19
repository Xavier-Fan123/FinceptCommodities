"""Commodity seasonality analytics.

Calendar-month return statistics, EIA-style seasonal price bands
(N-year min/max/avg with current-year overlay), and seasonal-strength tests.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

try:
    from scipy import stats as scipy_stats
except ImportError:  # scipy is bundled, but degrade gracefully
    scipy_stats = None

try:
    from .common import ok, err, normalize_history
except ImportError:
    from common import ok, err, normalize_history

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class SeasonalityAnalyzer:
    """Seasonal pattern analysis on a daily/weekly/monthly price history."""

    def analyze(self, history: Any, years: int = 10,
                value_key: Optional[str] = None) -> Dict[str, Any]:
        """Composite seasonality report: monthly stats, bands, strength test."""
        try:
            df = normalize_history(history, value_key=value_key)
        except ValueError as exc:
            return err(f"seasonality: {exc}")

        close = df["close"]
        span_years = (close.index[-1] - close.index[0]).days / 365.25
        if span_years < 2:
            return err("seasonality needs at least ~2 years of history",
                       span_years=round(span_years, 2))

        cutoff = close.index[-1] - pd.DateOffset(years=years)
        close = close[close.index >= cutoff]

        monthly = self._monthly_stats(close)
        bands = self._seasonal_bands(close)
        strength = self._seasonal_strength(close)

        ranked = [m for m in monthly if m["avg_return_pct"] is not None]
        ranked.sort(key=lambda m: m["avg_return_pct"], reverse=True)

        return ok({
            "window_years": round(min(span_years, years), 2),
            "observations": int(len(close)),
            "monthly_stats": monthly,
            "best_months": [m["month"] for m in ranked[:3]],
            "worst_months": [m["month"] for m in ranked[-3:]][::-1],
            "seasonal_strength": strength,
            "seasonal_bands": bands,
        })

    def _monthly_stats(self, close: pd.Series) -> list:
        """Average return, median, win rate per calendar month (month-end returns)."""
        monthly_close = close.resample("ME").last().dropna()
        rets = monthly_close.pct_change().dropna() * 100
        out = []
        for month in range(1, 13):
            grp = rets[rets.index.month == month]
            if len(grp) > 0:
                out.append({
                    "month": _MONTHS[month - 1],
                    "avg_return_pct": float(grp.mean()),
                    "median_return_pct": float(grp.median()),
                    "win_rate_pct": float((grp > 0).mean() * 100),
                    "years_observed": int(len(grp)),
                })
            else:
                out.append({"month": _MONTHS[month - 1], "avg_return_pct": None,
                            "median_return_pct": None, "win_rate_pct": None,
                            "years_observed": 0})
        return out

    def _seasonal_bands(self, close: pd.Series) -> Dict[str, Any]:
        """Per calendar month: min/max/avg of monthly average price across past
        years, plus the current (latest) year's monthly averages as overlay."""
        frame = close.to_frame("close")
        frame["year"] = frame.index.year
        frame["month"] = frame.index.month
        monthly_avg = frame.groupby(["year", "month"])["close"].mean().reset_index()

        current_year = int(frame["year"].max())
        hist = monthly_avg[monthly_avg["year"] < current_year]
        cur = monthly_avg[monthly_avg["year"] == current_year]

        bands = []
        for month in range(1, 13):
            h = hist[hist["month"] == month]["close"]
            c = cur[cur["month"] == month]["close"]
            bands.append({
                "month": _MONTHS[month - 1],
                "hist_min": float(h.min()) if len(h) else None,
                "hist_max": float(h.max()) if len(h) else None,
                "hist_avg": float(h.mean()) if len(h) else None,
                "current_year": float(c.iloc[0]) if len(c) else None,
            })
        return {"current_year": current_year,
                "years_in_band": int(hist["year"].nunique()),
                "bands": bands}

    def _seasonal_strength(self, close: pd.Series) -> Dict[str, Any]:
        """One-way ANOVA across month-of-year return groups (Kruskal fallback)."""
        monthly_close = close.resample("ME").last().dropna()
        rets = monthly_close.pct_change().dropna()
        groups = [rets[rets.index.month == m].values for m in range(1, 13)]
        groups = [g for g in groups if len(g) >= 2]
        if scipy_stats is None or len(groups) < 6:
            return {"test": None, "note": "insufficient data or scipy unavailable"}
        try:
            f_stat, p_value = scipy_stats.f_oneway(*groups)
            result = {"test": "anova_f", "statistic": float(f_stat), "p_value": float(p_value)}
        except (ValueError, TypeError):
            try:
                h_stat, p_value = scipy_stats.kruskal(*groups)
                result = {"test": "kruskal", "statistic": float(h_stat), "p_value": float(p_value)}
            except (ValueError, TypeError):
                return {"test": None, "note": "test failed on this data"}
        result["significant_at_10pct"] = bool(result["p_value"] < 0.10)
        result["interpretation"] = (
            "statistically detectable seasonal pattern" if result["p_value"] < 0.10
            else "no statistically significant monthly seasonality"
        )
        return result
