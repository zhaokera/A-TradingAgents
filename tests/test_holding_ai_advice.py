import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.holding_ai_advice import (
    build_holding_ai_advice,
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


def test_build_holding_report_advice_nulls_active_prices_when_fee_rr_fails(monkeypatch):
    async def fake_latest_report_context(code: str):
        return {
            "report": {
                "id": "report-id",
                "analysis_id": "000977_20260603",
                "analysis_date": "2026-07-10",
                "created_at": "2026-07-10T15:10:00+08:00",
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

    advice = asyncio.run(
        build_holding_report_advice(
            {
                "code": "000977",
                "current_price": 63.36,
                "quote_snapshot": {"freshness": {"actionable": True, "status": "fresh"}},
                "technical_price_plan": {
                    "actionable": True,
                    "stop_loss_price": 61.8,
                    "suggested_buy_price": 65.8,
                    "suggested_sell_price": 67.0,
                    "target_price": 70.0,
                },
                "benchmark_session_dates": ["2026-07-10", "2026-07-13"],
                "guardrail_as_of": datetime(
                    2026,
                    7,
                    13,
                    10,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            }
        )
    )

    assert advice["stop_loss_price"] is None
    assert advice["suggested_buy_price"] is None
    assert advice["suggested_sell_price"] is None
    assert advice["target_price"] is None
    assert advice["provider"] == "analysis_report"
    assert advice["price_plan_guardrail"]["actionable"] is False
    assert advice["price_plan_guardrail"]["status"] == "net_rr_below_1_5"
    assert advice["price_plan_guardrail"]["fee_aware_trade"]["net_reward_risk"] < 1.5
    assert advice["price_plan_guardrail"]["target_price"] == 70.27
    assert advice["price_plan_guardrail"]["sources"]["target_price"] == "report"
    assert advice["historical_report_price_plan"]["target_price"] == 70.27
    assert advice["report_freshness"]["status"] == "fresh_report"


def test_build_holding_report_advice_moves_stale_prices_to_history(monkeypatch):
    async def fake_latest_report_context(code: str):
        return {
            "report": {
                "id": "old-report",
                "analysis_id": "000977_20260710",
                "analysis_date": "2026-07-10",
                "created_at": "2026-07-10T15:10:00+08:00",
                "model_info": "analysis_report",
                "recommendation": "投资建议：卖出。目标价格：32.0元。决策依据：历史估值偏高。",
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
    holding = {
        "code": "000977",
        "current_price": 72.0,
        "quote_snapshot": {"freshness": {"actionable": True, "status": "fresh"}},
        "technical_price_plan": {
            "actionable": True,
            "stop_loss_price": 69.0,
            "suggested_buy_price": 73.0,
            "suggested_sell_price": 76.0,
            "target_price": 82.0,
        },
        "benchmark_session_dates": ["2026-07-10", "2026-07-13", "2026-07-14"],
        "guardrail_as_of": datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    }

    advice = asyncio.run(build_holding_report_advice(holding))

    assert advice["report_freshness"]["status"] == "stale_report"
    assert advice["stop_loss_price"] == 69.0
    assert advice["suggested_buy_price"] == 73.0
    assert advice["suggested_sell_price"] == 76.0
    assert advice["target_price"] == 82.0
    assert advice["price_plan_guardrail"]["sources"]["target_price"] == "technical"
    assert advice["historical_report_price_plan"]["target_price"] == 70.27


def test_build_holding_report_advice_nulls_prices_when_no_current_technical_plan(monkeypatch):
    async def fake_latest_report_context(code: str):
        return {
            "report": {
                "id": "old-report",
                "analysis_date": "2026-07-10",
                "created_at": "2026-07-10T15:10:00+08:00",
                "model_info": "analysis_report",
                "recommendation": "投资建议：卖出。决策依据：历史报告。",
                "decision": {"action": "卖出"},
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

    advice = asyncio.run(
        build_holding_report_advice(
            {
                "code": "000977",
                "quote_snapshot": {"freshness": {"actionable": False, "status": "off_session"}},
                "technical_price_plan": {"actionable": False, "status": "quote_not_actionable"},
                "benchmark_session_dates": ["2026-07-10", "2026-07-13", "2026-07-14"],
                "guardrail_as_of": datetime(
                    2026,
                    7,
                    14,
                    10,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            }
        )
    )

    assert advice["price_plan_guardrail"]["actionable"] is False
    assert advice["stop_loss_price"] is None
    assert advice["suggested_buy_price"] is None
    assert advice["suggested_sell_price"] is None
    assert advice["target_price"] is None
    assert advice["historical_report_price_plan"]["stop_loss_price"] == 62.0


def test_model_advice_without_report_cannot_bypass_current_price_guardrails(monkeypatch):
    async def no_report(code: str):
        return {"report": None, "text": "暂无历史分析报告。"}

    async def fake_llm_config():
        return {
            "provider": "test",
            "model_name": "test-model",
            "api_base": "https://example.invalid/v1",
            "api_key": "test-key",
            "temperature": 0.0,
            "max_tokens": 1000,
            "timeout": 10,
            "retry_times": 0,
        }

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"action":"sell","confidence":0.9,'
                                '"stop_loss_price":30,"suggested_buy_price":31,'
                                '"suggested_sell_price":32,"target_price":33,'
                                '"position_suggestion":"观察","reason":"旧模型价位",'
                                '"risks":[]}'
                            )
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.services.holding_ai_advice._latest_report_context",
        no_report,
    )
    monkeypatch.setattr(
        "app.services.holding_ai_advice._select_llm_config",
        fake_llm_config,
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeOpenAI))

    advice = asyncio.run(
        build_holding_ai_advice(
            {
                "code": "000977",
                "current_price": 63.5,
                "quote_snapshot": {"freshness": {"actionable": True, "status": "fresh"}},
                "technical_price_plan": {
                    "actionable": True,
                    "stop_loss_price": 63.0,
                    "suggested_buy_price": 66.0,
                    "suggested_sell_price": 68.0,
                    "target_price": 72.0,
                },
            }
        )
    )

    assert advice["stop_loss_price"] == 63.0
    assert advice["suggested_buy_price"] == 66.0
    assert advice["suggested_sell_price"] == 68.0
    assert advice["target_price"] == 72.0
    assert advice["historical_model_price_plan"]["target_price"] == 33.0
    assert advice["price_plan_guardrail"]["sources"]["target_price"] == "technical"
