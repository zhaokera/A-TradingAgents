from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.decision_research_service import (
    DecisionResearchService,
)
from app.services.decision_workflow_errors import DecisionWorkflowError


def _candidate_item(*, action="avoid", reason_codes=None):
    return {
        "identity": {
            "code": "600406",
            "name": "国电南瑞",
            "market": "A股",
            "rank": 1,
            "rank_score": 88.0,
            "objective_tier": "core",
            "objective_segment": "新型电力系统",
            "objective_match_score": 1.0,
        },
        "action": action,
        "reason_codes": list(reason_codes or ["market_red"]),
        "quote": {
            "price": 22.10,
            "source": "tencent",
            "trade_at": "2026-07-24T14:30:00+08:00",
            "quote_checked_at": "2026-07-24T14:30:01+08:00",
            "status": "fresh",
            "actionable": True,
        },
        "plans": {
            "short": {
                "entry_strategy": "pullback",
                "entry_price": 21.20,
                "stop_price": 20.90,
                "target_price": 23.80,
                "entry_status": "waiting_pullback",
            },
            "swing": {},
            "position": {},
        },
        "profile": {
            "status": "complete",
            "confidence": "high",
            "provider_sector": "工业",
            "industry": "电网设备",
            "main_business": "电网自动化与数字化",
            "provider_sector_evidence": {
                "value": "工业",
                "source": "tushare",
                "source_endpoint": "stock_basic",
            },
            "industry_evidence": {
                "value": "电网设备",
                "source": "tushare",
                "source_endpoint": "stock_basic",
            },
            "main_business_evidence": {
                "value": "电网自动化与数字化",
                "source": "tushare",
                "source_endpoint": "stock_company",
            },
        },
        "allocation": {
            "status": "wait",
            "reason": "market_red",
            "reason_codes": ["market_red"],
            "quantity": 0,
            "amount": 0.0,
            "position_pct": 0.0,
        },
        "portfolio_impact": {
            "exposure": {
                "theme": {
                    "taxonomy_value": "新型电力系统",
                    "before_amount": 0.0,
                    "before_pct": 0.0,
                    "cap_pct": 35.0,
                },
                "provider_sector": {
                    "taxonomy_value": "工业",
                    "before_amount": 0.0,
                    "before_pct": 0.0,
                    "cap_pct": 40.0,
                },
                "industry": {
                    "taxonomy_value": "电网设备",
                    "before_amount": 0.0,
                    "before_pct": 0.0,
                    "cap_pct": 30.0,
                },
            },
            "symbol_exposure": {
                "before_amount": 0.0,
                "before_pct": 0.0,
                "cap_pct": 45.0,
            },
            "correlation": {
                "cap": 0.8,
                "comparisons": [],
                "blocking_pair": None,
            },
        },
        "planned_loss": {"amount": 0.0, "pct_of_assets": 0.0},
        "invalidation": {
            "stop_price": 20.90,
            "plan_expires_at": "2026-07-28T15:00:00+08:00",
            "risk_flags": [],
        },
        "versions": {"rule_version": "decision-v1"},
        "plan_id": "plan-600406",
    }


def _baseline(*, bucket="avoid", reason_codes=None, total_assets=10_685.41):
    item = _candidate_item(action=bucket, reason_codes=reason_codes)
    buckets = {"buy_now": [], "condition_order": [], "wait": [], "avoid": []}
    buckets[bucket].append(item)
    return {
        "decision_id": "decision-1",
        "user_id": "owner-1",
        "decision_date": "2026-07-24",
        "market_phase": "live_pm",
        "revision": 1,
        "as_of": "2026-07-24T14:30:02+08:00",
        "candidate_run_id": "run-1",
        "briefing_as_of": "2026-07-24T14:29:00+08:00",
        "market_session": {
            "phase": "live_pm",
            "quote_freshness_required_seconds": 90,
        },
        "account": {
            "total_assets": total_assets,
            "available_cash": total_assets,
            "current_exposure_pct": 0.0,
        },
        "market": {
            "combined_regime": "red",
            "domestic_regime": "red",
        },
        "portfolio_constraints": {
            "effective_limits": {
                "theme_exposure_cap_pct": 35.0,
                "provider_sector_exposure_cap_pct": 40.0,
                "industry_exposure_cap_pct": 30.0,
                "pairwise_correlation_cap": 0.8,
            },
            "holding_valuation_audit": [],
        },
        "effective_policy": {
            "green_new_exposure_cap_pct": 60.0,
            "yellow_new_exposure_cap_pct": 30.0,
            "available_new_exposure_pct": 0.0,
            "preferred_single_symbol_pct": 35.0,
            "hard_single_symbol_cap_pct": 45.0,
            "per_position_loss_budget_pct": 1.0,
            "total_new_position_loss_budget_pct": 2.0,
            "policy_version": "investment-policy-v2",
        },
        "summary": {
            "buy_now_count": len(buckets["buy_now"]),
            "condition_order_count": len(buckets["condition_order"]),
            "wait_count": len(buckets["wait"]),
            "avoid_count": len(buckets["avoid"]),
        },
        **buckets,
        "data_quality": {},
        "rule_version": "decision-v1",
        "material_hash": "baseline-material-1",
    }


class FakeBaselineService:
    def __init__(self, payload):
        self.payload = deepcopy(payload)
        self.calls = []

    async def today(self, user_id, *, refresh=True, now=None):
        self.calls.append((user_id, refresh, now))
        return deepcopy(self.payload)


class FakeCollection:
    def __init__(self):
        self.rows = []

    async def find_one(self, query, projection=None, sort=None):
        rows = [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in query.items())
        ]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(field), reverse=direction < 0)
        return deepcopy(rows[0]) if rows else None

    async def insert_one(self, document):
        self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document["research_packet_id"])


class FakeDatabase:
    def __init__(self):
        self.research = FakeCollection()

    def __getitem__(self, name):
        assert name == "decision_research_packets"
        return self.research


def _service(payload, **kwargs):
    return DecisionResearchService(
        baseline_service=FakeBaselineService(payload),
        db=FakeDatabase(),
        market_red_blocks_new_positions=kwargs.get(
            "market_red_blocks_new_positions", False
        ),
        max_new_positions=2,
        primary_position_count=1,
    )


@pytest.mark.asyncio
async def test_market_red_is_soft_and_keeps_nonzero_hard_envelope():
    packet = await _service(_baseline()).today("owner-1", refresh=False)
    candidate = packet["candidates"][0]

    assert [item["code"] for item in candidate["soft_warnings"]] == [
        "market_red"
    ]
    assert candidate["hard_constraints"] == []
    assert candidate["risk_envelope"]["max_allowed_quantity"] >= 100
    assert packet["hard_risk_policy"]["available_new_exposure_pct"] > 0


@pytest.mark.asyncio
async def test_explicit_market_red_block_turns_warning_into_hard_constraint():
    packet = await _service(
        _baseline(),
        market_red_blocks_new_positions=True,
    ).today("owner-1", refresh=False)
    candidate = packet["candidates"][0]

    assert candidate["soft_warnings"] == []
    assert candidate["hard_constraints"][0]["code"] == "market_red"
    assert candidate["risk_envelope"]["max_allowed_quantity"] == 0


@pytest.mark.asyncio
async def test_account_blocked_remains_a_hard_constraint():
    packet = await _service(
        _baseline(bucket="wait", reason_codes=["account_blocked"])
    ).today("owner-1", refresh=False)

    assert packet["candidates"][0]["hard_constraints"][0]["code"] == (
        "account_blocked"
    )


@pytest.mark.asyncio
async def test_research_packet_preserves_profile_provenance_and_wait_semantics():
    baseline = _baseline(
        bucket="wait",
        reason_codes=[
            "formal_research_required",
            "pullback_reversal_confirmation_required",
        ],
    )
    item = baseline["wait"][0]
    item["profile_contract"] = {
        "discovery_research_tier": "structured",
        "formal_research_selected": True,
        "candidate_status": "deferred_structured_layer",
        "candidate_decision_critical_complete": False,
        "resolved_status": "verified",
        "resolved_decision_critical_complete": True,
        "eligible_for_buy_now": False,
    }
    item["candidate_reason_summary"] = (
        "回调结构候选，等待观察区间企稳后再评估。"
    )
    item["candidate_source_profile"] = {
        "status": "deferred_structured_layer",
        "confidence": "deferred",
    }
    item["resolved_profile"] = {
        "status": "verified",
        "confidence": "high",
    }

    packet = await _service(baseline).today("owner-1", refresh=False)
    candidate = packet["candidates"][0]

    assert candidate["profile_contract"] == item["profile_contract"]
    assert candidate["candidate_reason_summary"] == (
        "回调结构候选，等待观察区间企稳后再评估。"
    )
    assert candidate["candidate_source_profile"] == (
        item["candidate_source_profile"]
    )
    assert candidate["resolved_profile"] == item["resolved_profile"]
    assert [row["code"] for row in candidate["hard_constraints"]] == [
        "formal_research_required",
        "pullback_reversal_confirmation_required",
    ]
    assert candidate["hard_constraints"][1]["applies_to"] == ["buy_now"]


@pytest.mark.asyncio
async def test_unknown_reason_is_not_silently_overrideable():
    packet = await _service(
        _baseline(bucket="avoid", reason_codes=["new_unknown_gate"])
    ).today("owner-1", refresh=False)

    constraint = packet["candidates"][0]["hard_constraints"][0]
    assert constraint["code"] == "unclassified_gate"
    assert constraint["details"]["original_code"] == "new_unknown_gate"


@pytest.mark.asyncio
async def test_research_packet_evidence_ids_are_stable_and_unique():
    service = _service(_baseline())

    first = await service.today("owner-1", refresh=False)
    second = await service.today("owner-1", refresh=False)
    first_ids = [
        item["evidence_id"] for item in first["candidates"][0]["evidence"]
    ]
    second_ids = [
        item["evidence_id"] for item in second["candidates"][0]["evidence"]
    ]

    assert first_ids == second_ids
    assert len(first_ids) == len(set(first_ids))
    assert first["research_packet_id"] == second["research_packet_id"]


@pytest.mark.asyncio
async def test_research_packet_preserves_rolling_pool_audit():
    baseline = _baseline()
    baseline["candidate_run_id"] = "run-top-100"
    baseline["candidate_research"] = {
        "job_id": "job-top-100",
        "status": "completed",
    }
    baseline["rolling_pool"] = {
        "capacity": 100,
        "total_count": 68,
        "formal_research_capacity": 15,
        "formal_research_count": 15,
        "current_count": 66,
        "aging_count": 2,
        "expired_count": 0,
        "invalidated_count": 0,
        "candidates": [
            {
                "code": "600406",
                "lifecycle_state": "current",
                "research_tier": "deep",
                "selection_reason": "dynamic_formal_research_selected",
            },
            {
                "code": "600562",
                "lifecycle_state": "aging",
                "research_tier": "structured",
                "selection_reason": "outside_dynamic_formal_research_tier",
            },
        ],
    }

    packet = await _service(baseline).today("owner-1", refresh=False)

    assert packet["candidate_run_id"] == "run-top-100"
    assert packet["candidate_research"] == {
        "job_id": "job-top-100",
        "status": "completed",
    }
    assert packet["rolling_pool"] == baseline["rolling_pool"]


@pytest.mark.asyncio
async def test_research_packet_cannot_be_loaded_by_another_user():
    service = _service(_baseline())
    packet = await service.today("owner-1", refresh=False)

    with pytest.raises(DecisionWorkflowError) as error:
        await service.get("owner-2", packet["research_packet_id"])

    assert error.value.code == "research_packet_not_found"


@pytest.mark.asyncio
async def test_research_packet_labels_source_as_software_baseline():
    packet = await _service(_baseline()).today("owner-1", refresh=False)

    assert packet["software_baseline"]["authority"] == "software_baseline"
    assert packet["software_baseline"]["is_final_decision"] is False
