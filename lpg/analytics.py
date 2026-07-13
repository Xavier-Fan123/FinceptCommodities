"""Pure-stdlib LPG spread, distribution, and seasonality analytics."""

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import json_safe


MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def business_days_between(earlier: Any, later: Any) -> int:
    """Count weekdays after ``earlier`` through ``later`` (weekends ignored)."""
    start, end = _date(earlier), _date(later)
    if end < start:
        start, end = end, start
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def numeric_statistics(records: Iterable[Mapping[str, Any]], window: int = 252,
                       value_key: str = "value") -> Dict[str, Any]:
    usable: List[Tuple[str, float]] = []
    for row in records:
        try:
            value = float(row.get(value_key))
            if math.isfinite(value):
                usable.append((str(row.get("date") or row.get("observation_date") or ""), value))
        except (TypeError, ValueError):
            continue
    usable.sort(key=lambda item: item[0])
    if window > 0:
        usable = usable[-window:]
    if not usable:
        return {
            "count": 0, "first_date": None, "last_date": None,
            "current": None, "mean": None, "median": None,
            "minimum": None, "maximum": None, "stddev": None,
            "zscore": None, "percentile": None,
        }
    values = [item[1] for item in usable]
    current = values[-1]
    mean = statistics.fmean(values)
    stddev = statistics.pstdev(values) if len(values) > 1 else 0.0
    less = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    percentile = 100.0 * (less + 0.5 * equal) / len(values)
    return json_safe({
        "count": len(values),
        "first_date": usable[0][0],
        "last_date": usable[-1][0],
        "current": current,
        "mean": mean,
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
        "stddev": stddev,
        "zscore": (current - mean) / stddev if stddev else None,
        "percentile": percentile,
    })


def seasonality(records: Iterable[Mapping[str, Any]],
                value_key: str = "value") -> Dict[str, Any]:
    """Calendar-month return statistics and monthly price bands."""
    values: Dict[date, float] = {}
    for row in records:
        try:
            observed = _date(row.get("date") or row.get("observation_date"))
            value = float(row.get(value_key))
            if math.isfinite(value):
                values[observed] = value
        except (TypeError, ValueError):
            continue
    if not values:
        return {"monthly_stats": [], "bands": [], "current_year": None, "years": 0}

    monthly_values: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    for observed, value in values.items():
        monthly_values[(observed.year, observed.month)].append(value)

    monthly_end: List[Tuple[Tuple[int, int], float]] = []
    for year_month in sorted(monthly_values):
        year, month = year_month
        candidates = [(day, value) for day, value in values.items()
                      if day.year == year and day.month == month]
        monthly_end.append((year_month, max(candidates, key=lambda item: item[0])[1]))

    returns: Dict[int, List[float]] = defaultdict(list)
    for index in range(1, len(monthly_end)):
        (_, previous), ((_, month), current) = monthly_end[index - 1], monthly_end[index]
        if previous:
            returns[month].append((current / previous - 1.0) * 100.0)

    monthly_stats = []
    for month in range(1, 13):
        group = returns.get(month, [])
        monthly_stats.append({
            "month": MONTH_NAMES[month - 1],
            "month_number": month,
            "avg_return_pct": statistics.fmean(group) if group else None,
            "median_return_pct": statistics.median(group) if group else None,
            "win_rate_pct": 100.0 * sum(value > 0 for value in group) / len(group) if group else None,
            "years_observed": len(group),
        })

    current_year = max(day.year for day in values)
    years = sorted({day.year for day in values})
    bands = []
    for month in range(1, 13):
        by_year = {
            year: statistics.fmean(monthly_values[(year, month)])
            for year in years if (year, month) in monthly_values
        }
        historical = [value for year, value in by_year.items() if year < current_year]
        bands.append({
            "month": MONTH_NAMES[month - 1],
            "month_number": month,
            "hist_min": min(historical) if historical else None,
            "hist_max": max(historical) if historical else None,
            "hist_avg": statistics.fmean(historical) if historical else None,
            "current_year": by_year.get(current_year),
            "years_in_band": len(historical),
        })
    return json_safe({
        "monthly_stats": monthly_stats,
        "bands": bands,
        "current_year": current_year,
        "years": len(years),
    })


def freshness(observation_date: Optional[str], now: Optional[date] = None) -> Dict[str, Any]:
    if not observation_date:
        return {"status": "missing", "business_days": None}
    today = now or datetime.now(timezone.utc).date()
    lag = business_days_between(observation_date, today)
    if lag <= 1:
        status = "fresh"
    elif lag <= 3:
        status = "delayed"
    else:
        status = "stale"
    return {"status": status, "business_days": lag}


def _effective_record(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    raw_value = row.get("value")
    if raw_value is None:
        raw_value = row.get("value_normalized")
    if raw_value is None:
        raw_value = row.get("value_native")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    observed = row.get("date") or row.get("observation_date")
    if not observed:
        return None
    return {
        "date": _date(observed).isoformat(),
        "value": value,
        "currency": row.get("currency") or row.get("currency_normalized") or row.get("currency_native"),
        "unit": row.get("unit") or row.get("unit_normalized") or row.get("unit_native"),
    }


def calculate_spread(
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    definition: Mapping[str, Any],
    as_of: Optional[str] = None,
    window: int = 252,
    max_stale_business_days: int = 1,
) -> Dict[str, Any]:
    """Calculate an exact-date spread and block stale or incompatible legs."""
    result: Dict[str, Any] = {
        "id": definition["id"],
        "name": definition["name"],
        "success": False,
        "value": None,
        "currency": None,
        "unit": None,
        "observation_date": None,
        "legs": [],
        "history": [],
        "statistics": numeric_statistics([]),
        "seasonality": seasonality([]),
        "blocked_reason": None,
        "alignment": str(definition.get("alignment") or "exact_date"),
    }
    alignment = result["alignment"]
    series_maps: Dict[str, Dict[str, Dict[str, Any]]] = {}
    cutoff = _date(as_of) if as_of else None
    missing = []
    for key, coefficient in definition["legs"]:
        dated: Dict[str, Dict[str, Any]] = {}
        for raw in histories.get(key, []):
            record = _effective_record(raw)
            if record and (cutoff is None or _date(record["date"]) <= cutoff):
                bucket = record["date"]
                if alignment == "contract_month":
                    bucket = str(raw.get("contract_month") or record["date"][:7])[:7]
                current = dated.get(bucket)
                if current is None or record["date"] >= current["date"]:
                    dated[bucket] = record
        if not dated:
            missing.append(key)
        series_maps[key] = dated
    if missing:
        result["blocked_reason"] = "missing_legs"
        result["missing_legs"] = missing
        return result

    latest_by_leg = {key: max(rows) for key, rows in series_maps.items()}
    newest = max(series_maps[key][bucket]["date"] for key, bucket in latest_by_leg.items())
    oldest = min(series_maps[key][bucket]["date"] for key, bucket in latest_by_leg.items())
    if alignment == "exact_date" and business_days_between(oldest, newest) > max_stale_business_days:
        result["blocked_reason"] = "stale_leg"
        result["legs"] = [
            {"canonical_key": key, "coefficient": coefficient,
             "latest_date": series_maps[key][latest_by_leg[key]]["date"]}
            for key, coefficient in definition["legs"]
        ]
        return result

    common_dates = set.intersection(*(set(rows) for rows in series_maps.values()))
    if not common_dates:
        result["blocked_reason"] = "no_common_date"
        return result
    latest_common = max(common_dates)
    common_date = max(series_maps[key][latest_common]["date"] for key, _ in definition["legs"])
    if alignment == "exact_date" and business_days_between(latest_common, newest) > max_stale_business_days:
        result["blocked_reason"] = "stale_common_date"
        return result

    currencies = {series_maps[key][latest_common].get("currency")
                  for key, _ in definition["legs"]}
    units = {series_maps[key][latest_common].get("unit")
             for key, _ in definition["legs"]}
    if len(currencies) != 1 or len(units) != 1 or None in currencies or None in units:
        result["blocked_reason"] = "incompatible_units"
        result["leg_units"] = [
            {"canonical_key": key,
             "currency": series_maps[key][latest_common].get("currency"),
             "unit": series_maps[key][latest_common].get("unit")}
            for key, _ in definition["legs"]
        ]
        return result

    spread_history = []
    for observed in sorted(common_dates):
        value = sum(coefficient * series_maps[key][observed]["value"]
                    for key, coefficient in definition["legs"])
        effective_date = observed if alignment == "exact_date" else max(
            series_maps[key][observed]["date"] for key, _ in definition["legs"]
        )
        item = {"date": effective_date, "value": value}
        if alignment == "contract_month":
            item["contract_month"] = observed
        spread_history.append(item)
    if window > 0:
        chart_history = spread_history[-window:]
    else:
        chart_history = spread_history
    latest_value = spread_history[-1]["value"]
    result.update({
        "success": True,
        "value": latest_value,
        "currency": next(iter(currencies)),
        "unit": next(iter(units)),
        "observation_date": common_date,
        "contract_month": latest_common if alignment == "contract_month" else None,
        "legs": [
            {
                "canonical_key": key,
                "coefficient": coefficient,
                "value": series_maps[key][latest_common]["value"],
                "observation_date": series_maps[key][latest_common]["date"],
                "contract_month": latest_common if alignment == "contract_month" else None,
            }
            for key, coefficient in definition["legs"]
        ],
        "history": chart_history,
        "statistics": numeric_statistics(spread_history, window=window),
        "seasonality": seasonality(spread_history),
        "blocked_reason": None,
    })
    result["zscore"] = result["statistics"]["zscore"]
    result["percentile"] = result["statistics"]["percentile"]
    return json_safe(result)
