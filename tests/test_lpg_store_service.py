import unittest

from _support import WorkspaceScratchMixin
from lpg.service import LpgService


FIXED_FETCHED_AT = "2026-07-10T01:00:00+00:00"


class StoreServiceTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = LpgService(db_path=self.scratch / "lpg.sqlite")
        self.store = self.service.store

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


if __name__ == "__main__":
    unittest.main()
