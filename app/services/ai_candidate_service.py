"""Persisted AI research candidates and controlled favorite promotion."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from bson import ObjectId
from starlette.concurrency import run_in_threadpool

from app.core.database import get_mongo_db
from app.models.notification import NotificationCreate
from app.services.candidate_research_pipeline import run_candidate_research
from app.services.favorites_service import FavoritesService, favorites_service
from app.services.investment_policy import (
    INVESTMENT_OBJECTIVE,
    allocate_candidate_portfolio,
    build_dynamic_portfolio_policy,
    calculate_candidate_position_sizing,
    classify_investment_objective,
    objective_tier_rank,
)
from app.services.global_macro_risk_service import GlobalMacroRiskService
from app.services.stock_master_data_service import StockMasterDataService
from app.services.a_share_calendar_service import AShareCalendarService
from app.services.notifications_service import get_notifications_service
from app.services.tencent_quote_service import (
    TencentQuoteService,
    get_tencent_quote_service,
)


AI_CANDIDATE_SOURCE = "ai_screening"
AI_CANDIDATE_TAG = "AI候选"
AI_CANDIDATE_RUN_TTL_DAYS = 90
AI_CANDIDATE_JOB_TTL_DAYS = 7
_A_SHARE_CODE = re.compile(r"^[0-9]{6}$")
logger = logging.getLogger(__name__)

_BLOCKING_RISK_SEVERITIES = {"error", "critical", "blocker", "blocked"}
_BLOCKING_RISK_CODES = {
    "quote_not_actionable",
    "corporate_action",
    "corporate_action_blocked",
    "technical_deep_check_timeout",
    "earnings_risk_blocked",
    "notice_risk_blocked",
    "suspended",
    "delisted",
}

_ACTIONABILITY_LABELS = {
    "ready_now": "研究价格条件已满足",
    "condition_order": "研究条件待触发",
    "blocked": "风险阻断",
    "invalidated": "计划失效",
    "target_reached": "已达到目标价",
    "expired": "需要重新分析",
    "quote_unavailable": "行情待刷新",
    "incomplete": "价格计划不完整",
}

_ACTIONABILITY_ORDER = {
    "ready_now": 0,
    "condition_order": 1,
    "blocked": 2,
    "quote_unavailable": 3,
    "incomplete": 4,
    "target_reached": 5,
    "expired": 6,
    "invalidated": 7,
}

_ALLOCATION_ORDER = {
    "allocated": 0,
    "watch_only": 1,
    "budget_exhausted": 2,
    "market_blocked": 3,
}

_ENTRY_STRATEGY_LABELS = {
    "pullback": "回落参考",
    "breakout": "突破参考",
    "reference": "观察参考",
}

_ENTRY_STATUS_LABELS = {
    "waiting_pullback": "等待回落",
    "waiting_breakout": "等待突破",
    "price_ready": "价格条件已满足",
    "price_ready_risk_blocked": "价格到位，风险阻断",
    "invalidated": "价格计划已失效",
    "quote_unavailable": "行情待刷新",
    "plan_unavailable": "暂无可靠入手价",
}


class AICandidateRunNotFoundError(LookupError):
    """The requested run does not exist or belongs to another user."""


class InvalidAICandidateSelectionError(ValueError):
    """The requested codes are not part of the persisted run."""


def _finite_number(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            return round(number, 4)
    return None


def _quote_event_changed(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    for field in ("price", "volume", "amount"):
        before = _finite_number(previous.get(field))
        after = _finite_number(current.get(field))
        if before is not None and after is not None and before != after:
            return True
    return False


def _normalized_code_set(value: Any) -> set[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return set()
    return {
        str(code or "").strip()
        for code in value
        if _A_SHARE_CODE.fullmatch(str(code or "").strip())
    }


def _candidate_governance_reason(
    code: Any,
    governance: Mapping[str, Any],
) -> Optional[str]:
    normalized = str(code or "").strip()
    if normalized in _normalized_code_set(governance.get("excluded_codes")):
        return "user_excluded"
    if normalized.startswith(("688", "689")):
        star = governance.get("star_market")
        star = star if isinstance(star, Mapping) else {}
        if star.get("eligible") is not True:
            return (
                "star_market_permission_denied"
                if star.get("verified") is True
                else "star_market_permission_unverified"
            )
    return None


def _candidate_governance_from_settings(
    settings: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    settings = settings if isinstance(settings, Mapping) else {}
    capabilities = settings.get("execution_capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    market_permissions = capabilities.get("market_permissions")
    market_permissions = (
        market_permissions if isinstance(market_permissions, Mapping) else {}
    )
    star = market_permissions.get("star_market")
    star = star if isinstance(star, Mapping) else {}
    star_verified = star.get("verified") is True
    star_tradable = star.get("tradable") is True
    return {
        "excluded_codes": sorted(
            _normalized_code_set(settings.get("excluded_codes"))
        ),
        "star_market": {
            "verified": star_verified,
            "tradable": star_tradable,
            "eligible": star_verified and star_tradable,
        },
    }


def _shadow_plan_identity(
    document: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    explicit = str(candidate.get("plan_id") or "").strip()
    if explicit:
        return explicit
    plan = (
        candidate.get("price_plan")
        if isinstance(candidate.get("price_plan"), Mapping)
        else {}
    )
    payload = {
        "code": str(candidate.get("code") or "").strip(),
        "entry_strategy": plan.get("entry_strategy"),
        "entry_price": _finite_number(plan.get("entry_price")),
        "stop_price": _finite_number(plan.get("stop_price")),
        "target_price": _finite_number(plan.get("target_price")),
        "order_limit_price": _finite_number(plan.get("order_limit_price")),
        "plan_expires_at": str(
            candidate.get("plan_expires_at")
            or document.get("plan_expires_at")
            or ""
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _stop_governance_shadow_tracking(
    candidate: Dict[str, Any],
    *,
    reason: str,
) -> None:
    performance = candidate.get("performance")
    if not isinstance(performance, Mapping):
        return
    performance = deepcopy(dict(performance))
    shadow = performance.get("shadow_trade")
    if not isinstance(shadow, Mapping):
        return
    shadow = deepcopy(dict(shadow))
    status = str(shadow.get("status") or "").strip()
    if status.startswith("closed_") or status == "stopped_governance":
        return
    shadow.update(
        {
            "previous_status": status or None,
            "status": "stopped_governance",
            "tracking_stop_reason": reason,
            "tracking_stopped_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    performance["shadow_trade"] = shadow
    candidate["performance"] = performance


def _normalize_observation_zone(value: Any) -> Optional[List[float]]:
    if isinstance(value, Mapping):
        low = _finite_number(value.get("low"), value.get("min"))
        high = _finite_number(value.get("high"), value.get("max"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        low = _finite_number(value[0])
        high = _finite_number(value[1])
    else:
        return None
    if low is None or high is None:
        return None
    return [min(low, high), max(low, high)]


def _candidate_evidence(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
) -> List[str]:
    evidence = ["全市场流动性与量价质量初筛通过", "腾讯实时行情复核通过"]
    if context.get("technical_deep_check_status") == "ok":
        evidence.append("技术面深度检查通过")
    if context.get("earnings_forecast_review_status") == "ok":
        evidence.append("业绩预告与最新财报风险门槛通过")
    corporate_action = candidate.get("corporate_action")
    if (
        isinstance(corporate_action, Mapping)
        and corporate_action.get("blocks_new_position") is False
    ):
        evidence.append("近期公司行动未触发阻断条件")
    return evidence


def _candidate_reason(candidate: Mapping[str, Any]) -> str:
    triggers = candidate.get("triggers")
    if isinstance(triggers, Mapping):
        note = str(triggers.get("note") or "").strip()
        if note:
            return note[:240]
    discovery = candidate.get("discovery")
    public = discovery.get("public") if isinstance(discovery, Mapping) else None
    bucket = public.get("bucket") if isinstance(public, Mapping) else None
    if bucket == "strength":
        return "强势量价候选，等待突破条件确认后再评估。"
    if bucket == "pullback":
        return "回调结构候选，等待观察区间企稳后再评估。"
    return "全市场多阶段筛选候选，需结合价格条件继续观察。"


def _normalize_risk_flags(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    flags: List[Dict[str, str]] = []
    for raw in value[:8]:
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("code") or raw.get("key") or "risk_flag").strip()
        message = str(raw.get("message") or raw.get("reason") or code).strip()
        if code == "quote_not_actionable":
            message = (
                "腾讯成交时间未通过当前时效门禁；可保留条件价和组合预算，"
                "但触发时必须刷新行情后再判定是否可执行。"
            )
        flags.append(
            {
                "code": code[:80],
                "severity": str(
                    raw.get("severity") or raw.get("level") or "warning"
                ).lower()[:20],
                "message": message[:240],
            }
        )
    return flags


def _is_blocking_risk_flag(flag: Mapping[str, Any]) -> bool:
    return (
        str(flag.get("severity") or "").lower() in _BLOCKING_RISK_SEVERITIES
        or str(flag.get("code") or "") in _BLOCKING_RISK_CODES
    )


def _derive_actionability(
    price_plan: Mapping[str, Any],
    *,
    performance: Optional[Mapping[str, Any]] = None,
) -> str:
    if performance and performance.get("target_hit_at"):
        return "target_reached"
    entry_status = str(price_plan.get("entry_status") or "plan_unavailable")
    if entry_status == "price_ready":
        return "ready_now"
    if entry_status in {"waiting_pullback", "waiting_breakout"}:
        return "condition_order"
    if entry_status == "price_ready_risk_blocked":
        return "blocked"
    if entry_status == "invalidated":
        return "invalidated"
    if entry_status == "quote_unavailable":
        return "quote_unavailable"
    return "incomplete"


def _apply_candidate_state(candidate: Dict[str, Any]) -> Dict[str, Any]:
    performance = (
        candidate.get("performance")
        if isinstance(candidate.get("performance"), Mapping)
        else None
    )
    actionability = _derive_actionability(
        candidate.get("price_plan") or {},
        performance=performance,
    )
    portfolio_gate = (
        candidate.get("portfolio_gate")
        if isinstance(candidate.get("portfolio_gate"), Mapping)
        else {}
    )
    if portfolio_gate.get("blocked") and actionability in {
        "ready_now",
        "condition_order",
    }:
        actionability = "blocked"
    elif candidate.get("plan_expired") is True and actionability not in {
        "target_reached",
        "invalidated",
    }:
        actionability = "expired"
    candidate["actionability"] = actionability
    candidate["actionability_label"] = _ACTIONABILITY_LABELS[actionability]
    candidate["research_status"] = actionability
    candidate["research_status_label"] = _ACTIONABILITY_LABELS[actionability]
    candidate["can_add_to_favorites"] = actionability in {
        "ready_now",
        "condition_order",
    }
    candidate["research_condition_ready"] = actionability == "condition_order"
    candidate["condition_order_ready"] = False
    candidate["execution_actionable"] = False
    candidate["execution_status"] = "research_only"
    return candidate


def _candidate_rank_score(candidate: Mapping[str, Any]) -> float:
    objective_score = float(candidate.get("objective_match_score") or 0) * 40
    actionability_score = {
        "ready_now": 40,
        "condition_order": 30,
        "blocked": 5,
        "quote_unavailable": 2,
        "incomplete": 0,
        "target_reached": -5,
        "expired": -8,
        "invalidated": -10,
    }.get(str(candidate.get("actionability") or ""), 0)
    plan = candidate.get("price_plan") if isinstance(candidate.get("price_plan"), Mapping) else {}
    entry = _finite_number(plan.get("entry_price"))
    stop = _finite_number(plan.get("stop_price"))
    target = _finite_number(plan.get("target_price"))
    reward_risk_score = 0.0
    if entry and stop is not None and target is not None and stop < entry < target:
        ratio = (target - entry) / (entry - stop)
        reward_risk_score = min(20.0, max(0.0, ratio * 8))
    priority = int(candidate.get("priority") or 999)
    profile = candidate.get("stock_profile")
    profile_confidence = (
        str(profile.get("confidence") or "missing")
        if isinstance(profile, Mapping)
        else "missing"
    )
    evidence_score = {"high": 8, "medium": 5, "low": 1, "missing": -6}.get(
        profile_confidence, -6
    )
    sizing = candidate.get("position_sizing")
    executable_score = (
        8
        if isinstance(sizing, Mapping) and sizing.get("status") == "sized"
        else -5
    )
    return round(
        objective_score
        + actionability_score
        + reward_risk_score
        + evidence_score
        + executable_score
        - min(priority, 50) * 0.1,
        2,
    )


def _infer_entry_strategy(
    plan: Mapping[str, Any],
    *,
    triggers: Mapping[str, Any],
    reference_price: Optional[float],
    entry_price: Optional[float],
) -> str:
    strategy = str(plan.get("entry_strategy") or "").strip().lower()
    if strategy in {"pullback", "breakout"}:
        return strategy

    breakout_price = _finite_number(
        plan.get("breakout_price"),
        triggers.get("breakout_price"),
    )
    if entry_price is not None and breakout_price is not None:
        if abs(entry_price - breakout_price) <= 0.011:
            return "breakout"
    if reference_price is not None and entry_price is not None:
        return "pullback" if entry_price <= reference_price else "breakout"
    return "reference"


def _build_candidate_price_plan(
    *,
    reference_price: Optional[float],
    plan: Mapping[str, Any],
    triggers: Mapping[str, Any],
    observation_zone: Optional[List[float]],
    risk_flags: List[Dict[str, str]],
) -> Dict[str, Any]:
    entry_price = _finite_number(
        plan.get("suggested_buy_price"),
        plan.get("entry_price"),
        triggers.get("breakout_price"),
        observation_zone[1] if observation_zone else None,
    )
    breakout_price = _finite_number(
        plan.get("breakout_price"),
        triggers.get("breakout_price"),
    )
    stop_price = _finite_number(
        plan.get("stop_loss_price"),
        plan.get("stop_price"),
        triggers.get("invalidation_price"),
    )
    target_price = _finite_number(plan.get("target_price"))
    plan_status = str(plan.get("status") or "reference_only")
    entry_strategy = _infer_entry_strategy(
        plan,
        triggers=triggers,
        reference_price=reference_price,
        entry_price=entry_price,
    )
    distance_to_entry_pct = None
    if reference_price and entry_price is not None:
        distance_to_entry_pct = round(
            (entry_price - reference_price) / reference_price * 100,
            2,
        )

    complete_price_order = bool(
        entry_price is not None
        and stop_price is not None
        and target_price is not None
        and stop_price < entry_price < target_price
    )
    plan_available = plan_status == "ok" and complete_price_order
    blocking_risk_flags = [flag for flag in risk_flags if _is_blocking_risk_flag(flag)]
    risk_blocked = bool(blocking_risk_flags)
    price_condition_met = False

    if not plan_available:
        entry_status = "plan_unavailable"
    elif reference_price is None:
        entry_status = "quote_unavailable"
    elif stop_price is not None and reference_price <= stop_price:
        entry_status = "invalidated"
    else:
        if entry_strategy == "pullback":
            price_condition_met = bool(reference_price <= entry_price)
            waiting_status = "waiting_pullback"
        else:
            price_condition_met = bool(reference_price >= entry_price)
            waiting_status = "waiting_breakout"
        if price_condition_met:
            entry_status = (
                "price_ready_risk_blocked" if risk_blocked else "price_ready"
            )
        else:
            entry_status = waiting_status

    strategy_label = _ENTRY_STRATEGY_LABELS[entry_strategy]
    entry_text = f"¥{entry_price:.2f}" if entry_price is not None else "-"
    current_text = f"¥{reference_price:.2f}" if reference_price is not None else "-"
    distance = abs(distance_to_entry_pct or 0.0)
    if entry_status == "waiting_pullback":
        guidance = (
            f"参考回落价 {entry_text}；当前 {current_text}，高出 {distance:.2f}%，"
            "等待回落，不追价。"
        )
    elif entry_status == "waiting_breakout":
        guidance = (
            f"参考突破价 {entry_text}；当前 {current_text}，距触发还差 {distance:.2f}%，"
            "等待有效突破。"
        )
    elif entry_status == "price_ready_risk_blocked":
        if any(flag.get("code") == "quote_not_actionable" for flag in risk_flags):
            guidance = (
                f"价格已进入 {strategy_label}{entry_text} 条件，但腾讯行情时效门槛未通过；"
                "刷新行情后再确认。"
            )
        else:
            guidance = (
                f"价格已进入 {strategy_label}{entry_text} 条件，但风险门槛未解除；"
                "暂不视为可执行信号。"
            )
    elif entry_status == "price_ready":
        guidance = (
            f"价格已进入 {strategy_label}{entry_text} 条件；"
            "仍需结合实时成交和风险门槛确认。"
        )
    elif entry_status == "invalidated":
        guidance = (
            f"当前 {current_text} 已触及失效价 ¥{stop_price:.2f}；"
            "原价格计划失效，需重新分析。"
        )
    elif entry_status == "quote_unavailable":
        guidance = f"{strategy_label}价 {entry_text}；腾讯现价缺失，暂无法判断。"
    elif entry_price is not None:
        guidance = f"现有 {entry_text} 仅作观察；技术价格计划未通过完整校验。"
    else:
        guidance = "技术价格计划未通过完整校验，暂无可靠参考入手价。"

    return {
        "observation_zone": observation_zone,
        "entry_strategy": entry_strategy,
        "entry_strategy_label": strategy_label,
        "entry_price": entry_price,
        "breakout_price": breakout_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "distance_to_entry_pct": distance_to_entry_pct,
        "price_condition_met": price_condition_met,
        "risk_blocked": risk_blocked,
        "blocking_risk_count": len(blocking_risk_flags),
        "entry_status": entry_status,
        "entry_status_label": _ENTRY_STATUS_LABELS[entry_status],
        "entry_guidance": guidance,
        "status": plan_status,
    }


def _build_horizon_plans(price_plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Derive explicit short, swing and position plans from one validated plan."""

    entry = _finite_number(price_plan.get("entry_price"))
    stop = _finite_number(price_plan.get("stop_price"))
    target = _finite_number(price_plan.get("target_price"))
    short = deepcopy(dict(price_plan))
    short.update(horizon="short", horizon_label="短线", validity="2-3个交易日")
    if entry is None or stop is None or target is None or not stop < entry < target:
        unavailable = {
            "status": "unavailable",
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
        }
        return {
            "short": short,
            "swing": {**unavailable, "horizon": "swing", "horizon_label": "波段", "validity": "2-6周"},
            "position": {**unavailable, "horizon": "position", "horizon_label": "中长期", "validity": "3-12个月"},
        }
    risk = entry - stop
    swing_stop = round(max(0.01, entry - risk * 1.6), 2)
    swing_target = round(max(target, entry + risk * 2.8), 2)
    position_stop = round(max(0.01, entry - risk * 2.5), 2)
    position_target = round(max(swing_target, entry + risk * 5.0), 2)
    return {
        "short": short,
        "swing": {
            "status": "reference",
            "horizon": "swing",
            "horizon_label": "波段",
            "validity": "2-6周",
            "entry_price": entry,
            "stop_price": swing_stop,
            "target_price": swing_target,
            "reward_risk_ratio": round((swing_target - entry) / (entry - swing_stop), 2),
            "basis": "短线技术计划按1.6倍风险距离放宽止损、2.8R设置目标。",
        },
        "position": {
            "status": "research_required",
            "horizon": "position",
            "horizon_label": "中长期",
            "validity": "3-12个月",
            "entry_price": entry,
            "stop_price": position_stop,
            "target_price": position_target,
            "reward_risk_ratio": round((position_target - entry) / (entry - position_stop), 2),
            "basis": "价格仅作估值研究锚点，需结合主营、景气和财报继续验证。",
        },
    }


def _enrich_saved_candidate(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    candidate = deepcopy(dict(value))
    objective = classify_investment_objective(
        candidate.get("code"),
        candidate.get("name"),
    )
    for key, default in objective.items():
        candidate.setdefault(key, default)
    saved_plan = (
        candidate.get("price_plan")
        if isinstance(candidate.get("price_plan"), Mapping)
        else {}
    )
    observation_zone = _normalize_observation_zone(
        saved_plan.get("observation_zone")
    )
    risk_flags = _normalize_risk_flags(candidate.get("risk_flags"))
    candidate["risk_flags"] = risk_flags
    rebuild_plan = dict(saved_plan)
    if not rebuild_plan.get("status") and all(
        _finite_number(rebuild_plan.get(key)) is not None
        for key in ("entry_price", "stop_price", "target_price")
    ):
        rebuild_plan["status"] = "ok"
    candidate["price_plan"] = _build_candidate_price_plan(
        reference_price=_finite_number(candidate.get("reference_price")),
        plan=rebuild_plan,
        triggers={
            "breakout_price": saved_plan.get("breakout_price"),
            "invalidation_price": saved_plan.get("stop_price"),
        },
        observation_zone=observation_zone,
        risk_flags=risk_flags,
    )
    candidate["plans"] = _build_horizon_plans(candidate["price_plan"])
    _apply_candidate_state(candidate)
    candidate["rank_score"] = _candidate_rank_score(candidate)
    return candidate


def normalize_ai_candidate(
    candidate: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    favorite_codes: set[str],
) -> Optional[Dict[str, Any]]:
    code = str(candidate.get("code") or "").strip()
    if _A_SHARE_CODE.fullmatch(code) is None:
        return None
    quote = candidate.get("quote") if isinstance(candidate.get("quote"), Mapping) else {}
    discovery = (
        candidate.get("discovery")
        if isinstance(candidate.get("discovery"), Mapping)
        else {}
    )
    tencent = discovery.get("tencent") if isinstance(discovery.get("tencent"), Mapping) else {}
    plan = (
        candidate.get("guarded_price_plan")
        if isinstance(candidate.get("guarded_price_plan"), Mapping)
        else {}
    )
    triggers = (
        candidate.get("triggers")
        if isinstance(candidate.get("triggers"), Mapping)
        else {}
    )
    observation_zone = _normalize_observation_zone(triggers.get("observation_zone"))
    reference_price = _finite_number(quote.get("price"), tencent.get("price"))
    risk_flags = _normalize_risk_flags(candidate.get("risk_flags"))
    price_plan = _build_candidate_price_plan(
        reference_price=reference_price,
        plan=plan,
        triggers=triggers,
        observation_zone=observation_zone,
        risk_flags=risk_flags,
    )
    objective = classify_investment_objective(code, candidate.get("name"))
    if candidate.get("objective_tier") in {"core", "related", "non_core"}:
        objective = {
            key: candidate.get(key, value)
            for key, value in objective.items()
        }
    normalized = {
        "code": code,
        "name": str(candidate.get("name") or code).strip()[:80],
        "market": "A股",
        "priority": int(candidate.get("priority") or 999),
        **objective,
        "reference_price": reference_price,
        "initial_reference_price": reference_price,
        "pct_change": _finite_number(quote.get("pct_change"), tencent.get("pct_change")),
        "trade_at": quote.get("trade_at"),
        "quote": {
            "price": reference_price,
            "source": str(
                quote.get("source")
                or quote.get("data_source")
                or tencent.get("source")
                or tencent.get("data_source")
                or "unknown"
            ).strip().lower(),
            "trade_at": quote.get("trade_at"),
            "quote_checked_at": quote.get("quote_checked_at"),
            "volume": _finite_number(quote.get("volume")),
            "amount": _finite_number(quote.get("amount")),
            "event_confirmation_required": True,
            "event_change_detected": False,
            "event_observed_at": None,
        },
        "price_plan": price_plan,
        "plans": _build_horizon_plans(price_plan),
        "reason_summary": _candidate_reason(candidate),
        "evidence": _candidate_evidence(candidate, context),
        "risk_flags": risk_flags,
        "favorite_status": "in_favorites" if code in favorite_codes else "not_added",
        "source": "public_full_market",
        "is_reference_only": True,
    }
    _apply_candidate_state(normalized)
    normalized["rank_score"] = _candidate_rank_score(normalized)
    return normalized


def normalize_ai_candidate_run(
    payload: Mapping[str, Any],
    *,
    max_candidates: int,
    favorite_codes: set[str],
) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    context = data.get("context") if isinstance(data.get("context"), Mapping) else {}
    raw_candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    candidates = [
        normalized
        for raw in raw_candidates
        if isinstance(raw, Mapping)
        for normalized in [
            normalize_ai_candidate(raw, context=context, favorite_codes=favorite_codes)
        ]
        if normalized is not None
    ]
    candidates.sort(
        key=lambda item: (
            _ACTIONABILITY_ORDER.get(str(item.get("actionability")), 99),
            objective_tier_rank(item.get("objective_tier")),
            -float(item.get("rank_score") or 0),
            item["priority"],
            item["code"],
        )
    )
    candidates = candidates[:max_candidates]
    discovery = (
        data.get("candidate_discovery")
        if isinstance(data.get("candidate_discovery"), Mapping)
        else {}
    )
    market_status = (
        data.get("market_status")
        if isinstance(data.get("market_status"), Mapping)
        else {}
    )
    market_session = (
        market_status.get("market_session")
        if isinstance(market_status.get("market_session"), Mapping)
        else {}
    )
    market_decision = (
        market_status.get("decision")
        if isinstance(market_status.get("decision"), Mapping)
        else {}
    )
    if market_decision.get("action") == "evaluate_candidates":
        market_regime = "green"
    elif market_decision.get("reason_code") == "breadth_confirmation_required":
        market_regime = "yellow"
    else:
        market_regime = "red"
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    objective_counts = {
        tier: sum(item.get("objective_tier") == tier for item in candidates)
        for tier in ("core", "related", "non_core")
    }
    actionability_counts = {
        status: sum(item.get("actionability") == status for item in candidates)
        for status in _ACTIONABILITY_LABELS
    }
    return {
        "status": "completed",
        "source": "public_full_market",
        "source_detail": meta.get("source"),
        "execution": {
            "actionable": False,
            "status": "research_only",
            "requires_daily_decision": True,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "actionability_counts": actionability_counts,
        "objective": {
            "id": INVESTMENT_OBJECTIVE["id"],
            "label": INVESTMENT_OBJECTIVE["label"],
            "description": INVESTMENT_OBJECTIVE["description"],
            "candidate_counts": objective_counts,
            "portfolio": deepcopy(INVESTMENT_OBJECTIVE["portfolio"]),
        },
        "discovery": {
            "benchmark_trade_date": discovery.get("benchmark_trade_date"),
            "universe_count": discovery.get("universe_count"),
            "eligible_count": discovery.get("eligible_count"),
            "selected_count": discovery.get("selected_count"),
            "technical_passed_count": discovery.get("technical_passed_count"),
            "earnings_selected_count": discovery.get("earnings_selected_count"),
            "total_coverage_ratio": discovery.get("total_coverage_ratio"),
            "permission_prefilter_excluded_count": discovery.get(
                "permission_prefilter_excluded_count", 0
            ),
            "permission_prefilter_excluded": deepcopy(
                discovery.get("permission_prefilter_excluded") or []
            ),
        },
        "market": {
            "session": market_session.get("session"),
            "is_trading_hours": market_session.get("is_trading_hours"),
            "local_time": market_session.get("local_time"),
            "regime": market_regime,
            "decision": market_decision.get("action"),
            "reason_code": market_decision.get("reason_code"),
        },
        "context": {
            "horizon": context.get("horizon") or "未来两个交易日",
            "technical_status": context.get("technical_deep_check_status"),
            "earnings_status": context.get("earnings_forecast_review_status"),
        },
        "disclaimer": str(
            data.get("disclaimer") or "仅供研究参考，不构成投资建议或交易指令。"
        ),
    }


class AICandidateService:
    def __init__(
        self,
        *,
        research_runner: Callable[[], Dict[str, Any]] = run_candidate_research,
        favorites: FavoritesService = favorites_service,
        quotes: Optional[TencentQuoteService] = None,
        stock_master: Any = None,
        macro_risk: Any = None,
        trading_calendar: Any = None,
    ) -> None:
        self._research_runner = research_runner
        self._favorites = favorites
        self._quotes = quotes or get_tencent_quote_service()
        self._stock_master = stock_master or StockMasterDataService()
        self._macro_risk = macro_risk or GlobalMacroRiskService()
        self._trading_calendar = trading_calendar or AShareCalendarService()
        self.db = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def _get_db(self):
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    @staticmethod
    def _serialize_run(document: Mapping[str, Any]) -> Dict[str, Any]:
        result = deepcopy(dict(document))
        result["run_id"] = str(result.pop("_id"))
        candidates = result.get("candidates")
        if isinstance(candidates, list):
            result["candidates"] = [
                _enrich_saved_candidate(candidate) for candidate in candidates
            ]
        for field in ("generated_at", "plan_expires_at", "expires_at", "updated_at"):
            value = result.get(field)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                result[field] = value.isoformat()
        return result

    @staticmethod
    def _serialize_job(document: Mapping[str, Any]) -> Dict[str, Any]:
        result = deepcopy(dict(document))
        result["job_id"] = str(result.pop("_id"))
        for field in ("created_at", "started_at", "completed_at", "expires_at"):
            value = result.get(field)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                result[field] = value.isoformat()
        return result

    @staticmethod
    def _update_performance(
        candidate: Dict[str, Any],
        *,
        current_price: Optional[float],
        checked_at: str,
        observation_key: str,
        session_high: Optional[float] = None,
        session_low: Optional[float] = None,
        benchmark_price: Optional[float] = None,
    ) -> None:
        if current_price is None or current_price <= 0:
            return
        performance = deepcopy(
            candidate.get("performance")
            if isinstance(candidate.get("performance"), Mapping)
            else {}
        )
        baseline = _finite_number(
            performance.get("baseline_price"),
            candidate.get("initial_reference_price"),
            candidate.get("reference_price"),
        )
        performance["baseline_price"] = baseline
        performance["latest_price"] = current_price
        performance["last_checked_at"] = checked_at
        is_new_observation = performance.get("last_observation_key") != observation_key
        if is_new_observation:
            performance["observation_count"] = int(
                performance.get("observation_count") or 0
            ) + 1
            performance["last_observation_key"] = observation_key
        if baseline and baseline > 0:
            current_return = round((current_price - baseline) / baseline * 100, 2)
            performance["return_since_generated_pct"] = current_return
            performance["max_return_pct"] = max(
                current_return,
                float(performance.get("max_return_pct") or current_return),
            )
            performance["min_return_pct"] = min(
                current_return,
                float(performance.get("min_return_pct") or current_return),
            )
        plan = candidate.get("price_plan") if isinstance(candidate.get("price_plan"), Mapping) else {}
        target = _finite_number(plan.get("target_price"))
        stop = _finite_number(plan.get("stop_price"))
        if target is not None and current_price >= target:
            performance.setdefault("target_hit_at", checked_at)
        if stop is not None and current_price <= stop:
            performance.setdefault("stop_hit_at", checked_at)
        shadow = deepcopy(
            performance.get("shadow_trade")
            if isinstance(performance.get("shadow_trade"), Mapping)
            else {}
        )
        status = str(shadow.get("status") or "waiting_entry")
        shadow.setdefault("status", status)
        entry = _finite_number(plan.get("entry_price"))
        strategy = str(plan.get("entry_strategy") or "pullback")
        allocation = candidate.get("portfolio_allocation")
        allocation = allocation if isinstance(allocation, Mapping) else {}
        quantity = int(allocation.get("quantity") or 0)
        low = session_low if session_low is not None else current_price
        high = session_high if session_high is not None else current_price
        entry_triggered = bool(
            entry
            and quantity > 0
            and (
                (strategy == "breakout" and high is not None and high >= entry)
                or (strategy != "breakout" and low is not None and low <= entry)
            )
        )
        if status == "waiting_entry" and entry_triggered and entry is not None:
            buy_fee = max(5.0, entry * quantity * 0.0003)
            shadow.update(
                status="active",
                entry_triggered_at=checked_at,
                entry_price=entry,
                quantity=quantity,
                invested_amount=round(entry * quantity + buy_fee, 2),
                buy_fee=round(buy_fee, 2),
                max_return_pct=0.0,
                min_return_pct=0.0,
                benchmark_entry_price=benchmark_price,
            )
            status = "active"
        if status == "active":
            actual_entry = _finite_number(shadow.get("entry_price"))
            actual_quantity = int(shadow.get("quantity") or 0)
            if actual_entry and actual_quantity > 0:
                exit_price = current_price
                exit_reason = None
                if stop is not None and low is not None and low <= stop:
                    exit_price, exit_reason = stop, "stop"
                elif target is not None and high is not None and high >= target:
                    exit_price, exit_reason = target, "target"
                gross_return = (current_price - actual_entry) / actual_entry * 100
                shadow["latest_price"] = current_price
                shadow["return_pct"] = round(gross_return, 2)
                shadow["max_return_pct"] = max(
                    float(shadow.get("max_return_pct") or gross_return), gross_return
                )
                shadow["min_return_pct"] = min(
                    float(shadow.get("min_return_pct") or gross_return), gross_return
                )
                benchmark_entry = _finite_number(shadow.get("benchmark_entry_price"))
                if benchmark_entry and benchmark_price:
                    benchmark_return = (
                        benchmark_price - benchmark_entry
                    ) / benchmark_entry * 100
                    shadow["benchmark_latest_price"] = benchmark_price
                    shadow["benchmark_return_pct"] = round(benchmark_return, 2)
                    shadow["alpha_pct"] = round(gross_return - benchmark_return, 2)
                if exit_reason and exit_price is not None:
                    gross_proceeds = exit_price * actual_quantity
                    sell_fee = max(5.0, gross_proceeds * 0.0003)
                    stamp_duty = gross_proceeds * 0.0005
                    net_pnl = (
                        gross_proceeds
                        - sell_fee
                        - stamp_duty
                        - float(shadow.get("invested_amount") or actual_entry * actual_quantity)
                    )
                    shadow.update(
                        status=f"closed_{exit_reason}",
                        closed_at=checked_at,
                        exit_price=round(exit_price, 4),
                        exit_reason=exit_reason,
                        sell_fee=round(sell_fee, 2),
                        stamp_duty=round(stamp_duty, 2),
                        net_pnl=round(net_pnl, 2),
                        net_return_pct=round(
                            net_pnl / float(shadow["invested_amount"]) * 100, 2
                        ),
                    )
        performance["shadow_trade"] = shadow
        candidate["performance"] = performance

    async def _candidate_governance(self, user_id: str) -> Dict[str, Any]:
        db = await self._get_db()
        try:
            settings = await db["user_holding_settings"].find_one(
                {"user_id": str(user_id)}
            )
        except Exception:
            settings = None
        return _candidate_governance_from_settings(settings)

    async def _run_research(
        self,
        governance: Mapping[str, Any],
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        try:
            parameters = inspect.signature(self._research_runner).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_kwargs or "excluded_code_reasons" in parameters:
            kwargs["excluded_code_reasons"] = {
                code: "user_excluded"
                for code in governance.get("excluded_codes") or []
            }
        if accepts_kwargs or "star_market_exclusion_reason" in parameters:
            star = governance.get("star_market")
            star = star if isinstance(star, Mapping) else {}
            if star.get("eligible") is not True:
                kwargs["star_market_exclusion_reason"] = (
                    "star_market_permission_denied"
                    if star.get("verified") is True
                    else "star_market_permission_unverified"
                )
        result = await run_in_threadpool(self._research_runner, **kwargs)
        return dict(result) if isinstance(result, Mapping) else {}

    @staticmethod
    def _apply_candidate_governance(
        document: Dict[str, Any],
        governance: Mapping[str, Any],
    ) -> None:
        permitted: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        for candidate in document.get("candidates", []):
            if not isinstance(candidate, Mapping):
                continue
            item = deepcopy(dict(candidate))
            reason = _candidate_governance_reason(item.get("code"), governance)
            if reason:
                item["plan_id"] = _shadow_plan_identity(document, item)
                _stop_governance_shadow_tracking(item, reason=reason)
                excluded.append(
                    {
                        **item,
                        "governance_status": "excluded",
                        "governance_reason": reason,
                        "execution_actionable": False,
                        "execution_status": "governance_excluded",
                    }
                )
            else:
                permitted.append(item)
        existing_excluded = [
            deepcopy(dict(item))
            for item in document.get("governance_excluded_candidates", [])
            if isinstance(item, Mapping)
        ]
        for item in existing_excluded:
            reason = str(
                item.get("governance_reason")
                or _candidate_governance_reason(item.get("code"), governance)
                or "governance_excluded"
            )
            _stop_governance_shadow_tracking(item, reason=reason)
        known = {
            (
                str(item.get("code") or ""),
                str(item.get("plan_id") or ""),
                str(item.get("governance_reason") or ""),
            )
            for item in existing_excluded
        }
        for item in excluded:
            key = (
                str(item.get("code") or ""),
                str(item.get("plan_id") or ""),
                str(item.get("governance_reason") or ""),
            )
            if key not in known:
                existing_excluded.append(item)
                known.add(key)
        document["candidates"] = permitted
        document["governance"] = {
            "excluded_codes": list(governance.get("excluded_codes") or []),
            "star_market": deepcopy(dict(governance.get("star_market") or {})),
            "excluded_count": len(existing_excluded),
            "execution_scope": "candidate_research_only",
        }
        document["governance_excluded_candidates"] = existing_excluded

    async def _account_context(self, user_id: str) -> Dict[str, Any]:
        db = await self._get_db()
        try:
            settings_doc = await db["user_holding_settings"].find_one(
                {"user_id": str(user_id)}
            )
            cursor = db["user_holdings"].find(
                {"user_id": str(user_id)},
                {"code": 1, "quantity": 1, "cost_price": 1, "_id": 0},
            )
            holdings = await cursor.to_list(length=None)
        except Exception:
            settings_doc = None
            holdings = []
        holding_values: Dict[str, float] = {}
        total_holding_cost = 0.0
        for holding in holdings or []:
            try:
                value = max(0.0, float(holding.get("quantity") or 0)) * max(
                    0.0, float(holding.get("cost_price") or 0)
                )
            except (TypeError, ValueError):
                continue
            code = str(holding.get("code") or "").strip()
            if code:
                holding_values[code] = holding_values.get(code, 0.0) + value
            total_holding_cost += value
        try:
            total_assets = float((settings_doc or {}).get("total_assets") or 0)
        except (TypeError, ValueError):
            total_assets = 0.0
        if total_assets <= 0:
            total_assets = total_holding_cost
        available_cash = max(0.0, total_assets - total_holding_cost)
        exposure_pct = (
            total_holding_cost / total_assets * 100 if total_assets > 0 else 0.0
        )
        return {
            "total_assets": round(total_assets, 2),
            "available_cash": round(available_cash, 2),
            "current_exposure_pct": round(exposure_pct, 2),
            "holding_values": holding_values,
            "execution_capabilities": deepcopy(
                (settings_doc or {}).get("execution_capabilities") or {}
            ),
            "excluded_codes": sorted(
                _normalized_code_set((settings_doc or {}).get("excluded_codes"))
            ),
        }

    async def _apply_objective_profiles(self, document: Dict[str, Any]) -> None:
        candidates = [
            item for item in document.get("candidates", []) if isinstance(item, dict)
        ]
        codes = [str(item.get("code") or "") for item in candidates if item.get("code")]
        try:
            db = await self._get_db()
            if getattr(self._stock_master, "db", None) is None:
                self._stock_master.db = db
            profiles = await self._stock_master.resolve_many(codes)
        except Exception:
            logger.exception("Failed to resolve authoritative stock profiles")
            profiles = {}
        for candidate in candidates:
            code = str(candidate.get("code") or "")
            stock_profile = profiles.get(code) or {
                "code": code,
                "status": "missing",
                "confidence": "missing",
                "industry": None,
                "main_business": None,
                "source": None,
                "evidence": [],
            }
            profile = classify_investment_objective(
                code,
                candidate.get("name"),
                industry=stock_profile.get("industry"),
            )
            candidate.update(profile)
            candidate["stock_profile"] = stock_profile
            if stock_profile.get("industry"):
                candidate["industry"] = stock_profile["industry"]
            candidate["rank_score"] = _candidate_rank_score(candidate)
        objective = document.setdefault("objective", {})
        objective["candidate_counts"] = {
            tier: sum(item.get("objective_tier") == tier for item in candidates)
            for tier in ("core", "related", "non_core")
        }

    async def _apply_macro_policy(self, document: Dict[str, Any]) -> None:
        market = document.setdefault("market", {})
        domestic_regime = str(market.get("domestic_regime") or market.get("regime") or "yellow")
        market["domestic_regime"] = domestic_regime
        try:
            db = await self._get_db()
            if getattr(self._macro_risk, "db", None) is None:
                self._macro_risk.db = db
            macro = await self._macro_risk.get_current()
        except Exception as exc:
            logger.warning("Global macro risk unavailable: %s", exc)
            macro = {
                "status": "unavailable",
                "regime": "yellow",
                "score": None,
                "factors": [],
                "reason": str(exc)[:240],
            }
        macro_regime = str(macro.get("regime") or "yellow")
        regime_rank = {"green": 0, "yellow": 1, "red": 2}
        combined = max(
            (domestic_regime, macro_regime),
            key=lambda value: regime_rank.get(value, 1),
        )
        market["regime"] = combined
        market["macro_risk"] = macro
        market["regime_reason"] = (
            "global_macro_downgrade"
            if regime_rank.get(macro_regime, 1) > regime_rank.get(domestic_regime, 1)
            else "domestic_market_regime"
        )

    async def _apply_account_policy(
        self,
        document: Dict[str, Any],
        *,
        user_id: str,
    ) -> None:
        account = await self._account_context(user_id)
        market = document.get("market") if isinstance(document.get("market"), Mapping) else {}
        policy = build_dynamic_portfolio_policy(
            total_assets=account["total_assets"],
            current_exposure_pct=account["current_exposure_pct"],
            market_regime=str(market.get("regime") or "yellow"),
        )
        objective = document.setdefault("objective", {})
        objective["portfolio"] = policy
        document["account"] = {
            key: value for key, value in account.items() if key != "holding_values"
        }
        market_blocked = float(policy.get("available_new_exposure_pct") or 0) <= 0
        for candidate in document.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            plan = candidate.get("price_plan") if isinstance(candidate.get("price_plan"), Mapping) else {}
            candidate["position_sizing"] = calculate_candidate_position_sizing(
                entry_price=plan.get("entry_price"),
                stop_price=plan.get("stop_price"),
                total_assets=account["total_assets"],
                available_cash=account["available_cash"],
                current_symbol_value=account["holding_values"].get(
                    str(candidate.get("code") or ""), 0.0
                ),
                policy=policy,
            )
            candidate["portfolio_gate"] = {
                "blocked": market_blocked,
                "reason_code": (
                    "market_regime_new_exposure_blocked"
                    if market_blocked
                    else "new_exposure_available"
                ),
                "market_regime": policy.get("market_regime"),
                "available_new_exposure_pct": policy.get(
                    "available_new_exposure_pct"
                ),
            }
            _apply_candidate_state(candidate)
            candidate["rank_score"] = _candidate_rank_score(candidate)
        candidates = [
            candidate
            for candidate in document.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        candidates.sort(
            key=lambda item: (
                _ACTIONABILITY_ORDER.get(str(item.get("actionability")), 99),
                objective_tier_rank(item.get("objective_tier")),
                -float(item.get("rank_score") or 0),
            )
        )
        portfolio_plan = allocate_candidate_portfolio(
            candidates,
            total_assets=account["total_assets"],
            available_cash=account["available_cash"],
            policy=policy,
        )
        allocation_by_code = {
            str(item.get("code") or ""): item
            for item in portfolio_plan.get("allocations", [])
        }
        for candidate in candidates:
            candidate["portfolio_allocation"] = allocation_by_code.get(
                str(candidate.get("code") or ""), {}
            )
        candidates.sort(
            key=lambda item: (
                _ALLOCATION_ORDER.get(
                    str((item.get("portfolio_allocation") or {}).get("status")), 9
                ),
                _ACTIONABILITY_ORDER.get(str(item.get("actionability")), 99),
                objective_tier_rank(item.get("objective_tier")),
                -float(item.get("rank_score") or 0),
            )
        )
        document["candidates"] = candidates
        document["portfolio_plan"] = portfolio_plan

    @staticmethod
    def _update_run_counts(document: Dict[str, Any]) -> None:
        candidates = [
            item for item in document.get("candidates", []) if isinstance(item, Mapping)
        ]
        document["actionability_counts"] = {
            status: sum(item.get("actionability") == status for item in candidates)
            for status in _ACTIONABILITY_LABELS
        }
        document["candidate_count"] = len(candidates)

    async def _publish_transition(
        self,
        *,
        user_id: str,
        candidate: Dict[str, Any],
        previous_actionability: Optional[str],
    ) -> None:
        current = str(candidate.get("actionability") or "")
        if current == previous_actionability or current not in {
            "ready_now",
            "invalidated",
            "target_reached",
        }:
            return
        notified_events = list(candidate.get("notified_events") or [])
        event_key = f"{current}:{candidate.get('trade_at') or candidate.get('quote_checked_at')}"
        if event_key in notified_events:
            return
        try:
            await get_notifications_service().create_and_publish(
                NotificationCreate(
                    user_id=str(user_id),
                    type="alert",
                    title=f"{candidate.get('name') or candidate.get('code')}：{_ACTIONABILITY_LABELS[current]}",
                    content=str(
                        (candidate.get("price_plan") or {}).get("entry_guidance")
                        or candidate.get("reason_summary")
                        or "候选状态发生变化。"
                    ),
                    link="/screening",
                    source="ai_candidate_tracking",
                    severity="warning" if current == "invalidated" else "success",
                    metadata={
                        "code": candidate.get("code"),
                        "actionability": current,
                    },
                )
            )
            candidate["notified_events"] = [*notified_events[-9:], event_key]
        except Exception as exc:
            logger.warning("AI candidate notification failed: %s", exc)

    async def _refresh_document(
        self,
        document: Dict[str, Any],
        *,
        user_id: str,
        persist: bool = True,
        notify: bool = True,
    ) -> Dict[str, Any]:
        governance = await self._candidate_governance(str(user_id))
        self._apply_candidate_governance(document, governance)
        refresh_now = datetime.now(timezone.utc)
        checked_at = refresh_now.isoformat()
        candidates = [
            item for item in document.get("candidates", []) if isinstance(item, dict)
        ]
        codes = [str(item.get("code") or "") for item in candidates if item.get("code")]
        quote_map = await self._quotes.get_quotes([*codes, "sh000300"]) if codes else {}
        benchmark_quote = quote_map.pop("000300", {})
        benchmark_price = _finite_number(
            benchmark_quote.get("price") if isinstance(benchmark_quote, Mapping) else None,
            benchmark_quote.get("close") if isinstance(benchmark_quote, Mapping) else None,
        )
        if benchmark_price is not None:
            benchmark = document.setdefault("benchmark", {})
            benchmark.setdefault("baseline_price", benchmark_price)
            benchmark["latest_price"] = benchmark_price
            benchmark["code"] = "000300"
            benchmark["name"] = "沪深300"
            benchmark["return_since_generated_pct"] = round(
                (benchmark_price - float(benchmark["baseline_price"]))
                / float(benchmark["baseline_price"])
                * 100,
                2,
            )
            benchmark["checked_at"] = checked_at
        favorite_codes = await self._favorites.get_favorite_codes(user_id)
        plan_expires_at = document.get("plan_expires_at")
        if isinstance(plan_expires_at, datetime):
            if plan_expires_at.tzinfo is None:
                plan_expires_at = plan_expires_at.replace(tzinfo=timezone.utc)
            plan_expired = refresh_now >= plan_expires_at
        else:
            plan_expired = False
        for candidate in candidates:
            enriched_before = _enrich_saved_candidate(candidate)
            previous_actionability = str(
                enriched_before.get("actionability") or ""
            ) if isinstance(enriched_before, Mapping) else None
            quote = quote_map.get(str(candidate.get("code") or ""), {})
            previous_quote = (
                candidate.get("quote")
                if isinstance(candidate.get("quote"), Mapping)
                else {}
            )
            current_price = _finite_number(
                quote.get("price") if isinstance(quote, Mapping) else None,
                quote.get("close") if isinstance(quote, Mapping) else None,
                quote.get("current_price") if isinstance(quote, Mapping) else None,
            )
            quote_source = str(
                quote.get("source") or quote.get("data_source") or "unknown"
            ).strip().lower()
            current_quote = {
                "price": current_price,
                "source": quote_source,
                "trade_at": quote.get("trade_at"),
                "quote_checked_at": checked_at,
                "volume": _finite_number(quote.get("volume")),
                "amount": _finite_number(quote.get("amount")),
            }
            event_change_detected = _quote_event_changed(
                previous_quote,
                current_quote,
            )
            current_quote.update(
                {
                    "event_confirmation_required": True,
                    "event_change_detected": event_change_detected,
                    "event_observed_at": (
                        checked_at
                        if event_change_detected
                        else previous_quote.get("event_observed_at")
                    ),
                }
            )
            candidate["quote"] = current_quote
            if current_price is not None:
                candidate.setdefault(
                    "initial_reference_price", candidate.get("reference_price")
                )
                candidate["reference_price"] = current_price
                candidate["pct_change"] = _finite_number(quote.get("pct_chg"))
                candidate["trade_at"] = quote.get("trade_at")
                candidate["quote_source"] = quote_source
                candidate["quote_checked_at"] = checked_at
            refreshed = _enrich_saved_candidate(candidate)
            if isinstance(refreshed, dict):
                candidate.clear()
                candidate.update(refreshed)
            candidate["plan_expired"] = plan_expired
            if event_change_detected:
                self._update_performance(
                    candidate,
                    current_price=current_price,
                    checked_at=checked_at,
                    observation_key=str(
                        (
                            quote.get("trade_at")
                            if isinstance(quote, Mapping)
                            else None
                        )
                        or checked_at
                    ),
                    session_high=_finite_number(
                        quote.get("high") if isinstance(quote, Mapping) else None
                    ),
                    session_low=_finite_number(
                        quote.get("low") if isinstance(quote, Mapping) else None
                    ),
                    benchmark_price=benchmark_price,
                )
            if plan_expired:
                performance = candidate.get("performance")
                if isinstance(performance, dict):
                    shadow = performance.get("shadow_trade")
                    if isinstance(shadow, dict) and shadow.get("status") == "waiting_entry":
                        shadow["status"] = "expired_untriggered"
                        shadow["expired_at"] = checked_at
            _apply_candidate_state(candidate)
            candidate["rank_score"] = _candidate_rank_score(candidate)
            candidate["favorite_status"] = (
                "in_favorites"
                if candidate.get("code") in favorite_codes
                else "not_added"
            )
            if notify and event_change_detected:
                await self._publish_transition(
                    user_id=user_id,
                    candidate=candidate,
                    previous_actionability=previous_actionability,
                )
        await self._apply_objective_profiles(document)
        await self._apply_macro_policy(document)
        market = document.setdefault("market", {})
        discovery_snapshot = market.get("discovery_snapshot")
        if not isinstance(discovery_snapshot, Mapping):
            market["discovery_snapshot"] = {
                key: deepcopy(market.get(key))
                for key in (
                    "session",
                    "is_trading_hours",
                    "local_time",
                    "decision",
                    "reason_code",
                )
            }
        market.update(
            {
                "session": "quote_refresh_only",
                "is_trading_hours": False,
                "local_time": refresh_now.astimezone(
                    timezone(timedelta(hours=8))
                ).isoformat(),
                "execution_usable": False,
                "execution_status": "research_snapshot_not_execution_decision",
                "decision": None,
                "reason_code": "daily_decision_required",
            }
        )
        document["execution"] = {
            "actionable": False,
            "status": "research_only",
            "requires_daily_decision": True,
        }
        candidates.sort(
            key=lambda item: (
                _ACTIONABILITY_ORDER.get(str(item.get("actionability")), 99),
                objective_tier_rank(item.get("objective_tier")),
                -float(item.get("rank_score") or 0),
                int(item.get("priority") or 999),
            )
        )
        document["candidates"] = candidates
        document["updated_at"] = datetime.now(timezone.utc)
        document["quote_refreshed_at"] = checked_at
        await self._apply_account_policy(document, user_id=user_id)
        self._update_run_counts(document)
        update_tracking = getattr(
            self._favorites, "update_ai_candidate_tracking", None
        )
        if callable(update_tracking):
            generated_at = document.get("generated_at")
            generated_at_text = (
                generated_at.isoformat()
                if isinstance(generated_at, datetime)
                else str(generated_at or "")
            )
            for candidate in candidates:
                if candidate.get("code") not in favorite_codes:
                    continue
                tracking_candidate = deepcopy(candidate)
                tracking_candidate["run_id"] = str(document.get("_id") or "")
                tracking_candidate["generated_at"] = generated_at_text
                await update_tracking(
                    str(user_id),
                    str(candidate.get("code")),
                    tracking_candidate,
                )
        if persist:
            db = await self._get_db()
            await db["ai_candidate_runs"].update_one(
                {"_id": document["_id"], "user_id": str(user_id)},
                {
                    "$set": {
                        "candidates": deepcopy(document["candidates"]),
                        "actionability_counts": document["actionability_counts"],
                        "objective": deepcopy(document.get("objective")),
                        "account": deepcopy(document.get("account")),
                        "portfolio_plan": deepcopy(document.get("portfolio_plan")),
                        "market": deepcopy(document.get("market")),
                        "execution": deepcopy(document.get("execution")),
                        "governance": deepcopy(document.get("governance")),
                        "governance_excluded_candidates": deepcopy(
                            document.get("governance_excluded_candidates")
                        ),
                        "benchmark": deepcopy(document.get("benchmark")),
                        "updated_at": document["updated_at"],
                        "quote_refreshed_at": checked_at,
                    }
                },
            )
        return document

    async def run(self, user_id: str, *, max_candidates: int = 5) -> Dict[str, Any]:
        favorite_codes = await self._favorites.get_favorite_codes(user_id)
        governance = await self._candidate_governance(str(user_id))
        payload = await self._run_research(governance)
        normalized = normalize_ai_candidate_run(
            payload,
            max_candidates=max(20, max_candidates * 4),
            favorite_codes=favorite_codes,
        )
        now = datetime.now(timezone.utc)
        document = {
            "_id": ObjectId(),
            "user_id": str(user_id),
            "generated_at": now,
            "plan_expires_at": now + timedelta(days=3),
            "expires_at": now + timedelta(days=AI_CANDIDATE_RUN_TTL_DAYS),
            **normalized,
        }
        self._apply_candidate_governance(document, governance)
        await self._apply_objective_profiles(document)
        await self._apply_macro_policy(document)
        await self._apply_account_policy(document, user_id=str(user_id))
        document["candidates"] = list(document.get("candidates", []))[:max_candidates]
        await self._apply_account_policy(document, user_id=str(user_id))
        self._update_run_counts(document)
        db = await self._get_db()
        await db["ai_candidate_runs"].insert_one(deepcopy(document))
        reconcile = getattr(self._favorites, "reconcile_ai_candidate_lifecycle", None)
        if callable(reconcile):
            await reconcile(
                str(user_id),
                current_run_id=str(document["_id"]),
                current_codes=[
                    str(item.get("code") or "")
                    for item in document.get("candidates", [])
                    if isinstance(item, Mapping)
                ],
                generated_at=now,
            )
        update_tracking = getattr(self._favorites, "update_ai_candidate_tracking", None)
        if callable(update_tracking):
            for candidate in document.get("candidates", []):
                if (
                    not isinstance(candidate, Mapping)
                    or candidate.get("code") not in favorite_codes
                ):
                    continue
                tracking_candidate = deepcopy(dict(candidate))
                tracking_candidate["run_id"] = str(document["_id"])
                tracking_candidate["generated_at"] = now.isoformat()
                await update_tracking(
                    str(user_id),
                    str(candidate.get("code")),
                    tracking_candidate,
                )
        return self._serialize_run(document)

    async def _execute_job(
        self,
        *,
        job_id: ObjectId,
        user_id: str,
        max_candidates: int,
    ) -> None:
        db = await self._get_db()
        jobs = db["ai_candidate_jobs"]
        await jobs.update_one(
            {"_id": job_id, "user_id": str(user_id)},
            {
                "$set": {
                    "status": "running",
                    "started_at": datetime.now(timezone.utc),
                    "progress": {"stage": "full_market_research", "percent": 10},
                }
            },
        )
        try:
            result = await self.run(user_id, max_candidates=max_candidates)
            await jobs.update_one(
                {"_id": job_id, "user_id": str(user_id)},
                {
                    "$set": {
                        "status": "completed",
                        "completed_at": datetime.now(timezone.utc),
                        "run_id": result.get("run_id"),
                        "result": result,
                        "progress": {"stage": "completed", "percent": 100},
                    }
                },
            )
        except Exception as exc:
            logger.exception("AI candidate background research failed")
            details = getattr(exc, "details", None)
            stage = getattr(exc, "stage", None)
            if not stage and isinstance(details, Mapping):
                stage = details.get("stage")
            await jobs.update_one(
                {"_id": job_id, "user_id": str(user_id)},
                {
                    "$set": {
                        "status": "failed",
                        "completed_at": datetime.now(timezone.utc),
                        "error": {
                            "code": getattr(exc, "code", "candidate_research_failed"),
                            "message": str(getattr(exc, "message", exc))[:500],
                            "stage": str(stage) if stage else None,
                        },
                        "progress": {"stage": "failed", "percent": 100},
                    }
                },
            )

    async def start_run(
        self,
        user_id: str,
        *,
        max_candidates: int = 5,
    ) -> Dict[str, Any]:
        db = await self._get_db()
        jobs = db["ai_candidate_jobs"]
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=30)
        try:
            await jobs.update_many(
                {
                    "user_id": str(user_id),
                    "status": {"$in": ["queued", "running"]},
                    "created_at": {"$lt": stale_before},
                },
                {
                    "$set": {
                        "status": "failed",
                        "completed_at": datetime.now(timezone.utc),
                        "error": {
                            "code": "candidate_job_interrupted",
                            "message": "后台任务已中断，请重新运行。",
                        },
                    }
                },
            )
        except AttributeError:
            pass
        active = await jobs.find_one(
            {
                "user_id": str(user_id),
                "status": {"$in": ["queued", "running"]},
                "created_at": {"$gte": stale_before},
            },
            sort=[("created_at", -1)],
        )
        if active:
            return self._serialize_job(active)
        now = datetime.now(timezone.utc)
        document = {
            "_id": ObjectId(),
            "user_id": str(user_id),
            "status": "queued",
            "progress": {"stage": "queued", "percent": 0},
            "max_candidates": max_candidates,
            "created_at": now,
            "expires_at": now + timedelta(days=AI_CANDIDATE_JOB_TTL_DAYS),
        }
        await jobs.insert_one(deepcopy(document))
        task = asyncio.create_task(
            self._execute_job(
                job_id=document["_id"],
                user_id=str(user_id),
                max_candidates=max_candidates,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return self._serialize_job(document)

    async def get_job(self, user_id: str, job_id: str) -> Dict[str, Any]:
        if not ObjectId.is_valid(job_id):
            raise AICandidateRunNotFoundError(job_id)
        db = await self._get_db()
        document = await db["ai_candidate_jobs"].find_one(
            {"_id": ObjectId(job_id), "user_id": str(user_id)}
        )
        if not document:
            raise AICandidateRunNotFoundError(job_id)
        return self._serialize_job(document)

    async def refresh_all_active_runs(self) -> Dict[str, Any]:
        db = await self._get_db()
        runs = db["ai_candidate_runs"]
        cursor = runs.find(
            {"expires_at": {"$gt": datetime.now(timezone.utc)}}
        ).sort("generated_at", -1)
        documents = await cursor.to_list(length=500)
        refreshed_users: set[str] = set()
        refreshed_historical_runs = 0
        governance_cleaned_runs = 0
        failed_users: List[str] = []
        governance_by_user: Dict[str, Dict[str, Any]] = {}
        today = datetime.now(timezone.utc).date()
        for document in documents:
            user_id = str(document.get("user_id") or "")
            if not user_id:
                continue
            governance = governance_by_user.get(user_id)
            if governance is None:
                governance = await self._candidate_governance(user_id)
                governance_by_user[user_id] = governance
            candidate_count_before = len(document.get("candidates") or [])
            excluded_count_before = len(
                document.get("governance_excluded_candidates") or []
            )
            self._apply_candidate_governance(document, governance)
            governance_changed = (
                len(document.get("candidates") or []) != candidate_count_before
                or len(document.get("governance_excluded_candidates") or [])
                != excluded_count_before
            )
            if governance_changed:
                await runs.update_one(
                    {"_id": document["_id"], "user_id": user_id},
                    {
                        "$set": {
                            "candidates": deepcopy(document.get("candidates") or []),
                            "governance": deepcopy(document.get("governance") or {}),
                            "governance_excluded_candidates": deepcopy(
                                document.get("governance_excluded_candidates") or []
                            ),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
                governance_cleaned_runs += 1
            is_latest = user_id not in refreshed_users
            refreshed_at = document.get("quote_refreshed_at")
            if isinstance(refreshed_at, str):
                try:
                    refreshed_at = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
                except ValueError:
                    refreshed_at = None
            already_refreshed_today = (
                isinstance(refreshed_at, datetime) and refreshed_at.date() == today
            )
            if not is_latest and already_refreshed_today:
                continue
            if is_latest:
                refreshed_users.add(user_id)
            try:
                await self._refresh_document(
                    document,
                    user_id=user_id,
                    notify=is_latest,
                )
                if not is_latest:
                    refreshed_historical_runs += 1
            except Exception:
                if user_id not in failed_users:
                    failed_users.append(user_id)
                logger.exception("Scheduled AI candidate refresh failed: user=%s", user_id)
        return {
            "refreshed_user_count": len(refreshed_users) - len(failed_users),
            "refreshed_historical_run_count": refreshed_historical_runs,
            "governance_cleaned_run_count": governance_cleaned_runs,
            "failed_user_count": len(failed_users),
        }

    async def start_daily_research_for_active_users(self) -> Dict[str, Any]:
        db = await self._get_db()
        if getattr(self._trading_calendar, "db", None) is None:
            self._trading_calendar.db = db
        calendar = await self._trading_calendar.is_trading_day()
        if not calendar.get("is_trading_day"):
            return {
                "active_user_count": 0,
                "started_count": 0,
                "failed_count": 0,
                "skipped": True,
                "reason": "not_a_share_trading_day",
                "calendar": calendar,
            }
        user_ids: List[str] = []
        try:
            user_cursor = db["users"].find(
                {"is_active": {"$ne": False}},
                {"_id": 1},
            )
            for user in await user_cursor.to_list(length=500):
                user_id = str(user.get("_id") or "")
                if user_id and user_id not in user_ids:
                    user_ids.append(user_id)
        except Exception:
            logger.exception("Failed to load active users for daily candidate research")
        cursor = db["ai_candidate_runs"].find(
            {}, {"user_id": 1, "generated_at": 1}
        ).sort("generated_at", -1)
        documents = await cursor.to_list(length=500)
        for document in documents:
            user_id = str(document.get("user_id") or "")
            if user_id and user_id not in user_ids:
                user_ids.append(user_id)
        started = 0
        failed = 0
        for user_id in user_ids:
            try:
                job = await self.start_run(user_id, max_candidates=5)
                if job.get("status") == "queued":
                    started += 1
            except Exception:
                failed += 1
                logger.exception("Daily AI candidate research failed to start: user=%s", user_id)
        return {
            "active_user_count": len(user_ids),
            "started_count": started,
            "failed_count": failed,
        }

    async def performance_summary(self, user_id: str) -> Dict[str, Any]:
        db = await self._get_db()
        governance = await self._candidate_governance(str(user_id))
        cursor = db["ai_candidate_runs"].find(
            {"user_id": str(user_id)},
            {"candidates": 1, "generated_at": 1, "plan_expires_at": 1},
        ).sort("generated_at", -1).limit(30)
        documents = await cursor.to_list(length=30)
        rows: List[Dict[str, Any]] = []
        seen_plan_ids: set[str] = set()
        excluded_by_reason: Dict[str, int] = {}
        duplicate_plan_count = 0
        raw_tracked_item_count = 0
        for document in documents:
            for candidate in document.get("candidates", []):
                if not isinstance(candidate, Mapping):
                    continue
                performance = candidate.get("performance")
                if not isinstance(performance, Mapping):
                    continue
                raw_tracked_item_count += 1
                governance_reason = _candidate_governance_reason(
                    candidate.get("code"),
                    governance,
                )
                if governance_reason:
                    excluded_by_reason[governance_reason] = (
                        excluded_by_reason.get(governance_reason, 0) + 1
                    )
                    continue
                plan_identity = _shadow_plan_identity(document, candidate)
                if plan_identity in seen_plan_ids:
                    duplicate_plan_count += 1
                    continue
                seen_plan_ids.add(plan_identity)
                shadow = performance.get("shadow_trade")
                if isinstance(shadow, Mapping):
                    value = _finite_number(
                        shadow.get("net_return_pct"), shadow.get("return_pct")
                    )
                    status = shadow.get("status")
                    entry_triggered = status not in {
                        None,
                        "waiting_entry",
                        "expired_untriggered",
                    }
                    target_hit = status == "closed_target"
                    stop_hit = status == "closed_stop"
                    max_return = shadow.get("max_return_pct")
                    min_return = shadow.get("min_return_pct")
                    metric_basis = "shadow_trade"
                else:
                    value = _finite_number(performance.get("return_since_generated_pct"))
                    status = "legacy_generated_baseline"
                    entry_triggered = False
                    target_hit = bool(performance.get("target_hit_at"))
                    stop_hit = bool(performance.get("stop_hit_at"))
                    max_return = performance.get("max_return_pct")
                    min_return = performance.get("min_return_pct")
                    shadow = {}
                    metric_basis = "legacy_generated_baseline"
                if value is None:
                    continue
                rows.append(
                    {
                        "run_id": str(document.get("_id") or ""),
                        "plan_id": plan_identity,
                        "generated_at": document.get("generated_at"),
                        "code": candidate.get("code"),
                        "name": candidate.get("name"),
                        "status": status,
                        "metric_basis": metric_basis,
                        "entry_triggered": entry_triggered,
                        "entry_price": shadow.get("entry_price"),
                        "quantity": shadow.get("quantity"),
                        "return_pct": value,
                        "net_pnl": shadow.get("net_pnl"),
                        "max_return_pct": max_return,
                        "min_return_pct": min_return,
                        "target_hit": target_hit,
                        "stop_hit": stop_hit,
                        "observation_count": performance.get("observation_count", 0),
                        "statistics_scope": "candidate_shadow_diagnostic",
                        "counts_as_governed_decision_sample": False,
                        "eligible_for_learning": False,
                        "represents_real_account_position": False,
                    }
                )
        shadow_rows = [row for row in rows if row["metric_basis"] == "shadow_trade"]
        legacy_rows = [
            row for row in rows if row["metric_basis"] == "legacy_generated_baseline"
        ]
        triggered = [row for row in shadow_rows if row["entry_triggered"]]
        returns = [
            float(row["return_pct"])
            for row in triggered
            if row.get("return_pct") is not None
        ]
        closed = [
            row for row in triggered if str(row.get("status") or "").startswith("closed_")
        ]
        return {
            "statistics_scope": "candidate_shadow_diagnostics",
            "description": (
                "候选影子结果仅用于诊断候选生成，不代表真实账户持仓，"
                "不进入受治理决策绩效或学习样本。"
            ),
            "sample_count": len(shadow_rows),
            "diagnostic_sample_count": len(shadow_rows),
            "governed_decision_sample_count": 0,
            "learning_eligible_count": 0,
            "legacy_baseline_count": len(legacy_rows),
            "total_item_count": len(rows),
            "raw_tracked_item_count": raw_tracked_item_count,
            "duplicate_plan_count": duplicate_plan_count,
            "governance_excluded_count": sum(excluded_by_reason.values()),
            "governance_excluded_by_reason": dict(sorted(excluded_by_reason.items())),
            "triggered_count": len(triggered),
            "closed_count": len(closed),
            "average_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "positive_count": sum(value > 0 for value in returns),
            "closed_win_rate_pct": round(
                sum(float(row.get("net_pnl") or 0) > 0 for row in closed)
                / len(closed)
                * 100,
                2,
            )
            if closed
            else None,
            "target_hit_count": sum(bool(row["target_hit"]) for row in shadow_rows),
            "stop_hit_count": sum(bool(row["stop_hit"]) for row in shadow_rows),
            "items": rows[:100],
        }

    async def latest(
        self,
        user_id: str,
        *,
        refresh_quotes: bool = True,
    ) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        document = await db["ai_candidate_runs"].find_one(
            {"user_id": str(user_id)},
            sort=[("generated_at", -1)],
        )
        if not document:
            return None
        if refresh_quotes:
            document = await self._refresh_document(
                document,
                user_id=str(user_id),
            )
        else:
            governance = await self._candidate_governance(str(user_id))
            self._apply_candidate_governance(document, governance)
            document["execution"] = {
                "actionable": False,
                "status": "research_only",
                "requires_daily_decision": True,
            }
            favorite_codes = await self._favorites.get_favorite_codes(user_id)
            for candidate in document.get("candidates", []):
                if isinstance(candidate, dict):
                    candidate["favorite_status"] = (
                        "in_favorites"
                        if candidate.get("code") in favorite_codes
                        else "not_added"
                    )
            await self._apply_objective_profiles(document)
            await self._apply_macro_policy(document)
            await self._apply_account_policy(document, user_id=str(user_id))
            self._update_run_counts(document)
        return self._serialize_run(document)

    async def add_to_favorites(
        self,
        user_id: str,
        run_id: str,
        codes: Iterable[str],
    ) -> Dict[str, Any]:
        if not ObjectId.is_valid(run_id):
            raise AICandidateRunNotFoundError(run_id)
        db = await self._get_db()
        document = await db["ai_candidate_runs"].find_one(
            {"_id": ObjectId(run_id), "user_id": str(user_id)}
        )
        if not document:
            raise AICandidateRunNotFoundError(run_id)

        governance = await self._candidate_governance(str(user_id))
        candidate_map = {
            candidate["code"]: _enrich_saved_candidate(candidate)
            for candidate in document.get("candidates", [])
            if (
                isinstance(candidate, Mapping)
                and candidate.get("code")
                and _candidate_governance_reason(
                    candidate.get("code"),
                    governance,
                )
                is None
            )
        }
        requested_codes = list(dict.fromkeys(str(code).strip() for code in codes))
        invalid_codes = [code for code in requested_codes if code not in candidate_map]
        if not requested_codes or invalid_codes:
            raise InvalidAICandidateSelectionError(
                ",".join(invalid_codes) if invalid_codes else "codes_required"
            )

        existing_codes = await self._favorites.get_favorite_codes(user_id)
        unavailable_codes = [
            code
            for code in requested_codes
            if code not in existing_codes
            and not bool(candidate_map[code].get("can_add_to_favorites"))
        ]
        if unavailable_codes:
            raise InvalidAICandidateSelectionError(
                "candidate_not_trackable:" + ",".join(unavailable_codes)
            )
        added: List[str] = []
        already_exists: List[str] = []
        failed: List[str] = []
        generated_at = document.get("generated_at")
        generated_at_text = (
            generated_at.isoformat()
            if isinstance(generated_at, datetime)
            else str(generated_at or "")
        )
        for code in requested_codes:
            candidate = candidate_map[code]
            if code in existing_codes:
                already_exists.append(code)
                continue
            lifecycle_state = (
                str(candidate.get("actionability"))
                if candidate.get("actionability")
                in {"expired", "invalidated", "target_reached"}
                else "current"
            )
            ai_metadata = {
                "run_id": run_id,
                "generated_at": generated_at_text,
                "reason_summary": candidate.get("reason_summary"),
                "reference_price": candidate.get("reference_price"),
                "price_plan": deepcopy(candidate.get("price_plan")),
                "objective_id": candidate.get("objective_id"),
                "objective_label": candidate.get("objective_label"),
                "objective_tier": candidate.get("objective_tier"),
                "objective_tier_label": candidate.get("objective_tier_label"),
                "objective_segment": candidate.get("objective_segment"),
                "horizon": (document.get("context") or {}).get("horizon"),
                "source": document.get("source"),
                "is_reference_only": True,
                "tracking_enabled": True,
                "actionability": candidate.get("actionability"),
                "actionability_label": candidate.get("actionability_label"),
                "rank_score": candidate.get("rank_score"),
                "performance": deepcopy(candidate.get("performance")),
                "last_checked_at": candidate.get("quote_checked_at"),
                "lifecycle_state": lifecycle_state,
                "is_current": lifecycle_state == "current",
                "superseded_at": None,
                "superseded_by_run_id": None,
            }
            candidate_plan = (
                candidate.get("price_plan")
                if isinstance(candidate.get("price_plan"), Mapping)
                else {}
            )
            entry_price = _finite_number(candidate_plan.get("entry_price"))
            strategy = str(candidate_plan.get("entry_strategy") or "")
            success = await self._favorites.add_favorite(
                user_id=str(user_id),
                stock_code=code,
                stock_name=str(candidate.get("name") or code),
                market="A股",
                tags=[AI_CANDIDATE_TAG],
                alert_price_low=entry_price if strategy == "pullback" else None,
                alert_price_high=entry_price if strategy == "breakout" else None,
                source=AI_CANDIDATE_SOURCE,
                ai_metadata=ai_metadata,
            )
            if success:
                added.append(code)
                existing_codes.add(code)
            else:
                failed.append(code)

        updated_candidates = deepcopy(document.get("candidates", []))
        for candidate in updated_candidates:
            if isinstance(candidate, dict) and candidate.get("code") in existing_codes:
                candidate["favorite_status"] = "in_favorites"
        await db["ai_candidate_runs"].update_one(
            {"_id": document["_id"], "user_id": str(user_id)},
            {
                "$set": {
                    "candidates": updated_candidates,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return {
            "run_id": run_id,
            "requested_count": len(requested_codes),
            "added_count": len(added),
            "added_codes": added,
            "already_exists_codes": already_exists,
            "failed_codes": failed,
        }


ai_candidate_service = AICandidateService()
