from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.ai_candidate_service import AICandidateService
from app.services.ai_candidate_service import _normalize_risk_flags
from app.services.favorites_service import FavoritesService
from app.services.global_macro_risk_service import score_macro_snapshot
from app.services.investment_policy import (
    allocate_candidate_portfolio,
    build_dynamic_portfolio_policy,
)
from app.services.stock_master_data_service import select_master_profile
from app.services.stock_master_data_service import StockMasterDataService


def _candidate(code: str, entry: float, stop: float, quantity: int = 300):
    return {
        "code": code,
        "price_plan": {
            "entry_price": entry,
            "stop_price": stop,
            "target_price": entry + (entry - stop) * 2,
        },
        "position_sizing": {
            "status": "sized",
            "suggested_quantity": quantity,
        },
    }


def test_portfolio_allocator_enforces_shared_capital_and_loss_budgets():
    policy = build_dynamic_portfolio_policy(
        total_assets=10_000,
        current_exposure_pct=0,
        market_regime="green",
    )
    result = allocate_candidate_portfolio(
        [
            _candidate("600001", 10.0, 9.5),
            _candidate("600002", 10.0, 9.5),
            _candidate("600003", 10.0, 9.5),
        ],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy,
    )

    assert result["allocated_amount"] <= 6_000
    assert result["total_planned_loss"] <= 200
    assert all(item["quantity"] % 100 == 0 for item in result["allocations"])
    assert result["allocated_position_count"] == 2
    assert result["allocations"][2]["status"] == "budget_exhausted"


@pytest.mark.parametrize(
    ("entry", "stop"),
    [(float("nan"), 9.5), (10.0, float("inf"))],
)
def test_legacy_portfolio_allocator_rejects_non_finite_prices(entry, stop):
    result = allocate_candidate_portfolio(
        [_candidate("600001", entry, stop)],
        total_assets=10_000,
        available_cash=10_000,
        policy=build_dynamic_portfolio_policy(
            total_assets=10_000,
            current_exposure_pct=0,
            market_regime="green",
        ),
    )

    assert result["allocated_position_count"] == 0
    assert result["allocations"][0]["reason"] == "price_plan_or_account_unavailable"


def test_macro_snapshot_downgrades_on_cross_asset_stress():
    result = score_macro_snapshot(
        {
            "vix": 34.0,
            "nasdaq_change_pct": -2.2,
            "sp500_change_pct": -1.8,
            "usdcnh": 7.4,
        }
    )

    assert result["regime"] == "red"
    assert result["score"] >= 4
    assert len(result["factors"]) >= 3


def test_candidate_stale_quote_warning_matches_conditional_budget_semantics():
    flags = _normalize_risk_flags(
        [
            {
                "code": "quote_not_actionable",
                "severity": "warning",
                "message": "不能生成仓位数量",
            }
        ]
    )

    assert "可保留条件价和组合预算" in flags[0]["message"]
    assert "触发时必须刷新行情" in flags[0]["message"]
    assert "不能生成仓位数量" not in flags[0]["message"]


def test_stock_master_prefers_authoritative_industry_and_business_evidence():
    result = select_master_profile(
        "600001",
        [
            {
                "code": "600001",
                "name": "样例",
                "industry": "计算机设备",
                "main_business": "服务器与算力基础设施",
                "source": "tushare",
                "source_endpoint": "stock_basic",
                "source_record_key": "600001.SH:stock_basic",
                "retrieved_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
            },
            {
                "code": "600001",
                "name": "样例",
                "industry": "其他",
                "source": "akshare",
                "source_endpoint": "stock_individual_info_em",
                "source_record_key": "600001:stock_individual_info_em",
                "retrieved_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            },
        ],
    )

    assert result["source"] == "tushare"
    assert result["confidence"] == "high"
    assert {item["field"] for item in result["evidence"]} == {
        "industry",
        "main_business",
        "provider_sector",
    }


def test_stock_master_fails_closed_for_local_rows_without_provenance():
    result = select_master_profile(
        "600001",
        [{"code": "600001", "industry": "计算机", "main_business": "本地描述"}],
    )

    assert result["status"] == "missing"
    assert result["industry"] is None
    assert result["main_business"] is None
    assert result["data_quality"]["display_only"]


def test_stock_master_name_ignores_rows_for_other_codes():
    result = select_master_profile(
        "600001",
        [
            {"code": "000001", "name": "错误名称"},
            {"code": "600001", "name": "正确名称"},
        ],
    )

    assert result["name"] == "正确名称"


@pytest.mark.asyncio
async def test_stock_master_delegates_profile_resolution_and_keeps_local_name_display_only():
    class Cursor:
        async def to_list(self, length):
            return [{"code": "600001", "name": "展示名称"}]

    class Collection:
        def find(self, query, projection):
            assert query == {"code": {"$in": ["600001"]}}
            return Cursor()

    profile_service = SimpleNamespace(
        resolve_many=AsyncMock(
            return_value={
                "600001": {
                    "code": "600001",
                    "status": "incomplete",
                    "industry": None,
                    "main_business": None,
                    "data_quality": {"complete": False},
                }
            }
        )
    )
    db = {"stock_basic_info": Collection()}
    service = StockMasterDataService(db=db, profile_service=profile_service)

    result = await service.resolve_many(["600001"], refresh=False)

    profile_service.resolve_many.assert_awaited_once_with(["600001"], refresh=False)
    assert result["600001"]["name"] == "展示名称"
    assert result["600001"]["status"] == "incomplete"


def test_shadow_trade_waits_for_entry_then_uses_allocated_quantity():
    candidate = {
        "price_plan": {
            "entry_strategy": "pullback",
            "entry_price": 10.0,
            "stop_price": 9.5,
            "target_price": 11.0,
        },
        "portfolio_allocation": {"status": "allocated", "quantity": 100},
    }
    AICandidateService._update_performance(
        candidate,
        current_price=10.4,
        session_low=10.2,
        session_high=10.6,
        benchmark_price=4000,
        checked_at="2026-07-21T02:00:00+00:00",
        observation_key="1",
    )
    assert candidate["performance"]["shadow_trade"]["status"] == "waiting_entry"

    AICandidateService._update_performance(
        candidate,
        current_price=10.1,
        session_low=9.98,
        session_high=10.2,
        benchmark_price=4010,
        checked_at="2026-07-21T03:00:00+00:00",
        observation_key="2",
    )
    shadow = candidate["performance"]["shadow_trade"]
    assert shadow["status"] == "active"
    assert shadow["entry_price"] == 10.0
    assert shadow["quantity"] == 100


@pytest.mark.asyncio
async def test_favorite_lifecycle_archives_old_ai_candidates_without_deleting():
    favorites = [
        {
            "stock_code": "600001",
            "source": "ai_screening",
            "tags": ["用户标签"],
            "notes": "保留",
            "ai_metadata": {"run_id": "old"},
        },
        {
            "stock_code": "600002",
            "source": "ai_screening",
            "tags": ["AI候选"],
            "ai_metadata": {"run_id": "old"},
        },
        {"stock_code": "600003", "source": "manual", "notes": "手动"},
    ]
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value={"favorites": favorites}),
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
    )
    service = FavoritesService()
    service.db = SimpleNamespace(user_favorites=collection)

    result = await service.reconcile_ai_candidate_lifecycle(
        "admin",
        current_run_id="new",
        current_codes=["600002"],
        generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    saved = collection.update_one.await_args.args[1]["$set"]["favorites"]
    assert result == {"current": 1, "superseded": 1}
    assert saved[0]["ai_metadata"]["lifecycle_state"] == "superseded"
    assert saved[0]["tags"] == ["用户标签"]
    assert saved[0]["notes"] == "保留"
    assert saved[1]["ai_metadata"]["lifecycle_state"] == "current"
    assert saved[2]["source"] == "manual"
