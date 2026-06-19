"""Commodity index construction and basket analytics.

Excess-return and total-return index construction from futures price series,
and weighted multi-commodity basket performance with attribution.

Note: continuous front-month series (e.g. Yahoo CL=F) embed roll gaps; the
excess-return index here reflects that continuous series. For exact GSCI/BCOM
replication, per-contract settlement strips and roll calendars are required.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

try:
    from .common import ok, err, normalize_history, pct_returns, series_tail
    from .config import Constants
except ImportError:
    from common import ok, err, normalize_history, pct_returns, series_tail
    from config import Constants

ANN = Constants.TRADING_DAYS_IN_YEAR


class CommodityIndexBuilder:
    """Index/basket construction over futures histories."""

    def excess_return_index(self, history: Any, base: float = 100.0) -> Dict[str, Any]:
        """Excess-return index: cumulative product of futures returns."""
        try:
            close = normalize_history(history)["close"]
        except ValueError as exc:
            return err(f"excess_return_index: {exc}")
        rets = pct_returns(close)
        if len(rets) < 5:
            return err("need at least 5 observations")
        index = base * (1 + rets).cumprod()
        years = max((index.index[-1] - index.index[0]).days / 365.25, 1e-6)
        cagr = (float(index.iloc[-1]) / base) ** (1 / years) - 1
        return ok({
            "index_type": "excess_return",
            "base": base,
            "current": float(index.iloc[-1]),
            "cagr_pct": cagr * 100,
            "annualized_vol_pct": float(rets.std(ddof=1) * np.sqrt(ANN) * 100),
            "series": series_tail(index, 520),
        })

    def total_return_index(self, history: Any,
                           collateral_rate_annual: float = Constants.DEFAULT_RISK_FREE_RATE,
                           base: float = 100.0) -> Dict[str, Any]:
        """Total-return index: futures returns plus collateral yield accrual.

        TR_t = TR_{t-1} * (1 + r_fut + rf * dt) with dt in years between
        observations (fully collateralized futures position convention).
        """
        try:
            close = normalize_history(history)["close"]
        except ValueError as exc:
            return err(f"total_return_index: {exc}")
        rets = pct_returns(close)
        if len(rets) < 5:
            return err("need at least 5 observations")
        dt_days = close.index.to_series().diff().dt.days.reindex(rets.index).fillna(1)
        dt_years = dt_days / 365.25
        combined = rets + collateral_rate_annual * dt_years
        index = base * (1 + combined).cumprod()
        years = max((index.index[-1] - index.index[0]).days / 365.25, 1e-6)
        cagr = (float(index.iloc[-1]) / base) ** (1 / years) - 1
        er_index = base * (1 + rets).cumprod()
        return ok({
            "index_type": "total_return",
            "base": base,
            "collateral_rate_annual": collateral_rate_annual,
            "current": float(index.iloc[-1]),
            "cagr_pct": cagr * 100,
            "excess_return_current": float(er_index.iloc[-1]),
            "collateral_contribution_pct":
                (float(index.iloc[-1]) - float(er_index.iloc[-1])) / base * 100,
            "series": series_tail(index, 520),
        })

    def basket_performance(self, histories: Dict[str, Any],
                           weights: Optional[Dict[str, float]] = None,
                           base: float = 100.0,
                           rebalance: str = "monthly") -> Dict[str, Any]:
        """Weighted commodity basket with per-component attribution.

        histories: {label: history}; weights: {label: weight} (defaults equal,
        normalized to 1). Weights reset at each rebalance ('monthly'|'never').
        """
        if not isinstance(histories, dict) or len(histories) < 2:
            return err("basket requires a dict of >=2 histories")
        series = {}
        skipped = {}
        for label, hist in histories.items():
            try:
                series[label] = pct_returns(normalize_history(hist)["close"]).rename(label)
            except ValueError as exc:
                skipped[label] = str(exc)
        if len(series) < 2:
            return err("fewer than 2 usable series", skipped=skipped)

        rets = pd.concat(series.values(), axis=1, join="inner").dropna()
        if len(rets) < 20:
            return err("fewer than 20 overlapping observations", skipped=skipped)
        labels = list(rets.columns)

        if weights:
            w = np.array([float(weights.get(label, 0.0)) for label in labels])
            if w.sum() <= 0:
                return err("weights must sum to a positive number")
        else:
            w = np.ones(len(labels))
        w = w / w.sum()

        if rebalance == "never":
            # Buy-and-hold: component values drift with cumulative returns
            growth = (1 + rets).cumprod()
            port_value = growth.mul(w, axis=1).sum(axis=1)
            port_rets = port_value.pct_change().fillna(port_value.iloc[0] - 1)
        else:
            # Periodic reset to target weights (monthly): weight each return row
            port_rets = rets.mul(w, axis=1).sum(axis=1)

        index = base * (1 + port_rets).cumprod()
        years = max((index.index[-1] - index.index[0]).days / 365.25, 1e-6)
        cagr = (float(index.iloc[-1]) / base) ** (1 / years) - 1
        vol = float(port_rets.std(ddof=1) * np.sqrt(ANN))

        attribution = []
        for i, label in enumerate(labels):
            comp_total = float((1 + rets[label]).prod() - 1)
            attribution.append({
                "component": label,
                "weight": float(w[i]),
                "total_return_pct": comp_total * 100,
                "weighted_contribution_pct": float(w[i]) * comp_total * 100,
                "annualized_vol_pct": float(rets[label].std(ddof=1) * np.sqrt(ANN) * 100),
            })
        attribution.sort(key=lambda a: a["weighted_contribution_pct"], reverse=True)

        return ok({
            "components": labels,
            "rebalance": rebalance,
            "observations": int(len(rets)),
            "index_current": float(index.iloc[-1]),
            "cagr_pct": cagr * 100,
            "annualized_vol_pct": vol * 100,
            "return_risk_ratio": cagr / vol if vol > 0 else None,
            "attribution": attribution,
            "series": series_tail(index, 520),
            "skipped": skipped or None,
        })
