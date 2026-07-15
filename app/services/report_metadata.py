"""Small, dependency-free helpers for persisted analysis report metadata."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict


def resolve_report_analysis_date(result: Dict[str, Any], *, generated_at: datetime) -> str:
    """Keep the market-data session supplied by analysis instead of generation day."""
    value = result.get("analysis_date")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            pass
    return generated_at.date().isoformat()
