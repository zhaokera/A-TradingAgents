"""Shared investment objective and risk limits for research workflows."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


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
    "000066": ("core", "数字科技", "公司属于信创与国产计算基础设施方向。"),
    "000938": ("core", "数字科技", "公司属于网络与算力基础设施方向。"),
    "000977": ("core", "数字科技", "公司属于算力与服务器基础设施方向。"),
    "300750": ("core", "新能源", "公司属于动力电池与新能源产业方向。"),
    "600406": ("core", "新型电力系统", "公司属于电网数字化与新型电力系统方向。"),
    "601138": ("core", "数字科技", "公司属于算力基础设施与先进制造方向。"),
    "601899": ("related", "战略资源支撑", "铜金等战略资源为新产业提供上游支撑。"),
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
    ("数字科技", ("科技", "信息", "软件", "数据", "智能", "网络", "通信", "光电", "电子", "芯", "算力")),
    ("高端装备", ("机器人", "自动化", "机床", "装备", "航天", "航空", "卫星")),
    ("新型电力系统", ("电网", "电气", "储能")),
    ("新能源", ("新能源", "电池", "光伏", "风电", "氢能")),
    ("先进材料", ("新材", "稀土", "磁材", "碳纤维")),
)

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


def _profile(tier: str, segment: str, reason: str) -> Dict[str, Any]:
    scores = {"core": 1.0, "related": 0.55, "non_core": 0.0}
    return {
        "objective_id": INVESTMENT_OBJECTIVE["id"],
        "objective_label": INVESTMENT_OBJECTIVE["label"],
        "objective_tier": tier,
        "objective_tier_label": _TIER_LABELS[tier],
        "objective_segment": segment,
        "objective_match_score": scores[tier],
        "objective_reason": reason,
    }


def classify_investment_objective(
    code: Any,
    name: Any,
    *,
    industry: Optional[Any] = None,
) -> Dict[str, Any]:
    """Classify a candidate against the product's stated investment objective."""

    normalized_code = str(code or "").strip()
    normalized_name = str(name or "").strip()
    normalized_industry = str(industry or "").strip()

    anchor = _ANCHORS.get(normalized_code)
    if anchor is not None:
        return _profile(*anchor)

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

    for segment, terms in _CORE_NAME_GROUPS:
        if any(term in normalized_name for term in terms):
            return _profile("core", segment, f"公司名称特征匹配{segment}方向。")

    if normalized_code.startswith("688"):
        return _profile("core", "科技创新", "科创板公司纳入科技创新研究池。")

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
    risk_cap_pct = loss_budget_pct / stop_distance_pct * 100
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
    amount_cap = min(cash, assets * suggested_pct / 100)
    quantity = math.floor(amount_cap / entry / lot_size) * lot_size
    if quantity <= 0:
        return {
            "status": "one_lot_unaffordable",
            "reason": "account_or_risk_budget_below_one_lot",
            "lot_size": lot_size,
            "one_lot_amount": round(entry * lot_size, 2),
            "suggested_position_pct": round(suggested_pct, 2),
            "stop_distance_pct": round(stop_distance_pct, 2),
        }

    amount = round(quantity * entry, 2)
    planned_loss = round(quantity * (entry - stop), 2)
    return {
        "status": "sized",
        "lot_size": lot_size,
        "suggested_quantity": quantity,
        "suggested_amount": amount,
        "suggested_position_pct": round(amount / assets * 100, 2),
        "planned_loss_amount": planned_loss,
        "planned_loss_pct_of_assets": round(planned_loss / assets * 100, 2),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "risk_cap_pct": round(risk_cap_pct, 2),
        "symbol_room_pct": round(symbol_room_pct, 2),
    }
