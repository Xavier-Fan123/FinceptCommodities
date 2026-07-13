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
SCHEMA_VERSION = 2

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
        for key in ("active", "summary", "is_correction"):
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
                "direction", "importance", "tags_json", "entitlement_state",
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
                        tags_json=excluded.tags_json,entitlement_state=excluded.entitlement_state,
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
        filters["entitlement_state"] = "entitled"
        where, params = [], []
        for field in ("product", "region", "source", "direction", "importance", "topic",
                      "entitlement_state"):
            if filters.get(field) not in (None, ""):
                where.append(f"{field}=?")
                params.append(filters[field])
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
            rows = connection.execute(
                f"SELECT * FROM news{clause} ORDER BY published_at DESC,id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return {"items": [self._record(row) for row in rows], "total": total,
                "limit": limit, "offset": offset,
                "filters": {key: value for key, value in filters.items() if value not in (None, "")}}

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
            "sources": sources,
            "runs": self.recent_runs(20),
            "updated_at": utc_now(),
        }
