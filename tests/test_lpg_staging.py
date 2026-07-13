import json
import unittest

from _support import WorkspaceScratchMixin
from lpg.platts_excel import write_staging
from lpg.service import LpgService


GENERATED_AT = "2026-07-10T01:00:00+00:00"


class StagingImportTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = LpgService(db_path=self.scratch / "lpg.sqlite")

    @staticmethod
    def market_payload():
        return {
            "schema_version": 1,
            "generated_at": GENERATED_AT,
            "scope": "asia",
            "workbook": "fixture.xlsx",
            "workbook_mtime": GENERATED_AT,
            "status": "success",
            "records": [{
                "record_type": "price_observation",
                "source": "platts_excel",
                "series_id": "platts_cfr_na_propane",
                "candidate_id": "cfr_na_propane",
                "canonical_key": "FEI_PROPANE",
                "dataset": "MD",
                "data_series": "History-Symbol",
                "scope": "asia",
                "query_group": "north_asia",
                "basis": "Singapore close",
                "symbol": "PMAAV00",
                "description": "CFR North Asia propane",
                "value": 520.25,
                "currency": "USD",
                "uom": "MT",
                "bate": "c",
                "assess_date": "2026-07-09",
                "mod_date": "2026-07-09T14:00:00+00:00",
            }],
            "entitlement_results": [{
                "candidate_id": "cfr_na_propane",
                "canonical_key": "FEI_PROPANE",
                "series_id": "platts_cfr_na_propane",
                "symbol": "PMAAV00",
                "dataset": "MD",
                "data_series": "History-Symbol",
                "scope": "asia",
                "status": "entitled",
                "record_count": 1,
                "reason_code": "data_returned",
            }],
            "discovery": [],
            "errors": [],
        }

    def test_staging_record_uses_canonical_catalog_metadata(self):
        imported = self.service.import_platts_staging(self.market_payload())
        series = self.service.store.get_series("platts_cfr_na_propane")
        observation = self.service.store.history("platts_cfr_na_propane")[0]

        self.assertEqual("success", imported["status"])
        self.assertEqual({"rows_seen": 1, "rows_inserted": 1,
                          "rows_updated": 0, "rows_skipped": 0}, imported["counts"])
        self.assertEqual("FEI_PROPANE", series["canonical_key"])
        self.assertEqual("Propane", series["product"])
        self.assertEqual("CFR North Asia", series["market"])
        self.assertEqual("entitled", series["entitlement_state"])
        self.assertEqual((520.25, "USD", "mt"),
                         (observation["value"], observation["currency"], observation["unit"]))

    def test_workbook_candidate_id_updates_its_canonical_entitlement(self):
        self.service.import_platts_staging(self.market_payload())
        candidate = next(
            row for row in self.service.store.candidates()
            if row["canonical_key"] == "FEI_PROPANE"
        )

        self.assertEqual("entitled", candidate["discovery_status"])
        self.assertEqual("platts_cfr_na_propane", candidate["mapped_series_id"])

    def test_successful_variant_wins_over_stale_variant_error(self):
        payload = self.market_payload()
        payload["entitlement_results"].append({
            "candidate_id": "cfr_na_propane",
            "canonical_key": "FEI_PROPANE",
            "series_id": "platts_cfr_na_propane",
            "symbol": "PMAAV00",
            "dataset": "MD",
            "data_series": "Correction-Symbol",
            "scope": "asia",
            "status": "error",
            "record_count": 0,
            "reason_code": "session_or_formula_error",
        })

        self.service.import_platts_staging(payload)
        series = self.service.store.get_series("platts_cfr_na_propane")
        candidate = next(
            row for row in self.service.store.candidates()
            if row["canonical_key"] == "FEI_PROPANE"
        )

        self.assertEqual("entitled", series["entitlement_state"])
        self.assertEqual("entitled", candidate["discovery_status"])

    def test_discovery_only_staging_is_written_and_idempotently_persisted(self):
        payload = {
            "schema_version": 1,
            "generated_at": GENERATED_AT,
            "scope": "asia",
            "workbook": "discovery.xlsx",
            "workbook_mtime": GENERATED_AT,
            "status": "discovery_only",
            "records": [],
            "entitlement_results": [],
            "runtime_status": {"refresh_state": "success"},
            "discovery": [{
                "sheet": "md_catalog", "scope": "asia", "row_count": 2,
                "truncated": False, "error_code": None,
                "rows": [["MD", "Current-Symbol"], ["MD", "History-Symbol"]],
            }],
            "errors": [],
        }
        stage_dir = self.scratch / "staging"
        staged = write_staging(payload, staging_dir=stage_dir)

        self.assertEqual(stage_dir / "latest.json", staged)
        self.assertTrue((stage_dir / "status.json").exists())
        saved = json.loads(staged.read_text(encoding="utf-8"))
        self.assertEqual("discovery_only", saved["status"])
        self.assertEqual(payload["discovery"], saved["discovery"])

        first = self.service.import_platts_staging(staged)
        second = self.service.import_platts_staging(staged)
        persisted = self.service.explorer(dataset="dataset_rows", name="platts_discovery")
        self.assertEqual(("success", 2), (first["status"], first["discovery_rows"]))
        self.assertEqual(("success", 2), (second["status"], second["discovery_rows"]))
        self.assertEqual(2, persisted["total"])
        self.assertEqual(
            {("MD", "Current-Symbol"), ("MD", "History-Symbol")},
            {tuple(json.loads(row["payload_json"])["values"])
             for row in persisted["rows"]},
        )
        self.assertEqual(["success", "success"],
                         [run["status"] for run in self.service.store.recent_runs(2)])


if __name__ == "__main__":
    unittest.main()
