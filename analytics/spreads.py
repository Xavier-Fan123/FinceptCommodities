"""Commodity spread analytics.

Processing margins (crack, crush, spark), inter-commodity ratio/differential
spreads, calendar spreads, and mean-reversion statistics on spread series.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from .common import ok, err, align_closes, normalize_history, series_tail
    from .config import Constants
except ImportError:
    from common import ok, err, align_closes, normalize_history, series_tail
    from config import Constants

GAL = Constants.GALLONS_PER_BARREL


class SpreadAnalyzer:
    """Spread construction and statistics."""

    # ---------------- Processing margins (single-point) ----------------

    def crack_spread(self, crude_usd_bbl: float, gasoline_usd_gal: float,
                     heating_oil_usd_gal: float,
                     ratio: Tuple[int, int, int] = (3, 2, 1)) -> Dict[str, Any]:
        """Refining crack spread in USD/bbl.

        ratio = (crude, gasoline, distillate) barrels, e.g. (3,2,1) or (2,1,1).
        Spread = (n_rb*RB*42 + n_ho*HO*42 - n_cl*CL) / n_cl
        """
        try:
            n_cl, n_rb, n_ho = (int(x) for x in ratio)
            cl, rb, ho = float(crude_usd_bbl), float(gasoline_usd_gal), float(heating_oil_usd_gal)
        except (TypeError, ValueError):
            return err("crack_spread requires numeric prices and an integer ratio")
        if n_cl <= 0 or n_cl != n_rb + n_ho:
            return err("ratio must satisfy crude = gasoline + distillate (e.g. 3-2-1, 2-1-1)")
        product_value = n_rb * rb * GAL + n_ho * ho * GAL
        spread = (product_value - n_cl * cl) / n_cl
        return ok({
            "spread_usd_per_bbl": spread,
            "ratio": f"{n_cl}-{n_rb}-{n_ho}",
            "inputs": {"crude_usd_bbl": cl, "gasoline_usd_gal": rb, "heating_oil_usd_gal": ho},
            "components": {
                "gasoline_usd_bbl": rb * GAL,
                "heating_oil_usd_bbl": ho * GAL,
                "product_value_per_crude_bbl": product_value / n_cl,
            },
            "formula": "(n_rb*RB*42 + n_ho*HO*42 - n_cl*CL) / n_cl",
        })

    def crush_spread(self, soybeans_cents_bu: float, meal_usd_ton: float,
                     oil_cents_lb: float) -> Dict[str, Any]:
        """CBOT board crush margin in USD/bu.

        Crush = 0.022 * Meal($/short ton) + 0.11 * Oil(cents/lb) - Beans($/bu)
        (1 bu of soybeans yields ~44 lb meal and ~11 lb oil.)
        """
        try:
            zs = float(soybeans_cents_bu) / 100.0  # cents/bu -> $/bu
            zm = float(meal_usd_ton)
            zl = float(oil_cents_lb)
        except (TypeError, ValueError):
            return err("crush_spread requires numeric ZS (cents/bu), ZM ($/ton), ZL (cents/lb)")
        meal_value = 0.022 * zm
        oil_value = 0.11 * zl
        spread = meal_value + oil_value - zs
        return ok({
            "crush_usd_per_bu": spread,
            "inputs": {"soybeans_usd_bu": zs, "meal_usd_ton": zm, "oil_cents_lb": zl},
            "components": {"meal_value_usd_bu": meal_value, "oil_value_usd_bu": oil_value},
            "formula": "0.022*ZM + 0.11*ZL - ZS",
        })

    def spark_spread(self, power_usd_mwh: float, gas_usd_mmbtu: float,
                     heat_rate: float = Constants.DEFAULT_HEAT_RATE) -> Dict[str, Any]:
        """Gas-fired generation margin in USD/MWh: power - gas * heat_rate."""
        try:
            power = float(power_usd_mwh)
            gas = float(gas_usd_mmbtu)
            hr = float(heat_rate)
        except (TypeError, ValueError):
            return err("spark_spread requires numeric power, gas, heat_rate")
        spread = power - gas * hr
        return ok({
            "spark_spread_usd_per_mwh": spread,
            "inputs": {"power_usd_mwh": power, "gas_usd_mmbtu": gas, "heat_rate_mmbtu_per_mwh": hr},
            "fuel_cost_usd_per_mwh": gas * hr,
            "formula": "power - gas * heat_rate",
        })

    # ---------------- Series spreads ----------------

    def ratio_spread(self, series_a: Any, series_b: Any,
                     label_a: str = "A", label_b: str = "B",
                     window: int = 60) -> Dict[str, Any]:
        """Ratio A/B over aligned history with mean-reversion stats (e.g. gold/silver)."""
        try:
            joined = align_closes(series_a, series_b)
        except ValueError as exc:
            return err(f"ratio_spread: {exc}")
        ratio = (joined["a"] / joined["b"]).dropna()
        stats = self._spread_stats(ratio, window)
        return ok({
            "spread_type": "ratio",
            "pair": f"{label_a}/{label_b}",
            "current": float(ratio.iloc[-1]),
            "series": series_tail(ratio),
            **stats,
        })

    def diff_spread(self, series_a: Any, series_b: Any,
                    label_a: str = "A", label_b: str = "B",
                    window: int = 60) -> Dict[str, Any]:
        """Differential A-B over aligned history (e.g. WTI-Brent, calendar spreads)."""
        try:
            joined = align_closes(series_a, series_b)
        except ValueError as exc:
            return err(f"diff_spread: {exc}")
        diff = (joined["a"] - joined["b"]).dropna()
        stats = self._spread_stats(diff, window)
        return ok({
            "spread_type": "differential",
            "pair": f"{label_a}-{label_b}",
            "current": float(diff.iloc[-1]),
            "series": series_tail(diff),
            **stats,
        })

    def crack_spread_series(self, crude_history: Any, gasoline_history: Any,
                            heating_oil_history: Any,
                            ratio: Tuple[int, int, int] = (3, 2, 1),
                            window: int = 60) -> Dict[str, Any]:
        """Historical crack spread series with stats from three aligned histories."""
        try:
            n_cl, n_rb, n_ho = (int(x) for x in ratio)
            cl = normalize_history(crude_history)["close"].rename("cl")
            rb = normalize_history(gasoline_history)["close"].rename("rb")
            ho = normalize_history(heating_oil_history)["close"].rename("ho")
        except (TypeError, ValueError) as exc:
            return err(f"crack_spread_series: {exc}")
        if n_cl <= 0 or n_cl != n_rb + n_ho:
            return err("ratio must satisfy crude = gasoline + distillate")
        joined = pd.concat([cl, rb, ho], axis=1, join="inner").dropna()
        if joined.empty:
            return err("crack_spread_series: no overlapping dates")
        spread = (n_rb * joined["rb"] * GAL + n_ho * joined["ho"] * GAL
                  - n_cl * joined["cl"]) / n_cl
        stats = self._spread_stats(spread, window)
        return ok({
            "spread_type": "crack",
            "ratio": f"{n_cl}-{n_rb}-{n_ho}",
            "unit": "USD/bbl",
            "current": float(spread.iloc[-1]),
            "series": series_tail(spread),
            **stats,
        })

    def single_crack_series(self, crude_history: Any, product_history: Any,
                            product_label: str = "Product",
                            window: int = 60) -> Dict[str, Any]:
        """Single-product crack in USD/bbl: product($/gal)*42 - crude($/bbl).

        The gasoline (RBOB-WTI) or distillate (ULSD-WTI) refining margin for
        one cut, isolating which product is pulling the barrel.
        """
        try:
            cl = normalize_history(crude_history)["close"].rename("cl")
            pr = normalize_history(product_history)["close"].rename("pr")
        except (TypeError, ValueError) as exc:
            return err(f"single_crack_series: {exc}")
        joined = pd.concat([cl, pr], axis=1, join="inner").dropna()
        if joined.empty:
            return err("single_crack_series: no overlapping dates")
        spread = joined["pr"] * GAL - joined["cl"]
        stats = self._spread_stats(spread, window)
        return ok({
            "spread_type": "crack",
            "product": product_label,
            "unit": "USD/bbl",
            "current": float(spread.iloc[-1]),
            "series": series_tail(spread),
            **stats,
        })

    def crush_spread_series(self, soybeans_history: Any, meal_history: Any,
                            oil_history: Any, window: int = 60) -> Dict[str, Any]:
        """Historical board crush series (ZS cents/bu, ZM $/ton, ZL cents/lb)."""
        try:
            zs = normalize_history(soybeans_history)["close"].rename("zs")
            zm = normalize_history(meal_history)["close"].rename("zm")
            zl = normalize_history(oil_history)["close"].rename("zl")
        except ValueError as exc:
            return err(f"crush_spread_series: {exc}")
        joined = pd.concat([zs, zm, zl], axis=1, join="inner").dropna()
        if joined.empty:
            return err("crush_spread_series: no overlapping dates")
        spread = 0.022 * joined["zm"] + 0.11 * joined["zl"] - joined["zs"] / 100.0
        stats = self._spread_stats(spread, window)
        return ok({
            "spread_type": "crush",
            "unit": "USD/bu",
            "current": float(spread.iloc[-1]),
            "series": series_tail(spread),
            **stats,
        })

    # ---------------- Internals ----------------

    def _spread_stats(self, s: pd.Series, window: int) -> Dict[str, Any]:
        """Mean-reversion statistics for a spread series."""
        s = s.dropna()
        out: Dict[str, Any] = {
            "observations": int(len(s)),
            "mean": float(s.mean()),
            "std": float(s.std(ddof=1)) if len(s) > 2 else None,
            "min": float(s.min()),
            "max": float(s.max()),
            "percentile_of_current": float((s <= s.iloc[-1]).mean() * 100),
        }
        if len(s) >= max(window, 10):
            roll_mean = s.rolling(window).mean()
            roll_std = s.rolling(window).std(ddof=1)
            z = (s - roll_mean) / roll_std.replace(0, np.nan)
            z_last = z.dropna()
            out["rolling_window"] = window
            out["zscore_current"] = float(z_last.iloc[-1]) if not z_last.empty else None
        # AR(1) half-life of mean reversion: ds_t = a + b*s_{t-1}, hl = -ln2/ln(1+b)
        if len(s) >= 30:
            lag = s.shift(1).dropna()
            ds = s.diff().dropna()
            lag, ds = lag.align(ds, join="inner")
            if len(lag) > 10 and float(lag.std()) > 0:
                b = float(np.polyfit(lag.values, ds.values, 1)[0])
                if -1 < b < 0:
                    out["mean_reversion_half_life_days"] = float(-math.log(2) / math.log(1 + b))
                else:
                    out["mean_reversion_half_life_days"] = None
        return out
