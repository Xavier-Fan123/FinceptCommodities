"""Application facade for the LPG HTTP routes and import workflows."""

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .analytics import calculate_spread, freshness, numeric_statistics, seasonality
from .catalog import DEFAULT_SPREADS
from .intelligence import (
    EVENT_LABELS,
    INTELLIGENCE_VERSION,
    asset_catalog,
    baseline_alerts,
    build_events,
    run_scenario as calculate_scenario,
    route_catalog,
    scenario_catalog as intelligence_scenario_catalog,
)
from .models import json_safe, normalize_date, utc_now
from .store import LpgStore
from .vessels import VESSEL_INTELLIGENCE_VERSION, load_port_call_snapshot


DERIVED_PROMPT_CURVES = (
    {
        "canonical_key": "FEI_PROPANE_PROMPT_STRUCTURE",
        "name": "CFR North Asia Propane Prompt Half-Month Structure",
        "components": (
            ("HM1", "FEI_PROPANE_HM1"),
            ("HM2", "FEI_PROPANE_HM2"),
            ("HM3", "FEI_PROPANE_HM3"),
        ),
    },
    {
        "canonical_key": "CFR_NA_BUTANE_PROMPT_STRUCTURE",
        "name": "CFR North Asia Butane Prompt Half-Month Structure",
        "components": (
            ("HM1", "CFR_NA_BUTANE_HM1"),
            ("HM2", "CFR_NA_BUTANE_HM2"),
            ("HM3", "CFR_NA_BUTANE_HM3"),
        ),
    },
)
DERIVED_PROMPT_COMPONENT_KEYS = frozenset(
    canonical_key
    for spec in DERIVED_PROMPT_CURVES
    for _, canonical_key in spec["components"]
)

OFFICIAL_FC_CURVE_ACCESS = "not_entitled"

# Daily-derived curves are quality-controlled, but an anomaly never mutates or
# drops the licensed source observation.  These conservative thresholds are a
# review signal for USD/mt LPG assessments; the adaptive MAD threshold keeps a
# naturally volatile history from being flagged on every move.
CURVE_ANOMALY_ABS_USD_MT = 50.0
CURVE_ANOMALY_PCT = 0.08
CURVE_ANOMALY_MAD_MULTIPLIER = 6.0
CURVE_ANOMALY_WINDOW = 30


# Dataset coverage and refreshability are separate product concepts.  Keep the
# capability map close to the service response so every client sees the same
# entitlement boundary and knows which UI action, if any, is real.
DATASET_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "current": {
        "label": "Current prices", "category": "market_data", "view": "cockpit",
        "refresh_scope": "all", "refresh_supported": True,
        "access_mode": "licensed_excel",
    },
    "history": {
        "label": "History", "category": "market_data", "view": "history",
        "refresh_scope": "history", "refresh_supported": True,
        "access_mode": "licensed_excel",
    },
    "curves": {
        "label": "Daily-derived curves", "category": "market_data", "view": "curves",
        "refresh_scope": "all", "refresh_supported": True,
        "refresh_label": "Refresh daily prices",
        "access_mode": "derived_from_licensed_daily_assessments",
        "official_fc_status": OFFICIAL_FC_CURVE_ACCESS,
        "official_refresh_supported": False,
    },
    "moc": {
        "label": "MOC / eWindow", "category": "market_data", "view": "moc",
        "refresh_scope": "moc", "refresh_supported": True,
        "access_mode": "licensed_excel",
    },
    "fundamentals": {
        "label": "Fundamentals", "category": "market_data", "view": "moc",
        "refresh_scope": None, "refresh_supported": False,
        "access_mode": "not_configured",
    },
    "news": {
        "label": "LPG news", "category": "intelligence", "view": "news",
        "refresh_scope": "news", "refresh_supported": True,
        "access_mode": "public_discovery",
    },
    "situation": {
        "label": "Situation intelligence", "category": "intelligence",
        "view": "situation", "refresh_scope": "news", "refresh_supported": True,
        "access_mode": "derived_from_attributed_news",
    },
    "vessel_history": {
        "label": "Vessel history", "category": "shipping", "view": "situation",
        "refresh_scope": None, "refresh_supported": False,
        "access_mode": "local_snapshot",
    },
    "live_ais": {
        "label": "Live AIS positions", "category": "shipping", "view": "situation",
        "refresh_scope": None, "refresh_supported": False,
        "access_mode": "not_configured",
    },
}


class LpgService:
    def __init__(self, db_path: Optional[Any] = None, store: Optional[LpgStore] = None) -> None:
        self.store = store or LpgStore(db_path)

    # Write-side passthroughs keep importers independent from SQLite details.
    def upsert_series(self, value: Any) -> Dict[str, Any]:
        return self.store.upsert_series(value)

    def upsert_observation(self, value: Any) -> Dict[str, Any]:
        return self.store.upsert_observation(value)

    def upsert_curve_point(self, value: Any) -> Dict[str, Any]:
        return self.store.upsert_curve_point(value)

    def upsert_dataset_row(self, value: Any) -> Dict[str, Any]:
        return self.store.upsert_dataset_row(value)

    def upsert_news(self, value: Any) -> Dict[str, Any]:
        return self.store.upsert_news(value)

    def upsert_news_source_health(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        return self.store.upsert_news_source_health(value)

    def upsert_intelligence_event(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        return self.store.upsert_intelligence_event(value)

    def start_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.store.start_run(*args, **kwargs)

    def finish_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.store.finish_run(*args, **kwargs)

    def summary(self, as_of: Optional[str] = None) -> Dict[str, Any]:
        reference = date.fromisoformat(normalize_date(as_of)) if as_of else None
        prices = self.store.latest_observations(as_of=as_of, summary_only=True)
        for row in prices:
            row["freshness"] = freshness(row.get("observation_date"), reference)
        spreads = self.spreads(as_of=as_of)["items"]
        status = self.status()
        effective_as_of = normalize_date(as_of) if as_of else max(
            (row["observation_date"] for row in prices), default=None
        )
        return json_safe({
            "as_of": effective_as_of,
            "prices": prices,
            "spreads": spreads,
            "source_status": status["sources"],
            "updated_at": utc_now(),
        })

    def list_series(self, filters: Optional[Mapping[str, Any]] = None,
                    **kwargs: Any) -> Dict[str, Any]:
        merged = dict(filters or {})
        merged.update({key: value for key, value in kwargs.items() if value is not None})
        return self.store.list_series(merged)

    def series(self, filters: Optional[Mapping[str, Any]] = None,
               **kwargs: Any) -> Dict[str, Any]:
        return self.list_series(filters, **kwargs)

    def series_history(self, series_id: str, start: Optional[str] = None,
                       end: Optional[str] = None, limit: Optional[int] = None,
                       bate: Optional[str] = None) -> Dict[str, Any]:
        series = self.store.get_series(series_id)
        if series is None:
            raise KeyError(f"unknown LPG series: {series_id}")
        if series.get("entitlement_state") != "entitled" or not series.get("active"):
            raise PermissionError(f"LPG series is not entitled and active: {series_id}")
        records = self.store.history(series_id, start=start, end=end, limit=limit, bate=bate)
        if not records:
            availability = {
                "status": "empty", "reason": "no_history_observations_in_requested_range",
                "rows": 0, "dates": 0,
            }
        elif len({record["date"] for record in records}) < 2:
            availability = {
                "status": "limited", "reason": "single_date_only",
                "rows": len(records), "dates": 1,
            }
        else:
            availability = {
                "status": "ready", "reason": None, "rows": len(records),
                "dates": len({record["date"] for record in records}),
            }
        runtime = self._specialized_runtime_statuses().get("history")
        availability = self._with_runtime_status(availability, runtime)
        return json_safe({
            "series": series,
            "observations": records,
            "statistics": numeric_statistics(records),
            "seasonality": seasonality(records),
            "availability": availability,
            "refresh_state": runtime.get("state") if runtime else None,
            "refresh_reason": self._runtime_reason(runtime),
            "runtime_status": runtime,
        })

    def history(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.series_history(*args, **kwargs)

    def curves(self, as_of: Optional[str] = None,
               series_id: Optional[str] = None) -> Dict[str, Any]:
        rows = self.store.curves(as_of=as_of, series_id=series_id)
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            curve = grouped.setdefault(row["series_id"], {
                "series_id": row["series_id"],
                "canonical_key": row.get("canonical_key"),
                "name": row["name"],
                "source": row["source"],
                "entitlement_state": "entitled",
                "as_of_date": row["as_of_date"],
                "currency": row.get("currency_normalized") or row.get("currency_native"),
                "unit": row.get("unit_normalized") or row.get("unit_native"),
                "curve_kind": "official_fc_curve",
                "derived": False,
                "is_official": True,
                "component_series_ids": [row["series_id"]],
                "points": [],
            })
            curve["points"].append({
                "id": row["id"],
                "as_of_date": row["as_of_date"],
                "contract_month": row["contract_month"],
                "delivery_start": row.get("delivery_start"),
                "delivery_end": row.get("delivery_end"),
                "value": row.get("value_normalized")
                    if row.get("value_normalized") is not None else row["value_native"],
                "native_value": row["value_native"],
                "currency": row.get("currency_normalized") or row.get("currency_native"),
                "unit": row.get("unit_normalized") or row.get("unit_native"),
                "fetched_at": row["fetched_at"],
                "source_ref": row.get("source_ref"),
                "is_correction": row["is_correction"],
                "entitlement_state": "entitled",
                "freshness": freshness(row.get("as_of_date")),
            })
        derived_curves, derived_diagnostics = self._derived_prompt_curves(
            as_of=as_of, series_id=series_id,
        )
        quality = self._curve_quality_summary(derived_diagnostics, as_of=as_of)
        curves = [*grouped.values(), *derived_curves]
        effective_as_of = normalize_date(as_of) if as_of else max(
            (curve["as_of_date"] for curve in curves), default=None
        )
        official_count = len(grouped)
        derived_count = len(derived_curves)
        if official_count or derived_count:
            status, reason = "ready", None
        else:
            status = "empty"
            reason = "no_official_curve_points_or_complete_hm1_hm2_hm3_assessments"
        dataset_state, dataset_reason = status, reason
        if status == "ready" and quality.get("status") in {"warning", "incomplete"}:
            dataset_state = "available_with_warning"
            dataset_reason = next(iter(quality.get("reason_codes") or []), None)
        curve_dates = {
            str(curve_date)
            for curve in curves
            for curve_date in (
                curve.get("available_dates") or
                ([curve.get("as_of_date")] if curve.get("as_of_date") else [])
            )
        }
        dataset_status = {
            "status": dataset_state,
            "reason": dataset_reason,
            "rows": sum(len(curve["points"]) for curve in curves),
            "dates": len(curve_dates),
            "series": len(curves),
            "quality_status": quality.get("status"),
            "quality_reasons": quality.get("reason_codes") or [],
            "quality": quality,
        }
        runtime = self._daily_runtime_status()
        dataset_status = self._with_runtime_status(dataset_status, runtime)
        return json_safe({
            "as_of": effective_as_of,
            "curves": curves,
            "status": status,
            "reason": reason,
            "dataset_status": dataset_status,
            "refresh_state": runtime.get("state") if runtime else None,
            "refresh_reason": self._runtime_reason(runtime),
            "runtime_status": runtime,
            "official_curve_count": official_count,
            "official_point_count": sum(len(curve["points"]) for curve in grouped.values()),
            "derived_curve_count": derived_count,
            "derived_point_count": sum(len(curve["points"]) for curve in derived_curves),
            "derived_diagnostics": derived_diagnostics,
            "quality": quality,
            "curve_policy": "daily_derived_from_entitled_assessments",
            "official_fc_status": OFFICIAL_FC_CURVE_ACCESS,
            "official_refresh_supported": False,
        })

    def _derived_prompt_curves(self, as_of: Optional[str] = None,
                               series_id: Optional[str] = None) -> tuple[list[Dict[str, Any]],
                                                                           list[Dict[str, Any]]]:
        """Build daily curve snapshots from entitled HM1/HM2/HM3 assessments.

        Every snapshot is the intersection of three independently stored daily
        assessment series. Missing dates are never forward-filled and units are
        never inferred. The result is a local derived curve, not an official FC
        dataset, and remains traceable to each source observation.
        """
        curves, diagnostics = [], []
        quality_reference = date.fromisoformat(normalize_date(as_of)) if as_of else None
        for spec in DERIVED_PROMPT_CURVES:
            source_components, missing, component_health = [], [], []
            for tenor, canonical_key in spec["components"]:
                series = self.store.get_series_by_canonical_key(canonical_key)
                if (series is None or series.get("entitlement_state") != "entitled" or
                        not series.get("active")):
                    missing.append(canonical_key)
                    component_health.append({
                        "tenor": tenor,
                        "canonical_key": canonical_key,
                        "series_id": series.get("id") if series else None,
                        "status": "missing",
                        "reason": "missing_or_unentitled_component",
                        "observation_count": 0,
                        "first_date": None,
                        "last_date": None,
                        "freshness": {"status": "missing", "business_days": None},
                        "duplicate_record_count": 0,
                        "anomaly_count": 0,
                        "anomalies": [],
                    })
                    continue
                raw_records = self.store.history(series["id"], end=as_of)
                records, duplicates = self._curve_records_by_date(raw_records)
                if not records:
                    missing.append(canonical_key)
                    component_health.append({
                        "tenor": tenor,
                        "canonical_key": canonical_key,
                        "series_id": series["id"],
                        "status": "missing",
                        "reason": "no_daily_assessments",
                        "observation_count": 0,
                        "first_date": None,
                        "last_date": None,
                        "freshness": {"status": "missing", "business_days": None},
                        "duplicate_record_count": len(duplicates),
                        "anomaly_count": 0,
                        "anomalies": [],
                    })
                    continue
                record_dates = sorted(records)
                anomalies = self._curve_leg_anomalies(records)
                component_freshness = freshness(record_dates[-1], quality_reference)
                component_health.append({
                    "tenor": tenor,
                    "canonical_key": canonical_key,
                    "series_id": series["id"],
                    "status": (
                        "warning" if duplicates or anomalies or
                        component_freshness.get("status") in {"delayed", "stale"}
                        else "ready"
                    ),
                    "reason": (
                        "duplicate_curve_input_records" if duplicates
                        else "abnormal_daily_jump_detected" if anomalies
                        else f"curve_component_{component_freshness.get('status')}"
                        if component_freshness.get("status") in {"delayed", "stale"}
                        else None
                    ),
                    "observation_count": len(records),
                    "first_date": record_dates[0],
                    "last_date": record_dates[-1],
                    "latest_value": records[record_dates[-1]].get("value"),
                    "freshness": component_freshness,
                    "duplicate_record_count": len(duplicates),
                    "duplicate_records": duplicates[-10:],
                    "anomaly_count": len(anomalies),
                    "anomalies": anomalies[-10:],
                })
                source_components.append({
                    "tenor": tenor,
                    "series": series,
                    "records": records,
                })

            component_ids = [component["series"]["id"] for component in source_components]
            if series_id and series_id not in component_ids and series_id != spec["canonical_key"]:
                continue
            all_dates = sorted({
                curve_date
                for component in source_components
                for curve_date in component["records"]
            })
            latest_observed_date = all_dates[-1] if all_dates else None
            duplicate_count = sum(
                int(component.get("duplicate_record_count") or 0)
                for component in component_health
            )
            anomalies = [
                {"canonical_key": component["canonical_key"],
                 "tenor": component["tenor"], **anomaly}
                for component in component_health
                for anomaly in component.get("anomalies") or []
            ]
            diagnostic: Dict[str, Any] = {
                "canonical_key": spec["canonical_key"],
                "name": spec["name"],
                "expected_components": [key for _, key in spec["components"]],
                "component_series_ids": component_ids,
                "components": component_health,
                "expected_component_count": len(spec["components"]),
                "available_component_count": len(source_components),
                "missing_components": missing,
                "latest_observed_date": latest_observed_date,
                "duplicate_record_count": duplicate_count,
                "anomaly_count": len(anomalies),
                "anomalies": anomalies[-20:],
            }
            if missing or len(source_components) != len(spec["components"]):
                diagnostic.update({
                    "status": "incomplete",
                    "reason": "missing_or_unentitled_component",
                    "reason_codes": ["missing_or_unentitled_component"],
                    "available_dates": [],
                    "snapshot_count": 0,
                    "incomplete_date_count": len(all_dates),
                    "incomplete_dates": [
                        {
                            "date": curve_date,
                            "missing_components": [
                                key for _, key in spec["components"]
                                if key in missing or not any(
                                    component["series"].get("canonical_key") == key
                                    and curve_date in component["records"]
                                    for component in source_components
                                )
                            ],
                        }
                        for curve_date in all_dates[-30:]
                    ],
                })
                diagnostics.append(diagnostic)
                continue

            common_dates = set(source_components[0]["records"])
            for component in source_components[1:]:
                common_dates &= set(component["records"])
            incomplete_dates = []
            for curve_date in all_dates:
                absent = [
                    component["series"].get("canonical_key")
                    for component in source_components
                    if curve_date not in component["records"]
                ]
                if absent:
                    incomplete_dates.append({
                        "date": curve_date,
                        "missing_components": absent,
                    })

            snapshots = []
            rejected_dates: list[Dict[str, Any]] = []
            for curve_date in sorted(common_dates):
                records = [component["records"][curve_date] for component in source_components]
                currencies = {record.get("currency") for record in records}
                units = {record.get("unit") for record in records}
                if (len(currencies) != 1 or len(units) != 1 or
                        None in currencies or None in units):
                    rejected_dates.append({
                        "date": curve_date,
                        "reason": "currency_or_unit_mismatch",
                        "currencies": sorted(str(value) for value in currencies),
                        "units": sorted(str(value) for value in units),
                    })
                    continue
                values = {
                    component["tenor"]: record["value"]
                    for component, record in zip(source_components, records)
                }
                try:
                    numeric_values = [float(values[tenor]) for tenor, _ in spec["components"]]
                except (TypeError, ValueError):
                    rejected_dates.append({
                        "date": curve_date,
                        "reason": "non_numeric_curve_value",
                    })
                    continue
                if not all(math.isfinite(value) for value in numeric_values):
                    rejected_dates.append({
                        "date": curve_date,
                        "reason": "non_finite_curve_value",
                    })
                    continue
                snapshots.append({
                    "as_of_date": curve_date,
                    "values": values,
                    "currency": next(iter(currencies)),
                    "unit": next(iter(units)),
                    **self._daily_curve_shape(numeric_values),
                })

            if not snapshots:
                diagnostic.update({
                    "status": "incomplete",
                    "reason": "no_complete_same_date_unit_aligned_snapshot",
                    "reason_codes": ["no_complete_same_date_unit_aligned_snapshot"],
                    "rejected_dates": rejected_dates,
                    "available_dates": [],
                    "snapshot_count": 0,
                    "incomplete_date_count": len(incomplete_dates),
                    "incomplete_dates": incomplete_dates[-30:],
                })
                diagnostics.append(diagnostic)
                continue

            selected = snapshots[-1]
            previous = snapshots[-2] if len(snapshots) > 1 else None
            curve_date = selected["as_of_date"]
            primary_series = source_components[0]["series"]
            points = []
            front_value = float(selected["values"][source_components[0]["tenor"]])
            for component in source_components:
                tenor, source_series = component["tenor"], component["series"]
                record = component["records"][curve_date]
                value = float(record["value"])
                prior_value = (
                    float(previous["values"][tenor])
                    if previous and tenor in previous["values"] else None
                )
                points.append({
                    "id": f"derived:{record['id']}",
                    "as_of_date": curve_date,
                    "contract_month": tenor,
                    "tenor": tenor,
                    "delivery_start": None,
                    "delivery_end": None,
                    "value": record["value"],
                    "native_value": record["native_value"],
                    "currency": record.get("currency"),
                    "unit": record.get("unit"),
                    "fetched_at": record.get("fetched_at"),
                    "source_ref": record.get("source_ref"),
                    "is_correction": record.get("is_correction", False),
                    "entitlement_state": "entitled",
                    "freshness": freshness(curve_date),
                    "derived": True,
                    "curve_kind": "daily_derived_assessment_curve",
                    "source_series_id": source_series["id"],
                    "source_canonical_key": source_series.get("canonical_key"),
                    "day_change": round(value - prior_value, 6) if prior_value is not None else None,
                    "front_minus_tenor": round(front_value - value, 6),
                })
            latest_complete_date = selected["as_of_date"]
            latest_missing_components = [
                component["series"].get("canonical_key")
                for component in source_components
                if latest_observed_date and latest_observed_date not in component["records"]
            ]
            curve_freshness = freshness(latest_complete_date, quality_reference)
            reason_codes: list[str] = []
            if incomplete_dates:
                reason_codes.append("incomplete_curve_dates_detected")
            if latest_observed_date and latest_observed_date > latest_complete_date:
                reason_codes.append("latest_curve_date_incomplete")
            if duplicate_count:
                reason_codes.append("duplicate_curve_input_records")
            if rejected_dates:
                reason_codes.append("curve_unit_or_currency_mismatch")
            if anomalies:
                reason_codes.append("abnormal_daily_jump_detected")
            if curve_freshness.get("status") == "delayed":
                reason_codes.append("curve_pipeline_delayed")
            elif curve_freshness.get("status") == "stale":
                reason_codes.append("curve_pipeline_stale")
            quality_status = "warning" if reason_codes else "ready"
            curve_quality = {
                "status": quality_status,
                "reason_codes": reason_codes,
                "latest_observed_date": latest_observed_date,
                "latest_complete_date": latest_complete_date,
                "freshness": curve_freshness,
                "expected_components": len(spec["components"]),
                "available_components": len(source_components),
                "missing_latest_components": latest_missing_components,
                "incomplete_date_count": len(incomplete_dates),
                "duplicate_record_count": duplicate_count,
                "anomaly_count": len(anomalies),
                "rejected_date_count": len(rejected_dates),
            }
            curves.append({
                # Use an entitled component as the access-control anchor so
                # the existing UI catalog filter can validate and display it.
                "series_id": primary_series["id"],
                "canonical_key": spec["canonical_key"],
                "name": spec["name"],
                "source": "derived_from_current_assessments",
                "entitlement_state": "entitled",
                "as_of_date": curve_date,
                "currency": selected["currency"],
                "unit": selected["unit"],
                "curve_kind": "daily_derived_assessment_curve",
                "legacy_curve_kind": "derived_prompt_structure",
                "derived": True,
                "is_official": False,
                "component_series_ids": component_ids,
                "derivation_frequency": "daily",
                "methodology_version": "daily_hm_snapshot_v2_quality",
                "available_dates": [snapshot["as_of_date"] for snapshot in snapshots],
                "history_count": len(snapshots),
                "history": snapshots[-120:],
                "quality": curve_quality,
                "shape": {
                    key: selected[key] for key in (
                        "structure", "hm1_hm2", "hm2_hm3", "hm1_hm3",
                        "slope_per_half_month",
                    )
                },
                "methodology": (
                    "Rebuilt for each common assessment date from entitled "
                    "HM1/HM2/HM3 daily Current-Symbol observations; no forward-fill "
                    "or interpolation; not an official Platts FC curve."
                ),
                "points": points,
            })
            diagnostic.update({
                "status": quality_status,
                "reason": reason_codes[0] if reason_codes else None,
                "reason_codes": reason_codes,
                "as_of_date": curve_date,
                "first_date": snapshots[0]["as_of_date"],
                "last_date": curve_date,
                "available_dates": [snapshot["as_of_date"] for snapshot in snapshots],
                "snapshot_count": len(snapshots),
                "rejected_dates": rejected_dates,
                "incomplete_date_count": len(incomplete_dates),
                "incomplete_dates": incomplete_dates[-30:],
                "missing_latest_components": latest_missing_components,
                "freshness": curve_freshness,
            })
            diagnostics.append(diagnostic)
        return curves, diagnostics

    @staticmethod
    def _curve_records_by_date(
        records: Sequence[Mapping[str, Any]],
    ) -> tuple[Dict[str, Dict[str, Any]], list[Dict[str, Any]]]:
        """Index one effective daily leg and surface duplicates instead of hiding them."""
        indexed: Dict[str, Dict[str, Any]] = {}
        duplicates: list[Dict[str, Any]] = []
        for raw in records:
            curve_date = str(raw.get("date") or raw.get("observation_date") or "").strip()
            if not curve_date:
                continue
            record = dict(raw)
            current = indexed.get(curve_date)
            if current is not None:
                duplicates.append({
                    "date": curve_date,
                    "kept_id": current.get("id"),
                    "duplicate_id": record.get("id"),
                })
                current_rank = (
                    str(current.get("publication_time") or ""),
                    str(current.get("fetched_at") or ""),
                    int(current.get("id") or 0),
                )
                incoming_rank = (
                    str(record.get("publication_time") or ""),
                    str(record.get("fetched_at") or ""),
                    int(record.get("id") or 0),
                )
                if incoming_rank >= current_rank:
                    indexed[curve_date] = record
            else:
                indexed[curve_date] = record
        return indexed, duplicates

    @staticmethod
    def _curve_leg_anomalies(
        records: Mapping[str, Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        ordered: list[tuple[str, float]] = []
        for curve_date in sorted(records):
            try:
                value = float(records[curve_date].get("value"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                ordered.append((curve_date, value))
        changes = []
        for (prior_date, prior), (curve_date, value) in zip(ordered, ordered[1:]):
            delta = value - prior
            pct = abs(delta / prior) if prior else None
            changes.append({
                "date": curve_date,
                "prior_date": prior_date,
                "value": value,
                "prior_value": prior,
                "change": delta,
                "pct_change": pct,
            })
        recent = changes[-CURVE_ANOMALY_WINDOW:]
        absolute_changes = [abs(item["change"]) for item in recent]
        adaptive_threshold = CURVE_ANOMALY_ABS_USD_MT
        if len(absolute_changes) >= 5:
            center = median(absolute_changes)
            mad = median(abs(value - center) for value in absolute_changes)
            adaptive_threshold = max(
                CURVE_ANOMALY_ABS_USD_MT,
                center + CURVE_ANOMALY_MAD_MULTIPLIER * max(mad, 0.5),
            )
        anomalies = []
        for item in recent:
            reasons = []
            if abs(item["change"]) >= adaptive_threshold:
                reasons.append("absolute_or_robust_threshold")
            if item["pct_change"] is not None and item["pct_change"] >= CURVE_ANOMALY_PCT:
                reasons.append("percentage_threshold")
            if not reasons:
                continue
            anomalies.append({
                "date": item["date"],
                "prior_date": item["prior_date"],
                "value": round(item["value"], 6),
                "prior_value": round(item["prior_value"], 6),
                "change": round(item["change"], 6),
                "pct_change": round(item["pct_change"] * 100, 4)
                if item["pct_change"] is not None else None,
                "absolute_threshold": round(adaptive_threshold, 6),
                "percentage_threshold": CURVE_ANOMALY_PCT * 100,
                "reasons": reasons,
            })
        return anomalies

    def _curve_quality_summary(
        self,
        diagnostics: Sequence[Mapping[str, Any]],
        *,
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not diagnostics:
            return {
                "status": "not_applicable",
                "reason_codes": [],
                "pipeline": "daily_current_refresh",
            }
        components = [
            {"curve_key": diagnostic.get("canonical_key"), **dict(component)}
            for diagnostic in diagnostics
            for component in diagnostic.get("components") or []
        ]
        expected_legs = sum(int(row.get("expected_component_count") or 0)
                            for row in diagnostics)
        available_legs = sum(
            1 for component in components if component.get("last_date")
        )
        latest_observed_date = max(
            (str(component["last_date"]) for component in components
             if component.get("last_date")),
            default=None,
        )
        missing_latest_legs = [
            str(component.get("canonical_key"))
            for component in components
            if latest_observed_date and component.get("last_date") != latest_observed_date
        ]
        complete_date_sets = [
            set(str(value) for value in diagnostic.get("available_dates") or [])
            for diagnostic in diagnostics
        ]
        common_dates = set.intersection(*complete_date_sets) if complete_date_sets else set()
        latest_common_date = max(common_dates, default=None)
        quality_reference = date.fromisoformat(normalize_date(as_of)) if as_of else None
        common_freshness = freshness(latest_common_date, quality_reference)
        reason_codes: list[str] = []
        for diagnostic in diagnostics:
            for reason in diagnostic.get("reason_codes") or []:
                if reason not in reason_codes:
                    reason_codes.append(str(reason))
        if available_legs < expected_legs and "missing_or_unentitled_component" not in reason_codes:
            reason_codes.insert(0, "missing_or_unentitled_component")
        if missing_latest_legs and "six_leg_latest_date_incomplete" not in reason_codes:
            reason_codes.insert(0, "six_leg_latest_date_incomplete")
        if not latest_common_date and "no_complete_six_leg_snapshot" not in reason_codes:
            reason_codes.insert(0, "no_complete_six_leg_snapshot")

        latest_daily_import = None
        for run in self.store.recent_runs(50):
            metadata = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
            if (run.get("source") == "platts_excel" and
                    str(metadata.get("purpose") or "").lower() == "daily" and
                    str(run.get("scope") or "").lower() in {"asia", "all"}):
                latest_daily_import = {
                    "run_id": run.get("id"),
                    "status": run.get("status"),
                    "finished_at": run.get("finished_at"),
                    "rows_seen": run.get("rows_seen"),
                    "rows_inserted": run.get("rows_inserted"),
                    "rows_updated": run.get("rows_updated"),
                    "rows_unchanged": int(metadata.get("rows_unchanged") or 0),
                    "duplicate_input_rows": int(metadata.get("duplicate_input_rows") or 0),
                    "duplicate_curve_leg_rows": int(
                        metadata.get("duplicate_curve_leg_rows") or 0
                    ),
                }
                break
        if (latest_daily_import and latest_daily_import["duplicate_curve_leg_rows"] and
                "duplicate_daily_refresh_rows" not in reason_codes):
            reason_codes.append("duplicate_daily_refresh_rows")

        if available_legs < expected_legs or not latest_common_date:
            quality_status = "incomplete"
        elif reason_codes:
            quality_status = "warning"
        else:
            quality_status = "ready"
        anomalies = [
            {"curve_key": diagnostic.get("canonical_key"), **dict(anomaly)}
            for diagnostic in diagnostics
            for anomaly in diagnostic.get("anomalies") or []
        ]
        storage_duplicate_count = sum(
            int(row.get("duplicate_record_count") or 0) for row in diagnostics
        )
        import_duplicate_count = int(
            (latest_daily_import or {}).get("duplicate_curve_leg_rows") or 0
        )
        return {
            "status": quality_status,
            "reason_codes": reason_codes,
            "pipeline": "daily_current_refresh",
            "pipeline_role": "source_of_truth_for_daily_derived_curves",
            "storage_policy": "persist_source_legs_rebuild_complete_snapshots",
            "snapshot_policy": "same_date_hm1_hm2_hm3_required_per_curve",
            "incomplete_policy": "retain_source_observations_skip_curve_snapshot",
            "expected_legs": expected_legs,
            "available_legs": available_legs,
            "complete_curve_count": sum(
                1 for row in diagnostics if int(row.get("snapshot_count") or 0) > 0
            ),
            "expected_curve_count": len(diagnostics),
            "generated_snapshot_count": sum(
                int(row.get("snapshot_count") or 0) for row in diagnostics
            ),
            "common_six_leg_snapshot_count": len(common_dates),
            "latest_observed_date": latest_observed_date,
            "latest_common_date": latest_common_date,
            "latest_common_freshness": common_freshness,
            "missing_latest_legs": missing_latest_legs,
            "incomplete_date_count": sum(
                int(row.get("incomplete_date_count") or 0) for row in diagnostics
            ),
            "rejected_date_count": sum(
                len(row.get("rejected_dates") or []) for row in diagnostics
            ),
            "duplicate_record_count": storage_duplicate_count,
            "latest_import_duplicate_curve_leg_rows": import_duplicate_count,
            "duplicate_total_count": storage_duplicate_count + import_duplicate_count,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies[-20:],
            "components": components,
            "latest_daily_import": latest_daily_import,
        }

    @staticmethod
    def _daily_curve_shape(values: Sequence[Any]) -> Dict[str, Any]:
        numbers = [float(value) for value in values]
        if len(numbers) != 3:
            raise ValueError("daily derived curve requires exactly three tenor values")
        hm1_hm2 = numbers[0] - numbers[1]
        hm2_hm3 = numbers[1] - numbers[2]
        hm1_hm3 = numbers[0] - numbers[2]
        tolerance = max(0.01, max(abs(value) for value in numbers) * 0.000001)
        if abs(hm1_hm2) <= tolerance and abs(hm2_hm3) <= tolerance:
            structure = "flat"
        elif hm1_hm2 > tolerance and hm2_hm3 > tolerance:
            structure = "backwardation"
        elif hm1_hm2 < -tolerance and hm2_hm3 < -tolerance:
            structure = "contango"
        else:
            structure = "kinked"
        return {
            "structure": structure,
            "hm1_hm2": round(hm1_hm2, 6),
            "hm2_hm3": round(hm2_hm3, 6),
            "hm1_hm3": round(hm1_hm3, 6),
            "slope_per_half_month": round((numbers[2] - numbers[0]) / 2, 6),
        }

    def spreads(self, as_of: Optional[str] = None, window: int = 252,
                definitions: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        definitions = definitions or DEFAULT_SPREADS
        keys = {key for definition in definitions for key, _ in definition["legs"]}
        histories, series_by_key = {}, {}
        for key in keys:
            series = self.store.get_series_by_canonical_key(key)
            if series is None or series["entitlement_state"] != "entitled" or not series["active"]:
                histories[key] = []
                continue
            series_by_key[key] = series
            histories[key] = self.store.history(series["id"], end=as_of)

        items = []
        for definition in definitions:
            spread = calculate_spread(
                histories, definition, as_of=as_of, window=window,
                max_stale_business_days=1,
            )
            for leg in spread.get("legs", []):
                series = series_by_key.get(leg["canonical_key"])
                if series:
                    leg["series_id"] = series["id"]
                    leg["name"] = series["name"]
            items.append(spread)
        effective_as_of = normalize_date(as_of) if as_of else max(
            (item["observation_date"] for item in items if item.get("observation_date")),
            default=None,
        )
        return json_safe({"as_of": effective_as_of, "items": items})

    def news(self, filters: Optional[Mapping[str, Any]] = None,
             **kwargs: Any) -> Dict[str, Any]:
        merged = dict(filters or {})
        merged.update({key: value for key, value in kwargs.items() if value is not None})
        # Licensed/public article content is only queryable after the adapter
        # has marked it entitled.  Metadata about denied access belongs in
        # Data Status, not in the news payload.
        merged["entitlement_state"] = "entitled"
        payload = self.store.news(merged)
        from .news_sources import freshness_metadata
        freshness_counts: Dict[str, int] = defaultdict(int)
        for item in payload["items"]:
            fresh = freshness_metadata(item.get("published_at"))
            metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            if str(metadata.get("date_quality") or "reported") not in {
                "reported", "assumed_utc",
            }:
                fresh = {**fresh, "freshness": "unknown", "freshness_score": 0}
            item.update(fresh)
            # A high-impact discovery hit is not automatically confirmed.
            # Ingestion only persists is_breaking after the official/multi-source
            # confirmation rule has been applied; query-time freshness may age a
            # confirmed item out, but must never promote an unconfirmed one.
            item["is_breaking"] = bool(
                fresh["freshness"] == "breaking" and item.get("is_breaking")
            )
            item["rank_score"] = round(
                float(item.get("relevance_score") or 0) * 0.68
                + float(fresh["freshness_score"]) * 0.22
                + int(item.get("source_tier") or 0) * 2
                + (8 if item["is_breaking"] else 0), 2,
            )
            freshness_counts[fresh["freshness"]] += 1
        payload["items"].sort(
            key=lambda item: (bool(item.get("is_breaking")),
                              float(item.get("rank_score") or 0),
                              str(item.get("published_at") or "")),
            reverse=True,
        )
        payload["freshness"] = dict(freshness_counts)
        payload["source_health"] = self._news_source_health()
        payload["source_status"] = {
            "healthy": sum(row.get("status") in {"healthy", "empty"}
                           for row in payload["source_health"]),
            "stale": sum(row.get("status") == "stale" for row in payload["source_health"]),
            "failed": sum(row.get("status") == "error" for row in payload["source_health"]),
            "total": len(payload["source_health"]),
        }
        payload["ranking"] = {
            "strategy": "lpg_relevance_freshness_source",
            "entitlement_boundary": "platts_api_separate_public_sources_labelled",
        }
        return json_safe(payload)

    def _news_source_health(self) -> list[Dict[str, Any]]:
        from .news_sources import freshness_metadata
        rows = self.store.news_source_health()
        for row in rows:
            attempt = freshness_metadata(row["last_attempt_at"]) if row.get("last_attempt_at") else None
            latest = freshness_metadata(row["latest_published_at"]) if row.get("latest_published_at") else None
            if attempt:
                row["attempt_age_minutes"] = attempt["age_minutes"]
                row["stale"] = attempt["age_minutes"] > 30
                if row["stale"] and row.get("status") in {"healthy", "empty"}:
                    row["stored_status"] = row["status"]
                    row["status"] = "stale"
            else:
                row["stale"] = True
            if latest:
                row["latest_age_minutes"] = latest["age_minutes"]
                row["latest_freshness"] = latest["freshness"]
        return rows

    def refresh_intelligence(self, limit: int = 5000) -> Dict[str, Any]:
        """Rebuild durable LPG events from the entitled news evidence of record."""
        news = self.store.news({"limit": max(1, min(int(limit), 5000)), "offset": 0})
        events = build_events(news.get("items") or [])
        counts = {"seen": len(events), "inserted": 0, "updated": 0, "failed": 0}
        errors = []
        try:
            persisted = self.store.upsert_intelligence_events(events)
            counts["inserted"] = persisted["inserted"]
            counts["updated"] = persisted["updated"]
        except Exception as exc:  # noqa: BLE001 - derived-event isolation boundary
            counts["failed"] = len(events)
            errors.append(str(exc))
        return json_safe({
            "status": "partial" if errors and events else "failed" if errors else "success",
            "counts": counts,
            "errors": errors[:20],
            "version": INTELLIGENCE_VERSION,
            "updated_at": utc_now(),
        })

    def situation(self, filters: Optional[Mapping[str, Any]] = None,
                  **kwargs: Any) -> Dict[str, Any]:
        """Return the map-first LPG situation and its explicit evidence gaps."""
        merged = dict(filters or {})
        merged.update({key: value for key, value in kwargs.items() if value is not None})
        if merged.get("as_of") and not merged.get("end"):
            merged["end"] = merged.pop("as_of")
        status = self.store.intelligence_status()
        sync = None
        if self.store.intelligence_needs_sync():
            sync = self.refresh_intelligence()
            status = self.store.intelligence_status()
        payload = self.store.intelligence_events(merged)
        events = payload["items"]
        for event in events:
            event["event_type_label"] = EVENT_LABELS.get(
                str(event.get("event_type")), str(event.get("event_type") or "").replace("_", " ").title(),
            )
            confidence = int(event.get("confidence_score") or 0)
            event["confidence_label"] = "high" if confidence >= 80 else "medium" if confidence >= 55 else "low"
            event["active"] = bool(event.get("active"))
        all_events = self.store.intelligence_events({"limit": 5000}).get("items") or []
        market = self.summary(as_of=merged.get("end"))
        located = sum(event.get("latitude") is not None and event.get("longitude") is not None
                      for event in events)
        confirmed = sum(event.get("confirmation_state") == "confirmed" for event in events)
        gap_counts: Dict[str, int] = defaultdict(int)
        for event in events:
            for gap in event.get("data_gaps") or []:
                gap_counts[str(gap)] += 1
        try:
            from .news_sources import PlattsNewsClient
            news_api_configured = PlattsNewsClient().configured
        except Exception:
            news_api_configured = False
        intelligence_gaps = [
            {
                "id": "satellite_ais",
                "status": "unavailable",
                "detail": "No entitled satellite AIS or live vessel-position feed is configured; routes are reference corridors, not live tracks.",
            },
            {
                "id": "terminal_operations",
                "status": "unavailable",
                "detail": "No authoritative live terminal operating-status feed is configured; disruption state comes from attributed news evidence.",
            },
            {
                "id": "platts_news",
                "status": "configured" if news_api_configured else "not_configured",
                "detail": "Machine-readable Platts News remains a separate entitlement from the Excel Add-in.",
            },
        ]
        return json_safe({
            **payload,
            "items": events,
            "events": events,
            "assets": asset_catalog(),
            "routes": route_catalog(),
            "scenario_engine": intelligence_scenario_catalog(),
            "vessel_intelligence": self.vessel_intelligence({"compact": True}),
            "market_snapshot": {
                "as_of": market.get("as_of"),
                "prices": market.get("prices") or [],
                "spreads": market.get("spreads") or [],
                "errors": market.get("errors") or {},
            },
            "coverage": {
                "total": len(events),
                "located": located,
                "unlocated": len(events) - located,
                "confirmed": confirmed,
                "developing": len(events) - confirmed,
                "active": sum(bool(event.get("active")) for event in events),
                "gap_counts": dict(gap_counts),
            },
            "alerting": baseline_alerts(all_events),
            "intelligence_gaps": intelligence_gaps,
            "methodology": {
                "version": INTELLIGENCE_VERSION,
                "event_source": "entitled rows in the local LPG news store",
                "geography": "explicit named-location matching only; unresolved events remain unlocated",
                "route_state": "curated reference corridors; not live AIS tracks",
                "impact_state": "inferred exposure, never an official price assessment",
                "corroboration": "official machine-readable evidence or at least two attributed sources",
            },
            "persistence": status,
            "sync": sync,
            "updated_at": utc_now(),
        })

    def import_vessel_port_calls(self, path: Any,
                                 fleet_group: str = "reference_fleet") -> Dict[str, Any]:
        """Import a historical port-call snapshot with an auditable run record."""
        snapshot = load_port_call_snapshot(path, fleet_group=fleet_group)
        run = self.store.start_run(
            "vessel_snapshot", scope=fleet_group,
            metadata={
                "file_name": snapshot["source_file_name"],
                "sha256": snapshot["source_file_sha256"],
                "boundary": snapshot["boundary"],
            },
        )
        try:
            counts = self.store.import_vessel_snapshot(snapshot, ingestion_run_id=run["id"])
            finished = self.store.finish_run(
                run["id"], "success",
                rows_seen=counts["port_calls_seen"],
                rows_inserted=counts["port_calls_inserted"],
                rows_updated=counts["port_calls_updated"],
                rows_skipped=counts["port_calls_unchanged"],
            )
        except Exception as exc:
            self.store.finish_run(run["id"], "failed", error=str(exc))
            raise
        return json_safe({
            "status": "success",
            "version": VESSEL_INTELLIGENCE_VERSION,
            "counts": counts,
            "coverage": snapshot["coverage"],
            "source_file": snapshot["source_file_name"],
            "source_file_sha256": snapshot["source_file_sha256"],
            "source_snapshot_at": snapshot["source_snapshot_at"],
            "boundary": snapshot["boundary"],
            "run": finished,
        })

    @staticmethod
    def _position_state(position: Mapping[str, Any]) -> Dict[str, Any]:
        observed = position.get("observed_at")
        if not observed:
            return {"freshness": "unknown", "age_hours": None}
        try:
            parsed = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return {"freshness": "timezone_unverified", "age_hours": None}
            age = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600)
        except (TypeError, ValueError):
            return {"freshness": "timestamp_unverified", "age_hours": None}
        freshness = "live" if age <= 1 else "recent" if age <= 24 else "stale"
        return {"freshness": freshness, "age_hours": round(age, 1)}

    @staticmethod
    def _public_vessel_evidence(value: Mapping[str, Any]) -> Dict[str, Any]:
        """Keep raw provider evidence private while exposing its audit handle."""
        output = dict(value)
        raw = output.pop("raw", None)
        output["raw_evidence_available"] = raw not in (None, {}, [], "")
        return json_safe(output)

    def list_vessels(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        payload = self.store.vessels(filters)
        for vessel in payload["items"]:
            if vessel.get("last_port_call"):
                vessel["last_port_call"] = self._public_vessel_evidence(
                    vessel["last_port_call"],
                )
            if vessel.get("last_position"):
                vessel["last_position"] = self._public_vessel_evidence(
                    vessel["last_position"],
                )
        return json_safe(payload)

    def vessel_port_calls(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        payload = self.store.vessel_port_calls(filters)
        payload["items"] = [self._public_vessel_evidence(item) for item in payload["items"]]
        return json_safe(payload)

    def vessel_intelligence(self, filters: Optional[Mapping[str, Any]] = None,
                            **kwargs: Any) -> Dict[str, Any]:
        """Return vessel context while keeping live and historical evidence separate."""
        merged = dict(filters or {})
        merged.update({key: value for key, value in kwargs.items() if value is not None})
        fleet = self.list_vessels({
            "q": merged.get("q"), "fleet_group": merged.get("fleet_group"),
            "active": merged.get("active", True), "limit": merged.get("vessel_limit", 500),
        })
        calls = self.vessel_port_calls({
            "vessel_id": merged.get("vessel_id"), "source": merged.get("source"),
            "operation_signal": merged.get("operation_signal"), "q": merged.get("q"),
            "start": merged.get("start"), "end": merged.get("end"),
            "limit": merged.get("limit", 1000), "offset": merged.get("offset", 0),
        })
        positions = self.store.vessel_positions({
            "vessel_id": merged.get("vessel_id"),
            "position_kind": merged.get("position_kind"),
            "limit": merged.get("position_limit", 1000),
        })
        positions["items"] = [self._public_vessel_evidence(item) for item in positions["items"]]
        for position in positions["items"]:
            position.update(self._position_state(position))
        if merged.get("compact"):
            per_vessel: Dict[str, int] = defaultdict(int)
            compact_calls = []
            for call in calls["items"]:
                vessel_id = str(call.get("vessel_id") or "")
                if per_vessel[vessel_id] >= 12:
                    continue
                compact_calls.append(call)
                per_vessel[vessel_id] += 1
            calls["items"] = compact_calls
            latest_positions = {}
            for position in positions["items"]:
                latest_positions.setdefault(str(position.get("vessel_id") or ""), position)
            positions["items"] = list(latest_positions.values())
        status = self.store.vessel_intelligence_status()
        live_count = sum(position.get("freshness") == "live" for position in positions["items"])
        return json_safe({
            "version": VESSEL_INTELLIGENCE_VERSION,
            "vessels": fleet["items"],
            "vessel_total": fleet["total"],
            "port_calls": calls["items"],
            "port_call_total": calls["total"],
            "port_call_returned": len(calls["items"]),
            "positions": positions["items"],
            "position_total": positions["total"],
            "position_returned": len(positions["items"]),
            "source_health": self.store.vessel_source_health(),
            "coverage": {
                **status,
                "fresh_live_positions": live_count,
                "display_state": "live_available" if live_count else (
                    "historical_only" if calls["total"] else "unavailable"
                ),
            },
            "intelligence_gaps": [
                {
                    "id": "live_ais",
                    "status": "configured" if live_count else "unavailable",
                    "detail": "No fresh timestamped live AIS position is available." if not live_count else
                              "Fresh provider positions are available and age-labelled.",
                },
                {
                    "id": "continuous_tracks",
                    "status": "unavailable" if not positions["total"] else "partial",
                    "detail": "Historical port-call points are not continuous vessel tracks.",
                },
                {
                    "id": "source_entitlement",
                    "status": "review_required",
                    "detail": "Snapshot availability does not establish production API or redistribution rights.",
                },
            ],
            "methodology": {
                "position_rule": "only timestamped vessel_positions may be presented as positions",
                "port_call_rule": "stopped coordinates remain historical port-call observations",
                "cargo_rule": "draught change is an inferred operation signal, never cargo proof",
                "timestamp_rule": "naive source timestamps remain source_timezone_unverified",
                "freshness_rule": "live <=1h; recent <=24h; older positions stale",
            },
            "updated_at": utc_now(),
        })

    def scenarios(self) -> Dict[str, Any]:
        """Return the bounded scenario templates without accessing market data."""
        return intelligence_scenario_catalog()

    def run_scenario(self, scenario_id: str,
                     inputs: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Evaluate assumptions and attach only entitled current market rows."""
        result = calculate_scenario(scenario_id, inputs)
        market = self.summary()
        result["market_snapshot"] = {
            "as_of": market.get("as_of"),
            "prices": market.get("prices") or [],
            "spreads": market.get("spreads") or [],
            "errors": market.get("errors") or {},
        }
        result["guardrail"] = (
            "Current entitled rows provide context only; the engine does not calculate a price, "
            "freight, probability, cargo-flow, or P&L outcome."
        )
        return json_safe(result)

    def explorer(self, dataset: str = "observations", **filters: Any) -> Dict[str, Any]:
        dataset = str(dataset or "observations").lower()
        as_of = filters.pop("as_of", None)
        if as_of and not filters.get("end"):
            filters["end"] = as_of
        if dataset in {"moc", "fundamentals", "all"}:
            names = {
                "moc": ("platts_ewindow",),
                "fundamentals": ("platts_fundamentals",),
                "all": ("platts_ewindow", "platts_fundamentals"),
            }[dataset]
            rows = []
            for name in names:
                result = self.store.explorer(
                    "dataset_rows", name=name, **filters,
                )
                rows.extend(self._flatten_dataset_row(row) for row in result["rows"])
            rows.sort(key=lambda row: (str(row.get("as_of_date") or ""),
                                       str(row.get("order_time") or "")), reverse=True)
            limit = max(1, min(int(filters.get("limit") or 500), 5000))
            offset = max(0, int(filters.get("offset") or 0))
            page = rows[offset:offset + limit]
            columns = list(page[0]) if page else []
            dataset_status = {
                "status": "ready" if rows else "empty",
                "reason": None if rows else (
                    "no_fundamentals_rows" if dataset == "fundamentals" else "no_moc_rows"
                ),
                "rows": len(rows),
                "dates": len({row.get("as_of_date") for row in rows if row.get("as_of_date")}),
                "series": len({row.get("series_id") for row in rows if row.get("series_id")}),
            }
            runtime = (
                self._specialized_runtime_statuses().get("moc")
                if dataset in {"moc", "all"} else None
            )
            dataset_status = self._with_runtime_status(dataset_status, runtime)
            return json_safe({
                "dataset": dataset, "columns": columns, "rows": page,
                "total": len(rows), "limit": limit, "offset": offset,
                "dataset_status": dataset_status,
                "refresh_state": runtime.get("state") if runtime else None,
                "refresh_reason": self._runtime_reason(runtime),
                "runtime_status": runtime,
            })
        if dataset == "spreads":
            rows = self.spreads(as_of=as_of or filters.get("end"))["items"]
            return self._rows_payload(dataset, rows, filters)
        if dataset == "runs":
            return self._rows_payload(dataset, self.store.recent_runs(200), filters)
        if dataset in {"events", "intelligence"}:
            payload = self.store.intelligence_events(filters)
            rows = payload.get("items") or []
            return {
                "dataset": "events", "columns": list(rows[0]) if rows else [], "rows": rows,
                "total": payload.get("total", len(rows)), "limit": payload.get("limit"),
                "offset": payload.get("offset"),
            }
        return self.store.explorer(dataset=dataset, **filters)

    def status(self) -> Dict[str, Any]:
        status = self.store.status()
        try:
            from .news_sources import PlattsNewsClient
            news_configured = PlattsNewsClient().configured
        except Exception:
            news_configured = False
        sources = list(status.get("sources") or [])
        if not any(source.get("source") == "platts_news_api" for source in sources):
            sources.append({
                "source": "platts_news_api",
                "status": "configured" if news_configured else "not_configured",
                "series_count": 0,
                "entitled_count": 0,
                "last_attempt_at": None,
                "last_success_at": None,
                "error": None if news_configured else "Machine-readable news API entitlement is not configured",
            })
        runtime = self._daily_runtime_status()
        excel = next((source for source in sources if source.get("source") == "platts_excel"), None)
        if excel is None:
            excel = {"source": "platts_excel", "series_count": 0, "entitled_count": 0}
            sources.append(excel)
        if runtime:
            excel.update({
                "status": runtime.get("state") or excel.get("status") or "unknown",
                "last_attempt_at": runtime.get("updated_at"),
                "error": ", ".join(runtime.get("reason_codes") or []) or None,
            })
        status["sources"] = sources
        status["news_sources"] = self._news_source_health()
        status["runtime"] = runtime
        status["news_api_configured"] = news_configured
        matrix = self.store.candidates()
        status["entitlement_matrix"] = matrix
        specialized = self._specialized_runtime_statuses()
        status["specialized_runtime"] = specialized
        dataset_runtimes = {
            name: value for name, value in specialized.items() if name != "curves"
        }
        if runtime:
            dataset_runtimes["current"] = runtime
            dataset_runtimes["curves"] = runtime
        coverage = self._dataset_health(status.get("datasets"), dataset_runtimes)
        status["datasets"] = self._operational_dataset_health(
            coverage, status=status, news_configured=news_configured, matrix=matrix,
        )
        status["availability_summary"] = self._availability_summary(status["datasets"])
        return json_safe(status)

    def _dataset_health(self, stored: Optional[Mapping[str, Any]] = None,
                        specialized: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Enrich persisted coverage metrics with official/derived curve availability."""
        datasets = {
            str(key): dict(value) for key, value in dict(stored or {}).items()
            if isinstance(value, Mapping)
        }
        curves = self.curves()
        all_curves = curves["curves"]
        curve_dates = sorted({
            str(curve_date)
            for curve in all_curves
            for curve_date in (
                curve.get("available_dates") or
                ([curve.get("as_of_date")] if curve.get("as_of_date") else [])
            )
        })
        official = [curve for curve in all_curves if curve.get("is_official")]
        derived = [curve for curve in all_curves if curve.get("derived")]
        quality = dict(curves.get("quality") or {})
        endpoint_status = dict(curves.get("dataset_status") or {})
        datasets["curves"] = {
            "rows": sum(len(curve.get("points") or []) for curve in all_curves),
            "dates": len(curve_dates),
            "series": len(all_curves),
            "first_date": curve_dates[0] if curve_dates else None,
            "last_date": curve_dates[-1] if curve_dates else None,
            "official_rows": sum(len(curve.get("points") or []) for curve in official),
            "official_series": len(official),
            "derived_rows": sum(len(curve.get("points") or []) for curve in derived),
            "derived_series": len(derived),
            "derived_snapshot_count": sum(
                int(curve.get("history_count") or 0) for curve in derived
            ),
            "official_fc_status": OFFICIAL_FC_CURVE_ACCESS,
            "status": endpoint_status.get("status") or curves["status"],
            "reason": endpoint_status.get("reason") or curves["reason"],
            "quality_status": quality.get("status"),
            "quality_reasons": quality.get("reason_codes") or [],
            "quality": quality,
            "expected_legs": quality.get("expected_legs"),
            "available_legs": quality.get("available_legs"),
            "latest_common_date": quality.get("latest_common_date"),
            "missing_latest_legs": quality.get("missing_latest_legs") or [],
            "duplicate_record_count": quality.get("duplicate_total_count", 0),
            "anomaly_count": quality.get("anomaly_count", 0),
        }
        for name, runtime in dict(specialized or {}).items():
            if name in datasets and isinstance(runtime, Mapping):
                datasets[name] = self._with_runtime_status(datasets[name], runtime)
        return datasets

    @classmethod
    def _operational_dataset_health(
        cls,
        coverage: Mapping[str, Mapping[str, Any]],
        *,
        status: Mapping[str, Any],
        news_configured: bool,
        matrix: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Add user-facing capability, access, and pipeline state to coverage rows."""
        datasets = {
            str(key): dict(value) for key, value in coverage.items()
            if isinstance(value, Mapping)
        }
        runs = [dict(row) for row in status.get("runs") or [] if isinstance(row, Mapping)]

        def latest_run(source: str) -> Optional[Dict[str, Any]]:
            return next((row for row in runs if row.get("source") == source), None)

        def run_runtime(source: str) -> Optional[Dict[str, Any]]:
            run = latest_run(source)
            if not run:
                return None
            return {
                "state": run.get("status") or "unknown",
                "updated_at": run.get("finished_at") or run.get("started_at"),
                "reason": run.get("error"),
                "run_id": run.get("id"),
                "rows_seen": run.get("rows_seen"),
                "rows_inserted": run.get("rows_inserted"),
                "rows_updated": run.get("rows_updated"),
            }

        news_runtime = run_runtime("lpg_news")
        if news_runtime and "news" in datasets:
            datasets["news"] = cls._with_runtime_status(datasets["news"], news_runtime)

        intelligence = dict(status.get("intelligence") or {})
        latest_event = intelligence.get("latest_event_at")
        situation = {
            "rows": int(intelligence.get("total") or 0),
            "dates": 1 if latest_event else 0,
            "series": 0,
            "first_date": None,
            "last_date": str(latest_event)[:10] if latest_event else None,
            "active_rows": int(intelligence.get("active") or 0),
            "located_rows": int(intelligence.get("located") or 0),
            "confirmed_rows": int(intelligence.get("confirmed") or 0),
            "status": "ready" if intelligence.get("total") else "empty",
            "reason": None if intelligence.get("total") else "no_situation_events",
        }
        datasets["situation"] = (
            cls._with_runtime_status(situation, news_runtime) if news_runtime else situation
        )

        vessel = dict(status.get("vessel_intelligence") or {})
        latest_call = vessel.get("latest_port_call_at")
        vessel_history = {
            "rows": int(vessel.get("historical_port_calls") or 0),
            "dates": 1 if latest_call else 0,
            "series": int(vessel.get("vessels") or 0),
            "first_date": None,
            "last_date": str(latest_call)[:10] if latest_call else None,
            "status": "ready" if vessel.get("historical_port_calls") else "empty",
            "reason": None if vessel.get("historical_port_calls") else "no_vessel_history_snapshot",
        }
        vessel_runtime = run_runtime("vessel_snapshot")
        datasets["vessel_history"] = (
            cls._with_runtime_status(vessel_history, vessel_runtime)
            if vessel_runtime else vessel_history
        )

        live_positions = int(vessel.get("live_positions") or 0)
        stored_positions = int(vessel.get("positions") or 0)
        if live_positions:
            live_status, live_reason = "ready", None
        elif stored_positions:
            live_status, live_reason = "limited", "no_fresh_live_ais_positions"
        else:
            live_status, live_reason = "not_configured", "no_live_ais_provider_configured"
        latest_position = vessel.get("latest_position_at")
        datasets["live_ais"] = {
            "rows": live_positions,
            "stored_rows": stored_positions,
            "dates": 1 if latest_position else 0,
            "series": int(vessel.get("vessels") or 0) if stored_positions else 0,
            "first_date": None,
            "last_date": str(latest_position)[:10] if latest_position else None,
            "status": live_status,
            "reason": live_reason,
        }

        candidate_families = {
            "current": {"price", "freight"},
            "curves": {"curve", "spread"},
            "moc": {"ewindow"},
        }
        for name, capability in DATASET_CAPABILITIES.items():
            row = datasets.setdefault(name, {
                "rows": 0, "dates": 0, "series": 0,
                "first_date": None, "last_date": None,
                "status": "empty", "reason": "dataset_not_loaded",
            })
            coverage_status = str(row.get("coverage_status") or row.get("status") or "empty")
            coverage_reason = row.get("coverage_reason", row.get("reason"))
            row.setdefault("coverage_status", coverage_status)
            row.setdefault("coverage_reason", coverage_reason)
            row.update(capability)

            if not row.get("refresh_status"):
                row["refresh_status"] = (
                    "not_run" if capability["refresh_supported"]
                    else "manual" if name == "vessel_history"
                    else "not_supported"
                )

            if name == "fundamentals":
                row["refresh_reason"] = "no_reliable_licensed_source_configured"
                if row.get("rows"):
                    row.update({
                        "status": "available_with_warning",
                        "reason": "stored_rows_available_without_refresh_pipeline",
                    })
                else:
                    row.update({
                        "status": "not_configured",
                        "reason": "no_reliable_licensed_source_configured",
                    })
            elif name in {"current", "history", "curves", "moc"}:
                if not row.get("rows") and row.get("refresh_status") == "not_run":
                    row.update({
                        "status": "not_loaded",
                        "reason": f"{name}_refresh_not_run",
                    })
            if (name == "moc" and not row.get("rows")
                    and row.get("refresh_status") in {
                        "success", "empty", "discovery_only", "complete", "completed",
                    }):
                row.update({
                    "status": "empty",
                    "reason": "no_moc_rows_in_current_window",
                    "access_check": "api_reached_no_matching_rows",
                })

            if name == "news":
                row["access_mode"] = (
                    "licensed_api_and_public_discovery" if news_configured
                    else "public_discovery"
                )
                row["licensed_feed_status"] = (
                    "configured" if news_configured else "not_configured"
                )
                if not news_configured:
                    row["gap_reason"] = "licensed_news_api_not_configured"
            elif name == "curves":
                row["official_fc_status"] = OFFICIAL_FC_CURVE_ACCESS
                row["official_refresh_supported"] = False
                row["derivation_method"] = "daily_hm_snapshot_v2_quality"
                if int(row.get("derived_series") or 0):
                    row.pop("gap_reason", None)
            elif name == "history" and row.get("refresh_status") in {
                "error", "failed", "blocked", "refresh_failed",
            }:
                row["gap_reason"] = row.get("refresh_reason") or "history_backfill_failed"

            families = candidate_families.get(name)
            if families:
                candidates = [item for item in matrix if str(item.get("family")) in families]
                states: Dict[str, int] = defaultdict(int)
                for item in candidates:
                    states[str(item.get("discovery_status") or "unknown")] += 1
                row["candidate_access"] = {
                    "total": len(candidates), "states": dict(states),
                }

            state_name = str(row.get("status") or "unknown")
            if row.get("rows"):
                row["availability_group"] = (
                    "available" if state_name == "ready" else "limited"
                )
            elif (name == "moc" and state_name == "empty"
                  and row.get("access_check") == "api_reached_no_matching_rows"):
                row["availability_group"] = "limited"
            elif state_name in {"not_configured", "unavailable", "unentitled"}:
                row["availability_group"] = "unavailable"
            else:
                row["availability_group"] = "not_loaded"
            row["visible_now"] = bool(row.get("rows"))

        return {name: datasets[name] for name in DATASET_CAPABILITIES}

    @staticmethod
    def _availability_summary(
        datasets: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, int]:
        groups: Dict[str, int] = defaultdict(int)
        refresh_issues = 0
        for row in datasets.values():
            groups[str(row.get("availability_group") or "not_loaded")] += 1
            if str(row.get("refresh_status") or "") in {
                "error", "failed", "blocked", "refresh_failed", "partial",
            }:
                refresh_issues += 1
        return {
            "total": len(datasets),
            "available": groups["available"],
            "limited": groups["limited"],
            "not_loaded": groups["not_loaded"],
            "unavailable": groups["unavailable"],
            "visible_now": sum(bool(row.get("visible_now")) for row in datasets.values()),
            "refresh_issues": refresh_issues,
        }

    @staticmethod
    def _runtime_reason(runtime: Optional[Mapping[str, Any]]) -> Optional[str]:
        if not runtime:
            return None
        reasons = runtime.get("reason_codes")
        if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)):
            joined = ", ".join(str(reason) for reason in reasons if reason)
            if joined:
                return joined
        return str(runtime.get("reason") or runtime.get("error") or "").strip() or None

    @classmethod
    def _with_runtime_status(cls, coverage: Mapping[str, Any],
                             runtime: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        output = dict(coverage)
        if not runtime:
            return output
        coverage_status = str(output.get("coverage_status") or output.get("status") or "empty")
        coverage_reason = output.get("coverage_reason", output.get("reason"))
        state = str(runtime.get("state") or runtime.get("status") or "unknown").lower()
        reason = cls._runtime_reason(runtime)
        output.update({
            "coverage_status": coverage_status,
            "coverage_reason": coverage_reason,
            "refresh_state": state,
            "refresh_status": state,
            "refresh_reason": reason,
            "last_refresh_at": runtime.get("updated_at") or runtime.get("finished_at")
                               or runtime.get("started_at"),
            "runtime_status": dict(runtime),
        })
        if state in {"error", "failed", "blocked", "refresh_failed"}:
            if int(output.get("rows") or 0) > 0:
                output.update({
                    "status": "available_with_warning",
                    "reason": reason or "latest_refresh_failed_last_good_data_preserved",
                })
            else:
                output.update({
                    "status": "refresh_failed",
                    "reason": reason or "specialized_refresh_failed",
                })
        elif state in {"deferred", "not_run", "not_loaded", "pending", "queued",
                       "running", "in_progress", "unknown"} and not output.get("rows"):
            output.update({
                "status": "not_loaded",
                "reason": reason or f"specialized_refresh_{state}",
            })
        elif state in {"partial", "partial_success"}:
            output.update({
                "status": "partial",
                "reason": reason or output.get("reason"),
            })
        return output

    @staticmethod
    def _daily_runtime_status() -> Dict[str, Any]:
        try:
            from .platts_excel import STAGING_DIR
            path = Path(STAGING_DIR) / "status.json"
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload) if isinstance(payload, Mapping) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _specialized_runtime_statuses() -> Dict[str, Dict[str, Any]]:
        try:
            from .platts_excel import STAGING_DIR
        except Exception:
            return {}
        locations = {
            "history": "backfill",
            "curves": "curve",
            "moc": "moc",
        }
        output: Dict[str, Dict[str, Any]] = {}
        for dataset, purpose in locations.items():
            path = Path(STAGING_DIR) / purpose / "status.json"
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("specialized status payload is not an object")
                output[dataset] = {
                    **dict(payload),
                    "dataset": dataset,
                    "purpose": purpose,
                    "status_path": str(path),
                }
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                output[dataset] = {
                    "dataset": dataset,
                    "purpose": purpose,
                    "state": "failed",
                    "reason_codes": ["invalid_specialized_status_file"],
                    "error": str(exc)[:500],
                    "status_path": str(path),
                }
        return json_safe(output)

    @staticmethod
    def _flatten_dataset_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        output = {key: value for key, value in row.items() if key != "payload_json"}
        payload = row.get("payload")
        if payload is None and row.get("payload_json"):
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, json.JSONDecodeError):
                payload = None
        if isinstance(payload, Mapping):
            output.update({str(key): value for key, value in payload.items()})
        return json_safe(output)

    @staticmethod
    def _rows_payload(dataset: str, rows: Sequence[Mapping[str, Any]],
                      filters: Mapping[str, Any]) -> Dict[str, Any]:
        query = str(filters.get("q") or "").lower().strip()
        materialized = [json_safe(dict(row)) for row in rows]
        if query:
            materialized = [row for row in materialized
                            if query in json.dumps(row, ensure_ascii=False).lower()]
        offset = max(0, int(filters.get("offset") or 0))
        limit = max(1, min(int(filters.get("limit") or 500), 5000))
        page = materialized[offset:offset + limit]
        columns = list(page[0]) if page else []
        return {"dataset": dataset, "columns": columns, "rows": page,
                "total": len(materialized), "limit": limit, "offset": offset}

    def export_rows(self, view: str, **filters: Any) -> Sequence[Dict[str, Any]]:
        """Return the entitlement-gated rows represented by an LPG UI view."""
        view = str(view or "cockpit").lower()
        filters = dict(filters)
        filters.pop("view", None)
        filters.pop("format", None)
        as_of = filters.get("as_of")
        if view == "cockpit":
            payload = self.summary(as_of=as_of)
            return [
                *({"record_type": "price", **row} for row in payload["prices"]),
                *({"record_type": "spread", **row} for row in payload["spreads"]),
            ]
        if view == "curves":
            curves = self.curves(as_of=as_of)["curves"]
            rows = []
            for curve in curves:
                for point in curve["points"]:
                    rows.append({"record_type": "curve", "series_id": curve["series_id"],
                                 "name": curve["name"], **point})
            rows.extend({"record_type": "spread", **row}
                        for row in self.spreads(as_of=as_of)["items"])
            return rows
        if view == "history":
            series_id = str(filters.get("series_id") or "")
            if not series_id:
                raise ValueError("history export requires series_id")
            return self.series_history(series_id, end=as_of)["observations"]
        if view == "news":
            if as_of and not filters.get("end"):
                filters["end"] = as_of
            return self.news(filters)["items"]
        if view == "situation":
            if as_of and not filters.get("end"):
                filters["end"] = as_of
            return self.situation(filters)["events"]
        if view in {"vessels", "port_calls"}:
            if as_of and not filters.get("end"):
                filters["end"] = as_of
            return self.vessel_intelligence(filters)["port_calls"]
        if view == "status":
            payload = self.status()
            return [
                *({"record_type": "source", **row} for row in payload.get("sources", [])),
                *({"record_type": "run", **row} for row in payload.get("runs", [])),
                *({"record_type": "candidate", **row} for row in self.store.candidates()),
            ]
        dataset = str(filters.get("dataset") or ("moc" if view == "moc" else "observations"))
        filters.pop("dataset", None)
        return self.explorer(dataset=dataset, **filters)["rows"]

    @staticmethod
    def _series_id(record: Mapping[str, Any]) -> str:
        explicit = str(record.get("series_id") or "").strip()
        if explicit:
            return explicit
        seed = str(record.get("symbol") or record.get("data_series") or
                   record.get("description") or "unknown")
        slug = re.sub(r"[^a-z0-9]+", "_", seed.lower()).strip("_")[:48] or "series"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
        return f"platts_{slug}_{digest}"

    @staticmethod
    def _entitlement_state(raw: Any) -> str:
        value = str(raw or "pending_review").strip().lower().replace(" ", "_")
        if value in {"entitled", "success", "available", "ok", "licensed"}:
            return "entitled"
        if value in {"unentitled", "not_entitled", "unauthorized", "forbidden", "no_access"}:
            return "unentitled"
        if value in {"retired", "inactive"}:
            return "retired"
        if value in {"error", "failed", "invalid"}:
            return "error"
        return "pending_review"

    @staticmethod
    def _native_and_normalized(record: Mapping[str, Any]) -> Dict[str, Any]:
        currency = str(record.get("currency") or "").strip() or None
        unit = str(record.get("uom") or record.get("unit") or "").strip() or None
        normalized_currency = normalized_unit = normalized_value = None
        currency_upper = (currency or "").upper()
        unit_upper = (unit or "").upper().replace(" ", "")
        if not currency and unit_upper.startswith("USD/"):
            currency, currency_upper = "USD", "USD"
            unit = unit.split("/", 1)[1]
            unit_upper = unit.upper()
        if currency_upper in {"USD", "US$", "$"} and unit_upper in {
            "MT", "TONNE", "TONNES", "USD/MT", "$/MT", "US$/MT",
        }:
            normalized_currency, normalized_unit = "USD", "mt"
            normalized_value = record.get("value")
        return {
            "value_native": record.get("value"),
            "currency_native": currency,
            "unit_native": unit,
            "value_normalized": normalized_value,
            "currency_normalized": normalized_currency,
            "unit_normalized": normalized_unit,
        }

    def _ensure_staging_series(
        self,
        record: Mapping[str, Any],
        candidate_by_key: Mapping[str, Mapping[str, Any]],
        entitlement_by_symbol: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        source = str(record.get("source") or "platts_excel")
        record_type = str(record.get("record_type") or "price_observation")
        explicit_series_id = str(record.get("series_id") or "").strip() or None
        row_symbol = str(record.get("symbol") or record.get("data_series") or "").strip() or None
        # FC rows commonly carry a contract code in ``symbol``.  That value is
        # point identity, not series identity; use the stable curve code (or
        # explicit manifest series id) so each official curve remains distinct.
        symbol = (
            str(record.get("curve_code") or explicit_series_id).strip() or None
            if record_type == "curve_point"
            else row_symbol
        )
        existing: Mapping[str, Any] = {}
        if explicit_series_id:
            existing = self.store.get_series(explicit_series_id) or {}
        if not existing and symbol and not (
            explicit_series_id and record_type == "curve_point"
        ):
            existing = self.store.get_series_by_symbol(source, symbol) or {}
        entitlement = (entitlement_by_symbol.get(row_symbol or "") or
                       entitlement_by_symbol.get(symbol or "") or
                       entitlement_by_symbol.get(str(record.get("canonical_key") or ""), {}))
        canonical_key = (record.get("canonical_key") or entitlement.get("canonical_key") or
                         entitlement.get("candidate_id"))
        candidate = candidate_by_key.get(str(canonical_key), {}) if canonical_key else {}
        if canonical_key and candidate:
            canonical_key = candidate["canonical_key"]
        series_id = str(
            explicit_series_id if record_type == "curve_point" and explicit_series_id
            else existing.get("id") or self._series_id(record)
        )
        native = self._native_and_normalized(record)
        return self.store.upsert_series({
            "id": series_id,
            "canonical_key": canonical_key or existing.get("canonical_key"),
            "symbol": symbol or existing.get("symbol"),
            "name": (record.get("description") or candidate.get("name") or
                     existing.get("name") or symbol or series_id),
            "product": record.get("product") or candidate.get("product") or existing.get("product"),
            "market": record.get("market") or candidate.get("market") or existing.get("market"),
            "region": record.get("region") or candidate.get("region") or existing.get("region"),
            "location": record.get("location") or candidate.get("location") or existing.get("location"),
            "basis": record.get("basis") or candidate.get("basis") or existing.get("basis"),
            "delivery_type": (record.get("delivery_type") or candidate.get("delivery_type") or
                              existing.get("delivery_type")),
            "quote_kind": "curve" if record.get("record_type") == "curve_point" else "assessment",
            "currency": (native["currency_native"] or candidate.get("expected_currency") or
                         existing.get("currency")),
            "unit": native["unit_native"] or candidate.get("expected_unit") or existing.get("unit"),
            "normalized_currency": native["currency_normalized"] or existing.get("normalized_currency"),
            "normalized_unit": native["unit_normalized"] or existing.get("normalized_unit"),
            "source": source,
            "source_dataset": record.get("dataset") or existing.get("source_dataset"),
            # A parsed market record is direct proof that this account can
            # retrieve the series.  Stale error rows for other data-series
            # variants must never hide successfully returned observations.
            "entitlement_state": "entitled",
            "entitlement_reason": "data_returned",
            "display_order": candidate.get("priority", 1000),
            "metadata": {"query_group": record.get("query_group"), "scope": record.get("scope")},
        })

    def import_platts_staging(self, payload_or_path: Union[Mapping[str, Any], str, Path]) -> Dict[str, Any]:
        """Idempotently import the atomic JSON written by the Excel workflow."""
        if isinstance(payload_or_path, Mapping):
            payload = dict(payload_or_path)
            input_path = None
        else:
            input_path = str(payload_or_path)
            with open(payload_or_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported Platts staging schema_version")
        records = payload.get("records") or []
        entitlements = payload.get("entitlement_results") or []
        if not isinstance(records, list) or not isinstance(entitlements, list):
            raise ValueError("staging records and entitlement_results must be arrays")

        run = self.store.start_run(
            "platts_excel", str(payload.get("scope") or "all"),
            metadata={
                "generated_at": payload.get("generated_at"),
                "workbook": payload.get("workbook"),
                "workbook_mtime": payload.get("workbook_mtime"),
                "input_path": input_path,
                "staging_status": payload.get("status"),
                "purpose": str(payload.get("purpose") or "daily").lower(),
            },
        )
        counts = {"rows_seen": len(records), "rows_inserted": 0,
                  "rows_updated": 0, "rows_skipped": 0}
        input_quality = {
            "rows_unchanged": 0,
            "duplicate_input_rows": 0,
            "duplicate_curve_leg_rows": 0,
        }
        errors = [str(error) for error in (payload.get("errors") or [])]
        candidates = self.store.candidates()
        candidate_by_key = {}
        for candidate in candidates:
            candidate_by_key[candidate["candidate_id"]] = candidate
            candidate_by_key[candidate["canonical_key"]] = candidate
        entitlement_by_symbol: Dict[str, Mapping[str, Any]] = {}
        candidate_entitlements: Dict[str, Mapping[str, Any]] = {}

        def prefer(current: Mapping[str, Any] | None,
                   incoming: Mapping[str, Any]) -> Mapping[str, Any]:
            ranks = {"entitled": 4, "unentitled": 3, "pending_review": 2,
                     "error": 1, "retired": 0}
            if current is None:
                return incoming
            current_state = self._entitlement_state(
                current.get("entitlement_state") or current.get("status") or current.get("state")
            )
            incoming_state = self._entitlement_state(
                incoming.get("entitlement_state") or incoming.get("status") or incoming.get("state")
            )
            current_score = (ranks[current_state], int(current.get("record_count") or 0))
            incoming_score = (ranks[incoming_state], int(incoming.get("record_count") or 0))
            return incoming if incoming_score >= current_score else current

        for entitlement in entitlements:
            if not isinstance(entitlement, Mapping):
                errors.append("invalid entitlement result")
                continue
            symbol = str(entitlement.get("symbol") or entitlement.get("data_series") or "").strip()
            if symbol:
                entitlement_by_symbol[symbol] = prefer(entitlement_by_symbol.get(symbol), entitlement)
            if entitlement.get("canonical_key"):
                key = str(entitlement["canonical_key"])
                entitlement_by_symbol[key] = prefer(entitlement_by_symbol.get(key), entitlement)
            if entitlement.get("candidate_id"):
                key = str(entitlement["candidate_id"])
                entitlement_by_symbol[key] = prefer(entitlement_by_symbol.get(key), entitlement)
            candidate_key = entitlement.get("candidate_id")
            if candidate_key not in candidate_by_key:
                candidate_key = entitlement.get("canonical_key")
            if candidate_key in candidate_by_key:
                catalog_id = str(candidate_by_key[candidate_key]["candidate_id"])
                candidate_entitlements[catalog_id] = prefer(
                    candidate_entitlements.get(catalog_id), entitlement
                )

        for candidate_key, entitlement in candidate_entitlements.items():
            state = self._entitlement_state(
                entitlement.get("entitlement_state") or entitlement.get("status") or entitlement.get("state")
            )
            mapped_series_id = entitlement.get("series_id")
            symbol = str(entitlement.get("symbol") or entitlement.get("data_series") or "").strip()
            if candidate_key in candidate_by_key:
                candidate = candidate_by_key[candidate_key]
                data_series = str(entitlement.get("data_series") or "").strip().lower()
                # MD entitlement is data-series specific.  A failed or denied
                # History-Symbol probe must not erase proven Current-Symbol
                # access in the candidate-level compatibility field.
                if (data_series == "history-symbol"
                        and candidate.get("discovery_status") == "entitled"
                        and state != "entitled"):
                    continue
                try:
                    self.store.update_candidate_entitlement(
                        str(candidate_key), state, str(mapped_series_id) if mapped_series_id else None,
                        entitlement.get("error") or entitlement.get("reason_code"),
                        payload.get("generated_at"),
                        symbol or None, entitlement.get("curve_code"), dict(entitlement),
                    )
                except (KeyError, ValueError) as exc:
                    errors.append(str(exc))

        seen_record_keys: set[str] = set()
        for index, record in enumerate(records):
            try:
                if not isinstance(record, Mapping):
                    raise ValueError("record is not an object")
                logical_key = self._staging_record_identity(record)
                if logical_key:
                    if logical_key in seen_record_keys:
                        input_quality["duplicate_input_rows"] += 1
                        if str(record.get("canonical_key") or "") in DERIVED_PROMPT_COMPONENT_KEYS:
                            input_quality["duplicate_curve_leg_rows"] += 1
                    else:
                        seen_record_keys.add(logical_key)
                record_type = str(record.get("record_type") or "price_observation")
                if record_type in {"price_observation", "correction"}:
                    series = self._ensure_staging_series(
                        record, candidate_by_key, entitlement_by_symbol
                    )
                    native = self._native_and_normalized(record)
                    output = self.store.upsert_observation({
                        "series_id": series["id"],
                        "observation_date": record.get("assess_date"),
                        **native,
                        "bate": record.get("bate") or "",
                        "publication_time": record.get("mod_date"),
                        "fetched_at": payload.get("generated_at"),
                        "source_ref": record.get("source_ref") or record.get("data_series"),
                        "revision_reason": "Platts correction" if record_type == "correction" else None,
                        "ingestion_run_id": run["id"],
                        "metadata": {key: value for key, value in record.items()
                                     if key not in {"value"}},
                    })
                elif record_type == "curve_point":
                    series = self._ensure_staging_series(
                        record, candidate_by_key, entitlement_by_symbol
                    )
                    native = self._native_and_normalized(record)
                    contract = str(record.get("contract_label") or
                                   record.get("contract_date") or
                                   record.get("contract_code") or
                                   record.get("symbol") or "").strip()
                    if not contract:
                        raise ValueError("curve point has no contract label/date")
                    output = self.store.upsert_curve_point({
                        "series_id": series["id"],
                        "as_of_date": record.get("assess_date"),
                        "contract_month": contract,
                        "delivery_start": record.get("contract_date"),
                        **native,
                        "fetched_at": payload.get("generated_at"),
                        "source_ref": record.get("source_ref") or record.get("curve_code"),
                        "ingestion_run_id": run["id"],
                        "metadata": {key: value for key, value in record.items()
                                     if key not in {"value"}},
                    })
                else:
                    dataset = "platts_ewindow" if record_type.startswith("ewindow") else str(
                        record.get("dataset") or "platts_fundamentals"
                    )
                    row_key = self._dataset_row_key(record, dataset)
                    output = self.store.upsert_dataset_row({
                        "dataset": dataset,
                        "row_key": row_key,
                        "as_of_date": record.get("assess_date"),
                        "payload": dict(record),
                        "source": record.get("source") or "platts_excel",
                        "fetched_at": payload.get("generated_at"),
                        "ingestion_run_id": run["id"],
                    })
                action = output["action"]
                if action == "inserted":
                    counts["rows_inserted"] += 1
                elif action == "updated":
                    counts["rows_updated"] += 1
                else:
                    input_quality["rows_unchanged"] += 1
                    counts["rows_skipped"] += 1
            except Exception as exc:  # isolate one bad/unentitled formula from the batch
                counts["rows_skipped"] += 1
                errors.append(f"record {index}: {exc}")

        # Discovery-only runs legitimately contain no market rows but still
        # update the entitlement matrix and schema catalog.
        discovery_rows = 0
        for block in payload.get("discovery") or []:
            if not isinstance(block, Mapping):
                continue
            sheet = str(block.get("sheet") or "unknown")
            for row_index, row_values in enumerate(block.get("rows") or []):
                try:
                    row_payload = {"sheet": sheet, "values": row_values,
                                   "error_code": block.get("error_code")}
                    seed = json.dumps(json_safe(row_payload), sort_keys=True, ensure_ascii=True)
                    self.store.upsert_dataset_row({
                        "dataset": "platts_discovery",
                        "row_key": f"{sheet}:{row_index}:{hashlib.sha1(seed.encode('utf-8')).hexdigest()}",
                        "as_of_date": str(payload.get("generated_at") or utc_now())[:10],
                        "payload": row_payload,
                        "source": "platts_excel",
                        "fetched_at": payload.get("generated_at"),
                        "ingestion_run_id": run["id"],
                    })
                    discovery_rows += 1
                except Exception as exc:
                    errors.append(f"discovery {sheet} row {row_index}: {exc}")

        staging_ok = str(payload.get("status") or "success").lower() in {
            "success", "ok", "complete", "completed", "discovery_only",
            "partial_success", "empty",
        }
        if errors or not staging_ok:
            final_status = "partial" if counts["rows_inserted"] + counts["rows_updated"] > 0 else "failed"
        else:
            final_status = "success"
        finished = self.store.finish_run(
            run["id"], final_status, **counts,
            error="; ".join(errors[:20]) if errors else None,
            metadata={
                "rows_unchanged": input_quality["rows_unchanged"],
                "duplicate_input_rows": input_quality["duplicate_input_rows"],
                "duplicate_curve_leg_rows": input_quality["duplicate_curve_leg_rows"],
            },
        )
        return json_safe({
            "success": final_status == "success",
            "status": final_status,
            "run": finished,
            "counts": counts,
            "input_quality": input_quality,
            "discovery_rows": discovery_rows,
            "errors": errors,
        })

    @staticmethod
    def _staging_record_identity(record: Mapping[str, Any]) -> Optional[str]:
        """Return the logical import key used to audit repeated workbook rows."""
        record_type = str(record.get("record_type") or "price_observation").lower()
        identity = (
            record.get("series_id") or record.get("canonical_key") or
            record.get("candidate_id") or record.get("symbol") or
            record.get("data_series")
        )
        assess_date = record.get("assess_date") or record.get("observation_date")
        if not identity or not assess_date:
            return None
        if record_type in {"price_observation", "correction"}:
            suffix = str(record.get("bate") or "")
            logical_type = "observation"
        elif record_type == "curve_point":
            suffix = str(
                record.get("contract_label") or record.get("contract_date") or
                record.get("contract_code") or record.get("symbol") or ""
            )
            logical_type = "curve_point"
        else:
            suffix = str(record.get("row_key") or record.get("trade_id") or "")
            logical_type = record_type
        seed = "|".join((logical_type, str(identity), str(assess_date), suffix))
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()

    @staticmethod
    def _dataset_row_key(record: Mapping[str, Any], dataset: str) -> str:
        """Return a stable identity for supplemental rows.

        eWindow payloads are mutable snapshots of an order. Hashing the full
        record makes every price/status update look like a new order, so MOC
        rows use the provider's order id when available. Older or partial
        responses without an order id fall back to immutable order dimensions
        and deliberately exclude value, volume, status, and update time.
        """
        explicit = str(record.get("row_key") or "").strip()
        if explicit:
            return explicit

        if dataset == "platts_ewindow":
            order_id = str(record.get("order_id") or "").strip()
            if order_id:
                return order_id
            identity_fields = (
                "record_type", "series_id", "canonical_key", "candidate_id",
                "scope", "data_series", "order_time", "symbol", "market",
                "product", "hub", "strip", "source_ref", "description",
            )
            identity = {
                key: json_safe(record.get(key))
                for key in identity_fields
                if record.get(key) not in (None, "")
            }
            seed = json.dumps(identity, sort_keys=True, ensure_ascii=True)
            return f"moc:{hashlib.sha1(seed.encode('utf-8')).hexdigest()}"

        row_seed = json.dumps(json_safe(dict(record)), sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(row_seed.encode("utf-8")).hexdigest()
