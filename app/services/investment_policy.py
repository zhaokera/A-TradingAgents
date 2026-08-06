"""Shared investment objective and risk limits for research workflows."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Mapping, Optional


INVESTMENT_OBJECTIVE: Dict[str, Any] = {
    "id": "technology_new_quality_productive_forces",
    "label": "科技 + 新质生产力",
    "description": "优先研究数字科技、高端装备、新型电力系统、新能源和先进材料。",
    "portfolio": {
        "green_new_exposure_cap_pct": 60.0,
        "yellow_new_exposure_cap_pct": 30.0,
        "reserve_cash_pct": 40.0,
        "preferred_single_symbol_pct": 35.0,
        "hard_single_symbol_cap_pct": 40.0,
        "per_position_loss_budget_pct": 1.0,
        "total_new_position_loss_budget_pct": 2.0,
    },
}

OBJECTIVE_TIER_ORDER = {
    "core": 0,
    "related": 1,
    "non_core": 2,
}

_TIER_LABELS = {
    "core": "核心方向",
    "related": "产业支撑",
    "non_core": "非核心方向",
}

_ANCHORS = {
    "000066": {"code": "000066", "tier": "core", "segment": "数字科技", "reason": "公司属于信创与国产计算基础设施方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "000938": {"code": "000938", "tier": "core", "segment": "数字科技", "reason": "公司属于网络与算力基础设施方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "000977": {"code": "000977", "tier": "core", "segment": "数字科技", "reason": "公司属于算力与服务器基础设施方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "300750": {"code": "300750", "tier": "core", "segment": "新能源", "reason": "公司属于动力电池与新能源产业方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "600406": {"code": "600406", "tier": "core", "segment": "新型电力系统", "reason": "公司属于电网数字化与新型电力系统方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "601138": {"code": "601138", "tier": "core", "segment": "数字科技", "reason": "公司属于算力基础设施与先进制造方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "601899": {"code": "601899", "tier": "related", "segment": "战略资源支撑", "reason": "铜金等战略资源为新产业提供上游支撑。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "688169": {"code": "688169", "tier": "core", "segment": "高端装备", "reason": "公司属于服务机器人与智能制造方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "688208": {"code": "688208", "tier": "core", "segment": "高端装备", "reason": "公司属于智能汽车诊断与充电基础设施方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "300725": {"code": "300725", "tier": "core", "segment": "生命科技", "reason": "公司属于创新药研发与生命科技产业链方向。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
    "601021": {"code": "601021", "tier": "non_core", "segment": "交通运输", "reason": "公司主营航空运输，不属于航空航天装备制造。", "reviewer": "product_policy", "evidence_source": "curated_anchor_v1", "reviewed_at": "2026-07-01T00:00:00+00:00"},
}

_REVIEWED_ANCHOR_FIELDS = {
    "code",
    "tier",
    "segment",
    "reason",
    "reviewer",
    "evidence_source",
    "reviewed_at",
}

_CORE_INDUSTRY_GROUPS = (
    (
        "数字科技",
        (
            "半导体",
            "计算机",
            "软件",
            "通信",
            "电子",
            "互联网",
            "数据",
            "云计算",
            "人工智能",
        ),
    ),
    (
        "高端装备",
        (
            "自动化",
            "机器人",
            "工业母机",
            "机床",
            "仪器仪表",
            "专用设备",
            "通用设备",
            "航空航天",
            "军工",
        ),
    ),
    (
        "新型电力系统",
        ("电网设备", "电力设备", "储能", "智能电网"),
    ),
    (
        "新能源",
        ("电池", "光伏", "风电", "新能源", "氢能"),
    ),
    (
        "先进材料",
        ("新材料", "先进材料", "碳纤维", "稀土永磁"),
    ),
    (
        "生命科技",
        ("生物科技", "生物制品", "医疗器械", "创新药"),
    ),
)

_CORE_NAME_GROUPS = (
    (
        "数字科技",
        (
            "软件",
            "数据",
            "网络",
            "通信",
            "光电",
            "微电子",
            "半导体",
            "芯片",
            "算力",
            "信创",
            "数码",
            "云计算",
            "智算",
        ),
    ),
    ("高端装备", ("机器人", "自动化", "机床", "装备", "航天", "卫星", "激光")),
    ("新型电力系统", ("电网", "电气", "储能", "电力电子")),
    ("新能源", ("新能源", "电池", "光伏", "风电", "氢能")),
    ("先进材料", ("新材", "稀土", "磁材", "碳纤维")),
)

_AMBIGUOUS_INNOVATION_NAME_TERMS = ("科技", "智能", "电子", "信息", "航空")

_RELATED_TERMS = (
    "矿业",
    "有色",
    "铜业",
    "黄金",
    "铝业",
    "锂业",
    "钴业",
    "资源",
)


def _profile(
    tier: str,
    segment: str,
    reason: str,
    review_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    scores = {"core": 1.0, "related": 0.55, "non_core": 0.0}
    result = {
        "objective_id": INVESTMENT_OBJECTIVE["id"],
        "objective_label": INVESTMENT_OBJECTIVE["label"],
        "objective_tier": tier,
        "objective_tier_label": _TIER_LABELS[tier],
        "objective_segment": segment,
        "objective_match_score": scores[tier],
        "objective_reason": reason,
    }
    if review_metadata:
        review = deepcopy(dict(review_metadata))
        result.update(
            {
                "reviewer": review["reviewer"],
                "evidence_source": review["evidence_source"],
                "reviewed_at": review["reviewed_at"],
                "anchor_review": review,
            }
        )
    return result


def _normalize_objective_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(SH|SZ)\.(\d{1,6})", text)
    if match:
        return match.group(2).zfill(6)
    match = re.fullmatch(r"(\d{1,6})\.(SH|SZ)", text)
    if match:
        return match.group(1).zfill(6)
    match = re.fullmatch(r"(SH|SZ)(\d{1,6})", text)
    if match:
        return match.group(2).zfill(6)
    if re.fullmatch(r"\d{1,6}", text):
        return text.zfill(6)
    return text


def _validated_reviewed_anchor(
    anchor: Any, requested_code: str
) -> Dict[str, Any] | None:
    if not isinstance(anchor, Mapping):
        return None
    if not _REVIEWED_ANCHOR_FIELDS.issubset(anchor):
        return None
    if _normalize_objective_code(anchor.get("code")) != requested_code:
        return None
    if str(anchor.get("tier")) not in OBJECTIVE_TIER_ORDER:
        return None
    if str(anchor.get("reviewer")) != "product_policy":
        return None
    if str(anchor.get("evidence_source")) != "curated_anchor_v1":
        return None
    if not str(anchor.get("segment") or "").strip() or not str(anchor.get("reason") or "").strip():
        return None
    reviewed_at = anchor.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        return None
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return dict(anchor)


def classify_investment_objective(
    code: Any,
    name: Any,
    *,
    industry: Optional[Any] = None,
) -> Dict[str, Any]:
    """Classify a candidate against the product's stated investment objective."""

    normalized_code = _normalize_objective_code(code)
    normalized_name = str(name or "").strip()
    normalized_industry = str(industry or "").strip()

    anchor = _ANCHORS.get(normalized_code)
    if anchor is not None:
        if isinstance(anchor, Mapping):
            reviewed_anchor = _validated_reviewed_anchor(anchor, normalized_code)
            if reviewed_anchor is not None:
                return _profile(
                    str(reviewed_anchor["tier"]),
                    str(reviewed_anchor["segment"]),
                    str(reviewed_anchor["reason"]),
                    review_metadata=reviewed_anchor,
                )
        # Older integrations may still inject tuple anchors. They are not
        # reviewed evidence, so a tuple requesting core cannot grant core.
        if isinstance(anchor, (tuple, list)) and len(anchor) >= 3:
            legacy_tier = str(anchor[0])
            if legacy_tier == "core":
                legacy_tier = "related"
            if legacy_tier in OBJECTIVE_TIER_ORDER:
                return _profile(legacy_tier, str(anchor[1]), str(anchor[2]))

    if normalized_industry:
        for segment, terms in _CORE_INDUSTRY_GROUPS:
            if any(term in normalized_industry for term in terms):
                return _profile(
                    "core",
                    segment,
                    "行业属于科技与数字基础设施方向。"
                    if segment == "数字科技"
                    else f"行业属于{segment}方向。",
                )

        combined_industry = normalized_industry
        if any(term in combined_industry for term in _RELATED_TERMS):
            return _profile(
                "related",
                "战略资源支撑",
                "行业属于科技与新质生产力上游支撑方向。",
            )
        return _profile(
            "non_core",
            "其他行业",
            "已取得行业信息，但未匹配科技与新质生产力方向。",
        )

    for segment, terms in _CORE_NAME_GROUPS:
        if any(term in normalized_name for term in terms):
            return _profile(
                "related",
                segment,
                f"公司名称包含{segment}线索；取得权威行业或主营证据前不判定为核心方向。",
            )

    if any(term in normalized_name for term in _AMBIGUOUS_INNOVATION_NAME_TERMS):
        return _profile(
            "related",
            "业务方向待核验",
            "公司名称仅提供弱线索，需取得行业或主营业务证据后再判定核心方向。",
        )

    if normalized_code.startswith("688"):
        return _profile(
            "related",
            "科技创新待核验",
            "科创板属性只作为待核验线索，不能单独判定为核心方向。",
        )

    combined = f"{normalized_name} {normalized_industry}"
    if any(term in combined for term in _RELATED_TERMS):
        return _profile(
            "related",
            "战略资源支撑",
            "上游资源与材料对新产业具有支撑作用，但不是科技主线。",
        )

    return _profile(
        "non_core",
        "其他行业",
        "暂未匹配科技与新质生产力核心或产业支撑方向。",
    )


def objective_tier_rank(value: Any) -> int:
    return OBJECTIVE_TIER_ORDER.get(str(value or "non_core"), 2)


def build_dynamic_portfolio_policy(
    *,
    total_assets: Any,
    current_exposure_pct: Any = 0,
    market_regime: str = "green",
) -> Dict[str, Any]:
    """Build account-aware concentration limits for candidate sizing."""

    try:
        assets = max(0.0, float(total_assets or 0))
    except (TypeError, ValueError):
        assets = 0.0
    try:
        exposure = min(100.0, max(0.0, float(current_exposure_pct or 0)))
    except (TypeError, ValueError):
        exposure = 0.0

    if assets <= 20_000:
        preferred_single, hard_single = 35.0, 45.0
    elif assets <= 100_000:
        preferred_single, hard_single = 25.0, 35.0
    elif assets <= 500_000:
        preferred_single, hard_single = 20.0, 30.0
    else:
        preferred_single, hard_single = 15.0, 25.0

    normalized_regime = str(market_regime or "green").strip().lower()
    exposure_caps = {
        "green": 60.0,
        "yellow": 30.0,
        "red": 0.0,
    }
    new_exposure_cap = exposure_caps.get(normalized_regime, 30.0)
    available_new_exposure = max(0.0, new_exposure_cap - exposure)

    return {
        **INVESTMENT_OBJECTIVE["portfolio"],
        "policy_source": "dynamic_account_risk",
        "market_regime": (
            normalized_regime if normalized_regime in exposure_caps else "unknown"
        ),
        "total_assets": round(assets, 2),
        "current_exposure_pct": round(exposure, 2),
        "new_exposure_cap_pct": new_exposure_cap,
        "available_new_exposure_pct": round(available_new_exposure, 2),
        "reserve_cash_pct": round(100.0 - new_exposure_cap, 2),
        "preferred_single_symbol_pct": preferred_single,
        "hard_single_symbol_cap_pct": hard_single,
    }


def calculate_candidate_position_sizing(
    *,
    entry_price: Any,
    stop_price: Any,
    total_assets: Any,
    available_cash: Any,
    current_symbol_value: Any = 0,
    policy: Optional[Mapping[str, Any]] = None,
    lot_size: int = 100,
) -> Dict[str, Any]:
    """Size an A-share candidate from account, concentration and loss limits."""

    try:
        entry = float(entry_price)
        stop = float(stop_price)
        assets = float(total_assets)
        cash = max(0.0, float(available_cash or 0))
        existing_value = max(0.0, float(current_symbol_value or 0))
    except (TypeError, ValueError):
        return {"status": "unavailable", "reason": "invalid_account_or_price_data"}
    values = (entry, stop, assets, cash, existing_value)
    if (
        not all(math.isfinite(value) for value in values)
        or entry <= 0
        or stop <= 0
        or stop >= entry
        or assets <= 0
        or lot_size <= 0
    ):
        return {"status": "unavailable", "reason": "invalid_account_or_price_data"}

    effective_policy = dict(
        policy or build_dynamic_portfolio_policy(total_assets=assets)
    )
    stop_distance_pct = (entry - stop) / entry * 100
    loss_budget_pct = float(
        effective_policy.get("per_position_loss_budget_pct", 1.0)
    )
    hard_cap_pct = float(
        effective_policy.get("hard_single_symbol_cap_pct", 30.0)
    )
    preferred_pct = float(
        effective_policy.get("preferred_single_symbol_pct", hard_cap_pct)
    )
    available_new_exposure_pct = float(
        effective_policy.get("available_new_exposure_pct", 0.0)
    )
    if available_new_exposure_pct <= 0:
        return {
            "status": "market_blocked",
            "reason": "market_regime_allows_no_new_exposure",
            "lot_size": lot_size,
            "suggested_quantity": 0,
            "suggested_amount": 0.0,
            "suggested_position_pct": 0.0,
            "stop_distance_pct": round(stop_distance_pct, 2),
        }
    existing_symbol_pct = existing_value / assets * 100
    symbol_room_pct = max(0.0, hard_cap_pct - existing_symbol_pct)
    raw_loss_budget = Decimal(str(assets)) * Decimal(str(loss_budget_pct)) / Decimal("100")
    effective_loss_budget = raw_loss_budget.quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    risk_budget_precision = {
        "basis": "stop_price_loss_excluding_fees",
        "raw_loss_budget_amount": float(raw_loss_budget),
        "effective_loss_budget_amount": float(effective_loss_budget),
        "rounding_delta": float(effective_loss_budget - raw_loss_budget),
        "rounding_unit": "CNY_1",
        "rounding_mode": "ROUND_HALF_UP",
    }
    risk_cap_pct = float(effective_loss_budget) / assets / stop_distance_pct * 10000
    suggested_pct = max(
        0.0,
        min(
            preferred_pct,
            symbol_room_pct,
            risk_cap_pct,
            available_new_exposure_pct,
            cash / assets * 100,
        ),
    )
    amount_cap = round(min(cash, assets * suggested_pct / 100), 2)
    quantity = math.floor(amount_cap / entry / lot_size) * lot_size
    if quantity <= 0:
        one_lot_planned_loss = round((entry - stop) * lot_size, 2)
        one_lot_buy_fee = max(5.0, entry * lot_size * 0.0003)
        one_lot_stop_proceeds = stop * lot_size
        one_lot_sell_fee = max(5.0, one_lot_stop_proceeds * 0.0003)
        one_lot_stamp_duty = one_lot_stop_proceeds * 0.0005
        one_lot_estimated_fees = round(
            one_lot_buy_fee + one_lot_sell_fee + one_lot_stamp_duty,
            2,
        )
        return {
            "status": "one_lot_unaffordable",
            "reason": "account_or_risk_budget_below_one_lot",
            "lot_size": lot_size,
            "one_lot_amount": round(entry * lot_size, 2),
            "one_lot_planned_loss": one_lot_planned_loss,
            "one_lot_estimated_fees": one_lot_estimated_fees,
            "one_lot_estimated_net_drawdown": round(
                one_lot_planned_loss + one_lot_estimated_fees, 2
            ),
            "suggested_position_pct": round(suggested_pct, 2),
            "stop_distance_pct": round(stop_distance_pct, 2),
            "risk_budget_precision": risk_budget_precision,
        }

    amount = round(quantity * entry, 2)
    planned_loss = round(quantity * (entry - stop), 2)
    buy_fee = max(5.0, entry * quantity * 0.0003)
    stop_proceeds = stop * quantity
    sell_fee = max(5.0, stop_proceeds * 0.0003)
    stamp_duty = stop_proceeds * 0.0005
    estimated_fees = round(buy_fee + sell_fee + stamp_duty, 2)
    return {
        "status": "sized",
        "lot_size": lot_size,
        "suggested_quantity": quantity,
        "suggested_amount": amount,
        "suggested_position_pct": round(amount / assets * 100, 2),
        "planned_loss_amount": planned_loss,
        "estimated_round_trip_fees_at_stop": estimated_fees,
        "estimated_net_drawdown_at_stop": round(
            planned_loss + estimated_fees, 2
        ),
        "planned_loss_pct_of_assets": round(planned_loss / assets * 100, 2),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "risk_cap_pct": round(risk_cap_pct, 2),
        "symbol_room_pct": round(symbol_room_pct, 2),
        "risk_budget_precision": risk_budget_precision,
    }


def allocate_candidate_portfolio(
    candidates: Iterable[Mapping[str, Any]],
    *,
    total_assets: Any,
    available_cash: Any,
    policy: Mapping[str, Any],
    lot_size: int = 100,
) -> Dict[str, Any]:
    """Allocate candidates against one shared capital and loss budget.

    Input order is the research priority order. The allocator never rounds a
    position up: every allocation is an A-share board lot and all aggregate
    limits are checked again after rounding.
    """

    def finite_non_negative(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return parsed

    assets = finite_non_negative(total_assets)
    cash = finite_non_negative(available_cash)
    exposure_pct = finite_non_negative(policy.get("available_new_exposure_pct"))
    loss_budget_pct = finite_non_negative(
        policy.get("total_new_position_loss_budget_pct")
    )
    hard_cap_pct = finite_non_negative(policy.get("hard_single_symbol_cap_pct"))
    policy_valid = bool(
        assets is not None
        and assets > 0
        and cash is not None
        and exposure_pct is not None
        and loss_budget_pct is not None
        and hard_cap_pct is not None
        and isinstance(lot_size, int)
        and not isinstance(lot_size, bool)
        and lot_size > 0
    )
    if not policy_valid:
        assets = cash = exposure_pct = loss_budget_pct = hard_cap_pct = 0.0

    capital_budget = min(cash, assets * exposure_pct / 100) if assets > 0 else 0.0
    loss_budget = assets * loss_budget_pct / 100 if assets > 0 else 0.0
    remaining_capital = capital_budget
    remaining_loss = loss_budget
    allocations: List[Dict[str, Any]] = []

    for rank, raw in enumerate(candidates, 1):
        candidate = dict(raw)
        code = str(candidate.get("code") or "")
        plan = candidate.get("price_plan")
        sizing = candidate.get("position_sizing")
        plan = plan if isinstance(plan, Mapping) else {}
        sizing = sizing if isinstance(sizing, Mapping) else {}
        entry = finite_non_negative(plan.get("entry_price"))
        stop = finite_non_negative(plan.get("stop_price"))

        base: Dict[str, Any] = {
            "rank": rank,
            "code": code,
            "status": "watch_only",
            "reason": "price_plan_or_account_unavailable",
            "quantity": 0,
            "amount": 0.0,
            "position_pct": 0.0,
            "planned_loss_amount": 0.0,
            "planned_loss_pct_of_assets": 0.0,
        }
        if not policy_valid:
            base.update(
                status="watch_only",
                reason="invalid_portfolio_policy",
            )
            allocations.append(base)
            continue
        if exposure_pct <= 0 or capital_budget <= 0 or loss_budget <= 0:
            base.update(
                status="market_blocked",
                reason="portfolio_new_exposure_blocked",
            )
            allocations.append(base)
            continue
        if entry is None or stop is None or entry <= 0 or stop <= 0 or stop >= entry:
            allocations.append(base)
            continue

        one_lot_amount = entry * lot_size
        one_lot_loss = (entry - stop) * lot_size
        suggested_value = finite_non_negative(sizing.get("suggested_quantity"))
        suggested_quantity = (
            math.floor(suggested_value / lot_size) * lot_size
            if suggested_value is not None
            else 0
        )
        if suggested_quantity <= 0:
            base.update(
                reason=str(sizing.get("reason") or "one_lot_not_executable"),
                one_lot_amount=round(one_lot_amount, 2),
                one_lot_planned_loss=round(one_lot_loss, 2),
            )
            allocations.append(base)
            continue

        max_by_capital = math.floor(remaining_capital / entry / lot_size) * lot_size
        max_by_loss = math.floor(remaining_loss / (entry - stop) / lot_size) * lot_size
        existing_symbol_value = finite_non_negative(
            candidate.get(
                "current_symbol_value",
                sizing.get("current_symbol_value", 0),
            )
        )
        if existing_symbol_value is None:
            base.update(reason="invalid_portfolio_policy")
            allocations.append(base)
            continue
        symbol_room = max(0.0, assets * hard_cap_pct / 100 - existing_symbol_value)
        max_by_symbol = math.floor(symbol_room / entry / lot_size) * lot_size
        quantity = min(
            suggested_quantity,
            max_by_capital,
            max_by_loss,
            max_by_symbol,
        )
        if quantity <= 0:
            if max_by_symbol <= 0:
                reason = "hard_single_symbol_cap"
            elif remaining_capital < one_lot_amount:
                reason = "shared_capital_budget_exhausted"
            else:
                reason = "shared_loss_budget_exhausted"
            base.update(
                status="budget_exhausted",
                reason=reason,
                one_lot_amount=round(one_lot_amount, 2),
                one_lot_planned_loss=round(one_lot_loss, 2),
            )
            allocations.append(base)
            continue

        amount = round(quantity * entry, 2)
        planned_loss = round(quantity * (entry - stop), 2)
        remaining_capital = max(0.0, remaining_capital - amount)
        remaining_loss = max(0.0, remaining_loss - planned_loss)
        base.update(
            status="allocated",
            reason="shared_portfolio_budget_allocated",
            quantity=quantity,
            amount=amount,
            position_pct=round(amount / assets * 100, 2),
            planned_loss_amount=planned_loss,
            planned_loss_pct_of_assets=round(planned_loss / assets * 100, 2),
        )
        allocations.append(base)

    allocated = [item for item in allocations if item["status"] == "allocated"]
    allocated_amount = round(sum(float(item["amount"]) for item in allocated), 2)
    total_planned_loss = round(
        sum(float(item["planned_loss_amount"]) for item in allocated), 2
    )
    return {
        "status": "allocated" if allocated else "no_executable_position",
        "policy": deepcopy(dict(policy)),
        "capital_budget": round(capital_budget, 2),
        "allocated_amount": allocated_amount,
        "remaining_capital": round(max(0.0, capital_budget - allocated_amount), 2),
        "allocated_exposure_pct": round(allocated_amount / assets * 100, 2)
        if assets > 0
        else 0.0,
        "loss_budget": round(loss_budget, 2),
        "total_planned_loss": total_planned_loss,
        "remaining_loss_budget": round(max(0.0, loss_budget - total_planned_loss), 2),
        "total_planned_loss_pct": round(total_planned_loss / assets * 100, 2)
        if assets > 0
        else 0.0,
        "allocated_position_count": len(allocated),
        "watch_only_count": len(allocations) - len(allocated),
        "allocations": allocations,
    }
