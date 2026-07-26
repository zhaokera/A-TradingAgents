"""Resolve company profiles through the evidence-backed enrichment service."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.core.database import get_mongo_db
from app.services.company_profile_enrichment_service import (
    CompanyProfileEnrichmentService,
    select_evidence_profile,
)


BUSINESS_FIELDS = (
    "main_business",
    "business_scope",
    "business",
    "introduction",
    "company_profile",
)


def _normalise_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith(("SH.", "SZ.")):
        text = text[3:]
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text else ""


def _row_to_source_document(code: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only an already-proven row contract into the selector."""

    row = dict(raw)
    document = {
        "code": _normalise_code(row.get("code") or row.get("symbol") or code),
        "source": row.get("source") or row.get("data_source"),
        "source_endpoint": row.get("source_endpoint"),
        "source_record_key": row.get("source_record_key"),
        "retrieved_at": row.get("retrieved_at"),
        "source_updated_at": row.get("source_updated_at"),
        "industry": row.get("industry") or row.get("sector"),
        "provider_sector": row.get("provider_sector"),
        "business_scope": row.get("business_scope"),
    }
    value = row.get("main_business") or row.get("business") or row.get("company_profile")
    if value:
        document["main_business"] = value
    return {key: value for key, value in document.items() if value is not None}


def select_master_profile(code: str, rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Backward-compatible selector that fails closed on unproven local rows."""

    normalized = _normalise_code(code)
    rows = [dict(row) for row in rows or []]
    documents = [
        _row_to_source_document(normalized, row)
        for row in rows
        if _normalise_code(row.get("code") or row.get("symbol")) == normalized
    ]
    profile = select_evidence_profile(normalized, documents)
    names = [
        str(row.get("name") or "").strip()
        for row in rows
        if _normalise_code(row.get("code") or row.get("symbol")) == normalized
        and str(row.get("name") or "").strip()
    ]
    profile["name"] = names[0] if names else normalized
    return profile


class StockMasterDataService:
    def __init__(
        self,
        db: Any = None,
        profile_service: CompanyProfileEnrichmentService | None = None,
        provider_fetchers: Mapping[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.profile_service = profile_service
        self.provider_fetchers = provider_fetchers

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    async def _get_profile_service(self, db: Any) -> CompanyProfileEnrichmentService:
        if self.profile_service is None:
            self.profile_service = CompanyProfileEnrichmentService(
                db=db,
                provider_fetchers=self.provider_fetchers,
            )
        elif getattr(self.profile_service, "db", None) is None:
            self.profile_service.db = db
        return self.profile_service

    async def resolve_many(
        self, codes: Iterable[str], refresh: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        normalized_codes = list(
            dict.fromkeys(_normalise_code(code) for code in codes if code)
        )
        normalized_codes = [code for code in normalized_codes if code]
        if not normalized_codes:
            return {}

        db = await self._get_db()
        profile_service = await self._get_profile_service(db)
        profiles = await profile_service.resolve_many(normalized_codes, refresh=refresh)

        projection: Dict[str, int] = {
            "code": 1,
            "symbol": 1,
            "name": 1,
            "_id": 0,
        }
        cursor = db["stock_basic_info"].find(
            {"code": {"$in": normalized_codes}}, projection
        )
        rows = await cursor.to_list(length=max(100, len(normalized_codes) * 5))
        names: Dict[str, str] = {}
        for row in rows or []:
            row_code = _normalise_code(row.get("code") or row.get("symbol"))
            name = str(row.get("name") or "").strip()
            if row_code in normalized_codes and name and row_code not in names:
                names[row_code] = name

        result = {}
        for code in normalized_codes:
            profile = dict(profiles.get(code) or select_evidence_profile(code, []))
            profile["name"] = names.get(code, profile.get("name") or code)
            result[code] = profile
        return result


stock_master_data_service = StockMasterDataService()
