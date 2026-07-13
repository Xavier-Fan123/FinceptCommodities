import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import WorkspaceScratchMixin
from lpg.workflow import LpgRefreshWorkflow


class FakeService:
    def __init__(self, import_status="success"):
        self.import_status = import_status
        self.imported = []

    def import_platts_staging(self, path):
        self.imported.append(Path(path))
        return {"status": self.import_status, "counts": {"rows_inserted": 1}}


class WorkflowTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.script = self.scratch / "Refresh-LpgPlattsWorkbook.ps1"
        self.script.write_text("# offline fixture\n", encoding="utf-8")
        self.patches = (
            patch("lpg.workflow.REFRESH_SCRIPT", self.script),
            patch("lpg.workflow.shutil.which", return_value="powershell.exe"),
        )
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_mocked_process_runner_maps_excel_terminal_states(self):
        expected = {
            0: ("succeeded", None),
            2: ("failed", "excel_session_or_formula_unresolved"),
            3: ("failed", "excel_automation_failed"),
            4: ("deferred", "excel_already_open"),
            19: ("failed", "unexpected_excel_exit"),
        }
        workbook = self.scratch / "fixture.xlsx"
        workbook.write_bytes(b"fixture")
        for returncode, outcome in expected.items():
            with self.subTest(returncode=returncode):
                runner = Mock(return_value=subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout="standard output",
                    stderr="standard error",
                ))
                result = LpgRefreshWorkflow(
                    service=FakeService(), process_runner=runner,
                )._excel_refresh(workbook, 30)
                self.assertEqual(outcome, (result["state"], result["reason"]))
                self.assertEqual(returncode, result["returncode"])
                self.assertEqual("standard output\nstandard error", result["output"])
                command = runner.call_args.args[0]
                self.assertIn(str(self.script), command)
                self.assertIn(str(workbook), command)
                self.assertEqual("60", command[command.index("-TimeoutSec") + 1])
                self.assertEqual(180, runner.call_args.kwargs["timeout"])

    def test_mocked_process_timeout_is_reported_without_retry(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired(cmd="powershell.exe", timeout=180))
        result = LpgRefreshWorkflow(
            service=FakeService(), process_runner=runner,
        )._excel_refresh(self.scratch / "fixture.xlsx", 60)
        self.assertEqual({"state": "failed", "returncode": 3,
                          "reason": "excel_refresh_timeout"}, result)
        runner.assert_called_once()

    @patch("lpg.workflow.write_runtime_status")
    @patch("lpg.workflow.write_staging")
    @patch("lpg.workflow.parse_workbook")
    @patch("lpg.workflow.build_scope_workbooks")
    def test_market_refresh_imports_successful_scope_and_marks_partial(
        self, build_scope_workbooks, parse_workbook, write_staging, write_runtime_status,
    ):
        asia = self.scratch / "asia.xlsx"
        overnight = self.scratch / "overnight.xlsx"
        asia.write_bytes(b"asia")
        overnight.write_bytes(b"overnight")
        staged = self.scratch / "staging" / "latest.json"
        build_scope_workbooks.return_value = [asia, overnight]
        parse_workbook.return_value = {
            "schema_version": 1, "scope": "asia", "records": [{"value": 1}],
            "entitlement_results": [], "errors": [],
        }
        write_staging.return_value = staged
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, "ok", ""),
            subprocess.CompletedProcess([], 4, "", "already open"),
        ])
        service = FakeService()

        result = LpgRefreshWorkflow(service=service, process_runner=runner).refresh_market(
            "all", timeout_seconds=60,
        )

        self.assertEqual("partial", result["state"])
        self.assertEqual([staged], service.imported)
        self.assertEqual(["succeeded", "deferred"],
                         [row["state"] for row in result["results"]])
        parse_workbook.assert_called_once_with(asia)
        write_runtime_status.assert_called_once_with(
            scope="overnight", state="deferred", reason_code="excel_already_open",
        )
        write_staging.assert_called_once()


if __name__ == "__main__":
    unittest.main()
