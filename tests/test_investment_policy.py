import pytest

from app.services.investment_policy import (
    INVESTMENT_OBJECTIVE,
    build_dynamic_portfolio_policy,
    calculate_candidate_position_sizing,
    classify_investment_objective,
)
import app.services.investment_policy as investment_policy


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


def test_objective_classifier_does_not_treat_every_star_market_stock_as_core():
    result = classify_investment_objective("688999", "测试股份")

    assert result["objective_tier"] == "related"
    assert result["objective_segment"] == "科技创新待核验"


def test_known_non_core_industry_overrides_generic_company_name_keyword():
    result = classify_investment_objective(
        "600999",
        "智能商业",
        industry="食品饮料",
    )

    assert result["objective_tier"] == "non_core"


def test_generic_company_name_terms_do_not_create_false_core_matches():
    generic_technology = classify_investment_objective("300999", "测试科技")
    airline = classify_investment_objective("600999", "测试航空")

    assert generic_technology["objective_tier"] == "related"
    assert airline["objective_tier"] == "related"
    assert generic_technology["objective_segment"] == "业务方向待核验"


def test_known_airline_is_not_classified_as_aerospace_equipment():
    result = classify_investment_objective("601021", "春秋航空")

    assert result["objective_tier"] == "non_core"
    assert result["objective_segment"] == "交通运输"


def test_verified_life_science_anchor_is_core_even_with_generic_name():
    result = classify_investment_objective("300725", "药石科技")

    assert result["objective_tier"] == "core"
    assert result["objective_segment"] == "生命科技"


def test_reviewed_anchor_returns_fixed_review_metadata():
    result = classify_investment_objective("600406", "国电南瑞")

    assert result["objective_tier"] == "core"
    assert result["reviewer"] == "product_policy"
    assert result["evidence_source"] == "curated_anchor_v1"
    assert result["reviewed_at"].endswith("+00:00")
    assert result["anchor_review"]["code"] == "600406"


@pytest.mark.parametrize("code", ["600406.SH", "SH.600406", "sh600406"])
def test_reviewed_anchor_normalizes_exchange_code_forms(code):
    result = classify_investment_objective(code, "国电南瑞")

    assert result["objective_tier"] == "core"
    assert result["reviewer"] == "product_policy"


def test_malformed_reviewed_anchor_degrades_without_core_grant(monkeypatch):
    monkeypatch.setitem(
        investment_policy._ANCHORS,
        "600406",
        {
            "code": "600406",
            "tier": "core",
            "segment": "新型电力系统",
            "reason": "缺少审查字段",
        },
    )

    result = classify_investment_objective("600406.SH", "国电南瑞")

    assert result["objective_tier"] != "core"
    assert "reviewer" not in result


def test_legacy_tuple_anchor_is_unreviewed_and_core_is_capped_at_related(monkeypatch):
    monkeypatch.setitem(
        investment_policy._ANCHORS,
        "999999",
        ("core", "数字科技", "兼容性测试锚点"),
    )

    result = classify_investment_objective("999999", "兼容公司")

    assert result["objective_tier"] == "related"
    assert "reviewer" not in result


def test_legacy_tuple_anchor_with_invalid_tier_falls_through(monkeypatch):
    monkeypatch.setitem(
        investment_policy._ANCHORS,
        "999998",
        ("unexpected", "错误分类", "不得进入未定义等级"),
    )

    result = classify_investment_objective(
        "999998",
        "测试公司",
        industry="食品饮料",
    )

    assert result["objective_tier"] == "non_core"
    assert result["objective_segment"] == "其他行业"


def test_dynamic_policy_adapts_concentration_to_account_size():
    small = build_dynamic_portfolio_policy(
        total_assets=10_000,
        current_exposure_pct=0,
        market_regime="green",
    )
    larger = build_dynamic_portfolio_policy(
        total_assets=300_000,
        current_exposure_pct=0,
        market_regime="green",
    )

    assert small["policy_source"] == "dynamic_account_risk"
    assert small["hard_single_symbol_cap_pct"] > larger["hard_single_symbol_cap_pct"]
    assert small["available_new_exposure_pct"] == 60.0
    assert larger["available_new_exposure_pct"] == 60.0


def test_candidate_sizing_uses_stop_distance_and_board_lot():
    policy = build_dynamic_portfolio_policy(
        total_assets=10_000,
        current_exposure_pct=0,
        market_regime="green",
    )

    sizing = calculate_candidate_position_sizing(
        entry_price=20.0,
        stop_price=19.0,
        total_assets=10_000,
        available_cash=6_000,
        current_symbol_value=0,
        policy=policy,
    )

    assert sizing["status"] == "sized"
    assert sizing["suggested_quantity"] % 100 == 0
    assert sizing["suggested_amount"] <= 6_000
    assert sizing["planned_loss_amount"] <= 100
def test_candidate_sizing_is_blocked_when_market_has_no_new_risk_budget():
    policy = build_dynamic_portfolio_policy(
        total_assets=10_000,
        current_exposure_pct=0,
        market_regime="red",
    )

    sizing = calculate_candidate_position_sizing(
        entry_price=20.0,
        stop_price=19.0,
        total_assets=10_000,
        available_cash=10_000,
        policy=policy,
    )

    assert sizing["status"] == "market_blocked"
    assert sizing["suggested_quantity"] == 0


def test_one_lot_risk_uses_audited_whole_yuan_budget_precision():
    policy = build_dynamic_portfolio_policy(
        total_assets=10_685.41,
        current_exposure_pct=0,
        market_regime="green",
    )

    boundary = calculate_candidate_position_sizing(
        entry_price=30.39,
        stop_price=29.32,
        total_assets=10_685.41,
        available_cash=10_685.41,
        policy=policy,
    )
    clearly_over = calculate_candidate_position_sizing(
        entry_price=100.35,
        stop_price=95.41,
        total_assets=10_685.41,
        available_cash=10_685.41,
        policy=policy,
    )

    assert boundary["status"] == "sized"
    assert boundary["suggested_quantity"] == 100
    assert boundary["planned_loss_amount"] == 107.0
    assert boundary["risk_budget_precision"]["raw_loss_budget_amount"] == 106.8541
    assert boundary["risk_budget_precision"]["effective_loss_budget_amount"] == 107.0
    assert boundary["risk_budget_precision"]["rounding_unit"] == "CNY_1"
    assert clearly_over["status"] == "one_lot_unaffordable"
    assert clearly_over["one_lot_planned_loss"] == 494.0
