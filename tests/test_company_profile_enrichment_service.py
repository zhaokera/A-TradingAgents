import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pandas as pd

from app.services.company_profile_provider_adapters import (
    build_default_profile_fetchers,
    fetch_akshare_profile,
    fetch_baostock_profile,
    fetch_tushare_profile,
)
from app.services import company_profile_provider_adapters as provider_adapters
from app.services.company_profile_enrichment_service import (
    CompanyProfileEnrichmentService,
    normalize_provider_sector,
    select_evidence_profile,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class FakeTushareAPI:
    def __init__(self):
        self.calls = []

    def stock_basic(self, **kwargs):
        self.calls.append(("stock_basic", kwargs))
        return pd.DataFrame([{"ts_code": "000001.SZ", "industry": "银行"}])

    def stock_company(self, **kwargs):
        self.calls.append(("stock_company", kwargs))
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "introduction": "真实简介",
                    "main_business": "真实主营",
                    "business_scope": "真实经营范围",
                }
            ]
        )

    def fina_mainbz(self, **kwargs):
        self.calls.append(("fina_mainbz", kwargs))
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "2025-12-31",
                    "bz_item": "零售",
                    "bz_sales": 100,
                    "ann_date": "2026-03-01",
                }
            ]
        )


class MismatchTushareAPI(FakeTushareAPI):
    def stock_basic(self, **kwargs):
        self.calls.append(("stock_basic", kwargs))
        return pd.DataFrame([{"ts_code": "600000.SH", "industry": "错误行业"}])

    def stock_company(self, **kwargs):
        self.calls.append(("stock_company", kwargs))
        return pd.DataFrame([{"ts_code": "600000.SH", "main_business": "错误主营"}])

    def fina_mainbz(self, **kwargs):
        self.calls.append(("fina_mainbz", kwargs))
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "end_date": "2025-12-31", "bz_item": "有效"},
                {"ts_code": "600000.SH", "end_date": "2025-12-31", "bz_item": "错误"},
            ]
        )


class FakeConnectedProvider:
    def __init__(self, api=None, raw=None):
        self.api = api
        self.bs = raw
        self.ak = raw
        self.connected = False
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        self.connected = True
        return True


class FakeBaoStockResult:
    error_code = "0"
    fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]

    def __init__(self):
        self._rows = [["sz.000001", "平安银行", "1991-04-03", "", "1", "1"]]

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0)


class FakeBaoStockAPI:
    def __init__(self):
        self.calls = []

    def query_stock_basic(self, **kwargs):
        self.calls.append(kwargs)
        return FakeBaoStockResult()


class NonzeroBaoStockResult:
    error_code = "1001"
    error_msg = "secret provider response"
    fields = []


@pytest.mark.asyncio
async def test_tushare_adapter_maps_exact_endpoints_without_inventing_text():
    provider = FakeConnectedProvider(api=FakeTushareAPI())

    documents = await fetch_tushare_profile(
        "000001", provider_factory=lambda: provider
    )

    assert provider.connect_calls == 1
    assert [item[0] for item in provider.api.calls] == [
        "stock_basic",
        "stock_company",
        "fina_mainbz",
    ]
    assert all(call[1]["ts_code"] == "000001.SZ" for call in provider.api.calls)
    assert documents[0]["industry"] == "银行"
    assert documents[1]["introduction"] == "真实简介"
    assert documents[1]["main_business"] == "真实主营"
    assert documents[1]["business_scope"] == "真实经营范围"
    assert documents[2]["report_period"] == "2025-12-31"
    assert documents[2]["revenue_composition"]["items"][0]["item"] == "零售"
    assert {item["source_endpoint"] for item in documents} == {
        "stock_basic",
        "stock_company",
        "fina_mainbz",
    }
    assert all(item["code"] == "000001" for item in documents)
    assert all(item["retrieved_at"] for item in documents)


@pytest.mark.asyncio
async def test_tushare_adapter_drops_mismatched_return_codes_without_relabeling():
    result = await fetch_tushare_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(api=MismatchTushareAPI())
    )

    assert [item["source_endpoint"] for item in result] == ["fina_mainbz"]
    assert {item["source_endpoint"] for item in result.display_only} == {
        "stock_basic",
        "stock_company",
        "fina_mainbz",
    }
    assert result.display_only[0]["reason"] == "document_code_mismatch"


@pytest.mark.asyncio
async def test_tushare_revenue_keeps_only_rows_matching_requested_code():
    api = FakeTushareAPI()
    api.fina_mainbz = lambda **kwargs: pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "end_date": "2025-12-31", "bz_item": "有效"},
            {"ts_code": "600000.SH", "end_date": "2025-12-31", "bz_item": "错误"},
        ]
    )

    result = await fetch_tushare_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(api=api)
    )

    revenue = next(item for item in result if item["source_endpoint"] == "fina_mainbz")
    assert [item["item"] for item in revenue["revenue_composition"]["items"]] == ["有效"]
    assert result.display_only[-1]["reason"] == "document_code_mismatch"


@pytest.mark.asyncio
async def test_tushare_endpoint_failures_are_isolated_and_structured():
    class PartialAPI(FakeTushareAPI):
        def stock_company(self, **kwargs):
            raise PermissionError("secret permission detail")

        def fina_mainbz(self, **kwargs):
            raise TimeoutError("secret timeout detail")

    result = await fetch_tushare_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(api=PartialAPI())
    )

    assert [item["source_endpoint"] for item in result] == ["stock_basic"]
    assert result.provider_errors == [
        {
            "source": "tushare",
            "source_endpoint": "stock_company",
            "error_code": "provider_permission_denied",
        },
        {
            "source": "tushare",
            "source_endpoint": "fina_mainbz",
            "error_code": "provider_timeout",
        },
    ]
    assert "secret" not in repr(result.provider_errors)


@pytest.mark.asyncio
async def test_baostock_adapter_returns_one_strict_basic_document():
    raw = FakeBaoStockAPI()
    provider = FakeConnectedProvider(raw=raw)

    documents = await fetch_baostock_profile(
        "000001", provider_factory=lambda: provider
    )

    assert raw.calls == [{"code": "sz.000001"}]
    assert len(documents) == 1
    assert documents[0]["name"] == "平安银行"
    assert documents[0]["list_date"] == "1991-04-03"
    assert documents[0]["source_endpoint"] == "query_stock_basic"
    assert "industry" not in documents[0]


@pytest.mark.asyncio
async def test_baostock_adapter_drops_mismatched_provider_code():
    class MismatchResult(FakeBaoStockResult):
        def __init__(self):
            self._rows = [["sh.600000", "错误股票", "1991-04-03", "", "1", "1"]]

    class MismatchAPI(FakeBaoStockAPI):
        def query_stock_basic(self, **kwargs):
            self.calls.append(kwargs)
            return MismatchResult()

    result = await fetch_baostock_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(raw=MismatchAPI())
    )

    assert result == []
    assert result.display_only[0]["reason"] == "document_code_mismatch"


@pytest.mark.asyncio
async def test_baostock_query_errors_are_endpoint_specific_and_safe():
    class FailingAPI(FakeBaoStockAPI):
        def query_stock_basic(self, **kwargs):
            raise PermissionError("secret baostock permission")

    result = await fetch_baostock_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(raw=FailingAPI())
    )

    assert result == []
    assert result.provider_errors == [
        {
            "source": "baostock",
            "source_endpoint": "query_stock_basic",
            "error_code": "provider_permission_denied",
        }
    ]
    assert "secret" not in repr(result.provider_errors)


@pytest.mark.asyncio
async def test_baostock_nonzero_query_result_is_a_safe_endpoint_error():
    class NonzeroAPI(FakeBaoStockAPI):
        def query_stock_basic(self, **kwargs):
            return NonzeroBaoStockResult()

    result = await fetch_baostock_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(raw=NonzeroAPI())
    )

    assert result == []
    assert result.provider_errors == [
        {
            "source": "baostock",
            "source_endpoint": "query_stock_basic",
            "error_code": "provider_error",
        }
    ]
    assert "secret" not in repr(result.provider_errors)


@pytest.mark.asyncio
async def test_akshare_adapter_parses_item_value_frame_and_update_fields():
    raw = SimpleNamespace(
        stock_individual_info_em=lambda **kwargs: pd.DataFrame(
            [
                {"item": "所属行业", "value": "银行"},
                {"item": "主营业务", "value": "真实主营"},
                {"item": "经营范围", "value": "真实范围"},
                {"item": "上市时间", "value": "1991-04-03"},
                {"item": "更新时间", "value": "2026-07-21"},
            ]
        )
    )
    provider = FakeConnectedProvider(raw=raw)

    documents = await fetch_akshare_profile(
        "000001", provider_factory=lambda: provider
    )

    assert len(documents) == 1
    document = documents[0]
    assert document["industry"] == "银行"
    assert document["main_business"] == "真实主营"
    assert document["business_scope"] == "真实范围"
    assert document["listing_date"] == "1991-04-03"
    assert document["source_updated_at"] == "2026-07-21"
    assert document["source_endpoint"] == "stock_individual_info_em"


@pytest.mark.asyncio
async def test_akshare_adapter_accepts_industry_alias():
    raw = SimpleNamespace(
        stock_individual_info_em=lambda **kwargs: pd.DataFrame(
            [{"item": "行业", "value": "计算机"}]
        )
    )
    provider = FakeConnectedProvider(raw=raw)

    documents = await fetch_akshare_profile("000001", provider_factory=lambda: provider)

    assert documents[0]["industry"] == "计算机"


@pytest.mark.asyncio
async def test_akshare_adapter_uses_cninfo_company_profile_as_authoritative_fallback():
    raw = SimpleNamespace(
        stock_individual_info_em=lambda **kwargs: pd.DataFrame(
            [{"item": "所属行业", "value": "半导体"}]
        ),
        stock_profile_cninfo=lambda **kwargs: pd.DataFrame(
            [
                {
                    "A股代码": "603005",
                    "A股简称": "晶方科技",
                    "所属行业": "计算机、通信和其他电子设备制造业",
                    "主营业务": "集成电路的封装测试业务",
                    "经营范围": "研发、生产、封装和测试集成电路产品",
                }
            ]
        ),
    )
    provider = FakeConnectedProvider(raw=raw)

    documents = await fetch_akshare_profile(
        "603005", provider_factory=lambda: provider
    )

    cninfo = next(
        item for item in documents if item["source_endpoint"] == "stock_profile_cninfo"
    )
    assert cninfo["source"] == "cninfo"
    assert cninfo["main_business"] == "集成电路的封装测试业务"
    assert cninfo["business_scope"] == "研发、生产、封装和测试集成电路产品"
    profile = select_evidence_profile("603005", [cninfo], now=datetime.now(timezone.utc))
    assert profile["provider_sector"] == "信息技术"
    assert profile["data_quality"]["decision_critical_complete"] is True


def test_profile_marks_main_business_as_noncritical_when_sector_and_industry_are_proven():
    profile = select_evidence_profile(
        "603005",
        [
            evidence_doc(
                "akshare",
                "stock_individual_info_em",
                code="603005",
                industry="半导体",
                provider_sector="半导体",
            )
        ],
        now=NOW,
    )

    assert profile["data_quality"]["complete"] is False
    assert profile["data_quality"]["decision_critical_complete"] is True
    assert profile["data_quality"]["decision_critical_missing_fields"] == []
    assert profile["data_quality"]["noncritical_missing_fields"] == [
        "main_business"
    ]


@pytest.mark.asyncio
async def test_akshare_endpoint_errors_are_specific_and_safe():
    raw = SimpleNamespace(
        stock_individual_info_em=lambda **kwargs: (_ for _ in ()).throw(
            TimeoutError("secret akshare timeout")
        )
    )
    provider = FakeConnectedProvider(raw=raw)

    result = await fetch_akshare_profile("000001", provider_factory=lambda: provider)

    assert result == []
    assert result.provider_errors == [
        {
            "source": "akshare",
            "source_endpoint": "stock_individual_info_em",
            "error_code": "provider_timeout",
        }
    ]
    assert "secret" not in repr(result.provider_errors)


def test_default_profile_fetchers_expose_all_source_fetchers():
    assert set(build_default_profile_fetchers()) == {"tushare", "baostock", "akshare"}


@pytest.mark.asyncio
async def test_default_tushare_fetcher_reuses_provider_and_serializes_batches():
    factory_calls = []
    active = 0
    max_active = 0

    class SlowAPI(FakeTushareAPI):
        def _run(self, endpoint, **kwargs):
            nonlocal active, max_active
            self.calls.append((endpoint, kwargs))
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.01)
            active -= 1
            if endpoint == "fina_mainbz":
                return pd.DataFrame()
            return pd.DataFrame([{"ts_code": "000001.SZ", "industry": "银行"}])

        def stock_basic(self, **kwargs):
            return self._run("stock_basic", **kwargs)

        def stock_company(self, **kwargs):
            return self._run("stock_company", **kwargs)

        def fina_mainbz(self, **kwargs):
            return self._run("fina_mainbz", **kwargs)

    provider = FakeConnectedProvider(api=SlowAPI())

    def factory():
        factory_calls.append(provider)
        return provider

    fetcher = build_default_profile_fetchers(
        tushare_provider_factory=factory
    )["tushare"]
    await asyncio.gather(fetcher("000001"), fetcher("000002"))

    assert factory_calls == [provider]
    assert provider.connect_calls == 1
    assert max_active == 1


@pytest.mark.asyncio
async def test_tushare_prefers_sync_connection_off_event_loop():
    provider = FakeConnectedProvider(api=FakeTushareAPI())
    provider.connect = lambda: (_ for _ in ()).throw(AssertionError("async connect used"))
    provider.connect_sync_calls = 0

    def connect_sync():
        provider.connect_sync_calls += 1
        provider.connected = True
        return True

    provider.connect_sync = connect_sync

    await fetch_tushare_profile("000001", provider_factory=lambda: provider)

    assert provider.connect_sync_calls == 1


@pytest.mark.asyncio
async def test_provider_connection_failure_is_safe_and_endpoint_specific():
    class UnavailableProvider:
        api = None
        connected = False

        async def connect(self):
            return False

    result = await fetch_tushare_profile(
        "000001", provider_factory=UnavailableProvider
    )

    assert result == []
    assert result.provider_errors == [
        {
            "source": "tushare",
            "source_endpoint": "connect",
            "error_code": "provider_unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_tushare_endpoint_timeout_is_safe(monkeypatch):
    class TimeoutAPI(FakeTushareAPI):
        def stock_basic(self, **kwargs):
            time.sleep(0.03)
            return super().stock_basic(**kwargs)

    monkeypatch.setattr(provider_adapters, "PROVIDER_ENDPOINT_TIMEOUT_SECONDS", 0.001)
    result = await fetch_tushare_profile(
        "000001", provider_factory=lambda: FakeConnectedProvider(api=TimeoutAPI())
    )

    assert result.provider_errors[0] == {
        "source": "tushare",
        "source_endpoint": "stock_basic",
        "error_code": "provider_timeout",
    }


@pytest.mark.asyncio
async def test_shared_fetcher_blocks_overlap_after_timed_out_provider_thread(monkeypatch):
    entered = threading.Event()
    active = 0
    max_active = 0

    class BlockingAPI(FakeTushareAPI):
        def stock_basic(self, **kwargs):
            nonlocal active, max_active
            self.calls.append(("stock_basic", kwargs))
            entered.set()
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.06)
            active -= 1
            return pd.DataFrame([{"ts_code": "000001.SZ", "industry": "银行"}])

    monkeypatch.setattr(provider_adapters, "PROVIDER_ENDPOINT_TIMEOUT_SECONDS", 0.001)
    provider = FakeConnectedProvider(api=BlockingAPI())
    fetcher = build_default_profile_fetchers(
        tushare_provider_factory=lambda: provider
    )["tushare"]
    first_task = asyncio.create_task(fetcher("000001"))
    await asyncio.to_thread(entered.wait, 1)

    first_result = await first_task
    second_result = await fetcher("000002")

    assert first_result.provider_errors[0]["error_code"] == "provider_timeout"
    assert second_result.provider_errors == [
        {
            "source": "tushare",
            "source_endpoint": "provider_busy",
            "error_code": "provider_busy",
        }
    ]
    assert max_active == 1
    await asyncio.sleep(0.07)


@pytest.mark.asyncio
async def test_baostock_query_uses_one_login_lifecycle_when_raw_module_is_ready():
    class LoginResult:
        error_code = "0"

    class LoginCountingAPI(FakeBaoStockAPI):
        def __init__(self):
            super().__init__()
            self.login_calls = 0
            self.logout_calls = 0

        def login(self):
            self.login_calls += 1
            return LoginResult()

        def logout(self):
            self.logout_calls += 1

    raw = LoginCountingAPI()
    provider = FakeConnectedProvider(raw=raw)
    provider.connect = lambda: (_ for _ in ()).throw(AssertionError("probe login used"))

    result = await fetch_baostock_profile("000001", provider_factory=lambda: provider)

    assert len(result) == 1
    assert raw.login_calls == 1
    assert raw.logout_calls == 1


def evidence_doc(source, endpoint, **fields):
    return {
        "code": "000001",
        "source": source,
        "source_endpoint": endpoint,
        "source_record_key": f"000001:{source}",
        "retrieved_at": NOW - timedelta(days=1),
        **fields,
    }


def fresh_runtime_evidence_doc(source, endpoint, **fields):
    return evidence_doc(
        source,
        endpoint,
        retrieved_at=datetime.now(timezone.utc) - timedelta(days=1),
        **fields,
    )


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


def test_business_scope_is_independent_from_main_business_evidence():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "stock_company",
                introduction="简介不能替代主营",
                main_business="明确主营",
                business_scope="独立经营范围",
                industry="计算机",
                provider_sector="计算机",
            )
        ],
        now=NOW,
    )

    assert profile["main_business"] == "明确主营"
    assert profile["business_scope"] == "独立经营范围"
    assert profile["main_business_evidence"]["value"] == "明确主营"
    assert profile["business_scope_evidence"]["value"] == "独立经营范围"
    assert profile["data_quality"]["complete"] is True


def test_business_scope_does_not_complete_missing_main_business():
    profile = select_evidence_profile(
        "000001",
        [evidence_doc("tushare", "stock_company", business_scope="只有经营范围")],
        now=NOW,
    )

    assert profile["main_business"] is None
    assert profile["business_scope"] == "只有经营范围"
    assert profile["data_quality"]["complete"] is False


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


def test_same_key_revenue_documents_are_input_order_independent():
    first_document = evidence_doc(
        "tushare",
        "fina_mainbz",
        source_record_key="ts:same-key",
        report_period="2026-03-31",
        revenue_composition=[{"item": "alpha", "revenue": 1}],
    )
    second_document = evidence_doc(
        "tushare",
        "fina_mainbz",
        source_record_key="ts:same-key",
        report_period="2026-03-31",
        revenue_composition=[{"item": "beta", "revenue": 2}],
    )

    forward = select_evidence_profile(
        "000001", [first_document, second_document], now=NOW
    )
    reversed_order = select_evidence_profile(
        "000001", [second_document, first_document], now=NOW
    )

    assert forward["revenue_composition"] == reversed_order["revenue_composition"]
    assert forward["data_quality"]["profile_conflicts"] == reversed_order["data_quality"]["profile_conflicts"]


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


def test_future_source_updated_at_is_display_only():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "stock_basic",
                source_updated_at=NOW + timedelta(seconds=1),
                industry="future update",
            )
        ],
        now=NOW,
    )

    assert profile["industry"] is None
    assert profile["data_quality"]["display_only"][0]["reason"] == "future_source_updated_at"


def test_future_report_period_inside_revenue_mapping_items_is_display_only():
    profile = select_evidence_profile(
        "000001",
        [
            evidence_doc(
                "tushare",
                "fina_mainbz",
                revenue_composition={
                    "items": [
                        {"item": "future item", "report_period": "2027-03-31"}
                    ]
                },
            )
        ],
        now=NOW,
    )

    assert profile["revenue_composition"] is None
    assert profile["data_quality"]["display_only"][0]["reason"] == "future_report_period"


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
    cached = fresh_runtime_evidence_doc(
        "tushare", "stock_basic", industry="cached industry"
    )
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
async def test_refresh_true_skips_providers_for_fresh_complete_cache():
    cached_documents = [
        fresh_runtime_evidence_doc(
            "tushare",
            "stock_basic",
            industry="计算机",
            provider_sector="计算机",
        ),
        fresh_runtime_evidence_doc(
            "tushare",
            "stock_company",
            main_business="真实主营",
        ),
        fresh_runtime_evidence_doc(
            "tushare",
            "fina_mainbz",
            report_period="2026-03-31",
            revenue_composition=[{"item": "主营", "revenue": 1}],
        ),
    ]
    fetchers = {
        "tushare": AsyncMock(side_effect=AssertionError("fresh cache must not fetch")),
        "baostock": AsyncMock(side_effect=AssertionError("fresh cache must not fetch")),
    }
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([{"code": "000001", "source_documents": cached_documents}])),
        provider_fetchers=fetchers,
    )

    result = await service.resolve_many(["000001"], refresh=True)

    assert result["000001"]["status"] == "verified"
    for fetcher in fetchers.values():
        fetcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_true_calls_providers_for_incomplete_cache():
    fetcher = AsyncMock(
        return_value=[evidence_doc("tushare", "stock_basic", industry="计算机", provider_sector="计算机")]
    )
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([{"code": "000001", "source_documents": []}])),
        provider_fetchers={"tushare": fetcher},
    )

    await service.resolve_many(["000001"], refresh=True)

    fetcher.assert_awaited_once_with("000001")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revenue_document",
    [None, evidence_doc("tushare", "fina_mainbz", report_period="2024-01-01", revenue_composition=[{"item": "旧"}])],
)
async def test_refresh_true_calls_providers_when_revenue_is_missing_or_stale(revenue_document):
    documents = [
        evidence_doc("tushare", "stock_basic", industry="计算机", provider_sector="计算机"),
        evidence_doc("tushare", "stock_company", main_business="真实主营"),
    ]
    if revenue_document:
        documents.append(revenue_document)
    fetcher = AsyncMock(return_value=documents + [evidence_doc(
        "tushare", "fina_mainbz", report_period="2026-03-31", revenue_composition=[{"item": "新"}]
    )])
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([{"code": "000001", "source_documents": documents}])),
        provider_fetchers={"tushare": fetcher},
    )

    await service.resolve_many(["000001"], refresh=True)

    fetcher.assert_awaited_once_with("000001")


@pytest.mark.asyncio
async def test_recent_failed_refresh_attempt_backoff_skips_provider_calls():
    fetcher = AsyncMock(side_effect=AssertionError("backoff must not fetch"))
    collection = FakeCollection(
        [
            {
                "code": "000001",
                "source_documents": [],
                "refresh_started_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "data_quality": {
                    "provider_errors": [
                        {"source": "tushare", "error_code": "provider_permission_denied"}
                    ]
                },
            }
        ]
    )
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection), provider_fetchers={"tushare": fetcher}
    )

    result = await service.resolve_many(["000001"], refresh=True)

    fetcher.assert_not_awaited()
    assert result["000001"]["data_quality"]["provider_errors"][0]["error_code"] == "provider_permission_denied"


@pytest.mark.asyncio
async def test_refresh_persists_source_documents_and_structured_errors():
    fresh = fresh_runtime_evidence_doc(
        "tushare", "stock_basic", industry="fresh industry"
    )
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
            "error_code": "provider_timeout",
            "message": "Provider request timed out.",
        },
        {"source": "akshare", "error_code": "provider_error", "message": "Provider request failed."},
    ]
    assert len(collection.updated) == 1
    saved = collection.updated[0]["$set"]
    assert saved["code"] == "000001"
    assert saved["source_documents"] == [fresh]
    fetchers["tushare"].assert_awaited_once_with("000001")
    fetchers["baostock"].assert_awaited_once_with("000001")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "error_code", "safe_message"),
    [
        (TimeoutError("timeout secret"), "provider_timeout", "Provider request timed out."),
        (PermissionError("permission secret"), "provider_permission_denied", "Provider access denied."),
        (ValueError("generic secret"), "provider_error", "Provider request failed."),
    ],
)
async def test_provider_errors_are_safe_but_originals_are_logged(
    exception, error_code, safe_message, caplog
):
    fetcher = AsyncMock(side_effect=exception)
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([])), provider_fetchers={"tushare": fetcher}
    )

    with caplog.at_level(logging.ERROR, logger="app.services.company_profile_enrichment_service"):
        profile = (await service.resolve_many(["000001"], refresh=True))["000001"]

    assert profile["data_quality"]["provider_errors"] == [
        {
            "source": "tushare",
            "error_code": error_code,
            "message": safe_message,
        }
    ]
    assert str(exception) not in repr(profile)
    assert str(exception) in caplog.text


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
    assert profile["data_quality"]["provider_errors"][0]["error_code"] == "provider_timeout"


@pytest.mark.asyncio
async def test_cached_errors_and_display_only_are_sorted_for_both_refresh_modes():
    cached = {
        "code": "000001",
        "source_documents": [],
        "data_quality": {
            "provider_errors": [
                {"source": "akshare", "error_code": "provider_error", "message": "old ak"},
                {"source": "tushare", "error_code": "provider_error", "message": "old ts"},
            ],
            "display_only": [
                {"reason": "unknown_provider_sector", "source": "tushare", "raw_taxonomy_value": "z"},
                {"reason": "unknown_provider_sector", "source": "tushare", "raw_taxonomy_value": "a"},
            ],
        },
    }
    outputs = []
    for refresh in (False, True):
        collection = FakeCollection([cached])
        service = CompanyProfileEnrichmentService(
            db=FakeDatabase(collection), provider_fetchers={}
        )
        outputs.append((await service.resolve_many(["000001"], refresh=refresh))["000001"])

    assert outputs[0]["data_quality"]["provider_errors"] == [
        {"source": "tushare", "error_code": "provider_error", "message": "Provider request failed."},
        {"source": "akshare", "error_code": "provider_error", "message": "Provider request failed."},
    ]
    assert outputs[0]["data_quality"]["display_only"] == outputs[1]["data_quality"]["display_only"]
    assert outputs[0]["data_quality"]["provider_errors"] == outputs[1]["data_quality"]["provider_errors"]


@pytest.mark.asyncio
async def test_slower_refresh_cannot_overwrite_newer_generation():
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    slow_doc = fresh_runtime_evidence_doc(
        "tushare", "stock_basic", industry="slow generation"
    )
    fast_doc = fresh_runtime_evidence_doc(
        "tushare", "stock_basic", industry="fast generation"
    )

    async def slow_fetch(_code):
        slow_started.set()
        await release_slow.wait()
        return [slow_doc]

    async def fast_fetch(_code):
        return [fast_doc]

    collection = FakeCollection([], raise_duplicate_on_conditional_miss=True)
    slow_service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection), provider_fetchers={"tushare": slow_fetch}
    )
    fast_service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection), provider_fetchers={"tushare": fast_fetch}
    )

    slow_task = asyncio.create_task(slow_service.resolve_many(["000001"], refresh=True))
    await slow_started.wait()
    fast_result = await fast_service.resolve_many(["000001"], refresh=True)
    release_slow.set()
    slow_result = await slow_task

    assert fast_result["000001"]["industry"] == "fast generation"
    assert slow_result["000001"]["industry"] == "fast generation"
    assert collection.rows[0]["source_documents"][0]["industry"] == "fast generation"
    assert collection.indexes == [
        ([ ("code", 1) ], True, "stock_company_profiles_code_unique"),
        ([ ("code", 1) ], True, "stock_company_profiles_code_unique"),
    ]


@pytest.mark.asyncio
async def test_refresh_validates_timestamps_after_provider_fetch_completes():
    async def delayed_fetch(_code):
        await asyncio.sleep(0.01)
        return [
            evidence_doc(
                "tushare",
                "stock_basic",
                retrieved_at=datetime.now(timezone.utc),
                industry="created during fetch",
            )
        ]

    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(FakeCollection([])), provider_fetchers={"tushare": delayed_fetch}
    )

    result = await service.resolve_many(["000001"], refresh=True)

    assert result["000001"]["industry"] == "created during fetch"


@pytest.mark.asyncio
async def test_cache_query_isolated_by_code():
    collection = FakeCollection(
        [{"code": "000002", "source_documents": [evidence_doc("tushare", "stock_basic", industry="other")]}]
    )
    service = CompanyProfileEnrichmentService(db=FakeDatabase(collection))

    result = await service.resolve_many(["000001"], refresh=False)

    assert result["000001"]["industry"] is None
    assert collection.queries[-1] == {"code": {"$in": ["000001"]}}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length):
        return self.rows[:length]


class FakeCollection:
    def __init__(self, rows, raise_duplicate_on_conditional_miss=False):
        self.rows = [dict(row) for row in rows]
        self.updated = []
        self.queries = []
        self.indexes = []
        self.raise_duplicate_on_conditional_miss = raise_duplicate_on_conditional_miss

    async def create_index(self, keys, unique=False, name=None):
        self.indexes.append((keys, unique, name))
        return name

    def find(self, query):
        self.queries.append(query)
        codes = set(query.get("code", {}).get("$in", []))
        return FakeCursor([row for row in self.rows if row.get("code") in codes])

    async def update_one(self, query, update, upsert=False):
        self.updated.append(update)
        code = update["$set"]["code"]
        existing = next((row for row in self.rows if row.get("code") == code), None)
        if existing is not None:
            if "$or" in query:
                allowed = False
                for condition in query["$or"]:
                    clause = condition.get("refresh_started_at", {})
                    if clause.get("$exists") is False and "refresh_started_at" not in existing:
                        allowed = True
                    if "$lte" in clause and existing.get("refresh_started_at") is not None:
                        allowed = allowed or existing["refresh_started_at"] <= clause["$lte"]
                if not allowed:
                    if self.raise_duplicate_on_conditional_miss:
                        raise DuplicateKeyError()
                    return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
            existing.update(update["$set"])
            return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if upsert:
            self.rows.append(dict(update["$set"]))
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=code)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)


class DuplicateKeyError(Exception):
    pass


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "stock_company_profiles"
        return self.collection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_code",
    ["600406", "600406.SH", "SH.600406", "sh600406"],
)
async def test_resolve_many_normalises_exchange_code_forms(input_code):
    collection = FakeCollection(
        [
            {
                "code": "600406",
                "source_documents": [
                    fresh_runtime_evidence_doc(
                        "tushare",
                        "stock_basic",
                        code="600406",
                        source_record_key="600406.SH",
                        industry="电力设备",
                    )
                ],
            }
        ]
    )
    service = CompanyProfileEnrichmentService(
        db=FakeDatabase(collection),
        provider_fetchers={},
    )

    result = await service.resolve_many([input_code])

    assert list(result) == ["600406"]
    assert result["600406"]["industry"] == "电力设备"
