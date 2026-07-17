import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import WorkspaceScratchMixin
from lpg.news_sources import dedupe_articles, normalize_article
from lpg.service import LpgService
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

    @patch("lpg.workflow.write_staging")
    @patch("lpg.workflow.parse_workbook")
    @patch("lpg.workflow.build_backfill_workbooks")
    def test_history_refresh_runs_build_excel_parse_stage_import_per_symbol_batch(
        self, build_backfill_workbooks, parse_workbook, write_staging,
    ):
        workbook = self.scratch / "Platts_LPG_asia_2025_cfr_na_propane.xlsx"
        workbook.write_bytes(b"fixture")
        staged = self.scratch / "staging" / "backfill" / "2025_asia.json"
        build_backfill_workbooks.return_value = [workbook]
        parse_workbook.return_value = {
            "schema_version": 1, "scope": "asia", "status": "success",
            "records": [{"value": 1}], "entitlement_results": [], "errors": [],
        }
        write_staging.return_value = staged
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        service = FakeService()

        result = LpgRefreshWorkflow(service=service, process_runner=runner).refresh_history(
            start_year=2025, end_year=2025, scope="asia",
            symbols=("PMAAV00",), batch_size=1, timeout_seconds=60,
        )

        self.assertEqual("succeeded", result["state"])
        self.assertEqual(1, result["record_count"])
        self.assertEqual([staged], service.imported)
        build_backfill_workbooks.assert_called_once_with(
            start_year=2025, end_year=2025, scope="asia",
            symbols=("PMAAV00",), batch_size=1, force=False,
        )
        self.assertEqual(
            {"purpose": "backfill", "year": 2025,
             "batch_id": workbook.stem},
            write_staging.call_args.kwargs,
        )

    @patch("lpg.workflow.write_staging")
    @patch("lpg.workflow.parse_workbook")
    @patch("lpg.workflow.build_curve_workbooks")
    def test_curve_refresh_has_independent_stage_and_import_path(
        self, build_curve_workbooks, parse_workbook, write_staging,
    ):
        workbook = self.scratch / "Platts_LPG_FC_asia_curve_cfr_na_propane.xlsx"
        workbook.write_bytes(b"fixture")
        staged = self.scratch / "staging" / "curve" / "latest_asia.json"
        build_curve_workbooks.return_value = [workbook]
        parse_workbook.return_value = {
            "schema_version": 1, "scope": "asia", "status": "success",
            "records": [{"record_type": "curve_point"}],
            "entitlement_results": [], "errors": [],
        }
        write_staging.return_value = staged
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        service = FakeService()

        result = LpgRefreshWorkflow(service=service, process_runner=runner).refresh_curves(
            scope="asia", curve_ids=("CN3HO",), timeout_seconds=60,
        )

        self.assertEqual("succeeded", result["state"])
        self.assertEqual([staged], service.imported)
        self.assertEqual("curve", write_staging.call_args.kwargs["purpose"])
        build_curve_workbooks.assert_called_once_with(
            scope="asia", curve_ids=("CN3HO",), batch_size=1, force=False,
        )

    @patch("lpg.workflow.write_staging")
    @patch("lpg.workflow.parse_workbook")
    @patch("lpg.workflow.build_moc_workbooks")
    def test_moc_refresh_is_ewindow_only_and_fundamentals_stay_unavailable(
        self, build_moc_workbooks, parse_workbook, write_staging,
    ):
        workbook = self.scratch / "Platts_LPG_MOC_asia.xlsx"
        workbook.write_bytes(b"fixture")
        staged = self.scratch / "staging" / "moc" / "latest_asia.json"
        build_moc_workbooks.return_value = [workbook]
        parse_workbook.return_value = {
            "schema_version": 1, "scope": "asia", "status": "success",
            "records": [{"record_type": "ewindow_trade"}],
            "entitlement_results": [], "errors": [],
        }
        write_staging.return_value = staged
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))

        result = LpgRefreshWorkflow(
            service=FakeService(), process_runner=runner,
        ).refresh_moc(scope="asia", timeout_seconds=60)

        self.assertEqual("succeeded", result["state"])
        self.assertEqual("platts_ewindow", result["dataset"])
        self.assertEqual("unavailable", result["fundamentals"]["state"])
        self.assertEqual("moc", write_staging.call_args.kwargs["purpose"])

    def test_generic_refresh_dispatches_specialized_async_scopes(self):
        workflow = LpgRefreshWorkflow(service=FakeService())
        with patch.object(workflow, "refresh_history", return_value={"state": "succeeded"}) as history:
            self.assertEqual("succeeded", workflow.refresh("history", 75)["state"])
            history.assert_called_once_with(
                scope="all", symbols=None, batch_size=5, timeout_seconds=600,
            )
        with patch.object(workflow, "refresh_curves", return_value={"state": "succeeded"}) as curves:
            self.assertEqual("succeeded", workflow.refresh("curves", 80)["state"])
            curves.assert_called_once_with(
                scope="all", curve_ids=None, batch_size=4, timeout_seconds=600,
            )
        with patch.object(workflow, "refresh_moc", return_value={"state": "succeeded"}) as moc:
            self.assertEqual("succeeded", workflow.refresh("moc", 90)["state"])
            moc.assert_called_once_with(scope="all", timeout_seconds=600)

    def test_generic_history_refresh_can_target_selected_ui_symbol(self):
        workflow = LpgRefreshWorkflow(service=FakeService())
        with patch.object(workflow, "refresh_history", return_value={"state": "succeeded"}) as history:
            result = workflow.refresh(
                "history", symbols=["PMAAV00"], market_scope="asia",
            )
        self.assertEqual("succeeded", result["state"])
        history.assert_called_once_with(
            scope="asia", symbols=["PMAAV00"], batch_size=1,
            timeout_seconds=600,
        )

    @patch("lpg.workflow.public_lpg_news")
    @patch("lpg.workflow.PlattsNewsClient")
    def test_public_news_success_is_not_partial_when_platts_is_unconfigured(
        self, platts_client, public_lpg_news,
    ):
        platts_client.return_value.configured = False
        article = dedupe_articles([normalize_article({
            "title": "Asia LPG propane cargo supply tightens",
            "source": "Fixture Publisher", "feed_id": "google_asia_lpg",
            "url": "https://fixture.test/lpg",
            "published": "2026-07-13T01:00:00Z",
        }, "public", "public")])[0]
        public_lpg_news.return_value = {
            "articles": [article], "error": None,
            "sources": [{
                "source_id": "google_asia_lpg", "source_name": "Google News",
                "kind": "search_rss", "status": "healthy",
                "last_attempt_at": "2026-07-13T01:01:00Z",
                "last_success_at": "2026-07-13T01:01:00Z",
                "latest_published_at": "2026-07-13T01:00:00Z",
                "article_count": 1, "relevant_count": 1, "latency_ms": 10,
                "metadata": {"role": "discovery_fallback", "production_sla": False},
            }],
        }
        service = LpgService(db_path=self.scratch / "news.sqlite")

        result = LpgRefreshWorkflow(service=service).refresh_news(limit=20)

        self.assertEqual("succeeded", result["state"])
        self.assertEqual(["platts_news_api_not_configured"], result["warnings"])
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["counts"]["rows_inserted"])
        self.assertEqual("healthy", service.store.news_source_health()[0]["status"])
        self.assertEqual("public_source",
                         service.news()["items"][0]["metadata"]["content_boundary"])

    @patch("lpg.workflow.public_lpg_news")
    @patch("lpg.workflow.PlattsNewsClient")
    def test_healthy_empty_news_window_is_success_not_failure(
        self, platts_client, public_lpg_news,
    ):
        platts_client.return_value.configured = False
        public_lpg_news.return_value = {
            "articles": [], "error": None,
            "sources": [{
                "source_id": "official_empty", "source_name": "Official Feed",
                "kind": "official_rss", "status": "empty",
                "last_attempt_at": "2026-07-13T01:01:00Z",
                "last_success_at": "2026-07-13T01:01:00Z",
                "latest_published_at": None, "article_count": 5,
                "relevant_count": 0, "latency_ms": 10,
            }],
        }
        service = LpgService(db_path=self.scratch / "empty-news.sqlite")

        result = LpgRefreshWorkflow(service=service).refresh_news(limit=20)

        self.assertEqual("succeeded", result["state"])
        self.assertEqual("no_relevant_headlines", result["message"])
        self.assertEqual(0, result["counts"]["rows_seen"])


if __name__ == "__main__":
    unittest.main()
