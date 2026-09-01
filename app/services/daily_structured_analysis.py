"""Auditable daily structured-analysis completion contract."""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional


DAILY_STRUCTURED_ANALYSIS_MINIMUM = 100
_UNUSABLE_QUOTE_STATUSES = {
    "calendar_unknown",
    "future_provider_timestamp",
    "invalid",
    "invalid_price",
    "missing_trade_at",
    "not_live_session",
    "off_session",
    "quote_unavailable",
    "stale",
    "stale_trade_at",
    "unavailable",
    "unsupported_source",
    "wrong_trade_date",
}


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _date_from_timestamp(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _quote_trade_date(candidate: Mapping[str, Any]) -> Optional[str]:
    quote = _mapping(candidate.get("quote"))
    explicit = quote.get("trade_date")
    if isinstance(explicit, str) and explicit:
        return explicit
    return _date_from_timestamp(
        quote.get("trade_at")
        or candidate.get("trade_at")
        or quote.get("provider_updated_at")
    )


def _stage_status(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "missing"
    return str(value.get("status") or "missing")


def _candidate_completion(
    candidate: Mapping[str, Any],
    *,
    trade_date: str,
) -> Dict[str, Any]:
    quote = _mapping(candidate.get("quote"))
    structured = _mapping(candidate.get("structured_review"))
    technical = _mapping(structured.get("technical"))
    earnings = _mapping(structured.get("earnings"))
    notice = _mapping(structured.get("notice"))
    corporate_action = _mapping(
        structured.get("corporate_action") or candidate.get("corporate_action")
    )
    objective = _mapping(candidate.get("objective_profile"))
    price_plan = _mapping(candidate.get("price_plan"))
    account_fit = _mapping(
        candidate.get("account_fit") or candidate.get("position_sizing")
    )
    portfolio = _mapping(candidate.get("portfolio_allocation"))

    raw_freshness = quote.get("freshness")
    freshness = _mapping(raw_freshness)
    quote_status = str(
        freshness.get("status")
        or (raw_freshness if isinstance(raw_freshness, str) else None)
        or quote.get("status")
        or "ok"
    )
    quote_date = _quote_trade_date(candidate)
    missing: List[str] = []
    if not _positive_number(quote.get("price")):
        missing.append("quote_price_invalid")
    if quote_status in _UNUSABLE_QUOTE_STATUSES:
        missing.append("quote_evidence_unavailable")
    if quote_date != trade_date:
        missing.append("quote_trade_date_mismatch")
    if _stage_status(technical) != "passed":
        missing.append("technical_analysis_incomplete")
    if _stage_status(earnings) in {"missing", "unavailable"}:
        missing.append("earnings_evidence_unavailable")
    if _stage_status(notice) in {"missing", "unavailable"}:
        missing.append("notice_evidence_unavailable")
    if _stage_status(corporate_action) in {"missing", "unavailable", "corporate_action_unavailable"}:
        missing.append("corporate_action_evidence_unavailable")

    objective_complete = bool(
        objective.get("status") == "complete"
        or candidate.get("objective_tier") in {"core", "related", "non_core"}
    )
    if not objective_complete:
        missing.append("objective_classification_incomplete")
    if not (
        price_plan.get("status") in {"ok", "valid"}
        and _positive_number(price_plan.get("entry_price"))
        and _positive_number(
            price_plan.get("stop_price") or price_plan.get("stop_loss_price")
        )
        and _positive_number(price_plan.get("target_price"))
    ):
        missing.append("price_plan_incomplete")
    if not account_fit or str(account_fit.get("status") or "") in {
        "",
        "unavailable",
    }:
        missing.append("account_fit_incomplete")
    if not portfolio or not str(portfolio.get("status") or ""):
        missing.append("portfolio_risk_incomplete")

    sources = {
        "quote": quote.get("source"),
        "technical": technical.get("source"),
        "earnings": earnings.get("source"),
        "notice": notice.get("source"),
        "corporate_action": corporate_action.get("source"),
        "objective": objective.get("source") or candidate.get("objective_source"),
        "price_plan": price_plan.get("source") or "tencent_daily_bars",
        "account_fit": account_fit.get("source") or "account_policy",
        "portfolio_risk": portfolio.get("source") or "portfolio_allocator",
    }
    return {
        "code": str(candidate.get("code") or ""),
        "trade_date": trade_date,
        "status": "completed" if not missing else "incomplete",
        "research_tier": str(candidate.get("research_tier") or "structured"),
        "stage_sources": sources,
        "missing_reasons": missing,
        "hard_risk_status": structured.get("hard_risk_status"),
        "execution_actionable": candidate.get("execution_actionable") is True,
    }


def build_daily_structured_analysis(
    candidates: List[Mapping[str, Any]],
    *,
    discovery: Mapping[str, Any],
    trade_date: str,
    daily_minimum: int = DAILY_STRUCTURED_ANALYSIS_MINIMUM,
) -> Dict[str, Any]:
    """Summarise current-day completions without counting aging candidates."""

    current = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and str(candidate.get("rolling_pool_state") or "current") == "current"
    ]
    aging_not_counted = sum(
        isinstance(candidate, Mapping)
        and str(candidate.get("rolling_pool_state") or "current") != "current"
        for candidate in candidates
    )
    items = [
        _candidate_completion(candidate, trade_date=trade_date)
        for candidate in current
    ]
    completed = [item for item in items if item["status"] == "completed"]
    incomplete = [item for item in items if item["status"] == "incomplete"]
    reasons = Counter(
        reason
        for item in incomplete
        for reason in item.get("missing_reasons", [])
    )
    universe_count = int(discovery.get("universe_count") or 0)
    permission_excluded_count = int(
        discovery.get("permission_prefilter_excluded_count") or 0
    )
    permission_eligible_count = max(
        0, universe_count - permission_excluded_count
    )
    screening_eligible_count = int(discovery.get("eligible_count") or 0)
    return {
        "status": "complete" if len(completed) >= daily_minimum else "incomplete",
        "trade_date": trade_date,
        "daily_minimum": daily_minimum,
        "minimum_met": len(completed) >= daily_minimum,
        "universe_count": universe_count,
        "permission_prefilter_excluded_count": permission_excluded_count,
        "permission_eligible_count": permission_eligible_count,
        "researchable_count": permission_eligible_count,
        "screening_eligible_count": screening_eligible_count,
        "planned_count": min(daily_minimum, screening_eligible_count),
        "completed_count": len(completed),
        "incomplete_count": len(incomplete),
        "aging_not_counted": aging_not_counted,
        "formal_deep_count": sum(
            item.get("research_tier") == "deep" for item in items
        ),
        "structured_count": sum(
            item.get("research_tier") == "structured" for item in items
        ),
        "executable_count": sum(
            item.get("execution_actionable") is True for item in items
        ),
        "incomplete_reasons": dict(sorted(reasons.items())),
        "stage_sources": deepcopy(dict(discovery.get("stage_sources") or {})),
        "items": items,
    }


def apply_daily_analysis_execution_gate(document: Dict[str, Any]) -> None:
    audit = _mapping(document.get("daily_structured_analysis"))
    if audit.get("minimum_met") is True:
        return
    for candidate in document.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        candidate["execution_actionable"] = False
        candidate["condition_order_ready"] = False
        candidate["execution_status"] = "daily_structured_analysis_minimum_not_met"
    portfolio = document.get("portfolio_plan")
    if not isinstance(portfolio, dict):
        portfolio = {}
        document["portfolio_plan"] = portfolio
    portfolio["status"] = "research_only"
    portfolio["reason_code"] = "daily_structured_analysis_minimum_not_met"
    document["execution"] = {
        "actionable": False,
        "status": "daily_structured_analysis_minimum_not_met",
        "requires_daily_decision": True,
    }
