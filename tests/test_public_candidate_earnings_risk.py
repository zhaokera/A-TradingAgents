from datetime import date

import pytest

from app.services.public_candidate_earnings_risk import (
    EARNINGS_ACTUAL_SOURCE,
    EARNINGS_FORECAST_SOURCE,
    latest_completed_reporting_period,
    latest_mandatory_actual_reporting_period,
    screen_public_candidate_earnings_risk,
)


def _positive_actual_rows(*codes):
    return [
        {
            "股票代码": code,
            "股票简称": f"candidate-{code}",
            "净利润-净利润": 10_000_000.0,
            "净利润-同比增长": 10.0,
            "营业总收入-营业总收入": 100_000_000.0,
            "营业总收入-同比增长": 5.0,
            "最新公告日期": "2026-04-29",
        }
        for code in codes
    ]


@pytest.mark.parametrize(
    ("trade_date", "expected_period"),
    [
        ("2026-01-15", "20251231"),
        ("2026-04-01", "20260331"),
        (date(2026, 7, 17), "20260630"),
        ("2026-10-31", "20260930"),
    ],
)
def test_latest_completed_reporting_period(trade_date, expected_period):
    assert latest_completed_reporting_period(trade_date) == expected_period


def test_earnings_risk_blocks_loss_forecasts_and_preserves_audit_evidence():
    rows = [
        {
            "股票代码": "688599",
            "股票简称": "天合光能",
            "预测指标": "归属于上市公司股东的净利润",
            "业绩变动": "预计2026年1-6月净利润亏损",
            "预测数值": -270_000_000,
            "业绩变动幅度": 90.745,
            "业绩变动原因": "投资收益改善归母利润，但主营仍承压。",
            "预告类型": "减亏",
            "公告日期": "2026-07-17",
        },
        {
            "股票代码": "688599",
            "股票简称": "天合光能",
            "预测指标": "扣除非经常性损益后的净利润",
            "业绩变动": "预计2026年1-6月扣非净利润亏损",
            "预测数值": -2_870_000_000,
            "业绩变动幅度": 2.895,
            "业绩变动原因": "投资收益改善归母利润，但主营仍承压。",
            "预告类型": "续亏",
            "公告日期": "2026-07-17",
        },
        {
            "股票代码": 2165,
            "股票简称": "红宝丽",
            "预测指标": "归属于上市公司股东的净利润",
            "业绩变动": "预计2026年1-6月净利润亏损",
            "预测数值": -7_500_000,
            "业绩变动幅度": -130.75,
            "业绩变动原因": "国际形势导致主要原料成本同比上涨超过23%。",
            "预告类型": "首亏",
            "公告日期": "2026-07-14",
        },
    ]

    result = screen_public_candidate_earnings_risk(
        ["688599", "002165", "300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: rows,
        actual_loader=lambda _period: _positive_actual_rows(
            "688599", "002165", "300113"
        ),
    )

    assert result["status"] == "ok"
    assert result["source"] == EARNINGS_FORECAST_SOURCE
    assert result["report_period"] == "20260630"
    assert result["screened_count"] == 3
    assert result["blocked_count"] == 2
    assert result["selected_count"] == 1
    assert result["blocked_codes"] == ["688599", "002165"]
    assert result["selected_codes"] == ["300113"]
    assert result["status_counts"] == {
        "loss_forecast": 2,
        "no_forecast": 1,
    }
    assert result["actual_status_counts"] == {"positive_profit": 3}

    by_code = {item["code"]: item for item in result["results"]}
    assert by_code["688599"]["blocks_new_position"] is True
    assert by_code["688599"]["forecast_types"] == ["减亏", "续亏"]
    assert by_code["688599"]["loss_metrics"] == [
        "归属于上市公司股东的净利润",
        "扣除非经常性损益后的净利润",
    ]
    assert len(by_code["688599"]["evidence"]) == 2
    assert by_code["002165"]["reason_summary"].startswith("国际形势")
    assert by_code["300113"]["status"] == "no_forecast"
    assert by_code["300113"]["blocks_new_position"] is False
    assert by_code["300113"]["latest_actual"]["status"] == "positive_profit"


def test_earnings_risk_uses_latest_announcement_for_each_candidate():
    rows = [
        {
            "股票代码": "300113",
            "股票简称": "顺网科技",
            "预测指标": "归属于上市公司股东的净利润",
            "业绩变动": "预计亏损",
            "预测数值": -10_000_000,
            "预告类型": "首亏",
            "公告日期": "2026-07-01",
        },
        {
            "股票代码": "300113",
            "股票简称": "顺网科技",
            "预测指标": "归属于上市公司股东的净利润",
            "业绩变动": "预计盈利",
            "预测数值": 20_000_000,
            "预告类型": "预增",
            "公告日期": "2026-07-15",
        },
    ]

    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: rows,
        actual_loader=lambda _period: _positive_actual_rows("300113"),
    )

    assert result["blocked_count"] == 0
    assert result["selected_codes"] == ["300113"]
    assert result["results"][0]["status"] == "non_loss_forecast"
    assert result["results"][0]["announcement_date"] == "2026-07-15"
    assert result["results"][0]["forecast_types"] == ["预增"]


def test_earnings_risk_blocks_explicit_loss_when_numeric_value_is_missing():
    result = screen_public_candidate_earnings_risk(
        ["002165"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [
            {
                "股票代码": "002165",
                "预测指标": "扣除非经常性损益后的净利润",
                "业绩变动": "预计扣非净利润亏损",
                "预测数值": float("nan"),
                "业绩变动幅度": float("nan"),
                "预告类型": "续亏",
                "公告日期": "2026-07-14",
            }
        ],
        actual_loader=lambda _period: _positive_actual_rows("002165"),
    )

    evidence = result["results"][0]["evidence"][0]
    assert result["blocked_codes"] == ["002165"]
    assert evidence["forecast_value"] is None
    assert evidence["forecast_change_pct"] is None


def test_earnings_risk_fails_closed_when_loader_raises():
    def failing_loader(_period):
        raise TimeoutError("provider timeout")

    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=failing_loader,
    )

    assert result == {
        "status": "earnings_forecast_unavailable",
        "source": EARNINGS_FORECAST_SOURCE,
        "report_period": "20260630",
        "error_type": "TimeoutError",
        "results": [],
    }


@pytest.mark.parametrize(
    ("codes", "trade_date", "expected_error"),
    [
        (["300113", "300113"], "2026-07-17", "duplicate_code"),
        (["ABC"], "2026-07-17", "invalid_code"),
        (["300113"], "invalid", "benchmark_trade_date_invalid"),
    ],
)
def test_earnings_risk_rejects_invalid_input(codes, trade_date, expected_error):
    result = screen_public_candidate_earnings_risk(
        codes,
        benchmark_trade_date=trade_date,
        loader=lambda _period: [],
    )

    assert result["status"] == "earnings_forecast_invalid_input"
    assert result["error_type"] == expected_error
    assert result["results"] == []


@pytest.mark.parametrize(
    ("trade_date", "expected_period"),
    [
        ("2026-01-15", "20250930"),
        ("2026-04-30", "20250930"),
        ("2026-05-01", "20260331"),
        ("2026-08-31", "20260331"),
        ("2026-09-01", "20260630"),
        ("2026-10-31", "20260630"),
        ("2026-11-01", "20260930"),
    ],
)
def test_latest_mandatory_actual_reporting_period(trade_date, expected_period):
    assert latest_mandatory_actual_reporting_period(trade_date) == expected_period


def test_no_forecast_blocks_severe_latest_actual_revenue_contraction():
    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [],
        actual_loader=lambda _period: [
            {
                "股票代码": "300113",
                "股票简称": "顺网科技",
                "每股收益": 0.12,
                "营业总收入-营业总收入": 284_584_509.08,
                "营业总收入-同比增长": -50.7654,
                "营业总收入-季度环比增长": 20.7777,
                "净利润-净利润": 80_491_519.51,
                "净利润-同比增长": 9.56,
                "净利润-季度环比增长": -14.2452,
                "每股净资产": 3.8526,
                "净资产收益率": 3.1,
                "每股经营现金流量": 0.0227,
                "销售毛利率": 69.0031,
                "所处行业": "游戏Ⅱ",
                "最新公告日期": "2026-04-29",
            }
        ],
    )

    assert result["status"] == "ok"
    assert result["actual_source"] == EARNINGS_ACTUAL_SOURCE
    assert result["actual_report_period"] == "20260331"
    assert result["actual_status_counts"] == {"positive_profit": 1}
    assert result["blocked_codes"] == ["300113"]
    assert result["selected_codes"] == []
    assert result["results"][0]["blocks_new_position"] is True
    actual = result["results"][0]["latest_actual"]
    assert actual == {
        "status": "positive_profit",
        "report_period": "20260331",
        "announcement_date": "2026-04-29",
        "net_profit": 80_491_519.51,
        "net_profit_yoy_pct": 9.56,
        "net_profit_qoq_pct": -14.2452,
        "revenue": 284_584_509.08,
        "revenue_yoy_pct": -50.7654,
        "revenue_qoq_pct": 20.7777,
        "eps": 0.12,
        "book_value_per_share": 3.8526,
        "roe_pct": 3.1,
        "operating_cash_flow_per_share": 0.0227,
        "gross_margin_pct": 69.0031,
        "industry": "游戏Ⅱ",
        "risk_flags": ["severe_revenue_contraction"],
    }


def test_non_loss_forecast_blocks_severe_profit_decline():
    result = screen_public_candidate_earnings_risk(
        ["002318"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [
            {
                "股票代码": "002318",
                "股票简称": "久立特材",
                "预测指标": "归属于上市公司股东的净利润",
                "业绩变动": "预计2026年1-6月净利润同比下降",
                "预测数值": 400_000_000,
                "业绩变动幅度": -52.0,
                "业绩变动原因": "需求偏弱且海外交付延后。",
                "预告类型": "预减",
                "公告日期": "2026-07-17",
            }
        ],
        actual_loader=lambda _period: _positive_actual_rows("002318"),
    )

    assert result["status"] == "ok"
    assert result["blocked_codes"] == ["002318"]
    assert result["selected_codes"] == []
    assert result["results"][0]["status"] == "non_loss_forecast"
    assert result["results"][0]["blocks_new_position"] is True


def test_moderate_actual_decline_and_negative_cash_flow_remain_warning_only():
    result = screen_public_candidate_earnings_risk(
        ["300803"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [],
        actual_loader=lambda _period: [
            {
                "股票代码": "300803",
                "净利润-净利润": 111_000_000,
                "净利润-同比增长": -19.8,
                "营业总收入-营业总收入": 350_000_000,
                "营业总收入-同比增长": 30.96,
                "每股经营现金流量": -0.2,
                "最新公告日期": "2026-04-29",
            }
        ],
    )

    assert result["blocked_codes"] == []
    assert result["selected_codes"] == ["300803"]
    assert result["results"][0]["blocks_new_position"] is False
    assert result["results"][0]["latest_actual"]["risk_flags"] == [
        "net_profit_yoy_decline",
        "negative_operating_cash_flow",
    ]


def test_latest_actual_loss_blocks_candidate_without_loss_forecast():
    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [],
        actual_loader=lambda _period: [
            {
                "股票代码": "300113",
                "净利润-净利润": -5_000_000,
                "净利润-同比增长": -120.0,
                "营业总收入-营业总收入": 100_000_000,
                "营业总收入-同比增长": -10.0,
                "最新公告日期": "2026-04-29",
            }
        ],
    )

    assert result["blocked_codes"] == ["300113"]
    assert result["actual_status_counts"] == {"actual_loss": 1}
    assert result["results"][0]["status"] == "no_forecast"
    assert result["results"][0]["latest_actual"]["status"] == "actual_loss"
    assert result["results"][0]["blocks_new_position"] is True


def test_missing_or_future_actual_report_blocks_candidate():
    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [],
        actual_loader=lambda _period: [
            {
                "股票代码": "300113",
                "净利润-净利润": 80_000_000,
                "最新公告日期": "2026-07-18",
            }
        ],
    )

    assert result["blocked_codes"] == ["300113"]
    assert result["actual_status_counts"] == {"actual_missing": 1}
    assert result["results"][0]["latest_actual"]["risk_flags"] == [
        "actual_report_missing"
    ]


def test_actual_earnings_provider_failure_is_fail_closed():
    def failing_actual_loader(_period):
        raise TimeoutError("provider timeout")

    result = screen_public_candidate_earnings_risk(
        ["300113"],
        benchmark_trade_date="2026-07-17",
        loader=lambda _period: [],
        actual_loader=failing_actual_loader,
    )

    assert result == {
        "status": "earnings_actual_unavailable",
        "source": EARNINGS_FORECAST_SOURCE,
        "actual_source": EARNINGS_ACTUAL_SOURCE,
        "report_period": "20260630",
        "actual_report_period": "20260331",
        "error_type": "TimeoutError",
        "results": [],
    }
