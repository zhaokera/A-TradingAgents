from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

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
    merge_rolling_candidate_pool,
)
from app.services.favorites_service import FavoritesService
from app.services.holdings_cli import CLIError


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


def _live_market_status(*, level: str = "green") -> dict:
    local_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return {
        "ok": True,
        "data": {
            "market_session": {
                "session": "trading",
                "is_trading_hours": True,
                "local_time": f"{local_date}T10:00:00+08:00",
            },
            "market_gate": {
                "status": "ok",
                "level": level,
                "trade_date": local_date,
                "benchmark_trade_date": local_date,
                "new_position_allowed": level != "red",
                "max_new_exposure_multiplier": (
                    1.0 if level == "green" else 0.5 if level == "yellow" else 0.0
                ),
                "breadth_regime": {
                    "status": "ok",
                    "level": level,
                    "source": "akshare.sina.stock_zh_a_spot",
                    "total_coverage_ratio": 0.997,
                },
            },
        },
        "meta": {
            "source": "tencent_major_indices+akshare_sina_public_breadth",
            "generated_at": f"{local_date}T02:00:00Z",
        },
    }


def _research_payload():
    return {
        "ok": True,
        "data": {
            "candidate_discovery": {
                "status": "ok",
                "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
                "benchmark_trade_date": "2026-07-17",
                "checked_at": "2026-07-17T07:00:00+00:00",
                "freshness": "fresh",
                "degraded": False,
                "provider_errors": [],
                "stage_sources": {
                    "public_snapshot": {
                        "provider": "akshare.sina.stock_zh_a_spot",
                        "status": "ok",
                    },
                    "tencent_verification": {
                        "provider": "tencent_batch_quotes",
                        "status": "ok",
                    },
                },
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
    assert first["research_status"] == "watch_trigger"
    assert first["watch_trigger_ready"] is True
    assert first["research_condition_ready"] is False
    assert first["condition_order_ready"] is False
    assert first["execution_status"] == "price_alert_manual_confirmation"
    assert "可设置条件单" not in str(first)
    assert first["execution_actionable"] is False
    assert first["execution_status"] == "price_alert_manual_confirmation"
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
    assert result["discovery"] == {
        "status": "ok",
        "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
        "trade_date": "2026-07-17",
        "benchmark_trade_date": "2026-07-17",
        "checked_at": "2026-07-17T07:00:00+00:00",
        "freshness": "fresh",
        "degraded": False,
        "cache_age_seconds": None,
        "attempt_count": None,
        "provider_health": None,
        "provider_errors": [],
        "stage_sources": {
            "public_snapshot": {
                "provider": "akshare.sina.stock_zh_a_spot",
                "status": "ok",
            },
            "tencent_verification": {
                "provider": "tencent_batch_quotes",
                "status": "ok",
            },
        },
        "universe_count": 5527,
        "eligible_count": 2134,
        "selected_count": 2,
        "technical_passed_count": 2,
        "earnings_selected_count": 2,
        "total_coverage_ratio": 1.0,
        "permission_prefilter_excluded_count": 0,
        "permission_prefilter_excluded": [],
    }


def test_normalize_ai_candidate_run_preserves_degraded_discovery_audit():
    payload = _research_payload()
    discovery = payload["data"]["candidate_discovery"]
    discovery.update(
        {
            "source": "mongo.candidate_market_snapshots+tencent_batch_quotes",
            "checked_at": "2026-07-17T06:55:00+00:00",
            "freshness": "cached_fresh",
            "degraded": True,
            "cache_age_seconds": 300.0,
            "attempt_count": 2,
            "provider_errors": [
                {
                    "provider": "akshare.sina.stock_zh_a_spot",
                    "status": "public_breadth_fetch_failed",
                    "error_type": "RemoteDisconnected",
                    "checked_at": "2026-07-17T07:00:00+00:00",
                }
            ],
        }
    )

    result = normalize_ai_candidate_run(
        payload,
        max_candidates=5,
        favorite_codes=set(),
    )

    audit = result["discovery"]
    assert audit["status"] == "ok"
    assert audit["source"].startswith("mongo.candidate_market_snapshots")
    assert audit["trade_date"] == "2026-07-17"
    assert audit["freshness"] == "cached_fresh"
    assert audit["degraded"] is True
    assert audit["cache_age_seconds"] == 300.0
    assert audit["attempt_count"] == 2
    assert audit["provider_errors"][0]["error_type"] == "RemoteDisconnected"


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


def test_normalize_ai_candidate_run_does_not_truncate_rolling_pool_to_display_limit():
    payload = _research_payload()
    payload["data"]["candidates"] = [
        {
            "code": f"{600000 + index:06d}",
            "name": f"候选{index}",
            "priority": index + 1,
            "quote": {"price": 10.0 + index / 100},
            "guarded_price_plan": {"status": "history_unavailable"},
            "risk_flags": [],
            "research_tier": "deep" if index < 15 else "structured",
        }
        for index in range(100)
    ]

    result = normalize_ai_candidate_run(
        payload,
        max_candidates=5,
        favorite_codes=set(),
    )

    assert result["candidate_count"] == 100
    assert len(result["candidates"]) == 100
    assert result["rolling_pool"] == {
        "capacity": 100,
        "current_count": 100,
        "aging_count": 0,
        "expired_count": 0,
        "invalidated_count": 0,
        "deep_count": 15,
        "structured_count": 85,
    }


def test_merge_rolling_pool_preserves_unseen_candidates_as_aging():
    current = [
        {"code": "600001", "rank_score": 90.0, "rolling_pool_state": "current"}
    ]
    previous = [
        {
            "code": "600001",
            "rank_score": 80.0,
            "first_seen_at": "2026-08-20T01:40:00+00:00",
            "last_seen_at": "2026-08-21T01:40:00+00:00",
            "rolling_pool_state": "current",
        },
        {
            "code": "600002",
            "rank_score": 70.0,
            "first_seen_at": "2026-08-20T01:40:00+00:00",
            "last_seen_at": "2026-08-21T01:40:00+00:00",
            "rolling_pool_state": "current",
        },
    ]

    active, retired = merge_rolling_candidate_pool(
        current,
        previous,
        generated_at=datetime.fromisoformat("2026-08-24T01:40:00+00:00"),
    )

    assert [item["code"] for item in active] == ["600001", "600002"]
    assert active[0]["rolling_pool_state"] == "current"
    assert active[0]["first_seen_at"] == "2026-08-20T01:40:00+00:00"
    assert active[1]["rolling_pool_state"] == "aging"
    assert active[1]["rolling_age_trading_days"] == 2
    assert active[1]["execution_actionable"] is False
    assert retired == []


def test_merge_rolling_pool_expires_after_ten_trading_days_and_keeps_audit():
    previous = [
        {
            "code": "600002",
            "rank_score": 70.0,
            "first_seen_at": "2026-08-10T01:40:00+00:00",
            "last_seen_at": "2026-08-10T01:40:00+00:00",
            "rolling_pool_state": "aging",
        }
    ]

    active, retired = merge_rolling_candidate_pool(
        [],
        previous,
        generated_at=datetime.fromisoformat("2026-08-24T01:40:00+00:00"),
    )

    assert active == []
    assert retired[0]["code"] == "600002"
    assert retired[0]["rolling_pool_state"] == "expired"
    assert retired[0]["rolling_age_trading_days"] == 10
    assert retired[0]["execution_actionable"] is False


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


def test_pullback_price_in_entry_zone_waits_for_reversal_confirmation():
    candidate = normalize_ai_candidate(
        {
            "code": "600360",
            "name": "华微电子",
            "quote": {"price": 9.9},
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
    assert candidate["price_plan"]["price_condition_met"] is True
    assert candidate["price_plan"]["entry_status"] == (
        "waiting_pullback_confirmation"
    )
    assert candidate["actionability"] == "watch_trigger"
    assert candidate["execution_actionable"] is False


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
    collection = SimpleNamespace(
        insert_one=AsyncMock(),
        update_one=AsyncMock(),
    )
    db = MagicMock()
    db.__getitem__.return_value = collection
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value={"600001"}),
        sync_auto_ai_candidates=AsyncMock(
            return_value={
                "selected_codes": ["600001"],
                "added_codes": [],
                "condition_orders_created": 0,
            }
        ),
    )
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=favorites,
    )
    service.db = db

    result = await service.run("admin-id", max_candidates=1)

    assert result["run_id"]
    assert result["user_id"] == "admin-id"
    assert result["candidate_count"] == 2
    assert [item["code"] for item in result["candidates"]] == [
        "600001",
        "600000",
    ]
    stored = collection.insert_one.await_args.args[0]
    assert stored["user_id"] == "admin-id"
    assert stored["expires_at"] > stored["generated_at"]
    assert stored["candidate_discovery"] == stored["discovery"]
    assert result["candidate_discovery"] == result["discovery"]
    favorites.sync_auto_ai_candidates.assert_awaited_once()
    assert result["auto_favorites"]["selected_codes"] == ["600001"]


@pytest.mark.asyncio
async def test_missing_stock_profile_remains_a_nonblocking_visible_warning():
    stock_master = SimpleNamespace(
        db=None,
        resolve_many=AsyncMock(return_value={}),
    )
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
        stock_master=stock_master,
    )
    service.db = {}
    candidate = {
        "code": "000969",
        "name": "安泰科技",
        "priority": 1,
        "can_add_to_favorites": True,
        "actionability": "watch_trigger",
        "risk_flags": [],
        "price_plan": {
            "entry_price": 15.9,
            "stop_price": 15.13,
            "target_price": 21.52,
        },
        "position_sizing": {"status": "sized"},
    }
    document = {"candidates": [candidate], "objective": {}}

    await service._apply_objective_profiles(document)

    assert candidate["can_add_to_favorites"] is True
    assert candidate["actionability"] == "watch_trigger"
    assert candidate["profile_evidence"] == {
        "status": "missing",
        "confidence": "missing",
        "complete": False,
        "decision_critical_complete": False,
        "missing_fields": ["industry", "main_business"],
        "warning_code": "stock_profile_evidence_incomplete",
        "message": "行业或板块证据不完整，当前候选仅作价格提醒，不提升为可执行结论。",
        "noncritical_missing_fields": [],
    }
    assert candidate["risk_flags"][-1] == {
        "code": "stock_profile_evidence_incomplete",
        "severity": "warning",
        "message": "行业或板块证据不完整，当前候选仅作价格提醒，不提升为可执行结论。",
    }


@pytest.mark.asyncio
async def test_latest_reconciles_favorite_status_with_current_favorites():
    failed_job_id = ObjectId()
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
    jobs = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": failed_job_id,
                "user_id": "admin-id",
                "status": "failed",
                "created_at": datetime.now(timezone.utc),
                "completed_at": datetime.now(timezone.utc),
                "progress": {"stage": "failed", "percent": 100},
                "error": {
                    "code": "candidate_discovery_unavailable",
                    "message": "公开全市场候选发现不可用",
                    "stage": "candidate_discovery",
                },
            }
        )
    )
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: (
        jobs if name == "ai_candidate_jobs" else collection
    )
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
        market_status_loader=AsyncMock(return_value=_live_market_status()),
    )
    service.db = db

    result = await service.latest("admin-id", refresh_quotes=False)

    assert result is not None
    assert [item["favorite_status"] for item in result["candidates"]] == [
        "not_added",
        "in_favorites",
    ]
    assert result["candidate_research"] == {
        "job_id": str(failed_job_id),
        "status": "failed",
        "stage": "candidate_discovery",
        "error": {
            "code": "candidate_discovery_unavailable",
            "message": "公开全市场候选发现不可用",
            "stage": "candidate_discovery",
        },
        "created_at": result["candidate_research"]["created_at"],
        "started_at": None,
        "completed_at": result["candidate_research"]["completed_at"],
        "run_id": None,
        "is_today": True,
        "current_scan_available": False,
        "serving_mode": "stale_completed_run_fallback",
    }
    assert result["current_scan_available"] is False
    assert result["serving_mode"] == "stale_completed_run_fallback"


@pytest.mark.asyncio
async def test_latest_without_completed_run_exposes_running_research_job():
    job_id = ObjectId()
    runs = SimpleNamespace(find_one=AsyncMock(return_value=None))
    jobs = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": job_id,
                "user_id": "admin-id",
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
                "progress": {"stage": "technical_deep_check", "percent": 45},
            }
        )
    )
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: {
        "ai_candidate_runs": runs,
        "ai_candidate_jobs": jobs,
    }[name]
    stock_master, macro_risk = _offline_research_dependencies()
    service = AICandidateService(
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
        stock_master=stock_master,
        macro_risk=macro_risk,
    )
    service.db = db

    result = await service.latest("admin-id", refresh_quotes=False)

    assert result["status"] == "candidate_research_running"
    assert result["candidates"] == []
    assert result["candidate_research"]["job_id"] == str(job_id)
    assert result["candidate_research"]["stage"] == "technical_deep_check"
    assert result["candidate_research"]["current_scan_available"] is False


@pytest.mark.asyncio
async def test_latest_refreshes_tencent_quote_and_candidate_lifecycle():
    run_id = ObjectId()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": run_id,
                "user_id": "admin-id",
                "generated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
                "market": {
                    "session": "closed",
                    "is_trading_hours": False,
                    "local_time": "2026-07-20T14:00:00+08:00",
                    "domestic_regime": "red",
                    "regime": "red",
                },
                "candidates": [
                    {
                        "code": "600000",
                        "name": "候选",
                        "reference_price": 10.6,
                        "quote": {
                            "price": 10.6,
                            "source": "tencent",
                            "trade_at": "2026-07-21T09:59:00+08:00",
                            "volume": 1000,
                            "amount": 10600.0,
                            "event_confirmation_required": True,
                            "event_observed_at": None,
                        },
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
                    "volume": 1200,
                    "amount": 11880.0,
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
        market_status_loader=AsyncMock(return_value=_live_market_status()),
    )
    service.db = db

    result = await service.latest("admin-id")

    assert result is not None
    candidate = result["candidates"][0]
    assert candidate["reference_price"] == 9.9
    assert candidate["price_plan"]["entry_status"] == (
        "waiting_pullback_confirmation"
    )
    assert candidate["actionability"] == "watch_trigger"
    assert candidate["quote_source"] == "tencent"
    assert {
        key: value
        for key, value in candidate["quote"].items()
        if key != "freshness"
    } == {
        "price": 9.9,
        "source": "tencent",
        "trade_at": "2026-07-21T10:00:00+08:00",
        "provider_updated_at": None,
        "quote_time_semantics": None,
        "exchange_trade_time_verified": False,
        "quote_checked_at": candidate["quote_checked_at"],
        "volume": 1200.0,
        "amount": 11880.0,
        "event_confirmation_required": True,
        "event_change_detected": True,
        "event_observed_at": candidate["quote_checked_at"],
    }
    assert candidate["quote"]["freshness"]["display_usable"] is True
    assert candidate["quote"]["freshness"]["tracking_usable"] is False
    assert candidate["condition_order_ready"] is False
    assert candidate["execution_actionable"] is False
    assert result["market"]["execution_usable"] is False
    assert result["market"]["execution_status"] == (
        "research_snapshot_not_execution_decision"
    )
    assert result["market"]["reason_code"] == "daily_decision_required"
    assert result["market"]["discovery_snapshot"]["local_time"] == (
        "2026-07-20T14:00:00+08:00"
    )
    assert result["market"]["discovery_snapshot"]["domestic_regime"] == "red"
    assert result["market"]["domestic_regime"] == "green"
    assert result["market"]["regime"] == "green"
    assert result["market"]["live_gate"]["usable"] is True
    assert result["market"]["live_gate"]["source"] == (
        "tencent_major_indices+akshare_sina_public_breadth"
    )
    assert result["market"]["live_gate"]["market_gate"]["breadth_regime"][
        "total_coverage_ratio"
    ] == 0.997
    assert "performance" not in candidate
    collection.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_pullback_requires_a_later_rising_event_before_price_ready():
    class AlwaysFreshSessionPolicy:
        async def classify(self, *, now=None):
            return {
                "phase": "live_am",
                "classified_at": now.isoformat(),
            }

        async def quote_status(self, quote, *, now=None, session=None):
            return {
                "actionable": True,
                "display_usable": True,
                "tracking_usable": True,
                "status": "fresh",
            }

    quotes = SimpleNamespace(
        get_quotes=AsyncMock(
            side_effect=[
                {
                    "600360": {
                        "price": 10.28,
                        "source": "tencent",
                        "trade_at": "2026-08-26T09:38:00+08:00",
                        "volume": 1_000,
                        "amount": 10_280,
                    }
                },
                {
                    "600360": {
                        "price": 10.32,
                        "source": "tencent",
                        "trade_at": "2026-08-26T09:39:00+08:00",
                        "volume": 1_200,
                        "amount": 12_384,
                    }
                },
            ]
        )
    )
    favorites = SimpleNamespace(
        get_favorite_codes=AsyncMock(return_value=set()),
        update_ai_candidate_tracking=AsyncMock(return_value=True),
    )
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=favorites,
        quotes=quotes,
        market_session=AlwaysFreshSessionPolicy(),
        market_status_loader=AsyncMock(return_value=_live_market_status()),
    )
    service._candidate_governance = AsyncMock(
        return_value={"excluded_codes": [], "market_permissions": {}}
    )
    service._apply_objective_profiles = AsyncMock()
    service._apply_live_market_gate = AsyncMock()
    service._apply_macro_policy = AsyncMock()
    service._apply_account_policy = AsyncMock()
    document = {
        "_id": ObjectId(),
        "user_id": "admin-id",
        "generated_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
        "candidates": [
            {
                "code": "600360",
                "name": "华微电子",
                "reference_price": 10.4,
                "quote": {
                    "price": 10.4,
                    "source": "tencent",
                    "trade_at": "2026-08-26T09:37:00+08:00",
                    "volume": 800,
                    "amount": 8_320,
                },
                "price_plan": {
                    "entry_strategy": "pullback",
                    "entry_price": 10.31,
                    "stop_price": 9.95,
                    "target_price": 11.27,
                    "status": "ok",
                },
                "risk_flags": [],
            }
        ],
    }

    falling = await service._refresh_document(
        deepcopy(document),
        user_id="admin-id",
        persist=False,
        notify=False,
    )
    rising = await service._refresh_document(
        deepcopy(falling),
        user_id="admin-id",
        persist=False,
        notify=False,
    )

    falling_candidate = falling["candidates"][0]
    assert falling_candidate["price_plan"]["entry_status"] == (
        "waiting_pullback_confirmation"
    )
    assert falling_candidate["actionability"] == "watch_trigger"
    assert falling_candidate["price_plan"]["entry_confirmation"]["status"] == (
        "waiting_reversal"
    )

    rising_candidate = rising["candidates"][0]
    confirmation = rising_candidate["price_plan"]["entry_confirmation"]
    assert confirmation["status"] == "confirmed"
    assert confirmation["independent_event_confirmed"] is True
    assert confirmation["previous_price"] == 10.28
    assert confirmation["confirmation_price"] == 10.32
    assert rising_candidate["price_plan"]["entry_status"] == "price_ready"
    assert rising_candidate["actionability"] == "ready_now"


@pytest.mark.asyncio
async def test_live_market_gate_fails_closed_on_intraday_trade_date_mismatch():
    stale_payload = _live_market_status()
    stale_payload["data"]["market_gate"]["trade_date"] = "2026-07-28"
    stale_payload["data"]["market_gate"]["benchmark_trade_date"] = "2026-07-28"
    service = AICandidateService(
        research_runner=_research_payload,
        market_status_loader=AsyncMock(return_value=stale_payload),
    )
    document = {"market": {"domestic_regime": "green"}}

    await service._apply_live_market_gate(
        document,
        checked_at=datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc),
    )

    assert document["market"]["domestic_regime"] == "red"
    assert document["market"]["domestic_regime_source"] == (
        "live_market_gate_unavailable_fail_closed"
    )
    assert document["market"]["live_gate"]["usable"] is False
    assert document["market"]["live_gate"]["fail_closed"] is True
    assert document["market"]["live_gate"]["provider_errors"][0]["reason"] == (
        "live_trade_date_mismatch"
    )


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
async def test_add_to_favorites_rejects_governance_excluded_candidate():
    run_id = ObjectId()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "_id": run_id,
                "user_id": "admin-id",
                "candidates": [
                    {
                        "code": "688208",
                        "name": "无权限科创板",
                        "actionability": "ready_now",
                        "can_add_to_favorites": True,
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
    service._candidate_governance = AsyncMock(
        return_value={
            "excluded_codes": ["600406"],
            "star_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        }
    )

    with pytest.raises(InvalidAICandidateSelectionError):
        await service.add_to_favorites("admin-id", str(run_id), ["688208"])


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
    assert result["max_candidates"] == 100
    assert collection.insert_one.await_args.args[0]["max_candidates"] == 100
    collection.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_job_keeps_discovery_stage_from_error_details():
    job_id = ObjectId()
    jobs = SimpleNamespace(update_one=AsyncMock())
    db = MagicMock()
    db.__getitem__.return_value = jobs

    def fail_research():
        raise CLIError(
            "公开全市场候选发现不可用",
            code="candidate_discovery_unavailable",
            exit_code=4,
            details={
                "stage": "candidate_discovery",
                "candidate_discovery": {
                    "status": "candidate_discovery_unavailable",
                    "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
                    "benchmark_trade_date": "2026-08-13",
                    "provider_expected_count": 5542,
                    "universe_count": 5539,
                    "total_coverage_ratio": 5539 / 5542,
                    "rejection_counts": {"future_trade_at": 1},
                    "quality_counts": {},
                    "provider_errors": [],
                    "stage_sources": {
                        "public_snapshot": {
                            "provider": "akshare.sina.stock_zh_a_spot",
                            "status": "ok",
                        },
                        "tencent_verification": {
                            "provider": "tencent_batch_quotes",
                            "status": "ok",
                        },
                    },
                    "permission_prefilter_excluded_count": 2349,
                    "permission_prefilter_excluded": [
                        {"code": "300001", "reason_code": "permission_denied"}
                    ],
                },
            },
        )

    service = AICandidateService(
        research_runner=fail_research,
        favorites=SimpleNamespace(
            get_favorite_codes=AsyncMock(return_value=set())
        ),
        quotes=SimpleNamespace(),
    )
    service.db = db
    service._candidate_governance = AsyncMock(
        return_value={
            "excluded_codes": [],
            "star_market": {"verified": True, "tradable": True},
        }
    )

    await service._execute_job(
        job_id=job_id,
        user_id="admin-id",
        max_candidates=5,
    )

    failed_update = jobs.update_one.await_args_list[-1].args[1]["$set"]
    assert failed_update["status"] == "failed"
    expected_diagnostic = {
        "stage": "candidate_discovery",
        "candidate_discovery": {
            "status": "candidate_discovery_unavailable",
            "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
            "benchmark_trade_date": "2026-08-13",
            "provider_expected_count": 5542,
            "universe_count": 5539,
            "total_coverage_ratio": 5539 / 5542,
            "rejection_counts": {"future_trade_at": 1},
            "quality_counts": {},
            "provider_errors": [],
            "stage_sources": {
                "public_snapshot": {
                    "provider": "akshare.sina.stock_zh_a_spot",
                    "status": "ok",
                },
                "tencent_verification": {
                    "provider": "tencent_batch_quotes",
                    "status": "ok",
                },
            },
            "permission_prefilter_excluded_count": 2349,
        },
    }
    assert failed_update["attempts"][0]["diagnostic"] == expected_diagnostic
    assert failed_update["error"] == {
        "code": "candidate_discovery_unavailable",
        "message": "公开全市场候选发现不可用",
        "stage": "candidate_discovery",
        "attempt_count": 1,
        "attempts": [
            {
                "attempt": 1,
                "code": "candidate_discovery_unavailable",
                "stage": "candidate_discovery",
                "diagnostic": expected_diagnostic,
            }
        ],
        "diagnostic": expected_diagnostic,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retryable_code",
    ["technical_deep_check_timeout", "TechnicalHistoryFetchError"],
)
async def test_background_job_retries_transient_technical_failure_once(
    monkeypatch,
    retryable_code,
):
    job_id = ObjectId()
    jobs = SimpleNamespace(update_one=AsyncMock())
    db = MagicMock()
    db.__getitem__.return_value = jobs
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )
    service.db = db
    service.run = AsyncMock(
        side_effect=[
            CLIError(
                "公开候选技术初筛超时",
                code=retryable_code,
                exit_code=4,
                stage="technical_deep_check",
            ),
            {"run_id": "run-after-cache"},
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(
        "app.services.ai_candidate_service.asyncio.sleep",
        sleep,
    )

    await service._execute_job(
        job_id=job_id,
        user_id="admin-id",
        max_candidates=5,
    )

    assert service.run.await_count == 2
    sleep.assert_awaited_once()
    completed = jobs.update_one.await_args_list[-1].args[1]["$set"]
    assert completed["status"] == "completed"
    assert completed["run_id"] == "run-after-cache"
    assert completed["attempt_count"] == 2
    assert completed["attempts"] == [
        {
            "attempt": 1,
            "code": retryable_code,
            "stage": "technical_deep_check",
        }
    ]


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
                                    "tracking_version": "shadow_trade_v2",
                                    "status": "closed_target",
                                    "entry_observation_key": "2026-07-21T09:30:00+08:00",
                                    "closed_observation_key": "2026-07-22T10:00:00+08:00",
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
    assert formatted["quote_provider_updated_at"] is None
    assert formatted["quote_time_semantics"] is None
    assert formatted["exchange_trade_time_verified"] is False


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
            "provider_updated_at": "2026-07-20T10:00:00+08:00",
            "quote_time_semantics": "provider_snapshot_updated_at",
            "exchange_trade_time_verified": False,
            "price_plan": {"entry_price": 10.0, "stop_price": 9.2},
        },
    )

    assert updated is True
    update = collection.update_one.await_args.args[1]["$set"]
    assert "favorites.$.notes" not in update
    assert "favorites.$.tags" not in update
    assert update["favorites.$.ai_metadata"]["actionability"] == "ready_now"
    assert update["favorites.$.ai_metadata"]["quote_provider_updated_at"] == (
        "2026-07-20T10:00:00+08:00"
    )
    assert update["favorites.$.ai_metadata"]["quote_time_semantics"] == (
        "provider_snapshot_updated_at"
    )
    assert (
        update["favorites.$.ai_metadata"]["exchange_trade_time_verified"]
        is False
    )


def test_serialize_run_marks_mongo_naive_datetime_as_utc():
    serialized = AICandidateService._serialize_run(
        {
            "_id": ObjectId(),
            "generated_at": datetime(2026, 7, 20, 7, 12, 38),
        }
    )

    assert serialized["generated_at"] == "2026-07-20T07:12:38+00:00"


def test_serialize_run_exposes_only_compact_permission_prefilter_audit():
    serialized = AICandidateService._serialize_run(
        {
            "_id": ObjectId(),
            "governance": {
                "excluded_codes": ["600406"],
                "star_market": {
                    "verified": True,
                    "tradable": False,
                    "eligible": False,
                },
            },
            "governance_excluded_candidates": [
                {
                    "code": "688208",
                    "name": "道通科技",
                    "governance_reason": "star_market_permission_unverified",
                    "price_plan": {"entry_price": 31.2},
                    "performance": {
                        "shadow_trade": {"status": "stopped_governance"}
                    },
                },
                {
                    "code": "600406",
                    "name": "国电南瑞",
                    "governance_reason": "user_excluded",
                    "quote": {"price": 24.5},
                },
            ],
        }
    )

    assert "governance_excluded_candidates" not in serialized
    assert serialized["permission_prefilter_excluded_count"] == 2
    assert serialized["permission_prefilter_excluded"] == [
        {
            "code": "688208",
            "name": "道通科技",
            "board": "STAR",
            "reason_code": "star_market_permission_denied",
        },
        {
            "code": "600406",
            "name": "国电南瑞",
            "board": "A_SHARE",
            "reason_code": "user_excluded",
        },
    ]


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


def test_serialize_run_exposes_structured_profile_provenance():
    serialized = AICandidateService._serialize_run(
        {
            "_id": ObjectId(),
            "candidates": [
                {
                    "code": "600360",
                    "name": "华微电子",
                    "research_tier": "structured",
                    "reason_summary": (
                        "回调结构候选，等待观察区间企稳后再评估。"
                    ),
                    "stock_profile": {
                        "code": "600360",
                        "status": "deferred_structured_layer",
                        "confidence": "deferred",
                        "industry": None,
                        "main_business": None,
                    },
                    "profile_evidence": {
                        "status": "deferred_structured_layer",
                        "confidence": "deferred",
                        "decision_critical_complete": False,
                    },
                }
            ],
        }
    )

    candidate = serialized["candidates"][0]
    assert candidate["candidate_source_profile"]["status"] == (
        "deferred_structured_layer"
    )
    assert candidate["resolved_profile"]["status"] == (
        "not_resolved_structured_layer"
    )
    assert candidate["profile_contract"] == {
        "scope": "candidate_latest",
        "discovery_research_tier": "structured",
        "formal_research_selected": False,
        "candidate_status": "deferred_structured_layer",
        "candidate_confidence": "deferred",
        "candidate_decision_critical_complete": False,
        "resolved_status": "not_resolved_structured_layer",
        "resolved_confidence": "deferred",
        "resolved_decision_critical_complete": False,
        "eligible_for_buy_now": False,
    }
    assert candidate["candidate_reason_summary"] == (
        "回调结构候选，等待观察区间企稳后再评估。"
    )


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


def test_shadow_trade_never_enters_and_exits_on_the_same_observation():
    candidate = {
        "initial_reference_price": 18.0,
        "price_plan": {
            "entry_strategy": "pullback",
            "entry_price": 17.06,
            "stop_price": 16.52,
            "target_price": 23.75,
            "entry_confirmation": {
                "status": "confirmed",
                "independent_event_confirmed": True,
                "confirmation_observation_key": (
                    "2026-08-06T09:20:05+08:00"
                ),
            },
        },
        "portfolio_allocation": {"quantity": 100},
    }

    AICandidateService._update_performance(
        candidate,
        current_price=17.0,
        checked_at="2026-08-06T01:20:05+00:00",
        observation_key="2026-08-06T09:20:05+08:00",
        session_high=18.5,
        session_low=16.0,
    )

    shadow = candidate["performance"]["shadow_trade"]
    assert shadow["status"] == "active"
    assert shadow["tracking_version"] == "shadow_trade_v2"
    assert shadow["entry_observation_key"] == "2026-08-06T09:20:05+08:00"
    assert "closed_at" not in shadow

    AICandidateService._update_performance(
        candidate,
        current_price=16.5,
        checked_at="2026-08-06T01:21:05+00:00",
        observation_key="2026-08-06T09:21:05+08:00",
        session_high=18.5,
        session_low=16.0,
    )

    shadow = candidate["performance"]["shadow_trade"]
    assert shadow["status"] == "closed_stop"
    assert shadow["closed_observation_key"] == "2026-08-06T09:21:05+08:00"


def test_shadow_pullback_waits_for_confirmed_current_reversal_event():
    candidate = {
        "initial_reference_price": 18.0,
        "price_plan": {
            "entry_strategy": "pullback",
            "entry_price": 17.06,
            "stop_price": 16.52,
            "target_price": 23.75,
            "entry_confirmation": {
                "status": "waiting_reversal",
                "independent_event_confirmed": False,
            },
        },
        "portfolio_allocation": {"quantity": 100},
    }

    AICandidateService._update_performance(
        candidate,
        current_price=17.0,
        checked_at="2026-08-06T01:20:05+00:00",
        observation_key="2026-08-06T09:20:05+08:00",
    )

    assert candidate["performance"]["shadow_trade"]["status"] == (
        "waiting_entry"
    )

    candidate["price_plan"]["entry_confirmation"] = {
        "status": "confirmed",
        "independent_event_confirmed": True,
        "confirmation_observation_key": "2026-08-06T09:21:05+08:00",
    }
    AICandidateService._update_performance(
        candidate,
        current_price=17.02,
        checked_at="2026-08-06T01:21:05+00:00",
        observation_key="2026-08-06T09:21:05+08:00",
    )

    shadow = candidate["performance"]["shadow_trade"]
    assert shadow["status"] == "active"
    assert shadow["entry_observation_key"] == (
        "2026-08-06T09:21:05+08:00"
    )


@pytest.mark.asyncio
async def test_research_entry_receives_user_and_star_market_prefilters():
    captured = {}

    def runner(*, excluded_code_reasons, star_market_exclusion_reason):
        captured["excluded_code_reasons"] = excluded_code_reasons
        captured["star_market_exclusion_reason"] = star_market_exclusion_reason
        return _research_payload()

    service = AICandidateService(
        research_runner=runner,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )

    payload = await service._run_research(
        {
            "excluded_codes": ["600406"],
            "star_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        }
    )

    assert payload["ok"] is True
    assert captured == {
        "excluded_code_reasons": {"600406": "user_excluded"},
        "star_market_exclusion_reason": "star_market_permission_denied",
    }


@pytest.mark.asyncio
async def test_research_entry_receives_all_restricted_board_prefilters():
    captured = {}

    def runner(*, excluded_code_reasons, board_exclusion_reasons):
        captured["excluded_code_reasons"] = excluded_code_reasons
        captured["board_exclusion_reasons"] = board_exclusion_reasons
        return _research_payload()

    service = AICandidateService(
        research_runner=runner,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )

    payload = await service._run_research(
        {
            "excluded_codes": ["600406"],
            "market_permissions": {
                "star_market": {
                    "verified": True,
                    "tradable": False,
                },
                "beijing_stock_exchange": {
                    "verified": False,
                    "tradable": False,
                },
                "chi_next_market": {
                    "verified": True,
                    "tradable": False,
                },
            },
        }
    )

    assert payload["ok"] is True
    assert captured == {
        "excluded_code_reasons": {"600406": "user_excluded"},
        "board_exclusion_reasons": {
            "STAR": "star_market_permission_denied",
            "BSE": "beijing_stock_exchange_permission_unverified",
            "CHINEXT": "chi_next_market_permission_denied",
        },
    }


def test_governance_stops_old_star_and_user_excluded_shadow_plans():
    document = {
        "candidates": [
            {"code": "688208", "performance": {"shadow_trade": {"status": "active"}}},
            {"code": "600406", "performance": {"shadow_trade": {"status": "active"}}},
            {"code": "000977", "performance": {"shadow_trade": {"status": "active"}}},
        ]
    }

    AICandidateService._apply_candidate_governance(
        document,
        {
            "excluded_codes": ["600406"],
            "star_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        },
    )

    assert [item["code"] for item in document["candidates"]] == ["000977"]
    excluded = {
        item["code"]: item for item in document["governance_excluded_candidates"]
    }
    assert excluded["688208"]["governance_reason"] == (
        "star_market_permission_denied"
    )
    assert excluded["600406"]["governance_reason"] == "user_excluded"
    assert all(
        item["execution_status"] == "governance_excluded"
        for item in excluded.values()
    )
    assert all(
        item["performance"]["shadow_trade"]["status"] == "stopped_governance"
        for item in excluded.values()
    )
    assert all(
        item["performance"]["shadow_trade"]["previous_status"] == "active"
        for item in excluded.values()
    )


@pytest.mark.parametrize(
    ("permission", "expected_reason"),
    [
        (
            {"verified": False, "tradable": False, "eligible": False},
            "beijing_stock_exchange_permission_unverified",
        ),
        (
            {"verified": True, "tradable": False, "eligible": False},
            "beijing_stock_exchange_permission_denied",
        ),
    ],
)
def test_governance_stops_beijing_exchange_shadow_plans(
    permission,
    expected_reason,
):
    document = {
        "candidates": [
            {
                "code": "920493",
                "performance": {"shadow_trade": {"status": "active"}},
            },
            {
                "code": "000977",
                "performance": {"shadow_trade": {"status": "active"}},
            },
        ]
    }

    AICandidateService._apply_candidate_governance(
        document,
        {
            "excluded_codes": [],
            "beijing_stock_exchange": permission,
        },
    )

    assert [item["code"] for item in document["candidates"]] == ["000977"]
    excluded = document["governance_excluded_candidates"]
    assert excluded[0]["code"] == "920493"
    assert excluded[0]["governance_reason"] == expected_reason
    assert excluded[0]["execution_actionable"] is False
    assert excluded[0]["performance"]["shadow_trade"]["status"] == (
        "stopped_governance"
    )


def test_governance_stops_chinext_shadow_plan_and_preserves_audit():
    document = {
        "candidates": [
            {
                "code": "300450",
                "name": "先导智能",
                "performance": {"shadow_trade": {"status": "active"}},
            },
            {
                "code": "002171",
                "performance": {"shadow_trade": {"status": "active"}},
            },
        ]
    }

    AICandidateService._apply_candidate_governance(
        document,
        {
            "excluded_codes": [],
            "chi_next_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        },
    )

    assert [item["code"] for item in document["candidates"]] == ["002171"]
    excluded = document["governance_excluded_candidates"]
    assert excluded[0]["code"] == "300450"
    assert excluded[0]["governance_reason"] == (
        "chi_next_market_permission_denied"
    )
    assert excluded[0]["execution_actionable"] is False
    assert excluded[0]["performance"]["shadow_trade"]["status"] == (
        "stopped_governance"
    )


@pytest.mark.asyncio
async def test_permission_change_reconciles_active_runs_and_auto_favorites():
    generated_at = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)
    run = {
        "_id": ObjectId(),
        "user_id": "owner-1",
        "generated_at": generated_at,
        "expires_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "candidates": [
            {
                "code": "300450",
                "name": "先导智能",
                "performance": {"shadow_trade": {"status": "active"}},
            },
            {
                "code": "002171",
                "name": "楚江新材",
                "actionability": "watch_trigger",
                "can_add_to_favorites": True,
            },
        ],
    }
    cursor = SimpleNamespace(
        sort=lambda *_args, **_kwargs: cursor,
        to_list=AsyncMock(return_value=[deepcopy(run)]),
    )
    runs = SimpleNamespace(
        find=MagicMock(return_value=cursor),
        update_one=AsyncMock(),
    )
    settings = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "user_id": "owner-1",
                "execution_capabilities": {
                    "source": "user_confirmed",
                    "market_permissions": {
                        "chi_next_market": {
                            "verified": True,
                            "tradable": False,
                            "source": "user_confirmed",
                        }
                    },
                },
            }
        )
    )
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: {
        "ai_candidate_runs": runs,
        "user_holding_settings": settings,
    }[name]
    favorites = SimpleNamespace(
        sync_auto_ai_candidates=AsyncMock(
            return_value={
                "removed_codes": ["300450"],
                "selected_codes": ["002171"],
            }
        )
    )
    service = AICandidateService(
        favorites=favorites,
        quotes=SimpleNamespace(),
    )
    service.db = db

    result = await service.reconcile_user_governance("owner-1")

    assert result["active_run_count"] == 1
    assert result["excluded_count"] == 1
    update = runs.update_one.await_args.args[1]["$set"]
    assert [item["code"] for item in update["candidates"]] == ["002171"]
    excluded = update["governance_excluded_candidates"]
    assert excluded[0]["code"] == "300450"
    assert excluded[0]["governance_reason"] == (
        "chi_next_market_permission_denied"
    )
    sync_kwargs = favorites.sync_auto_ai_candidates.await_args.kwargs
    assert [item["code"] for item in sync_kwargs["candidates"]] == ["002171"]
    assert sync_kwargs["market_permissions"]["chi_next_market"][
        "eligible"
    ] is False


@pytest.mark.asyncio
async def test_candidate_performance_is_diagnostic_deduplicated_and_governed():
    plan = {
        "entry_strategy": "pullback",
        "entry_price": 10.0,
        "stop_price": 9.0,
        "target_price": 12.0,
    }
    tracked = {
        "price_plan": plan,
        "performance": {
            "shadow_trade": {
                "tracking_version": "shadow_trade_v2",
                "status": "closed_stop",
                "entry_triggered_at": "2026-07-28T01:30:00+00:00",
                "closed_at": "2026-07-29T01:30:00+00:00",
                "entry_observation_key": "2026-07-28T09:30:00+08:00",
                "closed_observation_key": "2026-07-29T09:30:00+08:00",
                "entry_price": 10.0,
                "quantity": 100,
                "net_return_pct": -10.5,
                "net_pnl": -105.0,
                "max_return_pct": 1.0,
                "min_return_pct": -10.0,
            }
        },
    }
    documents = [
        {
            "_id": ObjectId(),
            "generated_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
            "plan_expires_at": "2026-07-30T15:00:00+08:00",
            "candidates": [
                {**tracked, "code": "002558", "name": "巨人网络"},
                {**tracked, "code": "688208", "name": "道通科技"},
                {**tracked, "code": "600406", "name": "国电南瑞"},
            ],
        },
        {
            "_id": ObjectId(),
            "generated_at": datetime(2026, 7, 26, tzinfo=timezone.utc),
            "plan_expires_at": "2026-07-30T15:00:00+08:00",
            "candidates": [
                {**tracked, "code": "002558", "name": "巨人网络"},
            ],
        },
    ]

    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        async def to_list(self, *, length):
            assert length == 30
            return documents

    settings = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "excluded_codes": ["600406"],
                "execution_capabilities": {
                    "market_permissions": {
                        "star_market": {"verified": True, "tradable": False}
                    }
                },
            }
        )
    )
    runs = SimpleNamespace(find=MagicMock(return_value=Cursor()))
    db = {
        "user_holding_settings": settings,
        "ai_candidate_runs": runs,
    }
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )
    service.db = db

    result = await service.performance_summary("admin-id")

    assert result["statistics_scope"] == "candidate_shadow_diagnostics"
    assert result["diagnostic_sample_count"] == 1
    assert result["governed_decision_sample_count"] == 0
    assert result["learning_eligible_count"] == 0
    assert result["duplicate_plan_count"] == 1
    assert result["governance_excluded_by_reason"] == {
        "star_market_permission_denied": 1,
        "user_excluded": 1,
    }
    assert [item["code"] for item in result["items"]] == ["002558"]
    assert result["items"][0]["counts_as_governed_decision_sample"] is False
    assert result["items"][0]["represents_real_account_position"] is False


@pytest.mark.asyncio
async def test_candidate_performance_quarantines_legacy_unordered_shadow_results():
    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        async def to_list(self, *, length):
            return [
                {
                    "_id": ObjectId(),
                    "generated_at": datetime(2026, 8, 5, tzinfo=timezone.utc),
                    "candidates": [
                        {
                            "code": "002645",
                            "name": "华宏科技",
                            "price_plan": {
                                "entry_price": 17.06,
                                "stop_price": 16.52,
                                "target_price": 23.75,
                            },
                            "performance": {
                                "observation_count": 43,
                                "shadow_trade": {
                                    "status": "closed_stop",
                                    "entry_triggered_at": "2026-08-06T01:20:05+00:00",
                                    "closed_at": "2026-08-06T01:20:05+00:00",
                                    "entry_price": 17.06,
                                    "exit_price": 16.52,
                                    "latest_price": 18.5,
                                    "return_pct": 8.44,
                                    "net_return_pct": -3.79,
                                    "min_return_pct": 8.44,
                                },
                            },
                        }
                    ],
                }
            ]

    settings = SimpleNamespace(find_one=AsyncMock(return_value={}))
    runs = SimpleNamespace(find=MagicMock(return_value=Cursor()))
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(),
        quotes=SimpleNamespace(),
    )
    service.db = {
        "user_holding_settings": settings,
        "ai_candidate_runs": runs,
    }

    result = await service.performance_summary("admin-id")

    assert result["sample_count"] == 0
    assert result["closed_count"] == 0
    assert result["invalid_shadow_sample_count"] == 1
    assert result["invalid_shadow_by_reason"] == {
        "legacy_path_unverifiable": 1
    }
    assert result["items"] == []


@pytest.mark.asyncio
async def test_scheduled_quote_refresh_never_polls_governance_excluded_codes():
    requested = []

    async def get_quotes(codes):
        requested.extend(codes)
        return {
            "000977": {
                "price": 63.0,
                "source": "tencent",
                "trade_at": "2026-07-27T14:55:00+08:00",
                "volume": 1000,
                "amount": 63_000,
            }
        }

    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(
            get_favorite_codes=AsyncMock(return_value=set())
        ),
        quotes=SimpleNamespace(get_quotes=get_quotes),
        market_status_loader=AsyncMock(return_value=_live_market_status()),
    )
    service._candidate_governance = AsyncMock(
        return_value={
            "excluded_codes": ["600406"],
            "star_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        }
    )
    service._apply_objective_profiles = AsyncMock()
    service._apply_macro_policy = AsyncMock()
    service._apply_account_policy = AsyncMock()
    document = {
        "_id": ObjectId(),
        "user_id": "admin-id",
        "generated_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
        "candidates": [
            {"code": "688208", "price_plan": {}},
            {"code": "600406", "price_plan": {}},
            {
                "code": "000977",
                "price_plan": {},
                "quote": {
                    "price": 63.0,
                    "source": "tencent",
                    "trade_at": "2026-07-27T14:54:00+08:00",
                    "volume": 1000,
                    "amount": 63_000,
                    "event_observed_at": None,
                },
            },
        ],
    }

    refreshed = await service._refresh_document(
        document,
        user_id="admin-id",
        persist=False,
        notify=False,
    )

    assert requested == ["000977", "sh000300"]
    assert [item["code"] for item in refreshed["candidates"]] == ["000977"]
    assert {
        item["code"] for item in refreshed["governance_excluded_candidates"]
    } == {"688208", "600406"}
    active = refreshed["candidates"][0]
    assert active["quote"]["event_change_detected"] is False
    assert active["quote"]["event_observed_at"] is None
    assert "performance" not in active


@pytest.mark.asyncio
async def test_scheduler_entry_never_polls_governance_excluded_codes():
    requested = []

    async def get_quotes(codes):
        requested.extend(codes)
        return {
            "000977": {
                "price": 63.1,
                "source": "tencent",
                "trade_at": "2026-07-27T14:56:00+08:00",
                "volume": 1100,
                "amount": 69_410,
            }
        }

    document = {
        "_id": ObjectId(),
        "user_id": "admin-id",
        "generated_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "candidates": [
            {"code": "688208", "price_plan": {}},
            {"code": "600406", "price_plan": {}},
            {
                "code": "000977",
                "price_plan": {},
                "quote": {
                    "price": 63.0,
                    "source": "tencent",
                    "trade_at": "2026-07-27T14:55:00+08:00",
                    "volume": 1000,
                    "amount": 63_000,
                },
            },
        ],
    }
    historical_document = {
        "_id": ObjectId(),
        "user_id": "admin-id",
        "generated_at": datetime(2026, 7, 26, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "quote_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "candidates": [
            {
                "code": "688208",
                "price_plan": {},
                "performance": {"shadow_trade": {"status": "active"}},
            },
            {
                "code": "600406",
                "price_plan": {},
                "performance": {"shadow_trade": {"status": "active"}},
            },
        ],
    }

    class Cursor:
        def sort(self, *_args):
            return self

        async def to_list(self, *, length):
            assert length == 500
            return [document, historical_document]

    runs = SimpleNamespace(
        find=MagicMock(return_value=Cursor()),
        update_one=AsyncMock(),
    )
    db = {"ai_candidate_runs": runs}
    service = AICandidateService(
        research_runner=_research_payload,
        favorites=SimpleNamespace(
            get_favorite_codes=AsyncMock(return_value=set())
        ),
        quotes=SimpleNamespace(get_quotes=get_quotes),
    )
    service.db = db
    service._candidate_governance = AsyncMock(
        return_value={
            "excluded_codes": ["600406"],
            "star_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
            },
        }
    )
    service._apply_objective_profiles = AsyncMock()
    service._apply_macro_policy = AsyncMock()
    service._apply_account_policy = AsyncMock()
    service._publish_transition = AsyncMock()

    result = await service.refresh_all_active_runs()

    assert result == {
        "refreshed_user_count": 1,
        "refreshed_historical_run_count": 0,
        "governance_cleaned_run_count": 2,
        "failed_user_count": 0,
    }
    assert requested == ["000977", "sh000300"]
    persisted_updates = [
        call.args[1]["$set"] for call in runs.update_one.await_args_list
    ]
    persisted = next(
        update
        for update in persisted_updates
        if [item["code"] for item in update.get("candidates", [])]
        == ["000977"]
        and "quote_refreshed_at" in update
    )
    assert [item["code"] for item in persisted["candidates"]] == ["000977"]
    assert {
        item["code"] for item in persisted["governance_excluded_candidates"]
    } == {"688208", "600406"}
    historical_update = next(
        update
        for update in persisted_updates
        if update.get("candidates") == []
        and len(update.get("governance_excluded_candidates") or []) == 2
    )
    assert all(
        item["performance"]["shadow_trade"]["status"] == "stopped_governance"
        for item in historical_update["governance_excluded_candidates"]
    )
