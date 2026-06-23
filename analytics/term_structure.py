"""Futures term-structure analytics.

Curve construction from contract-month settlements, contango/backwardation
classification, slope metrics, implied convenience yield, and roll yield.
"""

import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from .common import ok, err
    from .config import Constants, MONTH_CODES
except ImportError:
    from common import ok, err
    from config import Constants, MONTH_CODES

_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_expiry(value: Any, reference: Optional[datetime] = None) -> Optional[datetime]:
    """Parse contract expiry labels into a datetime (mid-month convention).

    Accepts: "2026-07", "2026-07-15", "JUL 26" / "JUL26" (CME settlement labels),
    "N26" (month-code + 2-digit year), or unix timestamps.
    """
    ref = reference or datetime.now()
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().upper()

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 15)
        except ValueError:
            return None
    m = re.fullmatch(r"([A-Z]{3})\s*(\d{2,4})", text)
    if m and m.group(1) in _MONTH_ABBR:
        year = int(m.group(2))
        if year < 100:
            year += 2000
        return datetime(year, _MONTH_ABBR[m.group(1)], 15)
    m = re.fullmatch(r"([FGHJKMNQUVXZ])(\d{1,4})", text)
    if m:
        year = int(m.group(2))
        if year < 10:
            year += (ref.year // 10) * 10
            if year < ref.year - 1:
                year += 10
        elif year < 100:
            year += 2000
        return datetime(year, MONTH_CODES[m.group(1)], 15)
    return None


class TermStructureAnalyzer:
    """Analyze a futures curve given contract expiries and prices."""

    def analyze(self, contracts: List[Dict[str, Any]],
                spot: Optional[float] = None,
                risk_free_rate: float = Constants.DEFAULT_RISK_FREE_RATE,
                storage_cost: float = Constants.DEFAULT_STORAGE_COST,
                as_of: Optional[str] = None) -> Dict[str, Any]:
        """Full term-structure report.

        contracts: [{"expiry": <label>, "price": <float>}, ...]
        spot: spot price; defaults to front contract (flagged in output).
        """
        if not contracts or not isinstance(contracts, list):
            return err("contracts must be a non-empty list of {expiry, price}")

        ref = None
        if as_of:
            ref = parse_expiry(as_of)
        ref = ref or datetime.now()

        points = []
        for c in contracts:
            if not isinstance(c, dict):
                continue
            expiry = parse_expiry(c.get("expiry") or c.get("month") or c.get("label"), ref)
            try:
                price = float(c.get("price") if c.get("price") is not None else c.get("settle"))
            except (TypeError, ValueError):
                continue
            if expiry is None or not math.isfinite(price) or price <= 0:
                continue
            t_years = max((expiry - ref).days, 1) / Constants.DAYS_IN_YEAR
            points.append({"expiry": expiry, "t_years": t_years, "price": price})

        points.sort(key=lambda p: p["expiry"])
        if len(points) < 2:
            return err("need at least 2 parseable contracts to build a curve",
                       parsed_count=len(points))

        front = points[0]
        spot_used = float(spot) if spot else front["price"]
        spot_is_proxy = spot is None

        # Adjacent-segment slopes and roll yields
        segments = []
        up_segments = 0
        for near, far in zip(points, points[1:]):
            dt_years = max(far["t_years"] - near["t_years"], 1 / Constants.DAYS_IN_YEAR)
            slope_pct = (far["price"] - near["price"]) / near["price"]
            if slope_pct > 0:
                up_segments += 1
            # Positive roll yield in backwardation (long position rolls down the curve)
            roll_yield_ann = math.log(near["price"] / far["price"]) / dt_years
            segments.append({
                "from": near["expiry"].strftime("%Y-%m"),
                "to": far["expiry"].strftime("%Y-%m"),
                "price_change_pct": slope_pct * 100,
                "annualized_slope_pct": slope_pct / dt_years * 100,
                "annualized_roll_yield_pct": roll_yield_ann * 100,
            })

        # Structure classification
        back = points[-1]
        overall = (back["price"] - front["price"]) / front["price"]
        up_ratio = up_segments / len(segments)
        if up_ratio >= 0.75 and overall > 0:
            structure = "contango"
        elif up_ratio <= 0.25 and overall < 0:
            structure = "backwardation"
        else:
            structure = "mixed"

        # Slope metrics
        second = points[1]
        dt12 = second["t_years"] - front["t_years"]
        front_to_second_ann = ((second["price"] / front["price"]) - 1) / max(dt12, 1e-6) * 100
        target_12m = front["t_years"] + 1.0
        twelve = min(points[1:], key=lambda p: abs(p["t_years"] - target_12m))
        dt_f12 = max(twelve["t_years"] - front["t_years"], 1e-6)
        front_to_12m_ann = ((twelve["price"] / front["price"]) - 1) / dt_f12 * 100

        # Implied convenience yield per contract: c = r + u - ln(F/S)/T
        convenience = []
        for p in points:
            cy = risk_free_rate + storage_cost - math.log(p["price"] / spot_used) / p["t_years"]
            convenience.append({
                "expiry": p["expiry"].strftime("%Y-%m"),
                "implied_convenience_yield_pct": cy * 100,
            })

        # Front-roll carry: annualized return from rolling front into second
        front_roll_yield_ann = math.log(front["price"] / second["price"]) / max(dt12, 1e-6)

        return ok({
            "as_of": ref.strftime("%Y-%m-%d"),
            "market_structure": structure,
            "contracts_used": len(points),
            "spot_price": spot_used,
            "spot_is_front_proxy": spot_is_proxy,
            "front_contract": {"expiry": front["expiry"].strftime("%Y-%m"), "price": front["price"]},
            "back_contract": {"expiry": back["expiry"].strftime("%Y-%m"), "price": back["price"]},
            "front_to_back_pct": overall * 100,
            "front_to_second_annualized_pct": front_to_second_ann,
            "front_to_12m_annualized_pct": front_to_12m_ann,
            "front_roll_yield_annualized_pct": front_roll_yield_ann * 100,
            "implied_convenience_yield": convenience,
            "segments": segments,
            "curve": [{"expiry": p["expiry"].strftime("%Y-%m"),
                       "t_years": round(p["t_years"], 4),
                       "price": p["price"]} for p in points],
            "assumptions": {
                "risk_free_rate": risk_free_rate,
                "storage_cost": storage_cost,
                "convenience_yield_formula": "c = r + u - ln(F/S)/T",
                "roll_yield_formula": "ln(F_near/F_far) / dt (positive in backwardation)",
            },
        })


def _curve_points(contracts: List[Dict[str, Any]],
                  ref: datetime) -> List[Dict[str, Any]]:
    """Parse [{expiry, price}] into sorted {expiry, t_years, price} points."""
    points = []
    for c in contracts or []:
        if not isinstance(c, dict):
            continue
        expiry = parse_expiry(c.get("expiry") or c.get("month") or c.get("label"), ref)
        try:
            price = float(c.get("price") if c.get("price") is not None else c.get("settle"))
        except (TypeError, ValueError):
            continue
        if expiry is None or not math.isfinite(price) or price <= 0:
            continue
        t_years = max((expiry - ref).days, 1) / Constants.DAYS_IN_YEAR
        points.append({"expiry": expiry, "t_years": t_years, "price": price})
    points.sort(key=lambda p: p["expiry"])
    return points


def calendar_spreads(contracts: List[Dict[str, Any]],
                     as_of: Optional[str] = None) -> Dict[str, Any]:
    """Calendar (time) spreads off a futures curve.

    The trader's view of the curve: the prompt M1-M2 spread, a time-distance
    ladder (front vs the contract nearest ~+1/2/3/6/12 months), and the
    adjacent-month "spread curve". Spreads are in the contract's own price
    units; a *positive* near-minus-far spread means the front is richer than
    the deferred contract — i.e. backwardation, the signature of a tight,
    inventory-drawing market.
    """
    if not contracts or not isinstance(contracts, list):
        return err("contracts must be a non-empty list of {expiry, price}")
    ref = (parse_expiry(as_of) if as_of else None) or datetime.now()
    points = _curve_points(contracts, ref)
    if len(points) < 2:
        return err("need at least 2 parseable contracts to build spreads",
                   parsed_count=len(points))

    def label(p: Dict[str, Any]) -> str:
        return p["expiry"].strftime("%Y-%m")

    def pair(near: Dict[str, Any], far: Dict[str, Any]) -> Dict[str, Any]:
        dt = max(far["t_years"] - near["t_years"], 1e-6)
        spread = near["price"] - far["price"]
        return {
            "near": label(near), "far": label(far),
            "near_price": near["price"], "far_price": far["price"],
            "months_apart": round((far["t_years"] - near["t_years"]) * 12, 1),
            "spread": spread,
            "spread_pct": spread / far["price"] * 100,
            "annualized_roll_yield_pct": math.log(near["price"] / far["price"]) / dt * 100,
            "structure": ("backwardation" if spread > 0
                          else "contango" if spread < 0 else "flat"),
        }

    front = points[0]
    prompt = pair(points[0], points[1])

    # Ladder: front vs the contract nearest to +1/2/3/6/12 months out.
    ladder, seen = [], {0}
    for months in (1, 2, 3, 6, 12):
        target = front["t_years"] + months / 12.0
        cand = min(range(1, len(points)),
                   key=lambda i: abs(points[i]["t_years"] - target))
        if cand in seen:
            continue
        seen.add(cand)
        entry = pair(front, points[cand])
        entry["bucket_months"] = months
        ladder.append(entry)

    # Adjacent-month spread curve (the shape the front spread sits on).
    segments = [{"near": label(n), "far": label(f), "spread": n["price"] - f["price"]}
                for n, f in zip(points, points[1:])]

    return ok({
        "as_of": ref.strftime("%Y-%m-%d"),
        "contracts_used": len(points),
        "front_contract": {"expiry": label(front), "price": front["price"]},
        "structure": prompt["structure"],
        "prompt_spread": prompt,
        "front_to_back": pair(points[0], points[-1]),
        "ladder": ladder,
        "segments": segments,
    })
