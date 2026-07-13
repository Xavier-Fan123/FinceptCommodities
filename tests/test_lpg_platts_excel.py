import unittest

from openpyxl import load_workbook

from _support import WorkspaceScratchMixin
from lpg.platts_excel import build_probe_workbook, build_workbook, parse_workbook


class PlattsWorkbookTests(WorkspaceScratchMixin, unittest.TestCase):
    def test_probe_workbook_has_one_current_query_and_no_discovery(self):
        from unittest.mock import patch

        with patch("lpg.platts_excel.PRIVATE_DIR", self.scratch):
            path = build_probe_workbook(force=True)
        workbook = load_workbook(path, data_only=False, read_only=True)
        try:
            manifest = list(workbook["_query_manifest"].iter_rows(values_only=True))
            headers = list(manifest[0])
            rows = [dict(zip(headers, row)) for row in manifest[1:] if row[0]]
            self.assertEqual(1, len(rows))
            self.assertEqual(("cfr_na_propane", "current"),
                             (rows[0]["candidate_id"], rows[0]["kind"]))
            self.assertNotIn("datasets", workbook.sheetnames)
            self.assertNotIn("md_catalog", workbook.sheetnames)
        finally:
            workbook.close()

    def test_build_workbook_isolated_queries_and_manifest(self):
        path = build_workbook(
            self.scratch / "Platts_LPG_asia.xlsx", scope="asia", force=True,
        )
        workbook = load_workbook(path, data_only=False, read_only=True)
        try:
            self.assertEqual("veryHidden", workbook["_query_manifest"].sheet_state)
            self.assertEqual("veryHidden", workbook["_runtime_status"].sheet_state)
            manifest = list(workbook["_query_manifest"].iter_rows(values_only=True))
            headers = list(manifest[0])
            rows = [dict(zip(headers, row)) for row in manifest[1:] if row[0]]
            self.assertGreater(len(rows), 20)
            self.assertEqual(len(rows), len({row["sheet"] for row in rows}))
            cfr = [row for row in rows if row["candidate_id"] == "cfr_na_propane"]
            self.assertEqual({"current", "history", "correction"},
                             {row["kind"] for row in cfr})
            self.assertTrue(all(row["canonical_key"] == "FEI_PROPANE" for row in cfr))
            for row in rows:
                formula = workbook[row["sheet"]]["A2"].value
                self.assertIsInstance(formula, str)
                self.assertTrue(formula.startswith("=PlattsGetData("))
        finally:
            workbook.close()

    def test_parse_workbook_fixture_maps_market_data_and_discovery(self):
        path = build_workbook(
            self.scratch / "Platts_LPG_asia.xlsx", scope="asia", force=True,
        )
        workbook = load_workbook(path, data_only=False)
        manifest = workbook["_query_manifest"]
        headers = [cell.value for cell in manifest[1]]
        target = None
        for values in manifest.iter_rows(min_row=2, values_only=True):
            row = dict(zip(headers, values))
            if row.get("candidate_id") == "cfr_na_propane" and row.get("kind") == "current":
                target = row
                break
        self.assertIsNotNone(target)
        query = workbook[target["sheet"]]
        query.delete_rows(1, query.max_row)
        query.append(["Symbol", "Description", "Value", "Currency", "UOM",
                      "BATE", "Assess Date", "Mod Date"])
        query.append(["PMAAV00", "CFR North Asia propane", 523.75, "USD", "MT",
                      "c", "2026-07-09", "2026-07-09T14:30:00+00:00"])
        runtime = workbook["_runtime_status"]
        for row in runtime.iter_rows(min_row=2):
            if row[0].value == "refresh_state":
                row[1].value = "success"
        discovery = workbook["md_catalog"]
        discovery["A2"] = "MD"
        discovery["B2"] = "Current-Symbol"
        workbook.save(path)
        workbook.close()

        payload = parse_workbook(path)
        records = [row for row in payload["records"]
                   if row.get("series_id") == "platts_cfr_na_propane"]
        entitlement = next(
            row for row in payload["entitlement_results"]
            if row["candidate_id"] == "cfr_na_propane"
            and row["data_series"] == "Current-Symbol"
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("success", payload["runtime_status"]["refresh_state"])
        self.assertEqual(1, len(records))
        self.assertEqual(
            ("FEI_PROPANE", "PMAAV00", 523.75, "c", "2026-07-09"),
            (records[0]["canonical_key"], records[0]["symbol"], records[0]["value"],
             records[0]["bate"], records[0]["assess_date"]),
        )
        self.assertEqual(("entitled", 1, "data_returned"),
                         (entitlement["status"], entitlement["record_count"],
                          entitlement["reason_code"]))
        catalog = next(row for row in payload["discovery"] if row["sheet"] == "md_catalog")
        self.assertEqual([["MD", "Current-Symbol"]], catalog["rows"])


if __name__ == "__main__":
    unittest.main()
