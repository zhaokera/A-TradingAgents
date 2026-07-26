from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from app.services.ai_candidate_service import (
    AI_CANDIDATE_SOURCE,
    AICandidateRunNotFoundError,
    AICandidateService,
    InvalidAICandidateSelectionError,
    normalize_ai_candidate,
    normalize_ai_candidate_run,
)
from app.services.favorites_service import FavoritesService


def _research_payload():
    return {
        "ok": True,
        "data": {
            "candidate_discovery": {
                "benchmark_trade_date": "2026-07-17",
                "universe_count": 5527,
                "eligible_count": 2134,
                "selected_count": 2,
                "technical_passed_count": 2,
                "earnings_selected_count": 2,
                "total_coverage_ratio": 1.0,
            },
            "market_status": {
                "market_session": {
                    "session": "closed",
                    "is_trading_hours": False,
                    "local_time": "2026-07-20T14:00:00+08:00",
                }
            },
            "context": {
                "horizon": "未来两个交易日",
                "technical_deep_check_status": "ok",
                "earnings_forecast_review_status": "ok",
            },
            "candidates": [
                {
                    "code": "600001",
                    "name": "候选一",
                    "priority": 2,
                    "quote": {
                        "price": 11.2,
                        "trade_at": "2026-07-17T15:00:00+08:00",
                    },
                    "discovery": {
                        "public": {"bucket": "pullback"},
                        "tencent": {"pct_change": -0.8},
                    },
                    "guarded_price_plan": {
                        "status": "ok",
                        "entry_strategy": "pullback",
                        "suggested_buy_price": 11.0,
                        "stop_loss_price": 10.4,
                        "target_price": 12.6,
                    },
                    "triggers": {
                        "observation_zone": [10.8, 11.1],
                        "breakout_price": 11.4,
                        "invalidation_price": 10.35,
                    },
                    "risk_flags": [],
                    "is_reference_only": True,
                },
                {
                    "code": "600000",
                    "name": "候选二",
                    "priority": 1,
                    "quote": {"price": 9.8},
                    "discovery": {
                        "public": {"bucket": "strength"},
                        "tencent": {"pct_change": 1.2},
                    },
                    "guarded_price_plan": {"status": "history_unavailable"},
                    "triggers": {
                        "breakout_price": 10.0,
                        "invalidation_price": 9.2,
                    },
                    "risk_flags": [
                        {
                            "code": "wait_for_breakout",
                            "severity": "warning",
                            "message": "等待有效突破。",
                        }
                    ],
                    "is_reference_only": True,
                },
            ],
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
            "generated_at": "2026-07-20T06:00:00Z",
        },
    }


def test_normalize_ai_candidate_run_keeps_reference_only_price_evidence():
    result = normalize_ai_candidate_run(
        _research_payload(),
        max_candidates=5,
        favorite_codes={"600001"},
    )

    assert [item["code"] for item in result["candidates"]] == ["600000", "600001"]
    first = result["candidates"][0]
    second = result["candidates"][1]
    assert first["research_status"] == "observe"
    assert first["is_reference_only"] is True
    assert first["price_plan"]["entry_price"] == 10.0
    assert first["price_plan"]["stop_price"] == 9.2
    assert first["price_plan"]["entry_status"] == "plan_unavailable"
    assert first["favorite_status"] == "not_added"
    assert second["price_plan"]["observation_zone"] == [10.8, 11.1]
    assert second["price_plan"]["entry_price"] == 11.0
    assert second["price_plan"]["entry_strategy"] == "pullback"
    assert second["price_plan"]["entry_status"] == "waiting_pullback"
    assert second["price_plan"]["distance_to_entry_pct"] == -1.79
    assert "等待回落" in second["price_plan"]["entry_guidance"]
    assert second["favorite_status"] == "in_favorites"
    assert "suggested_quantity" not in second
    assert result["disclaimer"].startswith("仅供研究参考")


def test_normalize_ai_candidate_run_sorts_core_objective_before_non_core():
    payload = _research_payload()
    payload["data"]["candidates"] = [
        {
            "code": "600690",
            "name": "海尔智家",
            "priority": 1,
            "quote": {"price": 21.8},
            "guarded_price_plan": {"status": "history_unavailable"},
            "risk_flags": [],
        },
        {
            "code": "600406",
            "name": "国电南瑞",
            "priority": 2,
            "quote": {"price": 23.5},
            "guarded_price_plan": {"status": "history_unavailable"},
            "risk_flags": [],
        },
    ]

    result = normalize_ai_candidate_run(
        payload,
        max_candidates=5,
        favorite_codes=set(),
    )

    assert [item["code"] for item in result["candidates"]] == [
        "600406",
        "600690",
    ]
    assert result["objective"]["label"] == "科技 + 新质生产力"
    assert result["candidates"][0]["objective_tier"] == "core"
    assert result["candidates"][1]["objective_tier"] == "non_core"


def test_breakout_price_ready_is_still_blocked_by_quote_risk():
    candidate = normalize_ai_candidate(
        {
            "code": "600010",
            "name": "突破候选",
            "quote": {"price": 10.2},
            "guarded_price_plan": {
                "status": "ok",
                "entry_strategy": "breakout",
                "suggested_buy_price": 10.0,
                "stop_loss_price": 9.4,
                "target_price": 11.8,
            },
            "risk_flags": [
                {
                    "code": "quote_not_actionable",
                    "message": "腾讯行情时效门槛未通过。",
                }
            ],
        },
        context={},
        favorite_codes=set(),
    )

    assert candidate is not None
    price_plan = candidate["price_plan"]
    assert price_plan["entry_strategy_label"] == "突破参考"
    assert price_plan["price_condition_met"] is True
    assert price_plan["risk_blocked"] is True
    assert price_plan["entry_status"] == "price_ready_risk_blocked"
    assert "刷新行情后再确认" in price_plan["entry_guidance"]


def test_price_below_stop_marks_original_entry_plan_invalidated():
    candidate = normalize_ai_candidate(
        {
            "code": "600011",
            "name": "失效候选",
            "quote": {"price": 9.1},
            "guarded_price_plan": {
                "status": "ok",
                "entry_strategy": "pullback",
                "suggested_buy_price": 10.0,
                "stop_loss_price": 9.2,
                "target_price": 11.5,
            },
            "risk_flags": [],
        },
        context={},
        favorite_codes=set(),
    )

    assert candidate is not None
    price_plan = candidate["price_plan"]
    assert price_plan["price_condition_met"] is False
    assert price_plan["entry_status"] == "invalidated"
    assert "原价格计划失效" in price_plan["entry_guidance"]


@pytest.mark.asyncio
async def test_run_persists_user_owned_candidate_batch():
    collection = SimpleNamespace(insert_one=AsyncMock())
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value={"600001"})
    )
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=favorites,
    )
    service.db = db

    result = await service.run("admin-id", max_candidates=1)

    assert result["run_id"]
    assert result["user_id"] == "admin-id"
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["code"] == "600000"
    stored = collection.insert_one.await_args.args[0]
    assert stored["user_id"] == "admin-id"
    assert stored["expires_at"] > stored["generated_at"]


@pytest.mark.asyncio
async def test_latest_reconciles_favorite_status_with_current_favorites():
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": ObjectId(),
                "user_id": "admin-id",
                "generated_at": datetime(2026, 7, 20, 7, 12, 38),
                "candidates": [
                    {"code": "600000", "favorite_status": "in_favorites"},
                    {"code": "600001", "favorite_status": "not_added"},
                ],
            }
        )
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value={"600001"})
    )
    service = AICandidateService(research_runner=_research_payload, favorites=favorites)
    service.db = db

    result = await service.latest("admin-id")

    assert result is not None
    assert [item["favorite_status"] for item in result["candidates"]] == [
        "not_added",
        "in_favorites",
    ]


@pytest.mark.asyncio
async def test_add_to_favorites_uses_persisted_candidate_and_ai_source():
    run_id = ObjectId()
    document = {
        "_id": run_id,
        "user_id": "admin-id",
        "generated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "source": "public_full_market",
        "context": {"horizon": "未来两个交易日"},
        "candidates": [
            {
                "code": "600000",
                "name": "候选二",
                "reference_price": 9.8,
                "reason_summary": "等待突破条件确认。",
                "objective_id": "technology_new_quality_productive_forces",
                "objective_label": "科技 + 新质生产力",
                "objective_tier": "core",
                "objective_tier_label": "核心方向",
                "objective_segment": "数字科技",
                "price_plan": {
                    "entry_price": 10.0,
                    "stop_price": 9.2,
                    "target_price": 11.5,
                },
                "favorite_status": "not_added",
            },
            {
                "code": "600001",
                "name": "候选一",
                "favorite_status": "in_favorites",
            },
        ],
    }
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=document),
        update_one=AsyncMock(),
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value={"600001"}),
        add_favorite=AsyncMock(return_value=True),
    )
    service = AICandidateService(research_runner=_research_payload, favorites=favorites)
    service.db = db

    result = await service.add_to_favorites(
        "admin-id",
        str(run_id),
        ["600000", "600001"],
    )

    assert result["added_codes"] == ["600000"]
    assert result["already_exists_codes"] == ["600001"]
    call = favorites.add_favorite.await_args
    assert call.kwargs["source"] == AI_CANDIDATE_SOURCE
    assert call.kwargs["ai_metadata"]["run_id"] == str(run_id)
    assert call.kwargs["ai_metadata"]["price_plan"]["stop_price"] == 9.2
    assert call.kwargs["ai_metadata"]["objective_label"] == "科技 + 新质生产力"
    assert call.kwargs["ai_metadata"]["objective_tier"] == "core"
    updated_candidates = collection.update_one.await_args.args[1]["$set"]["candidates"]
    assert all(item["favorite_status"] == "in_favorites" for item in updated_candidates)


@pytest.mark.asyncio
async def test_add_to_favorites_rejects_unknown_or_foreign_run():
    collection = SimpleNamespace(find_one=AsyncMock(return_value=None))
    db = MagicMock()
    db.__getitem__.return_value = collection
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
    )
    service.db = db

    with pytest.raises(AICandidateRunNotFoundError):
        await service.add_to_favorites("other-user", str(ObjectId()), ["600000"])


@pytest.mark.asyncio
async def test_add_to_favorites_rejects_code_outside_persisted_run():
    run_id = ObjectId()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": run_id,
                "user_id": "admin-id",
                "candidates": [{"code": "600000", "name": "候选"}],
            }
        )
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
    )
    service.db = db

    with pytest.raises(InvalidAICandidateSelectionError):
        await service.add_to_favorites("admin-id", str(run_id), ["600999"])


def test_favorite_format_defaults_old_records_to_manual_source():
    formatted = FavoritesService()._format_favorite(
        {
            "stock_code": "600000",
            "stock_name": "旧自选",
            "market": "A股",
        }
    )

    assert formatted["source"] == "manual"
    assert formatted["ai_metadata"] is None


def test_serialize_run_marks_mongo_naive_datetime_as_utc():
    serialized = AICandidateService._serialize_run(
        {
            "_id": ObjectId(),
            "generated_at": datetime(2026, 7, 20, 7, 12, 38),
        }
    )

    assert serialized["generated_at"] == "2026-07-20T07:12:38+00:00"


def test_serialize_run_backfills_entry_state_for_existing_candidates():
    serialized = AICandidateService._serialize_run(
        {
            "_id": ObjectId(),
            "candidates": [
                {
                    "code": "000333",
                    "reference_price": 84.3,
                    "price_plan": {
                        "entry_price": 82.78,
                        "stop_price": 80.58,
                        "target_price": 91.59,
                        "status": "ok",
                    },
                    "risk_flags": [
                        {
                            "code": "quote_not_actionable",
                            "message": "行情时效门槛未通过。",
                        }
                    ],
                }
            ],
        }
    )

    price_plan = serialized["candidates"][0]["price_plan"]
    assert price_plan["entry_strategy"] == "pullback"
    assert price_plan["entry_status"] == "waiting_pullback"
    assert price_plan["entry_status_label"] == "等待回落"
    assert price_plan["distance_to_entry_pct"] == -1.8
