"""Operational LPG refresh workflows.

The paid-data boundary is intentionally narrow: Python builds/parses private
workbooks, while the signed-in official Excel Add-in performs every licensed
query.  Credentials are never passed to this module or written to disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .models import json_safe
from .news_sources import (
    PlattsNewsClient,
    dedupe_articles,
    public_lpg_news,
    to_news_input,
)
from .platts_excel import (
    build_backfill_workbooks,
    build_curve_workbooks,
    build_moc_workbooks,
    build_scope_workbooks,
    combine_payloads,
    parse_workbook,
    write_runtime_status,
    write_staging,
)
from .service import LpgService


ROOT = Path(__file__).resolve().parents[1]
REFRESH_SCRIPT = ROOT / "scripts" / "Refresh-LpgPlattsWorkbook.ps1"
OFFICIAL_FC_REFRESH_ENABLED = str(
    os.getenv("LPG_PLATTS_FC_REFRESH_ENABLED") or ""
).strip().lower() in {"1", "true", "yes", "on"}


class LpgRefreshWorkflow:
    def __init__(
        self,
        service: Optional[LpgService] = None,
        process_runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        self.service = service or LpgService()
        self.process_runner = process_runner or subprocess.run

    @staticmethod
    def _scopes(scope: str) -> tuple[str, ...]:
        scope = str(scope or "all").lower()
        if scope not in {"asia", "overnight", "all"}:
            raise ValueError(f"invalid market refresh scope: {scope}")
        return ("asia", "overnight") if scope == "all" else (scope,)

    def _excel_refresh(
        self,
        workbook: Path,
        timeout_seconds: int,
        *,
        isolated_instance: bool = False,
    ) -> Dict[str, Any]:
        if os.name != "nt":
            return {"state": "failed", "returncode": 3,
                    "reason": "Excel refresh is only available on Windows"}
        if not REFRESH_SCRIPT.exists():
            return {"state": "failed", "returncode": 3,
                    "reason": f"refresh script is missing: {REFRESH_SCRIPT}"}
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
        command = [
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(REFRESH_SCRIPT), "-Workbook", str(workbook),
            "-TimeoutSec", str(max(60, int(timeout_seconds))),
        ]
        if isolated_instance:
            command.append("-IsolatedInstance")
        workbook_mtime_before = workbook.stat().st_mtime_ns if workbook.exists() else None
        try:
            completed = self.process_runner(
                command, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=max(180, timeout_seconds + 120),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            workbook_mtime_after = workbook.stat().st_mtime_ns if workbook.exists() else None
            return {
                "state": "failed",
                "returncode": 3,
                "reason": "excel_refresh_timeout",
                "workbook_updated": (
                    workbook_mtime_before is not None
                    and workbook_mtime_after is not None
                    and workbook_mtime_after > workbook_mtime_before
                ),
            }
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr)
                           if part and part.strip())[-4000:]
        workbook_mtime_after = workbook.stat().st_mtime_ns if workbook.exists() else None
        states = {0: "succeeded", 2: "failed", 3: "failed", 4: "deferred"}
        reasons = {0: None, 2: "excel_session_or_formula_unresolved",
                   3: "excel_automation_failed", 4: "excel_already_open"}
        return {
            "state": states.get(completed.returncode, "failed"),
            "returncode": completed.returncode,
            "reason": reasons.get(completed.returncode, "unexpected_excel_exit"),
            "output": output,
            "workbook_updated": (
                workbook_mtime_before is not None
                and workbook_mtime_after is not None
                and workbook_mtime_after > workbook_mtime_before
            ),
        }

    def _curve_pipeline_audit(self) -> Dict[str, Any]:
        """Audit the durable daily HM legs after import without requesting FC data."""
        try:
            payload = self.service.curves()
            quality = dict(payload.get("quality") or {})
            return json_safe({
                "state": quality.get("status") or "unknown",
                "pipeline": "daily_current_refresh",
                "official_fc_requested": False,
                "latest_common_date": quality.get("latest_common_date"),
                "expected_legs": quality.get("expected_legs", 0),
                "available_legs": quality.get("available_legs", 0),
                "generated_snapshot_count": quality.get("generated_snapshot_count", 0),
                "missing_latest_legs": quality.get("missing_latest_legs") or [],
                "duplicate_record_count": quality.get("duplicate_total_count", 0),
                "anomaly_count": quality.get("anomaly_count", 0),
                "reason_codes": quality.get("reason_codes") or [],
                "quality": quality,
            })
        except Exception as exc:
            return {
                "state": "error",
                "pipeline": "daily_current_refresh",
                "official_fc_requested": False,
                "reason_codes": ["curve_pipeline_audit_failed"],
                "error": str(exc)[:1000],
            }

    def refresh_market(self, scope: str = "all", timeout_seconds: int = 240) -> Dict[str, Any]:
        requested = str(scope or "all").lower()
        concrete = self._scopes(requested)
        workbooks = build_scope_workbooks(scope=requested)
        results, payloads = [], []
        for item_scope, workbook in zip(concrete, workbooks):
            result = self._excel_refresh(workbook, timeout_seconds)
            result.update({"scope": item_scope, "workbook": str(workbook)})
            if result["state"] == "succeeded":
                try:
                    payload = parse_workbook(workbook)
                    payloads.append(payload)
                    result["records"] = len(payload.get("records") or [])
                    result["entitlements"] = len(payload.get("entitlement_results") or [])
                except Exception as exc:  # parsed boundary, never overwrite last-good staging
                    result.update({"state": "failed", "reason": "workbook_parse_failed",
                                   "error": str(exc)[:1000]})
            else:
                write_runtime_status(
                    scope=item_scope,
                    state=result["state"],
                    reason_code=str(result.get("reason") or "excel_refresh_failed"),
                )
            results.append(result)

        if not payloads:
            state = "deferred" if results and all(row["state"] == "deferred" for row in results) else "failed"
            return json_safe({
                "state": state,
                "scope": requested,
                "results": results,
                "error": "No workbook produced a valid saved result",
            })

        payload = combine_payloads(payloads, requested_scope=requested)
        staged = write_staging(payload)
        if staged is None:
            return json_safe({"state": "failed", "scope": requested, "results": results,
                              "error": "Refresh returned neither data nor entitlement results"})
        imported = self.service.import_platts_staging(staged)
        curve_pipeline = (
            self._curve_pipeline_audit() if requested in {"asia", "all"} else None
        )
        failed_scopes = [row for row in results if row["state"] != "succeeded"]
        record_count = sum(int(row.get("records") or 0) for row in results)
        if failed_scopes or imported.get("status") == "partial" or record_count == 0:
            state = "partial"
        elif imported.get("status") == "failed":
            state = "failed"
        elif curve_pipeline and curve_pipeline.get("state") == "error":
            state = "partial"
        else:
            state = "succeeded"
        warnings = []
        if curve_pipeline and curve_pipeline.get("state") in {"warning", "incomplete", "error"}:
            warnings.extend(curve_pipeline.get("reason_codes") or [])
        return json_safe({
            "state": state,
            "scope": requested,
            "staging": str(staged),
            "results": results,
            "import": imported,
            "curve_pipeline": curve_pipeline,
            "warnings": list(dict.fromkeys(warnings)),
            "message": "entitlement_discovery_only" if record_count == 0 else None,
        })

    @staticmethod
    def _workbook_scope(workbook: Path) -> str:
        name = workbook.name.lower()
        if "_overnight" in name:
            return "overnight"
        if "_asia" in name:
            return "asia"
        raise ValueError(f"cannot determine LPG scope from workbook name: {workbook.name}")

    @staticmethod
    def _aggregate_states(results: Sequence[Mapping[str, Any]]) -> str:
        states = [str(item.get("state") or "failed") for item in results]
        if states and all(state == "succeeded" for state in states):
            return "succeeded"
        if any(state in {"succeeded", "partial"} for state in states):
            return "partial"
        if states and all(state == "deferred" for state in states):
            return "deferred"
        return "failed"

    def _refresh_specialized_workbooks(
        self,
        workbooks: Sequence[Path],
        *,
        purpose: str,
        timeout_seconds: int,
        year: int | None = None,
    ) -> Dict[str, Any]:
        """Refresh, parse, stage, and import independent licensed-data batches."""
        results: list[dict[str, Any]] = []
        for workbook in workbooks:
            item_scope = self._workbook_scope(workbook)
            result = self._excel_refresh(
                workbook,
                timeout_seconds,
                isolated_instance=True,
            )
            payload: dict[str, Any] | None = None
            if result["state"] != "succeeded" and result.get("workbook_updated"):
                try:
                    recovered = parse_workbook(workbook)
                    recovered_status = str(recovered.get("status") or "").lower()
                    valid_empty = (
                        recovered_status in {
                            "empty", "discovery_only", "success", "partial_success",
                        }
                        and bool(recovered.get("entitlement_results") or recovered.get("discovery"))
                        and not recovered.get("errors")
                    )
                    if recovered.get("records") or valid_empty:
                        payload = recovered
                        result.update({
                            "state": "succeeded",
                            "reason": "saved_workbook_recovered_after_excel_exit",
                            "recovered_saved_workbook": True,
                        })
                except Exception:
                    pass
            result.update({
                "scope": item_scope,
                "workbook": str(workbook),
                "purpose": purpose,
                "year": year,
            })
            if result["state"] != "succeeded":
                write_runtime_status(
                    scope=item_scope,
                    state=result["state"],
                    reason_code=str(result.get("reason") or "excel_refresh_failed"),
                    purpose=purpose,
                )
                results.append(result)
                continue

            try:
                if payload is None:
                    payload = parse_workbook(workbook)
                result["records"] = len(payload.get("records") or [])
                result["entitlements"] = len(payload.get("entitlement_results") or [])
                staged = write_staging(
                    payload,
                    purpose=purpose,
                    year=year,
                    batch_id=workbook.stem,
                )
                if staged is None:
                    result.update({
                        "state": "failed",
                        "reason": "empty_workbook_payload",
                    })
                else:
                    imported = self.service.import_platts_staging(staged)
                    result["staging"] = str(staged)
                    result["import"] = imported
                    if imported.get("status") == "failed":
                        result.update({"state": "failed", "reason": "staging_import_failed"})
                    elif imported.get("status") == "partial" or not result["records"]:
                        result["state"] = "partial"
                        result["reason"] = "no_data_returned" if not result["records"] else None
            except Exception as exc:
                # One failed symbol/curve is isolated; successful batches have
                # already been durably imported and can be skipped by upsert on retry.
                result.update({
                    "state": "failed",
                    "reason": "workbook_pipeline_failed",
                    "error": str(exc)[:1000],
                })
            results.append(result)

        return json_safe({
            "state": self._aggregate_states(results),
            "purpose": purpose,
            "year": year,
            "results": results,
            "workbook_count": len(workbooks),
            "record_count": sum(int(item.get("records") or 0) for item in results),
            "message": "no_workbooks_configured" if not workbooks else None,
        })

    def refresh_history(
        self,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        scope: str = "all",
        symbols: Sequence[str] | None = None,
        batch_size: int = 1,
        timeout_seconds: int = 240,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Run retryable History-Symbol backfill batches through SQLite import."""
        first_year = start_year or datetime.now(timezone.utc).year
        last_year = end_year or first_year
        if first_year > last_year:
            raise ValueError("start_year must be <= end_year")
        yearly: list[dict[str, Any]] = []
        for year in range(first_year, last_year + 1):
            workbooks = build_backfill_workbooks(
                start_year=year,
                end_year=year,
                scope=scope,
                symbols=symbols,
                batch_size=batch_size,
                force=force,
            )
            yearly.append(self._refresh_specialized_workbooks(
                workbooks,
                purpose="backfill",
                timeout_seconds=timeout_seconds,
                year=year,
            ))
        results = [item for run in yearly for item in (run.get("results") or [])]
        return json_safe({
            "state": self._aggregate_states(results),
            "purpose": "history",
            "scope": scope,
            "start_year": first_year,
            "end_year": last_year,
            "symbols": list(symbols or []),
            "batch_size": batch_size,
            "record_count": sum(int(item.get("records") or 0) for item in results),
            "results": results,
        })

    def refresh_curves(
        self,
        *,
        scope: str = "all",
        curve_ids: Sequence[str] | None = None,
        batch_size: int = 1,
        timeout_seconds: int = 240,
        force: bool = False,
    ) -> Dict[str, Any]:
        if not OFFICIAL_FC_REFRESH_ENABLED:
            return json_safe({
                "state": "blocked",
                "purpose": "curve",
                "scope": scope,
                "curve_ids": list(curve_ids or []),
                "reason": "official_fc_curve_entitlement_not_available",
                "official_refresh_supported": False,
                "alternative": "daily_derived_curve_from_current_assessments",
            })
        workbooks = build_curve_workbooks(
            scope=scope,
            curve_ids=curve_ids,
            batch_size=batch_size,
            force=force,
        )
        output = self._refresh_specialized_workbooks(
            workbooks, purpose="curve", timeout_seconds=timeout_seconds,
        )
        output.update({"scope": scope, "curve_ids": list(curve_ids or [])})
        return json_safe(output)

    def refresh_moc(
        self,
        *,
        scope: str = "all",
        timeout_seconds: int = 240,
        force: bool = False,
    ) -> Dict[str, Any]:
        workbooks = build_moc_workbooks(scope=scope, force=force)
        output = self._refresh_specialized_workbooks(
            workbooks, purpose="moc", timeout_seconds=timeout_seconds,
        )
        output.update({
            "scope": scope,
            "dataset": "platts_ewindow",
            "fundamentals": {
                "state": "unavailable",
                "reason": "no_reliable_licensed_source_configured",
            },
        })
        return json_safe(output)

    def refresh_news(self, limit: int = 100) -> Dict[str, Any]:
        run = self.service.start_run("lpg_news", "news", metadata={"limit": limit})
        errors, warnings, articles, source_health = [], [], [], []
        official = PlattsNewsClient()
        if not official.configured:
            warnings.append("platts_news_api_not_configured")

        def fetch_official() -> Dict[str, Any]:
            started = time.perf_counter()
            attempted = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            try:
                payload = official.fetch(
                    page_size=min(max(limit, 20), 500),
                    max_pages=max(1, min(5, (max(limit, 20) + 99) // 100)),
                )
                rows = payload.get("articles") or []
                latest = max((row.get("published_at") for row in rows), default=None)
                health = {
                    "source_id": "platts_news_api",
                    "source_name": "S&P Global Commodity Insights",
                    "kind": "licensed_api", "status": "healthy" if rows else "empty",
                    "last_attempt_at": attempted, "last_success_at": attempted,
                    "latest_published_at": latest, "article_count": len(rows),
                    "relevant_count": sum(bool(row.get("is_relevant")) for row in rows),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "error": payload.get("error"),
                    "metadata": {"content_boundary": "separate_machine_readable_entitlement"},
                }
                return {"kind": "official", "payload": payload, "health": health}
            except Exception as exc:
                return {"kind": "official", "error": str(exc), "health": {
                    "source_id": "platts_news_api",
                    "source_name": "S&P Global Commodity Insights",
                    "kind": "licensed_api", "status": "error",
                    "last_attempt_at": attempted, "last_success_at": None,
                    "article_count": 0, "relevant_count": 0,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                    "metadata": {"content_boundary": "separate_machine_readable_entitlement"},
                }}

        def fetch_public() -> Dict[str, Any]:
            try:
                return {"kind": "public", "payload": public_lpg_news(limit=max(limit * 2, 160))}
            except Exception as exc:
                return {"kind": "public", "error": str(exc)}

        jobs = [fetch_public]
        if official.configured:
            jobs.append(fetch_official)
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="lpg-news-provider") as executor:
            futures = [executor.submit(job) for job in jobs]
            for future in as_completed(futures):
                result = future.result()
                if result.get("health"):
                    source_health.append(result["health"])
                if result.get("error"):
                    errors.append(f"{result['kind']}_news: {result['error']}")
                    continue
                payload = result.get("payload") or {}
                articles.extend(payload.get("articles") or [])
                if result["kind"] == "public":
                    source_health.extend(payload.get("sources") or [])
                if payload.get("error"):
                    errors.append(f"{result['kind']}_news: {payload['error']}")

        for health in source_health:
            try:
                self.service.upsert_news_source_health(health)
            except Exception as exc:
                errors.append(f"news_source_health: {exc}")

        counts = {"rows_seen": len(articles), "rows_inserted": 0,
                  "rows_updated": 0, "rows_skipped": 0}
        ranked = dedupe_articles(article for article in articles if article.get("is_relevant"))
        for article in ranked:
            try:
                record = to_news_input(article)
                record["ingestion_run_id"] = run["id"]
                saved = self.service.upsert_news(record)
                if saved.get("action") == "inserted":
                    counts["rows_inserted"] += 1
                else:
                    counts["rows_updated"] += 1
            except Exception as exc:
                counts["rows_skipped"] += 1
                errors.append(f"news_record: {exc}")
        try:
            intelligence = self.service.refresh_intelligence()
            if intelligence.get("status") in {"partial", "failed"}:
                warnings.append("situation_intelligence_partial")
        except Exception as exc:  # noqa: BLE001 - news remains usable if derived intelligence fails
            intelligence = {"status": "failed", "errors": [str(exc)]}
            warnings.append("situation_intelligence_failed")
        wrote = counts["rows_inserted"] + counts["rows_updated"]
        provider_available = any(
            health.get("status") in {"healthy", "empty"} for health in source_health
        )
        if wrote and errors:
            status, state = "partial", "partial"
        elif wrote:
            status, state = "success", "succeeded"
        elif provider_available and errors:
            status, state = "partial", "partial"
        elif provider_available:
            # A quiet LPG news window is a valid empty snapshot, not a failed
            # ingestion. Source health carries the distinction to the UI.
            status, state = "success", "succeeded"
        else:
            status, state = "failed", "failed"
        finished = self.service.finish_run(
            run["id"], status, **counts,
            error="; ".join(errors[:20]) if errors else None,
        )
        return json_safe({"state": state, "run": finished, "counts": counts,
                          "errors": errors, "warnings": warnings,
                          "intelligence": intelligence,
                          "source_health": source_health,
                          "clusters": len({row.get("cluster_key") for row in ranked
                                           if row.get("cluster_key")}),
                          "message": "no_relevant_headlines" if not ranked else None,
                          "entitlement_boundary": {
                              "platts": "separate_machine_readable_api_only",
                              "public": "publisher_attributed_discovery_no_sla",
                          }})

    def refresh(
        self,
        scope: str = "all",
        timeout_seconds: int = 240,
        *,
        symbols: Sequence[str] | None = None,
        curve_ids: Sequence[str] | None = None,
        market_scope: str = "all",
        batch_size: int | None = None,
    ) -> Dict[str, Any]:
        scope = str(scope or "all").lower()
        if scope == "news":
            return self.refresh_news()
        if scope == "history":
            # Server/UI refreshes intentionally cover only the current year.
            # Multi-year backfills remain an explicit CLI operation.
            return self.refresh_history(
                scope=market_scope,
                symbols=symbols,
                batch_size=batch_size or (1 if symbols else 5),
                timeout_seconds=max(timeout_seconds, 600),
            )
        if scope in {"curve", "curves"}:
            return self.refresh_curves(
                scope=market_scope,
                curve_ids=curve_ids,
                batch_size=batch_size or 4,
                timeout_seconds=max(timeout_seconds, 600),
            )
        if scope == "moc":
            return self.refresh_moc(
                scope=market_scope,
                timeout_seconds=max(timeout_seconds, 600),
            )
        market = self.refresh_market(scope, timeout_seconds=timeout_seconds)
        if scope != "all":
            return market
        news = self.refresh_news()
        states = {market.get("state"), news.get("state")}
        if states == {"succeeded"}:
            state = "succeeded"
        elif "succeeded" in states or "partial" in states:
            state = "partial"
        elif states == {"deferred"}:
            state = "deferred"
        else:
            state = "failed"
        return json_safe({"state": state, "scope": scope, "market": market, "news": news})

    def build_backfill(
        self,
        start_year: int,
        end_year: int,
        scope: str = "all",
        symbols: Sequence[str] | None = None,
        batch_size: int = 1,
        force: bool = False,
    ) -> Dict[str, Any]:
        paths = build_backfill_workbooks(
            start_year=start_year,
            end_year=end_year,
            scope=scope,
            symbols=symbols,
            batch_size=batch_size,
            force=force,
        )
        return {"state": "succeeded", "scope": scope,
                "start_year": start_year, "end_year": end_year,
                "symbols": list(symbols or []), "batch_size": batch_size,
                "workbooks": [str(path) for path in paths]}

    def build_curves(
        self,
        *,
        scope: str = "all",
        curve_ids: Sequence[str] | None = None,
        batch_size: int = 1,
        force: bool = False,
    ) -> Dict[str, Any]:
        paths = build_curve_workbooks(
            scope=scope, curve_ids=curve_ids, batch_size=batch_size, force=force,
        )
        return {"state": "succeeded" if paths else "failed", "scope": scope,
                "curve_ids": list(curve_ids or []), "batch_size": batch_size,
                "workbooks": [str(path) for path in paths]}

    def build_moc(self, *, scope: str = "all", force: bool = False) -> Dict[str, Any]:
        paths = build_moc_workbooks(scope=scope, force=force)
        return {"state": "succeeded", "scope": scope,
                "workbooks": [str(path) for path in paths],
                "fundamentals": {"state": "unavailable",
                                 "reason": "no_reliable_licensed_source_configured"}}


def default_workflow() -> LpgRefreshWorkflow:
    return LpgRefreshWorkflow()
