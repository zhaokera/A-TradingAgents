from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.favorites_service import FavoritesService


def _candidate(
    code: str,
    *,
    rank_score: float,
    actionability: str = "watch_trigger",
    can_add: bool = True,
) -> dict:
    return {
        "code": code,
        "name": f"候选{code}",
        "market": "A股",
        "rank_score": rank_score,
        "actionability": actionability,
        "actionability_label": "等待触发",
        "can_add_to_favorites": can_add,
        "reference_price": 10.2,
        "quote_source": "tencent",
        "trade_at": "2026-07-29T14:00:00+08:00",
        "provider_updated_at": "2026-07-29T14:00:00+08:00",
        "quote_time_semantics": "provider_snapshot_updated_at",
        "exchange_trade_time_verified": False,
        "price_plan": {
            "entry_strategy": "pullback",
            "entry_price": 10.0,
            "stop_price": 9.4,
            "target_price": 11.8,
        },
        "portfolio_gate": {"blocked": actionability == "blocked"},
        "objective_id": "technology_new_quality_productive_forces",
        "objective_label": "科技 + 新质生产力",
        "objective_tier": "core",
        "objective_tier_label": "核心方向",
        "objective_segment": "数字科技",
        "profile_evidence": {
            "status": "missing",
            "confidence": "missing",
            "complete": False,
            "missing_fields": ["industry", "main_business"],
            "warning_code": "stock_profile_evidence_incomplete",
            "message": "主营与行业证据缺失，当前仅作价格提醒。",
        },
    }


@pytest.mark.asyncio
async def test_auto_ai_sync_promotes_only_trackable_candidates_and_preserves_user_items():
    existing = [
        {
            "stock_code": "000001",
            "stock_name": "手工自选",
            "source": "manual",
            "tags": ["长期"],
            "notes": "用户内容",
        },
        {
            "stock_code": "000837",
            "stock_name": "用户从候选手工加入",
            "source": "ai_screening",
            "tags": ["AI候选"],
            "ai_metadata": {"run_id": "old-user-selected"},
        },
        {
            "stock_code": "830799",
            "stock_name": "用户手工北交所自选",
            "source": "manual",
            "tags": ["研究"],
            "notes": "保留研究项",
        },
        {
            "stock_code": "600999",
            "stock_name": "旧自动候选",
            "source": "ai_screening",
            "tags": ["AI候选"],
            "ai_metadata": {
                "run_id": "old-auto",
                "auto_promoted": True,
                "lifecycle_state": "current",
            },
        },
    ]
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={"user_id": "admin-id", "favorites": deepcopy(existing)}
        ),
        update_one=AsyncMock(),
    )
    service = FavoritesService()
    service.db = SimpleNamespace(user_favorites=collection)
    candidates = [
        _candidate("688001", rank_score=100),
        _candidate("920493", rank_score=99),
        _candidate("300450", rank_score=98),
        _candidate("600001", rank_score=95),
        _candidate("600002", rank_score=90, actionability="blocked", can_add=False),
        _candidate("600003", rank_score=85),
        _candidate("600004", rank_score=80),
    ]

    result = await service.sync_auto_ai_candidates(
        "admin-id",
        candidates=candidates,
        run_id="run-new",
        generated_at=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
        max_auto_candidates=2,
        market_permissions={
            "star_market": {"verified": False, "tradable": False},
            "beijing_stock_exchange": {
                "verified": False,
                "tradable": False,
            },
            "chi_next_market": {
                "verified": True,
                "tradable": False,
            },
        },
    )

    assert result["selected_codes"] == ["600001", "600003"]
    assert result["added_codes"] == ["600001", "600003"]
    assert result["removed_codes"] == ["600999"]
    assert result["preserved_user_codes"] == ["000001", "000837", "830799"]
    stored = collection.update_one.await_args.args[1]["$set"]["favorites"]
    by_code = {item["stock_code"]: item for item in stored}
    assert set(by_code) == {
        "000001",
        "000837",
        "830799",
        "600001",
        "600003",
    }
    assert by_code["000001"]["notes"] == "用户内容"
    assert by_code["000837"]["ai_metadata"]["run_id"] == "old-user-selected"
    promoted = by_code["600001"]
    assert promoted["source"] == "ai_screening"
    assert promoted["alert_price_low"] == 10.0
    assert promoted["alert_price_high"] is None
    assert promoted["ai_metadata"]["auto_promoted"] is True
    assert promoted["ai_metadata"]["price_alert_only"] is True
    assert promoted["ai_metadata"]["condition_order_created"] is False
    assert promoted["ai_metadata"]["profile_evidence"]["status"] == "missing"
    assert promoted["ai_metadata"]["profile_evidence"]["complete"] is False
    assert promoted["ai_metadata"]["quote_provider_updated_at"] == (
        "2026-07-29T14:00:00+08:00"
    )
    assert promoted["ai_metadata"]["quote_time_semantics"] == (
        "provider_snapshot_updated_at"
    )
    assert promoted["ai_metadata"]["exchange_trade_time_verified"] is False
    assert "688001" not in by_code
    assert "920493" not in by_code
    assert "300450" not in by_code
    assert "600002" not in by_code


@pytest.mark.asyncio
async def test_auto_ai_sync_updates_existing_auto_pick_without_duplication():
    existing = {
        "stock_code": "600001",
        "stock_name": "已有自动候选",
        "source": "ai_screening",
        "tags": ["AI候选", "用户标签"],
        "notes": "保留备注",
        "alert_price_low": 9.8,
        "ai_metadata": {
            "run_id": "old",
            "auto_promoted": True,
            "lifecycle_state": "current",
        },
    }
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={"user_id": "admin-id", "favorites": [deepcopy(existing)]}
        ),
        update_one=AsyncMock(),
    )
    service = FavoritesService()
    service.db = SimpleNamespace(user_favorites=collection)

    result = await service.sync_auto_ai_candidates(
        "admin-id",
        candidates=[_candidate("600001", rank_score=95)],
        run_id="run-new",
        generated_at=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
        max_auto_candidates=5,
    )

    assert result["added_codes"] == []
    assert result["updated_codes"] == ["600001"]
    stored = collection.update_one.await_args.args[1]["$set"]["favorites"]
    assert len(stored) == 1
    assert stored[0]["notes"] == "保留备注"
    assert stored[0]["tags"] == ["AI候选", "用户标签"]
    assert stored[0]["alert_price_low"] == 9.8
    assert stored[0]["ai_metadata"]["run_id"] == "run-new"


@pytest.mark.asyncio
async def test_manual_beijing_favorite_is_permission_blocked_research(
    monkeypatch,
):
    user_favorites = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "user_id": "admin-id",
                "favorites": [
                    {
                        "stock_code": "920493",
                        "stock_name": "并行科技",
                        "source": "manual",
                        "tags": ["研究"],
                        "notes": "用户手工保留",
                    }
                ],
            }
        )
    )
    settings = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "execution_capabilities": {
                    "market_permissions": {
                        "star_market": {
                            "verified": True,
                            "tradable": False,
                        }
                    }
                }
            }
        )
    )
    basic_cursor = SimpleNamespace(to_list=AsyncMock(return_value=[]))
    basic_info = SimpleNamespace(find=MagicMock(return_value=basic_cursor))
    db = MagicMock()
    db.user_favorites = user_favorites
    db.__getitem__.side_effect = lambda name: {
        "user_holding_settings": settings,
        "stock_basic_info": basic_info,
    }[name]
    quote_service = SimpleNamespace(get_quotes=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.favorites_service.get_quotes_service",
        lambda: quote_service,
    )
    monkeypatch.setattr(
        "app.core.unified_config.UnifiedConfigManager."
        "get_data_source_configs_async",
        AsyncMock(return_value=[]),
    )
    service = FavoritesService()
    service.db = db

    items = await service.get_user_favorites("admin-id")

    assert len(items) == 1
    item = items[0]
    assert item["stock_code"] == "920493"
    assert item["notes"] == "用户手工保留"
    assert item["source"] == "manual"
    assert item["execution_actionable"] is False
    assert item["is_reference_only"] is True
    assert item["permission_reason_code"] == (
        "beijing_stock_exchange_permission_unverified"
    )
    assert item["market_permission"]["reason_code"] == (
        "permission_unverified"
    )


@pytest.mark.asyncio
async def test_manual_chinext_favorite_is_preserved_as_permission_blocked_research(
    monkeypatch,
):
    user_favorites = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "user_id": "admin-id",
                "favorites": [
                    {
                        "stock_code": "300450",
                        "stock_name": "先导智能",
                        "source": "manual",
                        "tags": ["研究"],
                        "notes": "用户手工保留",
                    }
                ],
            }
        )
    )
    settings = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "execution_capabilities": {
                    "market_permissions": {
                        "chi_next_market": {
                            "verified": True,
                            "tradable": False,
                            "source": "user_confirmed",
                        }
                    }
                }
            }
        )
    )
    basic_cursor = SimpleNamespace(to_list=AsyncMock(return_value=[]))
    basic_info = SimpleNamespace(find=MagicMock(return_value=basic_cursor))
    db = MagicMock()
    db.user_favorites = user_favorites
    db.__getitem__.side_effect = lambda name: {
        "user_holding_settings": settings,
        "stock_basic_info": basic_info,
    }[name]
    quote_service = SimpleNamespace(get_quotes=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.favorites_service.get_quotes_service",
        lambda: quote_service,
    )
    monkeypatch.setattr(
        "app.core.unified_config.UnifiedConfigManager."
        "get_data_source_configs_async",
        AsyncMock(return_value=[]),
    )
    service = FavoritesService()
    service.db = db

    items = await service.get_user_favorites("admin-id")

    assert len(items) == 1
    item = items[0]
    assert item["stock_code"] == "300450"
    assert item["notes"] == "用户手工保留"
    assert item["source"] == "manual"
    assert item["execution_actionable"] is False
    assert item["is_reference_only"] is True
    assert item["permission_reason_code"] == (
        "chi_next_market_permission_denied"
    )
    assert item["market_permission"]["reason_code"] == "permission_denied"
