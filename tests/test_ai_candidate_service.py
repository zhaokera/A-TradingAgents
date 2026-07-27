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
    _apply_candidate_state,
    normalize_ai_candidate,
    normalize_ai_candidate_run,
)
from app.services.favorites_service import FavoritesService


def _offline_research_dependencies():
    stock_master = SimpleNamespace(
        db=None,
        resolve_many=AsyncMock(return_value={}),
    )
    macro_risk = SimpleNamespace(
        db=None,
        get_current=AsyncMock(
            return_value={
                "status": "ok",
                "regime": "green",
                "score": 0,
                "factors": [],
            }
        ),
    )
    return stock_master, macro_risk


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

    assert [item["code"] for item in result["candidates"]] == ["600001", "600000"]
    first = result["candidates"][0]
    second = result["candidates"][1]
    assert first["research_status"] == "condition_order"
    assert first["is_reference_only"] is True
    assert first["price_plan"]["observation_zone"] == [10.8, 11.1]
    assert first["price_plan"]["entry_price"] == 11.0
    assert first["price_plan"]["entry_strategy"] == "pullback"
    assert first["price_plan"]["entry_status"] == "waiting_pullback"
    assert first["price_plan"]["distance_to_entry_pct"] == -1.79
    assert "等待回落" in first["price_plan"]["entry_guidance"]
    assert first["favorite_status"] == "in_favorites"
    assert second["price_plan"]["entry_price"] == 10.0
    assert second["price_plan"]["stop_price"] == 9.2
    assert second["price_plan"]["entry_status"] == "plan_unavailable"
    assert second["favorite_status"] == "not_added"
    assert "suggested_quantity" not in first
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


def test_warning_risk_does_not_block_price_ready_candidate():
    candidate = normalize_ai_candidate(
        {
            "code": "600012",
            "name": "普通警告候选",
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
                    "code": "wait_for_confirmation",
                    "severity": "warning",
                    "message": "仍需确认成交量。",
                }
            ],
        },
        context={},
        favorite_codes=set(),
    )

    assert candidate is not None
    assert candidate["price_plan"]["risk_blocked"] is False
    assert candidate["price_plan"]["entry_status"] == "price_ready"
    assert candidate["actionability"] == "ready_now"
    assert candidate["can_add_to_favorites"] is True


def test_invalidated_candidate_is_not_addable():
    candidate = normalize_ai_candidate(
        {
            "code": "600013",
            "name": "失效候选",
            "quote": {"price": 9.0},
            "guarded_price_plan": {
                "status": "ok",
                "entry_strategy": "pullback",
                "suggested_buy_price": 10.0,
                "stop_loss_price": 9.2,
                "target_price": 11.8,
            },
            "risk_flags": [],
        },
        context={},
        favorite_codes=set(),
    )

    assert candidate is not None
    assert candidate["actionability"] == "invalidated"
    assert candidate["can_add_to_favorites"] is False


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
    assert result["candidates"][0]["code"] == "600001"
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
        ),
        update_one=AsyncMock(),
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value={"600001"})
    )
    quotes = SimpleNamespace(get_quotes=AsyncMock(return_value={}))
    stock_master, macro_risk = _offline_research_dependencies()
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=favorites,
        quotes=quotes,
        stock_master=stock_master,
        macro_risk=macro_risk,
    )
    service.db = db

    result = await service.latest("admin-id")

    assert result is not None
    assert [item["favorite_status"] for item in result["candidates"]] == [
        "not_added",
        "in_favorites",
    ]


@pytest.mark.asyncio
async def test_latest_refreshes_tencent_quote_and_candidate_lifecycle():
    run_id = ObjectId()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": run_id,
                "user_id": "admin-id",
                "generated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
                "candidates": [
                    {
                        "code": "600000",
                        "name": "候选",
                        "reference_price": 10.6,
                        "price_plan": {
                            "entry_strategy": "pullback",
                            "entry_price": 10.0,
                            "stop_price": 9.2,
                            "target_price": 11.5,
                            "status": "ok",
                        },
                        "risk_flags": [],
                        "favorite_status": "not_added",
                    }
                ],
            }
        ),
        update_one=AsyncMock(),
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value=set()),
        update_ai_candidate_tracking=AsyncMock(return_value=True),
    )
    quotes = SimpleNamespace(
        get_quotes=AsyncMock(
            return_value={
                "600000": {
                    "price": 9.9,
                    "close": 9.9,
                    "pct_chg": -1.0,
                    "source": "tencent",
                    "trade_at": "2026-07-21T10:00:00+08:00",
                }
            }
        )
    )
    stock_master, macro_risk = _offline_research_dependencies()
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=favorites,
        quotes=quotes,
        stock_master=stock_master,
        macro_risk=macro_risk,
    )
    service.db = db

    result = await service.latest("admin-id")

    assert result is not None
    candidate = result["candidates"][0]
    assert candidate["reference_price"] == 9.9
    assert candidate["price_plan"]["entry_status"] == "price_ready"
    assert candidate["actionability"] == "ready_now"
    assert candidate["quote_source"] == "tencent"
    assert candidate["performance"]["observation_count"] == 1
    collection.update_one.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_add_to_favorites_rejects_invalidated_candidate():
    run_id = ObjectId()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": run_id,
                "user_id": "admin-id",
                "candidates": [
                    {
                        "code": "600000",
                        "name": "失效候选",
                        "actionability": "invalidated",
                        "can_add_to_favorites": False,
                    }
                ],
            }
        )
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(
            get_favorite_codes=AsyncMock(return_value=set())
        ),
        quotes=SimpleNamespace(),
    )
    service.db = db

    with pytest.raises(InvalidAICandidateSelectionError):
        await service.add_to_favorites("admin-id", str(run_id), ["600000"])


@pytest.mark.asyncio
async def test_start_run_returns_background_job_without_waiting(monkeypatch):
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=None),
        insert_one=AsyncMock(),
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )
    service.db = db

    class FakeTask:
        def add_done_callback(self, _callback):
            return None

    def fake_create_task(coro):
        coro.close()
        return FakeTask()

    monkeypatch.setattr(
        "app.services.ai_candidate_service.asyncio.create_task",
        fake_create_task,
    )

    result = await service.start_run("admin-id", max_candidates=5)

    assert result["status"] == "queued"
    assert result["job_id"]
    collection.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_performance_summary_aggregates_tracked_candidate_results():
    class FakeCursor:
        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        async def to_list(self, *, length):
            assert length == 30
            return [
                {
                    "_id": ObjectId(),
                    "generated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
                    "candidates": [
                        {
                            "code": "600000",
                            "name": "候选",
                            "performance": {
                                "return_since_generated_pct": 5.0,
                                "max_return_pct": 6.0,
                                "min_return_pct": -1.0,
                                "target_hit_at": "2026-07-21T10:00:00Z",
                                "observation_count": 3,
                            },
                        },
                        {
                            "code": "600001",
                            "name": "候选二",
                            "performance": {
                                "return_since_generated_pct": -1.0,
                                "stop_hit_at": "2026-07-21T11:00:00Z",
                                "observation_count": 2,
                            },
                        },
                        {
                            "code": "600002",
                            "name": "新版影子交易",
                            "performance": {
                                "observation_count": 4,
                                "shadow_trade": {
                                    "status": "closed_target",
                                    "entry_price": 10.0,
                                    "quantity": 100,
                                    "net_return_pct": 8.5,
                                    "net_pnl": 85.0,
                                    "max_return_pct": 10.0,
                                    "min_return_pct": -1.0,
                                },
                            },
                        },
                    ],
                }
            ]

    collection = SimpleNamespace(find=MagicMock(return_value=FakeCursor()))
    db = MagicMock()
    db.__getitem__.return_value = collection
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )
    service.db = db

    result = await service.performance_summary("admin-id")

    assert result["sample_count"] == 1
    assert result["legacy_baseline_count"] == 2
    assert result["total_item_count"] == 3
    assert result["triggered_count"] == 1
    assert result["closed_count"] == 1
    assert result["average_return_pct"] == 8.5
    assert result["positive_count"] == 1
    assert result["target_hit_count"] == 1
    assert result["stop_hit_count"] == 0
    legacy = [
        item
        for item in result["items"]
        if item["metric_basis"] == "legacy_generated_baseline"
    ]
    assert all(item["entry_triggered"] is False for item in legacy)


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


@pytest.mark.asyncio
async def test_favorite_tracking_update_preserves_user_fields():
    collection = SimpleNamespace(
        update_one=AsyncMock(
            return_value=SimpleNamespace(modified_count=1)
        )
    )
    db = SimpleNamespace(user_favorites=collection)
    service = FavoritesService()
    service.db = db

    updated = await service.update_ai_candidate_tracking(
        "admin-id",
        "600000",
        {
            "run_id": "run-1",
            "generated_at": "2026-07-20T00:00:00Z",
            "reference_price": 10.2,
            "actionability": "ready_now",
            "actionability_label": "价格条件已满足",
            "price_plan": {"entry_price": 10.0, "stop_price": 9.2},
        },
    )

    assert updated is True
    update = collection.update_one.await_args.args[1]["$set"]
    assert "favorites.$.notes" not in update
    assert "favorites.$.tags" not in update
    assert update["favorites.$.ai_metadata"]["actionability"] == "ready_now"


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


def test_portfolio_gate_overrides_price_ready_state():
    candidate = {
        "price_plan": {"entry_status": "price_ready"},
        "portfolio_gate": {
            "blocked": True,
            "reason_code": "market_regime_new_exposure_blocked",
        },
    }

    _apply_candidate_state(candidate)

    assert candidate["actionability"] == "blocked"
    assert candidate["can_add_to_favorites"] is False


def test_performance_counts_each_tencent_trade_timestamp_once():
    candidate = {
        "initial_reference_price": 10.0,
        "price_plan": {"stop_price": 9.0, "target_price": 12.0},
    }

    AICandidateService._update_performance(
        candidate,
        current_price=10.5,
        checked_at="2026-07-21T02:00:00+00:00",
        observation_key="2026-07-21T10:00:00+08:00",
    )
    AICandidateService._update_performance(
        candidate,
        current_price=10.5,
        checked_at="2026-07-21T02:05:00+00:00",
        observation_key="2026-07-21T10:00:00+08:00",
    )

    assert candidate["performance"]["observation_count"] == 1
