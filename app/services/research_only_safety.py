"""Recursive output guardrails for research-only payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FALSE_KEYS = {
    "actionable",
    "reference_actionable",
    "new_position_allowed",
}

ZERO_KEYS = {
    "suggested_lots",
    "suggested_quantity",
    "new_position_lots",
    "new_position_quantity",
    "max_new_exposure_amount",
    "max_new_exposure_pct",
    "external_new_exposure_amount",
    "market_adjusted_new_exposure_cap",
}


def enforce_research_only_safety(value: Any) -> Any:
    """Sanitize an acyclic JSON-like tree without mutating the input."""
    if isinstance(value, Mapping):
        return {
            key: (
                False
                if key in FALSE_KEYS
                else 0
                if key in ZERO_KEYS
                else enforce_research_only_safety(nested)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [enforce_research_only_safety(item) for item in value]
    if isinstance(value, tuple):
        return tuple(enforce_research_only_safety(item) for item in value)
    return value
