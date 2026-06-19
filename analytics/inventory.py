"""Commodity inventory analytics.

Stocks vs 5-year seasonal bands (EIA-style), days of supply, build/draw
streaks, and inventory-price relationship. Input is any weekly/monthly level
series, e.g. EIA crude stocks from scripts/eia_data.py or AkShare warehouse
receipts.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

try:
    from .common import ok, err, normalize_history, series_tail
except ImportError:
    from common import ok, err, normalize_history, series_tail


class InventoryAnalyzer:
    """Inventory-level analytics on a dated stocks series."""

    def analyze(self, records: Any, value_key: Optional[str] = None,
                date_key: Optional[str] = None,
                demand_rate_per_day: Optional[float] = None,
                band_years: int = 5, unit: str = "units") -> Dict[str, Any]:
        try:
            df = normalize_history(records, value_key=value_key, date_key=date_key)
        except ValueError as exc:
            return err(f"inventory: {exc}")
        stocks = df["close"]

        current = float(stocks.iloc[-1])
        as_of = stocks.index[-1]

        # Same-week-of-year band over prior `band_years` years
        frame = stocks.to_frame("level")
        frame["year"] = frame.index.year
        frame["week"] = frame.index.isocalendar().week.astype(int)
        current_year = int(frame["year"].max())
        current_week = int(frame["week"].iloc[-1])
        hist = frame[(frame["year"] < current_year)
                     & (frame["year"] >= current_year - band_years)]
        same_week = hist[hist["week"] == current_week]["level"]

        band = None
        if len(same_week) >= 2:
            avg = float(same_week.mean())
            std = float(same_week.std(ddof=1))
            band = {
                "week_of_year": current_week,
                "years_in_band": int(same_week.count()),
                "min": float(same_week.min()),
                "max": float(same_week.max()),
                "avg": avg,
                "vs_avg_pct": (current - avg) / avg * 100 if avg else None,
                "zscore": (current - avg) / std if std > 0 else None,
                "position": ("above_5yr_range" if current > float(same_week.max())
                             else "below_5yr_range" if current < float(same_week.min())
                             else "within_5yr_range"),
            }

        # Build/draw streak
        changes = stocks.diff().dropna()
        last_change = float(changes.iloc[-1]) if len(changes) else None
        streak = 0
        if len(changes):
            sign = np.sign(changes.iloc[-1])
            for v in reversed(changes.values):
                if np.sign(v) == sign and sign != 0:
                    streak += 1
                else:
                    break
        streak_dir = ("build" if last_change and last_change > 0
                      else "draw" if last_change and last_change < 0 else "flat")

        days_of_supply = None
        if demand_rate_per_day and demand_rate_per_day > 0:
            days_of_supply = current / float(demand_rate_per_day)

        # Seasonal band payload for charting (weekly avg/min/max across years)
        weekly = hist.groupby("week")["level"].agg(["min", "max", "mean"])
        cur_year_frame = frame[frame["year"] == current_year].groupby("week")["level"].mean()
        chart = [{
            "week": int(w),
            "hist_min": float(weekly.loc[w, "min"]) if w in weekly.index else None,
            "hist_max": float(weekly.loc[w, "max"]) if w in weekly.index else None,
            "hist_avg": float(weekly.loc[w, "mean"]) if w in weekly.index else None,
            "current_year": float(cur_year_frame.loc[w]) if w in cur_year_frame.index else None,
        } for w in range(1, 54)]

        return ok({
            "as_of": as_of.strftime("%Y-%m-%d"),
            "unit": unit,
            "current_level": current,
            "last_change": last_change,
            "streak": {"direction": streak_dir, "periods": int(streak)},
            "five_year_band": band,
            "days_of_supply": days_of_supply,
            "seasonal_chart": {"current_year": current_year, "weeks": chart},
            "recent_series": series_tail(stocks, 104),
        })

    def price_inventory_relationship(self, stock_records: Any, price_history: Any,
                                     stock_value_key: Optional[str] = None) -> Dict[str, Any]:
        """Correlation between inventory changes and price changes (weekly aligned)."""
        try:
            stocks = normalize_history(stock_records, value_key=stock_value_key)["close"]
            prices = normalize_history(price_history)["close"]
        except ValueError as exc:
            return err(f"price_inventory_relationship: {exc}")
        weekly_stocks = stocks.resample("W").last().dropna()
        weekly_prices = prices.resample("W").last().dropna()
        joined = pd.concat(
            [weekly_stocks.pct_change().rename("d_stocks"),
             weekly_prices.pct_change().rename("d_price")],
            axis=1, join="inner").dropna()
        if len(joined) < 8:
            return err("insufficient overlapping weekly data", overlapping_weeks=len(joined))
        corr = float(joined["d_stocks"].corr(joined["d_price"]))
        return ok({
            "overlapping_weeks": int(len(joined)),
            "correlation_dstocks_dprice": corr,
            "interpretation": ("inventory builds tend to coincide with price weakness"
                               if corr < -0.1 else
                               "inventory draws tend to coincide with price weakness"
                               if corr > 0.1 else
                               "weak contemporaneous inventory-price relationship"),
        })
