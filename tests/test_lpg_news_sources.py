import os
import unittest
from unittest.mock import patch

from _support import WorkspaceScratchMixin
from lpg.news_sources import (
    PlattsNewsClient,
    dedupe_articles,
    load_local_env,
    normalize_article,
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

    def test_boolean_feed_summary_is_not_rendered_as_true(self):
        row = normalize_article({
            "title": "LPG market update",
            "summary": True,
            "url": "https://example.test/lpg",
        }, "public", "public")
        self.assertEqual("", row["summary"])

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
