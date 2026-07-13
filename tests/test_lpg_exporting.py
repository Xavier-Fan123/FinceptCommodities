import io
import unittest

from openpyxl import load_workbook

from lpg.exporting import to_csv, to_xlsx


class ExportTests(unittest.TestCase):
    def test_csv_serializes_nested_values(self):
        payload = to_csv([{"symbol": "PMAAV00", "tags": ["asia", "propane"]}])
        text = payload.decode("utf-8-sig")
        self.assertIn("PMAAV00", text)
        self.assertIn("propane", text)

    def test_xlsx_round_trip(self):
        payload = to_xlsx([{"series": "fei", "value": 510.25}], "History")
        wb = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        self.assertEqual("History", wb.active.title)
        self.assertEqual("series", wb.active["A1"].value)
        self.assertEqual("fei", wb.active["A2"].value)


if __name__ == "__main__":
    unittest.main()
