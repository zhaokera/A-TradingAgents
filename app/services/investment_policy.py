"""Shared investment objective and risk limits for research workflows."""

from __future__ import annotations

from typing import Any, Dict, Optional


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
