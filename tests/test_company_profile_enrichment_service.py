from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.company_profile_enrichment_service import (
    CompanyProfileEnrichmentService,
    normalize_provider_sector,
    select_evidence_profile,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def evidence_doc(source, endpoint, **fields):
    return {
        "code": "000001",
        "source": source,
        "source_endpoint": endpoint,
        "source_record_key": f"000001:{source}",
        "retrieved_at": NOW - timedelta(days=1),
        **fields,
    }


def test_normalize_provider_sector_keeps_raw_taxonomy_and_version():
    assert normalize_provider_sector("计算机") == {
        "value": "信息技术",
        "raw_taxonomy_value": "计算机",
        "normalization_version": "cn-sector-v1",
    }
    assert normalize_provider_sector("  ") is None


def test_selects_each_field_by_priority_and_retains_lower_source_conflicts():
    profile = select_evidence_profile(
        "1",
        [
            evidence_doc(
                "akshare",
                "stock_individual_info_em",
                industry="AK industry",
                main_business="AK business",
                provider_sector="银行",
            ),
            evidence_doc(
                "tushare",
                "stock_basic",
                industry="TS industry",
                provider_sector="计算机",
            ),
            evidence_doc(
                "baostock",
                "query_stock_basic",
                industry="BS industry",
                main_business="BS business",
                provider_sector="银行",
            ),
        ],
        now=NOW,
    )

    assert profile["code"] == "000001"
    assert profile["industry"] == "TS industry"
    assert profile["main_business"] == "BS business"
    assert profile["provider_sector"] == "信息技术"
    assert profile["provider_sector_evidence"]["value"] == "信息技术"
    assert profile["provider_sector_evidence"]["raw_taxonomy_value"] == "计算机"
    assert profile["main_business_evidence"]["source"] == "baostock"
    conflicts = profile["data_quality"]["profile_conflicts"]
    assert [(item["field"], item["source"]) for item in conflicts] == [
        ("industry", "baostock"),
        ("industry", "akshare"),
        ("main_business", "akshare"),
        ("provider_sector", "baostock"),
        ("provider_sector", "akshare"),
    ]


def test_expiry_applies_to_profile_fields_and_revenue_uses_newest_report():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "stock_basic",
                retrieved_at=NOW - timedelta(days=31),
                industry="expired industry",
            ),
            evidence_doc(
                "baostock",
                "query_stock_basic",
                industry="fresh industry",
            ),
            evidence_doc(
                "tushare",
                "fina_mainbz",
                report_period="2025-12-31",
                revenue_composition=[{"item": "old", "revenue": 1}],
            ),
            evidence_doc(
                "tushare",
                "fina_mainbz",
                report_period="2026-03-31",
                revenue_composition=[
                    {"item": "z item", "revenue": 2},
                    {"item": "a item", "revenue": 3},
                ],
            ),
        ],
        now=NOW,
    )

    assert profile["industry"] == "fresh industry"
    assert profile["revenue_composition"]["report_period"] == "2026-03-31"
    assert [item["item"] for item in profile["revenue_composition"]["items"]] == [
        "a item",
        "z item",
    ]


def test_profile_and_revenue_expiry_boundaries_are_inclusive():
    boundary_now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "stock_basic",
                retrieved_at=boundary_now - timedelta(days=30),
                industry="still fresh",
            ),
            evidence_doc(
                "tushare",
                "fina_mainbz",
                retrieved_at=boundary_now - timedelta(days=550),
                report_period=(boundary_now - timedelta(days=550)).date().isoformat(),
                revenue_composition=[{"item": "boundary", "revenue": 1}],
            ),
        ],
        now=boundary_now,
    )

    assert profile["industry"] == "still fresh"
    assert profile["revenue_composition"]["items"] == [
        {"item": "boundary", "revenue": 1}
    ]


def test_revenue_source_priority_precedes_report_period_and_conflicts_are_deterministic():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "fina_mainbz",
                source_record_key="ts:old",
                report_period="2025-12-31",
                revenue_composition=[{"item": "tushare old", "revenue": 1}],
            ),
            evidence_doc(
                "tushare",
                "fina_mainbz",
                source_record_key="ts:new",
                report_period="2026-03-31",
                revenue_composition=[{"item": "tushare new", "revenue": 2}],
            ),
            evidence_doc(
                "baostock",
                "query_stock_basic",
                source_record_key="bs:newest",
                report_period="2026-06-30",
                revenue_composition=[{"item": "baostock newest", "revenue": 3}],
            ),
        ],
        now=NOW,
    )

    assert profile["revenue_composition"]["source"] == "tushare"
    assert profile["revenue_composition"]["report_period"] == "2026-03-31"
    conflicts = profile["data_quality"]["profile_conflicts"]
    revenue_conflicts = [item for item in conflicts if item["field"] == "revenue_composition"]
    assert [(item["source"], item["source_record_key"]) for item in revenue_conflicts] == [
        ("tushare", "ts:old"),
        ("baostock", "bs:newest"),
    ]


def test_missing_or_mismatched_document_code_and_record_key_are_display_only():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc("tushare", "stock_basic", source_record_key="", industry="no key"),
            evidence_doc("tushare", "stock_basic", code="000002", industry="wrong code"),
            {
                **evidence_doc("tushare", "stock_basic", industry="no code"),
                "code": "",
            },
            evidence_doc(
                "tushare",
                "stock_basic",
                industry="valid industry",
                provider_sector="计算机",
            ),
        ],
        now=NOW,
    )

    assert profile["industry"] == "valid industry"
    display_only = profile["data_quality"]["display_only"]
    assert [item["reason"] for item in display_only] == [
        "missing_source_record_key",
        "document_code_mismatch",
        "missing_document_code",
    ]


def test_unknown_provider_taxonomy_is_retained_as_display_only_raw_value():
    profile = select_evidence_profile(
        "000001",
        [evidence_doc("tushare", "stock_basic", provider_sector="未来产业", industry="行业")],
        now=NOW,
    )

    assert profile["provider_sector"] is None
    assert profile["data_quality"]["display_only"] == [
        {
            "reason": "unknown_provider_sector",
            "raw_taxonomy_value": "未来产业",
            "source": "tushare",
            "source_endpoint": "stock_basic",
            "source_record_key": "000001:tushare",
            "retrieved_at": NOW - timedelta(days=1),
        }
    ]


def test_future_retrieval_and_report_period_are_not_valid_evidence():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "stock_basic",
                retrieved_at=NOW + timedelta(minutes=1),
                industry="future industry",
            ),
            evidence_doc(
                "tushare",
                "fina_mainbz",
                report_period="2026-12-31",
                revenue_composition=[{"item": "future", "revenue": 1}],
            ),
        ],
        now=NOW,
    )

    assert profile["industry"] is None
    assert profile["revenue_composition"] is None
    assert {item["reason"] for item in profile["data_quality"]["display_only"]} >= {
        "future_retrieved_at",
        "future_report_period",
    }


def test_invalid_or_unproven_documents_are_display_only():
    profile = select_evidence_profile(
        "000001",
        [
            {
                "code": "000001",
                "industry": "local guess",
                "main_business": "local prose",
            },
            evidence_doc(
                "tushare",
                "unsupported_endpoint",
                industry="unsupported industry",
            ),
        ],
        now=NOW,
    )

    assert profile["industry"] is None
    assert profile["main_business"] is None
    assert profile["status"] == "missing"
    assert len(profile["data_quality"]["display_only"]) == 2


@pytest.mark.asyncio
async def test_refresh_false_reads_cache_without_calling_providers():
    cached = evidence_doc("tushare", "stock_basic", industry="cached industry")
    collection = FakeCollection([{"code": "000001", "source_documents": [cached]}])
    db = FakeDatabase(collection)
    fetcher = AsyncMock(side_effect=AssertionError("refresh=False must not fetch"))

    service = CompanyProfileEnrichmentService(
        db=db, provider_fetchers={"tushare": fetcher}
    )
    result = await service.resolve_many(["1"], refresh=False)

    assert result["000001"]["industry"] == "cached industry"
    fetcher.assert_not_awaited()
    assert collection.updated == []


@pytest.mark.asyncio
async def test_refresh_persists_source_documents_and_structured_errors():
    fresh = evidence_doc("tushare", "stock_basic", industry="fresh industry")
    collection = FakeCollection(
        [
            {
                "code": "000001",
                "source_documents": [],
                "data_quality": {
                    "provider_errors": [
                        {"source": "baostock", "error_code": "provider_error", "message": "old"},
                        {"source": "akshare", "error_code": "provider_error", "message": "keep"},
                    ]
                },
            }
        ]
    )
    fetchers = {
        "tushare": AsyncMock(return_value=[fresh]),
        "baostock": AsyncMock(side_effect=TimeoutError("provider timeout")),
    }
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection), provider_fetchers=fetchers
    )

    result = await service.resolve_many(["000001"], refresh=True)

    assert result["000001"]["industry"] == "fresh industry"
    assert result["000001"]["data_quality"]["provider_errors"] == [
        {
            "source": "baostock",
            "error_code": "provider_error",
            "message": "provider timeout",
        },
        {"source": "akshare", "error_code": "provider_error", "message": "keep"},
    ]
    assert len(collection.updated) == 1
    saved = collection.updated[0]["$set"]
    assert saved["code"] == "000001"
    assert saved["source_documents"] == [fresh]
    fetchers["tushare"].assert_awaited_once_with("000001")
    fetchers["baostock"].assert_awaited_once_with("000001")


@pytest.mark.asyncio
async def test_refresh_rejects_unsupported_documents_before_persistence():
    invalid_endpoint = evidence_doc(
        "tushare", "not_allowed", industry="must not persist"
    )
    invalid_source = evidence_doc(
        "mystery", "stock_basic", industry="must not persist either"
    )
    invalid_code = evidence_doc(
        "tushare", "stock_basic", code="000002", industry="wrong code"
    )
    collection = FakeCollection([])
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection),
        provider_fetchers={
            "tushare": AsyncMock(
                return_value=[invalid_endpoint, invalid_source, invalid_code]
            )
        },
    )

    result = await service.resolve_many(["000001"], refresh=True)

    saved = collection.updated[0]["$set"]
    assert saved["source_documents"] == []
    assert result["000001"]["industry"] is None
    assert {item["reason"] for item in saved["data_quality"]["display_only"]} == {
        "unsupported_endpoint",
        "unsupported_source",
        "document_code_mismatch",
    }
    assert "must not persist" not in repr(saved)


@pytest.mark.asyncio
async def test_provider_error_never_invents_fallback_business_prose():
    fetcher = AsyncMock(side_effect=TimeoutError("unavailable"))
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([])), provider_fetchers={"tushare": fetcher}
    )

    profile = (await service.resolve_many(["000001"], refresh=True))["000001"]

    assert profile["main_business"] is None
    assert profile["main_business_evidence"] is None
    assert profile["data_quality"]["provider_errors"][0]["error_code"] == "provider_error"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length):
        return self.rows[:length]


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.updated = []

    def find(self, query):
        return FakeCursor(self.rows)

    async def update_one(self, query, update, upsert=False):
        self.updated.append(update)


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "stock_company_profiles"
        return self.collection
