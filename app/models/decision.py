"""Strict request contracts for the governed Codex decision workflow."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROPOSAL_SCHEMA_VERSION = "codex-proposal-v1"
DEFAULT_PROMPT_VERSION = "codex-decision-v1"


class DecisionAction(str, Enum):
    BUY_NOW = "buy_now"
    CONDITION_ORDER = "condition_order"
    WAIT = "wait"
    AVOID = "avoid"


class DecisionScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_new_positions: int = Field(default=2, ge=0, le=10)
    primary_position_count: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def primary_count_fits_scope(self) -> "DecisionScope":
        if self.primary_position_count > self.max_new_positions:
            raise ValueError("primary_position_count cannot exceed max_new_positions")
        return self


class CodexDecisionOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning_code: str = Field(min_length=1)
    reason: str = Field(min_length=4)
    risk_adjustment: str = Field(min_length=1)


class CodexSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: DecisionAction
    position_role: Literal["primary", "secondary", "none"]
    requested_quantity: Optional[int] = Field(default=None, ge=0)
    entry_strategy: Optional[Literal["pullback", "breakout", "reference"]] = None
    trigger_price: Optional[Decimal] = Field(default=None, gt=0)
    stop_price: Optional[Decimal] = Field(default=None, gt=0)
    target_price: Optional[Decimal] = Field(default=None, gt=0)
    expires_at: Optional[datetime] = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    thesis: str = Field(min_length=4)
    evidence_refs: list[str] = Field(min_length=1)
    overrides: list[CodexDecisionOverride] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) != 6:
            raise ValueError("symbol must contain six digits")
        return digits

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("evidence_refs cannot contain empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_action_shape(self) -> "CodexSelection":
        actionable = self.action in {
            DecisionAction.BUY_NOW,
            DecisionAction.CONDITION_ORDER,
        }
        if actionable:
            required = (
                self.requested_quantity,
                self.entry_strategy,
                self.trigger_price,
                self.stop_price,
                self.target_price,
                self.expires_at,
            )
            if any(value is None for value in required) or not self.requested_quantity:
                raise ValueError(
                    "actionable selection requires quantity and price plan"
                )
            if self.position_role == "none":
                raise ValueError("actionable selection requires a position role")
            if not self.stop_price < self.trigger_price < self.target_price:
                raise ValueError(
                    "expected stop_price < trigger_price < target_price"
                )
        else:
            if self.requested_quantity not in (None, 0):
                raise ValueError("wait/avoid selection cannot carry executable quantity")
            if self.position_role != "none":
                raise ValueError("wait/avoid selection position_role must be none")
        return self


class CodexDecisionProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_packet_id: str = Field(min_length=1)
    proposal_schema_version: Literal["codex-proposal-v1"] = PROPOSAL_SCHEMA_VERSION
    decision_scope: DecisionScope = Field(default_factory=DecisionScope)
    selections: list[CodexSelection] = Field(default_factory=list)
    portfolio_rationale: str = Field(min_length=4)
    no_action_reason: Optional[str] = None
    prompt_version: str = Field(default=DEFAULT_PROMPT_VERSION, min_length=1)

    @model_validator(mode="after")
    def validate_portfolio_shape(self) -> "CodexDecisionProposalInput":
        if not self.selections and not str(self.no_action_reason or "").strip():
            raise ValueError("no_action_reason is required when selections is empty")

        symbols = [selection.symbol for selection in self.selections]
        if len(set(symbols)) != len(symbols):
            raise ValueError("each symbol may appear only once")

        actionable = [
            selection
            for selection in self.selections
            if selection.action
            in {DecisionAction.BUY_NOW, DecisionAction.CONDITION_ORDER}
        ]
        if len(actionable) > self.decision_scope.max_new_positions:
            raise ValueError("actionable selections exceed max_new_positions")

        primary_count = sum(
            selection.position_role == "primary" for selection in actionable
        )
        if primary_count > self.decision_scope.primary_position_count:
            raise ValueError("primary selections exceed primary_position_count")
        return self


class DecisionConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_id: str = Field(min_length=1)
    accepted: bool
    reason: Optional[str] = None
