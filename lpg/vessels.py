"""Vessel-intelligence normalization with explicit evidence boundaries.

Historical port calls, continuous AIS track points, and current positions are
different capabilities.  This module imports only what a source actually
contains and never promotes a stopped/port-call coordinate to a live position.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .models import json_safe


VESSEL_INTELLIGENCE_VERSION = "lpg-vessel-intelligence-v1"
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_ROWS = 100_000

REQUIRED_PORT_CALL_COLUMNS = frozenset({
    "vessel_name", "imo", "mmsi", "port", "arrival_utc", "departure_utc",
    "latitude", "longitude", "source",
})


def _text(value: Any, maximum: int = 1000) -> str:
    return str(value or "").strip()[:maximum]


def _number(value: Any, *, minimum: Optional[float] = None,
            maximum: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric vessel value: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError("vessel numeric values must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"vessel numeric value must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"vessel numeric value must be at most {maximum}")
    return number


def _source_timestamp(value: Any) -> Tuple[Optional[str], str]:
    """Preserve naive source timestamps without silently labelling them UTC."""
    text = _text(value, 64)
    if not text:
        return None, "unavailable"
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return text, "source_format_unverified"
    if len(text) <= 10:
        return parsed.date().isoformat(), "date_only"
    if parsed.tzinfo is None:
        return parsed.replace(microsecond=0).isoformat(), "source_timezone_unverified"
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat(), "normalized_utc"


def _timestamp_state(*states: str) -> str:
    priority = {
        "source_format_unverified": 4,
        "source_timezone_unverified": 3,
        "date_only": 2,
        "normalized_utc": 1,
        "unavailable": 0,
    }
    available = [state for state in states if state != "unavailable"]
    return max(available, key=lambda item: priority.get(item, 5)) if available else "unavailable"


def _vessel_id(imo: str, mmsi: str) -> str:
    if imo:
        return f"imo-{re.sub(r'[^0-9]', '', imo)}"
    if mmsi:
        return f"mmsi-{re.sub(r'[^0-9]', '', mmsi)}"
    raise ValueError("each vessel snapshot row requires IMO or MMSI")


def _json_value(value: str) -> Any:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"unparsed": value[:10_000]}
    return parsed if isinstance(parsed, (dict, list)) else {"value": parsed}


def _operation_signal(arrival: Optional[float], departure: Optional[float]) -> Tuple[str, str, Optional[float]]:
    if arrival is None or departure is None:
        return "unknown", "unavailable", None
    change = round(departure - arrival, 3)
    if change >= 0.3:
        return "loaded", "draught_change_inference", change
    if change <= -0.3:
        return "discharged", "draught_change_inference", change
    return "no_major_change", "draught_change_inference", change


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_port_call_snapshot(path: Any, *, fleet_group: str = "reference_fleet") -> Dict[str, Any]:
    """Load a normalized, auditable port-call snapshot from a CSV export."""
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"vessel port-call snapshot not found: {source_path}")
    if source_path.suffix.lower() != ".csv":
        raise ValueError("vessel port-call snapshot must be a CSV file")
    size = source_path.stat().st_size
    if size > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"vessel port-call snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
    snapshot_at = datetime.fromtimestamp(
        source_path.stat().st_mtime, tz=timezone.utc,
    ).replace(microsecond=0).isoformat()
    checksum = _file_sha256(source_path)
    vessels: Dict[str, Dict[str, Any]] = {}
    calls: Dict[str, Dict[str, Any]] = {}
    health: Dict[str, Dict[str, Any]] = {}

    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_PORT_CALL_COLUMNS - columns)
        if missing:
            raise ValueError(f"vessel snapshot missing required column(s): {', '.join(missing)}")
        for index, row in enumerate(reader, start=2):
            if index > MAX_SNAPSHOT_ROWS + 1:
                raise ValueError(f"vessel snapshot exceeds {MAX_SNAPSHOT_ROWS} rows")
            name = _text(row.get("vessel_name"), 256)
            imo = re.sub(r"[^0-9]", "", _text(row.get("imo"), 32))
            mmsi = re.sub(r"[^0-9]", "", _text(row.get("mmsi"), 32))
            if not name:
                raise ValueError(f"vessel snapshot row {index} has no vessel_name")
            vessel_id = _vessel_id(imo, mmsi)
            vessels[vessel_id] = {
                "vessel_id": vessel_id,
                "name": name,
                "imo": imo or None,
                "mmsi": mmsi or None,
                "vessel_type": "LPG tanker",
                "fleet_group": _text(fleet_group, 128) or "reference_fleet",
                "active": True,
                "metadata": {
                    "identity_state": "source_reported",
                    "snapshot_file": source_path.name,
                    "snapshot_sha256": checksum,
                },
            }

            arrived_at, arrived_state = _source_timestamp(row.get("arrival_utc"))
            berthed_at, berthed_state = _source_timestamp(row.get("berth_utc"))
            departed_at, departed_state = _source_timestamp(row.get("departure_utc"))
            timestamp_state = _timestamp_state(arrived_state, berthed_state, departed_state)
            latitude = _number(row.get("latitude"), minimum=-90, maximum=90)
            longitude = _number(row.get("longitude"), minimum=-180, maximum=180)
            draught_arrival = _number(row.get("draught_arrival"), minimum=0, maximum=40)
            draught_departure = _number(row.get("draught_departure"), minimum=0, maximum=40)
            signal, signal_state, draught_change = _operation_signal(
                draught_arrival, draught_departure,
            )
            source = _text(row.get("source"), 128) or "unknown"
            source_key = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_") or "unknown"
            raw_payload = _json_value(_text(row.get("raw"), 100_000))
            identity = "|".join((
                source, mmsi, imo, _text(row.get("locode"), 32),
                _text(row.get("port"), 256).lower(), arrived_at or "", departed_at or "",
                str(latitude if latitude is not None else ""),
                str(longitude if longitude is not None else ""),
            ))
            call_key = "vpc-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            evidence_hash = hashlib.sha256(json.dumps(
                json_safe({"row": row, "normalized_identity": identity}),
                ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            calls[call_key] = {
                "call_key": call_key,
                "vessel_id": vessel_id,
                "port_name": _text(row.get("port"), 256),
                "port_name_local": _text(row.get("port_cn"), 256) or None,
                "locode": _text(row.get("locode"), 32) or None,
                "country": _text(row.get("country"), 128) or None,
                "country_code": _text(row.get("country_code"), 16) or None,
                "arrived_at": arrived_at,
                "berthed_at": berthed_at,
                "departed_at": departed_at,
                "timestamp_state": timestamp_state,
                "source_timezone": _text(row.get("timezone"), 64) or None,
                "stay_hours": _number(row.get("stay_hours"), minimum=0, maximum=24 * 365),
                "latitude": latitude,
                "longitude": longitude,
                "geo_precision": "port_call_stop_point" if latitude is not None else "unavailable",
                "draught_arrival": draught_arrival,
                "draught_departure": draught_departure,
                "draught_change": draught_change,
                "operation_signal": signal,
                "operation_signal_state": signal_state,
                "next_destination": _text(row.get("next_destination"), 256) or None,
                "source": source,
                "source_detail": _text(row.get("source_detail"), 1000) or None,
                "evidence_state": "source_reported",
                "source_snapshot_at": snapshot_at,
                "evidence_hash": evidence_hash,
                "raw": raw_payload,
                "metadata": {
                    "legacy_confidence_label": _text(row.get("confidence"), 32) or None,
                    "legacy_cargo_op_label": _text(row.get("cargo_op"), 128) or None,
                    "legacy_sequence": _text(row.get("seq"), 32) or None,
                    "leg_distance_nm": _number(row.get("leg_distance_nm"), minimum=0),
                    "leg_navigation_hours": _number(row.get("leg_nav_hours"), minimum=0),
                    "leg_average_speed_kn": _number(row.get("leg_avg_speed_kn"), minimum=0),
                },
            }
            item = health.setdefault(source_key, {
                "source_id": source_key,
                "source_name": source,
                "capabilities": ["historical_port_calls"],
                "access_state": "snapshot_only",
                "status": "snapshot_loaded",
                "entitlement_state": "unverified",
                "last_attempt_at": snapshot_at,
                "last_success_at": snapshot_at,
                "latest_observation_at": None,
                "vessel_ids": set(),
                "row_count": 0,
                "metadata": {
                    "snapshot_file": source_path.name,
                    "snapshot_sha256": checksum,
                    "live_position_capability": False,
                },
            })
            item["vessel_ids"].add(vessel_id)
            item["row_count"] += 1
            observed = arrived_at or departed_at
            if observed and observed > str(item.get("latest_observation_at") or ""):
                item["latest_observation_at"] = observed

    health_rows = []
    for item in health.values():
        item["vessel_count"] = len(item.pop("vessel_ids"))
        health_rows.append(item)
    return json_safe({
        "version": VESSEL_INTELLIGENCE_VERSION,
        "source_file": str(source_path),
        "source_file_name": source_path.name,
        "source_file_size": size,
        "source_file_sha256": checksum,
        "source_snapshot_at": snapshot_at,
        "fleet_group": fleet_group,
        "vessels": list(vessels.values()),
        "port_calls": list(calls.values()),
        "source_health": health_rows,
        "coverage": {
            "vessels": len(vessels),
            "historical_port_calls": len(calls),
            "live_positions": 0,
            "continuous_track_points": 0,
        },
        "boundary": "port-call coordinates remain historical observations; no live position is created",
    })
