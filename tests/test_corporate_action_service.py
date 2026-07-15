from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.corporate_action_service import (
    assess_cn_dividend_actions,
    fetch_cn_dividend_calendar_sync,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


def _dividend_row(*, ex_date: str = "2026-07-17"):
    return {
        "实施方案公告日期": "2026-07-10",
        "分红类型": "年度分红",
        "派息比例": 7.9,
        "股权登记日": "2026-07-16",
        "除权日": ex_date,
        "派息日": ex_date,
        "实施方案分红说明": "10派7.9元(含税)",
        "报告时间": "2025年报",
    }


def test_dividend_three_to_five_sessions_away_is_watch_only():
    result = assess_cn_dividend_actions(
        [_dividend_row()],
        as_of=datetime(2026, 7, 13, 22, 0, tzinfo=CN_TZ),
    )

    assert result["status"] == "upcoming_corporate_action"
    assert result["blocks_new_position"] is False
    assert result["price_plan_adjustment_required"] is False
    assert result["sessions_until_ex_date"] == 4
    assert result["nearest_action"] == {
        "announcement_date": "2026-07-10",
        "action_type": "年度分红",
        "record_date": "2026-07-16",
        "ex_date": "2026-07-17",
        "payment_date": "2026-07-17",
        "cash_dividend_per_share": 0.79,
        "description": "10派7.9元(含税)",
        "report_period": "2025年报",
    }


def test_dividend_inside_two_session_horizon_blocks_new_position():
    result = assess_cn_dividend_actions(
        [_dividend_row()],
        as_of=datetime(2026, 7, 15, 15, 30, tzinfo=CN_TZ),
    )

    assert result["status"] == "corporate_action_within_horizon"
    assert result["blocks_new_position"] is True
    assert result["price_plan_adjustment_required"] is True
    assert result["sessions_until_ex_date"] == 2


def test_ex_date_today_blocks_even_when_market_is_closed():
    result = assess_cn_dividend_actions(
        [_dividend_row()],
        as_of=datetime(2026, 7, 17, 18, 0, tzinfo=CN_TZ),
    )

    assert result["status"] == "corporate_action_today"
    assert result["blocks_new_position"] is True
    assert result["sessions_until_ex_date"] == 0


def test_past_actions_do_not_create_upcoming_risk():
    result = assess_cn_dividend_actions(
        [_dividend_row(ex_date="2026-07-10")],
        as_of=datetime(2026, 7, 13, 22, 0, tzinfo=CN_TZ),
    )

    assert result["status"] == "no_upcoming_corporate_action"
    assert result["blocks_new_position"] is False
    assert result["nearest_action"] is None


def test_nat_ex_dates_are_ignored_without_hiding_valid_upcoming_action():
    result = assess_cn_dividend_actions(
        [{"除权日": pd.NaT}, {"除权日": pd.NA}, _dividend_row()],
        as_of=datetime(2026, 7, 13, 22, 0, tzinfo=CN_TZ),
    )

    assert result["status"] == "upcoming_corporate_action"
    assert result["nearest_action"]["ex_date"] == "2026-07-17"


def test_fetch_dividend_calendar_uses_cninfo_rows(monkeypatch):
    calls = []

    class FakeFrame:
        def to_dict(self, orient):
            assert orient == "records"
            return [_dividend_row()]

    monkeypatch.setitem(
        __import__("sys").modules,
        "akshare",
        SimpleNamespace(
            stock_dividend_cninfo=lambda symbol: calls.append(symbol) or FakeFrame(),
        ),
    )

    result = fetch_cn_dividend_calendar_sync(
        "sh600900",
        as_of=datetime(2026, 7, 13, 22, 0, tzinfo=CN_TZ),
    )

    assert calls == ["600900"]
    assert result["ok"] is True
    assert result["source"] == "cninfo_via_akshare"
    assert result["code"] == "600900"
    assert result["status"] == "upcoming_corporate_action"
    assert result["nearest_action"]["ex_date"] == "2026-07-17"


def test_fetch_dividend_calendar_returns_structured_unavailable(monkeypatch):
    def fail(*, symbol):
        assert symbol == "600900"
        raise RuntimeError("cninfo unavailable")

    monkeypatch.setitem(
        __import__("sys").modules,
        "akshare",
        SimpleNamespace(stock_dividend_cninfo=fail),
    )

    result = fetch_cn_dividend_calendar_sync("600900")

    assert result == {
        "ok": False,
        "source": "cninfo_via_akshare",
        "code": "600900",
        "status": "corporate_action_unavailable",
        "blocks_new_position": False,
        "price_plan_adjustment_required": False,
        "sessions_until_ex_date": None,
        "nearest_action": None,
        "reason": "cninfo unavailable",
        "is_reference_only": True,
    }
