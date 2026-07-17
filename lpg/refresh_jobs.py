"""Single-flight background job manager for local LPG refresh operations."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional


VALID_SCOPES = {
    "asia", "overnight", "all", "news",
    "history", "curves", "moc",
}


class RefreshBusy(RuntimeError):
    def __init__(self, job: Dict[str, Any]):
        super().__init__(f"refresh job {job['id']} is already {job['state']}")
        self.job = job


class RefreshJobManager:
    def __init__(self, runner: Callable[..., Dict[str, Any]], keep: int = 50):
        self.runner = runner
        self.keep = max(10, keep)
        self._jobs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._active_id: Optional[str] = None
        self._lock = threading.Lock()

    def start(self, scope: str, *, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        scope = (scope or "all").lower()
        if scope not in VALID_SCOPES:
            raise ValueError(f"invalid refresh scope '{scope}'")
        parameters = dict(parameters or {})
        with self._lock:
            if self._active_id:
                active = self._jobs.get(self._active_id)
                if active and active["state"] in ("queued", "running"):
                    raise RefreshBusy(dict(active))
            now = int(time.time())
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "scope": scope,
                "parameters": parameters,
                "state": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._active_id = job_id
            self._prune_locked()
        threading.Thread(target=self._run, args=(job_id,), daemon=True,
                         name=f"lpg-refresh-{job_id[:8]}").start()
        return dict(job)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["state"] = "running"
            job["started_at"] = int(time.time())
            scope = job["scope"]
            parameters = dict(job.get("parameters") or {})
        try:
            result = (self.runner(scope, **parameters) if parameters else self.runner(scope)) or {}
            state = str(result.get("state") or "succeeded")
            if state not in ("succeeded", "deferred", "partial", "failed", "blocked"):
                state = "failed"
            with self._lock:
                job = self._jobs[job_id]
                job["state"] = state
                job["result"] = result
                if state in ("failed", "blocked"):
                    job["error"] = str(result.get("error") or result.get("message") or state)[:1000]
        except Exception as exc:  # noqa: BLE001 - background boundary must report failures
            with self._lock:
                job = self._jobs[job_id]
                job["state"] = "failed"
                job["error"] = str(exc)[:1000]
        finally:
            with self._lock:
                job = self._jobs[job_id]
                job["finished_at"] = int(time.time())
                if self._active_id == job_id:
                    self._active_id = None

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def active(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(self._active_id or "")
            return dict(job) if job else None

    def list(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self._lock:
            return [dict(job) for job in list(self._jobs.values())[-max(1, limit):]][::-1]

    def _prune_locked(self) -> None:
        while len(self._jobs) > self.keep:
            first_id = next(iter(self._jobs))
            if first_id == self._active_id:
                break
            self._jobs.popitem(last=False)
