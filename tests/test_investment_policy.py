from app.services.investment_policy import (
    INVESTMENT_OBJECTIVE,
    classify_investment_objective,
)


def test_investment_objective_exposes_small_account_risk_limits():
    portfolio = INVESTMENT_OBJECTIVE["portfolio"]

    assert INVESTMENT_OBJECTIVE["label"] == "科技 + 新质生产力"
    assert portfolio["green_new_exposure_cap_pct"] == 60.0
    assert portfolio["yellow_new_exposure_cap_pct"] == 30.0
    assert portfolio["reserve_cash_pct"] == 40.0
    assert portfolio["preferred_single_symbol_pct"] == 35.0
    assert portfolio["hard_single_symbol_cap_pct"] == 40.0
    assert portfolio["per_position_loss_budget_pct"] == 1.0
    assert portfolio["total_new_position_loss_budget_pct"] == 2.0


def test_objective_classifier_distinguishes_core_support_and_non_core():
    nari = classify_investment_objective("600406", "国电南瑞")
    zijin = classify_investment_objective("601899", "紫金矿业")
    haier = classify_investment_objective("600690", "海尔智家")

    assert nari["objective_tier"] == "core"
    assert nari["objective_segment"] == "新型电力系统"
    assert zijin["objective_tier"] == "related"
    assert zijin["objective_segment"] == "战略资源支撑"
    assert haier["objective_tier"] == "non_core"
    assert nari["objective_match_score"] > zijin["objective_match_score"] > haier[
        "objective_match_score"
    ]


def test_objective_classifier_uses_industry_when_name_is_ambiguous():
    result = classify_investment_objective(
        "600123",
        "测试股份",
        industry="半导体及元件",
    )

    assert result["objective_tier"] == "core"
    assert result["objective_segment"] == "数字科技"
    assert result["objective_reason"] == "行业属于科技与数字基础设施方向。"
