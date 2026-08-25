"""Deterministic daily decision packets and append-only snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.services.a_share_permissions import (
    classify_a_share_board,
    normalize_a_share_code,
    normalize_market_permissions,
    permission_for_code,
)
from app.services.ai_candidate_service import ai_candidate_service
from app.services.daily_briefing_service import daily_briefing_service
from app.services.decision_tracking_service import DecisionTrackingService
from app.services.investment_policy import classify_investment_objective
from app.services.market_session_policy_service import market_session_policy_service
from app.services.portfolio_diversification_service import (
    PortfolioDiversificationService,
)
from app.services.stock_master_data_service import StockMasterDataService


RULE_VERSION = "decision-v1"
TAXONOMY_VERSION = "cn-sector-v1"
DEFAULT_FEE_POLICY_VERSION = "cn_a_v1"
PROVIDER_VERSIONS = {
    "company_profile": "company-profile-adapters-v1",
    "market_quote": "tencent-quote-v1",
    "trading_calendar": "a-share-calendar-v1",
}
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
LIVE_PHASES = frozenset({"live_am", "live_pm"})
VALID_PHASES = frozenset(
    {
        "pre_open",
        "live_am",
        "midday_break",
        "live_pm",
        "post_close",
        "closed_day",
        "calendar_unknown",
    }
)
BUCKETS = ("avoid", "wait", "buy_now", "condition_order")
FORMAL_RESEARCH_CAPACITY = 15
ROLLING_POOL_CAPACITY = 100

AVOID_REASON_CODES = (
    "plan_invalidated",
    "plan_expired",
    "target_reached",
    "blocking_event",
    "market_red",
    "objective_mismatch",
    "hard_data_failure",
)
WAIT_REASON_CODES = (
    "current_candidate_scan_unavailable",
    "account_blocked",
    "calendar_unknown",
    "profile_incomplete",
    "one_lot_unaffordable",
    "holding_valuation_missing",
    "holding_taxonomy_missing",
    "concentration_limit",
    "correlation_limit",
    "loss_budget_exhausted",
)
CONDITION_ORDER_CAPABILITY_REASON = "condition_order_capability_unverified"
CONDITION_ORDER_PRICE_REASON = "condition_order_order_price_missing"

_TOP_LEVEL_VOLATILE_FIELDS = {
    "decision_id",
    "as_of",
    "briefing_as_of",
    "created_at",
    "persisted_at",
    "persistence",
}
_MONEY_OR_PERCENT_RE = re.compile(
    r"(?:price|amount|assets|cash|loss|pct|percent|exposure|score|value)$"
)
_SOURCE_PRIORITY = {"tushare": 0, "baostock": 1, "cninfo": 2, "akshare": 3}


class DecisionPersistenceError(RuntimeError):
    """Raised when a decision cannot be durably audited in MongoDB."""


def _normalise_code(value: Any) -> str:
    return normalize_a_share_code(value)


def _execution_capabilities(account: Mapping[str, Any]) -> Dict[str, Any]:
    raw = account.get("execution_capabilities")
    raw = raw if isinstance(raw, Mapping) else {}
    condition = raw.get("condition_order")
    condition = condition if isinstance(condition, Mapping) else {}
    verified = condition.get("verified") is True
    independent_trigger = (
        condition.get("independent_trigger_price_supported") is True
    )
    separate_limit = (
        condition.get("separate_order_limit_price_supported") is True
    )
    eligible = verified and independent_trigger and separate_limit
    market_permissions = raw.get("market_permissions")
    market_permissions = (
        market_permissions if isinstance(market_permissions, Mapping) else {}
    )
    normalized_market_permissions = normalize_market_permissions(
        market_permissions
    )
    excluded_codes = account.get("excluded_codes")
    excluded_codes = (
        excluded_codes
        if isinstance(excluded_codes, Iterable)
        and not isinstance(excluded_codes, (str, bytes, Mapping))
        else []
    )
    return {
        "source": str(raw.get("source") or "unverified").strip(),
        "condition_order": {
            "verified": verified,
            "independent_trigger_price_supported": independent_trigger,
            "separate_order_limit_price_supported": separate_limit,
            "eligible": eligible,
        },
        "market_permissions": normalized_market_permissions,
        "excluded_codes": sorted(
            {
                _normalise_code(code)
                for code in excluded_codes
                if re.fullmatch(r"\d{6}", _normalise_code(code))
            }
        ),
    }


def _permission_prefilter_reason(
    code: str,
    execution_capabilities: Mapping[str, Any],
) -> Optional[str]:
    if code in set(execution_capabilities.get("excluded_codes") or []):
        return "user_excluded"
    market_permissions = execution_capabilities.get("market_permissions")
    permission = permission_for_code(code, market_permissions)
    return permission.get("exclusion_reason_code")


def _candidate_formal_research_order(
    candidate: Mapping[str, Any],
) -> tuple[int, int, Decimal, Decimal, Decimal, str]:
    state = str(candidate.get("rolling_pool_state") or "current")
    hard_blocked = _candidate_has_hard_risk(candidate)
    objective_order = {
        "core": 0,
        "adjacent": 1,
        "non_core": 2,
    }.get(str(candidate.get("objective_tier") or ""), 1)
    plan = candidate.get("price_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    distance = _finite_decimal(plan.get("distance_to_entry_pct"))
    rank_score = _finite_decimal(candidate.get("rank_score")) or Decimal(0)
    rank = _finite_decimal(candidate.get("rank")) or Decimal("Infinity")
    return (
        1 if state in {"expired", "invalidated"} or hard_blocked else 0,
        objective_order,
        abs(distance) if distance is not None else Decimal("Infinity"),
        -rank_score,
        rank,
        _normalise_code(candidate.get("code")),
    )


def _candidate_has_hard_risk(candidate: Mapping[str, Any]) -> bool:
    structured = candidate.get("structured_review")
    structured = structured if isinstance(structured, Mapping) else {}
    return bool(
        candidate.get("earnings_risk_blocked") is True
        or candidate.get("notice_risk_blocked") is True
        or structured.get("hard_risk_clear") is False
    )


def _select_formal_research_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    capacity: int = FORMAL_RESEARCH_CAPACITY,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    ordered = sorted(
        (dict(item) for item in candidates),
        key=_candidate_formal_research_order,
    )
    selected: list[Dict[str, Any]] = []
    audit: list[Dict[str, Any]] = []
    for candidate in ordered:
        state = str(candidate.get("rolling_pool_state") or "current")
        hard_blocked = _candidate_has_hard_risk(candidate)
        selectable = state not in {"expired", "invalidated"} and not hard_blocked
        chosen = selectable and len(selected) < max(0, capacity)
        if chosen:
            selected.append(candidate)
        structured = candidate.get("structured_review")
        structured = structured if isinstance(structured, Mapping) else {}
        audit.append(
            {
                "code": _normalise_code(candidate.get("code")),
                "name": str(candidate.get("name") or candidate.get("code") or ""),
                "lifecycle_state": state,
                "age_trading_days": candidate.get("rolling_age_trading_days", 0),
                "discovery_research_tier": candidate.get("research_tier"),
                "objective_tier": candidate.get("objective_tier"),
                "rank": candidate.get("rank"),
                "rank_score": candidate.get("rank_score"),
                "distance_to_entry_pct": (
                    (candidate.get("price_plan") or {}).get("distance_to_entry_pct")
                    if isinstance(candidate.get("price_plan"), Mapping)
                    else None
                ),
                "hard_risk_clear": structured.get("hard_risk_clear"),
                "selected_for_formal_research": chosen,
                "selection_reason": (
                    "dynamic_formal_research_selected"
                    if chosen
                    else (
                        "rolling_candidate_not_active"
                        if not selectable
                        and not hard_blocked
                        else (
                            "structured_hard_risk_blocked"
                            if hard_blocked
                            else "outside_dynamic_formal_research_tier"
                        )
                    )
                ),
            }
        )
    return selected, audit


def _as_shanghai(value: Any) -> datetime:
    if value is None:
        parsed = datetime.now(SHANGHAI_TIMEZONE)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError("now must be a datetime-compatible value")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed.astimezone(SHANGHAI_TIMEZONE)


def _finite_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _round_half_up(value: Any, places: int = 2) -> Any:
    parsed = _finite_decimal(value)
    if parsed is None:
        return value
    quantizer = Decimal(1).scaleb(-places)
    return float(parsed.quantize(quantizer, rounding=ROUND_HALF_UP))


def _normalise_numbers(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_numbers(item, (*path, str(key)))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_numbers(item, (*path, "[]")) for item in value]
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        key = path[-1].lower() if path else ""
        if "correlation" in ".".join(path).lower() and key in {"value", "cap"}:
            return _round_half_up(value, 4)
        if _MONEY_OR_PERCENT_RE.search(key) or key in {
            "entry",
            "stop",
            "target",
            "rank_score",
        }:
            return _round_half_up(value, 2)
        return _round_half_up(value, 6)
    return value


def _canonical_sanitise(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if not path and key in _TOP_LEVEL_VOLATILE_FIELDS:
                continue
            if key == "retrieved_at" and any(
                segment.endswith("_evidence")
                or segment
                in {
                    "evidence",
                    "revenue_composition",
                    "profile_conflicts",
                    "provider_errors",
                    "display_only",
                }
                for segment in path
            ):
                continue
            if key == "quote_checked_at" and any(
                segment in {"quote", "transport"} for segment in path
            ):
                continue
            if key in {"age_seconds", "event_age_seconds"} and "quote" in path:
                continue
            result[key] = _canonical_sanitise(item, (*path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_sanitise(item, (*path, "[]")) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def material_hash(packet: Mapping[str, Any]) -> str:
    """Hash only material decision state using canonical UTF-8 JSON."""

    canonical = _normalise_numbers(_canonical_sanitise(packet))
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decision_state_hash(packet: Mapping[str, Any]) -> str:
    """Hash decision semantics while excluding quote transport observations."""

    market = packet.get("market")
    market = market if isinstance(market, Mapping) else {}
    payload: Dict[str, Any] = {
        "decision_date": packet.get("decision_date"),
        "market_phase": packet.get("market_phase"),
        "candidate_run_id": packet.get("candidate_run_id"),
        "authority": packet.get("authority"),
        "is_final_decision": packet.get("is_final_decision"),
        "summary": packet.get("summary"),
        "market": {
            key: market.get(key)
            for key in (
                "combined_regime",
                "domestic_regime",
                "global_regime",
            )
        },
        "permission_prefilter_excluded": packet.get(
            "permission_prefilter_excluded"
        ),
    }
    for bucket in BUCKETS:
        values = []
        for raw in packet.get(bucket, []) or []:
            if not isinstance(raw, Mapping):
                continue
            identity = raw.get("identity")
            identity = identity if isinstance(identity, Mapping) else {}
            execution = raw.get("execution")
            execution = execution if isinstance(execution, Mapping) else {}
            allocation = raw.get("allocation")
            allocation = allocation if isinstance(allocation, Mapping) else {}
            invalidation = raw.get("invalidation")
            invalidation = (
                invalidation if isinstance(invalidation, Mapping) else {}
            )
            values.append(
                {
                    "code": _normalise_code(identity.get("code")),
                    "plan_id": raw.get("plan_id"),
                    "action": raw.get("action"),
                    "reason_codes": raw.get("reason_codes"),
                    "execution": {
                        key: execution.get(key)
                        for key in ("status", "order_limit_price")
                    },
                    "allocation": {
                        key: allocation.get(key)
                        for key in (
                            "status",
                            "reason",
                            "reason_codes",
                            "quantity",
                            "amount",
                            "position_pct",
                        )
                    },
                    "planned_loss": raw.get("planned_loss"),
                    "invalidation": {
                        "stop_price": invalidation.get("stop_price"),
                        "plan_expires_at": invalidation.get(
                            "plan_expires_at"
                        ),
                        "risk_flags": invalidation.get("risk_flags"),
                    },
                }
            )
        payload[bucket] = values
    encoded = json.dumps(
        _normalise_numbers(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_plan_id(
    *, user_id: str, candidate_run_id: Any, candidate: Mapping[str, Any], allocation: Mapping[str, Any]
) -> str:
    plan = candidate.get("price_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    payload = {
        "user_id": str(user_id),
        "candidate_run_id": str(candidate_run_id or ""),
        "code": _normalise_code(candidate.get("code")),
        "entry_strategy": plan.get("entry_strategy"),
        "entry_price": plan.get("entry_price"),
        "stop_price": plan.get("stop_price"),
        "target_price": plan.get("target_price"),
        "plan_expires_at": candidate.get("plan_expires_at"),
        "allocation": {
            key: allocation.get(key)
            for key in ("status", "quantity", "amount", "position_pct")
        },
        "rule_version": RULE_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(
            _normalise_numbers(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"plan_{digest}"


def _calibration_features(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    bucket: str,
) -> Dict[str, float]:
    objective_match = _finite_decimal(candidate.get("objective_match_score"))
    objective_value = min(Decimal("1"), max(Decimal("0"), objective_match or Decimal("0")))

    reward_risk = _finite_decimal(candidate.get("reward_risk_ratio"))
    if reward_risk is None:
        entry = _finite_decimal(plan.get("entry_price"))
        stop = _finite_decimal(plan.get("stop_price"))
        target = _finite_decimal(plan.get("target_price"))
        if entry is not None and stop is not None and target is not None and entry > stop:
            reward_risk = (target - entry) / (entry - stop)
    reward_value = min(
        Decimal("1"),
        max(Decimal("0"), (reward_risk or Decimal("0")) / Decimal("3")),
    )

    quality = profile.get("data_quality")
    quality = quality if isinstance(quality, Mapping) else {}
    required_fields = ("provider_sector", "industry", "main_business")
    present = sum(bool(profile.get(field)) for field in required_fields)
    evidence_value = Decimal(present) / Decimal(len(required_fields))
    if quality.get("complete") is True:
        evidence_value = Decimal("1")

    action_value = {
        "buy_now": Decimal("1"),
        "condition_order": Decimal("0.75"),
        "wait": Decimal("0.25"),
        "avoid": Decimal("0"),
    }.get(bucket, Decimal("0"))
    return {
        "objective_match": float(objective_value),
        "reward_risk": float(reward_value),
        "evidence_completeness": float(evidence_value),
        "actionability": float(action_value),
    }


def _dedupe_ordered(values: Iterable[str], order: Sequence[str]) -> list[str]:
    present = {str(value) for value in values if value}
    return [value for value in order if value in present]


def _is_blocking_flag(flag: Mapping[str, Any]) -> bool:
    return str(flag.get("severity") or flag.get("level") or "").lower() in {
        "critical",
        "high",
        "error",
        "blocking",
        "block",
        "blocker",
        "blocked",
    } or str(flag.get("code") or "").lower() in {
        "blocking_event",
        "suspension",
        "suspended",
        "delisted",
        "delisting_risk",
        "regulatory_investigation",
        "corporate_action",
        "corporate_action_blocked",
        "technical_deep_check_timeout",
        "earnings_risk_blocked",
        "notice_risk_blocked",
    }


def _plan_has_expired(value: Any, now: datetime) -> bool:
    if not value:
        return False
    try:
        return _as_shanghai(value) <= _as_shanghai(now)
    except (TypeError, ValueError):
        return False


def _profile_complete(profile: Mapping[str, Any]) -> bool:
    quality = profile.get("data_quality")
    if isinstance(quality, Mapping):
        if quality.get("decision_critical_complete") is True:
            return True
        if quality.get("decision_critical_missing_fields"):
            return False
    return all(
        str(profile.get(field) or "").strip()
        for field in ("provider_sector", "industry")
    )


def _stable_profile(profile: Mapping[str, Any]) -> Dict[str, Any]:
    result = deepcopy(dict(profile))
    revenue = result.get("revenue_composition")
    if isinstance(revenue, Mapping):
        revenue = deepcopy(dict(revenue))
        items = [
            dict(item)
            for item in revenue.get("items", [])
            if isinstance(item, Mapping)
        ]

        def revenue_key(item: Mapping[str, Any]) -> tuple[int, str, str]:
            period = str(item.get("report_period") or revenue.get("report_period") or "")
            digits = re.sub(r"\D", "", period)
            return (
                -int(digits or 0),
                str(item.get("composition_type") or item.get("type") or ""),
                str(item.get("name") or item.get("item_name") or ""),
            )

        revenue["items"] = sorted(items, key=revenue_key)
        result["revenue_composition"] = revenue

    quality = result.get("data_quality")
    if isinstance(quality, Mapping):
        quality = deepcopy(dict(quality))

        def evidence_error_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
            return (
                str(item.get("field") or ""),
                _SOURCE_PRIORITY.get(str(item.get("source") or "").lower(), 99),
                str(item.get("source_endpoint") or ""),
                str(item.get("error_code") or ""),
                str(item.get("source_record_key") or ""),
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
            )

        for field in ("profile_conflicts", "provider_errors", "display_only"):
            values = [
                dict(item)
                for item in quality.get(field, [])
                if isinstance(item, Mapping)
            ]
            quality[field] = sorted(values, key=evidence_error_key)
        result["data_quality"] = quality

    evidence = [
        dict(item)
        for item in result.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    if evidence:
        result["evidence"] = sorted(
            evidence,
            key=lambda item: (
                str(item.get("field") or ""),
                _SOURCE_PRIORITY.get(str(item.get("source") or "").lower(), 99),
                str(item.get("source_endpoint") or ""),
                str(item.get("source_record_key") or ""),
            ),
        )
    return result


def _allocation_wait_reason(allocation: Mapping[str, Any]) -> Optional[str]:
    reasons = [
        str(value)
        for value in allocation.get("reason_codes", [])
        if value
    ]
    reason = str(allocation.get("reason") or "")
    if reason:
        reasons.append(reason)
    mapping = {
        "account_blocked": "account_blocked",
        "invalid_portfolio_policy": "account_blocked",
        "price_plan_or_account_unavailable": "account_blocked",
        "calendar_unknown": "calendar_unknown",
        "profile_incomplete": "profile_incomplete",
        "candidate_taxonomy_missing": "profile_incomplete",
        "one_lot_unaffordable": "one_lot_unaffordable",
        "shared_capital_budget_exhausted": "one_lot_unaffordable",
        "holding_valuation_missing": "holding_valuation_missing",
        "holding_valuation_invalid": "holding_valuation_missing",
        "holding_quote_trade_at_missing": "holding_valuation_missing",
        "holding_quote_stale": "holding_valuation_missing",
        "holding_valuation_phase_missing": "holding_valuation_missing",
        "holding_valuation_phase_mismatch": "holding_valuation_missing",
        "holding_denominator_missing": "holding_valuation_missing",
        "holding_denominator_mismatch": "holding_valuation_missing",
        "holding_taxonomy_missing": "holding_taxonomy_missing",
        "concentration_limit": "concentration_limit",
        "hard_single_symbol_cap": "concentration_limit",
        "correlation_limit": "correlation_limit",
        "loss_budget_exhausted": "loss_budget_exhausted",
        "shared_loss_budget_exhausted": "loss_budget_exhausted",
    }
    for code in WAIT_REASON_CODES:
        if code in reasons:
            return code
    for value in reasons:
        if value in mapping:
            return mapping[value]
    return None


def _serialize_mongo(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _serialize_mongo(item)
            for key, item in value.items()
            if str(key) != "_id"
        }
    if isinstance(value, list):
        return [_serialize_mongo(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class DailyDecisionService:
    """Compose, classify, hash, and persist one daily decision read model."""

    def __init__(
        self,
        *,
        briefing_service: Any = None,
        candidate_service: Any = None,
        candidate_loader: Optional[Callable[..., Awaitable[Any] | Any]] = None,
        market_session_policy: Any = None,
        profile_resolver: Any = None,
        diversification_service: Any = None,
        tracking_service: Any = None,
        db: Any = None,
        duplicate_key_errors: tuple[type[BaseException], ...] = (DuplicateKeyError,),
        lease_seconds: int = 15,
        lease_wait_attempts: int = 40,
        lease_wait_seconds: float = 0.025,
    ) -> None:
        self.briefing_service = briefing_service or daily_briefing_service
        self.candidate_service = candidate_service or ai_candidate_service
        self.candidate_loader = candidate_loader
        self.market_session_policy = (
            market_session_policy or market_session_policy_service
        )
        self.profile_resolver = profile_resolver or StockMasterDataService()
        self.diversification_service = (
            diversification_service or PortfolioDiversificationService()
        )
        self.tracking_service = tracking_service or DecisionTrackingService(db=db)
        self.db = db
        self.duplicate_key_errors = duplicate_key_errors
        self.lease_seconds = lease_seconds
        self.lease_wait_attempts = lease_wait_attempts
        self.lease_wait_seconds = lease_wait_seconds

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        if inspect.isawaitable(self.db):
            self.db = await self.db
        return self.db

    async def _latest_candidate_run(self, user_id: str, refresh: bool) -> Mapping[str, Any]:
        if self.candidate_loader is not None:
            result = self.candidate_loader(user_id, refresh=refresh)
        else:
            result = self.candidate_service.latest(
                user_id,
                refresh_quotes=refresh,
            )
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, Mapping) else {}

    @staticmethod
    def _candidate_quote(candidate: Mapping[str, Any]) -> Dict[str, Any]:
        quote = candidate.get("quote")
        quote = dict(quote) if isinstance(quote, Mapping) else {}
        source = quote.get("source") or quote.get("data_source")
        trade_at = quote.get("trade_at")
        price = quote.get("price") or quote.get("close")
        result = {
            **quote,
            "price": price,
            "source": str(source or "unknown").strip().lower(),
            "trade_at": trade_at,
            "quote_checked_at": quote.get("quote_checked_at"),
        }
        if "quote_fresh" in candidate:
            result["fresh"] = bool(candidate.get("quote_fresh"))
        return result

    @staticmethod
    def _explicit_reason_codes(candidate: Mapping[str, Any]) -> list[str]:
        values: list[str] = []
        for field in ("decision_reason_codes", "avoid_reason_codes", "wait_reason_codes"):
            raw = candidate.get(field)
            if isinstance(raw, str):
                values.append(raw)
            elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, Mapping)):
                values.extend(str(value) for value in raw if value)
        return values

    @staticmethod
    def _avoid_reasons(
        candidate: Mapping[str, Any],
        *,
        market: Mapping[str, Any],
        now: datetime,
    ) -> list[str]:
        reasons = DailyDecisionService._explicit_reason_codes(candidate)
        plan = candidate.get("price_plan")
        plan = plan if isinstance(plan, Mapping) else {}
        actionability = str(candidate.get("actionability") or "")
        if actionability == "invalidated" or plan.get("entry_status") == "invalidated":
            reasons.append("plan_invalidated")
        if (
            candidate.get("plan_expired") is True
            or actionability == "expired"
            or _plan_has_expired(candidate.get("plan_expires_at"), now)
        ):
            reasons.append("plan_expired")
        performance = candidate.get("performance")
        if actionability == "target_reached" or (
            isinstance(performance, Mapping) and performance.get("target_hit_at")
        ):
            reasons.append("target_reached")
        risk_flags = candidate.get("risk_flags")
        if isinstance(risk_flags, list) and any(
            isinstance(flag, Mapping) and _is_blocking_flag(flag) for flag in risk_flags
        ):
            reasons.append("blocking_event")
        regimes = {
            str(value or "").strip().lower()
            for value in (
                candidate.get("market_regime"),
                market.get("combined_regime"),
                market.get("domestic_regime"),
                market.get("regime"),
            )
        }
        if candidate.get("market_red") is True or "red" in regimes:
            reasons.append("market_red")
        if candidate.get("objective_mismatch") is True or str(
            candidate.get("objective_tier") or ""
        ) == "non_core":
            reasons.append("objective_mismatch")
        if candidate.get("hard_data_failure") is True:
            reasons.append("hard_data_failure")
        return _dedupe_ordered(reasons, AVOID_REASON_CODES)

    @staticmethod
    def _wait_reasons(
        candidate: Mapping[str, Any],
        *,
        session: Mapping[str, Any],
        briefing: Mapping[str, Any],
        profile: Mapping[str, Any],
        allocation: Mapping[str, Any],
        current_scan_available: bool = True,
    ) -> list[str]:
        reasons = DailyDecisionService._explicit_reason_codes(candidate)
        if not current_scan_available:
            reasons.append("current_candidate_scan_unavailable")
        account = briefing.get("account")
        account = account if isinstance(account, Mapping) else {}
        total_assets = _finite_decimal(account.get("total_assets"))
        cash = _finite_decimal(account.get("available_cash"))
        quality = briefing.get("data_quality")
        quality = quality if isinstance(quality, Mapping) else {}
        if (
            total_assets is None
            or total_assets <= 0
            or cash is None
            or quality.get("account") == "blocked"
        ):
            reasons.append("account_blocked")
        if session.get("phase") == "calendar_unknown":
            reasons.append("calendar_unknown")
        if not _profile_complete(profile):
            reasons.append("profile_incomplete")
        allocation_reason = _allocation_wait_reason(allocation)
        if allocation.get("status") != "allocated":
            reasons.append(allocation_reason or "account_blocked")
        return _dedupe_ordered(reasons, WAIT_REASON_CODES)

    @staticmethod
    def _augment_profile(
        candidate: Mapping[str, Any], profile: Mapping[str, Any]
    ) -> Dict[str, Any]:
        result = _stable_profile(profile)
        result.setdefault("code", _normalise_code(candidate.get("code")))
        result.setdefault("name", candidate.get("name") or result["code"])
        return result

    @staticmethod
    def _objective(candidate: Mapping[str, Any], profile: Mapping[str, Any]) -> Dict[str, Any]:
        objective = classify_investment_objective(
            candidate.get("code"),
            candidate.get("name") or profile.get("name"),
            industry=profile.get("industry"),
        )
        for field in ("objective_tier", "objective_segment", "objective_match_score", "objective_reason"):
            if candidate.get(field) is not None:
                objective[field] = candidate.get(field)
        return objective

    async def _compose_packet(
        self,
        user_id: str,
        *,
        refresh: bool,
        now: datetime,
    ) -> Dict[str, Any]:
        session = await self.market_session_policy.classify(now=now)
        phase = str(session.get("phase") or "calendar_unknown")
        if phase not in VALID_PHASES:
            phase = "calendar_unknown"
            session = {**dict(session), "phase": phase, "buy_now_allowed": False}

        candidate_run = await self._latest_candidate_run(user_id, refresh)
        briefing = await self.briefing_service.build(user_id, refresh=False)
        briefing = dict(briefing) if isinstance(briefing, Mapping) else {}
        account = briefing.get("account")
        account = dict(account) if isinstance(account, Mapping) else {}
        execution_capabilities = _execution_capabilities(account)
        raw_candidates = [
            dict(item)
            for item in candidate_run.get("candidates", [])
            if isinstance(item, Mapping)
        ]
        raw_run_permission_excluded = candidate_run.get(
            "permission_prefilter_excluded"
        )
        raw_run_permission_excluded = (
            raw_run_permission_excluded
            if isinstance(raw_run_permission_excluded, list)
            else []
        )
        permission_prefilter_excluded: list[Dict[str, str]] = [
            {
                "code": _normalise_code(item.get("code")),
                "name": str(item.get("name") or item.get("code") or ""),
                "board": str(item.get("board") or "A_SHARE"),
                "reason_code": str(
                    item.get("reason_code") or "governance_excluded"
                ),
            }
            for item in raw_run_permission_excluded
            if isinstance(item, Mapping)
            and re.fullmatch(r"\d{6}", _normalise_code(item.get("code")))
        ]
        permission_audit_keys = {
            (item["code"], item["reason_code"])
            for item in permission_prefilter_excluded
        }
        permitted_candidates: list[Dict[str, Any]] = []
        for candidate in raw_candidates:
            code = _normalise_code(candidate.get("code"))
            permission_reason = _permission_prefilter_reason(
                code,
                execution_capabilities,
            )
            if permission_reason:
                audit_key = (code, permission_reason)
                if audit_key not in permission_audit_keys:
                    permission_prefilter_excluded.append(
                        {
                            "code": code,
                            "name": str(candidate.get("name") or code),
                            "board": classify_a_share_board(code)["board"],
                            "reason_code": permission_reason,
                        }
                    )
                    permission_audit_keys.add(audit_key)
                continue
            permitted_candidates.append(candidate)
        rolling_candidates = permitted_candidates[:ROLLING_POOL_CAPACITY]
        raw_candidates, rolling_candidate_audit = (
            _select_formal_research_candidates(rolling_candidates)
        )
        holdings_payload = briefing.get("holdings")
        holdings_payload = holdings_payload if isinstance(holdings_payload, Mapping) else {}
        raw_holdings = [
            dict(item)
            for item in holdings_payload.get("items", [])
            if isinstance(item, Mapping)
        ]
        codes = list(
            dict.fromkeys(
                _normalise_code(item.get("code") or item.get("stock_code"))
                for item in [*raw_candidates, *raw_holdings]
                if _normalise_code(item.get("code") or item.get("stock_code"))
            )
        )
        profiles = self.profile_resolver.resolve_many(codes, refresh=refresh)
        if inspect.isawaitable(profiles):
            profiles = await profiles
        profiles = profiles if isinstance(profiles, Mapping) else {}

        enriched_candidates: list[Dict[str, Any]] = []
        for raw in raw_candidates:
            code = _normalise_code(raw.get("code"))
            profile = self._augment_profile(raw, profiles.get(code) or {})
            objective = self._objective(raw, profile)
            enriched_candidates.append(
                {
                    **raw,
                    **objective,
                    "code": code,
                    "provider_sector": profile.get("provider_sector"),
                    "industry": profile.get("industry"),
                    "stock_profile": profile,
                    "plan_expires_at": raw.get("plan_expires_at")
                    or candidate_run.get("plan_expires_at"),
                }
            )

        condition_order_capability = execution_capabilities["condition_order"]
        total_assets = account.get("total_assets")
        available_cash = account.get("available_cash")
        holdings: list[Dict[str, Any]] = []
        for raw in raw_holdings:
            code = _normalise_code(raw.get("code") or raw.get("stock_code"))
            profile = self._augment_profile(raw, profiles.get(code) or {})
            objective = self._objective(raw, profile)
            holdings.append(
                {
                    **raw,
                    **objective,
                    "code": code,
                    "provider_sector": profile.get("provider_sector"),
                    "industry": profile.get("industry"),
                    "valuation_phase": phase,
                    "total_assets_denominator": total_assets,
                }
            )

        old_portfolio = candidate_run.get("portfolio_plan")
        old_portfolio = old_portfolio if isinstance(old_portfolio, Mapping) else {}
        policy = old_portfolio.get("policy")
        policy = deepcopy(dict(policy)) if isinstance(policy, Mapping) else {}
        diversification = await self.diversification_service.allocate(
            enriched_candidates,
            holdings=holdings,
            total_assets=total_assets,
            available_cash=available_cash,
            policy=policy,
            market_phase=phase,
            as_of=now,
        )
        diversification = (
            dict(diversification) if isinstance(diversification, Mapping) else {}
        )
        allocation_map = {
            _normalise_code(item.get("code")): dict(item)
            for item in diversification.get("allocations", [])
            if isinstance(item, Mapping)
        }
        effective_policy = {
            **policy,
            **(
                dict(diversification.get("effective_limits"))
                if isinstance(diversification.get("effective_limits"), Mapping)
                else {}
            ),
            "quote_freshness_required_seconds": session.get(
                "quote_freshness_required_seconds"
            ),
            "rule_version": RULE_VERSION,
            "taxonomy_version": policy.get("taxonomy_version")
            or TAXONOMY_VERSION,
            "fee_policy_version": policy.get("fee_policy_version")
            or DEFAULT_FEE_POLICY_VERSION,
            "provider_versions": deepcopy(PROVIDER_VERSIONS),
        }

        briefing_market = briefing.get("market")
        market = (
            deepcopy(dict(briefing_market))
            if isinstance(briefing_market, Mapping)
            else {}
        )
        candidate_market = candidate_run.get("market")
        candidate_market = (
            candidate_market if isinstance(candidate_market, Mapping) else {}
        )
        live_gate = candidate_market.get("live_gate")
        if isinstance(live_gate, Mapping) and live_gate.get("usable") is True:
            market = deepcopy(dict(candidate_market))
            market["combined_regime"] = str(
                candidate_market.get("regime")
                or candidate_market.get("combined_regime")
                or candidate_market.get("domestic_regime")
                or "red"
            )
        market["session"] = phase
        market["is_trading_hours"] = phase in LIVE_PHASES
        live_gate_trading = bool(
            isinstance(live_gate, Mapping)
            and live_gate.get("usable") is True
            and live_gate.get("is_trading_hours") is True
        )
        current_scan_available = (
            candidate_run.get("current_scan_available") is not False
        )
        execution_usable = (
            phase in LIVE_PHASES
            and live_gate_trading
            and current_scan_available
        )
        market["execution_usable"] = execution_usable
        if execution_usable:
            market["execution_status"] = "live_market_gate_usable"
        elif phase in LIVE_PHASES and not current_scan_available:
            market["execution_status"] = "current_candidate_scan_unavailable"
        else:
            market["execution_status"] = (
                "research_snapshot_not_execution_decision"
            )
        bucket_items: Dict[str, list[Dict[str, Any]]] = {name: [] for name in BUCKETS}
        profile_errors: list[Any] = []
        profile_conflicts: list[Any] = []
        for candidate in enriched_candidates:
            code = _normalise_code(candidate.get("code"))
            profile = candidate.get("stock_profile")
            profile = profile if isinstance(profile, Mapping) else {}
            quality = profile.get("data_quality")
            quality = quality if isinstance(quality, Mapping) else {}
            profile_errors.extend(quality.get("provider_errors") or [])
            profile_conflicts.extend(quality.get("profile_conflicts") or [])
            allocation = allocation_map.get(code) or {
                "status": "wait",
                "reason": "account_blocked",
                "reason_codes": ["account_blocked"],
                "quantity": 0,
                "amount": 0.0,
                "planned_loss_amount": 0.0,
            }
            quote = self._candidate_quote(candidate)
            quote_status = await self.market_session_policy.quote_status(
                quote,
                now=now,
                session=session,
            )
            plan = candidate.get("price_plan")
            plan = dict(plan) if isinstance(plan, Mapping) else {}
            quote_price = _finite_decimal(quote.get("price"))
            stop_price = _finite_decimal(plan.get("stop_price"))
            has_live_price = quote_price is not None and quote_price > 0
            avoid_reasons = self._avoid_reasons(candidate, market=market, now=now)
            if (
                quote_status.get("actionable") is True
                and has_live_price
                and stop_price is not None
                and quote_price <= stop_price
            ):
                avoid_reasons = _dedupe_ordered(
                    [*avoid_reasons, "plan_invalidated"],
                    AVOID_REASON_CODES,
                )
            wait_reasons = self._wait_reasons(
                candidate,
                session=session,
                briefing=briefing,
                profile=profile,
                allocation=allocation,
                current_scan_available=(
                    candidate_run.get("current_scan_available") is not False
                ),
            )
            if avoid_reasons:
                bucket = "avoid"
                reasons = avoid_reasons
            elif wait_reasons:
                bucket = "wait"
                reasons = wait_reasons
            elif (
                phase in LIVE_PHASES
                and quote_status.get("actionable") is True
                and has_live_price
                and plan.get("price_condition_met") is True
                and allocation.get("status") == "allocated"
            ):
                bucket = "buy_now"
                reasons = ["live_price_condition_met"]
            elif (
                quote_status.get("actionable") is not True
                or not has_live_price
            ):
                bucket = "wait"
                reasons = ["live_quote_recheck_required"]
            elif plan.get("price_condition_met") is not True:
                if condition_order_capability.get("eligible") is not True:
                    bucket = "wait"
                    reasons = [CONDITION_ORDER_CAPABILITY_REASON]
                elif _finite_decimal(plan.get("order_limit_price")) is None:
                    bucket = "wait"
                    reasons = [CONDITION_ORDER_PRICE_REASON]
                else:
                    bucket = "condition_order"
                    reasons = ["valid_allocated_plan", "entry_condition_not_met"]
            else:
                bucket = "wait"
                reasons = ["live_quote_recheck_required"]

            plans = candidate.get("plans")
            plans = plans if isinstance(plans, Mapping) else {}
            plan_id = _stable_plan_id(
                user_id=user_id,
                candidate_run_id=candidate_run.get("run_id"),
                candidate=candidate,
                allocation=allocation,
            )
            item = {
                "identity": {
                    "code": code,
                    "name": candidate.get("name") or profile.get("name") or code,
                    "market": candidate.get("market") or "A股",
                    "rank": candidate.get("rank"),
                    "rank_score": candidate.get("rank_score"),
                    "objective_tier": candidate.get("objective_tier"),
                    "objective_segment": candidate.get("objective_segment"),
                    "objective_match_score": candidate.get("objective_match_score"),
                },
                "action": bucket,
                "reason_codes": reasons,
                "calibration_features": _calibration_features(
                    candidate,
                    profile,
                    plan,
                    bucket,
                ),
                "quote": {
                    **quote,
                    "status": quote_status.get("status"),
                    "actionable": quote_status.get("actionable"),
                    "age_seconds": quote_status.get("age_seconds"),
                    "event_age_seconds": quote_status.get("event_age_seconds"),
                },
                "execution": {
                    "status": (
                        "condition_order_eligible"
                        if bucket == "condition_order"
                        else "research_only"
                    ),
                    "condition_order_capability_verified": (
                        condition_order_capability.get("eligible") is True
                    ),
                    "requires_separate_trigger_and_limit_fields": True,
                    "order_limit_price": plan.get("order_limit_price"),
                },
                "plans": {
                    "short": deepcopy(plans.get("short") or plan),
                    "swing": deepcopy(plans.get("swing") or {}),
                    "position": deepcopy(plans.get("position") or {}),
                },
                "profile": deepcopy(dict(profile)),
                "allocation": {
                    key: deepcopy(allocation.get(key))
                    for key in (
                        "status",
                        "reason",
                        "reason_codes",
                        "rank",
                        "quantity",
                        "amount",
                        "position_pct",
                        "quantity_caps",
                    )
                    if key in allocation
                },
                "portfolio_impact": {
                    "exposure": deepcopy(allocation.get("exposure_audit") or {}),
                    "symbol_exposure": deepcopy(
                        allocation.get("symbol_exposure_audit") or {}
                    ),
                    "correlation": deepcopy(
                        allocation.get("correlation_audit") or {}
                    ),
                },
                "planned_loss": {
                    "amount": allocation.get("planned_loss_amount") or 0,
                    "pct_of_assets": allocation.get(
                        "planned_loss_pct_of_assets"
                    )
                    or 0,
                },
                "invalidation": {
                    "stop_price": plan.get("stop_price"),
                    "plan_expires_at": candidate.get("plan_expires_at"),
                    "risk_flags": deepcopy(candidate.get("risk_flags") or []),
                },
                "versions": {
                    "rule_version": RULE_VERSION,
                    "source": candidate.get("source"),
                    "source_policy_version": candidate.get(
                        "source_policy_version"
                    ),
                    "policy_version": effective_policy.get("policy_version"),
                    "taxonomy_version": effective_policy.get("taxonomy_version"),
                    "fee_policy_version": effective_policy.get("fee_policy_version"),
                    "profile_normalization_version": (
                        (profile.get("provider_sector_evidence") or {}).get(
                            "normalization_version"
                        )
                        if isinstance(profile.get("provider_sector_evidence"), Mapping)
                        else None
                    ),
                    "provider_versions": deepcopy(PROVIDER_VERSIONS),
                },
                "plan_id": plan_id,
            }
            bucket_items[bucket].append(_normalise_numbers(item))

        def item_order(item: Mapping[str, Any]) -> tuple[Decimal, Decimal, str]:
            identity = item.get("identity")
            identity = identity if isinstance(identity, Mapping) else {}
            allocation = item.get("allocation")
            allocation = allocation if isinstance(allocation, Mapping) else {}
            rank = _finite_decimal(allocation.get("rank")) or _finite_decimal(
                identity.get("rank")
            )
            score = _finite_decimal(identity.get("rank_score")) or Decimal(0)
            return (
                rank if rank is not None else Decimal("Infinity"),
                -score,
                _normalise_code(identity.get("code")),
            )

        for bucket in BUCKETS:
            bucket_items[bucket].sort(key=item_order)

        holding_valuation_audit = [
            deepcopy(dict(item))
            for item in diversification.get("holding_valuation_audit", [])
            if isinstance(item, Mapping)
        ]
        holding_valuation_audit.sort(
            key=lambda item: _normalise_code(item.get("code"))
        )

        def quality_key(item: Any) -> tuple[Any, ...]:
            value = item if isinstance(item, Mapping) else {"value": item}
            return (
                str(value.get("field") or ""),
                _SOURCE_PRIORITY.get(str(value.get("source") or "").lower(), 99),
                str(value.get("source_endpoint") or ""),
                str(value.get("error_code") or ""),
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
            )

        profile_errors.sort(key=quality_key)
        profile_conflicts.sort(key=quality_key)

        local_now = _as_shanghai(now)
        stable_session = deepcopy(dict(session))
        stable_session.pop("classified_at", None)
        packet: Dict[str, Any] = {
            "decision_id": f"decision_{uuid.uuid4().hex}",
            "user_id": str(user_id),
            "decision_date": local_now.date().isoformat(),
            "market_phase": phase,
            "revision": None,
            "as_of": local_now.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "candidate_run_id": candidate_run.get("run_id"),
            "candidate_generated_at": candidate_run.get("generated_at"),
            "candidate_research": deepcopy(
                candidate_run.get("candidate_research") or {}
            ),
            "briefing_as_of": briefing.get("as_of"),
            "market_session": stable_session,
            "account": deepcopy(account),
            "execution_capabilities": deepcopy(execution_capabilities),
            "market": deepcopy(market),
            "portfolio_constraints": {
                "effective_limits": deepcopy(
                    diversification.get("effective_limits") or {}
                ),
                "holding_valuation_audit": deepcopy(
                    holding_valuation_audit
                ),
            },
            "rolling_pool": {
                "capacity": ROLLING_POOL_CAPACITY,
                "total_count": len(rolling_candidates),
                "formal_research_capacity": FORMAL_RESEARCH_CAPACITY,
                "formal_research_count": len(raw_candidates),
                "current_count": sum(
                    str(item.get("rolling_pool_state") or "current") == "current"
                    for item in rolling_candidates
                ),
                "aging_count": sum(
                    item.get("rolling_pool_state") == "aging"
                    for item in rolling_candidates
                ),
                "expired_count": sum(
                    item.get("rolling_pool_state") == "expired"
                    for item in rolling_candidates
                ),
                "invalidated_count": sum(
                    item.get("rolling_pool_state") == "invalidated"
                    for item in rolling_candidates
                ),
                "candidates": rolling_candidate_audit,
            },
            "effective_policy": effective_policy,
            "authority": "software_baseline",
            "is_final_decision": False,
            "summary": {
                **{
                    f"{bucket}_count": len(bucket_items[bucket])
                    for bucket in BUCKETS
                },
                "permission_prefilter_excluded_count": len(
                    permission_prefilter_excluded
                ),
            },
            **bucket_items,
            "permission_prefilter_excluded": permission_prefilter_excluded,
            "data_quality": {
                "candidate_run_available": bool(candidate_run),
                "current_candidate_scan_available": (
                    candidate_run.get("current_scan_available") is not False
                ),
                "candidate_serving_mode": candidate_run.get("serving_mode"),
                "profile_errors": profile_errors,
                "profile_conflicts": profile_conflicts,
                "calendar_authoritative": session.get("calendar_authoritative"),
            },
            "rule_version": RULE_VERSION,
        }
        packet = _normalise_numbers(packet)
        packet["decision_state_hash"] = decision_state_hash(packet)
        packet["material_hash"] = material_hash(packet)
        return packet

    async def _acquire_lease(
        self, packet: Mapping[str, Any], owner: str
    ) -> Optional[int]:
        db = await self._get_db()
        now = datetime.now(timezone.utc)
        lock_id = (
            f"daily-decision:{packet['user_id']}:{packet['decision_date']}:"
            f"{packet['market_phase']}"
        )
        try:
            latest = await db["daily_decisions"].find_one(
                {
                    "user_id": packet["user_id"],
                    "decision_date": packet["decision_date"],
                    "market_phase": packet["market_phase"],
                },
                {"revision": 1},
                sort=[("revision", -1)],
            )
            revision_floor = int((latest or {}).get("revision") or 0)
            document = await db["job_locks"].find_one_and_update(
                {
                    "_id": lock_id,
                    "$or": [
                        {"lease_until": {"$lte": now}},
                        {"lease_until": {"$exists": False}},
                        {"owner": owner},
                    ],
                },
                {
                    "$set": {
                        "owner": owner,
                        "lease_until": now + timedelta(seconds=self.lease_seconds),
                        "updated_at": now,
                        "user_id": packet["user_id"],
                    },
                    "$inc": {"fence": 1},
                    "$max": {"next_revision": revision_floor},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except self.duplicate_key_errors:
            return None
        except Exception as exc:
            raise DecisionPersistenceError("decision_persistence_failed") from exc
        if not document or document.get("owner") != owner:
            return None
        fence = int(document.get("fence") or 0)
        return fence if fence > 0 else None

    async def _reserve_revision(
        self, packet: Mapping[str, Any], owner: str, fence: int
    ) -> Optional[int]:
        db = await self._get_db()
        now = datetime.now(timezone.utc)
        lock_id = (
            f"daily-decision:{packet['user_id']}:{packet['decision_date']}:"
            f"{packet['market_phase']}"
        )
        try:
            document = await db["job_locks"].find_one_and_update(
                {
                    "_id": lock_id,
                    "owner": owner,
                    "fence": fence,
                    "lease_until": {"$gt": now},
                },
                {
                    "$set": {
                        "lease_until": now
                        + timedelta(seconds=self.lease_seconds),
                        "updated_at": now,
                    },
                    "$inc": {"next_revision": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        except Exception as exc:
            raise DecisionPersistenceError("decision_persistence_failed") from exc
        if not document or document.get("owner") != owner:
            return None
        return int(document.get("next_revision") or 0) or None

    async def _release_lease(
        self, packet: Mapping[str, Any], owner: str, fence: int
    ) -> None:
        db = await self._get_db()
        lock_id = (
            f"daily-decision:{packet['user_id']}:{packet['decision_date']}:"
            f"{packet['market_phase']}"
        )
        try:
            await db["job_locks"].update_one(
                {"_id": lock_id, "owner": owner, "fence": fence},
                {
                    "$set": {
                        "lease_until": datetime.now(timezone.utc),
                        "released_at": datetime.now(timezone.utc),
                    }
                },
            )
        except Exception:
            # The snapshot is already durable. Lease expiry remains the fallback.
            pass

    async def _find_existing_hash(self, packet: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        document = await db["daily_decisions"].find_one(
            {
                "user_id": packet["user_id"],
                "decision_date": packet["decision_date"],
                "market_phase": packet["market_phase"],
                "material_hash": packet["material_hash"],
            }
        )
        return _serialize_mongo(document) if document else None

    async def _persist_packet(
        self,
        packet: Dict[str, Any],
        *,
        recompute: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        try:
            existing = await self._find_existing_hash(packet)
        except Exception as exc:
            raise DecisionPersistenceError("decision_persistence_failed") from exc
        if existing:
            return existing

        owner = uuid.uuid4().hex
        fence: Optional[int] = None
        saw_contention = False
        for _ in range(self.lease_wait_attempts):
            acquired_fence = await self._acquire_lease(packet, owner)
            if acquired_fence is not None:
                fence = acquired_fence
                break
            saw_contention = True
            await asyncio.sleep(self.lease_wait_seconds)
            try:
                existing = await self._find_existing_hash(packet)
            except Exception as exc:
                raise DecisionPersistenceError("decision_persistence_failed") from exc
            if existing:
                return existing
        else:
            raise DecisionPersistenceError("decision_persistence_failed")
        if fence is None:
            raise DecisionPersistenceError("decision_persistence_failed")

        if saw_contention and recompute is not None:
            await self._release_lease(packet, owner, fence)
            recomputed = await recompute()
            return await self._persist_packet(recomputed, recompute=None)

        try:
            existing = await self._find_existing_hash(packet)
            if existing:
                return existing
            db = await self._get_db()
            revision = await self._reserve_revision(packet, owner, fence)
            if revision is None:
                if recompute is None:
                    raise DecisionPersistenceError("decision_lease_lost")
                recomputed = await recompute()
                return await self._persist_packet(recomputed, recompute=None)
            document = deepcopy(packet)
            document["decision_id"] = f"decision_{uuid.uuid4().hex}"
            document["revision"] = revision
            document["persisted_at"] = datetime.now(timezone.utc)
            document["persistence"] = {
                "collection": "daily_decisions",
                "mode": "append_only",
                "lease_owner": owner,
                "lease_fence": fence,
                "reserved_revision": revision,
            }
            try:
                await db["daily_decisions"].insert_one(document)
            except self.duplicate_key_errors:
                winner = await self._find_existing_hash(packet)
                if winner:
                    return winner
                retry_revision = await self._reserve_revision(packet, owner, fence)
                if retry_revision is None:
                    if recompute is None:
                        raise DecisionPersistenceError("decision_lease_lost")
                    recomputed = await recompute()
                    return await self._persist_packet(recomputed, recompute=None)
                document["decision_id"] = f"decision_{uuid.uuid4().hex}"
                document["revision"] = retry_revision
                document["persistence"]["reserved_revision"] = retry_revision
                await db["daily_decisions"].insert_one(document)
            return _serialize_mongo(document)
        except DecisionPersistenceError:
            raise
        except Exception as exc:
            raise DecisionPersistenceError("decision_persistence_failed") from exc
        finally:
            await self._release_lease(packet, owner, fence)

    async def today(
        self,
        user_id: str,
        refresh: bool = True,
        now: datetime | None = None,
    ) -> dict:
        """Return one persisted decision packet for the authenticated user."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        effective_now = _as_shanghai(now)

        async def compose() -> Dict[str, Any]:
            return await self._compose_packet(
                normalized_user_id,
                refresh=bool(refresh),
                now=effective_now,
            )

        packet = await compose()
        snapshot = await self._persist_packet(packet, recompute=compose)
        try:
            registered = self.tracking_service.register_decision(snapshot)
            if inspect.isawaitable(registered):
                await registered
        except Exception as exc:
            raise DecisionPersistenceError("decision_tracking_failed") from exc
        return snapshot

    async def history(self, user_id: str, limit: int = 20) -> list:
        """Read newest append-only snapshots for one authenticated user."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        try:
            parsed_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError, OverflowError):
            parsed_limit = 20
        fetch_limit = min(500, max(parsed_limit * 20, 100))
        try:
            db = await self._get_db()
            cursor = (
                db["daily_decisions"]
                .find({"user_id": normalized_user_id})
                .sort(
                    [
                        ("decision_date", -1),
                        ("created_at", -1),
                        ("revision", -1),
                    ]
                )
                .limit(fetch_limit)
            )
            rows = await cursor.to_list(length=fetch_limit)
        except Exception as exc:
            raise DecisionPersistenceError("decision_history_unavailable") from exc
        compacted = []
        seen_states: set[str] = set()
        for row in rows:
            serialized = _serialize_mongo(row)
            state_hash = str(
                serialized.get("decision_state_hash")
                or decision_state_hash(serialized)
            )
            if state_hash in seen_states:
                continue
            seen_states.add(state_hash)
            serialized["decision_state_hash"] = state_hash
            serialized["history_compacted"] = True
            compacted.append(serialized)
            if len(compacted) >= parsed_limit:
                break
        return compacted


daily_decision_service = DailyDecisionService()
