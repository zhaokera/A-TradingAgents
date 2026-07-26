from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.decision import (
    CodexDecisionProposalInput,
    CodexSelection,
)


EXPIRES_AT = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)


def _selection(**updates):
    value = {
        "symbol": "SH.600406",
        "action": "condition_order",
        "position_role": "primary",
        "requested_quantity": 300,
        "entry_strategy": "pullback",
        "trigger_price": "21.20",
        "stop_price": "20.10",
        "target_price": "23.80",
        "expires_at": EXPIRES_AT,
        "confidence": 0.78,
        "thesis": "电网数字化基本面与回调价格计划匹配",
        "evidence_refs": ["600406:quote", "600406:plan:short"],
        "overrides": [],
    }
    value.update(updates)
    return value


def _proposal(**updates):
    value = {
        "research_packet_id": "research-1",
        "proposal_schema_version": "codex-proposal-v1",
        "decision_scope": {
            "max_new_positions": 2,
            "primary_position_count": 1,
        },
        "selections": [_selection()],
        "portfolio_rationale": "只配置一个主仓候选并保留现金",
        "no_action_reason": None,
        "prompt_version": "codex-decision-v1",
    }
    value.update(updates)
    return value


def test_actionable_selection_requires_complete_price_plan():
    raw = _selection()
    raw.pop("stop_price")

    with pytest.raises(ValidationError, match="quantity and price plan"):
        CodexSelection.model_validate(raw)


def test_valid_selection_normalizes_symbol_and_prices():
    selection = CodexSelection.model_validate(_selection())

    assert selection.symbol == "600406"
    assert selection.trigger_price == Decimal("21.20")
    assert selection.requested_quantity == 300


def test_actionable_selection_requires_ordered_prices():
    with pytest.raises(ValidationError, match="stop_price < trigger_price < target_price"):
        CodexSelection.model_validate(_selection(stop_price="21.30"))


def test_wait_selection_cannot_carry_executable_quantity():
    with pytest.raises(ValidationError, match="cannot carry executable quantity"):
        CodexSelection.model_validate(
            _selection(
                action="wait",
                position_role="none",
                requested_quantity=100,
                entry_strategy=None,
                trigger_price=None,
                stop_price=None,
                target_price=None,
                expires_at=None,
            )
        )


def test_empty_selection_requires_no_action_reason():
    with pytest.raises(ValidationError, match="no_action_reason"):
        CodexDecisionProposalInput.model_validate(
            _proposal(selections=[], no_action_reason=None)
        )


def test_proposal_rejects_more_actionable_positions_than_scope():
    with pytest.raises(ValidationError, match="max_new_positions"):
        CodexDecisionProposalInput.model_validate(
            _proposal(
                decision_scope={
                    "max_new_positions": 1,
                    "primary_position_count": 1,
                },
                selections=[
                    _selection(),
                    _selection(
                        symbol="601138",
                        position_role="secondary",
                        evidence_refs=["601138:quote"],
                    ),
                ],
            )
        )


def test_proposal_rejects_more_primary_positions_than_scope():
    with pytest.raises(ValidationError, match="primary_position_count"):
        CodexDecisionProposalInput.model_validate(
            _proposal(
                selections=[
                    _selection(),
                    _selection(symbol="601138", evidence_refs=["601138:quote"]),
                ]
            )
        )


def test_contract_forbids_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CodexDecisionProposalInput.model_validate(_proposal(untrusted_field=True))
