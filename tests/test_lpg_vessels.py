import csv
import unittest

from _support import WorkspaceScratchMixin
from lpg.service import LpgService
from lpg.vessels import load_port_call_snapshot


FIELDS = (
    "vessel_name", "imo", "mmsi", "seq", "port", "port_cn", "locode",
    "country", "country_code", "arrival_utc", "berth_utc", "departure_utc",
    "stay_hours", "draught_arrival", "draught_departure", "cargo_op",
    "next_destination", "leg_distance_nm", "leg_nav_hours", "leg_avg_speed_kn",
    "timezone", "latitude", "longitude", "source", "source_detail",
    "confidence", "raw",
)


class VesselIntelligenceTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = LpgService(db_path=self.scratch / "vessels.sqlite")
        self.csv_path = self.scratch / "port_calls.csv"
        rows = [
            {
                "vessel_name": "GAS TEST", "imo": "9990001", "mmsi": "352000001",
                "seq": "1", "port": "Kemaman", "port_cn": "", "locode": "MYKEM",
                "country": "Malaysia", "country_code": "MY",
                "arrival_utc": "2026-06-01T10:00:00", "berth_utc": "2026-06-01T11:00:00",
                "departure_utc": "2026-06-02T10:00:00", "stay_hours": "24",
                "draught_arrival": "5.0", "draught_departure": "6.2",
                "cargo_op": "Loaded (draught up)", "next_destination": "VN VUT",
                "leg_distance_nm": "0", "leg_nav_hours": "0", "leg_avg_speed_kn": "0",
                "timezone": "GMT +8", "latitude": "4.195", "longitude": "103.515",
                "source": "hifleet", "source_detail": "snapshot endpoint",
                "confidence": "high", "raw": '{"accuracy":1}',
            },
            {
                "vessel_name": "GAS TEST", "imo": "9990001", "mmsi": "352000001",
                "seq": "2", "port": "Cai Mep", "port_cn": "", "locode": "VNCMT",
                "country": "Vietnam", "country_code": "VN",
                "arrival_utc": "2026-06-05", "berth_utc": "",
                "departure_utc": "2026-06-06", "stay_hours": "24",
                "draught_arrival": "6.2", "draught_departure": "5.1",
                "cargo_op": "Discharged (draught down)", "next_destination": "SG SIN",
                "leg_distance_nm": "0", "leg_nav_hours": "0", "leg_avg_speed_kn": "0",
                "timezone": "GMT +7", "latitude": "10.531", "longitude": "107.023",
                "source": "hifleet", "source_detail": "snapshot endpoint",
                "confidence": "high", "raw": '{"accuracy":1}',
            },
        ]
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_snapshot_import_is_idempotent_and_never_creates_live_positions(self):
        first = self.service.import_vessel_port_calls(
            self.csv_path, fleet_group="equinor_reference",
        )
        second = self.service.import_vessel_port_calls(
            self.csv_path, fleet_group="equinor_reference",
        )
        intelligence = self.service.vessel_intelligence()

        self.assertEqual(5, self.service.store.status()["database"]["schema_version"])
        self.assertEqual((1, 2, 0), (
            first["counts"]["vessels_inserted"],
            first["counts"]["port_calls_inserted"],
            first["counts"]["positions_inserted"],
        ))
        self.assertEqual((1, 2), (
            second["counts"]["vessels_unchanged"],
            second["counts"]["port_calls_unchanged"],
        ))
        self.assertEqual(("historical_only", 0, 2), (
            intelligence["coverage"]["display_state"],
            intelligence["position_total"],
            intelligence["port_call_total"],
        ))
        self.assertEqual("source_timezone_unverified",
                         intelligence["port_calls"][1]["timestamp_state"])
        signals = {row["operation_signal"] for row in intelligence["port_calls"]}
        self.assertEqual({"loaded", "discharged"}, signals)
        self.assertEqual("draught_change_inference",
                         intelligence["port_calls"][0]["operation_signal_state"])
        self.assertEqual("unverified", intelligence["source_health"][0]["entitlement_state"])
        self.assertIn("no live position", first["boundary"])
        self.assertNotIn("raw", intelligence["port_calls"][0])
        self.assertTrue(intelligence["port_calls"][0]["raw_evidence_available"])

    def test_timestamped_position_is_separate_and_freshness_labelled(self):
        self.service.import_vessel_port_calls(self.csv_path)
        stored = self.service.store.upsert_vessel_position({
            "position_key": "fixture-position", "vessel_id": "imo-9990001",
            "observed_at": "2020-01-01T00:00:00+00:00", "timestamp_state": "normalized_utc",
            "latitude": 1.25, "longitude": 103.8, "position_kind": "historical",
            "source": "fixture_ais", "speed_kn": 11.2,
        })
        intelligence = self.service.vessel_intelligence()

        self.assertEqual("inserted", stored["action"])
        self.assertEqual((1, "stale"), (
            intelligence["position_total"], intelligence["positions"][0]["freshness"],
        ))
        self.assertNotEqual(
            intelligence["positions"][0]["latitude"],
            intelligence["port_calls"][0]["latitude"],
        )

    def test_invalid_snapshot_contract_is_rejected(self):
        invalid = self.scratch / "invalid.csv"
        invalid.write_text("vessel_name,imo\nGAS TEST,9990001\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing required column"):
            load_port_call_snapshot(invalid)


if __name__ == "__main__":
    unittest.main()
