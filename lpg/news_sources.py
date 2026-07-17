"""Entitlement-aware LPG news ingestion, ranking, and event clustering.

The official Platts adapter only reads a separately contracted machine-to-
machine news API.  Public RSS feeds are an explicitly labelled fallback; this
module never scrapes Platts pages or reuses an interactive login.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


ROOT = Path(__file__).resolve().parent.parent
_UNKNOWN_PUBLISHED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)

_REGION_TERMS = {
    "asia": ("asia", "china", "japan", "korea", "singapore", "india", "philippines",
             "vietnam", "indonesia", "thailand", "malaysia", "taiwan"),
    "middle_east": ("saudi", "aramco", "arab gulf", "persian gulf", "qatar", "uae",
                    "iran", "middle east", "kuwait"),
    "united_states": ("united states", "u.s.", "us gulf", "u.s. gulf", "mont belvieu",
                      "texas", "conway", "marcus hook"),
    "europe": ("europe", "ara", "northwest europe", "mediterranean"),
}
_PRODUCT_TERMS = {
    "propane": ("propane", "c3", "fei"),
    "butane": ("butane", "c4", "isobutane", "normal butane", "n-butane"),
    "lpg": ("lpg", "liquefied petroleum gas"),
    "ngl": ("ngl", "ngls", "natural gas liquids"),
    "naphtha": ("naphtha", "mopj"),
    "propylene": ("propylene", "pdh"),
}
_DRIVER_TERMS = {
    "supply": ("supply", "production", "output", "export", "cargo", "cargoes", "loading",
               "terminal"),
    "demand": ("demand", "import", "buying", "consumption", "heating", "tender"),
    "freight": ("vlgc", "freight", "shipping", "tanker", "panama", "hormuz", "fixture"),
    "petrochemicals": ("pdh", "cracker", "petrochemical", "propylene", "feedstock"),
    "storage": ("inventory", "inventories", "stock", "storage"),
    "outage": ("outage", "shutdown", "maintenance", "force majeure", "disruption", "fire"),
    "pricing": ("saudi cp", "contract price", "fei", "assessment", "premium", "discount",
                "mont belvieu", "arbitrage"),
    "policy": ("sanction", "tariff", "policy", "regulation", "quota"),
}
_BULLISH = ("outage", "shutdown", "disruption", "tight", "shortage", "draw", "cut",
            "surge", "force majeure", "export halt", "delay")
_BEARISH = ("surplus", "oversupply", "weak demand", "build", "restart", "rise in output",
            "glut", "run cut", "demand falls")
_HIGH_PRIORITY = ("saudi cp", "aramco", "fei", "mont belvieu", "vlgc", "hormuz",
                  "force majeure", "terminal outage", "export halt")
_BREAKING_TERMS = ("breaking", "just in", "force majeure", "explosion", "attack", "halt",
                   "shutdown", "outage", "sanctions", "closes", "reopens")

# Scores deliberately favour LPG-specific market language.  Related naphtha or
# propylene stories need additional market context to clear the relevance bar.
_CORE_RELEVANCE = {
    "liquefied petroleum gas": 36, "saudi cp": 34, "contract price": 22,
    "mont belvieu": 30, "lpg": 34, "vlgc": 28, "fei": 28,
    "propane": 25, "butane": 25, "isobutane": 24, "n-butane": 24,
    "natural gas liquids": 24, "ngl": 22, "pdh": 19, "propylene": 11,
    "naphtha": 9,
}
_MARKET_CONTEXT = ("price", "prices", "market", "cargo", "cargoes", "export", "import",
                   "supply", "demand", "premium", "discount", "assessment", "tender",
                   "terminal", "freight", "shipping", "inventory", "production", "plant",
                   "cracker", "feedstock", "arbitrage", "trade", "trading")
_NEGATIVE_CONTEXT = ("propane grill", "barbecue recipe", "bbq recipe", "camping stove",
                     "patio heater", "lpg conversion kit", "video game", "celebrity")
_SOURCE_TIER_HINTS = {
    "platts": 5, "s&p global commodity insights": 5,
    "eia": 4, "u.s. energy information administration": 4, "iea": 4,
    "saudi aramco": 4, "national hurricane center": 4,
    "reuters": 4, "argus": 4, "opis": 4,
    "bloomberg": 3, "cnbc": 3, "oilprice": 3, "rigzone": 3,
}
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "gclid", "fbclid", "mc_cid", "mc_eid"}
_TITLE_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "of", "on",
    "or", "the", "to", "with", "after", "amid", "says", "update", "latest", "market",
}


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


def _text(value: Any) -> str:
    """Normalize feed text without turning boolean flags into headlines."""
    if value is None or isinstance(value, bool):
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def _strip_html(value: Any) -> str:
    return _text(re.sub(r"<[^>]+>", " ", _text(value)))


def _contains(text: str, term: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def _matches(text: str, taxonomy: Mapping[str, Iterable[str]]) -> List[str]:
    return [name for name, terms in taxonomy.items() if any(_contains(text, term) for term in terms)]


def _parse_datetime(value: Any, fallback: Optional[datetime] = None) -> tuple[datetime, str]:
    quality = "reported"
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        raw = _text(value)
        dt = None
        if raw:
            try:
                dt = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                pass
            if dt is None:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
            if dt is None:
                try:
                    dt = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
        if dt is None:
            # Unknown is intentionally old, not "now": otherwise an undated
            # feed item is promoted as fresh again on every polling cycle.
            dt = fallback or _UNKNOWN_PUBLISHED_AT
            quality = "inferred"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        quality = "assumed_utc" if quality == "reported" else quality
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if dt > now.replace(microsecond=0) and (dt - now).total_seconds() > 300:
        dt = fallback or _UNKNOWN_PUBLISHED_AT
        quality = "future_corrected"
    return dt, quality


def _iso_date(value: Any) -> str:
    return _parse_datetime(value)[0].isoformat()


def _article_id(source: str, title: str, url: str) -> str:
    raw = f"{source}\n{title}\n{url}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def _canonical_url(url: str) -> str:
    raw = _text(url)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                           if key.lower() not in _TRACKING_PARAMS])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                           query, ""))
    except ValueError:
        return raw


def _source_tier(source: str, provider: str) -> int:
    text = f"{source} {provider}".lower()
    return max((tier for hint, tier in _SOURCE_TIER_HINTS.items() if hint in text), default=2)


def freshness_metadata(published_at: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    published, quality = _parse_datetime(published_at)
    age_minutes = max(0, int((reference - published).total_seconds() // 60))
    if quality not in {"reported", "assumed_utc"}:
        return {"freshness": "unknown", "age_minutes": age_minutes,
                "freshness_score": 0}
    if age_minutes <= 180:
        bucket, freshness_score = "breaking", 100
    elif age_minutes <= 24 * 60:
        bucket, freshness_score = "fresh", 82
    elif age_minutes <= 72 * 60:
        bucket, freshness_score = "recent", 58
    elif age_minutes <= 7 * 24 * 60:
        bucket, freshness_score = "week", 30
    else:
        bucket, freshness_score = "archive", 8
    return {"freshness": bucket, "age_minutes": age_minutes,
            "freshness_score": freshness_score}


def tag_article(article: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Attach explainable trader tags, relevance, freshness, and rank."""
    title = _text(article.get("title") or article.get("headline"))
    summary = _text(article.get("summary"))
    body = _text(article.get("body"))
    title_lower = title.lower()
    combined = f"{title} {summary} {body}".lower()

    relevance = 0.0
    reasons: List[str] = []
    for term, weight in _CORE_RELEVANCE.items():
        if _contains(title_lower, term):
            relevance += weight
            reasons.append(f"headline:{term}")
        elif _contains(combined, term):
            relevance += weight * 0.58
            reasons.append(f"body:{term}")
    context_hits = [term for term in _MARKET_CONTEXT if _contains(combined, term)]
    relevance += min(20, len(context_hits) * 3.0)
    if any(_contains(combined, term) for term in ("asia", "china", "saudi", "mont belvieu",
                                                  "us gulf", "u.s. gulf")):
        relevance += 6
    negatives = [term for term in _NEGATIVE_CONTEXT if _contains(combined, term)]
    relevance -= 45 * len(negatives)
    relevance = round(max(0.0, min(100.0, relevance)), 1)
    reasons.extend(f"context:{term}" for term in context_hits[:4])
    reasons.extend(f"excluded:{term}" for term in negatives)

    regions = _matches(combined, _REGION_TERMS)
    products = _matches(combined, _PRODUCT_TERMS)
    drivers = _matches(combined, _DRIVER_TERMS)
    bull = sum(_contains(combined, term) for term in _BULLISH)
    bear = sum(_contains(combined, term) for term in _BEARISH)
    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    high_hits = sum(_contains(combined, term) for term in _HIGH_PRIORITY)
    importance = "high" if relevance >= 70 or high_hits >= 2 else (
        "medium" if relevance >= 42 or high_hits or len(drivers) >= 2 else "low"
    )
    date_quality = str(article.get("date_quality") or "reported")
    fresh = freshness_metadata(article.get("published_at") or article.get("published"), now=now)
    if date_quality not in {"reported", "assumed_utc"}:
        fresh = {"freshness": "unknown", "age_minutes": fresh["age_minutes"],
                 "freshness_score": 0}
    event_hit = any(_contains(combined, term) for term in _BREAKING_TERMS)
    is_breaking = (date_quality in {"reported", "assumed_utc"}
                   and fresh["age_minutes"] <= 180
                   and (importance == "high" or event_hit))
    tier = int(article.get("source_tier") or _source_tier(
        _text(article.get("source")), _text(article.get("provider"))))
    rank_score = round(relevance * 0.68 + fresh["freshness_score"] * 0.22 + tier * 2
                       + (8 if is_breaking else 0), 2)
    return {
        **article,
        "regions": regions,
        "products": products,
        "drivers": drivers,
        "direction": direction,
        "importance": importance,
        "source_tier": tier,
        "relevance_score": relevance,
        "relevance_reasons": reasons[:12],
        "is_relevant": relevance >= 28 and bool(products),
        "is_breaking": is_breaking,
        "rank_score": rank_score,
        **fresh,
    }


def normalize_article(raw: Dict[str, Any], provider: str, entitlement: str) -> Dict[str, Any]:
    source = _text(raw.get("source") or raw.get("publisher") or provider)
    title = _strip_html(raw.get("title") or raw.get("headline"))
    url = _canonical_url(_text(raw.get("url") or raw.get("link") or raw.get("webUrl")))
    date_value = (raw.get("published_at") or raw.get("published_iso") or raw.get("published")
                  or raw.get("publishDate") or raw.get("pubDate") or raw.get("updated"))
    published, date_quality = _parse_datetime(date_value)
    external_id = str(raw.get("id") or raw.get("articleId") or
                      _article_id(source, title, url))
    public_content = provider == "public" or entitlement == "public"
    safe_raw = dict(raw)
    if public_content:
        # Discovery feeds may expose Atom <content> or publisher HTML.  The
        # public fallback stores attributed metadata/link and a bounded feed
        # excerpt only; it never persists a discovered article body.
        for key in ("body", "content", "articleBody", "fullText"):
            safe_raw.pop(key, None)
        for key in ("summary", "description", "snippet"):
            if key in safe_raw:
                safe_raw[key] = _strip_html(safe_raw.get(key))[:1200]
    article = {
        "id": external_id,
        "provider": provider,
        "source": source,
        "title": title,
        "summary": _strip_html(raw.get("summary") or raw.get("description") or raw.get("snippet"))[:1200],
        "body": "" if public_content else _strip_html(raw.get("body") or raw.get("content")),
        "url": url,
        "published_at": published.isoformat(),
        "date_quality": date_quality,
        "language": _text(raw.get("language")) or "en",
        "feed_id": _text(raw.get("feed_id")) or None,
        "entitlement": entitlement,
        "raw": safe_raw,
    }
    return tag_article(article)


def _title_tokens(title: str) -> set[str]:
    cleaned = re.sub(r"\s+-\s+[^-]{2,50}$", "", title.lower())
    return {token for token in re.findall(r"[a-z0-9]+", cleaned)
            if len(token) > 1 and token not in _TITLE_STOPWORDS}


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def dedupe_articles(articles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove exact syndication copies and cluster near-duplicate event coverage."""
    candidates = [dict(article) for article in articles if _text(article.get("title"))]
    candidates.sort(key=lambda row: (
        int(row.get("source_tier") or 0), len(_text(row.get("summary"))),
        _text(row.get("published_at"))), reverse=True)
    seen_titles: Dict[str, Dict[str, Any]] = {}
    seen_urls: Dict[str, Dict[str, Any]] = {}
    unique: List[Dict[str, Any]] = []
    for article in candidates:
        tokens = _title_tokens(_text(article.get("title")))
        title_key = "".join(sorted(tokens))
        url_key = _canonical_url(_text(article.get("url")))
        duplicate = seen_titles.get(title_key) if title_key else None
        if not duplicate and url_key:
            duplicate = seen_urls.get(url_key)
        if duplicate:
            sources = list(duplicate.get("duplicate_sources") or [duplicate.get("source")])
            if article.get("source") not in sources:
                sources.append(article.get("source"))
            duplicate["duplicate_sources"] = [source for source in sources if source]
            duplicate["duplicate_count"] = int(duplicate.get("duplicate_count") or 1) + 1
            continue
        article["_tokens"] = tokens
        article["duplicate_sources"] = [article.get("source")] if article.get("source") else []
        article["duplicate_count"] = 1
        unique.append(article)
        if title_key:
            seen_titles[title_key] = article
        if url_key:
            seen_urls[url_key] = article

    clusters: List[List[Dict[str, Any]]] = []
    for article in unique:
        placed = False
        article_time, _ = _parse_datetime(article.get("published_at"))
        for cluster in clusters:
            representative = cluster[0]
            rep_time, _ = _parse_datetime(representative.get("published_at"))
            if abs((article_time - rep_time).total_seconds()) > 96 * 3600:
                continue
            if _similarity(article["_tokens"], representative["_tokens"]) >= 0.48:
                cluster.append(article)
                placed = True
                break
        if not placed:
            clusters.append([article])

    for cluster in clusters:
        signature = min(" ".join(sorted(row["_tokens"])) for row in cluster)
        cluster_key = "evt_" + hashlib.sha1(signature.encode("utf-8", "replace")).hexdigest()[:16]
        cluster_sources = sorted({
            str(source)
            for row in cluster
            for source in (row.get("duplicate_sources") or [row.get("source")])
            if source
        })
        for row in cluster:
            row.pop("_tokens", None)
            row["cluster_key"] = cluster_key
            row["cluster_size"] = len(cluster)
            row["cluster_sources"] = cluster_sources
            feed_id = str(row.get("feed_id") or "").lower()
            discovery_only = feed_id.startswith(("google_", "gdelt_"))
            official = (str(row.get("provider") or "").lower() == "platts" or
                        (int(row.get("source_tier") or 0) >= 4 and not discovery_only))
            confirmed = official or len(cluster_sources) >= 2
            row["confirmation_state"] = "confirmed" if confirmed else "developing"
            if row.get("is_breaking") and not confirmed:
                row["is_breaking"] = False
                row["rank_score"] = round(max(0.0, float(row.get("rank_score") or 0) - 8), 2)
    unique.sort(key=lambda row: (float(row.get("rank_score") or 0),
                                 _text(row.get("published_at"))), reverse=True)
    return unique


def _google_news_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urlencode({
        "q": query, "hl": "en-SG", "gl": "SG", "ceid": "SG:en",
    })


def public_feed_definitions() -> List[Dict[str, str]]:
    """Return diverse LPG-specific public discovery feeds.

    Google News is used only as an RSS discovery index; every item keeps the
    underlying publisher in ``source``.  Official feeds remain separately
    labelled and all stories pass the same LPG relevance gate.
    """
    queries = (
        ("asia_lpg", '(LPG OR propane OR butane OR "Saudi CP" OR FEI) Asia when:3d'),
        ("lpg_shipping", '(VLGC OR "LPG shipping" OR "propane freight") when:3d'),
        ("us_lpg", '(propane OR butane OR NGL) ("Mont Belvieu" OR "US Gulf" OR export) when:3d'),
        ("middle_east_lpg", '(propane OR butane OR LPG) (Aramco OR Saudi OR Qatar OR UAE) when:3d'),
        ("pdh_feedstock", '(PDH OR propylene) (propane OR LPG OR feedstock) when:3d'),
        ("lpg_disruption", '(LPG OR propane OR butane) (outage OR shutdown OR sanctions OR terminal) when:3d'),
    )
    feeds = [{"id": f"google_{feed_id}", "source": "Google News",
              "url": _google_news_url(query), "kind": "search_rss",
              "role": "discovery_fallback", "production_sla": "false"}
             for feed_id, query in queries]
    gdelt_queries = (
        ("asia", '(LPG OR propane OR butane OR VLGC) (Asia OR China OR Japan OR Korea)'),
        ("middle_east", '(LPG OR propane OR butane OR "Saudi CP") (Aramco OR Saudi OR Qatar OR UAE)'),
        ("us_gulf", '(propane OR butane OR NGL) ("Mont Belvieu" OR "US Gulf" OR Texas)'),
        ("freight", '(VLGC OR "LPG shipping" OR "propane freight")'),
    )
    for region, gdelt_query in gdelt_queries:
        feeds.append({
            "id": f"gdelt_{region}", "source": "GDELT DOC 2.0",
            "url": "https://api.gdeltproject.org/api/v2/doc/doc?" + urlencode({
                "query": gdelt_query, "mode": "ArtList", "maxrecords": "100",
                "format": "json", "sort": "HybridRel", "timespan": "3d",
            }),
            "kind": "gdelt_json", "role": "multilingual_discovery", "production_sla": "false",
        })
    feeds.extend((
        {
            "id": "aramco_news", "source": "Saudi Aramco",
            "url": "https://www.aramco.com/api/v1/com/rss/news?sc_lang=en",
            "kind": "official_rss", "role": "official_publication", "production_sla": "false",
        },
        {
            "id": "eia_today", "source": "U.S. Energy Information Administration",
            "url": "https://www.eia.gov/rss/todayinenergy.xml", "kind": "official_rss",
            "role": "official_publication", "production_sla": "false",
        },
        {
            "id": "eia_press", "source": "U.S. Energy Information Administration",
            "url": "https://www.eia.gov/rss/press_rss.xml", "kind": "official_rss",
            "role": "official_publication", "production_sla": "false",
        },
        {
            "id": "eia_propane", "source": "U.S. Energy Information Administration",
            "url": "https://www.eia.gov/petroleum/heatingoilpropane/includes/hopu_rss.xml",
            "kind": "official_rss", "role": "official_publication", "production_sla": "false",
        },
        {
            "id": "nhc_atlantic", "source": "U.S. National Hurricane Center",
            "url": "https://www.nhc.noaa.gov/index-at.xml", "kind": "official_rss",
            "role": "disruption_monitor", "production_sla": "false",
        },
    ))
    custom = os.environ.get("LPG_NEWS_FEEDS_JSON", "").strip()
    if custom:
        try:
            rows = json.loads(custom)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("id") and row.get("url"):
                        feeds.append({"id": _text(row["id"]),
                                      "source": _text(row.get("source")) or _text(row["id"]),
                                      "url": _text(row["url"]),
                                      "kind": _text(row.get("kind")) or "custom_rss"})
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return feeds


def _node_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return _text("".join(node.itertext()))


def _first_node(children: Mapping[str, ET.Element], *names: str) -> Optional[ET.Element]:
    """Pick the first present XML node without relying on Element truthiness.

    ``Element`` instances with no child elements are false-y even when their
    text is populated, which is the normal shape of RSS ``pubDate`` nodes.
    """
    for name in names:
        node = children.get(name)
        if node is not None:
            return node
    return None


def _fetch_rss_feed(feed: Mapping[str, str], timeout: int = 12) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False  # Local corporate proxy settings frequently break public RSS.
    response = session.get(
        feed["url"], timeout=(4, timeout),
        headers={"User-Agent": "FinceptCommoditiesLocal/2.0",
                 "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                 "Cache-Control": "no-cache"},
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries = root.findall(".//item") or root.findall(".//{*}item")
    is_atom = False
    if not entries:
        entries = root.findall(".//{*}entry")
        is_atom = True
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        children = {child.tag.rsplit("}", 1)[-1].lower(): child for child in list(entry)}
        title = _strip_html(_node_text(children.get("title")))
        if not title:
            continue
        if is_atom:
            link_node = children.get("link")
            link = _text(link_node.attrib.get("href")) if link_node is not None else ""
            description = _node_text(_first_node(children, "summary", "content"))
            published = _node_text(_first_node(children, "published", "updated"))
        else:
            link = _node_text(children.get("link"))
            description = _node_text(_first_node(children, "description", "summary"))
            published = _node_text(_first_node(children, "pubdate", "date"))
        source = _node_text(children.get("source")) or feed.get("source") or feed["id"]
        rows.append({"title": title, "summary": _strip_html(description), "url": link,
                     "published": published, "source": source, "feed_id": feed["id"]})
    return rows


def _fetch_gdelt(feed: Mapping[str, str], timeout: int = 20) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        # GDELT's TLS handshake can be materially slower than the RSS feeds.
        feed["url"], timeout=(15, timeout),
        headers={"User-Agent": "FinceptCommoditiesLocal/2.0",
                 "Accept": "application/json", "Cache-Control": "no-cache"},
    )
    response.raise_for_status()
    payload = response.json()
    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        raise RuntimeError("GDELT response did not contain an articles list")
    rows = []
    for item in articles:
        if not isinstance(item, dict):
            continue
        rows.append({
            "title": item.get("title"), "summary": item.get("snippet"),
            "url": item.get("url") or item.get("url_mobile"),
            "published": item.get("seendate"),
            "source": item.get("domain") or feed.get("source"),
            "language": item.get("language") or "en", "feed_id": feed["id"],
        })
    return rows


def _fetch_public_source(feed: Mapping[str, str]) -> List[Dict[str, Any]]:
    if feed.get("kind") == "gdelt_json":
        return _fetch_gdelt(feed)
    return _fetch_rss_feed(feed)


class PublicNewsAggregator:
    """Concurrent, proxy-independent RSS aggregator with per-feed health."""

    def __init__(
        self,
        feeds: Optional[Sequence[Mapping[str, str]]] = None,
        fetcher: Optional[Callable[[Mapping[str, str]], List[Dict[str, Any]]]] = None,
        max_workers: int = 8,
    ) -> None:
        self.feeds = [dict(feed) for feed in (feeds or public_feed_definitions())]
        self.fetcher = fetcher or _fetch_public_source
        self.max_workers = max(1, min(int(max_workers), 12))

    def _one(self, feed: Mapping[str, str]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        started = time.perf_counter()
        attempted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            raw_rows = self.fetcher(feed)
            normalized = [normalize_article(row, "public", "public") for row in raw_rows]
            relevant = [row for row in normalized if row.get("is_relevant")]
            latest = max((row.get("published_at") for row in relevant), default=None)
            latency = int((time.perf_counter() - started) * 1000)
            status = "healthy" if relevant else "empty"
            return relevant, {
                "source_id": feed["id"], "source_name": feed.get("source") or feed["id"],
                "kind": feed.get("kind") or "rss", "status": status,
                "ok": True, "last_attempt_at": attempted_at,
                "last_success_at": attempted_at, "latest_published_at": latest,
                "article_count": len(raw_rows), "relevant_count": len(relevant),
                "latency_ms": latency, "error": None,
                "metadata": {"role": feed.get("role") or "public_discovery",
                             "production_sla": str(feed.get("production_sla") or "false").lower() == "true"},
            }
        except Exception as exc:  # Per-feed isolation is the reliability boundary.
            return [], {
                "source_id": feed["id"], "source_name": feed.get("source") or feed["id"],
                "kind": feed.get("kind") or "rss", "status": "error", "ok": False,
                "last_attempt_at": attempted_at, "last_success_at": None,
                "latest_published_at": None, "article_count": 0, "relevant_count": 0,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": str(exc)[:500],
                "metadata": {"role": feed.get("role") or "public_discovery",
                             "production_sla": str(feed.get("production_sla") or "false").lower() == "true"},
            }

    def fetch(self, limit: int = 160) -> Dict[str, Any]:
        articles: List[Dict[str, Any]] = []
        sources: List[Dict[str, Any]] = []
        feeds = list(self.feeds)
        gdelt = [feed for feed in feeds if feed.get("kind") == "gdelt_json"]
        if len(gdelt) > 1:
            # DOC 2.0 asks high-traffic clients to stay below one request per
            # five seconds.  Rotate one focused regional query per refresh;
            # the 2-minute server cadence covers the four-query set in eight
            # minutes without firing a prohibited concurrent burst.
            selected = gdelt[int(time.time() // 120) % len(gdelt)]
            feeds = [feed for feed in feeds if feed.get("kind") != "gdelt_json"] + [selected]
        workers = min(self.max_workers, max(1, len(feeds)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lpg-news") as executor:
            futures = {executor.submit(self._one, feed): feed for feed in feeds}
            for future in as_completed(futures):
                rows, health = future.result()
                articles.extend(rows)
                sources.append(health)
        sources.sort(key=lambda row: str(row["source_id"]))
        ranked = dedupe_articles(articles)
        limit = max(5, min(int(limit or 160), 500))
        ok = sum(1 for source in sources if source.get("ok"))
        return {
            "configured": True,
            "articles": ranked[:limit],
            "source_status": {"ok": ok, "failed": len(sources) - ok,
                              "degraded": ok == 0, "feeds": len(sources),
                              "configured_feeds": len(self.feeds)},
            "sources": sources,
            "error": "all_public_news_sources_failed" if sources and ok == 0 else None,
        }


class PlattsNewsClient:
    """OAuth client for a separately contracted S&P machine-readable news API."""

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
        return bool(self.api_url and (self.static_token or
                    (self.token_url and self.client_id and self.client_secret)))

    def _token(self) -> str:
        if self.static_token:
            return self.static_token
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        response = self.session.post(self.token_url, data=data,
                                     auth=(self.client_id, self.client_secret), timeout=30)
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Platts token response did not contain access_token")
        return str(token)

    def fetch(self, start: Optional[str] = None, end: Optional[str] = None,
              page_size: int = 100, max_pages: int = 100) -> Dict[str, Any]:
        if not self.configured:
            return {"configured": False, "articles": [],
                    "error": "Platts news API is not configured"}
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        articles: List[Dict[str, Any]] = []
        page = 1
        next_url: Optional[str] = self.api_url
        while next_url and page <= max_pages:
            params: Dict[str, Any] = {
                "page": page, "pageSize": max(1, min(page_size, 500)),
                "query": ("LPG OR liquefied petroleum gas OR propane OR butane OR NGL "
                          "OR FEI OR Saudi CP OR VLGC OR PDH OR Mont Belvieu"),
            }
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
            articles.extend(normalize_article(row, "platts", "entitled")
                            for row in rows if isinstance(row, dict))
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


def public_lpg_news(limit: int = 160,
                    aggregator: Optional[PublicNewsAggregator] = None) -> Dict[str, Any]:
    """Fetch public LPG coverage without presenting it as Platts content."""
    return (aggregator or PublicNewsAggregator()).fetch(limit=limit)


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
        "relevance_score": float(article.get("relevance_score") or 0),
        "rank_score": float(article.get("rank_score") or 0),
        "source_tier": int(article.get("source_tier") or 0),
        "cluster_key": str(article.get("cluster_key") or "") or None,
        "is_breaking": bool(article.get("is_breaking")),
        "metadata": {
            "provider": article.get("provider"), "feed_id": article.get("feed_id"),
            "regions": regions, "products": products, "drivers": drivers,
            "date_quality": article.get("date_quality"),
            "relevance_reasons": article.get("relevance_reasons") or [],
            "cluster_size": article.get("cluster_size") or 1,
            "cluster_sources": article.get("cluster_sources") or [],
            "confirmation_state": article.get("confirmation_state") or "developing",
            "duplicate_count": article.get("duplicate_count") or 1,
            "duplicate_sources": article.get("duplicate_sources") or [],
            "content_boundary": ("licensed_machine_readable" if article.get("provider") == "platts"
                                 else "public_source"),
            "raw": article.get("raw") or {},
        },
    }
