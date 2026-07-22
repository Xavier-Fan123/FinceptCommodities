import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from _support import WorkspaceScratchMixin
from http.server import ThreadingHTTPServer

import server
from lpg.refresh_jobs import RefreshJobManager
from lpg.service import LpgService


class LpgHttpApiTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.staging_patch = patch(
            "lpg.platts_excel.STAGING_DIR", self.scratch / "staging",
        )
        self.staging_patch.start()
        self.addCleanup(self.staging_patch.stop)
        self.service = LpgService(db_path=self.scratch / "http.sqlite")
        self._seed()
        self.refresh_calls = []

        def refresh_runner(scope, **parameters):
            self.refresh_calls.append((scope, parameters))
            return {"state": "succeeded", "scope": scope,
                    "parameters": parameters, "counts": {"rows_inserted": 1}}

        self.jobs = RefreshJobManager(
            refresh_runner,
        )
        self.previous_runtime = server._LPG_RUNTIME
        server._LPG_RUNTIME = (self.service, object(), self.jobs)
        self.addCleanup(setattr, server, "_LPG_RUNTIME", self.previous_runtime)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _seed(self):
        for series_id, canonical, entitlement, summary in (
            ("fei", "FEI_PROPANE", "entitled", True),
            ("denied", "CP_PROPANE", "unentitled", True),
        ):
            self.service.upsert_series({
                "id": series_id, "canonical_key": canonical,
                "name": series_id.upper(), "source": "fixture",
                "symbol": series_id.upper(), "currency": "USD", "unit": "mt",
                "normalized_currency": "USD", "normalized_unit": "mt",
                "entitlement_state": entitlement, "active": True,
                "display_order": 1 if series_id == "fei" else 2,
            })
            if summary:
                candidate = self.service.store.get_series(series_id)
                self.assertIsNotNone(candidate)
        self.service.upsert_observation({
            "series_id": "fei", "observation_date": "2026-07-10",
            "value_native": 520, "currency_native": "USD", "unit_native": "mt",
            "value_normalized": 520, "currency_normalized": "USD",
            "unit_normalized": "mt", "bate": "c",
            "fetched_at": "2026-07-10T10:00:00+00:00",
        })
        self.service.upsert_observation({
            "series_id": "denied", "observation_date": "2026-07-10",
            "value_native": 500, "currency_native": "USD", "unit_native": "mt",
            "fetched_at": "2026-07-10T10:00:00+00:00",
        })
        self.service.upsert_curve_point({
            "series_id": "fei", "as_of_date": "2026-07-10",
            "contract_month": "2026-08", "value_native": 525,
            "currency_native": "USD", "unit_native": "mt",
            "fetched_at": "2026-07-10T10:00:00+00:00",
        })
        self.service.upsert_news({
            "article_key": "allowed", "headline": "Asia LPG fixture",
            "source": "fixture", "published_at": "2026-07-10T10:00:00+00:00",
            "entitlement_state": "entitled", "tags": ["asia", "propane"],
        })
        self.service.upsert_news({
            "article_key": "denied", "headline": "Denied fixture",
            "source": "fixture", "published_at": "2026-07-10T10:00:00+00:00",
            "entitlement_state": "unentitled",
        })
        self.service.upsert_dataset_row({
            "dataset": "platts_ewindow", "row_key": "trade-1",
            "as_of_date": "2026-07-10", "source": "platts_excel",
            "payload": {"product": "Propane", "price": 521, "order_id": "trade-1"},
        })

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def json_request(self, method, path, payload=None):
        status, headers, body = self.request(method, path, payload)
        return status, headers, json.loads(body.decode("utf-8"))

    def test_all_read_routes_and_entitlement_boundary(self):
        status, _, summary = self.json_request("GET", "/api/lpg/summary?as_of=2026-07-10")
        self.assertEqual(200, status)
        self.assertEqual(["fei"], [row["series_id"] for row in summary["prices"]])

        status, _, series = self.json_request(
            "GET", "/api/lpg/series?entitlement_state=entitled&active=1",
        )
        self.assertEqual((200, ["fei"]), (status, [row["id"] for row in series["items"]]))

        status, _, history = self.json_request(
            "GET", "/api/lpg/series/fei/history?as_of=2026-07-10",
        )
        self.assertEqual((200, 1), (status, len(history["observations"])))
        status, _, denied = self.json_request("GET", "/api/lpg/series/denied/history")
        self.assertEqual(403, status)
        self.assertIn("not entitled", denied["error"])

        for path, key in (
            ("/api/lpg/curves", "curves"),
            ("/api/lpg/spreads", "items"),
            ("/api/lpg/situation", "events"),
            ("/api/lpg/scenarios", "templates"),
            ("/api/lpg/vessel-intelligence", "vessels"),
            ("/api/lpg/vessels", "items"),
            ("/api/lpg/news", "items"),
            ("/api/lpg/explorer?dataset=observations", "rows"),
            ("/api/lpg/explorer?dataset=moc", "rows"),
            ("/api/lpg/status", "entitlement_matrix"),
        ):
            with self.subTest(path=path):
                status, _, payload = self.json_request("GET", path)
                self.assertEqual(200, status)
                self.assertIn(key, payload)
        _, _, news = self.json_request("GET", "/api/lpg/news")
        self.assertEqual(["allowed"], [row["article_key"] for row in news["items"]])
        _, _, situation = self.json_request("GET", "/api/lpg/situation")
        self.assertEqual(1, situation["total"])
        self.assertEqual("unavailable", situation["intelligence_gaps"][0]["status"])
        _, _, observations = self.json_request("GET", "/api/lpg/explorer?dataset=observations")
        self.assertEqual(["fei"], [row["series_id"] for row in observations["rows"]])

    def test_scenario_api_and_situation_static_assets(self):
        status, _, catalog = self.json_request("GET", "/api/lpg/scenarios")
        self.assertEqual((200, 6, "hypothetical_not_forecast"), (
            status, len(catalog["templates"]), catalog["state"],
        ))

        status, _, result = self.json_request("POST", "/api/lpg/scenarios/run", {
            "scenario_id": "panama_disruption",
            "inputs": {"shock_pct": 40, "duration_days": 30, "extra_transit_days": 12},
        })
        self.assertEqual((200, "panama_disruption", 55), (
            status, result["scenario_id"], result["stress_index"]["score"],
        ))
        self.assertEqual("hypothetical_not_forecast", result["scenario_state"])
        self.assertIn("usgc_north_asia_cape", result["alternative_route_ids"])
        self.assertIn("prices", result["market_snapshot"])
        self.assertIn("does not calculate a price", result["guardrail"])

        status, _, invalid = self.json_request("POST", "/api/lpg/scenarios/run", {
            "scenario_id": "panama_disruption", "inputs": {"shock_pct": 200},
        })
        self.assertEqual(400, status)
        self.assertIn("between 0 and 100", invalid["error"])

        for path, marker in (("/situation.js", b"FinceptLpgSituation"),
                             ("/situation.css", b"sit-scenario-lab")):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertIn(marker, body)

    def test_history_curve_provenance_and_dataset_health_contract(self):
        self.service.upsert_observation({
            "series_id": "fei", "observation_date": "2026-07-09",
            "value_native": 518, "currency_native": "USD", "unit_native": "mt",
            "value_normalized": 518, "currency_normalized": "USD",
            "unit_normalized": "mt", "bate": "c",
            "fetched_at": "2026-07-10T10:00:00+00:00",
        })
        for series_id, canonical, value in (
            ("hm1", "FEI_PROPANE_HM1", 520),
            ("hm2", "FEI_PROPANE_HM2", 524),
            ("hm3", "FEI_PROPANE_HM3", 529),
        ):
            self.service.upsert_series({
                "id": series_id, "canonical_key": canonical, "name": canonical,
                "source": "fixture", "symbol": series_id.upper(),
                "currency": "USD", "unit": "mt", "normalized_currency": "USD",
                "normalized_unit": "mt", "entitlement_state": "entitled", "active": True,
            })
            self.service.upsert_observation({
                "series_id": series_id, "observation_date": "2026-07-10",
                "value_native": value, "currency_native": "USD", "unit_native": "mt",
                "value_normalized": value, "currency_normalized": "USD",
                "unit_normalized": "mt", "bate": "c",
                "fetched_at": "2026-07-10T10:00:00+00:00",
            })
        self.service.upsert_dataset_row({
            "dataset": "platts_fundamentals", "row_key": "inventory-1",
            "as_of_date": "2026-07-10", "source": "fixture",
            "payload": {"series_id": "inventory", "value": 12},
        })

        status, _, history = self.json_request("GET", "/api/lpg/series/fei/history")
        self.assertEqual((200, 2, "ready"), (
            status, len(history["observations"]), history["availability"]["status"],
        ))

        status, _, curves = self.json_request("GET", "/api/lpg/curves")
        self.assertEqual((200, 1, 1), (
            status, curves["official_curve_count"], curves["derived_curve_count"],
        ))
        derived = next(curve for curve in curves["curves"] if curve["derived"])
        self.assertEqual(("derived_prompt_structure", ["hm1", "hm2", "hm3"]), (
            derived["curve_kind"], derived["component_series_ids"],
        ))
        official = next(curve for curve in curves["curves"] if curve["is_official"])
        self.assertEqual("official_fc_curve", official["curve_kind"])

        status, _, empty = self.json_request(
            "GET", "/api/lpg/curves?series_id=missing-series",
        )
        self.assertEqual((200, [], "empty"), (status, empty["curves"], empty["status"]))
        self.assertTrue(empty["reason"])

        status, _, health = self.json_request("GET", "/api/lpg/status")
        self.assertEqual(200, status)
        datasets = health["datasets"]
        self.assertEqual({"current", "history", "curves", "moc", "fundamentals"},
                         set(datasets))
        for dataset in datasets.values():
            self.assertTrue({"rows", "dates", "series", "status", "reason"}
                            .issubset(dataset))
        self.assertEqual(("partial", 1), (
            datasets["history"]["status"], datasets["history"]["multi_date_series"],
        ))
        self.assertEqual((1, "ready"), (
            datasets["moc"]["rows"], datasets["moc"]["status"],
        ))
        self.assertEqual((1, "ready"), (
            datasets["fundamentals"]["rows"], datasets["fundamentals"]["status"],
        ))
        self.assertEqual((1, 1), (
            datasets["curves"]["official_series"], datasets["curves"]["derived_series"],
        ))

    def test_refresh_job_and_validation_errors(self):
        status, _, started = self.json_request("POST", "/api/lpg/refresh", {"scope": "asia"})
        self.assertEqual(202, status)
        job_id = started["job"]["id"]
        for _ in range(100):
            status, _, current = self.json_request("GET", f"/api/lpg/refresh/{job_id}")
            if current["job"]["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual((200, "succeeded"), (status, current["job"]["state"]))
        status, _, invalid = self.json_request("GET", "/api/lpg/explorer?dataset=secret")
        self.assertEqual(400, status)
        self.assertIn("unsupported", invalid["error"])
        status, _, missing = self.json_request("GET", "/api/lpg/refresh/not-a-job")
        self.assertEqual(404, status)
        self.assertIn("unknown", missing["error"])

    def test_history_refresh_targets_selected_symbol_and_validates_parameters(self):
        status, _, started = self.json_request("POST", "/api/lpg/refresh", {
            "scope": "history", "symbols": ["PMAAV00"], "market_scope": "asia",
        })
        self.assertEqual(202, status)
        self.assertEqual(
            {"symbols": ["PMAAV00"], "market_scope": "asia"},
            started["job"]["parameters"],
        )
        job_id = started["job"]["id"]
        for _ in range(100):
            _, _, current = self.json_request("GET", f"/api/lpg/refresh/{job_id}")
            if current["job"]["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual(
            ("history", {"symbols": ["PMAAV00"], "market_scope": "asia"}),
            self.refresh_calls[-1],
        )

        for payload in (
            {"scope": "history", "symbols": ["not a symbol"]},
            {"scope": "curves", "market_scope": "europe"},
            {"scope": "asia", "symbols": ["PMAAV00"]},
        ):
            with self.subTest(payload=payload):
                status, _, response = self.json_request("POST", "/api/lpg/refresh", payload)
                self.assertEqual(400, status)
                self.assertIn("error", response)

    def test_csv_and_xlsx_exports_have_safe_headers(self):
        status, headers, body = self.request(
            "GET", "/api/lpg/export?view=history&series_id=fei&format=csv",
        )
        self.assertEqual(200, status)
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))
        self.assertEqual('attachment; filename="fincept-lpg-history.csv"',
                         headers["Content-Disposition"])
        self.assertIn(b"520", body)

        status, headers, body = self.request(
            "GET", "/api/lpg/export?view=curves&format=xlsx",
        )
        self.assertEqual(200, status)
        self.assertIn("spreadsheetml", headers["Content-Type"])
        self.assertTrue(body.startswith(b"PK"))

        status, headers, body = self.request(
            "GET", "/api/lpg/export?view=situation&format=csv",
        )
        self.assertEqual(200, status)
        self.assertEqual('attachment; filename="fincept-lpg-situation.csv"',
                         headers["Content-Disposition"])
        self.assertIn(b"Asia LPG fixture", body)

    def test_legacy_route_still_works(self):
        status, _, payload = self.json_request("GET", "/api/energy-chemicals")
        self.assertEqual(200, status)
        self.assertIsInstance(payload, dict)


if __name__ == "__main__":
    unittest.main()
