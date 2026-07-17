"""Application facade for the LPG HTTP routes and import workflows."""

import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .analytics import calculate_spread, freshness, numeric_statistics, seasonality
from .catalog import DEFAULT_SPREADS
from .models import json_safe, normalize_date, utc_now
from .store import LpgStore


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
        curves = [*grouped.values(), *derived_curves]
        effective_as_of = normalize_date(as_of) if as_of else max(
            (curve["as_of_date"] for curve in curves), default=None
        )
        official_count = len(grouped)
        derived_count = len(derived_curves)
        if official_count:
            status, reason = "ready", None
        elif derived_count:
            status = "derived_only"
            reason = "official_curve_points_missing_using_derived_prompt_structure"
        else:
            status = "empty"
            reason = "no_official_curve_points_or_complete_hm1_hm2_hm3_assessments"
        dataset_status = {
            "status": status,
            "reason": reason,
            "rows": sum(len(curve["points"]) for curve in curves),
            "dates": len({curve["as_of_date"] for curve in curves}),
            "series": len(curves),
        }
        runtime = self._specialized_runtime_statuses().get("curves")
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
        })

    def _derived_prompt_curves(self, as_of: Optional[str] = None,
                               series_id: Optional[str] = None) -> tuple[list[Dict[str, Any]],
                                                                           list[Dict[str, Any]]]:
        """Compose clearly-labelled prompt structures from current HM assessments.

        The components are licensed assessment series already present in the
        catalog.  They are only combined when all three tenors are entitled,
        active, share one current assessment date, currency, and unit.  The
        result must never be represented as an official Platts FC curve.
        """
        curves, diagnostics = [], []
        for spec in DERIVED_PROMPT_CURVES:
            components, missing = [], []
            for tenor, canonical_key in spec["components"]:
                series = self.store.get_series_by_canonical_key(canonical_key)
                if (series is None or series.get("entitlement_state") != "entitled" or
                        not series.get("active")):
                    missing.append(canonical_key)
                    continue
                records = self.store.history(series["id"], end=as_of, limit=1)
                if not records:
                    missing.append(canonical_key)
                    continue
                components.append({"tenor": tenor, "series": series, "record": records[0]})

            component_ids = [component["series"]["id"] for component in components]
            if series_id and series_id not in component_ids and series_id != spec["canonical_key"]:
                continue
            diagnostic: Dict[str, Any] = {
                "canonical_key": spec["canonical_key"],
                "component_series_ids": component_ids,
                "missing_components": missing,
            }
            if missing or len(components) != len(spec["components"]):
                diagnostic.update({"status": "incomplete", "reason": "missing_or_unentitled_component"})
                diagnostics.append(diagnostic)
                continue

            dates = {component["record"]["date"] for component in components}
            currencies = {component["record"].get("currency") for component in components}
            units = {component["record"].get("unit") for component in components}
            if len(dates) != 1:
                diagnostic.update({
                    "status": "incomplete", "reason": "component_dates_do_not_match",
                    "component_dates": sorted(dates),
                })
                diagnostics.append(diagnostic)
                continue
            if len(currencies) != 1 or len(units) != 1:
                diagnostic.update({
                    "status": "incomplete", "reason": "component_units_do_not_match",
                    "component_currencies": sorted(str(value) for value in currencies),
                    "component_units": sorted(str(value) for value in units),
                })
                diagnostics.append(diagnostic)
                continue

            curve_date = next(iter(dates))
            primary_series = components[0]["series"]
            points = []
            for component in components:
                record, source_series = component["record"], component["series"]
                points.append({
                    "id": f"derived:{record['id']}",
                    "as_of_date": curve_date,
                    "contract_month": component["tenor"],
                    "tenor": component["tenor"],
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
                    "curve_kind": "derived_prompt_structure",
                    "source_series_id": source_series["id"],
                    "source_canonical_key": source_series.get("canonical_key"),
                })
            curves.append({
                # Use an entitled component as the access-control anchor so
                # the existing UI catalog filter can validate and display it.
                "series_id": primary_series["id"],
                "canonical_key": spec["canonical_key"],
                "name": spec["name"],
                "source": "derived_from_current_assessments",
                "entitlement_state": "entitled",
                "as_of_date": curve_date,
                "currency": next(iter(currencies)),
                "unit": next(iter(units)),
                "curve_kind": "derived_prompt_structure",
                "derived": True,
                "is_official": False,
                "component_series_ids": component_ids,
                "methodology": (
                    "Composed from entitled HM1/HM2/HM3 current assessments; "
                    "not an official Platts FC curve."
                ),
                "points": points,
            })
            diagnostic.update({"status": "ready", "reason": None, "as_of_date": curve_date})
            diagnostics.append(diagnostic)
        return curves, diagnostics

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
        try:
            from .platts_excel import STAGING_DIR
            runtime_path = STAGING_DIR / "status.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            runtime = {}
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
        status["entitlement_matrix"] = self.store.candidates()
        specialized = self._specialized_runtime_statuses()
        status["specialized_runtime"] = specialized
        status["datasets"] = self._dataset_health(status.get("datasets"), specialized)
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
            str(curve["as_of_date"]) for curve in all_curves if curve.get("as_of_date")
        })
        official = [curve for curve in all_curves if curve.get("is_official")]
        derived = [curve for curve in all_curves if curve.get("derived")]
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
            "status": curves["status"],
            "reason": curves["reason"],
        }
        for name in ("history", "curves", "moc"):
            runtime = dict(specialized or {}).get(name)
            if name in datasets and isinstance(runtime, Mapping):
                datasets[name] = self._with_runtime_status(datasets[name], runtime)
        return datasets

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
        state = str(runtime.get("state") or runtime.get("status") or "unknown").lower()
        reason = cls._runtime_reason(runtime)
        output.update({
            "refresh_state": state,
            "refresh_reason": reason,
            "runtime_status": dict(runtime),
        })
        if state in {"error", "failed", "blocked", "refresh_failed"}:
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
            },
        )
        counts = {"rows_seen": len(records), "rows_inserted": 0,
                  "rows_updated": 0, "rows_skipped": 0}
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

        for index, record in enumerate(records):
            try:
                if not isinstance(record, Mapping):
                    raise ValueError("record is not an object")
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
            "partial_success",
        }
        if errors or not staging_ok:
            final_status = "partial" if counts["rows_inserted"] + counts["rows_updated"] > 0 else "failed"
        else:
            final_status = "success"
        finished = self.store.finish_run(
            run["id"], final_status, **counts,
            error="; ".join(errors[:20]) if errors else None,
        )
        return json_safe({
            "success": final_status == "success",
            "status": final_status,
            "run": finished,
            "counts": counts,
            "discovery_rows": discovery_rows,
            "errors": errors,
        })

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
