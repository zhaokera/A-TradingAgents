from app.services.holding_ai_advice import parse_model_advice_response, parse_report_recommendation


def test_parse_model_advice_response_extracts_json_block():
    raw = """
    下面是建议：
    {
      "action": "sell",
      "confidence": 0.72,
      "suggested_buy_price": 58.5,
      "suggested_sell_price": 68.0,
      "target_price": 72.0,
      "stop_loss_price": 56.2,
      "position_suggestion": "减仓30%",
      "reason": "已接近月目标，分批兑现。",
      "risks": ["回撤风险", "成交量不足"]
    }
    """

    parsed = parse_model_advice_response(raw)

    assert parsed["action"] == "sell"
    assert parsed["suggested_sell_price"] == 68.0
    assert parsed["target_price"] == 72.0
    assert parsed["stop_loss_price"] == 56.2
    assert parsed["risks"] == ["回撤风险", "成交量不足"]


def test_parse_model_advice_response_returns_hold_fallback_for_invalid_text():
    parsed = parse_model_advice_response("模型没有按 JSON 返回")

    assert parsed["action"] == "hold"
    assert parsed["target_price"] is None
    assert "模型返回格式无法解析" in parsed["reason"]


def test_parse_report_recommendation_extracts_action_target_and_reason():
    parsed = parse_report_recommendation(
        "投资建议：卖出。目标价格：32.0元。决策依据：估值偏高且趋势走弱。",
        current_price=63.36,
    )

    assert parsed["action"] == "sell"
    assert parsed["target_price"] == 32.0
    assert parsed["suggested_sell_price"] == 63.36
    assert parsed["reason"] == "估值偏高且趋势走弱。"
    assert parsed["source"] == "analysis_report_recommendation"


def test_parse_report_recommendation_uses_current_price_for_buy_reference():
    parsed = parse_report_recommendation(
        "投资建议：买入。目标价：80元。决策依据：成长性改善。",
        current_price=63.36,
    )

    assert parsed["action"] == "buy"
    assert parsed["suggested_buy_price"] == 63.36
    assert parsed["suggested_sell_price"] is None
    assert parsed["target_price"] == 80.0
