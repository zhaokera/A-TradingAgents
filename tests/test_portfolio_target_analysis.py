from datetime import date

from app.services.portfolio_target_analysis import build_target_analysis


def test_monthly_target_reached_suggests_partial_sell():
    result = build_target_analysis(
        {
            "quantity": 1000,
            "cost_price": 10,
            "target_monthly_return_pct": 10,
            "stop_loss_pct": 8,
        },
        current_price=11.2,
        as_of=date(2026, 6, 15),
    )

    assert result["status"] == "目标已达成"
    assert result["action"] == "sell"
    assert result["suggested_ratio_min"] == 0.3
    assert result["suggested_ratio_max"] == 0.6
    assert result["suggested_shares_text"] == "300-600股"
    assert result["monthly_target_progress_pct"] >= 100


def test_loss_over_stop_loss_suggests_risk_sell():
    result = build_target_analysis(
        {
            "quantity": 1000,
            "cost_price": 10,
            "target_monthly_return_pct": 10,
            "stop_loss_pct": 8,
        },
        current_price=9.1,
        as_of=date(2026, 6, 15),
    )

    assert result["status"] == "触发止损"
    assert result["action"] == "sell"
    assert result["suggested_ratio_min"] == 0.5
    assert result["suggested_ratio_max"] == 1.0
    assert result["suggested_shares_text"] == "500-1000股"
    assert result["profit_loss_pct"] < 0


def test_behind_target_but_profitable_holds_without_chasing():
    result = build_target_analysis(
        {
            "quantity": 1000,
            "cost_price": 10,
            "target_monthly_return_pct": 10,
            "stop_loss_pct": 8,
        },
        current_price=10.3,
        as_of=date(2026, 6, 15),
    )

    assert result["status"] == "目标落后"
    assert result["action"] == "hold"
    assert result["required_daily_return_pct"] > 0
    assert result["suggested_ratio_text"] == "0%-0%"
