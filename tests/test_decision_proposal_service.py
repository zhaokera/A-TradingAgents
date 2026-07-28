from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.decision import (
    CodexDecisionProposalInput,
    DecisionConfirmationInput,
)
from app.services.decision_confirmation_service import DecisionConfirmationService
from app.services.decision_proposal_service import DecisionProposalService
from app.services.decision_validation_service import DecisionValidationService
from app.services.decision_workflow_errors import DecisionWorkflowError


NOW = datetime.fromisoformat("2026-07-24T14:30:30+08:00")


def _packet():
    return {
        "research_packet_id": "research-1",
        "user_id": "owner-1",
        "material_hash": "research-material-1",
        "as_of": "2026-07-24T14:30:02+08:00",
        "market_session": {
            "phase": "live_pm",
            "quote_freshness_required_seconds": 90,
        },
        "account": {
            "total_assets": 10_685.41,
            "available_cash": 10_685.41,
            "current_exposure_pct": 0.0,
        },
        "market": {"combined_regime": "red"},
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
        "software_baseline": {
            "baseline_id": "decision-1",
            "authority": "software_baseline",
            "is_final_decision": False,
        },
        "candidates": [
            {
                "symbol": "600406",
                "name": "国电南瑞",
                "identity": {
                    "code": "600406",
                    "name": "国电南瑞",
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
                    "max_allowed_quantity": 100,
                    "max_allowed_amount": 2120.0,
                    "max_planned_loss_amount": 30.0,
                },
                "evidence": [
                    {
                        "evidence_id": "600406:quote",
                        "kind": "quote",
                        "value": {},
                    },
                    {
                        "evidence_id": "600406:plan:short",
                        "kind": "price_plan",
                        "value": {},
                    },
                ],
            }
        ],
        "disclaimer": "仅供研究和参考，不构成投资建议或交易指令。",
    }


def _proposal(*, quantity=100):
    return CodexDecisionProposalInput.model_validate(
        {
            "research_packet_id": "research-1",
            "proposal_schema_version": "codex-proposal-v1",
            "decision_scope": {
                "max_new_positions": 2,
                "primary_position_count": 1,
            },
            "selections": [
                {
                    "symbol": "600406",
                    "action": "condition_order",
                    "position_role": "primary",
                    "requested_quantity": quantity,
                    "entry_strategy": "pullback",
                    "trigger_price": "21.20",
                    "order_limit_price": "21.20",
                    "stop_price": "20.90",
                    "target_price": "23.80",
                    "expires_at": "2026-07-28T15:00:00+08:00",
                    "confidence": 0.78,
                    "thesis": "电网数字化候选在回调计划内配置主仓",
                    "evidence_refs": [
                        "600406:quote",
                        "600406:plan:short",
                    ],
                    "overrides": [
                        {
                            "warning_code": "market_red",
                            "reason": "使用较小仓位并设置明确止损",
                            "risk_adjustment": "reduced_position",
                        }
                    ],
                }
            ],
            "portfolio_rationale": "只配置一只主仓并保留现金",
            "prompt_version": "codex-decision-v1",
        }
    )


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
        identifier = (
            document.get("proposal_id")
            or document.get("validation_id")
            or document.get("confirmation_id")
        )
        return SimpleNamespace(inserted_id=identifier)


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]


class FakeResearchService:
    def __init__(self, packet):
        self.packet = deepcopy(packet)

    async def get(self, user_id, research_packet_id):
        if (
            user_id != self.packet["user_id"]
            or research_packet_id != self.packet["research_packet_id"]
        ):
            raise DecisionWorkflowError(
                "research_packet_not_found",
                "研究包不存在",
                status_code=404,
            )
        return deepcopy(self.packet)

    async def latest(self, user_id):
        return deepcopy(self.packet) if user_id == self.packet["user_id"] else None

    async def today(self, user_id, *, refresh=True, now=None):
        return await self.get(user_id, self.packet["research_packet_id"])


class FakeBaselineService:
    async def today(self, user_id, *, refresh=True, now=None):
        return {
            "decision_id": "decision-1",
            "authority": "software_baseline",
            "is_final_decision": False,
            "summary": {
                "buy_now_count": 0,
                "condition_order_count": 0,
                "wait_count": 0,
                "avoid_count": 1,
            },
        }


class RotatedResearchService(FakeResearchService):
    def __init__(self):
        stale = _packet()
        stale["research_packet_id"] = "research-old"
        stale["source_baseline_id"] = "decision-old"
        stale["software_baseline"]["baseline_id"] = "decision-old"
        super().__init__(stale)
        self.today_calls = 0

    async def today(self, user_id, *, refresh=True, now=None):
        self.today_calls += 1
        fresh = _packet()
        fresh["research_packet_id"] = "research-new"
        fresh["source_baseline_id"] = "decision-new"
        fresh["software_baseline"]["baseline_id"] = "decision-new"
        return fresh


class RotatedBaselineService:
    async def today(self, user_id, *, refresh=True, now=None):
        return {
            "decision_id": "decision-new",
            "authority": "software_baseline",
            "is_final_decision": False,
            "summary": {},
        }


class EmptyProposalService:
    async def latest(self, user_id, *, research_packet_id=None):
        return None


def _services(*, authority_mode="codex_validated"):
    db = FakeDatabase()
    research = FakeResearchService(_packet())
    validator = DecisionValidationService(
        db=db,
        research_service=research,
        validation_ttl_seconds=60,
    )
    proposals = DecisionProposalService(
        db=db,
        research_service=research,
        validator=validator,
    )
    confirmations = DecisionConfirmationService(
        db=db,
        proposal_service=proposals,
        validation_service=validator,
        research_service=research,
        baseline_service=FakeBaselineService(),
        authority_mode=authority_mode,
    )
    return db, proposals, validator, confirmations


@pytest.mark.asyncio
async def test_submit_persists_one_idempotent_proposal_and_initial_validation():
    db, proposals, _validator, _confirmations = _services()

    first = await proposals.submit("owner-1", _proposal(), now=NOW)
    second = await proposals.submit("owner-1", _proposal(), now=NOW)

    assert first["proposal"]["proposal_id"] == second["proposal"]["proposal_id"]
    assert first["validation"]["status"] == "valid"
    assert len(db["codex_decision_proposals"].rows) == 1
    assert len(db["decision_validations"].rows) == 2


@pytest.mark.asyncio
async def test_invalid_proposal_is_audited_but_not_confirmable():
    _db, proposals, _validator, confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(quantity=200), now=NOW)

    assert submitted["validation"]["status"] == "invalid"
    with pytest.raises(DecisionWorkflowError) as error:
        await confirmations.confirm(
            "owner-1",
            submitted["proposal"]["proposal_id"],
            DecisionConfirmationInput(
                validation_id=submitted["validation"]["validation_id"],
                accepted=True,
            ),
            now=NOW,
        )
    assert error.value.code == "decision_validation_not_confirmable"


@pytest.mark.asyncio
async def test_cross_user_cannot_read_proposal():
    _db, proposals, _validator, _confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(), now=NOW)

    with pytest.raises(DecisionWorkflowError) as error:
        await proposals.get("owner-2", submitted["proposal"]["proposal_id"])

    assert error.value.code == "decision_proposal_not_found"


@pytest.mark.asyncio
async def test_expired_validation_cannot_be_accepted():
    db, proposals, _validator, confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(), now=NOW)
    db["decision_validations"].rows[0]["valid_until"] = (
        NOW - timedelta(seconds=1)
    ).isoformat()

    with pytest.raises(DecisionWorkflowError) as error:
        await confirmations.confirm(
            "owner-1",
            submitted["proposal"]["proposal_id"],
            DecisionConfirmationInput(
                validation_id=submitted["validation"]["validation_id"],
                accepted=True,
            ),
            now=NOW,
        )

    assert error.value.code == "decision_validation_expired"


@pytest.mark.asyncio
async def test_invalid_proposal_can_be_explicitly_rejected():
    _db, proposals, _validator, confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(quantity=200), now=NOW)

    result = await confirmations.confirm(
        "owner-1",
        submitted["proposal"]["proposal_id"],
        DecisionConfirmationInput(
            validation_id=submitted["validation"]["validation_id"],
            accepted=False,
            reason="风险边界不满足",
        ),
        now=NOW,
    )

    assert result["accepted"] is False


@pytest.mark.asyncio
async def test_workspace_promotes_only_validated_codex_proposal():
    _db, proposals, _validator, confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(), now=NOW)

    workspace = await confirmations.workspace("owner-1", refresh=False, now=NOW)

    assert workspace["authority_mode"] == "codex_validated"
    assert workspace["authority"] == "codex_validated"
    assert workspace["is_final_decision"] is True
    assert workspace["requires_user_confirmation"] is True
    assert (
        workspace["codex_proposal"]["proposal_id"]
        == submitted["proposal"]["proposal_id"]
    )


@pytest.mark.asyncio
async def test_shadow_mode_never_marks_codex_as_final():
    _db, proposals, _validator, confirmations = _services(
        authority_mode="codex_shadow"
    )
    await proposals.submit("owner-1", _proposal(), now=NOW)

    workspace = await confirmations.workspace("owner-1", refresh=False, now=NOW)

    assert workspace["authority"] == "software_baseline"
    assert workspace["is_final_decision"] is False


@pytest.mark.asyncio
async def test_workspace_rebuilds_research_when_latest_baseline_has_rotated():
    research = RotatedResearchService()
    confirmations = DecisionConfirmationService(
        db=FakeDatabase(),
        proposal_service=EmptyProposalService(),
        validation_service=object(),
        research_service=research,
        baseline_service=RotatedBaselineService(),
        authority_mode="codex_validated",
    )

    workspace = await confirmations.workspace("owner-1", refresh=False)

    assert workspace["software_baseline"]["decision_id"] == "decision-new"
    assert workspace["research_packet"]["source_baseline_id"] == "decision-new"
    assert research.today_calls == 1


class ChangedBaselineService:
    async def today(self, user_id, *, refresh=True, now=None):
        return {
            "decision_id": "decision-2",
            "authority": "software_baseline",
            "is_final_decision": False,
            "summary": {},
        }


@pytest.mark.asyncio
async def test_workspace_preserves_validated_proposal_when_baseline_rotates():
    db = FakeDatabase()
    research = FakeResearchService(_packet())
    validator = DecisionValidationService(
        db=db,
        research_service=research,
        validation_ttl_seconds=60,
    )
    proposals = DecisionProposalService(
        db=db,
        research_service=research,
        validator=validator,
    )
    submitted = await proposals.submit("owner-1", _proposal(), now=NOW)
    confirmations = DecisionConfirmationService(
        db=db,
        proposal_service=proposals,
        validation_service=validator,
        research_service=research,
        baseline_service=ChangedBaselineService(),
        authority_mode="codex_validated",
    )

    workspace = await confirmations.workspace(
        "owner-1",
        refresh=False,
        now=NOW + timedelta(minutes=2),
    )

    assert workspace["codex_proposal"]["proposal_id"] == (
        submitted["proposal"]["proposal_id"]
    )
    assert workspace["validation"]["validation_id"] == (
        submitted["validation"]["validation_id"]
    )
    assert workspace["research_packet"]["research_packet_id"] == "research-1"
    assert workspace["workflow_status"] == "proposal_revalidation_required"
    assert workspace["revalidation_required"] is True
    assert set(workspace["revalidation_reasons"]) == {
        "software_baseline_changed",
        "validation_expired",
    }
    assert workspace["authority"] == "software_baseline"
    assert workspace["primary_decision"] is None


@pytest.mark.asyncio
async def test_workspace_keeps_proposal_and_reports_missing_bound_research():
    db, proposals, _validator, confirmations = _services()
    submitted = await proposals.submit("owner-1", _proposal(), now=NOW)

    async def missing_research(_user_id, _research_packet_id):
        return None

    confirmations.research_service.get = missing_research
    workspace = await confirmations.workspace(
        "owner-1",
        refresh=False,
        now=NOW,
    )

    assert workspace["codex_proposal"]["proposal_id"] == (
        submitted["proposal"]["proposal_id"]
    )
    assert workspace["research_packet"] == {}
    assert workspace["revalidation_required"] is True
    assert "proposal_research_packet_missing" in workspace["revalidation_reasons"]
    assert workspace["authority"] == "software_baseline"
