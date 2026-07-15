from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.holding_price_guardrails import (
    assess_report_freshness,
    build_technical_price_plan,
    calculate_net_reward_risk,
    resolve_guarded_price_plan,
)


def _technical_bars():
    closes = [120.0] * 55 + [95.0, 96.0, 97.0, 98.0, 100.0]
    return [
        {
            "date": f"2026-04-{index + 1:02d}" if index < 30 else f"2026-05-{index - 29:02d}",
            "open": close,
            "close": close,
            "high": close + 1,
            "low": close - 1,
        }
        for index, close in enumerate(closes)
    ]


def test_build_technical_price_plan_uses_exact_indicator_and_rounding_contract():
    plan = build_technical_price_plan(_technical_bars(), current_price=100.0)

    assert plan["status"] == "ok"
    assert plan["actionable"] is True
    assert plan["metrics"] == {
        "ma5": 97.2,
        "ma10": 108.6,
        "ma20": 114.3,
        "ma60": 118.1,
        "boll_mid": 114.3,
        "boll_upper": 134.6351,
        "boll_lower": 93.9649,
        "recent_5_low": 94.0,
        "recent_20_low": 94.0,
        "recent_20_high": 121.0,
    }
    assert plan["levels"]["resistance_1"] == 108.6
    assert plan["levels"]["resistance_2"] == 114.3
    assert plan["levels"]["resistance_3"] == 118.1
    assert plan["stop_loss_price"] == 93.49
    assert plan["suggested_buy_price"] == 108.93
    assert plan["suggested_sell_price"] == 114.3
    assert plan["target_price"] == 118.1
    assert plan["research_watch_levels"]["nearest_support"] == 97.2
    assert plan["research_watch_levels"]["lower_supports"] == [94.0, 93.9649]
    assert plan["research_watch_levels"]["nearest_resistance"] == 108.6
    assert plan["research_watch_levels"]["higher_resistances"] == [
        114.3,
        118.1,
        121.0,
        134.6351,
    ]
    assert plan["research_watch_levels"]["actionable"] is False
    assert plan["rounding"] == {
        "tick": 0.01,
        "stop_buffer_pct": 0.5,
        "breakout_buffer_pct": 0.3,
        "stop_mode": "ROUND_FLOOR",
        "breakout_mode": "ROUND_CEILING",
        "default_mode": "ROUND_HALF_UP",
    }


def test_build_technical_price_plan_returns_structured_failure_for_missing_levels():
    bars = [
        {
            "date": f"2026-04-{index + 1:02d}" if index < 30 else f"2026-05-{index - 29:02d}",
            "open": 100.0,
            "close": 100.0,
            "high": 100.0,
            "low": 100.0,
        }
        for index in range(60)
    ]

    plan = build_technical_price_plan(bars, current_price=100.0)

    assert plan["actionable"] is False
    assert plan["status"] == "insufficient_ordered_levels"
    assert "resistance_1" in plan["missing_levels"]


def test_nonactionable_plan_keeps_structured_research_watch_levels():
    bars = [
        {
            "date": f"2026-04-{index + 1:02d}" if index < 30 else f"2026-05-{index - 29:02d}",
            "open": 100.0,
            "close": 100.0,
            "high": 100.0,
            "low": 100.0,
        }
        for index in range(60)
    ]

    plan = build_technical_price_plan(bars, current_price=101.0)

    assert plan["actionable"] is False
    assert plan["status"] == "insufficient_ordered_levels"
    assert plan["research_watch_levels"] == {
        "status": "reference_only",
        "actionable": False,
        "is_reference_only": True,
        "current_price": 101.0,
        "nearest_support": 100.0,
        "lower_supports": [],
        "nearest_resistance": None,
        "higher_resistances": [],
        "supports": [100.0],
        "resistances": [],
    }


def test_insufficient_history_exposes_unavailable_watch_level_schema():
    plan = build_technical_price_plan(_technical_bars()[:20], current_price=100.0)

    assert plan["status"] == "insufficient_history"
    assert plan["research_watch_levels"] == {
        "status": "unavailable_insufficient_history",
        "actionable": False,
        "is_reference_only": True,
        "current_price": 100.0,
        "nearest_support": None,
        "lower_supports": [],
        "nearest_resistance": None,
        "higher_resistances": [],
        "supports": [],
        "resistances": [],
    }


def test_report_freshness_keeps_friday_valid_monday_and_expires_tuesday():
    tz = ZoneInfo("Asia/Shanghai")
    session_dates = ["2026-07-10", "2026-07-13", "2026-07-14"]

    monday = assess_report_freshness(
        "2026-07-10",
        as_of=datetime(2026, 7, 13, 10, 0, tzinfo=tz),
        benchmark_session_dates=session_dates,
    )
    tuesday = assess_report_freshness(
        "2026-07-10",
        as_of=datetime(2026, 7, 14, 10, 0, tzinfo=tz),
        benchmark_session_dates=session_dates,
    )

    assert monday["actionable"] is True
    assert monday["started_sessions_after_report"] == 1
    assert monday["calendar_source"] == "tencent_benchmark"
    assert tuesday["actionable"] is False
    assert tuesday["status"] == "stale_report"
    assert tuesday["started_sessions_after_report"] == 2


def test_report_freshness_weekday_fallback_is_explicit_and_waits_for_open():
    tz = ZoneInfo("Asia/Shanghai")

    before_tuesday_open = assess_report_freshness(
        "2026-07-10",
        as_of=datetime(2026, 7, 14, 9, 0, tzinfo=tz),
        benchmark_session_dates=None,
    )
    after_tuesday_open = assess_report_freshness(
        "2026-07-10",
        as_of=datetime(2026, 7, 14, 9, 31, tzinfo=tz),
        benchmark_session_dates=None,
    )

    assert before_tuesday_open["actionable"] is True
    assert before_tuesday_open["started_sessions_after_report"] == 1
    assert before_tuesday_open["calendar_source"] == "weekday_fallback"
    assert before_tuesday_open["calendar_is_fallback"] is True
    assert after_tuesday_open["actionable"] is False
    assert after_tuesday_open["started_sessions_after_report"] == 2


def test_report_freshness_fails_closed_when_benchmark_calendar_has_weekday_gap():
    result = assess_report_freshness(
        "2026-07-10",
        as_of=datetime(
            2026,
            7,
            14,
            10,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        benchmark_session_dates=["2026-07-10", "2026-07-14"],
    )

    assert result["actionable"] is False
    assert result["started_sessions_after_report"] == 2
    assert result["calendar_source"] == "weekday_fallback"
    assert result["calendar_is_fallback"] is True
    assert result["calendar_fallback_reason"] == "incomplete_benchmark_calendar"


def test_resolve_guarded_plan_uses_manual_then_validated_report_then_technical():
    result = resolve_guarded_price_plan(
        manual_plan={"stop_loss_price": 89.0},
        report_plan={
            "stop_loss_price": 91.0,
            "suggested_buy_price": 102.0,
            "suggested_sell_price": 111.0,
            "target_price": 118.0,
        },
        technical_plan={
            "actionable": True,
            "stop_loss_price": 90.0,
            "suggested_buy_price": 100.0,
            "suggested_sell_price": 110.0,
            "target_price": 120.0,
        },
        report_freshness={"actionable": True, "status": "fresh_report"},
    )

    assert result["actionable"] is True
    assert result["stop_loss_price"] == 89.0
    assert result["suggested_buy_price"] == 102.0
    assert result["suggested_sell_price"] == 111.0
    assert result["target_price"] == 118.0
    assert result["sources"] == {
        "stop_loss_price": "manual",
        "suggested_buy_price": "report",
        "suggested_sell_price": "report",
        "target_price": "report",
    }
    assert result["executable_tuple"] == {"entry": 102.0, "stop": 89.0, "target": 118.0}


def test_resolve_guarded_plan_replaces_divergent_or_stale_report_fields():
    technical = {
        "actionable": True,
        "stop_loss_price": 90.0,
        "suggested_buy_price": 100.0,
        "suggested_sell_price": 110.0,
        "target_price": 120.0,
    }
    divergent = resolve_guarded_price_plan(
        manual_plan={},
        report_plan={"suggested_buy_price": 80.0, "target_price": 150.0},
        technical_plan=technical,
        report_freshness={"actionable": True, "status": "fresh_report"},
    )
    stale = resolve_guarded_price_plan(
        manual_plan={},
        report_plan={"suggested_buy_price": 102.0, "target_price": 118.0},
        technical_plan=technical,
        report_freshness={"actionable": False, "status": "stale_report"},
    )

    assert divergent["suggested_buy_price"] == 100.0
    assert divergent["target_price"] == 120.0
    assert set(divergent["rejected_report_fields"]) == {"suggested_buy_price", "target_price"}
    assert stale["suggested_buy_price"] == 100.0
    assert stale["target_price"] == 120.0
    assert stale["historical_report_price_plan"]["target_price"] == 118.0
    assert stale["sources"]["target_price"] == "technical"


def test_resolve_guarded_plan_rejects_invalid_price_ordering():
    result = resolve_guarded_price_plan(
        manual_plan={"stop_loss_price": 105.0},
        report_plan={},
        technical_plan={
            "actionable": True,
            "stop_loss_price": 90.0,
            "suggested_buy_price": 100.0,
            "suggested_sell_price": 110.0,
            "target_price": 120.0,
        },
        report_freshness={"actionable": False, "status": "stale_report"},
    )

    assert result["actionable"] is False
    assert result["status"] == "invalid_price_ordering"
    assert "stop_not_below_entry" in result["failed_gates"]


def test_calculate_net_reward_risk_uses_net_order_totals():
    result = calculate_net_reward_risk(
        entry_total_cost=10010.0,
        stop_net_proceeds=9510.0,
        target_net_proceeds=11010.0,
    )

    assert result == {
        "risk_amount": 500.0,
        "reward_amount": 1000.0,
        "net_reward_risk": 2.0,
    }
