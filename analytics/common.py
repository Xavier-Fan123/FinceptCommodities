"""Shared helpers for the commodities analytics module.

JSON-safety conversion, response envelopes, and normalization of the
heterogeneous price-history shapes produced by the data scripts
(yfinance_data.py records, World Bank records, generic date/value pairs).
"""

import math
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def json_safe(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars to native types; NaN/inf -> None."""
    if obj is None:
        return None
    if isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        val = float(obj)
        return val if math.isfinite(val) else None
    if isinstance(obj, (datetime, date, pd.Timestamp)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, np.ndarray, pd.Series, pd.Index)):
        return [json_safe(v) for v in list(obj)]
    return str(obj)


def ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"success": True}
    out.update(payload)
    out["timestamp"] = int(datetime.now().timestamp())
    return json_safe(out)


def err(message: str, **extra: Any) -> Dict[str, Any]:
    out = {"success": False, "error": str(message)}
    out.update(extra)
    out["timestamp"] = int(datetime.now().timestamp())
    return json_safe(out)


_DATE_KEYS = ("date", "Date", "report_date", "report_date_as_yyyy_mm_dd", "period")
_VALUE_KEYS = ("close", "Close", "price", "value", "settle", "settlement", "last")


def normalize_history(data: Any, value_key: Optional[str] = None,
                      date_key: Optional[str] = None) -> pd.DataFrame:
    """Normalize a price/level history into a DataFrame indexed by datetime.

    Accepts a list of record dicts carrying either a unix `timestamp` or a
    parseable date field, and a numeric value field (close/price/value/...).
    Returns a DataFrame with at least a `close` column, sorted ascending.
    Raises ValueError when the input cannot be interpreted.
    """
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list) or not data:
        raise ValueError("history must be a non-empty list of records")
    if not isinstance(data[0], dict):
        raise ValueError("history records must be objects")

    sample = data[0]
    dk = date_key if date_key in sample else None
    if dk is None:
        if "timestamp" in sample:
            dk = "timestamp"
        else:
            dk = next((k for k in _DATE_KEYS if k in sample), None)
    if dk is None:
        raise ValueError(f"no date/timestamp field found in record keys {list(sample.keys())}")

    vk = value_key if value_key in sample else None
    if vk is None:
        vk = next((k for k in _VALUE_KEYS if k in sample), None)
    if vk is None:
        numeric = [k for k, v in sample.items()
                   if k != dk and isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(numeric) == 1:
            vk = numeric[0]
    if vk is None:
        raise ValueError(f"no price/value field found in record keys {list(sample.keys())}")

    df = pd.DataFrame(data)
    if dk == "timestamp":
        idx = pd.to_datetime(pd.to_numeric(df[dk], errors="coerce"), unit="s")
    else:
        idx = pd.to_datetime(df[dk].astype(str), errors="coerce", format="mixed")
    df = df.assign(_dt=idx).dropna(subset=["_dt"]).set_index("_dt").sort_index()

    df["close"] = pd.to_numeric(df[vk], errors="coerce") if vk != "close" else pd.to_numeric(df["close"], errors="coerce")
    for col in ("open", "high", "low", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    if df.empty:
        raise ValueError("history contained no usable numeric values")
    return df


def pct_returns(close: pd.Series) -> pd.Series:
    """Simple period returns, NaN rows dropped."""
    return close.pct_change().dropna()


def align_closes(a: Any, b: Any, value_key: Optional[str] = None) -> pd.DataFrame:
    """Normalize two histories and inner-join their closes on date."""
    da = normalize_history(a, value_key=value_key)
    db = normalize_history(b, value_key=value_key)
    joined = pd.concat(
        [da["close"].rename("a"), db["close"].rename("b")], axis=1, join="inner"
    ).dropna()
    if joined.empty:
        raise ValueError("series have no overlapping dates")
    return joined


def series_tail(s: pd.Series, n: int = 260) -> List[Dict[str, Any]]:
    """Chart-ready [{date, value}] for the last n points."""
    tail = s.dropna().tail(n)
    return [{"date": idx.strftime("%Y-%m-%d"), "value": json_safe(val)}
            for idx, val in tail.items()]
