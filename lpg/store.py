"""SQLite persistence for licensed and public LPG market data."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .catalog import ASIA_LPG_CANDIDATES
from .models import (
    CurvePointInput,
    DatasetRowInput,
    NewsInput,
    ObservationInput,
    SeriesInput,
    json_safe,
    normalize_date,
    normalize_input,
    normalize_timestamp,
    utc_now,
)


DEFAULT_DB_PATH = Path(
    os.environ.get(
        "LPG_DB_PATH",
        Path(__file__).resolve().parents[1] / "data" / "private" / "lpg.sqlite",
    )
)
SCHEMA_VERSION = 5

# Platts can return several BATE values for the same assessment.  The official
# close (c) is the trading default, followed by u/e/l/h.  Keep the preference
# in SQL so history, statistics, spreads, and the latest-price screen all use
# the same effective observation.
BATE_PRIORITY_SQL = (
    "CASE lower(COALESCE(o.bate,'')) "
    "WHEN 'c' THEN 0 WHEN 'u' THEN 1 WHEN 'e' THEN 2 "
    "WHEN 'l' THEN 3 WHEN 'h' THEN 4 ELSE 9 END"
)


_MIGRATION_1 = r"""
CREATE TABLE IF NOT EXISTS catalog_candidates (
    candidate_id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    family TEXT NOT NULL,
    name TEXT NOT NULL,
    product TEXT,
    market TEXT,
    region TEXT,
    location TEXT,
    basis TEXT,
    delivery_type TEXT,
    expected_currency TEXT,
    expected_unit TEXT,
    priority INTEGER NOT NULL DEFAULT 1000,
    summary INTEGER NOT NULL DEFAULT 0,
    search_terms_json TEXT NOT NULL DEFAULT '[]',
    discovery_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (discovery_status IN ('entitled','unentitled','pending_review','retired','error')),
    mapped_series_id TEXT,
    last_checked_at TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'all',
    status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS series_catalog (
    id TEXT PRIMARY KEY,
    canonical_key TEXT,
    symbol TEXT,
    name TEXT NOT NULL,
    product TEXT,
    market TEXT,
    region TEXT,
    location TEXT,
    basis TEXT,
    delivery_type TEXT,
    quote_kind TEXT NOT NULL DEFAULT 'assessment',
    currency TEXT,
    unit TEXT,
    normalized_currency TEXT,
    normalized_unit TEXT,
    frequency TEXT,
    source TEXT NOT NULL,
    source_dataset TEXT,
    entitlement_state TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (entitlement_state IN ('entitled','unentitled','pending_review','retired','error')),
    entitlement_reason TEXT,
    description TEXT,
    first_date TEXT,
    last_date TEXT,
    last_checked_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 1000,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, symbol)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL REFERENCES series_catalog(id) ON DELETE CASCADE,
    observation_date TEXT NOT NULL,
    value_native REAL NOT NULL,
    currency_native TEXT,
    unit_native TEXT,
    value_normalized REAL,
    currency_normalized TEXT,
    unit_normalized TEXT,
    bate TEXT NOT NULL DEFAULT '',
    publication_time TEXT,
    fetched_at TEXT NOT NULL,
    source_ref TEXT,
    is_correction INTEGER NOT NULL DEFAULT 0,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, observation_date, bate)
);

CREATE TABLE IF NOT EXISTS observation_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    series_id TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    changed_fields_json TEXT NOT NULL,
    previous_snapshot_json TEXT NOT NULL,
    new_snapshot_json TEXT NOT NULL,
    revision_reason TEXT,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    revised_at TEXT NOT NULL,
    UNIQUE(observation_id, revision_number)
);

CREATE TABLE IF NOT EXISTS curve_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL REFERENCES series_catalog(id) ON DELETE CASCADE,
    as_of_date TEXT NOT NULL,
    contract_month TEXT NOT NULL,
    delivery_start TEXT,
    delivery_end TEXT,
    value_native REAL NOT NULL,
    value_normalized REAL,
    currency_native TEXT,
    unit_native TEXT,
    currency_normalized TEXT,
    unit_normalized TEXT,
    fetched_at TEXT NOT NULL,
    source_ref TEXT,
    is_correction INTEGER NOT NULL DEFAULT 0,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, as_of_date, contract_month)
);

CREATE TABLE IF NOT EXISTS curve_point_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curve_point_id INTEGER NOT NULL REFERENCES curve_points(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    changed_fields_json TEXT NOT NULL,
    previous_snapshot_json TEXT NOT NULL,
    new_snapshot_json TEXT NOT NULL,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    revised_at TEXT NOT NULL,
    UNIQUE(curve_point_id, revision_number)
);

CREATE TABLE IF NOT EXISTS dataset_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    row_key TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset, row_key, as_of_date)
);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_key TEXT NOT NULL UNIQUE,
    headline TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    url TEXT,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    region TEXT,
    product TEXT,
    topic TEXT,
    direction TEXT,
    importance TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    entitlement_state TEXT NOT NULL DEFAULT 'entitled'
        CHECK (entitlement_state IN ('entitled','unentitled','pending_review','retired','error')),
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_series_canonical ON series_catalog(canonical_key);
CREATE INDEX IF NOT EXISTS idx_series_filters ON series_catalog(product, market, entitlement_state, active);
CREATE INDEX IF NOT EXISTS idx_observations_series_date ON observations(series_id, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_observations_fetched ON observations(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_series_date ON observation_revisions(series_id, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_curves_series_asof ON curve_points(series_id, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_dataset_name_date ON dataset_rows(dataset, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_filters ON news(product, region, source, direction);
CREATE INDEX IF NOT EXISTS idx_runs_source_started ON ingestion_runs(source, started_at DESC);
"""

_MIGRATION_2 = r"""
ALTER TABLE catalog_candidates ADD COLUMN discovered_symbol TEXT;
ALTER TABLE catalog_candidates ADD COLUMN discovered_curve_code TEXT;
ALTER TABLE catalog_candidates ADD COLUMN entitlement_metadata_json TEXT NOT NULL DEFAULT '{}';
"""

_NEWS_V3_COLUMNS = {
    "relevance_score": "REAL NOT NULL DEFAULT 0",
    "rank_score": "REAL NOT NULL DEFAULT 0",
    "source_tier": "INTEGER NOT NULL DEFAULT 0",
    "cluster_key": "TEXT",
    "is_breaking": "INTEGER NOT NULL DEFAULT 0",
}

_NEWS_SOURCE_HEALTH_DDL = r"""CREATE TABLE IF NOT EXISTS news_source_health (
    source_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'rss',
    status TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    latest_published_at TEXT,
    article_count INTEGER NOT NULL DEFAULT 0,
    relevant_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _apply_migration_3(connection: sqlite3.Connection) -> None:
    """Apply the news migration safely after an interrupted/partial attempt."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news)")}
    for name, definition in _NEWS_V3_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE news ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_rank "
        "ON news(is_breaking DESC, relevance_score DESC, published_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cluster ON news(cluster_key, published_at DESC)"
    )
    connection.execute(_NEWS_SOURCE_HEALTH_DDL)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_health_status "
        "ON news_source_health(status, last_attempt_at DESC)"
    )


_INTELLIGENCE_V4_DDL = r"""
CREATE TABLE IF NOT EXISTS intelligence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    headline TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    risk_score INTEGER NOT NULL DEFAULT 0 CHECK (risk_score BETWEEN 0 AND 100),
    confirmation_state TEXT NOT NULL CHECK (confirmation_state IN ('confirmed','developing')),
    confidence_score INTEGER NOT NULL DEFAULT 0 CHECK (confidence_score BETWEEN 0 AND 100),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    location_name TEXT,
    geo_precision TEXT NOT NULL DEFAULT 'unresolved',
    region TEXT,
    direction TEXT NOT NULL DEFAULT 'neutral',
    source_count INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    sources_json TEXT NOT NULL DEFAULT '[]',
    asset_ids_json TEXT NOT NULL DEFAULT '[]',
    route_ids_json TEXT NOT NULL DEFAULT '[]',
    products_json TEXT NOT NULL DEFAULT '[]',
    affected_series_json TEXT NOT NULL DEFAULT '[]',
    impact_json TEXT NOT NULL DEFAULT '{}',
    data_gaps_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intelligence_event_rank
    ON intelligence_events(active DESC, risk_score DESC, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_intelligence_event_type
    ON intelligence_events(event_type, severity, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_intelligence_event_region
    ON intelligence_events(region, last_seen_at DESC);
"""

_INTELLIGENCE_EVENT_COLUMNS = (
    "event_key", "headline", "event_type", "severity", "risk_score",
    "confirmation_state", "confidence_score", "first_seen_at", "last_seen_at",
    "latitude", "longitude", "location_name", "geo_precision", "region",
    "direction", "source_count", "evidence_count", "active", "sources_json",
    "asset_ids_json", "route_ids_json", "products_json", "affected_series_json",
    "impact_json", "data_gaps_json", "evidence_json", "metadata_json",
)

_INTELLIGENCE_EVENT_UPSERT = f"""INSERT INTO intelligence_events
    ({','.join(_INTELLIGENCE_EVENT_COLUMNS)},created_at,updated_at)
    VALUES ({','.join('?' for _ in _INTELLIGENCE_EVENT_COLUMNS)},?,?)
    ON CONFLICT(event_key) DO UPDATE SET
        headline=excluded.headline,event_type=excluded.event_type,
        severity=excluded.severity,risk_score=excluded.risk_score,
        confirmation_state=excluded.confirmation_state,
        confidence_score=excluded.confidence_score,
        first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,
        latitude=excluded.latitude,longitude=excluded.longitude,
        location_name=excluded.location_name,geo_precision=excluded.geo_precision,
        region=excluded.region,direction=excluded.direction,
        source_count=excluded.source_count,evidence_count=excluded.evidence_count,
        active=excluded.active,sources_json=excluded.sources_json,
        asset_ids_json=excluded.asset_ids_json,route_ids_json=excluded.route_ids_json,
        products_json=excluded.products_json,
        affected_series_json=excluded.affected_series_json,
        impact_json=excluded.impact_json,data_gaps_json=excluded.data_gaps_json,
        evidence_json=excluded.evidence_json,metadata_json=excluded.metadata_json,
        updated_at=excluded.updated_at"""


_VESSEL_V5_DDL = r"""
CREATE TABLE IF NOT EXISTS vessels (
    vessel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    imo TEXT UNIQUE,
    mmsi TEXT UNIQUE,
    vessel_type TEXT,
    fleet_group TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vessel_port_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_key TEXT NOT NULL UNIQUE,
    vessel_id TEXT NOT NULL REFERENCES vessels(vessel_id) ON DELETE CASCADE,
    port_name TEXT NOT NULL,
    port_name_local TEXT,
    locode TEXT,
    country TEXT,
    country_code TEXT,
    arrived_at TEXT,
    berthed_at TEXT,
    departed_at TEXT,
    timestamp_state TEXT NOT NULL,
    source_timezone TEXT,
    stay_hours REAL,
    latitude REAL,
    longitude REAL,
    geo_precision TEXT NOT NULL DEFAULT 'unavailable',
    draught_arrival REAL,
    draught_departure REAL,
    draught_change REAL,
    operation_signal TEXT NOT NULL DEFAULT 'unknown'
        CHECK (operation_signal IN ('loaded','discharged','no_major_change','unknown')),
    operation_signal_state TEXT NOT NULL DEFAULT 'unavailable',
    next_destination TEXT,
    source TEXT NOT NULL,
    source_detail TEXT,
    evidence_state TEXT NOT NULL DEFAULT 'source_reported',
    source_snapshot_at TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vessel_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_key TEXT NOT NULL UNIQUE,
    vessel_id TEXT NOT NULL REFERENCES vessels(vessel_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    timestamp_state TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    speed_kn REAL,
    course_deg REAL,
    navigation_state TEXT,
    destination TEXT,
    position_kind TEXT NOT NULL DEFAULT 'historical'
        CHECK (position_kind IN ('live','recent','historical')),
    source TEXT NOT NULL,
    evidence_state TEXT NOT NULL DEFAULT 'source_reported',
    raw_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vessel_source_health (
    source_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    access_state TEXT NOT NULL,
    status TEXT NOT NULL,
    entitlement_state TEXT NOT NULL DEFAULT 'unverified',
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    latest_observation_at TEXT,
    vessel_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vessel_calls_vessel_time
    ON vessel_port_calls(vessel_id, arrived_at DESC, departed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vessel_calls_port_time
    ON vessel_port_calls(locode, port_name, arrived_at DESC);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_vessel_time
    ON vessel_positions(vessel_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vessel_source_status
    ON vessel_source_health(status, latest_observation_at DESC);
"""


def _limit(value: Any, default: int = 500, maximum: int = 5000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class LpgStore:
    """Small connection-per-operation store suitable for ThreadingHTTPServer."""

    def __init__(self, path: Optional[Any] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self):
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def _reader(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations"
            )}
            if 1 not in applied:
                connection.executescript(_MIGRATION_1)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, now),
                )
            if 2 not in applied:
                connection.executescript(_MIGRATION_2)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, now),
                )
            if 3 not in applied:
                _apply_migration_3(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, now),
                )
            if 4 not in applied:
                connection.executescript(_INTELLIGENCE_V4_DDL)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, now),
                )
            if 5 not in applied:
                connection.executescript(_VESSEL_V5_DDL)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, now),
                )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.seed_candidates()

    def seed_candidates(self) -> int:
        now = utc_now()
        with self._transaction() as connection:
            for item in ASIA_LPG_CANDIDATES:
                connection.execute(
                    """
                    INSERT INTO catalog_candidates (
                        candidate_id, canonical_key, family, name, product, market,
                        region, location, basis, delivery_type, expected_currency,
                        expected_unit, priority, summary, search_terms_json,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        canonical_key=excluded.canonical_key,
                        family=excluded.family, name=excluded.name,
                        product=excluded.product, market=excluded.market,
                        region=excluded.region, location=excluded.location,
                        basis=excluded.basis, delivery_type=excluded.delivery_type,
                        expected_currency=excluded.expected_currency,
                        expected_unit=excluded.expected_unit,
                        priority=excluded.priority, summary=excluded.summary,
                        search_terms_json=excluded.search_terms_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["candidate_id"], item["canonical_key"], item["family"],
                        item["name"], item.get("product"), item.get("market"),
                        item.get("region"), item.get("location"), item.get("basis"),
                        item.get("delivery_type"), item.get("expected_currency"),
                        item.get("expected_unit"), item.get("priority", 1000),
                        1 if item.get("summary") else 0,
                        json.dumps(item.get("search_terms", []), ensure_ascii=True),
                        now, now,
                    ),
                )
        return len(ASIA_LPG_CANDIDATES)

    @staticmethod
    def _record(row: Optional[sqlite3.Row], parse_json: bool = True) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        output = dict(row)
        if parse_json:
            for key in list(output):
                if not key.endswith("_json"):
                    continue
                target = key[:-5]
                try:
                    output[target] = json.loads(output.pop(key) or "null")
                except (TypeError, json.JSONDecodeError):
                    output[target] = None
        for key in ("active", "summary", "is_correction", "is_breaking"):
            if key in output:
                output[key] = bool(output[key])
        return json_safe(output)

    def candidates(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = """SELECT c.*,s.symbol mapped_symbol,s.name mapped_name,
                        s.first_date,s.last_date,s.currency,s.unit
                 FROM catalog_candidates c
                 LEFT JOIN series_catalog s ON s.id=c.mapped_series_id"""
        params: List[Any] = []
        if state:
            sql += " WHERE c.discovery_status = ?"
            params.append(state)
        sql += " ORDER BY c.priority, c.name"
        with self._reader() as connection:
            return [self._record(row) for row in connection.execute(sql, params)]

    def update_candidate_entitlement(
        self,
        candidate_id: str,
        state: str,
        mapped_series_id: Optional[str] = None,
        error: Optional[str] = None,
        checked_at: Optional[str] = None,
        symbol: Optional[str] = None,
        curve_code: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if state not in {"entitled", "unentitled", "pending_review", "retired", "error"}:
            raise ValueError(f"invalid discovery state: {state}")
        when = normalize_timestamp(checked_at) or utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE catalog_candidates
                   SET discovery_status=?, mapped_series_id=COALESCE(?, mapped_series_id),
                       error=?, last_checked_at=?, updated_at=?,
                       discovered_symbol=COALESCE(?,discovered_symbol),
                       discovered_curve_code=COALESCE(?,discovered_curve_code),
                       entitlement_metadata_json=?
                   WHERE candidate_id=? OR canonical_key=?""",
                (state, mapped_series_id, error, when, when, symbol, curve_code,
                 json.dumps(json_safe(dict(metadata or {})), ensure_ascii=True),
                 candidate_id, candidate_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown catalog candidate: {candidate_id}")
            row = connection.execute(
                "SELECT * FROM catalog_candidates WHERE candidate_id=? OR canonical_key=?",
                (candidate_id, candidate_id),
            ).fetchone()
        return self._record(row)

    def upsert_series(self, value: Any) -> Dict[str, Any]:
        item = normalize_input(value, SeriesInput)
        now = utc_now()
        columns = (
            "id", "canonical_key", "symbol", "name", "product", "market", "region",
            "location", "basis", "delivery_type", "quote_kind", "currency", "unit",
            "normalized_currency", "normalized_unit", "frequency", "source",
            "source_dataset", "entitlement_state", "entitlement_reason", "description",
            "active", "display_order", "metadata_json",
        )
        values = [item.get(column) for column in columns]
        with self._transaction() as connection:
            connection.execute(
                f"""INSERT INTO series_catalog ({','.join(columns)},created_at,updated_at)
                    VALUES ({','.join('?' for _ in columns)},?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        canonical_key=excluded.canonical_key, symbol=excluded.symbol,
                        name=excluded.name, product=excluded.product, market=excluded.market,
                        region=excluded.region, location=excluded.location,
                        basis=excluded.basis, delivery_type=excluded.delivery_type,
                        quote_kind=excluded.quote_kind, currency=excluded.currency,
                        unit=excluded.unit, normalized_currency=excluded.normalized_currency,
                        normalized_unit=excluded.normalized_unit, frequency=excluded.frequency,
                        source=excluded.source, source_dataset=excluded.source_dataset,
                        entitlement_state=excluded.entitlement_state,
                        entitlement_reason=excluded.entitlement_reason,
                        description=excluded.description, active=excluded.active,
                        display_order=excluded.display_order,
                        metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                values + [now, now],
            )
            row = connection.execute("SELECT * FROM series_catalog WHERE id=?", (item["id"],)).fetchone()
        return self._record(row)

    def get_series(self, series_id: str) -> Optional[Dict[str, Any]]:
        with self._reader() as connection:
            return self._record(connection.execute(
                "SELECT * FROM series_catalog WHERE id=?", (series_id,)
            ).fetchone())

    def get_series_by_symbol(self, source: str, symbol: str) -> Optional[Dict[str, Any]]:
        with self._reader() as connection:
            return self._record(connection.execute(
                "SELECT * FROM series_catalog WHERE source=? AND symbol=?", (source, symbol)
            ).fetchone())

    def get_series_by_canonical_key(self, canonical_key: str) -> Optional[Dict[str, Any]]:
        with self._reader() as connection:
            return self._record(connection.execute(
                """SELECT * FROM series_catalog WHERE canonical_key=?
                   ORDER BY CASE entitlement_state WHEN 'entitled' THEN 0 ELSE 1 END,
                            active DESC, display_order LIMIT 1""",
                (canonical_key,),
            ).fetchone())

    def list_series(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        where, params = [], []
        for field in ("entitlement_state", "product", "market", "region", "source",
                      "canonical_key", "quote_kind"):
            if filters.get(field) not in (None, ""):
                where.append(f"{field} = ?")
                params.append(filters[field])
        if filters.get("active") not in (None, ""):
            where.append("active = ?")
            raw = filters["active"]
            params.append(1 if raw is True or str(raw).lower() in {"1", "true", "yes"} else 0)
        if filters.get("q"):
            where.append("(name LIKE ? OR symbol LIKE ? OR canonical_key LIKE ? OR description LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term] * 4)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 500), _offset(filters.get("offset"))
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM series_catalog{clause}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM series_catalog{clause} ORDER BY display_order,name LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        clean_filters = {key: value for key, value in filters.items() if value not in (None, "")}
        return {"items": [self._record(row) for row in rows], "total": total,
                "limit": limit, "offset": offset, "filters": clean_filters}

    def upsert_observation(self, value: Any) -> Dict[str, Any]:
        item = normalize_input(value, ObservationInput)
        now = utc_now()
        # Only a changed market value/unit is a revision.  The same assessment
        # arriving through Current, History, and Correction queries must stay
        # idempotent even when transport metadata differs.
        tracked = (
            "value_native", "currency_native", "unit_native", "value_normalized",
            "currency_normalized", "unit_normalized",
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM observations WHERE series_id=? AND observation_date=? AND bate=?",
                (item["series_id"], item["observation_date"], item["bate"]),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO observations (
                        series_id, observation_date, value_native, currency_native, unit_native,
                        value_normalized, currency_normalized, unit_normalized, bate,
                        publication_time, fetched_at, source_ref, ingestion_run_id,
                        metadata_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["series_id"], item["observation_date"], item["value_native"],
                        item["currency_native"], item["unit_native"], item["value_normalized"],
                        item["currency_normalized"], item["unit_normalized"], item["bate"],
                        item["publication_time"], item["fetched_at"], item["source_ref"],
                        item["ingestion_run_id"], item["metadata_json"], now, now,
                    ),
                )
                observation_id, action = cursor.lastrowid, "inserted"
            else:
                old = dict(existing)
                changed = [key for key in tracked if old.get(key) != item.get(key)]
                observation_id = old["id"]
                if changed:
                    revision_number = connection.execute(
                        "SELECT COUNT(*) + 1 FROM observation_revisions WHERE observation_id=?",
                        (observation_id,),
                    ).fetchone()[0]
                    new_snapshot = {key: item.get(key) for key in tracked}
                    old_snapshot = {key: old.get(key) for key in tracked}
                    connection.execute(
                        """INSERT INTO observation_revisions (
                            observation_id, series_id, observation_date, revision_number,
                            changed_fields_json, previous_snapshot_json, new_snapshot_json,
                            revision_reason, ingestion_run_id, revised_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            observation_id, item["series_id"], item["observation_date"],
                            revision_number, json.dumps(changed), json.dumps(old_snapshot),
                            json.dumps(new_snapshot), item["revision_reason"],
                            item["ingestion_run_id"], now,
                        ),
                    )
                    connection.execute(
                        """UPDATE observations SET
                            value_native=?, currency_native=?, unit_native=?,
                            value_normalized=?, currency_normalized=?, unit_normalized=?,
                            publication_time=?, fetched_at=?, source_ref=?, is_correction=1,
                            ingestion_run_id=?, metadata_json=?, updated_at=? WHERE id=?""",
                        (
                            item["value_native"], item["currency_native"], item["unit_native"],
                            item["value_normalized"], item["currency_normalized"],
                            item["unit_normalized"], item["publication_time"], item["fetched_at"],
                            item["source_ref"], item["ingestion_run_id"], item["metadata_json"],
                            now, observation_id,
                        ),
                    )
                    action = "updated"
                else:
                    connection.execute(
                        """UPDATE observations SET publication_time=COALESCE(?,publication_time),
                           fetched_at=?,source_ref=COALESCE(?,source_ref),
                           is_correction=CASE WHEN ? IS NULL THEN is_correction ELSE 1 END,
                           ingestion_run_id=?,metadata_json=?,updated_at=? WHERE id=?""",
                        (item["publication_time"], item["fetched_at"], item["source_ref"],
                         item["revision_reason"], item["ingestion_run_id"],
                         item["metadata_json"], now, observation_id),
                    )
                    action = "unchanged"
            connection.execute(
                """UPDATE series_catalog SET
                       first_date=CASE WHEN first_date IS NULL OR first_date>? THEN ? ELSE first_date END,
                       last_date=CASE WHEN last_date IS NULL OR last_date<? THEN ? ELSE last_date END,
                       updated_at=? WHERE id=?""",
                (item["observation_date"], item["observation_date"], item["observation_date"],
                 item["observation_date"], now, item["series_id"]),
            )
            row = connection.execute(
                """SELECT o.*,
                          (SELECT COUNT(*) FROM observation_revisions r WHERE r.observation_id=o.id) revision_count
                   FROM observations o WHERE o.id=?""",
                (observation_id,),
            ).fetchone()
        output = self._record(row)
        output["action"] = action
        return output

    def observation_revisions(self, series_id: Optional[str] = None,
                              limit: int = 500) -> List[Dict[str, Any]]:
        where, params = "", []
        if series_id:
            where, params = " WHERE series_id=?", [series_id]
        with self._reader() as connection:
            rows = connection.execute(
                f"SELECT * FROM observation_revisions{where} ORDER BY revised_at DESC,id DESC LIMIT ?",
                params + [_limit(limit)],
            ).fetchall()
        return [self._record(row) for row in rows]

    def history(self, series_id: str, start: Optional[str] = None,
                end: Optional[str] = None, limit: Optional[int] = None,
                bate: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = ["o.series_id=?"], [series_id]
        if start:
            where.append("o.observation_date>=?")
            params.append(normalize_date(start))
        if end:
            where.append("o.observation_date<=?")
            params.append(normalize_date(end))
        if bate is not None:
            where.append("o.bate=?")
            params.append(bate)
        sql = f"""WITH preferred AS (
                    SELECT o.*, ROW_NUMBER() OVER (
                        PARTITION BY o.series_id,o.observation_date
                        ORDER BY {BATE_PRIORITY_SQL},o.publication_time DESC,o.id DESC
                    ) bate_rank
                    FROM observations o WHERE {' AND '.join(where)}
                 )
                 SELECT o.id, o.series_id, o.observation_date date,
                    COALESCE(o.value_normalized,o.value_native) value,
                    o.value_native native_value,
                    COALESCE(o.currency_normalized,o.currency_native) currency,
                    COALESCE(o.unit_normalized,o.unit_native) unit,
                    o.currency_native native_currency, o.unit_native native_unit,
                    o.bate, o.publication_time, o.fetched_at, o.source_ref,
                    o.is_correction, o.metadata_json,
                    (SELECT COUNT(*) FROM observation_revisions r WHERE r.observation_id=o.id) revision_count
                 FROM preferred o WHERE o.bate_rank=1
                 ORDER BY o.observation_date ASC,o.bate ASC,o.id ASC"""
        with self._reader() as connection:
            rows = connection.execute(sql, params).fetchall()
        if limit is not None:
            rows = rows[-_limit(limit):]
        return [self._record(row) for row in rows]

    def latest_observations(self, as_of: Optional[str] = None,
                            summary_only: bool = False) -> List[Dict[str, Any]]:
        cutoff = normalize_date(as_of) if as_of else None
        where = "WHERE o.observation_date<=?" if cutoff else ""
        params: List[Any] = [cutoff] if cutoff else []
        summary_join = "JOIN catalog_candidates c ON c.canonical_key=s.canonical_key AND c.summary=1" if summary_only else ""
        sql = f"""
            WITH ranked AS (
                SELECT o.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.series_id
                         ORDER BY o.observation_date DESC,
                                  {BATE_PRIORITY_SQL},
                                  o.publication_time DESC,o.id DESC
                    ) AS rn
                FROM observations o {where}
            )
            SELECT s.id series_id,s.canonical_key,s.symbol,s.name,s.product,s.market,
                   s.location,s.source,s.entitlement_state,s.display_order,
                   r.observation_date,
                   COALESCE(r.value_normalized,r.value_native) value,
                   r.value_native native_value,
                   COALESCE(r.currency_normalized,r.currency_native,s.normalized_currency,s.currency) currency,
                   COALESCE(r.unit_normalized,r.unit_native,s.normalized_unit,s.unit) unit,
                   r.currency_native native_currency,r.unit_native native_unit,
                   r.bate,r.publication_time,r.fetched_at,r.is_correction
            FROM series_catalog s JOIN ranked r ON r.series_id=s.id AND r.rn=1
            {summary_join}
            WHERE s.active=1 AND s.entitlement_state='entitled'
            ORDER BY s.display_order,s.name
        """
        with self._reader() as connection:
            return [self._record(row) for row in connection.execute(sql, params)]

    def upsert_curve_point(self, value: Any) -> Dict[str, Any]:
        item = normalize_input(value, CurvePointInput)
        now = utc_now()
        tracked = (
            "delivery_start", "delivery_end", "value_native", "value_normalized",
            "currency_native", "unit_native", "currency_normalized", "unit_normalized",
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM curve_points WHERE series_id=? AND as_of_date=? AND contract_month=?",
                (item["series_id"], item["as_of_date"], item["contract_month"]),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO curve_points (
                        series_id,as_of_date,contract_month,delivery_start,delivery_end,
                        value_native,value_normalized,currency_native,unit_native,
                        currency_normalized,unit_normalized,fetched_at,source_ref,
                        ingestion_run_id,metadata_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["series_id"], item["as_of_date"], item["contract_month"],
                        item["delivery_start"], item["delivery_end"], item["value_native"],
                        item["value_normalized"], item["currency_native"], item["unit_native"],
                        item["currency_normalized"], item["unit_normalized"], item["fetched_at"],
                        item["source_ref"], item["ingestion_run_id"], item["metadata_json"], now, now,
                    ),
                )
                point_id, action = cursor.lastrowid, "inserted"
            else:
                old = dict(existing)
                changed = [key for key in tracked if old.get(key) != item.get(key)]
                point_id = old["id"]
                if changed:
                    revision = connection.execute(
                        "SELECT COUNT(*)+1 FROM curve_point_revisions WHERE curve_point_id=?",
                        (point_id,),
                    ).fetchone()[0]
                    connection.execute(
                        """INSERT INTO curve_point_revisions (
                            curve_point_id,revision_number,changed_fields_json,
                            previous_snapshot_json,new_snapshot_json,ingestion_run_id,revised_at
                        ) VALUES (?,?,?,?,?,?,?)""",
                        (
                            point_id, revision, json.dumps(changed),
                            json.dumps({key: old.get(key) for key in tracked}),
                            json.dumps({key: item.get(key) for key in tracked}),
                            item["ingestion_run_id"], now,
                        ),
                    )
                    connection.execute(
                        """UPDATE curve_points SET delivery_start=?,delivery_end=?,
                            value_native=?,value_normalized=?,currency_native=?,unit_native=?,
                            currency_normalized=?,unit_normalized=?,fetched_at=?,source_ref=?,
                            is_correction=1,ingestion_run_id=?,metadata_json=?,updated_at=? WHERE id=?""",
                        (
                            item["delivery_start"], item["delivery_end"], item["value_native"],
                            item["value_normalized"], item["currency_native"], item["unit_native"],
                            item["currency_normalized"], item["unit_normalized"], item["fetched_at"],
                            item["source_ref"], item["ingestion_run_id"], item["metadata_json"],
                            now, point_id,
                        ),
                    )
                    action = "updated"
                else:
                    connection.execute(
                        """UPDATE curve_points SET fetched_at=?,source_ref=COALESCE(?,source_ref),
                           ingestion_run_id=?,metadata_json=?,updated_at=? WHERE id=?""",
                        (item["fetched_at"], item["source_ref"], item["ingestion_run_id"],
                         item["metadata_json"], now, point_id),
                    )
                    action = "unchanged"
            row = connection.execute("SELECT * FROM curve_points WHERE id=?", (point_id,)).fetchone()
        output = self._record(row)
        output["action"] = action
        return output

    def curves(self, as_of: Optional[str] = None,
               series_id: Optional[str] = None) -> List[Dict[str, Any]]:
        filters, params = [], []
        if series_id:
            filters.append("p.series_id=?")
            params.append(series_id)
        if as_of:
            filters.append("p.as_of_date<=?")
            params.append(normalize_date(as_of))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"""WITH eligible AS (
                    SELECT p.*, MAX(p.as_of_date) OVER (PARTITION BY p.series_id) selected_as_of
                    FROM curve_points p {where}
                  )
                  SELECT e.*,s.name,s.canonical_key,s.source
                  FROM eligible e JOIN series_catalog s ON s.id=e.series_id
                   WHERE e.as_of_date=e.selected_as_of
                     AND s.active=1 AND s.entitlement_state='entitled'
                  ORDER BY s.display_order,s.name,
                    COALESCE(e.delivery_start,e.contract_month),e.contract_month"""
        with self._reader() as connection:
            return [self._record(row) for row in connection.execute(sql, params)]

    def upsert_dataset_row(self, value: Any) -> Dict[str, Any]:
        item = normalize_input(value, DatasetRowInput)
        now = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id,payload_json FROM dataset_rows WHERE dataset=? AND row_key=? AND as_of_date=?",
                (item["dataset"], item["row_key"], item["as_of_date"]),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO dataset_rows (
                        dataset,row_key,as_of_date,payload_json,source,fetched_at,
                        ingestion_run_id,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        item["dataset"], item["row_key"], item["as_of_date"],
                        item["payload_json"], item["source"], item["fetched_at"],
                        item["ingestion_run_id"], now, now,
                    ),
                )
                row_id, action = cursor.lastrowid, "inserted"
            else:
                row_id = existing["id"]
                changed = existing["payload_json"] != item["payload_json"]
                connection.execute(
                    """UPDATE dataset_rows SET payload_json=?,source=?,fetched_at=?,
                       ingestion_run_id=?,updated_at=? WHERE id=?""",
                    (item["payload_json"], item["source"], item["fetched_at"],
                     item["ingestion_run_id"], now, row_id),
                )
                action = "updated" if changed else "unchanged"
            row = connection.execute("SELECT * FROM dataset_rows WHERE id=?", (row_id,)).fetchone()
        output = self._record(row)
        output["action"] = action
        return output

    def upsert_news(self, value: Any) -> Dict[str, Any]:
        item = normalize_input(value, NewsInput)
        now = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM news WHERE article_key=?", (item["article_key"],)
            ).fetchone()
            columns = (
                "article_key", "headline", "summary", "body", "url", "source",
                "published_at", "fetched_at", "language", "region", "product", "topic",
                "direction", "importance", "tags_json", "relevance_score", "rank_score",
                "source_tier", "cluster_key", "is_breaking", "entitlement_state",
                "ingestion_run_id", "metadata_json",
            )
            connection.execute(
                f"""INSERT INTO news ({','.join(columns)},created_at,updated_at)
                    VALUES ({','.join('?' for _ in columns)},?,?)
                    ON CONFLICT(article_key) DO UPDATE SET
                        headline=excluded.headline,summary=excluded.summary,body=excluded.body,
                        url=excluded.url,source=excluded.source,published_at=excluded.published_at,
                        fetched_at=excluded.fetched_at,language=excluded.language,
                        region=excluded.region,product=excluded.product,topic=excluded.topic,
                        direction=excluded.direction,importance=excluded.importance,
                        tags_json=excluded.tags_json,relevance_score=excluded.relevance_score,
                        rank_score=excluded.rank_score,source_tier=excluded.source_tier,
                        cluster_key=excluded.cluster_key,is_breaking=excluded.is_breaking,
                        entitlement_state=excluded.entitlement_state,
                        ingestion_run_id=excluded.ingestion_run_id,metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at""",
                [item.get(column) for column in columns] + [now, now],
            )
            row = connection.execute("SELECT * FROM news WHERE article_key=?", (item["article_key"],)).fetchone()
        output = self._record(row)
        output["action"] = "updated" if existing else "inserted"
        return output

    def news(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        if filters.get("cluster_id") and not filters.get("cluster_key"):
            filters["cluster_key"] = filters["cluster_id"]
        filters["entitlement_state"] = "entitled"
        where, params = [], []
        for field in ("product", "region", "source", "direction", "importance", "topic",
                      "cluster_key", "entitlement_state"):
            if filters.get(field) not in (None, ""):
                where.append(f"{field}=?")
                params.append(filters[field])
        if filters.get("breaking") not in (None, ""):
            where.append("is_breaking=?")
            params.append(1 if str(filters["breaking"]).lower() in {"1", "true", "yes"} else 0)
        if filters.get("min_relevance") not in (None, ""):
            where.append("relevance_score>=?")
            params.append(float(filters["min_relevance"]))
        if filters.get("start"):
            where.append("published_at>=?")
            params.append(normalize_timestamp(filters["start"]) or filters["start"])
        if filters.get("end"):
            end = str(filters["end"])
            where.append("published_at<?")
            params.append(end[:10] + "T23:59:59+00:00" if len(end) <= 10 else normalize_timestamp(end))
        if filters.get("q"):
            where.append("(headline LIKE ? OR summary LIKE ? OR body LIKE ? OR tags_json LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term] * 4)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 100), _offset(filters.get("offset"))
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM news{clause}", params).fetchone()[0]
            order = """CASE WHEN is_breaking=1
                                  AND julianday(published_at) >= julianday('now','-3 hours')
                             THEN 1 ELSE 0 END DESC,
                (relevance_score * 0.68
                 + CASE
                     WHEN julianday(published_at) >= julianday('now','-3 hours') THEN 22
                     WHEN julianday(published_at) >= julianday('now','-1 day') THEN 18
                     WHEN julianday(published_at) >= julianday('now','-3 days') THEN 12
                     WHEN julianday(published_at) >= julianday('now','-7 days') THEN 6 ELSE 0 END
                 + source_tier * 2) DESC,
                published_at DESC,id DESC"""
            rows = connection.execute(
                f"SELECT * FROM news{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return {"items": [self._record(row) for row in rows], "total": total,
                "limit": limit, "offset": offset,
                "filters": {key: value for key, value in filters.items() if value not in (None, "")}}

    def upsert_news_source_health(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        item = dict(value)
        source_id = str(item.get("source_id") or "").strip()
        source_name = str(item.get("source_name") or source_id).strip()
        if not source_id or not source_name:
            raise ValueError("news source_id and source_name are required")
        attempted = normalize_timestamp(item.get("last_attempt_at")) or utc_now()
        success = normalize_timestamp(item.get("last_success_at"))
        latest = normalize_timestamp(item.get("latest_published_at"))
        now = utc_now()
        values = (
            source_id, source_name, str(item.get("kind") or "rss"),
            str(item.get("status") or "unknown"), attempted, success, latest,
            max(0, int(item.get("article_count") or 0)),
            max(0, int(item.get("relevant_count") or 0)),
            max(0, int(item.get("latency_ms") or 0)) if item.get("latency_ms") is not None else None,
            str(item.get("error"))[:1000] if item.get("error") else None,
            json.dumps(json_safe(dict(item.get("metadata") or {})), ensure_ascii=True),
            now, now,
        )
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO news_source_health (
                       source_id,source_name,kind,status,last_attempt_at,last_success_at,
                       latest_published_at,article_count,relevant_count,latency_ms,error,
                       metadata_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       source_name=excluded.source_name,kind=excluded.kind,status=excluded.status,
                       last_attempt_at=excluded.last_attempt_at,
                       last_success_at=COALESCE(excluded.last_success_at,news_source_health.last_success_at),
                       latest_published_at=COALESCE(excluded.latest_published_at,
                                                   news_source_health.latest_published_at),
                       article_count=excluded.article_count,relevant_count=excluded.relevant_count,
                       latency_ms=excluded.latency_ms,error=excluded.error,
                       metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                values,
            )
            row = connection.execute(
                "SELECT * FROM news_source_health WHERE source_id=?", (source_id,),
            ).fetchone()
        return self._record(row)

    def news_source_health(self) -> List[Dict[str, Any]]:
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM news_source_health
                   ORDER BY CASE status WHEN 'healthy' THEN 0 WHEN 'empty' THEN 1 ELSE 2 END,
                            source_name"""
            ).fetchall()
        return [self._record(row) for row in rows]

    def upsert_intelligence_event(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        from .intelligence import normalize_event_for_store

        item = normalize_event_for_store(value)
        now = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM intelligence_events WHERE event_key=?", (item["event_key"],),
            ).fetchone()
            connection.execute(_INTELLIGENCE_EVENT_UPSERT, [
                item.get(column) for column in _INTELLIGENCE_EVENT_COLUMNS
            ] + [now, item["updated_at"]])
            row = connection.execute(
                "SELECT * FROM intelligence_events WHERE event_key=?", (item["event_key"],),
            ).fetchone()
        output = self._record(row)
        output["action"] = "updated" if existing else "inserted"
        return output

    def upsert_intelligence_events(self, values: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
        """Persist a complete derived-event batch in one SQLite transaction."""
        from .intelligence import normalize_event_for_store

        items = [normalize_event_for_store(value) for value in values]
        counts = {"seen": len(items), "inserted": 0, "updated": 0}
        if not items:
            return counts
        now = utc_now()
        with self._transaction() as connection:
            existing = {
                row[0] for row in connection.execute(
                    "SELECT event_key FROM intelligence_events",
                )
            }
            for item in items:
                connection.execute(_INTELLIGENCE_EVENT_UPSERT, [
                    item.get(column) for column in _INTELLIGENCE_EVENT_COLUMNS
                ] + [now, item["updated_at"]])
                counts["updated" if item["event_key"] in existing else "inserted"] += 1
        return counts

    def intelligence_events(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        where, params = [], []
        for field in ("event_type", "severity", "region", "confirmation_state"):
            if filters.get(field) not in (None, "", "all"):
                where.append(f"{field}=?")
                params.append(filters[field])
        if filters.get("active") not in (None, "", "all"):
            where.append("active=?")
            params.append(1 if str(filters["active"]).lower() in {"1", "true", "yes"} else 0)
        if filters.get("start"):
            where.append("last_seen_at>=?")
            params.append(normalize_timestamp(filters["start"]) or str(filters["start"]))
        if filters.get("end"):
            end = str(filters["end"])
            where.append("last_seen_at<?")
            params.append(end[:10] + "T23:59:59+00:00" if len(end) <= 10 else
                          normalize_timestamp(end) or end)
        if filters.get("asset_id"):
            where.append("asset_ids_json LIKE ?")
            params.append(f'%"{str(filters["asset_id"])}"%')
        if filters.get("route_id"):
            where.append("route_ids_json LIKE ?")
            params.append(f'%"{str(filters["route_id"])}"%')
        if filters.get("q"):
            where.append("(headline LIKE ? OR location_name LIKE ? OR sources_json LIKE ? "
                         "OR affected_series_json LIKE ? OR evidence_json LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term] * 5)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 300), _offset(filters.get("offset"))
        with self._reader() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM intelligence_events{clause}", params,
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT * FROM intelligence_events{clause}
                    ORDER BY active DESC,risk_score DESC,last_seen_at DESC,id DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return {
            "items": [self._record(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "filters": {key: value for key, value in filters.items() if value not in (None, "")},
        }

    def intelligence_status(self) -> Dict[str, Any]:
        with self._reader() as connection:
            row = connection.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) active,
                          SUM(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) located,
                          SUM(CASE WHEN confirmation_state='confirmed' THEN 1 ELSE 0 END) confirmed,
                          MAX(last_seen_at) latest_event_at,MAX(updated_at) last_sync_at
                   FROM intelligence_events"""
            ).fetchone()
        return json_safe(dict(row) if row else {
            "total": 0, "active": 0, "located": 0, "confirmed": 0,
            "latest_event_at": None, "last_sync_at": None,
        })

    def intelligence_needs_sync(self) -> bool:
        with self._reader() as connection:
            news = connection.execute(
                "SELECT COUNT(*) total,MAX(updated_at) latest FROM news "
                "WHERE entitlement_state='entitled'",
            ).fetchone()
            events = connection.execute(
                "SELECT COUNT(*) total,MAX(updated_at) latest FROM intelligence_events",
            ).fetchone()
        if not news or not int(news["total"] or 0):
            return False
        return not events or not int(events["total"] or 0) or str(news["latest"] or "") > str(events["latest"] or "")

    def import_vessel_snapshot(self, snapshot: Mapping[str, Any],
                               ingestion_run_id: Optional[int] = None) -> Dict[str, int]:
        """Persist a normalized vessel snapshot without creating live positions."""
        vessels = [dict(item) for item in snapshot.get("vessels") or []]
        calls = [dict(item) for item in snapshot.get("port_calls") or []]
        health = [dict(item) for item in snapshot.get("source_health") or []]
        counts = {
            "vessels_seen": len(vessels), "vessels_inserted": 0,
            "vessels_updated": 0, "vessels_unchanged": 0,
            "port_calls_seen": len(calls), "port_calls_inserted": 0,
            "port_calls_updated": 0, "port_calls_unchanged": 0,
            "positions_inserted": 0,
        }
        now = utc_now()
        with self._transaction() as connection:
            for item in vessels:
                vessel_id = str(item.get("vessel_id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not vessel_id or not name:
                    raise ValueError("vessel_id and name are required")
                values = (
                    name, item.get("imo"), item.get("mmsi"), item.get("vessel_type"),
                    item.get("fleet_group"), 1 if item.get("active", True) else 0,
                    json.dumps(json_safe(item.get("metadata") or {}), ensure_ascii=True),
                )
                existing = connection.execute(
                    "SELECT name,imo,mmsi,vessel_type,fleet_group,active,metadata_json "
                    "FROM vessels WHERE vessel_id=?", (vessel_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO vessels
                           (vessel_id,name,imo,mmsi,vessel_type,fleet_group,active,
                            metadata_json,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (vessel_id, *values, now, now),
                    )
                    counts["vessels_inserted"] += 1
                elif tuple(existing) == values:
                    counts["vessels_unchanged"] += 1
                else:
                    connection.execute(
                        """UPDATE vessels SET name=?,imo=?,mmsi=?,vessel_type=?,fleet_group=?,
                           active=?,metadata_json=?,updated_at=? WHERE vessel_id=?""",
                        (*values, now, vessel_id),
                    )
                    counts["vessels_updated"] += 1

            call_columns = (
                "call_key", "vessel_id", "port_name", "port_name_local", "locode",
                "country", "country_code", "arrived_at", "berthed_at", "departed_at",
                "timestamp_state", "source_timezone", "stay_hours", "latitude", "longitude",
                "geo_precision", "draught_arrival", "draught_departure", "draught_change",
                "operation_signal", "operation_signal_state", "next_destination", "source",
                "source_detail", "evidence_state", "source_snapshot_at", "evidence_hash",
                "raw_json", "metadata_json", "ingestion_run_id",
            )
            for item in calls:
                call_key = str(item.get("call_key") or "").strip()
                if not call_key or not item.get("vessel_id") or not item.get("port_name"):
                    raise ValueError("call_key, vessel_id, and port_name are required")
                item["raw_json"] = json.dumps(
                    json_safe(item.pop("raw", item.get("raw_json") or {})), ensure_ascii=True,
                )
                item["metadata_json"] = json.dumps(
                    json_safe(item.pop("metadata", item.get("metadata_json") or {})), ensure_ascii=True,
                )
                item["ingestion_run_id"] = ingestion_run_id
                existing = connection.execute(
                    "SELECT evidence_hash FROM vessel_port_calls WHERE call_key=?", (call_key,),
                ).fetchone()
                values = [item.get(column) for column in call_columns]
                if existing is None:
                    connection.execute(
                        f"""INSERT INTO vessel_port_calls
                            ({','.join(call_columns)},created_at,updated_at)
                            VALUES ({','.join('?' for _ in call_columns)},?,?)""",
                        values + [now, now],
                    )
                    counts["port_calls_inserted"] += 1
                elif str(existing["evidence_hash"]) == str(item.get("evidence_hash")):
                    counts["port_calls_unchanged"] += 1
                else:
                    assignments = ",".join(
                        f"{column}=?" for column in call_columns if column != "call_key"
                    )
                    connection.execute(
                        f"UPDATE vessel_port_calls SET {assignments},updated_at=? WHERE call_key=?",
                        [item.get(column) for column in call_columns if column != "call_key"]
                        + [now, call_key],
                    )
                    counts["port_calls_updated"] += 1

            for item in health:
                source_id = str(item.get("source_id") or "").strip()
                source_name = str(item.get("source_name") or source_id).strip()
                if not source_id or not source_name:
                    raise ValueError("vessel source_id and source_name are required")
                connection.execute(
                    """INSERT INTO vessel_source_health
                       (source_id,source_name,capabilities_json,access_state,status,
                        entitlement_state,last_attempt_at,last_success_at,latest_observation_at,
                        vessel_count,row_count,error,metadata_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id) DO UPDATE SET
                         source_name=excluded.source_name,
                         capabilities_json=excluded.capabilities_json,
                         access_state=excluded.access_state,status=excluded.status,
                         entitlement_state=excluded.entitlement_state,
                         last_attempt_at=excluded.last_attempt_at,
                         last_success_at=excluded.last_success_at,
                         latest_observation_at=excluded.latest_observation_at,
                         vessel_count=excluded.vessel_count,row_count=excluded.row_count,
                         error=excluded.error,metadata_json=excluded.metadata_json,
                         updated_at=excluded.updated_at""",
                    (
                        source_id, source_name,
                        json.dumps(json_safe(item.get("capabilities") or []), ensure_ascii=True),
                        str(item.get("access_state") or "unknown"),
                        str(item.get("status") or "unknown"),
                        str(item.get("entitlement_state") or "unverified"),
                        str(item.get("last_attempt_at") or now), item.get("last_success_at"),
                        item.get("latest_observation_at"), max(0, int(item.get("vessel_count") or 0)),
                        max(0, int(item.get("row_count") or 0)),
                        str(item.get("error"))[:1000] if item.get("error") else None,
                        json.dumps(json_safe(item.get("metadata") or {}), ensure_ascii=True),
                        now, now,
                    ),
                )
        return counts

    def upsert_vessel_position(self, value: Mapping[str, Any],
                               ingestion_run_id: Optional[int] = None) -> Dict[str, Any]:
        """Persist an actual timestamped position supplied by a configured provider."""
        item = dict(value)
        required = ("position_key", "vessel_id", "observed_at", "latitude", "longitude", "source")
        if any(item.get(key) in (None, "") for key in required):
            raise ValueError("position_key, vessel_id, observed_at, latitude, longitude, and source are required")
        latitude, longitude = float(item["latitude"]), float(item["longitude"])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("vessel position coordinates are out of range")
        kind = str(item.get("position_kind") or "historical")
        if kind not in {"live", "recent", "historical"}:
            raise ValueError("invalid vessel position_kind")
        now = utc_now()
        columns = (
            "position_key", "vessel_id", "observed_at", "timestamp_state", "latitude",
            "longitude", "speed_kn", "course_deg", "navigation_state", "destination",
            "position_kind", "source", "evidence_state", "raw_json", "metadata_json",
            "ingestion_run_id",
        )
        item.update({
            "latitude": latitude,
            "longitude": longitude,
            "timestamp_state": str(item.get("timestamp_state") or "normalized_utc"),
            "position_kind": kind,
            "evidence_state": str(item.get("evidence_state") or "source_reported"),
            "raw_json": json.dumps(json_safe(item.get("raw") or {}), ensure_ascii=True),
            "metadata_json": json.dumps(json_safe(item.get("metadata") or {}), ensure_ascii=True),
            "ingestion_run_id": ingestion_run_id,
        })
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM vessel_positions WHERE position_key=?", (item["position_key"],),
            ).fetchone()
            connection.execute(
                f"""INSERT INTO vessel_positions
                    ({','.join(columns)},created_at,updated_at)
                    VALUES ({','.join('?' for _ in columns)},?,?)
                    ON CONFLICT(position_key) DO UPDATE SET
                      observed_at=excluded.observed_at,timestamp_state=excluded.timestamp_state,
                      latitude=excluded.latitude,longitude=excluded.longitude,
                      speed_kn=excluded.speed_kn,course_deg=excluded.course_deg,
                      navigation_state=excluded.navigation_state,destination=excluded.destination,
                      position_kind=excluded.position_kind,source=excluded.source,
                      evidence_state=excluded.evidence_state,raw_json=excluded.raw_json,
                      metadata_json=excluded.metadata_json,ingestion_run_id=excluded.ingestion_run_id,
                      updated_at=excluded.updated_at""",
                [item.get(column) for column in columns] + [now, now],
            )
            row = connection.execute(
                "SELECT * FROM vessel_positions WHERE position_key=?", (item["position_key"],),
            ).fetchone()
        output = self._record(row)
        output["action"] = "updated" if existing else "inserted"
        return output

    def vessels(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        where, params = [], []
        if filters.get("active") not in (None, "", "all"):
            where.append("active=?")
            params.append(1 if str(filters["active"]).lower() in {"1", "true", "yes"} else 0)
        if filters.get("fleet_group") not in (None, "", "all"):
            where.append("fleet_group=?")
            params.append(filters["fleet_group"])
        if filters.get("q"):
            where.append("(name LIKE ? OR imo LIKE ? OR mmsi LIKE ? OR fleet_group LIKE ?)")
            params.extend([f"%{filters['q']}%"] * 4)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 100), _offset(filters.get("offset"))
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM vessels{clause}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM vessels{clause} ORDER BY active DESC,name LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            items = []
            for raw in rows:
                vessel = self._record(raw)
                call = connection.execute(
                    """SELECT * FROM vessel_port_calls WHERE vessel_id=?
                       ORDER BY COALESCE(arrived_at,departed_at,berthed_at) DESC,id DESC LIMIT 1""",
                    (vessel["vessel_id"],),
                ).fetchone()
                position = connection.execute(
                    """SELECT * FROM vessel_positions WHERE vessel_id=?
                       ORDER BY observed_at DESC,id DESC LIMIT 1""",
                    (vessel["vessel_id"],),
                ).fetchone()
                vessel["port_call_count"] = connection.execute(
                    "SELECT COUNT(*) FROM vessel_port_calls WHERE vessel_id=?",
                    (vessel["vessel_id"],),
                ).fetchone()[0]
                vessel["position_count"] = connection.execute(
                    "SELECT COUNT(*) FROM vessel_positions WHERE vessel_id=?",
                    (vessel["vessel_id"],),
                ).fetchone()[0]
                vessel["last_port_call"] = self._record(call)
                vessel["last_position"] = self._record(position)
                items.append(vessel)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def vessel_port_calls(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        where, params = [], []
        for field in ("vessel_id", "source", "locode", "operation_signal"):
            if filters.get(field) not in (None, "", "all"):
                where.append(f"c.{field}=?")
                params.append(filters[field])
        if filters.get("start"):
            where.append("COALESCE(c.arrived_at,c.departed_at,c.berthed_at)>=?")
            params.append(str(filters["start"]))
        if filters.get("end"):
            where.append("COALESCE(c.arrived_at,c.departed_at,c.berthed_at)<=?")
            params.append(str(filters["end"]))
        if filters.get("q"):
            where.append("(v.name LIKE ? OR c.port_name LIKE ? OR c.locode LIKE ? OR c.next_destination LIKE ?)")
            params.extend([f"%{filters['q']}%"] * 4)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 500), _offset(filters.get("offset"))
        base = " FROM vessel_port_calls c JOIN vessels v ON v.vessel_id=c.vessel_id"
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*){base}{clause}", params).fetchone()[0]
            rows = connection.execute(
                f"""SELECT c.*,v.name vessel_name,v.imo,v.mmsi,v.fleet_group{base}{clause}
                    ORDER BY COALESCE(c.arrived_at,c.departed_at,c.berthed_at) DESC,c.id DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return {"items": [self._record(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def vessel_positions(self, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        filters = dict(filters or {})
        where, params = [], []
        if filters.get("vessel_id"):
            where.append("p.vessel_id=?")
            params.append(filters["vessel_id"])
        if filters.get("position_kind") not in (None, "", "all"):
            where.append("p.position_kind=?")
            params.append(filters["position_kind"])
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        limit, offset = _limit(filters.get("limit"), 500), _offset(filters.get("offset"))
        base = " FROM vessel_positions p JOIN vessels v ON v.vessel_id=p.vessel_id"
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*){base}{clause}", params).fetchone()[0]
            rows = connection.execute(
                f"""SELECT p.*,v.name vessel_name,v.imo,v.mmsi{base}{clause}
                    ORDER BY p.observed_at DESC,p.id DESC LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return {"items": [self._record(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def vessel_source_health(self) -> List[Dict[str, Any]]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM vessel_source_health ORDER BY source_name",
            ).fetchall()
        return [self._record(row) for row in rows]

    def vessel_intelligence_status(self) -> Dict[str, Any]:
        with self._reader() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM vessels) vessels,
                     (SELECT COUNT(*) FROM vessel_port_calls) historical_port_calls,
                     (SELECT COUNT(*) FROM vessel_positions) positions,
                     (SELECT COUNT(*) FROM vessel_positions WHERE position_kind='live') live_positions,
                     (SELECT MAX(COALESCE(arrived_at,departed_at,berthed_at)) FROM vessel_port_calls) latest_port_call_at,
                     (SELECT MAX(observed_at) FROM vessel_positions) latest_position_at,
                     (SELECT MAX(updated_at) FROM vessel_source_health) last_source_update_at""",
            ).fetchone()
        return json_safe(dict(row))

    def start_run(self, source: str, scope: str = "all",
                  metadata: Optional[Mapping[str, Any]] = None,
                  started_at: Optional[str] = None) -> Dict[str, Any]:
        when = normalize_timestamp(started_at) or utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO ingestion_runs
                   (source,scope,status,started_at,metadata_json) VALUES (?,?,?,?,?)""",
                (source, scope, "running", when,
                 json.dumps(json_safe(dict(metadata or {})), ensure_ascii=True)),
            )
            row = connection.execute("SELECT * FROM ingestion_runs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._record(row)

    def finish_run(self, run_id: int, status: str = "success", **counts: Any) -> Dict[str, Any]:
        if status not in {"success", "partial", "failed"}:
            raise ValueError(f"invalid final run status: {status}")
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE ingestion_runs SET status=?,finished_at=?,rows_seen=?,rows_inserted=?,
                   rows_updated=?,rows_skipped=?,error=? WHERE id=?""",
                (
                    status, utc_now(), int(counts.get("rows_seen", 0)),
                    int(counts.get("rows_inserted", 0)), int(counts.get("rows_updated", 0)),
                    int(counts.get("rows_skipped", 0)), counts.get("error"), run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown ingestion run: {run_id}")
            row = connection.execute("SELECT * FROM ingestion_runs WHERE id=?", (run_id,)).fetchone()
        return self._record(row)

    def recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM ingestion_runs ORDER BY started_at DESC,id DESC LIMIT ?",
                (_limit(limit, 20, 200),),
            ).fetchall()
        return [self._record(row) for row in rows]

    def explorer(self, dataset: str = "observations", **filters: Any) -> Dict[str, Any]:
        allowed = {
            "observations", "curves", "news", "series", "candidates",
            "revisions", "dataset_rows",
        }
        if dataset not in allowed:
            raise ValueError(f"unsupported explorer dataset: {dataset}")
        limit, offset = _limit(filters.get("limit"), 500), _offset(filters.get("offset"))
        where, params = [], []
        query = str(filters.get("q") or "").strip()
        entitlement = str(filters.get("entitlement") or
                          filters.get("entitlement_state") or "").strip()
        if dataset == "observations":
            where.append("s.entitlement_state='entitled' AND s.active=1")
            if filters.get("series_id"):
                where.append("o.series_id=?")
                params.append(filters["series_id"])
            if filters.get("start"):
                where.append("o.observation_date>=?")
                params.append(normalize_date(filters["start"]))
            if filters.get("end"):
                where.append("o.observation_date<=?")
                params.append(normalize_date(filters["end"]))
            if query:
                where.append("(s.name LIKE ? OR s.symbol LIKE ? OR s.canonical_key LIKE ? OR o.source_ref LIKE ?)")
                params.extend([f"%{query}%"] * 4)
            if entitlement and entitlement != "entitled":
                where.append("1=0")
            base = """FROM observations o JOIN series_catalog s ON s.id=o.series_id"""
            select = """SELECT o.id,o.series_id,s.canonical_key,s.symbol,s.name,
                o.observation_date,COALESCE(o.value_normalized,o.value_native) value,
                COALESCE(o.currency_normalized,o.currency_native) currency,
                COALESCE(o.unit_normalized,o.unit_native) unit,o.value_native,
                o.currency_native,o.unit_native,o.bate,o.publication_time,o.fetched_at,
                o.source_ref,o.is_correction,o.metadata_json"""
            order = "o.observation_date DESC,o.id DESC"
        elif dataset == "curves":
            where.append("s.entitlement_state='entitled' AND s.active=1")
            if filters.get("series_id"):
                where.append("p.series_id=?")
                params.append(filters["series_id"])
            if filters.get("start"):
                where.append("p.as_of_date>=?")
                params.append(normalize_date(filters["start"]))
            if filters.get("end"):
                where.append("p.as_of_date<=?")
                params.append(normalize_date(filters["end"]))
            if query:
                where.append("(s.name LIKE ? OR s.symbol LIKE ? OR s.canonical_key LIKE ? OR p.contract_month LIKE ?)")
                params.extend([f"%{query}%"] * 4)
            if entitlement and entitlement != "entitled":
                where.append("1=0")
            base = "FROM curve_points p JOIN series_catalog s ON s.id=p.series_id"
            select = """SELECT p.id,p.series_id,s.canonical_key,s.symbol,s.name,p.as_of_date,
                p.contract_month,p.delivery_start,p.delivery_end,
                COALESCE(p.value_normalized,p.value_native) value,
                COALESCE(p.currency_normalized,p.currency_native) currency,
                COALESCE(p.unit_normalized,p.unit_native) unit,p.fetched_at,p.source_ref,
                p.is_correction,p.metadata_json"""
            order = "p.as_of_date DESC,p.contract_month,p.id"
        elif dataset == "news":
            where.append("n.entitlement_state='entitled'")
            if query:
                where.append("(n.headline LIKE ? OR n.summary LIKE ? OR n.source LIKE ? OR n.tags_json LIKE ?)")
                params.extend([f"%{query}%"] * 4)
            if entitlement and entitlement != "entitled":
                where.append("1=0")
            base, select, order = "FROM news n", "SELECT n.*", "n.published_at DESC,n.id DESC"
        elif dataset == "series":
            if query:
                where.append("(s.name LIKE ? OR s.symbol LIKE ? OR s.canonical_key LIKE ? OR s.description LIKE ?)")
                params.extend([f"%{query}%"] * 4)
            if entitlement:
                where.append("s.entitlement_state=?")
                params.append(entitlement)
            base, select, order = "FROM series_catalog s", "SELECT s.*", "s.display_order,s.name"
        elif dataset == "candidates":
            if query:
                where.append("(c.name LIKE ? OR c.canonical_key LIKE ? OR c.search_terms_json LIKE ?)")
                params.extend([f"%{query}%"] * 3)
            if entitlement:
                where.append("c.discovery_status=?")
                params.append(entitlement)
            base, select, order = "FROM catalog_candidates c", "SELECT c.*", "c.priority,c.name"
        elif dataset == "revisions":
            where.append("s.entitlement_state='entitled' AND s.active=1")
            if filters.get("series_id"):
                where.append("r.series_id=?")
                params.append(filters["series_id"])
            if query:
                where.append("(s.name LIKE ? OR s.symbol LIKE ? OR s.canonical_key LIKE ?)")
                params.extend([f"%{query}%"] * 3)
            if entitlement and entitlement != "entitled":
                where.append("1=0")
            base, select, order = (
                "FROM observation_revisions r JOIN series_catalog s ON s.id=r.series_id",
                "SELECT r.*", "r.revised_at DESC,r.id DESC",
            )
        else:
            if not filters.get("name"):
                raise ValueError("dataset_rows explorer requires name=<dataset>")
            where.append("d.dataset=?")
            params.append(filters["name"])
            if filters.get("start"):
                where.append("d.as_of_date>=?")
                params.append(normalize_date(filters["start"]))
            if filters.get("end"):
                where.append("d.as_of_date<=?")
                params.append(normalize_date(filters["end"]))
            if query:
                where.append("(d.payload_json LIKE ? OR d.source LIKE ? OR d.row_key LIKE ?)")
                params.extend([f"%{query}%"] * 3)
            base, select, order = "FROM dataset_rows d", "SELECT d.*", "d.as_of_date DESC,d.id DESC"
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._reader() as connection:
            total = connection.execute(f"SELECT COUNT(*) {base}{clause}", params).fetchone()[0]
            rows = connection.execute(
                f"{select} {base}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        flat_rows = [dict(row) for row in rows]
        columns = list(flat_rows[0]) if flat_rows else []
        return json_safe({"dataset": dataset, "columns": columns, "rows": flat_rows,
                          "total": total, "limit": limit, "offset": offset})

    def dataset_health(self) -> Dict[str, Dict[str, Any]]:
        """Return stable, entitlement-gated coverage metrics by LPG dataset.

        ``observations`` stores both the current snapshot and accumulated
        history, so those two health rows deliberately use different views of
        the same table: current is the latest effective BATE per series while
        history counts one preferred BATE per series/date.
        """
        with self._reader() as connection:
            current = connection.execute(
                f"""WITH ranked AS (
                        SELECT o.series_id,o.observation_date,
                               ROW_NUMBER() OVER (
                                   PARTITION BY o.series_id
                                   ORDER BY o.observation_date DESC,
                                            {BATE_PRIORITY_SQL},
                                            o.publication_time DESC,o.id DESC
                               ) row_rank
                        FROM observations o
                        JOIN series_catalog s ON s.id=o.series_id
                        WHERE s.active=1 AND s.entitlement_state='entitled'
                    )
                    SELECT COUNT(*) rows,COUNT(DISTINCT observation_date) dates,
                           COUNT(DISTINCT series_id) series,
                           MIN(observation_date) first_date,
                           MAX(observation_date) last_date
                    FROM ranked WHERE row_rank=1"""
            ).fetchone()
            history = connection.execute(
                f"""WITH preferred AS (
                        SELECT o.series_id,o.observation_date,
                               ROW_NUMBER() OVER (
                                   PARTITION BY o.series_id,o.observation_date
                                   ORDER BY {BATE_PRIORITY_SQL},
                                            o.publication_time DESC,o.id DESC
                               ) bate_rank
                        FROM observations o
                        JOIN series_catalog s ON s.id=o.series_id
                        WHERE s.active=1 AND s.entitlement_state='entitled'
                    ), effective AS (
                        SELECT series_id,observation_date
                        FROM preferred WHERE bate_rank=1
                    ), coverage AS (
                        SELECT series_id,COUNT(*) date_count
                        FROM effective GROUP BY series_id
                    )
                    SELECT (SELECT COUNT(*) FROM effective) rows,
                           (SELECT COUNT(DISTINCT observation_date) FROM effective) dates,
                           (SELECT COUNT(*) FROM coverage) series,
                           (SELECT COUNT(*) FROM coverage WHERE date_count>1) multi_date_series,
                           (SELECT MIN(observation_date) FROM effective) first_date,
                           (SELECT MAX(observation_date) FROM effective) last_date"""
            ).fetchone()
            official_curves = connection.execute(
                """SELECT COUNT(*) rows,COUNT(DISTINCT p.as_of_date) dates,
                          COUNT(DISTINCT p.series_id) series,
                          MIN(p.as_of_date) first_date,MAX(p.as_of_date) last_date
                   FROM curve_points p
                   JOIN series_catalog s ON s.id=p.series_id
                   WHERE s.active=1 AND s.entitlement_state='entitled'"""
            ).fetchone()
            dataset_rows = connection.execute(
                """SELECT dataset,row_key,as_of_date,payload_json
                   FROM dataset_rows
                   WHERE dataset IN ('platts_ewindow','platts_fundamentals')"""
            ).fetchall()

        def metric(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
            raw = dict(row) if row is not None else {}
            return {
                "rows": int(raw.get("rows") or 0),
                "dates": int(raw.get("dates") or 0),
                "series": int(raw.get("series") or 0),
                "first_date": raw.get("first_date"),
                "last_date": raw.get("last_date"),
            }

        current_metric = metric(current)
        current_metric.update({
            "status": "ready" if current_metric["rows"] else "empty",
            "reason": None if current_metric["rows"] else "no_entitled_current_observations",
        })

        history_metric = metric(history)
        history_metric["multi_date_series"] = int(
            dict(history).get("multi_date_series") or 0
        ) if history is not None else 0
        if not history_metric["rows"]:
            history_metric.update({
                "status": "empty", "reason": "no_entitled_history_observations",
            })
        elif not history_metric["multi_date_series"]:
            history_metric.update({
                "status": "limited", "reason": "single_date_only",
            })
        elif history_metric["multi_date_series"] < history_metric["series"]:
            history_metric.update({
                "status": "partial", "reason": "some_series_have_single_date_only",
            })
        else:
            history_metric.update({"status": "ready", "reason": None})

        curve_metric = metric(official_curves)
        curve_metric.update({
            "official_rows": curve_metric["rows"],
            "official_series": curve_metric["series"],
            "status": "ready" if curve_metric["rows"] else "empty",
            "reason": None if curve_metric["rows"] else "no_official_curve_points",
        })

        supplemental: Dict[str, Dict[str, Any]] = {
            "platts_ewindow": {"rows": 0, "dates": set(), "series": set()},
            "platts_fundamentals": {"rows": 0, "dates": set(), "series": set()},
        }
        for row in dataset_rows:
            bucket = supplemental[row["dataset"]]
            bucket["rows"] += 1
            bucket["dates"].add(row["as_of_date"])
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, Mapping):
                identity = (payload.get("series_id") or payload.get("data_series") or
                            payload.get("symbol"))
                if identity not in (None, ""):
                    bucket["series"].add(str(identity))

        def supplemental_metric(dataset: str, reason: str) -> Dict[str, Any]:
            raw = supplemental[dataset]
            dates = sorted(raw["dates"])
            rows = int(raw["rows"])
            return {
                "rows": rows,
                "dates": len(dates),
                "series": len(raw["series"]),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "status": "ready" if rows else "empty",
                "reason": None if rows else reason,
            }

        return json_safe({
            "current": current_metric,
            "history": history_metric,
            "curves": curve_metric,
            "moc": supplemental_metric("platts_ewindow", "no_moc_rows"),
            "fundamentals": supplemental_metric(
                "platts_fundamentals", "no_fundamentals_rows",
            ),
        })

    def status(self) -> Dict[str, Any]:
        with self._reader() as connection:
            states = {row["entitlement_state"]: row["count"] for row in connection.execute(
                "SELECT entitlement_state,COUNT(*) count FROM series_catalog GROUP BY entitlement_state"
            )}
            candidate_states = {row["discovery_status"]: row["count"] for row in connection.execute(
                "SELECT discovery_status,COUNT(*) count FROM catalog_candidates GROUP BY discovery_status"
            )}
            source_rows = connection.execute(
                """SELECT s.source,COUNT(DISTINCT s.id) series_count,
                   COUNT(DISTINCT CASE WHEN s.entitlement_state='entitled' THEN s.id END) entitled_count,
                   MAX(o.fetched_at) latest_data_at,MAX(o.observation_date) latest_observation_date
                   FROM series_catalog s LEFT JOIN observations o ON o.series_id=s.id
                   GROUP BY s.source ORDER BY s.source"""
            ).fetchall()
            latest_runs = connection.execute(
                """SELECT r.* FROM ingestion_runs r JOIN (
                       SELECT source,MAX(id) id FROM ingestion_runs GROUP BY source
                   ) latest ON latest.id=r.id"""
            ).fetchall()
        runs_by_source = {row["source"]: self._record(row) for row in latest_runs}
        sources = []
        for raw in source_rows:
            row = dict(raw)
            run = runs_by_source.get(row["source"])
            if run and run["status"] == "failed":
                state = "error"
            elif row["latest_data_at"]:
                state = "ok"
            elif run and run["status"] == "running":
                state = "running"
            else:
                state = "no_data"
            row.update({
                "status": state,
                "last_attempt_at": run["started_at"] if run else None,
                "last_success_at": run["finished_at"] if run and run["status"] == "success" else None,
                "error": run["error"] if run else None,
            })
            sources.append(json_safe(row))
        db_size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "database": {"path": str(self.path), "schema_version": SCHEMA_VERSION,
                         "size_bytes": db_size},
            "catalog": {"total": sum(states.values()), "states": states},
            "candidates": {"total": sum(candidate_states.values()), "states": candidate_states},
            "datasets": self.dataset_health(),
            "intelligence": self.intelligence_status(),
            "vessel_intelligence": self.vessel_intelligence_status(),
            "sources": sources,
            "news_sources": self.news_source_health(),
            "runs": self.recent_runs(20),
            "updated_at": utc_now(),
        }
