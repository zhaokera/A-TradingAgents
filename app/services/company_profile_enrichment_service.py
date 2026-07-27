"""Evidence-backed company profile selection and bounded provider refresh."""

from __future__ import annotations

import inspect
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

from app.core.database import get_mongo_db


logger = logging.getLogger(__name__)

SOURCE_PRIORITY = {"tushare": 0, "baostock": 1, "akshare": 2}
NORMALIZATION_VERSION = "cn-sector-v1"
PROFILE_MAX_AGE = timedelta(days=30)
REVENUE_MAX_AGE = timedelta(days=550)
REFRESH_RETRY_BACKOFF = timedelta(hours=24)
ALLOWED_ENDPOINTS = {
    "tushare": {"stock_basic", "stock_company", "fina_mainbz"},
    "baostock": {"query_stock_basic"},
    "akshare": {"stock_individual_info_em"},
}
SAFE_PROVIDER_ERROR_MESSAGES = {
    "provider_timeout": "Provider request timed out.",
    "provider_permission_denied": "Provider access denied.",
    "provider_error": "Provider request failed.",
}

# Provider taxonomies are deliberately broad. Unknown values are retained as
# raw display data only rather than being silently assigned to a taxonomy.
SECTOR_GROUPS = {
    "信息技术": {
        "电子",
        "计算机",
        "计算机设备",
        "通信",
        "通信设备",
        "软件服务",
        "软件开发",
        "半导体",
        "互联网",
    },
    "金融": {"银行", "非银金融", "证券", "保险", "多元金融"},
    "医疗保健": {"医药生物", "医疗器械", "医疗服务"},
    "必需消费": {"食品饮料", "农林牧渔"},
    "可选消费": {"汽车", "家用电器", "纺织服饰", "商贸零售", "美容护理"},
    "工业": {"机械设备", "电力设备", "国防军工", "建筑装饰", "交通运输", "公用事业"},
    "原材料": {"基础化工", "有色金属", "钢铁", "建筑材料", "石油石化", "煤炭"},
    "房地产": {"房地产"},
    "传媒": {"传媒"},
    "社会服务": {"社会服务"},
    "综合": {"综合"},
}
SECTOR_ALIASES = {
    "信息技术": "信息技术",
    "金融": "金融",
    "医疗": "医疗保健",
    "医药": "医疗保健",
    "消费": "可选消费",
    "工业": "工业",
    "材料": "原材料",
}


def _normalise_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    for pattern in (
        r"(?:SH|SZ)\.(\d{1,6})",
        r"(\d{1,6})\.(?:SH|SZ)",
        r"(?:SH|SZ)(\d{1,6})",
        r"(\d{1,6})",
    ):
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1).zfill(6)
    return text


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _timestamp(value: Any) -> float:
    parsed = _parse_datetime(value)
    return parsed.timestamp() if parsed else 0.0


def _report_period(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value).strip() if value is not None and str(value).strip() else None
    return parsed.date().isoformat()


def _report_period_date(value: Any) -> datetime | None:
    return _parse_datetime(value)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_provider_sector(raw: Any) -> dict[str, str] | None:
    """Map a known provider taxonomy value to the versioned broad taxonomy."""

    if isinstance(raw, Mapping):
        raw = raw.get("raw_taxonomy_value") or raw.get("value")
    raw_value = _clean_text(raw)
    if not raw_value:
        return None
    normalized = SECTOR_ALIASES.get(raw_value)
    if normalized is None:
        normalized = next(
            (group for group, values in SECTOR_GROUPS.items() if raw_value in values),
            None,
        )
    if normalized is None:
        return None
    return {
        "value": normalized,
        "raw_taxonomy_value": raw_value,
        "normalization_version": NORMALIZATION_VERSION,
    }


def _source_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    metadata = {
        "source": str(document.get("source") or "").lower(),
        "source_endpoint": document.get("source_endpoint"),
        "source_record_key": document.get("source_record_key"),
        "retrieved_at": document.get("retrieved_at"),
    }
    if document.get("source_updated_at") is not None:
        metadata["source_updated_at"] = document["source_updated_at"]
    return {key: value for key, value in metadata.items() if value is not None}


def _safe_provider_error(error: Mapping[str, Any]) -> dict[str, Any]:
    error_code = str(error.get("error_code") or "provider_error")
    if error_code not in SAFE_PROVIDER_ERROR_MESSAGES:
        error_code = "provider_error"
    result = {
        "source": str(error.get("source") or "").lower(),
        "error_code": error_code,
        "message": SAFE_PROVIDER_ERROR_MESSAGES[error_code],
    }
    if error.get("source_endpoint") is not None:
        result["source_endpoint"] = error["source_endpoint"]
    return result


def _provider_error_sort_key(error: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        SOURCE_PRIORITY.get(str(error.get("source") or "").lower(), 99),
        str(error.get("source_endpoint") or ""),
        str(error.get("error_code") or ""),
        str(error.get("message") or ""),
    )


def _sort_provider_errors(errors: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_safe_provider_error(error) for error in errors or []]
    return sorted(normalized, key=_provider_error_sort_key)


def _is_duplicate_key_error(error: BaseException) -> bool:
    return getattr(error, "code", None) == 11000 or error.__class__.__name__ in {
        "DuplicateKeyError",
        "DuplicateKeyException",
    }


def _provenance_reason(
    document: Mapping[str, Any], expected_code: str | None = None
) -> str | None:
    source = str(document.get("source") or "").lower()
    endpoint = document.get("source_endpoint")
    if source not in SOURCE_PRIORITY:
        return "unsupported_source"
    if endpoint not in ALLOWED_ENDPOINTS[source]:
        return "unsupported_endpoint"
    if expected_code is not None:
        document_code = _normalise_code(document.get("code"))
        if not document_code:
            return "missing_document_code"
        if document_code != expected_code:
            return "document_code_mismatch"
    if not _clean_text(document.get("source_record_key")):
        return "missing_source_record_key"
    if _parse_datetime(document.get("retrieved_at")) is None:
        return "missing_or_invalid_retrieved_at"
    return None


def _field_candidate(
    document: Mapping[str, Any], field: str, value: Any
) -> dict[str, Any] | None:
    text = _clean_text(value)
    if not text:
        return None
    candidate = {"field": field, "value": text, **_source_metadata(document)}
    return candidate


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    source = str(candidate.get("source") or "")
    return (
        SOURCE_PRIORITY.get(source, 99),
        -_timestamp(candidate.get("source_updated_at") or candidate.get("retrieved_at")),
        str(candidate.get("source_endpoint") or ""),
        str(candidate.get("source_record_key") or ""),
        str(candidate.get("value") or ""),
    )


def _revenue_candidate(document: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = document.get("revenue_composition")
    if isinstance(raw, Mapping):
        items = raw.get("items") or []
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return None
    if not isinstance(items, (list, tuple)):
        return None
    period = _document_report_period(document)
    normalized_period = _report_period(period)
    period_date = _report_period_date(period)
    if not normalized_period or period_date is None:
        return None
    normalized_items = []
    for item in items:
        if isinstance(item, Mapping):
            normalized_items.append(dict(item))
        elif item is not None:
            normalized_items.append({"item": str(item)})
    normalized_items.sort(
        key=lambda item: (
            str(item.get("composition_type") or item.get("type") or item.get("category") or ""),
            str(item.get("item") or item.get("name") or item.get("product") or ""),
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        )
    )
    return {
        "field": "revenue_composition",
        "items": normalized_items,
        "report_period": normalized_period,
        **_source_metadata(document),
    }


def _document_report_period(document: Mapping[str, Any]) -> Any:
    raw = document.get("revenue_composition")
    periods = [document.get("report_period")]
    items = []
    if isinstance(raw, Mapping):
        periods.append(raw.get("report_period"))
        items = raw.get("items") or []
    elif isinstance(raw, (list, tuple)):
        items = raw
    periods.extend(
        item.get("report_period")
        for item in items
        if isinstance(item, Mapping)
    )
    valid_periods = [period for period in periods if _report_period_date(period) is not None]
    if valid_periods:
        return max(valid_periods, key=lambda period: (_timestamp(period), str(period)))
    return next((period for period in periods if _clean_text(period)), None)


def _display_only_metadata(
    document: Mapping[str, Any], reason: str, **extra: Any
) -> dict[str, Any]:
    return {"reason": reason, **_source_metadata(document), **extra}


def _display_only_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        SOURCE_PRIORITY.get(str(item.get("source") or ""), 99),
        str(item.get("source_endpoint") or ""),
        str(item.get("source_record_key") or ""),
        str(item.get("reason") or ""),
        str(item.get("raw_taxonomy_value") or ""),
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
    )


def _merge_display_only(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for group in groups:
        for item in group or []:
            value = dict(item)
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            unique[key] = value
    return sorted(unique.values(), key=_display_only_sort_key)


def _valid(document: Mapping[str, Any], now: datetime, max_age: timedelta) -> bool:
    if _provenance_reason(document) is not None:
        return False
    retrieved_at = _parse_datetime(document.get("retrieved_at"))
    source_updated_at = _parse_datetime(document.get("source_updated_at"))
    return (
        retrieved_at is not None
        and (source_updated_at is None or source_updated_at <= now)
        and timedelta(0) <= now - retrieved_at <= max_age
    )


def select_evidence_profile(
    code: str, source_documents: Iterable[Mapping[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Select independent, recent evidence for a single company code."""

    as_of = _parse_datetime(now) or datetime.now(timezone.utc)
    normalized_code = _normalise_code(code)
    candidates = []
    display_only = []
    for raw_document in source_documents or []:
        document = dict(raw_document)
        reason = _provenance_reason(document, expected_code=normalized_code)
        if reason:
            display_only.append(_display_only_metadata(document, reason))
            continue
        retrieved_at = _parse_datetime(document.get("retrieved_at"))
        if retrieved_at and retrieved_at > as_of:
            display_only.append(_display_only_metadata(document, "future_retrieved_at"))
            continue
        source_updated_at = _parse_datetime(document.get("source_updated_at"))
        if source_updated_at and source_updated_at > as_of:
            display_only.append(_display_only_metadata(document, "future_source_updated_at"))
            continue
        candidates.append(document)

    field_candidates: dict[str, list[dict[str, Any]]] = {
        "industry": [],
        "provider_sector": [],
        "main_business": [],
        "business_scope": [],
    }
    revenue_candidates = []
    for document in candidates:
        if _valid(document, as_of, PROFILE_MAX_AGE):
            industry = _field_candidate(document, "industry", document.get("industry"))
            if industry:
                field_candidates["industry"].append(industry)
            business = _field_candidate(
                document, "main_business", document.get("main_business")
            )
            if business:
                field_candidates["main_business"].append(business)
            business_scope = _field_candidate(
                document, "business_scope", document.get("business_scope")
            )
            if business_scope:
                field_candidates["business_scope"].append(business_scope)
            raw_sector = (
                document.get("provider_sector")
                or document.get("sector")
                or document.get("sector_name")
                or document.get("industry")
            )
            sector = normalize_provider_sector(raw_sector)
            if sector:
                field_candidates["provider_sector"].append(
                    {"field": "provider_sector", **sector, **_source_metadata(document)}
                )
            elif _clean_text(raw_sector):
                raw_sector_value = raw_sector
                if isinstance(raw_sector, Mapping):
                    raw_sector_value = raw_sector.get("raw_taxonomy_value") or raw_sector.get("value")
                display_only.append(
                    _display_only_metadata(
                        document,
                        "unknown_provider_sector",
                        raw_taxonomy_value=_clean_text(raw_sector_value),
                    )
                )
        if _valid(document, as_of, REVENUE_MAX_AGE):
            report_period = _document_report_period(document)
            report_period_date = _report_period_date(report_period)
            if report_period_date and report_period_date > as_of:
                display_only.append(
                    _display_only_metadata(
                        document,
                        "future_report_period",
                        report_period=_report_period(report_period),
                    )
                )
                continue
            revenue = _revenue_candidate(document)
            if revenue and as_of - _report_period_date(revenue["report_period"]) <= REVENUE_MAX_AGE:
                revenue_candidates.append(revenue)

    display_only = _merge_display_only(display_only)

    selected: dict[str, dict[str, Any] | None] = {}
    for field, field_values in field_candidates.items():
        field_values.sort(key=_candidate_sort_key)
        selected[field] = field_values[0] if field_values else None

    winning_revenue_source = min(
        (str(item.get("source") or "") for item in revenue_candidates),
        key=lambda source: SOURCE_PRIORITY.get(source, 99),
        default=None,
    )
    winning_revenue_candidates = [
        item for item in revenue_candidates if item.get("source") == winning_revenue_source
    ]
    winning_revenue_candidates.sort(
        key=lambda item: (
            -_timestamp(item.get("report_period")),
            str(item.get("source_endpoint") or ""),
            str(item.get("source_record_key") or ""),
            json.dumps(
                item.get("items"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    )
    selected_revenue = winning_revenue_candidates[0] if winning_revenue_candidates else None

    conflicts = []
    for field, field_values in field_candidates.items():
        winner = selected[field]
        if winner is None:
            continue
        winner_value = winner.get("value")
        for candidate in field_values:
            if candidate is winner or candidate.get("value") == winner_value:
                continue
            conflicts.append(dict(candidate))
    if selected_revenue:
        selected_signature = (
            selected_revenue.get("report_period"),
            json.dumps(selected_revenue.get("items"), ensure_ascii=False, sort_keys=True, default=str),
        )
        for candidate in revenue_candidates:
            candidate_signature = (
                candidate.get("report_period"),
                json.dumps(candidate.get("items"), ensure_ascii=False, sort_keys=True, default=str),
            )
            if candidate_signature != selected_signature:
                conflicts.append(dict(candidate))
    conflicts.sort(
        key=lambda item: (
            str(item.get("field") or ""),
            SOURCE_PRIORITY.get(str(item.get("source") or ""), 99),
            str(item.get("source_endpoint") or ""),
            str(item.get("source_record_key") or ""),
            str(item.get("value") or item.get("report_period") or ""),
            json.dumps(item.get("items"), ensure_ascii=False, sort_keys=True, default=str),
        )
    )

    industry = selected["industry"]
    business = selected["main_business"]
    business_scope = selected["business_scope"]
    sector = selected["provider_sector"]
    selected_evidence = [
        item for item in (sector, industry, business, business_scope, selected_revenue) if item
    ]
    complete = all(selected[field] is not None for field in ("provider_sector", "industry", "main_business"))
    status = "verified" if complete else "incomplete" if selected_evidence else "missing"
    confidence = "high" if complete else "medium" if selected_evidence else "low"
    missing_fields = [field for field in ("provider_sector", "industry", "main_business") if selected[field] is None]
    profile = {
        "code": normalized_code,
        "industry": industry.get("value") if industry else None,
        "main_business": business.get("value") if business else None,
        "business_scope": business_scope.get("value") if business_scope else None,
        "provider_sector": sector.get("value") if sector else None,
        "source": min(
            (str(item.get("source")) for item in selected_evidence),
            key=lambda source: SOURCE_PRIORITY.get(source, 99),
            default=None,
        ),
        "status": status,
        "confidence": confidence,
        "evidence": selected_evidence,
        "provider_sector_evidence": sector,
        "industry_evidence": industry,
        "main_business_evidence": business,
        "business_scope_evidence": business_scope,
        "revenue_composition": selected_revenue,
        "data_quality": {
            "complete": complete,
            "missing_fields": missing_fields,
            "display_only": display_only,
            "profile_conflicts": conflicts,
            "provider_errors": [],
        },
    }
    return profile


ProviderFetcher = Callable[[str], Awaitable[Any] | Any]


class CompanyProfileEnrichmentService:
    def __init__(
        self,
        db: Any = None,
        provider_fetchers: Mapping[str, ProviderFetcher] | None = None,
    ) -> None:
        self.db = db
        if provider_fetchers is None:
            # Keep adapter imports lazy: the adapter module imports provider
            # implementations, while those implementations may import app code.
            from app.services.company_profile_provider_adapters import (
                build_default_profile_fetchers,
            )

            provider_fetchers = build_default_profile_fetchers()
        self.provider_fetchers = {
            str(source).lower(): fetcher
            for source, fetcher in provider_fetchers.items()
        }

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    async def ensure_indexes(self) -> Any:
        """Ensure the cache has one authoritative document per normalized code."""

        db = await self._get_db()
        collection = db["stock_company_profiles"]
        result = collection.create_index(
            [("code", 1)], unique=True, name="stock_company_profiles_code_unique"
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    def _documents_from_fetch(
        source: str, code: str, result: Any, now: datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if isinstance(result, Mapping):
            result = result.get("source_documents") or result.get("documents") or [result]
        if not isinstance(result, (list, tuple)):
            return [], []
        documents = []
        display_only = []
        for raw in result:
            if not isinstance(raw, Mapping):
                display_only.append({"reason": "invalid_source_document", "source": source})
                continue
            document = dict(raw)
            reason = _provenance_reason(document, expected_code=code)
            if reason:
                display_only.append(_display_only_metadata(document, reason))
                continue
            retrieved_at = _parse_datetime(document.get("retrieved_at"))
            if retrieved_at and retrieved_at > now:
                display_only.append(_display_only_metadata(document, "future_retrieved_at"))
                continue
            source_updated_at = _parse_datetime(document.get("source_updated_at"))
            if source_updated_at and source_updated_at > now:
                display_only.append(_display_only_metadata(document, "future_source_updated_at"))
                continue
            report_period = _document_report_period(document)
            report_period_date = _report_period_date(report_period)
            if report_period_date and report_period_date > now:
                display_only.append(
                    _display_only_metadata(
                        document,
                        "future_report_period",
                        report_period=_report_period(report_period),
                    )
                )
                continue
            documents.append(document)
        return documents, _merge_display_only(display_only)

    @staticmethod
    def _merge_documents(existing: Iterable[Mapping[str, Any]], fresh: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for document in [*existing, *fresh]:
            item = dict(document)
            key = (
                str(item.get("source") or "").lower(),
                item.get("source_endpoint"),
                item.get("source_record_key") or json.dumps(item, sort_keys=True, default=str),
            )
            merged[key] = item
        return sorted(
            merged.values(),
            key=lambda item: (
                SOURCE_PRIORITY.get(str(item.get("source") or "").lower(), 99),
                str(item.get("source_endpoint") or ""),
                str(item.get("source_record_key") or ""),
            ),
        )

    async def _read_cache(self, collection: Any, codes: list[str]) -> dict[str, dict[str, Any]]:
        cursor = collection.find({"code": {"$in": codes}})
        if inspect.isawaitable(cursor):
            cursor = await cursor
        if hasattr(cursor, "to_list"):
            rows = await cursor.to_list(length=max(100, len(codes) * 10))
        else:
            rows = list(cursor or [])
        grouped = {
            code: {
                "source_documents": [],
                "provider_errors": [],
                "display_only": [],
                "last_refresh_at": None,
            }
            for code in codes
        }
        for row in rows or []:
            code = _normalise_code(row.get("code") or row.get("symbol"))
            if code not in grouped:
                continue
            documents = row.get("source_documents")
            if documents is None and row.get("source"):
                documents = [row]
            grouped[code]["source_documents"].extend(documents or [])
            quality = row.get("data_quality") or {}
            grouped[code]["provider_errors"].extend(
                quality.get("provider_errors") or row.get("provider_errors") or []
            )
            grouped[code]["display_only"].extend(quality.get("display_only") or [])
            refresh_at = row.get("refresh_started_at") or row.get("last_refresh_at")
            parsed_refresh_at = _parse_datetime(refresh_at)
            current_refresh_at = _parse_datetime(grouped[code].get("last_refresh_at"))
            if parsed_refresh_at and (
                current_refresh_at is None or parsed_refresh_at > current_refresh_at
            ):
                grouped[code]["last_refresh_at"] = parsed_refresh_at
        for code in codes:
            grouped[code]["provider_errors"] = _sort_provider_errors(
                grouped[code]["provider_errors"]
            )
            grouped[code]["display_only"] = _merge_display_only(
                grouped[code]["display_only"]
            )
        return grouped

    async def _refresh_code(
        self, code: str, cached: Mapping[str, Any], now: datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], datetime]:
        documents, display_only = self._documents_from_fetch(
            "cache", code, cached.get("source_documents") or [], now
        )
        display_only = _merge_display_only(cached.get("display_only") or [], display_only)
        errors = _sort_provider_errors(cached.get("provider_errors") or [])
        validation_now = now
        for source in sorted(
            (value for value in self.provider_fetchers if value in SOURCE_PRIORITY),
            key=lambda value: SOURCE_PRIORITY[value],
        ):
            fetcher = self.provider_fetchers[source]
            if not callable(fetcher):
                continue
            errors = [
                error for error in errors if str(error.get("source") or "").lower() != source
            ]
            try:
                result = fetcher(code)
                if inspect.isawaitable(result):
                    result = await result
                validation_now = datetime.now(timezone.utc)
                fresh_documents, fresh_display_only = self._documents_from_fetch(
                    source, code, result, validation_now
                )
                documents = self._merge_documents(documents, fresh_documents)
                display_only = _merge_display_only(
                    display_only,
                    fresh_display_only,
                    getattr(result, "display_only", []),
                )
                errors.extend(getattr(result, "provider_errors", []))
            except Exception as exc:  # provider failures are part of the result contract
                if isinstance(exc, TimeoutError):
                    error_code = "provider_timeout"
                elif isinstance(exc, PermissionError):
                    error_code = "provider_permission_denied"
                else:
                    error_code = "provider_error"
                logger.exception("provider fetch failed source=%s code=%s", source, code)
                validation_now = datetime.now(timezone.utc)
                errors.append(
                    {
                        "source": source,
                        "error_code": error_code,
                        "message": SAFE_PROVIDER_ERROR_MESSAGES[error_code],
                    }
                )
        errors = _sort_provider_errors(errors)
        return documents, errors, display_only, validation_now

    async def resolve_many(self, codes: Iterable[str], refresh: bool = False) -> dict[str, dict[str, Any]]:
        normalized_codes = list(dict.fromkeys(_normalise_code(code) for code in codes if code))
        normalized_codes = [code for code in normalized_codes if code]
        if not normalized_codes:
            return {}
        db = await self._get_db()
        collection = db["stock_company_profiles"]
        await self.ensure_indexes()
        cached = await self._read_cache(collection, normalized_codes)
        now = datetime.now(timezone.utc)
        results = {}
        for code in normalized_codes:
            documents = cached[code]["source_documents"]
            provider_errors = cached[code]["provider_errors"]
            display_only = cached[code]["display_only"]
            cached_profile = select_evidence_profile(code, documents, now=now)
            needs_refresh = not bool(
                cached_profile.get("data_quality", {}).get("complete")
            ) or cached_profile.get("revenue_composition") is None
            validation_now = now
            last_refresh_at = _parse_datetime(cached[code].get("last_refresh_at"))
            retry_blocked = bool(
                last_refresh_at is not None
                and datetime.now(timezone.utc) - last_refresh_at < REFRESH_RETRY_BACKOFF
            )
            if refresh and needs_refresh and not retry_blocked:
                refresh_started_at = datetime.now(timezone.utc)
                documents, provider_errors, display_only, validation_now = await self._refresh_code(
                    code, cached[code], now
                )
                write = {
                    "$set": {
                        "code": code,
                        "source_documents": documents,
                        "data_quality": {
                            "provider_errors": provider_errors,
                            "display_only": display_only,
                        },
                        "updated_at": now,
                        "refresh_started_at": refresh_started_at,
                        "last_refresh_at": refresh_started_at,
                    }
                }
                lost_write = False
                try:
                    write_result = await collection.update_one(
                        {
                            "code": code,
                            "$or": [
                                {"refresh_started_at": {"$exists": False}},
                                {"refresh_started_at": {"$lte": refresh_started_at}},
                            ],
                        },
                        write,
                        upsert=True,
                    )
                except Exception as exc:
                    if not _is_duplicate_key_error(exc):
                        raise
                    logger.warning("cache refresh lost conditional insert code=%s", code)
                    lost_write = True
                    write_result = None
                matched_count = getattr(write_result, "matched_count", None)
                upserted_id = getattr(write_result, "upserted_id", None)
                if lost_write or (matched_count == 0 and upserted_id is None):
                    winning_cache = await self._read_cache(collection, [code])
                    documents = winning_cache[code]["source_documents"]
                    provider_errors = winning_cache[code]["provider_errors"]
                    display_only = winning_cache[code]["display_only"]
                    validation_now = datetime.now(timezone.utc)
            profile = select_evidence_profile(
                code, documents, now=validation_now if refresh else now
            )
            profile["data_quality"]["provider_errors"] = _sort_provider_errors(
                provider_errors
            )
            profile["data_quality"]["display_only"] = _merge_display_only(
                profile["data_quality"].get("display_only") or [], display_only
            )
            results[code] = profile
        return results
