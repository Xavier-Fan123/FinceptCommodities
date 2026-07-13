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
        return json_safe({
            "series": series,
            "observations": records,
            "statistics": numeric_statistics(records),
            "seasonality": seasonality(records),
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
        effective_as_of = normalize_date(as_of) if as_of else max(
            (curve["as_of_date"] for curve in grouped.values()), default=None
        )
        return json_safe({"as_of": effective_as_of, "curves": list(grouped.values())})

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
        return self.store.news(merged)

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
            return json_safe({"dataset": dataset, "columns": columns, "rows": page,
                              "total": len(rows), "limit": limit, "offset": offset})
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
        status["runtime"] = runtime
        status["news_api_configured"] = news_configured
        status["entitlement_matrix"] = self.store.candidates()
        return json_safe(status)

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
        symbol = str(record.get("symbol") or record.get("data_series") or "").strip() or None
        existing: Mapping[str, Any] = {}
        if record.get("series_id"):
            existing = self.store.get_series(str(record["series_id"])) or {}
        if not existing and symbol:
            existing = self.store.get_series_by_symbol(source, symbol) or {}
        entitlement = (entitlement_by_symbol.get(symbol or "") or
                       entitlement_by_symbol.get(str(record.get("canonical_key") or ""), {}))
        canonical_key = (record.get("canonical_key") or entitlement.get("canonical_key") or
                         entitlement.get("candidate_id"))
        candidate = candidate_by_key.get(str(canonical_key), {}) if canonical_key else {}
        if canonical_key and candidate:
            canonical_key = candidate["canonical_key"]
        series_id = str(existing.get("id") or self._series_id(record))
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
                series = self._ensure_staging_series(
                    record, candidate_by_key, entitlement_by_symbol
                )
                record_type = str(record.get("record_type") or "price_observation")
                native = self._native_and_normalized(record)
                if record_type in {"price_observation", "correction"}:
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
                    contract = str(record.get("contract_label") or
                                   record.get("contract_date") or "").strip()
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
                    row_seed = json.dumps(json_safe(dict(record)), sort_keys=True, ensure_ascii=True)
                    row_key = str(record.get("row_key") or
                                  hashlib.sha1(row_seed.encode("utf-8")).hexdigest())
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
