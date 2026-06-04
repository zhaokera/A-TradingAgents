import asyncio

from app.services.holding_ai_advice import (
    build_holding_report_advice,
    parse_model_advice_response,
    parse_report_recommendation,
)


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


def test_parse_report_recommendation_prefers_key_price_zone_over_valuation_target():
    market_report = """
    ### 3. 关键价格区间

    | 价格类型 | 具体价格（¥） | 说明 |
    |---------|-------------|------|
    | **强支撑位** | 62.33 | 布林带下轨，跌破则趋势恶化 |
    | **次支撑位** | 63.07 | 近5日最低价 |
    | **第一压力位** | 65.40 - 65.81 | MA5 与 MA60 密集区 |
    | **第二压力位** | 67.42 | MA10 |
    | **强压力位** | 70.27 | MA20 及布林带中轨 |
    | **突破买入价** | 66.00 | 放量站上 MA60 可轻仓试多 |
    | **跌破卖出价** | 62.00 | 有效跌破布林带下轨坚决止损 |
    """

    parsed = parse_report_recommendation(
        "投资建议：卖出。目标价格：32.0元。决策依据：估值偏高且趋势走弱。",
        current_price=63.36,
        decision={"action": "卖出", "target_price": 32, "confidence": 0.95},
        reports={"market_report": market_report},
    )

    assert parsed["action"] == "sell"
    assert parsed["stop_loss_price"] == 62.0
    assert parsed["suggested_buy_price"] == 66.0
    assert parsed["suggested_sell_price"] == 67.42
    assert parsed["target_price"] == 70.27
    assert parsed["source"] == "analysis_report_price_levels"


def test_build_holding_report_advice_uses_report_price_plan(monkeypatch):
    async def fake_latest_report_context(code: str):
        return {
            "report": {
                "id": "report-id",
                "analysis_id": "000977_20260603",
                "model_info": "analysis_report",
                "recommendation": "投资建议：卖出。目标价格：32.0元。决策依据：估值偏高。",
                "decision": {"action": "卖出", "target_price": 32, "confidence": 0.95},
                "price_plan": {
                    "stop_loss_price": 62.0,
                    "suggested_buy_price": 66.0,
                    "suggested_sell_price": 67.42,
                    "target_price": 70.27,
                },
            }
        }

    monkeypatch.setattr(
        "app.services.holding_ai_advice._latest_report_context",
        fake_latest_report_context,
    )

    advice = asyncio.run(build_holding_report_advice({"code": "000977", "current_price": 63.36}))

    assert advice["stop_loss_price"] == 62.0
    assert advice["suggested_buy_price"] == 66.0
    assert advice["suggested_sell_price"] == 67.42
    assert advice["target_price"] == 70.27
    assert advice["provider"] == "analysis_report"
