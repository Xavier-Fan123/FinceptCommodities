"""Operational LPG refresh workflows.

The paid-data boundary is intentionally narrow: Python builds/parses private
workbooks, while the signed-in official Excel Add-in performs every licensed
query.  Credentials are never passed to this module or written to disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .models import json_safe
from .news_sources import (
    PlattsNewsClient,
    dedupe_articles,
    public_lpg_news,
    to_news_input,
)
from .platts_excel import (
    STAGING_DIR,
    build_backfill_workbooks,
    build_scope_workbooks,
    combine_payloads,
    parse_workbook,
    write_runtime_status,
    write_staging,
)
from .service import LpgService


ROOT = Path(__file__).resolve().parents[1]
REFRESH_SCRIPT = ROOT / "scripts" / "Refresh-LpgPlattsWorkbook.ps1"


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

    def _excel_refresh(self, workbook: Path, timeout_seconds: int) -> Dict[str, Any]:
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
        try:
            completed = self.process_runner(
                command, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=max(180, timeout_seconds + 120),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return {"state": "failed", "returncode": 3, "reason": "excel_refresh_timeout"}
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr)
                           if part and part.strip())[-4000:]
        states = {0: "succeeded", 2: "failed", 3: "failed", 4: "deferred"}
        reasons = {0: None, 2: "excel_session_or_formula_unresolved",
                   3: "excel_automation_failed", 4: "excel_already_open"}
        return {
            "state": states.get(completed.returncode, "failed"),
            "returncode": completed.returncode,
            "reason": reasons.get(completed.returncode, "unexpected_excel_exit"),
            "output": output,
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
        failed_scopes = [row for row in results if row["state"] != "succeeded"]
        record_count = sum(int(row.get("records") or 0) for row in results)
        if failed_scopes or imported.get("status") == "partial" or record_count == 0:
            state = "partial"
        elif imported.get("status") == "failed":
            state = "failed"
        else:
            state = "succeeded"
        return json_safe({
            "state": state,
            "scope": requested,
            "staging": str(staged),
            "results": results,
            "import": imported,
            "message": "entitlement_discovery_only" if record_count == 0 else None,
        })

    def refresh_news(self, limit: int = 100) -> Dict[str, Any]:
        run = self.service.start_run("lpg_news", "news", metadata={"limit": limit})
        errors, articles = [], []
        official = PlattsNewsClient()
        if official.configured:
            try:
                payload = official.fetch(page_size=min(max(limit, 20), 500))
                articles.extend(payload.get("articles") or [])
                if payload.get("error"):
                    errors.append(str(payload["error"]))
            except Exception as exc:
                errors.append(f"platts_news_api: {exc}")
        else:
            errors.append("platts_news_api_not_configured")
        try:
            public = public_lpg_news(limit=limit)
            articles.extend(public.get("articles") or [])
            if public.get("error"):
                errors.append(str(public["error"]))
        except Exception as exc:
            errors.append(f"public_news: {exc}")

        counts = {"rows_seen": len(articles), "rows_inserted": 0,
                  "rows_updated": 0, "rows_skipped": 0}
        for article in dedupe_articles(articles):
            try:
                saved = self.service.upsert_news(to_news_input(article))
                if saved.get("action") == "inserted":
                    counts["rows_inserted"] += 1
                else:
                    counts["rows_updated"] += 1
            except Exception as exc:
                counts["rows_skipped"] += 1
                errors.append(f"news_record: {exc}")
        wrote = counts["rows_inserted"] + counts["rows_updated"]
        if wrote and errors:
            status, state = "partial", "partial"
        elif wrote:
            status, state = "success", "succeeded"
        else:
            status, state = "failed", "failed"
        finished = self.service.finish_run(
            run["id"], status, **counts,
            error="; ".join(errors[:20]) if errors else None,
        )
        return json_safe({"state": state, "run": finished, "counts": counts,
                          "errors": errors})

    def refresh(self, scope: str = "all", timeout_seconds: int = 240) -> Dict[str, Any]:
        scope = str(scope or "all").lower()
        if scope == "news":
            return self.refresh_news()
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

    def build_backfill(self, start_year: int, end_year: int,
                       scope: str = "all", force: bool = False) -> Dict[str, Any]:
        paths = build_backfill_workbooks(
            start_year=start_year, end_year=end_year, scope=scope, force=force,
        )
        return {"state": "succeeded", "scope": scope,
                "start_year": start_year, "end_year": end_year,
                "workbooks": [str(path) for path in paths]}


def default_workflow() -> LpgRefreshWorkflow:
    return LpgRefreshWorkflow()
