import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from _support import WorkspaceScratchMixin
from lpg.service import LpgService


FIXED_FETCHED_AT = "2026-07-10T01:00:00+00:00"


class StoreServiceTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = LpgService(db_path=self.scratch / "lpg.sqlite")
        self.store = self.service.store
        # Service health intentionally reads the live specialized staging
        # status. Keep unit tests isolated from a developer's real refresh run.
        self.staging_patch = patch(
            "lpg.platts_excel.STAGING_DIR", self.scratch / "staging",
        )
        self.staging_patch.start()
        self.addCleanup(self.staging_patch.stop)

    def add_series(self, series_id, canonical_key, entitlement="entitled", **extra):
        return self.store.upsert_series({
            "id": series_id,
            "name": extra.pop("name", series_id),
            "source": extra.pop("source", "fixture"),
            "symbol": extra.pop("symbol", series_id.upper()),
            "canonical_key": canonical_key,
            "currency": "USD",
            "unit": "mt",
            "normalized_currency": "USD",
            "normalized_unit": "mt",
            "entitlement_state": entitlement,
            **extra,
        })

    def add_observation(self, series_id, observed, value, bate="c", **extra):
        return self.store.upsert_observation({
            "series_id": series_id,
            "observation_date": observed,
            "value_native": value,
            "currency_native": "USD",
            "unit_native": "mt",
            "value_normalized": value,
            "currency_normalized": "USD",
            "unit_normalized": "mt",
            "bate": bate,
            "fetched_at": extra.pop("fetched_at", FIXED_FETCHED_AT),
            **extra,
        })

    def test_entitlement_isolation_across_query_surfaces(self):
        self.add_series("allowed", "FEI_PROPANE", "entitled", display_order=1)
        self.add_series("denied", "CP_PROPANE", "unentitled", display_order=2)
        self.add_observation("allowed", "2026-07-09", 520)
        self.add_observation("denied", "2026-07-09", 500)
        self.add_observation("denied", "2026-07-09", 501,
                             revision_reason="unentitled correction")
        self.store.upsert_curve_point({
            "series_id": "allowed", "as_of_date": "2026-07-09",
            "contract_month": "2026-08", "value_native": 525,
            "currency_native": "USD", "unit_native": "mt",
            "fetched_at": FIXED_FETCHED_AT,
        })
        self.store.upsert_curve_point({
            "series_id": "denied", "as_of_date": "2026-07-09",
            "contract_month": "2026-08", "value_native": 505,
            "currency_native": "USD", "unit_native": "mt",
            "fetched_at": FIXED_FETCHED_AT,
        })
        self.store.upsert_news({
            "article_key": "allowed-news", "headline": "Entitled LPG item",
            "source": "fixture", "published_at": FIXED_FETCHED_AT,
            "entitlement_state": "entitled",
        })
        self.store.upsert_news({
            "article_key": "denied-news", "headline": "Denied LPG item",
            "source": "fixture", "published_at": FIXED_FETCHED_AT,
            "entitlement_state": "unentitled",
        })

        self.assertEqual(["allowed"], [row["series_id"] for row in self.store.latest_observations()])
        self.assertEqual(["allowed"], [row["series_id"] for row in self.store.curves()])
        self.assertEqual(["allowed"], [row["series_id"] for row in self.store.explorer("observations")["rows"]])
        self.assertEqual(["allowed"], [row["series_id"] for row in self.store.explorer("curves")["rows"]])
        self.assertEqual(["allowed-news"], [row["article_key"] for row in self.service.news()["items"]])
        self.assertEqual(["allowed-news"], [row["article_key"] for row in self.store.explorer("news")["rows"]])
        self.assertEqual([], self.store.explorer("revisions")["rows"])
        self.assertEqual([], self.service.curves(series_id="denied")["curves"])
        with self.assertRaises(PermissionError):
            self.service.series_history("denied")

        # Callers cannot override the facade's news entitlement guard.
        forced = self.service.news({"entitlement_state": "unentitled"})
        self.assertEqual(["allowed-news"], [row["article_key"] for row in forced["items"]])

    def test_bate_close_is_preferred_but_explicit_bate_remains_queryable(self):
        self.add_series("fei", "FEI_PROPANE")
        self.add_observation("fei", "2026-07-09", 518, bate="u")
        self.add_observation("fei", "2026-07-09", 515, bate="h")
        self.add_observation("fei", "2026-07-09", 520, bate="c")

        preferred = self.store.history("fei")
        self.assertEqual(1, len(preferred))
        self.assertEqual("c", preferred[0]["bate"])
        self.assertEqual(520, preferred[0]["value"])
        self.assertEqual("c", self.store.latest_observations()[0]["bate"])

        update_only = self.store.history("fei", bate="u")
        self.assertEqual(1, len(update_only))
        self.assertEqual(("u", 518), (update_only[0]["bate"], update_only[0]["value"]))

    def test_observation_revisions_are_idempotent_for_transport_changes(self):
        self.add_series("fei", "FEI_PROPANE")
        inserted = self.add_observation(
            "fei", "2026-07-09", 520, source_ref="history",
            publication_time="2026-07-09T14:00:00+00:00", metadata={"query": "history"},
        )
        duplicate = self.add_observation(
            "fei", "2026-07-09", 520, source_ref="correction",
            publication_time="2026-07-09T15:00:00+00:00",
            fetched_at="2026-07-10T02:00:00+00:00",
            revision_reason="Platts correction", metadata={"query": "correction"},
        )
        corrected = self.add_observation(
            "fei", "2026-07-09", 521, source_ref="correction",
            fetched_at="2026-07-10T03:00:00+00:00",
            revision_reason="Platts correction",
        )
        repeated = self.add_observation(
            "fei", "2026-07-09", 521, source_ref="current",
            fetched_at="2026-07-10T04:00:00+00:00",
        )

        self.assertEqual("inserted", inserted["action"])
        self.assertEqual(("unchanged", 0), (duplicate["action"], duplicate["revision_count"]))
        self.assertEqual(("updated", 1), (corrected["action"], corrected["revision_count"]))
        self.assertEqual(("unchanged", 1), (repeated["action"], repeated["revision_count"]))
        revisions = self.store.observation_revisions("fei")
        self.assertEqual(1, len(revisions))
        self.assertEqual(["value_native", "value_normalized"], revisions[0]["changed_fields"])
        self.assertEqual(520, revisions[0]["previous_snapshot"]["value_native"])
        self.assertEqual(521, revisions[0]["new_snapshot"]["value_native"])

    def test_cp_spread_aligns_assessments_by_contract_month(self):
        self.add_series("fei", "FEI_PROPANE")
        self.add_series("cp", "CP_PROPANE")
        for observed, fei, cp_date, cp in (
            ("2026-06-29", 530, "2026-06-01", 490),
            ("2026-07-15", 550, "2026-07-01", 500),
        ):
            self.add_observation("fei", observed, fei)
            self.add_observation("cp", cp_date, cp)

        definition = {
            "id": "fixture_fei_cp", "name": "FEI - CP",
            "legs": (("FEI_PROPANE", 1.0), ("CP_PROPANE", -1.0)),
            "alignment": "contract_month",
        }
        spread = self.service.spreads(
            as_of="2026-07-31", definitions=[definition], window=12,
        )["items"][0]

        self.assertTrue(spread["success"])
        self.assertIsNone(spread["blocked_reason"])
        self.assertEqual("2026-07", spread["contract_month"])
        self.assertEqual("2026-07-15", spread["observation_date"])
        self.assertEqual(50, spread["value"])
        self.assertEqual(
            [("2026-06", 40), ("2026-07", 50)],
            [(row["contract_month"], row["value"]) for row in spread["history"]],
        )
        self.assertEqual(
            {"FEI_PROPANE": "2026-07-15", "CP_PROPANE": "2026-07-01"},
            {row["canonical_key"]: row["observation_date"] for row in spread["legs"]},
        )

    def test_multi_date_history_and_dataset_health_are_explicit(self):
        self.add_series("fei", "FEI_PROPANE")
        self.add_observation("fei", "2026-07-09", 518)
        self.add_observation("fei", "2026-07-10", 520)
        self.store.upsert_dataset_row({
            "dataset": "platts_ewindow", "row_key": "moc-1",
            "as_of_date": "2026-07-10", "source": "fixture",
            "payload": {"series_id": "fei", "price": 520},
        })
        self.store.upsert_dataset_row({
            "dataset": "platts_fundamentals", "row_key": "fund-1",
            "as_of_date": "2026-07-10", "source": "fixture",
            "payload": {"symbol": "fixture_inventory", "value": 12},
        })

        history = self.service.series_history("fei")
        self.assertEqual(["2026-07-09", "2026-07-10"],
                         [row["date"] for row in history["observations"]])
        self.assertEqual({"status": "ready", "reason": None, "rows": 2, "dates": 2},
                         history["availability"])

        datasets = self.service.status()["datasets"]
        self.assertEqual((1, 1, 1, "ready"), (
            datasets["current"]["rows"], datasets["current"]["dates"],
            datasets["current"]["series"], datasets["current"]["status"],
        ))
        self.assertEqual((2, 2, 1, 1, "ready"), (
            datasets["history"]["rows"], datasets["history"]["dates"],
            datasets["history"]["series"], datasets["history"]["multi_date_series"],
            datasets["history"]["status"],
        ))
        self.assertEqual((1, 1, 1, "ready"), (
            datasets["moc"]["rows"], datasets["moc"]["dates"],
            datasets["moc"]["series"], datasets["moc"]["status"],
        ))
        self.assertEqual(("available_with_warning", "ready", "not_supported"), (
            datasets["fundamentals"]["status"],
            datasets["fundamentals"]["coverage_status"],
            datasets["fundamentals"]["refresh_status"],
        ))
        self.assertTrue(datasets["fundamentals"]["visible_now"])
        self.assertFalse(datasets["fundamentals"]["refresh_supported"])

    def test_official_and_derived_prompt_curves_have_distinct_provenance(self):
        component_ids = []
        for tenor, canonical, value in (
            ("hm1", "FEI_PROPANE_HM1", 520),
            ("hm2", "FEI_PROPANE_HM2", 524),
            ("hm3", "FEI_PROPANE_HM3", 529),
        ):
            self.add_series(tenor, canonical)
            self.add_observation(tenor, "2026-07-10", value)
            component_ids.append(tenor)
        for tenor, value in (("hm1", 522), ("hm2", 525), ("hm3", 528)):
            self.add_observation(tenor, "2026-07-11", value)

        derived_only = self.service.curves()
        self.assertEqual(("ready", 0, 1), (
            derived_only["status"], derived_only["official_curve_count"],
            derived_only["derived_curve_count"],
        ))
        derived = derived_only["curves"][0]
        self.assertTrue(derived["derived"])
        self.assertFalse(derived["is_official"])
        self.assertEqual("daily_derived_assessment_curve", derived["curve_kind"])
        self.assertEqual("hm1", derived["series_id"])
        self.assertEqual(component_ids, derived["component_series_ids"])
        self.assertEqual(["HM1", "HM2", "HM3"],
                         [point["tenor"] for point in derived["points"]])
        self.assertEqual((2, "contango", -3, -3), (
            derived["history_count"], derived["shape"]["structure"],
            derived["shape"]["hm1_hm2"], derived["shape"]["hm2_hm3"],
        ))
        self.assertEqual(["2026-07-10", "2026-07-11"], derived["available_dates"])
        self.assertEqual([2, 1, -1], [point["day_change"] for point in derived["points"]])
        self.assertTrue(all(point["source_series_id"] in component_ids
                            for point in derived["points"]))
        self.assertIn("not an official Platts FC curve", derived["methodology"])
        curve_health = self.service.status()["datasets"]["curves"]
        self.assertEqual(("available_with_warning", "limited", "all", False), (
            curve_health["status"], curve_health["availability_group"],
            curve_health["refresh_scope"], curve_health["official_refresh_supported"],
        ))

        self.add_series("official", "CURVE_FEI_PROPANE", quote_kind="curve")
        self.store.upsert_curve_point({
            "series_id": "official", "as_of_date": "2026-07-10",
            "contract_month": "2026-08", "value_native": 525,
            "currency_native": "USD", "unit_native": "mt",
            "fetched_at": FIXED_FETCHED_AT,
        })
        combined = self.service.curves()
        self.assertEqual(("ready", 1, 1), (
            combined["status"], combined["official_curve_count"],
            combined["derived_curve_count"],
        ))
        official = next(curve for curve in combined["curves"] if curve["is_official"])
        self.assertEqual("official_fc_curve", official["curve_kind"])
        self.assertFalse(official["derived"])
        self.assertEqual((4, 2), (
            combined["dataset_status"]["rows"], combined["dataset_status"]["series"],
        ))

    def test_daily_derived_curve_never_forward_fills_a_missing_tenor(self):
        for tenor, canonical in (
            ("hm1", "FEI_PROPANE_HM1"),
            ("hm2", "FEI_PROPANE_HM2"),
            ("hm3", "FEI_PROPANE_HM3"),
        ):
            self.add_series(tenor, canonical)
            self.add_observation(tenor, "2026-07-10", 520)
        self.add_observation("hm1", "2026-07-11", 525)
        self.add_observation("hm2", "2026-07-11", 523)

        curve = self.service.curves(as_of="2026-07-11")["curves"][0]

        self.assertEqual(("2026-07-10", ["2026-07-10"], 1), (
            curve["as_of_date"], curve["available_dates"], curve["history_count"],
        ))

    def test_six_leg_quality_keeps_incomplete_inputs_but_skips_that_curve_date(self):
        specs = (
            ("p1", "FEI_PROPANE_HM1", 520),
            ("p2", "FEI_PROPANE_HM2", 515),
            ("p3", "FEI_PROPANE_HM3", 510),
            ("b1", "CFR_NA_BUTANE_HM1", 525),
            ("b2", "CFR_NA_BUTANE_HM2", 520),
            ("b3", "CFR_NA_BUTANE_HM3", 515),
        )
        for series_id, canonical, value in specs:
            self.add_series(series_id, canonical)
            self.add_observation(series_id, "2026-07-10", value)
            if series_id != "b3":
                self.add_observation(series_id, "2026-07-11", value + 2)

        payload = self.service.curves(as_of="2026-07-11")
        propane = next(row for row in payload["curves"]
                       if row["canonical_key"] == "FEI_PROPANE_PROMPT_STRUCTURE")
        butane = next(row for row in payload["curves"]
                      if row["canonical_key"] == "CFR_NA_BUTANE_PROMPT_STRUCTURE")
        quality = payload["quality"]

        self.assertEqual("2026-07-11", propane["as_of_date"])
        self.assertEqual(("2026-07-10", 1), (butane["as_of_date"], butane["history_count"]))
        self.assertEqual((6, 6, "2026-07-10"), (
            quality["expected_legs"], quality["available_legs"],
            quality["latest_common_date"],
        ))
        self.assertEqual(["CFR_NA_BUTANE_HM3"], quality["missing_latest_legs"])
        self.assertIn("six_leg_latest_date_incomplete", quality["reason_codes"])
        self.assertEqual(["2026-07-10", "2026-07-11"],
                         [row["date"] for row in self.store.history("b1")])

    def test_curve_quality_flags_large_moves_without_dropping_values(self):
        for index, canonical in enumerate((
            "FEI_PROPANE_HM1", "FEI_PROPANE_HM2", "FEI_PROPANE_HM3",
            "CFR_NA_BUTANE_HM1", "CFR_NA_BUTANE_HM2", "CFR_NA_BUTANE_HM3",
        )):
            series_id = f"leg-{index}"
            self.add_series(series_id, canonical)
            self.add_observation(series_id, "2026-07-09", 500 + index)
            self.add_observation(series_id, "2026-07-10", 505 + index)
            self.add_observation(series_id, "2026-07-11", 570 + index)

        payload = self.service.curves(as_of="2026-07-11")

        self.assertEqual("warning", payload["quality"]["status"])
        self.assertEqual(6, payload["quality"]["anomaly_count"])
        self.assertIn("abnormal_daily_jump_detected", payload["quality"]["reason_codes"])
        self.assertTrue(all(curve["as_of_date"] == "2026-07-11"
                            for curve in payload["curves"]))
        self.assertEqual(570, self.store.history("leg-0")[-1]["value"])

    def test_curve_quality_surfaces_duplicate_daily_inputs_deterministically(self):
        for index, canonical in enumerate((
            "FEI_PROPANE_HM1", "FEI_PROPANE_HM2", "FEI_PROPANE_HM3",
            "CFR_NA_BUTANE_HM1", "CFR_NA_BUTANE_HM2", "CFR_NA_BUTANE_HM3",
        )):
            series_id = f"leg-{index}"
            self.add_series(series_id, canonical)
            self.add_observation(series_id, "2026-07-10", 500 + index)
        original_history = self.store.history

        def history_with_duplicate(series_id, *args, **kwargs):
            rows = original_history(series_id, *args, **kwargs)
            if series_id == "leg-0" and rows:
                duplicate = {**rows[-1], "id": 9999,
                             "fetched_at": "2026-07-10T02:00:00+00:00"}
                return [*rows, duplicate]
            return rows

        with patch.object(self.store, "history", side_effect=history_with_duplicate):
            payload = self.service.curves(as_of="2026-07-10")

        self.assertEqual(1, payload["quality"]["duplicate_record_count"])
        self.assertIn("duplicate_curve_input_records", payload["quality"]["reason_codes"])
        propane = next(row for row in payload["curves"]
                       if row["canonical_key"] == "FEI_PROPANE_PROMPT_STRUCTURE")
        self.assertEqual("derived:9999", propane["points"][0]["id"])

    def test_empty_curve_response_explains_missing_inputs(self):
        payload = self.service.curves()
        self.assertEqual([], payload["curves"])
        self.assertEqual("empty", payload["status"])
        self.assertEqual(
            "no_official_curve_points_or_complete_hm1_hm2_hm3_assessments",
            payload["reason"],
        )
        self.assertEqual(payload["reason"], payload["dataset_status"]["reason"])

    def test_specialized_import_keeps_curve_identity_and_does_not_mutate_price_series(self):
        self.add_series(
            "price", "FEI_PROPANE", source="platts_excel", symbol="PMAAV00",
            name="CFR North Asia Propane",
        )
        payload = {
            "schema_version": 1,
            "generated_at": FIXED_FETCHED_AT,
            "scope": "asia",
            "status": "success",
            "records": [
                {
                    "record_type": "curve_point", "series_id": "curve_fei",
                    "canonical_key": "CURVE_FEI_PROPANE", "candidate_id": "curve_cfr_na_propane",
                    "dataset": "FC", "data_series": "CurveData", "curve_code": "CN3HO",
                    # ContractCode is emitted as symbol by the FC parser.
                    "symbol": "AUG26", "description": "FEI official curve",
                    "value": 525, "currency": "USD", "uom": "MT",
                    "assess_date": "2026-07-10", "scope": "asia",
                },
                {
                    "record_type": "curve_point", "series_id": "curve_cp",
                    "canonical_key": "CURVE_CP_PROPANE", "candidate_id": "curve_saudi_cp_propane",
                    "dataset": "FC", "data_series": "CurveData", "curve_code": "CN0PT",
                    # The same contract code must not merge two different curves.
                    "symbol": "AUG26", "description": "Saudi CP official curve",
                    "value": 500, "currency": "USD", "uom": "MT",
                    "assess_date": "2026-07-10", "scope": "asia",
                },
                {
                    "record_type": "ewindow_trade", "series_id": "platts_ewindow_asia",
                    "canonical_key": "EWINDOW_LPG_TRADES", "candidate_id": "ewindow_asia_asia_lpg",
                    "dataset": "eWMD", "data_series": "TradeData",
                    # This intentionally collides with an existing price symbol.
                    "symbol": "PMAAV00", "description": "Asia LPG trade",
                    "value": 521, "currency": "USD", "uom": "MT",
                    "assess_date": "2026-07-10", "scope": "asia", "order_id": "trade-1",
                },
            ],
            "entitlement_results": [],
            "errors": [],
        }

        imported = self.service.import_platts_staging(payload)

        self.assertEqual((3, 3, 0), (
            imported["counts"]["rows_seen"], imported["counts"]["rows_inserted"],
            imported["counts"]["rows_skipped"],
        ))
        curves = self.store.curves()
        self.assertEqual({"curve_fei", "curve_cp"}, {row["series_id"] for row in curves})
        self.assertEqual({"AUG26"}, {row["contract_month"] for row in curves})
        self.assertEqual("CN3HO", self.store.get_series("curve_fei")["symbol"])
        self.assertEqual("CN0PT", self.store.get_series("curve_cp")["symbol"])
        price = self.store.get_series("price")
        self.assertEqual(("FEI_PROPANE", "CFR North Asia Propane"),
                         (price["canonical_key"], price["name"]))
        self.assertIsNone(self.store.get_series("platts_ewindow_asia"))
        self.assertEqual(1, self.store.explorer(
            "dataset_rows", name="platts_ewindow",
        )["total"])

    def test_history_entitlement_does_not_downgrade_proven_current_candidate(self):
        self.add_series(
            "price", "FEI_PROPANE", source="platts_excel", symbol="PMAAV00",
        )
        self.store.update_candidate_entitlement(
            "FEI_PROPANE", "entitled", mapped_series_id="price",
            error="data_returned", symbol="PMAAV00",
        )
        result = self.service.import_platts_staging({
            "schema_version": 1,
            "generated_at": FIXED_FETCHED_AT,
            "scope": "asia",
            "status": "discovery_only",
            "records": [],
            "entitlement_results": [{
                "candidate_id": "cfr_na_propane",
                "canonical_key": "FEI_PROPANE",
                "series_id": "price",
                "symbol": "PMAAV00",
                "dataset": "MD",
                "data_series": "History-Symbol",
                "status": "unentitled",
                "reason_code": "not_entitled_or_invalid_symbol",
            }],
            "errors": [],
        })

        self.assertEqual("success", result["status"])
        candidate = next(
            row for row in self.store.candidates() if row["canonical_key"] == "FEI_PROPANE"
        )
        self.assertEqual(("entitled", "price", "PMAAV00"), (
            candidate["discovery_status"], candidate["mapped_series_id"],
            candidate["mapped_symbol"],
        ))

    def test_valid_empty_moc_snapshot_is_not_an_ingestion_failure(self):
        result = self.service.import_platts_staging({
            "schema_version": 1,
            "generated_at": FIXED_FETCHED_AT,
            "scope": "asia",
            "status": "empty",
            "records": [],
            "entitlement_results": [{
                "candidate_id": "ewindow_asia_asia_lpg",
                "canonical_key": "EWINDOW_LPG_TRADES",
                "series_id": "platts_ewindow_asia_asia_lpg",
                "dataset": "eWMD",
                "data_series": "TradeData",
                "status": "pending_review",
                "reason_code": "empty_result",
            }],
            "errors": [],
        })

        self.assertEqual((True, "success", 0), (
            result["success"], result["status"], result["counts"]["rows_seen"],
        ))

    def test_ewindow_import_uses_stable_order_identity_for_updates(self):
        def payload(records, generated_at):
            return {
                "schema_version": 1,
                "generated_at": generated_at,
                "scope": "asia",
                "status": "success",
                "records": records,
                "entitlement_results": [],
                "errors": [],
            }

        base = {
            "record_type": "ewindow_trade",
            "dataset": "eWMD",
            "data_series": "TradeData",
            "series_id": "platts_ewindow_asia",
            "candidate_id": "ewindow_asia_asia_lpg",
            "canonical_key": "EWINDOW_LPG_TRADES",
            "assess_date": "2026-07-10",
            "scope": "asia",
            "market": "FEI",
            "product": "Propane",
            "strip": "Aug 2026",
            "currency": "USD",
            "uom": "MT",
        }
        first = self.service.import_platts_staging(payload([
            {
                **base, "order_id": "order-123", "order_time": "2026-07-10T08:00:00Z",
                "value": 520, "volume": 5_000, "status": "active",
                "mod_date": "2026-07-10T08:01:00Z",
            },
            {
                **base, "order_id": None, "order_time": "2026-07-10T08:02:00Z",
                "value": 521, "volume": 3_000, "status": "active",
                "mod_date": "2026-07-10T08:03:00Z",
            },
        ], FIXED_FETCHED_AT))
        second = self.service.import_platts_staging(payload([
            {
                **base, "order_id": "order-123", "order_time": "2026-07-10T08:00:00Z",
                "value": 522, "volume": 4_000, "status": "done",
                "mod_date": "2026-07-10T08:05:00Z",
            },
            {
                **base, "order_id": None, "order_time": "2026-07-10T08:02:00Z",
                "value": 523, "volume": 2_000, "status": "done",
                "mod_date": "2026-07-10T08:06:00Z",
            },
        ], "2026-07-10T02:00:00+00:00"))

        self.assertEqual((2, 0), (
            first["counts"]["rows_inserted"], first["counts"]["rows_updated"],
        ))
        self.assertEqual((0, 2), (
            second["counts"]["rows_inserted"], second["counts"]["rows_updated"],
        ))
        rows = self.store.explorer(
            "dataset_rows", name="platts_ewindow", limit=10,
        )["rows"]
        self.assertEqual(2, len(rows))
        self.assertIn("order-123", {row["row_key"] for row in rows})
        payloads = [json.loads(row["payload_json"]) for row in rows]
        self.assertEqual({"done"}, {row["status"] for row in payloads})
        self.assertEqual({522, 523}, {row["value"] for row in payloads})

    def test_specialized_runtime_failures_are_exposed_by_read_responses(self):
        self.add_series("fei", "FEI_PROPANE")
        self.add_observation("fei", "2026-07-10", 520)
        staging = self.scratch / "staging"
        for directory, reason in (
            ("backfill", "history_formula_failed"),
            ("curve", "curve_formula_failed"),
            ("moc", "moc_formula_failed"),
        ):
            target = staging / directory
            target.mkdir(parents=True)
            (target / "status.json").write_text(json.dumps({
                "schema_version": 1,
                "updated_at": FIXED_FETCHED_AT,
                "scope": "asia",
                "state": "failed",
                "record_count": 0,
                "error_count": 1,
                "reason_codes": [reason],
            }), encoding="utf-8")
        (staging / "status.json").write_text(json.dumps({
            "schema_version": 1,
            "updated_at": FIXED_FETCHED_AT,
            "scope": "asia",
            "state": "failed",
            "record_count": 0,
            "error_count": 1,
            "reason_codes": ["daily_formula_failed"],
        }), encoding="utf-8")

        with patch("lpg.platts_excel.STAGING_DIR", staging):
            history = self.service.series_history("fei")
            curves = self.service.curves()
            moc = self.service.explorer(dataset="moc")
            status = self.service.status()

        self.assertEqual(("failed", "available_with_warning", "history_formula_failed"), (
            history["refresh_state"], history["availability"]["status"],
            history["availability"]["reason"],
        ))
        self.assertEqual("limited", history["availability"]["coverage_status"])
        self.assertEqual(("failed", "refresh_failed", "daily_formula_failed"), (
            curves["refresh_state"], curves["dataset_status"]["status"],
            curves["dataset_status"]["reason"],
        ))
        self.assertEqual(("failed", "refresh_failed", "moc_formula_failed"), (
            moc["refresh_state"], moc["dataset_status"]["status"],
            moc["dataset_status"]["reason"],
        ))
        self.assertEqual("available_with_warning", status["datasets"]["history"]["status"])
        self.assertEqual("limited", status["datasets"]["history"]["availability_group"])
        self.assertTrue(status["datasets"]["history"]["visible_now"])
        self.assertEqual("refresh_failed", status["datasets"]["curves"]["status"])
        self.assertEqual("refresh_failed", status["datasets"]["moc"]["status"])
        self.assertEqual({"history", "curves", "moc"}, set(status["specialized_runtime"]))

    def test_news_schema_ranking_cluster_alias_and_source_health(self):
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.store.upsert_news({
            "article_key": "ranked-news", "headline": "Asia LPG cargo disruption",
            "source": "fixture", "published_at": now,
            "relevance_score": 82, "rank_score": 88, "source_tier": 3,
            "cluster_key": "evt_fixture", "is_breaking": True,
            "importance": "high", "entitlement_state": "entitled",
        })
        self.service.upsert_news_source_health({
            "source_id": "fixture_feed", "source_name": "Fixture Feed",
            "kind": "rss", "status": "healthy", "last_attempt_at": now,
            "last_success_at": now, "latest_published_at": now,
            "article_count": 2, "relevant_count": 1, "latency_ms": 12,
            "metadata": {"production_sla": False},
        })

        payload = self.service.news({"cluster_id": "evt_fixture"})
        self.assertEqual(["ranked-news"], [row["article_key"] for row in payload["items"]])
        item = payload["items"][0]
        self.assertEqual("breaking", item["freshness"])
        self.assertTrue(item["is_breaking"])
        self.assertGreater(item["rank_score"], 0)
        self.assertEqual("healthy", payload["source_health"][0]["status"])
        self.assertEqual("lpg_relevance_freshness_source", payload["ranking"]["strategy"])

        self.store.upsert_news({
            "article_key": "unconfirmed-news",
            "headline": "Unconfirmed LPG terminal outage",
            "source": "discovery fixture", "published_at": now,
            "relevance_score": 90, "source_tier": 2,
            "is_breaking": False, "importance": "high",
            "entitlement_state": "entitled",
        })
        unconfirmed = self.service.news({"q": "Unconfirmed"})["items"][0]
        self.assertEqual("breaking", unconfirmed["freshness"])
        self.assertFalse(unconfirmed["is_breaking"])

        self.store.upsert_news({
            "article_key": "undated-news", "headline": "Undated LPG item",
            "source": "fixture", "published_at": "1970-01-01T00:00:00+00:00",
            "metadata": {"date_quality": "inferred"},
            "entitlement_state": "entitled",
        })
        undated = self.service.news({"q": "Undated"})["items"][0]
        self.assertEqual("unknown", undated["freshness"])
        self.assertFalse(undated["is_breaking"])

        # Simulate an interrupted migration where columns/tables exist but the
        # version marker was not committed; reinitialization must be recoverable.
        with self.store._transaction() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=3")
        self.store.initialize()

        with self.store._reader() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(news)")}
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"relevance_score", "rank_score", "source_tier",
                         "cluster_key", "is_breaking"}.issubset(columns))
        self.assertEqual(5, version)


if __name__ == "__main__":
    unittest.main()
