from app.services.holding_analysis import build_holding_analysis


def test_no_holding_returns_none():
    assert build_holding_analysis(None, {"action": "持有"}, current_price=64) is None


def test_near_take_profit_suggests_partial_sell():
    result = build_holding_analysis(
        {
            "cost_price": 60,
            "shares": 1000,
            "take_profit_price": 72,
            "stop_loss_price": 55,
        },
        {"action": "持有", "risk_score": 0.35},
        current_price=70,
    )

    assert result is not None
    assert result["status"] == "接近止盈"
    assert result["sell_ratio_min"] == 0.3
    assert result["sell_ratio_max"] == 0.5
    assert result["sell_shares_min"] == 300
    assert result["sell_shares_max"] == 500
    assert result["profit_loss_pct"] > 0


def test_stop_loss_suggests_high_sell_ratio():
    result = build_holding_analysis(
        {
            "cost_price": 60,
            "shares": 1000,
            "take_profit_price": 72,
            "stop_loss_price": 55,
        },
        {"action": "持有", "risk_score": 0.4},
        current_price=54,
    )

    assert result is not None
    assert result["status"] == "触发止损"
    assert result["sell_ratio_min"] == 0.7
    assert result["sell_ratio_max"] == 1.0
    assert result["sell_shares_min"] == 700
    assert result["sell_shares_max"] == 1000
    assert result["risk_level"] == "高"
