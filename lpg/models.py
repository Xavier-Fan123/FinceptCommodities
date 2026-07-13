"""Normalized write models and JSON-safe payload helpers."""

import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Union


ENTITLEMENT_STATES = frozenset(
    {"entitled", "unentitled", "pending_review", "retired", "error"}
)
RUN_STATUSES = frozenset({"running", "success", "partial", "failed"})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_date(value: Union[str, date, datetime]) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def normalize_timestamp(value: Optional[Union[str, datetime]]) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def finite_number(value: Optional[Any]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected numeric value, got {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError("numeric values must be finite")
    return number


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def json_text(value: Any, default: Any) -> str:
    return json.dumps(json_safe(default if value is None else value),
                      ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass
class SeriesInput:
    id: str
    name: str
    source: str
    symbol: Optional[str] = None
    canonical_key: Optional[str] = None
    product: Optional[str] = None
    market: Optional[str] = None
    region: Optional[str] = None
    location: Optional[str] = None
    basis: Optional[str] = None
    delivery_type: Optional[str] = None
    quote_kind: str = "assessment"
    currency: Optional[str] = None
    unit: Optional[str] = None
    normalized_currency: Optional[str] = None
    normalized_unit: Optional[str] = None
    frequency: Optional[str] = None
    source_dataset: Optional[str] = None
    entitlement_state: str = "pending_review"
    entitlement_reason: Optional[str] = None
    description: Optional[str] = None
    active: bool = True
    display_order: int = 1000
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> Dict[str, Any]:
        if not self.id.strip() or not self.name.strip() or not self.source.strip():
            raise ValueError("series id, name, and source are required")
        if self.entitlement_state not in ENTITLEMENT_STATES:
            raise ValueError(f"invalid entitlement state: {self.entitlement_state}")
        out = asdict(self)
        out["id"] = self.id.strip()
        out["name"] = self.name.strip()
        out["source"] = self.source.strip()
        out["active"] = 1 if self.active else 0
        out["metadata_json"] = json_text(out.pop("metadata"), {})
        return out


@dataclass
class ObservationInput:
    series_id: str
    observation_date: Union[str, date, datetime]
    value_native: float
    currency_native: Optional[str] = None
    unit_native: Optional[str] = None
    value_normalized: Optional[float] = None
    currency_normalized: Optional[str] = None
    unit_normalized: Optional[str] = None
    bate: str = ""
    publication_time: Optional[Union[str, datetime]] = None
    fetched_at: Optional[Union[str, datetime]] = None
    source_ref: Optional[str] = None
    revision_reason: Optional[str] = None
    ingestion_run_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> Dict[str, Any]:
        if not self.series_id.strip():
            raise ValueError("series_id is required")
        native = finite_number(self.value_native)
        normalized = finite_number(self.value_normalized)
        return {
            "series_id": self.series_id.strip(),
            "observation_date": normalize_date(self.observation_date),
            "value_native": native,
            "currency_native": self.currency_native,
            "unit_native": self.unit_native,
            "value_normalized": normalized,
            "currency_normalized": self.currency_normalized,
            "unit_normalized": self.unit_normalized,
            "bate": (self.bate or "").strip(),
            "publication_time": normalize_timestamp(self.publication_time),
            "fetched_at": normalize_timestamp(self.fetched_at) or utc_now(),
            "source_ref": self.source_ref,
            "revision_reason": self.revision_reason,
            "ingestion_run_id": self.ingestion_run_id,
            "metadata_json": json_text(self.metadata, {}),
        }


@dataclass
class CurvePointInput:
    series_id: str
    as_of_date: Union[str, date, datetime]
    contract_month: str
    value_native: float
    delivery_start: Optional[Union[str, date, datetime]] = None
    delivery_end: Optional[Union[str, date, datetime]] = None
    value_normalized: Optional[float] = None
    currency_native: Optional[str] = None
    unit_native: Optional[str] = None
    currency_normalized: Optional[str] = None
    unit_normalized: Optional[str] = None
    fetched_at: Optional[Union[str, datetime]] = None
    source_ref: Optional[str] = None
    ingestion_run_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> Dict[str, Any]:
        if not self.series_id.strip() or not self.contract_month.strip():
            raise ValueError("series_id and contract_month are required")
        return {
            "series_id": self.series_id.strip(),
            "as_of_date": normalize_date(self.as_of_date),
            "contract_month": self.contract_month.strip(),
            "delivery_start": normalize_date(self.delivery_start) if self.delivery_start else None,
            "delivery_end": normalize_date(self.delivery_end) if self.delivery_end else None,
            "value_native": finite_number(self.value_native),
            "value_normalized": finite_number(self.value_normalized),
            "currency_native": self.currency_native,
            "unit_native": self.unit_native,
            "currency_normalized": self.currency_normalized,
            "unit_normalized": self.unit_normalized,
            "fetched_at": normalize_timestamp(self.fetched_at) or utc_now(),
            "source_ref": self.source_ref,
            "ingestion_run_id": self.ingestion_run_id,
            "metadata_json": json_text(self.metadata, {}),
        }


@dataclass
class DatasetRowInput:
    dataset: str
    row_key: str
    as_of_date: Union[str, date, datetime]
    payload: Dict[str, Any]
    source: str
    fetched_at: Optional[Union[str, datetime]] = None
    ingestion_run_id: Optional[int] = None

    def normalized(self) -> Dict[str, Any]:
        if not self.dataset.strip() or not self.row_key.strip() or not self.source.strip():
            raise ValueError("dataset, row_key, and source are required")
        return {
            "dataset": self.dataset.strip(),
            "row_key": self.row_key.strip(),
            "as_of_date": normalize_date(self.as_of_date),
            "payload_json": json_text(self.payload, {}),
            "source": self.source.strip(),
            "fetched_at": normalize_timestamp(self.fetched_at) or utc_now(),
            "ingestion_run_id": self.ingestion_run_id,
        }


@dataclass
class NewsInput:
    article_key: str
    headline: str
    source: str
    published_at: Union[str, datetime]
    url: Optional[str] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    language: str = "en"
    region: Optional[str] = None
    product: Optional[str] = None
    topic: Optional[str] = None
    direction: Optional[str] = None
    importance: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    entitlement_state: str = "entitled"
    fetched_at: Optional[Union[str, datetime]] = None
    ingestion_run_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> Dict[str, Any]:
        if not self.article_key.strip() or not self.headline.strip() or not self.source.strip():
            raise ValueError("article_key, headline, and source are required")
        if self.entitlement_state not in ENTITLEMENT_STATES:
            raise ValueError(f"invalid entitlement state: {self.entitlement_state}")
        return {
            "article_key": self.article_key.strip(),
            "headline": self.headline.strip(),
            "source": self.source.strip(),
            "published_at": normalize_timestamp(self.published_at),
            "url": self.url,
            "summary": self.summary,
            "body": self.body,
            "language": self.language,
            "region": self.region,
            "product": self.product,
            "topic": self.topic,
            "direction": self.direction,
            "importance": self.importance,
            "tags_json": json_text(self.tags, []),
            "entitlement_state": self.entitlement_state,
            "fetched_at": normalize_timestamp(self.fetched_at) or utc_now(),
            "ingestion_run_id": self.ingestion_run_id,
            "metadata_json": json_text(self.metadata, {}),
        }


def normalize_input(value: Any, model_type: Any) -> Dict[str, Any]:
    """Accept a normalized model or a mapping and return a DB-ready dict."""
    if isinstance(value, model_type):
        return value.normalized()
    if isinstance(value, Mapping):
        return model_type(**dict(value)).normalized()
    raise TypeError(f"expected {model_type.__name__} or mapping")
