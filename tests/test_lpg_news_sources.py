import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from _support import WorkspaceScratchMixin
from lpg.news_sources import (
    PlattsNewsClient,
    PublicNewsAggregator,
    _fetch_rss_feed,
    dedupe_articles,
    freshness_metadata,
    load_local_env,
    normalize_article,
    public_feed_definitions,
    tag_article,
    to_news_input,
)


class NewsSourceTests(WorkspaceScratchMixin, unittest.TestCase):
    def test_trader_tags(self):
        tagged = tag_article({
            "title": "Saudi CP propane rises as VLGC disruption tightens Asian supply",
            "summary": "PDH buyers in China seek replacement cargoes.",
        })
        self.assertIn("asia", tagged["regions"])
        self.assertIn("middle_east", tagged["regions"])
        self.assertIn("propane", tagged["products"])
        self.assertIn("freight", tagged["drivers"])
        self.assertEqual("high", tagged["importance"])

    def test_normalize_and_dedupe(self):
        row = normalize_article({"headline": "LPG market", "link": "https://example.test/a"}, "public", "public")
        self.assertTrue(row["id"])
        self.assertEqual("LPG market", row["title"])
        self.assertEqual(1, len(dedupe_articles([row, dict(row)])))

    def test_timestamp_normalization_and_live_freshness(self):
        row = normalize_article({
            "title": "Asia propane cargo prices rise",
            "published": "Mon, 13 Jul 2026 10:30:00 +0800",
        }, "public", "public")
        self.assertEqual("2026-07-13T02:30:00+00:00", row["published_at"])
        freshness = freshness_metadata(
            row["published_at"], now=datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(("breaking", 90),
                         (freshness["freshness"], freshness["age_minutes"]))

    def test_missing_publication_time_is_unknown_not_fresh(self):
        row = normalize_article({
            "title": "Asia LPG cargo supply update without timestamp",
        }, "public", "public")
        self.assertEqual("1970-01-01T00:00:00+00:00", row["published_at"])
        self.assertEqual("inferred", row["date_quality"])
        self.assertEqual("unknown", row["freshness"])
        self.assertFalse(row["is_breaking"])

        future = normalize_article({
            "title": "Asia LPG cargo update with invalid future date",
            "published": "2999-01-01T00:00:00Z",
        }, "public", "public")
        self.assertEqual("1970-01-01T00:00:00+00:00", future["published_at"])
        self.assertEqual("future_corrected", future["date_quality"])
        self.assertFalse(future["is_breaking"])

    def test_rss_leaf_nodes_preserve_description_and_publication_time(self):
        class Response:
            content = b"""<?xml version='1.0'?><rss><channel><item>
                <title>Asia propane cargo premiums rise</title>
                <description>Supply tightens after terminal maintenance.</description>
                <link>https://publisher.test/story</link>
                <pubDate>Mon, 13 Jul 2026 10:30:00 +0800</pubDate>
                <source>Fixture Publisher</source>
            </item></channel></rss>"""

            def raise_for_status(self):
                return None

        class Session:
            trust_env = True

            def get(self, *args, **kwargs):
                return Response()

        with patch("lpg.news_sources.requests.Session", return_value=Session()):
            rows = _fetch_rss_feed({
                "id": "fixture", "source": "Fixture", "url": "https://feed.test/rss",
            })
        self.assertEqual("Mon, 13 Jul 2026 10:30:00 +0800", rows[0]["published"])
        self.assertEqual("Supply tightens after terminal maintenance.", rows[0]["summary"])

    def test_relevance_gate_rejects_consumer_noise(self):
        market = normalize_article({
            "title": "Asia propane cargo premiums rise as supply tightens",
            "published": "2026-07-13T00:00:00Z",
        }, "public", "public")
        noise = normalize_article({
            "title": "Best propane grill barbecue recipe for summer",
            "published": "2026-07-13T00:00:00Z",
        }, "public", "public")
        self.assertTrue(market["is_relevant"])
        self.assertFalse(noise["is_relevant"])
        self.assertGreater(market["relevance_score"], noise["relevance_score"])

    def test_exact_dedupe_and_near_duplicate_event_clustering(self):
        rows = [normalize_article(item, "public", "public") for item in (
            {"title": "Saudi Aramco raises July propane CP on tight supply",
             "source": "Publisher A", "url": "https://a.test/one",
             "published": "2026-07-13T01:00:00Z"},
            {"title": "Saudi Aramco raises July propane CP on tight supply",
             "source": "Publisher B", "url": "https://b.test/two",
             "published": "2026-07-13T01:05:00Z"},
            {"title": "Saudi Aramco lifts July propane CP amid tight supply",
             "source": "Publisher C", "url": "https://c.test/three",
             "published": "2026-07-13T01:10:00Z"},
        )]
        result = dedupe_articles(rows)
        self.assertEqual(2, len(result))
        self.assertEqual({2}, {row["cluster_size"] for row in result})
        self.assertEqual(2, max(row["duplicate_count"] for row in result))
        self.assertEqual(1, len({row["cluster_key"] for row in result}))

    def test_single_discovery_hit_is_developing_not_confirmed_breaking(self):
        article = normalize_article({
            "title": "Breaking Saudi CP propane export halt after terminal outage",
            "source": "Reuters", "feed_id": "google_asia_lpg",
            "published": datetime.now(timezone.utc).isoformat(),
        }, "public", "public")
        self.assertTrue(article["is_breaking"])
        clustered = dedupe_articles([article])[0]
        self.assertFalse(clustered["is_breaking"])
        self.assertEqual("developing", clustered["confirmation_state"])

    def test_public_sources_are_concurrent_isolated_and_labelled(self):
        feeds = [
            {"id": "google", "source": "Google News", "url": "https://g.test",
             "kind": "search_rss", "role": "discovery_fallback"},
            {"id": "gdelt", "source": "GDELT DOC 2.0", "url": "https://d.test",
             "kind": "gdelt_json", "role": "multilingual_discovery"},
            {"id": "broken", "source": "Broken", "url": "https://x.test"},
        ]

        def fetcher(feed):
            if feed["id"] == "broken":
                raise RuntimeError("offline")
            return [{
                "title": f"Asia LPG cargo supply update {feed['id']}",
                "source": f"Publisher {feed['id']}",
                "published": datetime.now(timezone.utc).isoformat(),
                "url": f"https://publisher.test/{feed['id']}",
                "feed_id": feed["id"],
            }]

        payload = PublicNewsAggregator(feeds=feeds, fetcher=fetcher).fetch(limit=20)
        self.assertEqual(2, len(payload["articles"]))
        self.assertEqual({"ok": 2, "failed": 1, "degraded": False, "feeds": 3,
                          "configured_feeds": 3},
                         payload["source_status"])
        health = {row["source_id"]: row for row in payload["sources"]}
        self.assertEqual("error", health["broken"]["status"])
        self.assertEqual("discovery_fallback", health["google"]["metadata"]["role"])

    def test_gdelt_queries_are_rotated_not_bursted_concurrently(self):
        calls = []
        feeds = [
            {"id": "google", "source": "Google", "url": "https://g.test", "kind": "search_rss"},
            {"id": "gdelt_asia", "source": "GDELT", "url": "https://d.test/a", "kind": "gdelt_json"},
            {"id": "gdelt_us", "source": "GDELT", "url": "https://d.test/u", "kind": "gdelt_json"},
        ]

        def fetcher(feed):
            calls.append(feed["id"])
            return [{"title": "Asia LPG cargo supply update", "source": feed["source"],
                     "published": datetime.now(timezone.utc).isoformat()}]

        payload = PublicNewsAggregator(feeds=feeds, fetcher=fetcher).fetch(limit=20)
        self.assertEqual(1, len([source for source in calls if source.startswith("gdelt_")]))
        self.assertEqual(2, payload["source_status"]["feeds"])
        self.assertEqual(3, payload["source_status"]["configured_feeds"])

    def test_default_public_discovery_is_diverse_and_no_sla(self):
        feeds = public_feed_definitions()
        kinds = {feed["kind"] for feed in feeds}
        self.assertTrue({"search_rss", "gdelt_json", "official_rss"}.issubset(kinds))
        self.assertTrue(any(feed["id"] == "aramco_news" for feed in feeds))
        self.assertTrue(any(feed["id"] == "eia_propane" for feed in feeds))
        self.assertTrue(all(feed.get("production_sla") == "false" for feed in feeds))

    def test_boolean_feed_summary_is_not_rendered_as_true(self):
        row = normalize_article({
            "title": "LPG market update",
            "summary": True,
            "url": "https://example.test/lpg",
        }, "public", "public")
        self.assertEqual("", row["summary"])

    def test_public_article_raw_metadata_drops_full_content(self):
        row = normalize_article({
            "title": "Asia propane cargo market update",
            "published": "2026-07-13T00:00:00Z",
            "content": "licensed-looking full article " * 500,
            "description": "Feed excerpt " * 300,
        }, "public", "public")
        self.assertEqual("", row["body"])
        self.assertNotIn("content", row["raw"])
        self.assertLessEqual(len(row["raw"]["description"]), 1200)

    def test_local_env_does_not_override_process(self):
        path = self.scratch / "news.env"
        path.write_text("PLATTS_NEWS_API_URL=https://file.test\n", encoding="utf-8")
        old = os.environ.get("PLATTS_NEWS_API_URL")
        try:
            os.environ["PLATTS_NEWS_API_URL"] = "https://process.test"
            load_local_env(path)
            self.assertEqual("https://process.test", os.environ["PLATTS_NEWS_API_URL"])
        finally:
            if old is None:
                os.environ.pop("PLATTS_NEWS_API_URL", None)
            else:
                os.environ["PLATTS_NEWS_API_URL"] = old

    def test_adapter_maps_normalized_article_to_news_contract(self):
        article = normalize_article({
            "articleId": "platts-123",
            "publisher": "S&P Global Commodity Insights",
            "headline": "Saudi CP propane tightens as VLGC disruption hits Asia",
            "description": "China PDH buyers seek replacement cargoes.",
            "webUrl": "https://example.test/news/123",
            "publishDate": "2026-07-09T12:30:00Z",
            "language": "en",
        }, "platts", "licensed")
        mapped = to_news_input(article)

        self.assertEqual("platts-123", mapped["article_key"])
        self.assertEqual(article["title"], mapped["headline"])
        self.assertEqual("entitled", mapped["entitlement_state"])
        self.assertEqual("asia", mapped["region"])
        self.assertEqual("propane", mapped["product"])
        self.assertEqual("supply", mapped["topic"])
        self.assertEqual("high", mapped["importance"])
        self.assertEqual(len(mapped["tags"]), len(set(mapped["tags"])))
        self.assertEqual("platts", mapped["metadata"]["provider"])
        self.assertEqual("platts-123", mapped["metadata"]["raw"]["articleId"])
        self.assertGreater(mapped["relevance_score"], 0)
        self.assertEqual(5, mapped["source_tier"])
        self.assertEqual("licensed_machine_readable", mapped["metadata"]["content_boundary"])

    def test_client_maps_items_envelope_without_network(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"items": [{
                    "id": "news-1", "headline": "FEI propane rises in Asia",
                    "publishDate": "2026-07-09T00:00:00Z",
                }]}

        class Session:
            trust_env = True

            def __init__(self):
                self.calls = []

            def get(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return Response()

        session = Session()
        with patch("lpg.news_sources.load_local_env"), patch.dict(os.environ, {
                "PLATTS_NEWS_API_URL": "https://api.example.test/news",
                "PLATTS_NEWS_API_TOKEN": "offline-token",
        }):
            client = PlattsNewsClient(session=session)
            result = client.fetch(page_size=20, max_pages=1)

        self.assertTrue(result["configured"])
        self.assertEqual(1, len(result["articles"]))
        self.assertEqual("news-1", result["articles"][0]["id"])
        self.assertEqual("entitled", result["articles"][0]["entitlement"])
        self.assertFalse(session.trust_env)
        self.assertEqual("Bearer offline-token",
                         session.calls[0][1]["headers"]["Authorization"])

    def test_unknown_adapter_entitlement_is_pending_review(self):
        mapped = to_news_input(normalize_article(
            {"headline": "LPG fixture", "publishDate": "2026-07-09T00:00:00Z"},
            "fixture", "mystery-state",
        ))
        self.assertEqual("pending_review", mapped["entitlement_state"])

    def test_unconfigured_client_is_explicit(self):
        keys = ["PLATTS_NEWS_API_URL", "PLATTS_NEWS_TOKEN_URL", "PLATTS_NEWS_CLIENT_ID",
                "PLATTS_NEWS_CLIENT_SECRET", "PLATTS_NEWS_API_TOKEN"]
        saved = {key: os.environ.pop(key) for key in keys if key in os.environ}
        try:
            with patch("lpg.news_sources.load_local_env"):
                client = PlattsNewsClient()
            self.assertFalse(client.configured)
            self.assertIn("not configured", client.fetch()["error"])
        finally:
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
