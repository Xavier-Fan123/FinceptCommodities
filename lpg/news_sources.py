"""LPG news adapters for official Platts API delivery and public fallback.

The Excel Add-in does not expose news functions. Platts content is therefore
read only from a separately entitled machine-to-machine API. Website scraping
and reuse of interactive-login credentials are deliberately unsupported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


ROOT = Path(__file__).resolve().parent.parent

_REGION_TERMS = {
    "asia": ("asia", "china", "japan", "korea", "singapore", "india", "philippines", "vietnam"),
    "middle_east": ("saudi", "aramco", "arab gulf", "qatar", "uae", "iran", "middle east"),
    "united_states": ("united states", "u.s.", "us gulf", "mont belvieu", "texas", "conway"),
    "europe": ("europe", "ara", "northwest europe", "mediterranean"),
}
_PRODUCT_TERMS = {
    "propane": ("propane", "c3", "fei"),
    "butane": ("butane", "c4", "isobutane", "normal butane"),
    "lpg": ("lpg", "liquefied petroleum gas"),
    "ngl": ("ngl", "natural gas liquids"),
    "naphtha": ("naphtha", "mopj"),
    "propylene": ("propylene", "pdh"),
}
_DRIVER_TERMS = {
    "supply": ("supply", "production", "output", "export", "cargo", "loading"),
    "demand": ("demand", "import", "buying", "consumption", "heating"),
    "freight": ("vlgc", "freight", "shipping", "tanker", "panama", "hormuz"),
    "petrochemicals": ("pdh", "cracker", "petrochemical", "propylene", "feedstock"),
    "storage": ("inventory", "inventories", "stock", "storage"),
    "outage": ("outage", "shutdown", "maintenance", "force majeure", "disruption"),
    "pricing": ("saudi cp", "contract price", "fei", "assessment", "premium", "discount"),
    "policy": ("sanction", "tariff", "policy", "regulation", "quota"),
}
_BULLISH = ("outage", "shutdown", "disruption", "tight", "shortage", "draw", "cut", "surge")
_BEARISH = ("surplus", "oversupply", "weak demand", "build", "restart", "rise in output", "glut")
_HIGH_PRIORITY = ("saudi cp", "aramco", "fei", "mont belvieu", "vlgc", "hormuz", "force majeure")


def load_local_env(path: Optional[Path] = None) -> None:
    """Load simple KEY=VALUE entries without adding a dotenv dependency."""
    env_path = path or ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _matches(text: str, taxonomy: Dict[str, Iterable[str]]) -> List[str]:
    return [name for name, terms in taxonomy.items() if any(term in text for term in terms)]


def tag_article(article: Dict[str, Any]) -> Dict[str, Any]:
    """Attach deterministic trader tags without inventing an article summary."""
    text = " ".join(str(article.get(key) or "") for key in ("title", "summary", "body")).lower()
    bull = sum(term in text for term in _BULLISH)
    bear = sum(term in text for term in _BEARISH)
    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    high_hits = sum(term in text for term in _HIGH_PRIORITY)
    driver_count = len(_matches(text, _DRIVER_TERMS))
    importance = "high" if high_hits >= 2 else "medium" if high_hits or driver_count >= 2 else "low"
    return {
        **article,
        "regions": _matches(text, _REGION_TERMS),
        "products": _matches(text, _PRODUCT_TERMS),
        "drivers": _matches(text, _DRIVER_TERMS),
        "direction": direction,
        "importance": importance,
    }


def _iso_date(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(timezone.utc).isoformat()


def _article_id(source: str, title: str, url: str) -> str:
    raw = f"{source}\n{title}\n{url}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def _text(value: Any) -> str:
    """Normalize feed text without turning boolean flags into headlines."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def normalize_article(raw: Dict[str, Any], provider: str, entitlement: str) -> Dict[str, Any]:
    source = _text(raw.get("source") or raw.get("publisher") or provider)
    title = _text(raw.get("title") or raw.get("headline"))
    url = _text(raw.get("url") or raw.get("link") or raw.get("webUrl"))
    external_id = str(raw.get("id") or raw.get("articleId") or _article_id(source, title, url))
    article = {
        "id": external_id,
        "provider": provider,
        "source": source,
        "title": title,
        "summary": _text(raw.get("summary") or raw.get("description") or raw.get("snippet")),
        "body": _text(raw.get("body") or raw.get("content")),
        "url": url,
        "published_at": _iso_date(raw.get("published_at") or raw.get("published") or raw.get("publishDate")),
        "entitlement": entitlement,
        "raw": raw,
    }
    return tag_article(article)


def dedupe_articles(articles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for article in articles:
        key = article.get("id") or re.sub(r"[^a-z0-9]", "", article.get("title", "").lower())[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out


class PlattsNewsClient:
    """Configurable OAuth client for a contracted S&P news API dataset.

    S&P supplies endpoint and response details with the entitlement. The
    parser accepts the common items/articles/results/data envelope shapes and
    keeps the unmodified record for later schema-specific mapping.
    """

    def __init__(self, session: Optional[requests.Session] = None):
        load_local_env()
        self.api_url = os.environ.get("PLATTS_NEWS_API_URL", "").strip()
        self.token_url = os.environ.get("PLATTS_NEWS_TOKEN_URL", "").strip()
        self.client_id = os.environ.get("PLATTS_NEWS_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("PLATTS_NEWS_CLIENT_SECRET", "").strip()
        self.scope = os.environ.get("PLATTS_NEWS_SCOPE", "").strip()
        self.static_token = os.environ.get("PLATTS_NEWS_API_TOKEN", "").strip()
        self.session = session or requests.Session()
        self.session.trust_env = False

    @property
    def configured(self) -> bool:
        return bool(self.api_url and (self.static_token or (self.token_url and self.client_id and self.client_secret)))

    def _token(self) -> str:
        if self.static_token:
            return self.static_token
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        response = self.session.post(
            self.token_url,
            data=data,
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Platts token response did not contain access_token")
        return str(token)

    def fetch(self, start: Optional[str] = None, end: Optional[str] = None,
              page_size: int = 100, max_pages: int = 100) -> Dict[str, Any]:
        if not self.configured:
            return {"configured": False, "articles": [], "error": "Platts news API is not configured"}
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        articles: List[Dict[str, Any]] = []
        page = 1
        next_url: Optional[str] = self.api_url
        while next_url and page <= max_pages:
            params: Dict[str, Any] = {"page": page, "pageSize": max(1, min(page_size, 500)),
                                      "query": "LPG OR propane OR butane OR NGL OR FEI OR Saudi CP OR VLGC"}
            if start:
                params["from"] = start
            if end:
                params["to"] = end
            response = self.session.get(next_url, params=params if next_url == self.api_url else None,
                                        headers=headers, timeout=45)
            response.raise_for_status()
            payload = response.json()
            rows: Any = payload
            if isinstance(payload, dict):
                for key in ("items", "articles", "results", "data"):
                    if isinstance(payload.get(key), list):
                        rows = payload[key]
                        break
            if not isinstance(rows, list):
                raise RuntimeError("Unsupported Platts news response envelope")
            articles.extend(normalize_article(row, "platts", "entitled") for row in rows if isinstance(row, dict))
            explicit_next = payload.get("next") if isinstance(payload, dict) else None
            if explicit_next:
                next_url = str(explicit_next)
                page += 1
            elif len(rows) >= params["pageSize"]:
                page += 1
                next_url = self.api_url
            else:
                break
        return {"configured": True, "articles": dedupe_articles(articles), "error": None}


def public_lpg_news(limit: int = 80) -> Dict[str, Any]:
    """Reuse the existing public RSS aggregator as an explicitly labelled fallback."""
    import energy_chemicals

    payload = energy_chemicals.news_payload("lpg_ngl", None, max(5, min(limit, 100)))
    articles = [normalize_article(row, "public", "public") for row in payload.get("articles", [])]
    return {
        "configured": True,
        "articles": dedupe_articles(articles),
        "source_status": payload.get("source_status") or {},
        "sources": payload.get("sources") or [],
        "error": None,
    }


def serialize_raw(article: Dict[str, Any]) -> str:
    return json.dumps(article.get("raw") or {}, ensure_ascii=False, sort_keys=True, default=str)


def to_news_input(article: Dict[str, Any]) -> Dict[str, Any]:
    """Map a normalized source article to the SQLite NewsInput contract."""
    regions = [str(item) for item in (article.get("regions") or []) if item]
    products = [str(item) for item in (article.get("products") or []) if item]
    drivers = [str(item) for item in (article.get("drivers") or []) if item]
    entitlement = str(article.get("entitlement") or "pending_review").lower()
    if entitlement in {"public", "licensed", "available", "ok"}:
        entitlement = "entitled"
    if entitlement not in {"entitled", "unentitled", "pending_review", "retired", "error"}:
        entitlement = "pending_review"
    tags = list(dict.fromkeys([*regions, *products, *drivers]))
    return {
        "article_key": str(article.get("id") or _article_id(
            str(article.get("source") or article.get("provider") or "news"),
            str(article.get("title") or ""), str(article.get("url") or ""),
        )),
        "headline": str(article.get("title") or article.get("headline") or "Untitled"),
        "source": str(article.get("source") or article.get("provider") or "news"),
        "published_at": _iso_date(article.get("published_at")),
        "url": str(article.get("url") or "") or None,
        "summary": str(article.get("summary") or "") or None,
        "body": str(article.get("body") or "") or None,
        "language": str(article.get("language") or "en"),
        "region": regions[0] if regions else None,
        "product": products[0] if products else None,
        "topic": drivers[0] if drivers else "lpg",
        "direction": str(article.get("direction") or "neutral"),
        "importance": str(article.get("importance") or "low"),
        "tags": tags,
        "entitlement_state": entitlement,
        "metadata": {
            "provider": article.get("provider"),
            "regions": regions,
            "products": products,
            "drivers": drivers,
            "raw": article.get("raw") or {},
        },
    }
