import pytest

from app.services.holding_risk_sizing import (
    apply_net_reward_risk_gate,
    build_external_risk_gate,
    evaluate_ashare_trade,
    size_ashare_candidate,
)


def test_evaluate_ashare_trade_includes_exact_fees_slippage_and_net_rr():
    result = evaluate_ashare_trade(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        quantity=100,
    )

    assert result["entry_order"] == {
        "side": "buy",
        "reference_price": 20.0,
        "execution_price": 20.01,
        "quantity": 100,
        "gross_amount": 2001.0,
        "commission": 5.0,
        "transfer_fee": 0.02,
        "stamp_duty": 0.0,
        "total_fees": 5.02,
        "total_cost": 2006.02,
        "net_proceeds": None,
    }
    assert result["stop_order"]["execution_price"] == 17.991
    assert result["stop_order"]["gross_amount"] == 1799.1
    assert result["stop_order"]["commission"] == 5.0
    assert result["stop_order"]["stamp_duty"] == 0.9
    assert result["stop_order"]["transfer_fee"] == 0.02
    assert result["stop_order"]["net_proceeds"] == 1793.18
    assert result["target_order"]["net_proceeds"] == 2392.58
    assert result["risk_amount"] == 212.84
    assert result["reward_amount"] == 386.56
    assert result["net_reward_risk"] == 1.8162


def test_apply_net_reward_risk_gate_marks_low_rr_plan_non_actionable():
    guarded = apply_net_reward_risk_gate(
        {
            "actionable": True,
            "status": "ok",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 20.8,
            "target_price": 21.5,
            "failed_gates": [],
        }
    )

    assert guarded["actionable"] is False
    assert guarded["status"] == "net_rr_below_1_5"
    assert guarded["fee_aware_trade"]["net_reward_risk"] < 1.5
    assert "net_rr_below_1_5" in guarded["failed_gates"]


@pytest.mark.parametrize(
    ("level", "expected_pct", "expected_amount"),
    [
        ("green", 20.0, 20000.0),
        ("yellow", 12.0, 12000.0),
        ("red", 0.0, 0.0),
        ("unknown", 0.0, 0.0),
        (None, 0.0, 0.0),
    ],
)
def test_external_risk_gate_has_stable_caps(level, expected_pct, expected_amount):
    gate = build_external_risk_gate(level, actionable_equity=100000.0)

    assert gate["level"] == (level or "unknown")
    assert gate["max_new_exposure_pct"] == expected_pct
    assert gate["max_new_exposure_amount"] == expected_amount
    assert gate["actionable"] is (expected_pct > 0)


def test_external_risk_gate_rejects_invalid_level():
    with pytest.raises(ValueError, match="external risk level"):
        build_external_risk_gate("orange", actionable_equity=100000.0)


def test_size_candidate_evaluates_whole_lots_against_shared_loss_budget():
    sizing = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        actionable_equity=100000.0,
        cash_available=50000.0,
        original_cash=50000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=25000.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=0.0,
    )

    assert sizing["suggested_lots"] == 3
    assert sizing["suggested_quantity"] == 300
    assert sizing["risk_budget_amount"] == 750.0
    assert sizing["trade"]["risk_amount"] <= 750.0
    assert sizing["failed_gates"] == []
    assert sizing["blocking_failed_gates"] == []


def test_size_candidate_accepts_explicit_deadline_mode_caps():
    sizing = size_ashare_candidate(
        entry_price=24.5,
        stop_price=23.1,
        target_price=27.2,
        actionable_equity=10685.41,
        cash_available=10685.41,
        original_cash=10685.41,
        remaining_new_exposure=6945.52,
        remaining_initial_deploy=6945.52,
        remaining_loss_budget=320.56,
        existing_symbol_market_value=0.0,
        candidate_cash_cap_amount=2671.35,
        post_trade_symbol_cap_pct=25.0,
    )

    assert sizing["suggested_lots"] == 1
    assert sizing["constraints"]["candidate_cash_cap"] == 2671.35
    assert sizing["constraints"]["post_trade_symbol_cap_pct"] == 25.0
    assert sizing["constraints"]["post_trade_symbol_cap"] == 2671.35


def test_size_candidate_fails_closed_for_rr_cash_and_symbol_caps():
    low_rr = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=22.0,
        actionable_equity=100000.0,
        cash_available=50000.0,
        original_cash=50000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=25000.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=0.0,
    )
    cash_short = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        actionable_equity=100000.0,
        cash_available=2000.0,
        original_cash=50000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=25000.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=0.0,
    )
    symbol_full = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        actionable_equity=100000.0,
        cash_available=50000.0,
        original_cash=50000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=25000.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=19000.0,
    )

    assert low_rr["suggested_lots"] == 0
    assert "net_rr_below_1_5" in low_rr["failed_gates"]
    assert cash_short["suggested_lots"] == 0
    assert "insufficient_cash_with_buy_costs" in cash_short["failed_gates"]
    assert symbol_full["suggested_lots"] == 0
    assert "post_trade_symbol_cap" in symbol_full["failed_gates"]


def test_size_candidate_enforces_35_percent_cash_and_50_percent_initial_caps():
    candidate_cap = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        actionable_equity=100000.0,
        cash_available=5000.0,
        original_cash=5000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=2500.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=0.0,
    )
    initial_cap = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=24.0,
        actionable_equity=100000.0,
        cash_available=50000.0,
        original_cash=50000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=2000.0,
        remaining_loss_budget=750.0,
        existing_symbol_market_value=0.0,
    )

    assert candidate_cap["suggested_lots"] == 0
    assert "candidate_cash_cap" in candidate_cap["failed_gates"]
    assert initial_cap["suggested_lots"] == 0
    assert "initial_deploy_cap" in initial_cap["failed_gates"]


def test_size_candidate_reports_rr_when_capacity_feasible_lots_all_fail_rr():
    sizing = size_ashare_candidate(
        entry_price=20.0,
        stop_price=18.0,
        target_price=22.0,
        actionable_equity=100000.0,
        cash_available=10000.0,
        original_cash=10000.0,
        remaining_new_exposure=20000.0,
        remaining_initial_deploy=10000.0,
        remaining_loss_budget=1000.0,
        existing_symbol_market_value=0.0,
    )

    assert sizing["suggested_lots"] == 0
    assert "net_rr_below_1_5" in sizing["failed_gates"]
    assert "candidate_cash_cap" in sizing["failed_gates"]
    assert sizing["blocking_failed_gates"] == ["net_rr_below_1_5"]


def test_size_candidate_does_not_report_larger_lot_failures_for_affordable_minimum_lot():
    sizing = size_ashare_candidate(
        entry_price=6.19,
        stop_price=5.30,
        target_price=7.87,
        actionable_equity=10640.0,
        cash_available=10640.0,
        original_cash=10640.0,
        remaining_new_exposure=1276.8,
        remaining_initial_deploy=5320.0,
        remaining_loss_budget=79.8,
        existing_symbol_market_value=0.0,
    )

    assert sizing["suggested_lots"] == 0
    assert "account_loss_budget" in sizing["failed_gates"]
    assert "insufficient_cash_with_buy_costs" in sizing["failed_gates"]
    assert sizing["blocking_failed_gates"] == ["account_loss_budget"]
