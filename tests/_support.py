"""Workspace-local test scratch helpers.

The Windows runner can leave system-temp directories with unusable ACLs, so LPG
tests deliberately create deterministic per-test directories under this suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path


SCRATCH_ROOT = Path(__file__).resolve().parent / ".scratch"


class WorkspaceScratchMixin:
    """Give each test a clean workspace-local directory and always remove it."""

    scratch: Path

    def setUp(self) -> None:
        super().setUp()
        name = f"{type(self).__name__}_{self._testMethodName}"
        self.scratch = SCRATCH_ROOT / name
        self._remove_scratch()
        self.scratch.mkdir(parents=True, exist_ok=False)
        self.addCleanup(self._remove_scratch)

    def _remove_scratch(self) -> None:
        if getattr(self, "scratch", None) and self.scratch.exists():
            shutil.rmtree(self.scratch)
        if SCRATCH_ROOT.exists():
            try:
                SCRATCH_ROOT.rmdir()
            except OSError:
                # Other tests may still own their per-test scratch directory.
                pass
