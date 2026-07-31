from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from bson import ObjectId

from app.models.decision import CodexDecisionProposalInput
from app.services.decision_validation_service import DecisionValidationService


NOW = datetime.fromisoformat("2026-07-24T14:30:30+08:00")


def _candidate(symbol="600406", **updates):
    value = {
        "symbol": symbol,
        "name": "国电南瑞" if symbol == "600406" else f"股票{symbol}",
        "identity": {
            "code": symbol,
            "name": "国电南瑞" if symbol == "600406" else f"股票{symbol}",
            "objective_segment": "新型电力系统",
        },
        "software_baseline_action": "avoid",
        "software_reason_codes": ["market_red"],
        "quote": {
            "price": 21.30,
            "source": "tencent",
            "trade_at": "2026-07-24T14:30:00+08:00",
            "status": "fresh",
            "actionable": True,
        },
        "plans": {
            "short": {
                "entry_strategy": "pullback",
                "entry_price": 21.20,
                "stop_price": 20.90,
                "target_price": 23.80,
            }
        },
        "profile": {
            "provider_sector": "工业",
            "industry": "电网设备",
        },
        "allocation": {},
        "portfolio_impact": {
            "exposure": {
                "theme": {
                    "taxonomy_value": "新型电力系统",
                    "before_amount": 0.0,
                    "cap_pct": 35.0,
                },
                "provider_sector": {
                    "taxonomy_value": "工业",
                    "before_amount": 0.0,
                    "cap_pct": 40.0,
                },
                "industry": {
                    "taxonomy_value": "电网设备",
                    "before_amount": 0.0,
                    "cap_pct": 30.0,
                },
            },
            "symbol_exposure": {
                "before_amount": 0.0,
                "cap_pct": 45.0,
            },
            "correlation": {"cap": 0.8, "comparisons": []},
        },
        "hard_constraints": [],
        "soft_warnings": [
            {
                "code": "market_red",
                "severity": "warning",
                "overrideable": True,
            }
        ],
        "risk_envelope": {
            "lot_size": 100,
            "max_allowed_quantity": 300,
            "max_allowed_amount": 6360.0,
            "max_planned_loss_amount": 90.0,
        },
        "evidence": [
            {"evidence_id": f"{symbol}:quote", "kind": "quote", "value": {}},
            {
                "evidence_id": f"{symbol}:plan:short",
                "kind": "price_plan",
                "value": {},
            },
        ],
    }
    value.update(updates)
    return value


def _packet(*, phase="live_pm", candidates=None, **updates):
    value = {
        "research_packet_id": "research-1",
        "user_id": "owner-1",
        "material_hash": "research-material-1",
        "as_of": "2026-07-24T14:30:02+08:00",
        "market_session": {
            "phase": phase,
            "quote_freshness_required_seconds": 90,
        },
        "account": {
            "total_assets": 10_685.41,
            "available_cash": 10_685.41,
            "current_exposure_pct": 0.0,
        },
        "market": {"combined_regime": "red", "domestic_regime": "red"},
        "decision_objective": {
            "max_new_positions": 2,
            "primary_position_count": 1,
        },
        "execution_capabilities": {
            "condition_order": {
                "verified": True,
                "independent_trigger_price_supported": True,
                "separate_order_limit_price_supported": True,
            }
        },
        "hard_risk_policy": {
            "available_new_exposure_pct": 60.0,
            "hard_single_symbol_cap_pct": 45.0,
            "per_position_loss_budget_pct": 1.0,
            "total_new_position_loss_budget_pct": 2.0,
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
        "candidates": candidates or [_candidate()],
    }
    value.update(updates)
    return value


def _selection(symbol="600406", **updates):
    value = {
        "symbol": symbol,
        "action": "condition_order",
        "position_role": "primary",
        "requested_quantity": 100,
        "entry_strategy": "pullback",
        "trigger_price": "21.20",
        "order_limit_price": "21.20",
        "stop_price": "20.90",
        "target_price": "23.80",
        "expires_at": "2026-07-28T15:00:00+08:00",
        "confidence": 0.78,
        "thesis": "电网数字化候选在回调计划内配置主仓",
        "evidence_refs": [f"{symbol}:quote", f"{symbol}:plan:short"],
        "overrides": [
            {
                "warning_code": "market_red",
                "reason": "使用较小仓位并设置明确止损",
                "risk_adjustment": "reduced_position",
            }
        ],
    }
    value.update(updates)
    return value


def _proposal(*, selections=None, **updates):
    value = {
        "research_packet_id": "research-1",
        "proposal_schema_version": "codex-proposal-v1",
        "decision_scope": {
            "max_new_positions": 2,
            "primary_position_count": 1,
        },
        "selections": selections or [_selection()],
        "portfolio_rationale": "一个主仓候选并保留其余现金",
        "no_action_reason": None,
        "prompt_version": "codex-decision-v1",
    }
    value.update(updates)
    return CodexDecisionProposalInput.model_validate(value).model_dump(mode="json")


def _failure_codes(result):
    return {item["code"] for item in result["hard_failures"]}


class _MutatingInsertCollection:
    def __init__(self, row=None):
        self.row = deepcopy(row)

    async def find_one(self, query):
        if self.row and all(self.row.get(key) == value for key, value in query.items()):
            return deepcopy(self.row)
        return None

    async def insert_one(self, document):
        document["_id"] = ObjectId()
        return SimpleNamespace(inserted_id=document["_id"])


class _ValidationDatabase:
    def __init__(self, proposal=None):
        self.collections = {
            "codex_decision_proposals": _MutatingInsertCollection(proposal),
            "decision_validations": _MutatingInsertCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _empty_proposal_document():
    payload = {
        "research_packet_id": "research-1",
        "proposal_schema_version": "codex-proposal-v1",
        "decision_scope": {
            "max_new_positions": 2,
            "primary_position_count": 1,
        },
        "selections": [],
        "portfolio_rationale": "当前不新增任何仓位",
        "no_action_reason": "等待市场门禁与候选条件改善",
        "prompt_version": "codex-decision-v1",
    }
    return {
        "proposal_id": "proposal-empty",
        "user_id": "owner-1",
        "research_packet_id": "research-1",
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_persist_does_not_return_mongo_object_id():
    service = DecisionValidationService(db=_ValidationDatabase())
    validation = await service.validate_document(
        "owner-1",
        _empty_proposal_document(),
        _packet(),
        now=NOW,
        proposal_id="proposal-empty",
    )

    persisted = await service.persist(validation)

    assert persisted["status"] == "valid"
    assert "_id" not in persisted
    assert not any(isinstance(value, ObjectId) for value in persisted.values())


@pytest.mark.asyncio
async def test_empty_no_action_proposal_refresh_stays_bound_to_immutable_packet():
    original = _packet()
    refreshed = deepcopy(original)
    refreshed["research_packet_id"] = "research-new"
    refreshed["material_hash"] = "research-material-new"

    class Research:
        async def get(self, user_id, research_packet_id):
            assert user_id == "owner-1"
            assert research_packet_id == "research-1"
            return deepcopy(original)

        async def today(self, user_id, *, refresh=True, now=None):
            assert refresh is True
            return deepcopy(refreshed)

    service = DecisionValidationService(
        db=_ValidationDatabase(_empty_proposal_document()),
        research_service=Research(),
    )

    result = await service.validate(
        "owner-1",
        "proposal-empty",
        refresh_quote=True,
        now=NOW,
    )

    assert result["status"] == "valid"
    assert result["research_packet_id"] == "research-1"
    assert result["hard_failures"] == []
    assert "_id" not in result


@pytest.mark.asyncio
async def test_market_red_override_can_validate():
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(),
        _packet(),
        now=NOW,
    )

    assert result["status"] == "valid", result["hard_failures"]
    assert result["accepted_overrides"][0]["warning_code"] == "market_red"
    assert result["recalculated"]["total_cost"] == 2120.0
    assert result["recalculated"]["total_planned_loss"] == 30.0
    assert result["valid_until"] == "2026-07-24T14:31:30+08:00"


@pytest.mark.asyncio
async def test_market_red_without_declared_override_is_invalid():
    selection = _selection(overrides=[])
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[selection]),
        _packet(),
        now=NOW,
    )

    assert result["status"] == "invalid"
    assert "soft_warning_override_missing" in _failure_codes(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "expected_code"),
    [
        (150, "invalid_board_lot"),
        (400, "requested_quantity_exceeds_hard_limit"),
    ],
)
async def test_invalid_quantity_is_rejected_not_rewritten(quantity, expected_code):
    proposal = _proposal(selections=[_selection(requested_quantity=quantity)])
    original = deepcopy(proposal)

    result = await DecisionValidationService().validate_document(
        "owner-1",
        proposal,
        _packet(),
        now=NOW,
    )

    assert expected_code in _failure_codes(result)
    assert proposal == original
    assert result["recalculated"]["selections"][0]["requested_quantity"] == quantity


@pytest.mark.asyncio
async def test_unknown_evidence_reference_is_rejected():
    selection = _selection(evidence_refs=["600406:made-up"])
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[selection]),
        _packet(),
        now=NOW,
    )

    assert "evidence_reference_not_found" in _failure_codes(result)


@pytest.mark.asyncio
async def test_cash_limit_is_recalculated_from_account_not_model_claims():
    packet = _packet()
    packet["account"]["available_cash"] = 1000.0
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(),
        packet,
        now=NOW,
    )

    assert "insufficient_cash" in _failure_codes(result)


@pytest.mark.asyncio
async def test_per_position_loss_limit_is_recalculated_from_stop_distance():
    selection = _selection(stop_price="19.50")
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[selection]),
        _packet(),
        now=NOW,
    )

    assert "per_position_loss_limit" in _failure_codes(result)


@pytest.mark.asyncio
async def test_exchange_price_tick_is_enforced():
    selection = _selection(trigger_price="21.205", target_price="23.805")
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[selection]),
        _packet(),
        now=NOW,
    )

    assert "invalid_price_tick" in _failure_codes(result)


@pytest.mark.asyncio
async def test_buy_now_requires_live_fresh_tencent_quote():
    candidate = _candidate(
        quote={
            "price": 21.10,
            "source": "tencent",
            "trade_at": "2026-07-24T14:20:00+08:00",
            "status": "stale_trade_at",
            "actionable": False,
        }
    )
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[_selection(action="buy_now")]),
        _packet(candidates=[candidate]),
        now=NOW,
    )

    assert result["status"] == "stale_revalidation_required", result[
        "hard_failures"
    ]
    assert "buy_now_quote_stale" in _failure_codes(result)


@pytest.mark.asyncio
async def test_buy_now_requires_live_market_phase():
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[_selection(action="buy_now")]),
        _packet(phase="post_close"),
        now=NOW,
    )

    assert result["status"] == "invalid"
    assert "buy_now_outside_live_session" in _failure_codes(result)


@pytest.mark.asyncio
async def test_condition_order_requires_a_fresh_live_quote():
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(),
        _packet(phase="post_close"),
        now=NOW,
    )

    assert result["status"] == "invalid"
    assert "condition_order_quote_stale" in _failure_codes(result)


@pytest.mark.asyncio
async def test_breakout_condition_order_requires_verified_separate_trigger_capability():
    candidate = _candidate(
        "002602",
        quote={
            "price": 12.45,
            "source": "tencent",
            "trade_at": "2026-07-24T14:30:00+08:00",
            "status": "fresh",
            "actionable": True,
        },
    )
    selection = _selection(
        "002602",
        entry_strategy="breakout",
        trigger_price="13.35",
        order_limit_price="13.40",
        stop_price="12.70",
        target_price="14.20",
    )

    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[selection]),
        _packet(candidates=[candidate], execution_capabilities={}),
        now=NOW,
    )

    assert result["status"] == "invalid"
    assert (
        "condition_order_execution_capability_unverified"
        in _failure_codes(result)
    )
    assert "condition_order_plan_already_invalidated" in _failure_codes(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permission", "expected_reason"),
    [
        (None, "beijing_stock_exchange_permission_unverified"),
        (
            {"verified": True, "tradable": False},
            "beijing_stock_exchange_permission_denied",
        ),
    ],
)
async def test_actionable_beijing_selection_requires_verified_permission(
    permission,
    expected_reason,
):
    capabilities = _packet()["execution_capabilities"]
    if permission is not None:
        capabilities["market_permissions"] = {
            "beijing_stock_exchange": permission
        }
    packet = _packet(
        candidates=[_candidate("920493")],
        execution_capabilities=capabilities,
    )

    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(
            selections=[
                _selection(
                    "920493",
                    action="buy_now",
                    entry_strategy="pullback",
                )
            ]
        ),
        packet,
        now=NOW,
    )

    assert expected_reason in _failure_codes(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permission", "expected_reason"),
    [
        (None, "chi_next_market_permission_unverified"),
        (
            {"verified": True, "tradable": False},
            "chi_next_market_permission_denied",
        ),
    ],
)
async def test_actionable_chinext_selection_requires_verified_permission(
    permission,
    expected_reason,
):
    capabilities = _packet()["execution_capabilities"]
    if permission is not None:
        capabilities["market_permissions"] = {
            "chi_next_market": permission
        }
    packet = _packet(
        candidates=[_candidate("300450")],
        execution_capabilities=capabilities,
    )

    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(
            selections=[
                _selection(
                    "300450",
                    action="buy_now",
                    entry_strategy="pullback",
                )
            ]
        ),
        packet,
        now=NOW,
    )

    assert expected_reason in _failure_codes(result)


@pytest.mark.asyncio
async def test_action_scoped_hard_constraint_blocks_matching_action():
    candidate = _candidate(
        hard_constraints=[
            {
                "code": "calendar_unknown",
                "overrideable": False,
                "applies_to": ["buy_now"],
            }
        ]
    )
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=[_selection(action="buy_now")]),
        _packet(candidates=[candidate]),
        now=NOW,
    )

    assert "calendar_unknown" in _failure_codes(result)


@pytest.mark.asyncio
async def test_plan_expiry_is_rejected():
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(
            selections=[
                _selection(expires_at=(NOW - timedelta(minutes=1)).isoformat())
            ]
        ),
        _packet(),
        now=NOW,
    )

    assert "plan_expired" in _failure_codes(result)


@pytest.mark.asyncio
async def test_same_industry_new_positions_hit_correlation_cap():
    second = _candidate(
        "601138",
        name="工业富联",
        identity={
            "code": "601138",
            "name": "工业富联",
            "objective_segment": "新型电力系统",
        },
        risk_envelope={
            "lot_size": 100,
            "max_allowed_quantity": 100,
            "max_allowed_amount": 2120.0,
            "max_planned_loss_amount": 30.0,
        },
        evidence=[
            {"evidence_id": "601138:quote", "kind": "quote", "value": {}},
            {
                "evidence_id": "601138:plan:short",
                "kind": "price_plan",
                "value": {},
            },
        ],
    )
    selections = [
        _selection(requested_quantity=100),
        _selection(
            "601138",
            position_role="secondary",
            requested_quantity=100,
        ),
    ]
    result = await DecisionValidationService().validate_document(
        "owner-1",
        _proposal(selections=selections),
        _packet(candidates=[_candidate(), second]),
        now=NOW,
    )

    assert "correlation_limit" in _failure_codes(result)
