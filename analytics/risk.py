"""Commodity risk analytics.

Realized and EWMA volatility, historical VaR/CVaR, drawdowns, higher moments,
cross-commodity correlation matrices, and rolling beta to a benchmark.
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


class CommodityRiskAnalyzer:
    """Risk metrics on price histories."""

    def analyze(self, history: Any, benchmark: Any = None,
                value_key: Optional[str] = None,
                ewma_lambda: float = Constants.EWMA_LAMBDA) -> Dict[str, Any]:
        try:
            close = normalize_history(history, value_key=value_key)["close"]
        except ValueError as exc:
            return err(f"risk: {exc}")
        rets = pct_returns(close)
        if len(rets) < 20:
            return err("need at least 20 return observations", observations=len(rets))

        daily_vol = float(rets.std(ddof=1))
        downside = rets[rets < 0]
        ewma_var = self._ewma_variance(rets.values, ewma_lambda)

        var95 = float(np.percentile(rets, 5))
        var99 = float(np.percentile(rets, 1))
        cvar95 = float(rets[rets <= var95].mean()) if (rets <= var95).any() else var95
        cvar99 = float(rets[rets <= var99].mean()) if (rets <= var99).any() else var99

        cum = (1 + rets).cumprod()
        running_max = cum.cummax()
        drawdown = cum / running_max - 1
        max_dd = float(drawdown.min())
        dd_end = drawdown.idxmin()
        dd_start = cum.loc[:dd_end].idxmax()

        payload: Dict[str, Any] = {
            "observations": int(len(rets)),
            "period_start": close.index[0].strftime("%Y-%m-%d"),
            "period_end": close.index[-1].strftime("%Y-%m-%d"),
            "last_price": float(close.iloc[-1]),
            "return_total_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
            "volatility": {
                "daily_pct": daily_vol * 100,
                "annualized_pct": daily_vol * np.sqrt(ANN) * 100,
                "ewma_annualized_pct": float(np.sqrt(ewma_var * ANN) * 100),
                "ewma_lambda": ewma_lambda,
                "downside_deviation_annualized_pct":
                    float(downside.std(ddof=1) * np.sqrt(ANN) * 100) if len(downside) > 2 else None,
            },
            "value_at_risk": {
                "var_95_daily_pct": var95 * 100,
                "var_99_daily_pct": var99 * 100,
                "cvar_95_daily_pct": cvar95 * 100,
                "cvar_99_daily_pct": cvar99 * 100,
                "method": "historical",
            },
            "max_drawdown": {
                "depth_pct": max_dd * 100,
                "peak_date": dd_start.strftime("%Y-%m-%d"),
                "trough_date": dd_end.strftime("%Y-%m-%d"),
            },
            "moments": {
                "skewness": float(rets.skew()),
                "excess_kurtosis": float(rets.kurtosis()),
            },
            "drawdown_series": series_tail(drawdown * 100, 260),
        }

        if benchmark is not None:
            beta_part = self._beta(rets, benchmark)
            payload["benchmark"] = beta_part
        return ok(payload)

    def correlation_matrix(self, histories: Dict[str, Any],
                           min_overlap: int = 30) -> Dict[str, Any]:
        """Correlation matrix of daily returns across multiple commodities.

        histories: {label: history-records, ...}
        """
        if not isinstance(histories, dict) or len(histories) < 2:
            return err("correlation_matrix requires a dict of >=2 histories")
        series = {}
        skipped = {}
        for label, hist in histories.items():
            try:
                series[label] = pct_returns(normalize_history(hist)["close"]).rename(label)
            except ValueError as exc:
                skipped[label] = str(exc)
        if len(series) < 2:
            return err("fewer than 2 usable series", skipped=skipped)
        joined = pd.concat(series.values(), axis=1, join="inner").dropna()
        if len(joined) < min_overlap:
            return err(f"only {len(joined)} overlapping observations (need {min_overlap})",
                       skipped=skipped)
        corr = joined.corr()
        labels = list(corr.columns)
        pairs = []
        for i, a in enumerate(labels):
            for b in labels[i + 1:]:
                pairs.append({"a": a, "b": b, "correlation": float(corr.loc[a, b])})
        pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)
        return ok({
            "labels": labels,
            "observations": int(len(joined)),
            "matrix": {a: {b: float(corr.loc[a, b]) for b in labels} for a in labels},
            "pairs_ranked": pairs,
            "skipped": skipped or None,
        })

    def rolling_beta(self, history: Any, benchmark: Any,
                     window: int = 60) -> Dict[str, Any]:
        """Rolling OLS beta of the commodity vs a benchmark series."""
        try:
            a = pct_returns(normalize_history(history)["close"]).rename("asset")
            b = pct_returns(normalize_history(benchmark)["close"]).rename("bench")
        except ValueError as exc:
            return err(f"rolling_beta: {exc}")
        joined = pd.concat([a, b], axis=1, join="inner").dropna()
        if len(joined) < window + 5:
            return err(f"need at least {window + 5} overlapping observations",
                       overlapping=len(joined))
        cov = joined["asset"].rolling(window).cov(joined["bench"])
        var = joined["bench"].rolling(window).var()
        beta = (cov / var.replace(0, np.nan)).dropna()
        return ok({
            "window": window,
            "current_beta": float(beta.iloc[-1]),
            "beta_avg": float(beta.mean()),
            "beta_series": series_tail(beta, 260),
        })

    # ---------------- Internals ----------------

    def _ewma_variance(self, returns: np.ndarray, lam: float) -> float:
        var = float(np.var(returns[:20], ddof=1)) if len(returns) >= 20 else float(np.var(returns))
        for r in returns[20:]:
            var = lam * var + (1 - lam) * r * r
        return var

    def _beta(self, asset_rets: pd.Series, benchmark: Any) -> Dict[str, Any]:
        try:
            bench_rets = pct_returns(normalize_history(benchmark)["close"]).rename("bench")
        except ValueError as exc:
            return {"error": f"benchmark: {exc}"}
        joined = pd.concat([asset_rets.rename("asset"), bench_rets], axis=1, join="inner").dropna()
        if len(joined) < 20:
            return {"error": "fewer than 20 overlapping observations with benchmark"}
        bvar = float(joined["bench"].var(ddof=1))
        beta = float(joined["asset"].cov(joined["bench"]) / bvar) if bvar > 0 else None
        corr = float(joined["asset"].corr(joined["bench"]))
        return {"beta": beta, "correlation": corr, "observations": int(len(joined))}
