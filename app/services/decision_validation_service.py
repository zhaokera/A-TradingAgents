"""Deterministic validation for Codex decision proposals."""

from __future__ import annotations

import inspect
import math
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_mongo_db
from app.models.decision import CodexDecisionProposalInput, DecisionAction
from app.services.decision_research_service import decision_research_service
from app.services.decision_workflow_errors import DecisionWorkflowError


VALIDATOR_VERSION = "decision-validator-v2"
LIVE_PHASES = frozenset({"live_am", "live_pm"})
ACTIONABLE_ACTIONS = frozenset({"buy_now", "condition_order"})
STALE_FAILURE_CODES = frozenset(
    {
        "buy_now_quote_stale",
        "research_packet_stale",
    }
)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MONEY_QUANTIZER = Decimal("0.01")


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money(value: Decimal) -> float:
    return float(value.quantize(MONEY_QUANTIZER, rounding=ROUND_HALF_UP))


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed


def _effective_now(value: Optional[datetime]) -> datetime:
    parsed = value or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _failure(
    code: str,
    *,
    symbol: Optional[str] = None,
    message: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "code": code,
        "message": message or code,
    }
    if symbol:
        result["symbol"] = symbol
    if details:
        result["details"] = deepcopy(dict(details))
    return result


def _dedupe_failures(values: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in values:
        value = deepcopy(dict(raw))
        details = value.get("details")
        key = (
            str(value.get("code") or ""),
            str(value.get("symbol") or ""),
            repr(sorted((details or {}).items())),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _proposal_payload(value: Any) -> tuple[Dict[str, Any], Optional[str]]:
    if isinstance(value, CodexDecisionProposalInput):
        return value.model_dump(mode="json"), None
    if not isinstance(value, Mapping):
        raise DecisionWorkflowError(
            "proposal_invalid",
            "Codex 提案必须是对象",
            status_code=422,
        )
    proposal_id = str(value.get("proposal_id") or "").strip() or None
    payload = value.get("payload")
    payload = payload if isinstance(payload, Mapping) else value
    validated = CodexDecisionProposalInput.model_validate(payload)
    return validated.model_dump(mode="json"), proposal_id


def _candidate_taxonomy(candidate: Mapping[str, Any]) -> Dict[str, str]:
    identity = candidate.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    profile = candidate.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return {
        "theme": str(identity.get("objective_segment") or "").strip(),
        "provider_sector": str(profile.get("provider_sector") or "").strip(),
        "industry": str(profile.get("industry") or "").strip(),
    }


def _fallback_correlation(
    left: Mapping[str, str], right: Mapping[str, str]
) -> float:
    if left.get("industry") and left["industry"] == right.get("industry"):
        return 1.0
    if left.get("theme") and left["theme"] == right.get("theme"):
        return 0.85
    return 0.50


def _condition_order_capability(packet: Mapping[str, Any]) -> bool:
    capabilities = packet.get("execution_capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    condition = capabilities.get("condition_order")
    condition = condition if isinstance(condition, Mapping) else {}
    return bool(
        condition.get("verified") is True
        and condition.get("independent_trigger_price_supported") is True
        and condition.get("separate_order_limit_price_supported") is True
    )


def _has_actionable_selections(proposal: Any) -> bool:
    payload, _ = _proposal_payload(proposal)
    return any(
        str(item.get("action") or "") in ACTIONABLE_ACTIONS
        for item in payload.get("selections") or []
        if isinstance(item, Mapping)
    )


class DecisionValidationService:
    """Reject hard-limit violations without modifying a Codex proposal."""

    def __init__(
        self,
        *,
        db: Any = None,
        research_service: Any = None,
        validation_ttl_seconds: Optional[int] = None,
    ) -> None:
        self.db = db
        self.research_service = research_service or decision_research_service
        self.validation_ttl_seconds = int(
            validation_ttl_seconds
            if validation_ttl_seconds is not None
            else getattr(settings, "CODEX_DECISION_VALIDATION_TTL_SECONDS", 60)
        )

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        if inspect.isawaitable(self.db):
            self.db = await self.db
        return self.db

    async def validate_document(
        self,
        user_id: str,
        proposal: Any,
        research_packet: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
        proposal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        packet = deepcopy(dict(research_packet))
        payload, embedded_proposal_id = _proposal_payload(proposal)
        proposal_id = proposal_id or embedded_proposal_id or "unpersisted"
        effective_now = _effective_now(now)
        failures: list[Dict[str, Any]] = []
        accepted_overrides: list[Dict[str, Any]] = []
        quote_checks: list[Dict[str, Any]] = []
        recalculated: list[Dict[str, Any]] = []

        if str(packet.get("user_id") or "") != owner:
            failures.append(
                _failure(
                    "research_packet_owner_mismatch",
                    message="研究包不属于当前用户",
                )
            )
        if payload["research_packet_id"] != packet.get("research_packet_id"):
            failures.append(
                _failure(
                    "research_packet_mismatch",
                    message="提案引用的研究包与校验输入不一致",
                )
            )

        objective = packet.get("decision_objective")
        objective = objective if isinstance(objective, Mapping) else {}
        scope = payload.get("decision_scope") or {}
        if int(scope.get("max_new_positions") or 0) > int(
            objective.get("max_new_positions") or 0
        ):
            failures.append(
                _failure(
                    "decision_scope_exceeds_research_policy",
                    details={"field": "max_new_positions"},
                )
            )
        if int(scope.get("primary_position_count") or 0) > int(
            objective.get("primary_position_count") or 0
        ):
            failures.append(
                _failure(
                    "decision_scope_exceeds_research_policy",
                    details={"field": "primary_position_count"},
                )
            )

        candidates = {
            str(item.get("symbol") or ""): item
            for item in packet.get("candidates") or []
            if isinstance(item, Mapping)
        }
        account = packet.get("account")
        account = account if isinstance(account, Mapping) else {}
        policy = packet.get("hard_risk_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        constraints = packet.get("portfolio_constraints")
        constraints = constraints if isinstance(constraints, Mapping) else {}
        effective_limits = constraints.get("effective_limits")
        effective_limits = (
            effective_limits if isinstance(effective_limits, Mapping) else {}
        )

        assets = _decimal(account.get("total_assets")) or Decimal(0)
        cash = _decimal(account.get("available_cash")) or Decimal(0)
        available_exposure_pct = _decimal(
            policy.get("available_new_exposure_pct")
        ) or Decimal(0)
        per_position_loss_pct = _decimal(
            policy.get("per_position_loss_budget_pct")
        ) or Decimal(0)
        total_loss_pct = _decimal(
            policy.get("total_new_position_loss_budget_pct")
        ) or Decimal(0)
        hard_symbol_cap_pct = _decimal(
            policy.get("hard_single_symbol_cap_pct")
        ) or Decimal(0)
        capital_limit = assets * available_exposure_pct / Decimal(100)
        total_loss_limit = assets * total_loss_pct / Decimal(100)
        per_position_loss_limit = assets * per_position_loss_pct / Decimal(100)

        total_cost = Decimal(0)
        total_planned_loss = Decimal(0)
        exposure_ledgers: Dict[tuple[str, str], Decimal] = {}
        selected_taxonomies: list[tuple[str, Dict[str, str]]] = []
        correlation_cap = _finite_float(
            effective_limits.get("pairwise_correlation_cap"), 0.80
        )
        earliest_valid_until: Optional[datetime] = None
        has_condition_order = False

        for raw_selection in payload.get("selections") or []:
            selection = deepcopy(dict(raw_selection))
            symbol = str(selection.get("symbol") or "")
            action = str(selection.get("action") or "")
            candidate = candidates.get(symbol)
            selection_calc: Dict[str, Any] = {
                "symbol": symbol,
                "action": action,
                "requested_quantity": selection.get("requested_quantity") or 0,
                "total_cost": 0.0,
                "planned_loss": 0.0,
                "position_weight_pct": 0.0,
            }
            recalculated.append(selection_calc)
            if not candidate:
                failures.append(
                    _failure(
                        "candidate_not_found",
                        symbol=symbol,
                        message="提案股票不在研究包中",
                    )
                )
                continue

            evidence_ids = {
                str(item.get("evidence_id") or "")
                for item in candidate.get("evidence") or []
                if isinstance(item, Mapping)
            }
            for evidence_ref in selection.get("evidence_refs") or []:
                if evidence_ref not in evidence_ids:
                    failures.append(
                        _failure(
                            "evidence_reference_not_found",
                            symbol=symbol,
                            details={"evidence_ref": evidence_ref},
                        )
                    )

            for raw_constraint in candidate.get("hard_constraints") or []:
                if not isinstance(raw_constraint, Mapping):
                    continue
                applies_to = raw_constraint.get("applies_to")
                applies_to = (
                    set(str(value) for value in applies_to)
                    if isinstance(applies_to, list)
                    else set(ACTIONABLE_ACTIONS)
                )
                if action in applies_to:
                    failures.append(
                        _failure(
                            str(raw_constraint.get("code") or "hard_constraint"),
                            symbol=symbol,
                            details=raw_constraint.get("details"),
                        )
                    )

            warning_codes = {
                str(item.get("code") or "")
                for item in candidate.get("soft_warnings") or []
                if isinstance(item, Mapping)
            }
            overrides = {
                str(item.get("warning_code") or ""): dict(item)
                for item in selection.get("overrides") or []
                if isinstance(item, Mapping)
            }
            if action in ACTIONABLE_ACTIONS:
                for warning_code in sorted(warning_codes):
                    if warning_code not in overrides:
                        failures.append(
                            _failure(
                                "soft_warning_override_missing",
                                symbol=symbol,
                                details={"warning_code": warning_code},
                            )
                        )
                    else:
                        accepted_overrides.append(
                            {"symbol": symbol, **deepcopy(overrides[warning_code])}
                        )
            for override_code in sorted(set(overrides) - warning_codes):
                failures.append(
                    _failure(
                        "soft_warning_override_not_present",
                        symbol=symbol,
                        details={"warning_code": override_code},
                    )
                )

            if action not in ACTIONABLE_ACTIONS:
                continue

            quantity = int(selection.get("requested_quantity") or 0)
            trigger = _decimal(selection.get("trigger_price")) or Decimal(0)
            order_limit = _decimal(selection.get("order_limit_price"))
            stop = _decimal(selection.get("stop_price")) or Decimal(0)
            target = _decimal(selection.get("target_price")) or Decimal(0)
            risk_envelope = candidate.get("risk_envelope")
            risk_envelope = (
                risk_envelope if isinstance(risk_envelope, Mapping) else {}
            )
            lot_size = int(risk_envelope.get("lot_size") or 100)
            max_quantity = int(risk_envelope.get("max_allowed_quantity") or 0)
            if quantity <= 0 or quantity % lot_size:
                failures.append(
                    _failure(
                        "invalid_board_lot",
                        symbol=symbol,
                        details={"quantity": quantity, "lot_size": lot_size},
                    )
                )
            if quantity > max_quantity:
                failures.append(
                    _failure(
                        "requested_quantity_exceeds_hard_limit",
                        symbol=symbol,
                        details={
                            "requested_quantity": quantity,
                            "max_allowed_quantity": max_quantity,
                        },
                    )
                )
            for field, price in (
                ("trigger_price", trigger),
                ("stop_price", stop),
                ("target_price", target),
            ):
                if price <= 0 or price != price.quantize(MONEY_QUANTIZER):
                    failures.append(
                        _failure(
                            "invalid_price_tick",
                            symbol=symbol,
                            details={"field": field, "value": str(price)},
                        )
                    )
            if action == DecisionAction.CONDITION_ORDER.value:
                if not _condition_order_capability(packet):
                    failures.append(
                        _failure(
                            "condition_order_execution_capability_unverified",
                            symbol=symbol,
                            message="券商条件单能力未核实，不能生成可执行条件单",
                        )
                    )
                if order_limit is None:
                    failures.append(
                        _failure(
                            "condition_order_order_limit_price_missing",
                            symbol=symbol,
                            message="条件单缺少独立委托限价",
                        )
                    )
                elif (
                    order_limit <= 0
                    or order_limit != order_limit.quantize(MONEY_QUANTIZER)
                ):
                    failures.append(
                        _failure(
                            "invalid_price_tick",
                            symbol=symbol,
                            details={
                                "field": "order_limit_price",
                                "value": str(order_limit),
                            },
                        )
                    )
                elif selection.get("entry_strategy") == "breakout" and not (
                    trigger <= order_limit < target
                ):
                    failures.append(
                        _failure(
                            "invalid_condition_order_price_plan",
                            symbol=symbol,
                            details={
                                "expected": (
                                    "trigger_price <= order_limit_price "
                                    "< target_price"
                                )
                            },
                        )
                    )
                elif selection.get("entry_strategy") in {
                    "pullback",
                    "reference",
                } and not (stop < order_limit <= trigger):
                    failures.append(
                        _failure(
                            "invalid_condition_order_price_plan",
                            symbol=symbol,
                            details={
                                "expected": (
                                    "stop_price < order_limit_price "
                                    "<= trigger_price"
                                )
                            },
                        )
                    )
            if not stop < trigger < target:
                failures.append(
                    _failure("invalid_price_plan", symbol=symbol)
                )

            expires_at = _as_datetime(selection.get("expires_at"))
            if expires_at is None or expires_at <= effective_now:
                failures.append(_failure("plan_expired", symbol=symbol))
            elif earliest_valid_until is None or expires_at < earliest_valid_until:
                earliest_valid_until = expires_at

            entry_cost_price = (
                order_limit
                if action == DecisionAction.CONDITION_ORDER.value
                and order_limit is not None
                else trigger
            )
            cost = entry_cost_price * quantity
            planned_loss = (entry_cost_price - stop) * quantity
            selection_calc.update(
                total_cost=_money(cost),
                planned_loss=_money(planned_loss),
                position_weight_pct=(
                    _money(cost / assets * Decimal(100)) if assets > 0 else 0.0
                ),
            )
            total_cost += cost
            total_planned_loss += planned_loss
            if assets <= 0 or cash < 0:
                failures.append(
                    _failure("account_blocked", symbol=symbol)
                )
            if total_cost > cash:
                failures.append(
                    _failure(
                        "insufficient_cash",
                        symbol=symbol,
                        details={
                            "required": _money(total_cost),
                            "available": _money(cash),
                        },
                    )
                )
            if total_cost > capital_limit:
                failures.append(
                    _failure(
                        "new_exposure_limit",
                        symbol=symbol,
                        details={
                            "required": _money(total_cost),
                            "limit": _money(capital_limit),
                        },
                    )
                )
            if planned_loss > per_position_loss_limit:
                failures.append(
                    _failure(
                        "per_position_loss_limit",
                        symbol=symbol,
                        details={
                            "planned_loss": _money(planned_loss),
                            "limit": _money(per_position_loss_limit),
                        },
                    )
                )
            if total_planned_loss > total_loss_limit:
                failures.append(
                    _failure(
                        "total_new_position_loss_limit",
                        symbol=symbol,
                        details={
                            "planned_loss": _money(total_planned_loss),
                            "limit": _money(total_loss_limit),
                        },
                    )
                )

            impact = candidate.get("portfolio_impact")
            impact = impact if isinstance(impact, Mapping) else {}
            symbol_exposure = impact.get("symbol_exposure")
            symbol_exposure = (
                symbol_exposure if isinstance(symbol_exposure, Mapping) else {}
            )
            existing_symbol = _decimal(
                symbol_exposure.get("before_amount")
            ) or Decimal(0)
            if existing_symbol + cost > assets * hard_symbol_cap_pct / Decimal(100):
                failures.append(
                    _failure("single_symbol_cap", symbol=symbol)
                )

            exposure = impact.get("exposure")
            exposure = exposure if isinstance(exposure, Mapping) else {}
            taxonomy = _candidate_taxonomy(candidate)
            for dimension, limit_field in (
                ("theme", "theme_exposure_cap_pct"),
                ("provider_sector", "provider_sector_exposure_cap_pct"),
                ("industry", "industry_exposure_cap_pct"),
            ):
                audit = exposure.get(dimension)
                audit = audit if isinstance(audit, Mapping) else {}
                taxonomy_value = str(
                    audit.get("taxonomy_value") or taxonomy.get(dimension) or ""
                ).strip()
                if not taxonomy_value:
                    failures.append(
                        _failure(
                            "taxonomy_unavailable_for_hard_limit",
                            symbol=symbol,
                            details={"dimension": dimension},
                        )
                    )
                    continue
                key = (dimension, taxonomy_value)
                if key not in exposure_ledgers:
                    exposure_ledgers[key] = _decimal(
                        audit.get("before_amount")
                    ) or Decimal(0)
                exposure_ledgers[key] += cost
                cap_pct = _decimal(audit.get("cap_pct")) or _decimal(
                    effective_limits.get(limit_field)
                )
                if cap_pct is not None and exposure_ledgers[key] > (
                    assets * cap_pct / Decimal(100)
                ):
                    failures.append(
                        _failure(
                            "concentration_limit",
                            symbol=symbol,
                            details={
                                "dimension": dimension,
                                "taxonomy_value": taxonomy_value,
                                "cap_pct": float(cap_pct),
                            },
                        )
                    )

            for compared_symbol, compared_taxonomy in selected_taxonomies:
                correlation = _fallback_correlation(taxonomy, compared_taxonomy)
                if correlation > correlation_cap:
                    failures.append(
                        _failure(
                            "correlation_limit",
                            symbol=symbol,
                            details={
                                "compared_symbol": compared_symbol,
                                "value": correlation,
                                "cap": correlation_cap,
                                "basis": "taxonomy_fallback",
                            },
                        )
                    )
            holding_audits = constraints.get("holding_valuation_audit")
            holding_audits = (
                holding_audits if isinstance(holding_audits, list) else []
            )
            for holding in holding_audits:
                if not isinstance(holding, Mapping):
                    continue
                if _finite_float(holding.get("quantity")) <= 0:
                    continue
                holding_taxonomy = {
                    "theme": str(holding.get("objective_segment") or "").strip(),
                    "provider_sector": str(
                        holding.get("provider_sector") or ""
                    ).strip(),
                    "industry": str(holding.get("industry") or "").strip(),
                }
                correlation = _fallback_correlation(taxonomy, holding_taxonomy)
                if correlation > correlation_cap:
                    failures.append(
                        _failure(
                            "correlation_limit",
                            symbol=symbol,
                            details={
                                "compared_symbol": holding.get("code"),
                                "value": correlation,
                                "cap": correlation_cap,
                                "basis": "taxonomy_fallback",
                            },
                        )
                    )
            selected_taxonomies.append((symbol, taxonomy))

            quote = candidate.get("quote")
            quote = quote if isinstance(quote, Mapping) else {}
            quote_check = {
                "symbol": symbol,
                "source": quote.get("source"),
                "trade_at": quote.get("trade_at"),
                "status": quote.get("status"),
                "actionable": quote.get("actionable"),
            }
            quote_checks.append(quote_check)
            if action == DecisionAction.BUY_NOW.value:
                phase = str(
                    (packet.get("market_session") or {}).get("phase") or ""
                )
                if phase not in LIVE_PHASES:
                    failures.append(
                        _failure(
                            "buy_now_outside_live_session",
                            symbol=symbol,
                            details={"market_phase": phase},
                        )
                    )
                trade_at = _as_datetime(quote.get("trade_at"))
                freshness = int(
                    (packet.get("market_session") or {}).get(
                        "quote_freshness_required_seconds"
                    )
                    or 90
                )
                quote_valid_until = (
                    trade_at + timedelta(seconds=freshness) if trade_at else None
                )
                fresh = bool(
                    str(quote.get("source") or "").lower() == "tencent"
                    and quote.get("actionable") is True
                    and quote.get("status") == "fresh"
                    and quote_valid_until is not None
                    and quote_valid_until >= effective_now
                    and trade_at.astimezone(SHANGHAI_TIMEZONE).date()
                    == effective_now.astimezone(SHANGHAI_TIMEZONE).date()
                )
                if not fresh:
                    failures.append(
                        _failure("buy_now_quote_stale", symbol=symbol)
                    )
                elif earliest_valid_until is None or (
                    quote_valid_until and quote_valid_until < earliest_valid_until
                ):
                    earliest_valid_until = quote_valid_until
                current_price = _decimal(quote.get("price"))
                strategy = selection.get("entry_strategy")
                condition_met = bool(
                    current_price is not None
                    and current_price > stop
                    and (
                        (
                            strategy in {"pullback", "reference"}
                            and current_price <= trigger
                        )
                        or (strategy == "breakout" and current_price >= trigger)
                    )
                )
                if fresh and not condition_met:
                    failures.append(
                        _failure("entry_condition_not_met", symbol=symbol)
                    )
            else:
                has_condition_order = True
                phase = str(
                    (packet.get("market_session") or {}).get("phase") or ""
                )
                trade_at = _as_datetime(quote.get("trade_at"))
                freshness = int(
                    (packet.get("market_session") or {}).get(
                        "quote_freshness_required_seconds"
                    )
                    or 90
                )
                quote_valid_until = (
                    trade_at + timedelta(seconds=freshness) if trade_at else None
                )
                fresh = bool(
                    phase in LIVE_PHASES
                    and str(quote.get("source") or "").lower() == "tencent"
                    and quote.get("actionable") is True
                    and quote.get("status") == "fresh"
                    and quote_valid_until is not None
                    and quote_valid_until >= effective_now
                    and trade_at.astimezone(SHANGHAI_TIMEZONE).date()
                    == effective_now.astimezone(SHANGHAI_TIMEZONE).date()
                )
                if not fresh:
                    failures.append(
                        _failure("condition_order_quote_stale", symbol=symbol)
                    )
                else:
                    if earliest_valid_until is None or (
                        quote_valid_until
                        and quote_valid_until < earliest_valid_until
                    ):
                        earliest_valid_until = quote_valid_until
                    current_price = _decimal(quote.get("price"))
                    strategy = selection.get("entry_strategy")
                    if current_price is not None and current_price <= stop:
                        failures.append(
                            _failure(
                                "condition_order_plan_already_invalidated",
                                symbol=symbol,
                                details={
                                    "current_price": str(current_price),
                                    "stop_price": str(stop),
                                },
                            )
                        )
                    condition_already_met = bool(
                        current_price is not None
                        and current_price > stop
                        and (
                            (
                                strategy in {"pullback", "reference"}
                                and current_price <= trigger
                            )
                            or (
                                strategy == "breakout"
                                and current_price >= trigger
                            )
                        )
                    )
                    if condition_already_met:
                        failures.append(
                            _failure(
                                "condition_order_trigger_already_met",
                                symbol=symbol,
                                details={"current_price": str(current_price)},
                            )
                        )

        failures = _dedupe_failures(failures)
        failure_codes = {value["code"] for value in failures}
        if failures:
            status = (
                "stale_revalidation_required"
                if failure_codes.issubset(STALE_FAILURE_CODES)
                else "invalid"
            )
        else:
            status = "valid"

        ttl_limit = effective_now + timedelta(seconds=self.validation_ttl_seconds)
        if any(
            str(item.get("action") or "") == DecisionAction.BUY_NOW.value
            for item in payload.get("selections") or []
        ):
            earliest_valid_until = (
                min(earliest_valid_until, ttl_limit)
                if earliest_valid_until
                else ttl_limit
            )
        result = {
            "validation_id": f"validation_{uuid.uuid4().hex}",
            "proposal_id": proposal_id,
            "research_packet_id": packet.get("research_packet_id"),
            "user_id": owner,
            "validated_at": effective_now.isoformat(),
            "status": status,
            "hard_failures": failures,
            "accepted_overrides": accepted_overrides,
            "recalculated": {
                "total_cost": _money(total_cost),
                "total_planned_loss": _money(total_planned_loss),
                "total_position_weight_pct": (
                    _money(total_cost / assets * Decimal(100))
                    if assets > 0
                    else 0.0
                ),
                "selections": recalculated,
            },
            "quote_check": {"items": quote_checks},
            "valid_until": (
                earliest_valid_until.isoformat() if earliest_valid_until else None
            ),
            "trigger_time_revalidation_required": has_condition_order,
            "validator_version": VALIDATOR_VERSION,
            "disclaimer": "仅供研究和参考，不构成投资建议或交易指令。",
        }
        return result

    async def persist(self, validation: Mapping[str, Any]) -> Dict[str, Any]:
        db = await self._get_db()
        document = deepcopy(dict(validation))
        document["persisted_at"] = datetime.now(timezone.utc).isoformat()
        await db["decision_validations"].insert_one(deepcopy(document))
        result = deepcopy(document)
        result.pop("_id", None)
        return result

    async def validate(
        self,
        user_id: str,
        proposal_id: str,
        *,
        refresh_quote: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        normalized_proposal_id = str(proposal_id or "").strip()
        db = await self._get_db()
        proposal = await db["codex_decision_proposals"].find_one(
            {"user_id": owner, "proposal_id": normalized_proposal_id}
        )
        if not proposal:
            raise DecisionWorkflowError(
                "decision_proposal_not_found",
                "Codex 提案不存在或不属于当前用户",
                status_code=404,
            )
        packet = await self.research_service.get(
            owner, str(proposal.get("research_packet_id") or "")
        )
        if refresh_quote:
            refreshed = await self.research_service.today(
                owner,
                refresh=True,
                now=now,
            )
            if (
                refreshed.get("material_hash") != packet.get("material_hash")
                and _has_actionable_selections(proposal)
            ):
                result = await self.validate_document(
                    owner,
                    proposal,
                    packet,
                    now=now,
                    proposal_id=normalized_proposal_id,
                )
                result["status"] = "stale_revalidation_required"
                result["hard_failures"] = _dedupe_failures(
                    [
                        *result["hard_failures"],
                        _failure(
                            "research_packet_stale",
                            details={
                                "latest_research_packet_id": refreshed.get(
                                    "research_packet_id"
                                )
                            },
                        ),
                    ]
                )
                return await self.persist(result)
            if refreshed.get("material_hash") == packet.get("material_hash"):
                packet = refreshed
        result = await self.validate_document(
            owner,
            proposal,
            packet,
            now=now,
            proposal_id=normalized_proposal_id,
        )
        return await self.persist(result)

    async def get(
        self, user_id: str, validation_id: str
    ) -> Dict[str, Any]:
        db = await self._get_db()
        row = await db["decision_validations"].find_one(
            {
                "user_id": str(user_id),
                "validation_id": str(validation_id),
            }
        )
        if not row:
            raise DecisionWorkflowError(
                "decision_validation_not_found",
                "校验结果不存在或不属于当前用户",
                status_code=404,
            )
        result = deepcopy(dict(row))
        result.pop("_id", None)
        return result


decision_validation_service = DecisionValidationService()
