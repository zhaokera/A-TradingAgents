from __future__ import annotations

from copy import deepcopy

from app.services.daily_structured_analysis import (
    apply_daily_analysis_execution_gate,
    build_daily_structured_analysis,
)


TRADE_DATE = "2026-09-01"


def _candidate(index: int, **overrides):
    code = f"{600000 + index:06d}"
    value = {
        "code": code,
        "rolling_pool_state": "current",
        "research_tier": "deep" if index < 15 else "structured",
        "quote": {
            "status": "ok",
            "trade_date": TRADE_DATE,
            "freshness": "live",
            "price": 10.0,
        },
        "structured_review": {
            "technical": {"status": "passed", "source": "tencent_daily_bars"},
            "earnings": {"status": "clear", "source": "cninfo"},
            "notice": {"status": "clear", "source": "cninfo"},
            "corporate_action": {"status": "clear", "source": "cninfo"},
            "hard_risk_status": "clear",
        },
        "objective_profile": {
            "status": "complete",
            "objective_tier": "core",
            "source": "structured_taxonomy",
        },
        "price_plan": {
            "status": "valid",
            "entry_price": 10.0,
            "stop_loss_price": 9.5,
            "target_price": 11.0,
        },
        "account_fit": {
            "status": "eligible",
            "one_lot_affordable": True,
        },
        "portfolio_allocation": {"status": "watch_only"},
        "risk_flags": [],
        "execution_actionable": False,
    }
    value.update(deepcopy(overrides))
    return value


def _discovery():
    return {
        "benchmark_trade_date": TRADE_DATE,
        "universe_count": 5544,
        "permission_prefilter_excluded_count": 2355,
        "eligible_count": 240,
        "technical_screened_count": 240,
        "technical_passed_count": 118,
        "stage_sources": {"public_snapshot": {"status": "ok"}},
    }


def test_daily_structured_analysis_requires_one_hundred_current_day_completions():
    result = build_daily_structured_analysis(
        [_candidate(index) for index in range(100)],
        discovery=_discovery(),
        trade_date=TRADE_DATE,
    )

    assert result["daily_minimum"] == 100
    assert result["minimum_met"] is True
    assert result["planned_count"] == 100
    assert result["completed_count"] == 100
    assert result["incomplete_count"] == 0
    assert result["formal_deep_count"] == 15
    assert result["structured_count"] == 85
    assert len(result["items"]) == 100
    assert all(item["trade_date"] == TRADE_DATE for item in result["items"])
    assert all(item["status"] == "completed" for item in result["items"])


def test_daily_structured_analysis_does_not_count_aging_or_incomplete_candidates():
    candidates = [_candidate(index) for index in range(100)]
    candidates[98]["rolling_pool_state"] = "aging"
    candidates[99]["structured_review"]["notice"] = {
        "status": "unavailable",
        "source": "cninfo",
    }

    result = build_daily_structured_analysis(
        candidates,
        discovery=_discovery(),
        trade_date=TRADE_DATE,
    )

    assert result["minimum_met"] is False
    assert result["completed_count"] == 98
    assert result["incomplete_count"] == 1
    assert result["aging_not_counted"] == 1
    assert result["incomplete_reasons"] == {"notice_evidence_unavailable": 1}


def test_daily_structured_analysis_rejects_unusable_structured_quote_freshness():
    candidate = _candidate(1)
    candidate["quote"]["freshness"] = {
        "status": "future_provider_timestamp",
        "actionable": False,
    }

    result = build_daily_structured_analysis(
        [candidate],
        discovery=_discovery(),
        trade_date=TRADE_DATE,
        daily_minimum=1,
    )

    assert result["minimum_met"] is False
    assert result["completed_count"] == 0
    assert result["incomplete_reasons"] == {"quote_evidence_unavailable": 1}


def test_daily_minimum_gate_closes_execution_without_discarding_research():
    document = {
        "candidates": [
            {
                **_candidate(index),
                "execution_actionable": True,
                "condition_order_ready": True,
            }
            for index in range(99)
        ],
        "portfolio_plan": {"status": "allocated"},
        "execution": {"actionable": True, "status": "ready"},
        "daily_structured_analysis": {"daily_minimum": 100, "minimum_met": False},
    }

    apply_daily_analysis_execution_gate(document)

    assert document["execution"] == {
        "actionable": False,
        "status": "daily_structured_analysis_minimum_not_met",
        "requires_daily_decision": True,
    }
    assert document["portfolio_plan"]["status"] == "research_only"
    assert all(item["execution_actionable"] is False for item in document["candidates"])
    assert all(item["condition_order_ready"] is False for item in document["candidates"])
