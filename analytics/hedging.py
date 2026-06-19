"""Commodity hedging analytics.

Minimum-variance hedge ratios, hedge effectiveness, contract sizing using
registry specs, and cross-hedge candidate ranking.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

try:
    from .common import ok, err, normalize_history, pct_returns
    from .config import get_spec
except ImportError:
    from common import ok, err, normalize_history, pct_returns
    from config import get_spec


class HedgeAnalyzer:
    """Hedge design from spot and futures price histories."""

    def min_variance_hedge(self, spot_history: Any, futures_history: Any,
                           use_returns: bool = True) -> Dict[str, Any]:
        """Minimum-variance hedge ratio h* = OLS slope of spot on futures.

        use_returns=True regresses pct returns (scale-free); False regresses
        price changes (when spot and futures share units).
        """
        try:
            spot = normalize_history(spot_history)["close"].rename("spot")
            fut = normalize_history(futures_history)["close"].rename("fut")
        except ValueError as exc:
            return err(f"min_variance_hedge: {exc}")
        joined = pd.concat([spot, fut], axis=1, join="inner").dropna()
        if len(joined) < 30:
            return err("need at least 30 overlapping observations",
                       overlapping=len(joined))

        if use_returns:
            ds = joined["spot"].pct_change().dropna()
            df_ = joined["fut"].pct_change().dropna()
        else:
            ds = joined["spot"].diff().dropna()
            df_ = joined["fut"].diff().dropna()
        ds, df_ = ds.align(df_, join="inner")

        fvar = float(df_.var(ddof=1))
        if fvar <= 0:
            return err("futures series has zero variance")
        h_star = float(ds.cov(df_) / fvar)
        corr = float(ds.corr(df_))
        r_squared = corr ** 2

        # Residual (basis) risk: stdev of unhedged vs hedged position changes
        hedged = ds - h_star * df_
        risk_reduction_pct = (1 - float(hedged.std(ddof=1)) / float(ds.std(ddof=1))) * 100 \
            if float(ds.std(ddof=1)) > 0 else None

        return ok({
            "hedge_ratio": h_star,
            "hedge_effectiveness_r2": r_squared,
            "correlation": corr,
            "observations": int(len(ds)),
            "basis": {
                "current": float(joined["spot"].iloc[-1] - joined["fut"].iloc[-1]),
                "mean": float((joined["spot"] - joined["fut"]).mean()),
                "std": float((joined["spot"] - joined["fut"]).std(ddof=1)),
            },
            "residual_risk_reduction_pct": risk_reduction_pct,
            "regression_on": "returns" if use_returns else "price_changes",
            "formula": "h* = Cov(dS, dF) / Var(dF); effectiveness = R^2",
        })

    def contracts_needed(self, exposure_units: float, commodity: str,
                         hedge_ratio: float = 1.0,
                         exposure_value: Optional[float] = None,
                         futures_price: Optional[float] = None) -> Dict[str, Any]:
        """Number of futures contracts for a physical or value exposure.

        Units-based: N = exposure_units * h / contract_size.
        Value-based (if exposure_value+futures_price given):
        N = (exposure_value * h) / (futures_price * contract_size).
        """
        spec = get_spec(commodity)
        if spec is None:
            return err(f"unknown commodity '{commodity}'")
        size = float(spec["contract_size"])
        try:
            h = float(hedge_ratio)
        except (TypeError, ValueError):
            return err("hedge_ratio must be numeric")

        result: Dict[str, Any] = {
            "commodity": spec["id"],
            "contract_size": size,
            "size_unit": spec["size_unit"],
            "hedge_ratio": h,
        }
        if exposure_value is not None and futures_price is not None:
            n = float(exposure_value) * h / (float(futures_price) * size)
            result["basis"] = "value"
            result["exposure_value"] = float(exposure_value)
            result["futures_price"] = float(futures_price)
        else:
            try:
                n = float(exposure_units) * h / size
            except (TypeError, ValueError):
                return err("exposure_units must be numeric")
            result["basis"] = "units"
            result["exposure_units"] = float(exposure_units)
        result["contracts_exact"] = n
        result["contracts_rounded"] = int(round(n))
        result["rounding_residual_pct"] = (
            (int(round(n)) - n) / n * 100 if n else None)
        return ok(result)

    def cross_hedge_rank(self, target_history: Any,
                         candidates: Dict[str, Any]) -> Dict[str, Any]:
        """Rank candidate futures for hedging a target exposure by |correlation|.

        candidates: {label: history, ...}
        """
        try:
            target = pct_returns(normalize_history(target_history)["close"]).rename("target")
        except ValueError as exc:
            return err(f"cross_hedge_rank target: {exc}")
        if not isinstance(candidates, dict) or not candidates:
            return err("candidates must be a non-empty dict of histories")

        ranked = []
        skipped = {}
        for label, hist in candidates.items():
            try:
                cand = pct_returns(normalize_history(hist)["close"]).rename("cand")
            except ValueError as exc:
                skipped[label] = str(exc)
                continue
            joined = pd.concat([target, cand], axis=1, join="inner").dropna()
            if len(joined) < 30:
                skipped[label] = f"only {len(joined)} overlapping observations"
                continue
            cvar = float(joined["cand"].var(ddof=1))
            corr = float(joined["target"].corr(joined["cand"]))
            ranked.append({
                "candidate": label,
                "correlation": corr,
                "abs_correlation": abs(corr),
                "hedge_ratio": float(joined["target"].cov(joined["cand"]) / cvar) if cvar > 0 else None,
                "effectiveness_r2": corr ** 2,
                "observations": int(len(joined)),
            })
        if not ranked:
            return err("no usable candidates", skipped=skipped)
        ranked.sort(key=lambda r: r["abs_correlation"], reverse=True)
        return ok({
            "best_hedge": ranked[0]["candidate"],
            "ranking": ranked,
            "skipped": skipped or None,
        })
