"""Persisted AI research candidates and controlled favorite promotion."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from bson import ObjectId
from starlette.concurrency import run_in_threadpool

from app.core.database import get_mongo_db
from app.services.favorites_service import FavoritesService, favorites_service
from app.services.holdings_cli import run_public_full_market_research
from app.services.investment_policy import (
    INVESTMENT_OBJECTIVE,
    classify_investment_objective,
    objective_tier_rank,
)


AI_CANDIDATE_SOURCE = "ai_screening"
AI_CANDIDATE_TAG = "AI候选"
AI_CANDIDATE_RUN_TTL_DAYS = 14
_A_SHARE_CODE = re.compile(r"^[0-9]{6}$")

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
        flags.append(
            {
                "code": code[:80],
                "severity": str(raw.get("severity") or "warning")[:20],
                "message": message[:240],
            }
        )
    return flags


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
    risk_blocked = bool(risk_flags)
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
        "entry_status": entry_status,
        "entry_status_label": _ENTRY_STATUS_LABELS[entry_status],
        "entry_guidance": guidance,
        "status": plan_status,
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
    candidate["price_plan"] = _build_candidate_price_plan(
        reference_price=_finite_number(candidate.get("reference_price")),
        plan=saved_plan,
        triggers={
            "breakout_price": saved_plan.get("breakout_price"),
            "invalidation_price": saved_plan.get("stop_price"),
        },
        observation_zone=observation_zone,
        risk_flags=risk_flags,
    )
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
    return {
        "code": code,
        "name": str(candidate.get("name") or code).strip()[:80],
        "market": "A股",
        "priority": int(candidate.get("priority") or 999),
        "research_status": "observe",
        "research_status_label": "研究候选",
        **objective,
        "reference_price": reference_price,
        "pct_change": _finite_number(quote.get("pct_change"), tencent.get("pct_change")),
        "trade_at": quote.get("trade_at"),
        "price_plan": price_plan,
        "reason_summary": _candidate_reason(candidate),
        "evidence": _candidate_evidence(candidate, context),
        "risk_flags": risk_flags,
        "favorite_status": "in_favorites" if code in favorite_codes else "not_added",
        "source": "public_full_market",
        "is_reference_only": True,
    }


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
            objective_tier_rank(item.get("objective_tier")),
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
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    objective_counts = {
        tier: sum(item.get("objective_tier") == tier for item in candidates)
        for tier in ("core", "related", "non_core")
    }
    return {
        "status": "completed",
        "source": "public_full_market",
        "source_detail": meta.get("source"),
        "candidate_count": len(candidates),
        "candidates": candidates,
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
        },
        "market": {
            "session": market_session.get("session"),
            "is_trading_hours": market_session.get("is_trading_hours"),
            "local_time": market_session.get("local_time"),
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
        research_runner: Callable[[], Dict[str, Any]] = run_public_full_market_research,
        favorites: FavoritesService = favorites_service,
    ) -> None:
        self._research_runner = research_runner
        self._favorites = favorites
        self.db = None

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
        for field in ("generated_at", "expires_at", "updated_at"):
            value = result.get(field)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                result[field] = value.isoformat()
        return result

    async def run(self, user_id: str, *, max_candidates: int = 5) -> Dict[str, Any]:
        favorite_codes = await self._favorites.get_favorite_codes(user_id)
        payload = await run_in_threadpool(self._research_runner)
        normalized = normalize_ai_candidate_run(
            payload,
            max_candidates=max_candidates,
            favorite_codes=favorite_codes,
        )
        now = datetime.now(timezone.utc)
        document = {
            "_id": ObjectId(),
            "user_id": str(user_id),
            "generated_at": now,
            "expires_at": now + timedelta(days=AI_CANDIDATE_RUN_TTL_DAYS),
            **normalized,
        }
        db = await self._get_db()
        await db["ai_candidate_runs"].insert_one(deepcopy(document))
        return self._serialize_run(document)

    async def latest(self, user_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        document = await db["ai_candidate_runs"].find_one(
            {"user_id": str(user_id)},
            sort=[("generated_at", -1)],
        )
        if not document:
            return None
        favorite_codes = await self._favorites.get_favorite_codes(user_id)
        for candidate in document.get("candidates", []):
            if isinstance(candidate, dict):
                candidate["favorite_status"] = (
                    "in_favorites"
                    if candidate.get("code") in favorite_codes
                    else "not_added"
                )
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

        candidate_map = {
            candidate["code"]: candidate
            for candidate in document.get("candidates", [])
            if isinstance(candidate, Mapping) and candidate.get("code")
        }
        requested_codes = list(dict.fromkeys(str(code).strip() for code in codes))
        invalid_codes = [code for code in requested_codes if code not in candidate_map]
        if not requested_codes or invalid_codes:
            raise InvalidAICandidateSelectionError(
                ",".join(invalid_codes) if invalid_codes else "codes_required"
            )

        existing_codes = await self._favorites.get_favorite_codes(user_id)
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
            }
            success = await self._favorites.add_favorite(
                user_id=str(user_id),
                stock_code=code,
                stock_name=str(candidate.get("name") or code),
                market="A股",
                tags=[AI_CANDIDATE_TAG],
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
