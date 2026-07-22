import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from _support import WorkspaceScratchMixin
from lpg.intelligence import baseline_alerts, build_events, run_scenario, scenario_catalog
from lpg.service import LpgService


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def article(key, headline, source="Fixture Wire", published=None, **extra):
    value = {
        "article_key": key,
        "headline": headline,
        "source": source,
        "published_at": (published or NOW).isoformat(),
        "entitlement_state": "entitled",
        "importance": "high",
        "relevance_score": 86,
        "rank_score": 90,
        "source_tier": 4,
        "cluster_key": "evt-terminal",
        "is_breaking": True,
        "direction": "bullish",
        "metadata": {
            "cluster_size": 2,
            "cluster_sources": ["Fixture Wire", "Second Source"],
            "confirmation_state": "confirmed",
            "content_boundary": "public_source",
        },
    }
    value.update(extra)
    return value


class IntelligenceAlgorithmTests(unittest.TestCase):
    def test_scenario_engine_is_bounded_transparent_and_non_predictive(self):
        catalog = scenario_catalog()
        result = run_scenario("hormuz_closure", {
            "shock_pct": 100, "duration_days": 14, "extra_transit_days": 14,
        })

        self.assertEqual((6, "hypothetical_not_forecast"), (
            len(catalog["templates"]), catalog["state"],
        ))
        self.assertEqual((73, "severe"), (
            result["stress_index"]["score"], result["stress_index"]["band"],
        ))
        self.assertIn("0.50*shock_pct/100", result["stress_index"]["formula"])
        self.assertIn("hormuz", result["asset_ids"])
        self.assertIn("FEI_PROPANE", result["affected_series"])
        self.assertEqual("hypothetical_not_forecast", result["scenario_state"])
        self.assertNotIn("price_forecast", result)

        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            run_scenario("hormuz_closure", {"shock_pct": 101})
        with self.assertRaisesRegex(ValueError, "unsupported scenario input"):
            run_scenario("hormuz_closure", {"price_change": 50})

    def test_explicit_location_cluster_builds_confirmed_trader_event(self):
        rows = [
            article("one", "Ras Tanura terminal outage delays LPG loading"),
            article("two", "LPG loading delayed at Ras Tanura", source="Second Source"),
        ]
        events = build_events(rows, now=NOW)

        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(("ras_tanura", "critical", "confirmed"), (
            event["asset_ids"][0], event["severity"], event["confirmation_state"],
        ))
        self.assertEqual("terminal_disruption", event["event_type"])
        self.assertIn("ag_north_asia_malacca", event["route_ids"])
        self.assertIn("CP_PROPANE", event["affected_series"])
        self.assertNotIn("single_source_not_corroborated", event["data_gaps"])
        self.assertEqual("inferred_not_official_assessment",
                         event["impact"]["interpretation_state"])

    def test_unresolved_single_source_stays_developing_with_visible_gaps(self):
        row = article(
            "one", "Unexpected LPG market development raises uncertainty",
            source="Single Blog", cluster_key="evt-unknown", is_breaking=False,
            importance="medium", relevance_score=48, source_tier=1,
            metadata={"cluster_size": 1, "cluster_sources": ["Single Blog"],
                      "confirmation_state": "developing", "content_boundary": "public_source"},
        )
        event = build_events([row], now=NOW)[0]

        self.assertIsNone(event["latitude"])
        self.assertEqual("developing", event["confirmation_state"])
        self.assertLessEqual(event["confidence_score"], 74)
        self.assertIn("event_location_not_resolved", event["data_gaps"])
        self.assertIn("single_source_not_corroborated", event["data_gaps"])

    def test_baseline_alert_requires_history_volume_and_source_diversity(self):
        events = []
        for index in range(4):
            events.append({
                "event_type": "shipping_disruption",
                "last_seen_at": (NOW - timedelta(days=index + 1)).isoformat(),
                "sources": [f"Baseline {index}"],
            })
        events.extend([
            {"event_type": "shipping_disruption",
             "last_seen_at": (NOW - timedelta(minutes=30)).isoformat(), "sources": ["Wire A"]},
            {"event_type": "shipping_disruption",
             "last_seen_at": (NOW - timedelta(minutes=60)).isoformat(), "sources": ["Wire B"]},
        ])

        result = baseline_alerts(events, now=NOW)
        self.assertEqual("ready", result["coverage_state"])
        self.assertEqual("surge", result["alerts"][0]["state"])
        self.assertEqual(2, result["alerts"][0]["source_diversity"])


class IntelligencePersistenceTests(WorkspaceScratchMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = LpgService(db_path=self.scratch / "lpg.sqlite")
        self.staging_patch = patch(
            "lpg.platts_excel.STAGING_DIR", self.scratch / "staging",
        )
        self.staging_patch.start()
        self.addCleanup(self.staging_patch.stop)

    def test_refresh_persists_idempotent_events_and_situation_contract(self):
        for row in (
            article("one", "Ras Tanura terminal outage delays LPG loading"),
            article("two", "LPG loading delayed at Ras Tanura", source="Second Source"),
        ):
            self.service.upsert_news(row)

        first = self.service.refresh_intelligence()
        second = self.service.refresh_intelligence()
        situation = self.service.situation()

        self.assertEqual(5, self.service.store.status()["database"]["schema_version"])
        self.assertEqual((1, 1, 0), (
            first["counts"]["inserted"], second["counts"]["updated"], second["counts"]["failed"],
        ))
        self.assertEqual(1, situation["total"])
        self.assertTrue(situation["assets"])
        self.assertTrue(situation["routes"])
        self.assertEqual(6, len(situation["scenario_engine"]["templates"]))
        self.assertEqual("curated reference corridors; not live AIS tracks",
                         situation["methodology"]["route_state"])
        self.assertEqual("ras_tanura", situation["events"][0]["asset_ids"][0])
        self.assertEqual("unavailable", situation["intelligence_gaps"][0]["status"])


if __name__ == "__main__":
    unittest.main()
