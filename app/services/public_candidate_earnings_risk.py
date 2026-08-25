"""Forecast and actual-earnings risk gate for public research candidates."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


EARNINGS_FORECAST_SOURCE = "akshare.eastmoney.stock_yjyg_em"
EARNINGS_ACTUAL_SOURCE = "akshare.eastmoney.stock_yjbb_em"
EARNINGS_REVIEW_SOURCE = f"{EARNINGS_FORECAST_SOURCE}+{EARNINGS_ACTUAL_SOURCE}"
MAX_EARNINGS_SCREEN_CANDIDATES = 100
SEVERE_EARNINGS_DECLINE_PCT = -30.0
PUBLIC_EARNINGS_SCREEN_STATUS_KEYS = frozenset(
    {
        "loss_forecast",
        "no_forecast",
        "non_loss_forecast",
    }
)
PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS = frozenset(
    {
        "positive_profit",
        "actual_loss",
        "actual_missing",
    }
)
PUBLIC_ACTUAL_EARNINGS_RISK_FLAGS = frozenset(
    {
        "actual_report_missing",
        "actual_net_profit_missing",
        "actual_net_loss",
        "severe_revenue_contraction",
        "net_profit_yoy_decline",
        "negative_operating_cash_flow",
    }
)
LOSS_FORECAST_TYPES = frozenset({"首亏", "续亏", "增亏", "减亏"})
RELEVANT_FORECAST_METRICS = frozenset(
    {
        "归属于上市公司股东的净利润",
        "归属于母公司所有者的净利润",
        "扣除非经常性损益后的净利润",
    }
)
FORECAST_METRIC_ORDER = {
    "归属于上市公司股东的净利润": 0,
    "归属于母公司所有者的净利润": 0,
    "扣除非经常性损益后的净利润": 1,
}
A_SHARE_CODE_PATTERN = re.compile(r"[0-9]{6}")
ForecastLoader = Callable[[str], Any]
ActualLoader = Callable[[str], Any]


def _normalized_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def latest_completed_reporting_period(benchmark_trade_date: Any) -> str:
    """Return the latest quarter end completed before the trade date."""
    trade_date = _normalized_date(benchmark_trade_date)
    if trade_date is None:
        raise ValueError("benchmark_trade_date_invalid")
    if trade_date.month <= 3:
        return f"{trade_date.year - 1}1231"
    if trade_date.month <= 6:
        return f"{trade_date.year}0331"
    if trade_date.month <= 9:
        return f"{trade_date.year}0630"
    return f"{trade_date.year}0930"


def latest_mandatory_actual_reporting_period(
    benchmark_trade_date: Any,
) -> str:
    """Return the latest reporting period whose filing deadline has passed."""
    trade_date = _normalized_date(benchmark_trade_date)
    if trade_date is None:
        raise ValueError("benchmark_trade_date_invalid")
    if trade_date.month <= 4:
        return f"{trade_date.year - 1}0930"
    if trade_date.month <= 8:
        return f"{trade_date.year}0331"
    if trade_date.month <= 10:
        return f"{trade_date.year}0630"
    return f"{trade_date.year}0930"


def _normalized_code(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value).zfill(6)
    elif isinstance(value, str):
        text = value.strip().zfill(6)
    else:
        return None
    return text if A_SHARE_CODE_PATTERN.fullmatch(text) else None


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalized_text(value: Any, *, limit: int = 300) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _rows_from_loader_payload(value: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(value, list):
        rows = value
    elif hasattr(value, "to_dict"):
        try:
            rows = value.to_dict(orient="records")
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not all(isinstance(row, Mapping) for row in rows):
        return None
    return [dict(row) for row in rows]


def _load_earnings_forecasts(report_period: str) -> Any:
    import akshare as ak

    return ak.stock_yjyg_em(date=report_period)


def _load_actual_earnings(report_period: str) -> Any:
    import akshare as ak

    return ak.stock_yjbb_em(date=report_period)


def _invalid_result(error_type: str) -> Dict[str, Any]:
    return {
        "status": "earnings_forecast_invalid_input",
        "source": EARNINGS_FORECAST_SOURCE,
        "error_type": error_type,
        "results": [],
    }


def _latest_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = list(rows)
    dated_rows = [
        (row, _normalized_date(row.get("公告日期")))
        for row in normalized
    ]
    valid_dates = [announcement_date for _, announcement_date in dated_rows if announcement_date]
    if not valid_dates:
        return normalized
    latest_date = max(valid_dates)
    return [
        row
        for row, announcement_date in dated_rows
        if announcement_date == latest_date
    ]


def _is_loss_forecast(row: Mapping[str, Any]) -> bool:
    metric = _normalized_text(row.get("预测指标"))
    if metric not in RELEVANT_FORECAST_METRICS:
        return False
    forecast_value = _finite_number(row.get("预测数值"))
    forecast_type = _normalized_text(row.get("预告类型"))
    change_text = _normalized_text(row.get("业绩变动"), limit=500) or ""
    return bool(
        (forecast_value is not None and forecast_value < 0)
        or forecast_type in LOSS_FORECAST_TYPES
        or "亏损" in change_text
    )


def _candidate_result(code: str, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    latest_rows = _latest_rows(rows)
    relevant_rows = sorted(
        (
            row
            for row in latest_rows
            if _normalized_text(row.get("预测指标"))
            in RELEVANT_FORECAST_METRICS
        ),
        key=lambda row: FORECAST_METRIC_ORDER.get(
            _normalized_text(row.get("预测指标")) or "",
            99,
        ),
    )
    loss_rows = [row for row in relevant_rows if _is_loss_forecast(row)]
    announcement_dates = [
        _normalized_date(row.get("公告日期"))
        for row in latest_rows
    ]
    valid_announcement_dates = [value for value in announcement_dates if value]
    announcement_date = (
        max(valid_announcement_dates).isoformat()
        if valid_announcement_dates
        else None
    )
    forecast_types = list(
        dict.fromkeys(
            text
            for text in (
                _normalized_text(row.get("预告类型"))
                for row in relevant_rows
            )
            if text
        )
    )
    loss_metrics = list(
        dict.fromkeys(
            text
            for text in (
                _normalized_text(row.get("预测指标"))
                for row in loss_rows
            )
            if text
        )
    )
    reason_summary = next(
        (
            text
            for text in (
                _normalized_text(row.get("业绩变动原因"))
                for row in latest_rows
            )
            if text
        ),
        None,
    )
    evidence = [
        {
            "metric": _normalized_text(row.get("预测指标")),
            "forecast_type": _normalized_text(row.get("预告类型")),
            "forecast_value": _finite_number(row.get("预测数值")),
            "forecast_change_pct": _finite_number(row.get("业绩变动幅度")),
            "forecast_text": _normalized_text(row.get("业绩变动"), limit=300),
        }
        for row in relevant_rows
    ]
    if not latest_rows:
        status = "no_forecast"
    elif loss_rows:
        status = "loss_forecast"
    else:
        status = "non_loss_forecast"
    return {
        "code": code,
        "status": status,
        "blocks_new_position": status == "loss_forecast",
        "announcement_date": announcement_date,
        "forecast_types": forecast_types,
        "loss_metrics": loss_metrics,
        "reason_summary": reason_summary,
        "evidence": evidence,
    }


def _empty_actual_result(
    report_period: str,
    *,
    risk_flag: str,
) -> Dict[str, Any]:
    return {
        "status": "actual_missing",
        "report_period": report_period,
        "announcement_date": None,
        "net_profit": None,
        "net_profit_yoy_pct": None,
        "net_profit_qoq_pct": None,
        "revenue": None,
        "revenue_yoy_pct": None,
        "revenue_qoq_pct": None,
        "eps": None,
        "book_value_per_share": None,
        "roe_pct": None,
        "operating_cash_flow_per_share": None,
        "gross_margin_pct": None,
        "industry": None,
        "risk_flags": [risk_flag],
    }


def _candidate_actual_result(
    report_period: str,
    rows: Sequence[Dict[str, Any]],
    *,
    benchmark_trade_date: date,
) -> Dict[str, Any]:
    eligible_rows = [
        (row, announcement_date)
        for row in rows
        if (
            (announcement_date := _normalized_date(row.get("最新公告日期")))
            is not None
            and announcement_date <= benchmark_trade_date
        )
    ]
    if not eligible_rows:
        return _empty_actual_result(
            report_period,
            risk_flag="actual_report_missing",
        )
    latest_date = max(announcement_date for _, announcement_date in eligible_rows)
    row = next(
        row
        for row, announcement_date in eligible_rows
        if announcement_date == latest_date
    )
    net_profit = _finite_number(row.get("净利润-净利润"))
    if net_profit is None:
        return _empty_actual_result(
            report_period,
            risk_flag="actual_net_profit_missing",
        )

    revenue_yoy_pct = _finite_number(row.get("营业总收入-同比增长"))
    net_profit_yoy_pct = _finite_number(row.get("净利润-同比增长"))
    operating_cash_flow_per_share = _finite_number(
        row.get("每股经营现金流量")
    )
    risk_flags: List[str] = []
    if net_profit < 0:
        risk_flags.append("actual_net_loss")
    if revenue_yoy_pct is not None and revenue_yoy_pct <= -30:
        risk_flags.append("severe_revenue_contraction")
    if net_profit_yoy_pct is not None and net_profit_yoy_pct < 0:
        risk_flags.append("net_profit_yoy_decline")
    if (
        operating_cash_flow_per_share is not None
        and operating_cash_flow_per_share < 0
    ):
        risk_flags.append("negative_operating_cash_flow")

    return {
        "status": "positive_profit" if net_profit > 0 else "actual_loss",
        "report_period": report_period,
        "announcement_date": latest_date.isoformat(),
        "net_profit": net_profit,
        "net_profit_yoy_pct": net_profit_yoy_pct,
        "net_profit_qoq_pct": _finite_number(
            row.get("净利润-季度环比增长")
        ),
        "revenue": _finite_number(row.get("营业总收入-营业总收入")),
        "revenue_yoy_pct": revenue_yoy_pct,
        "revenue_qoq_pct": _finite_number(
            row.get("营业总收入-季度环比增长")
        ),
        "eps": _finite_number(row.get("每股收益")),
        "book_value_per_share": _finite_number(row.get("每股净资产")),
        "roe_pct": _finite_number(row.get("净资产收益率")),
        "operating_cash_flow_per_share": operating_cash_flow_per_share,
        "gross_margin_pct": _finite_number(row.get("销售毛利率")),
        "industry": _normalized_text(row.get("所处行业"), limit=100),
        "risk_flags": risk_flags,
    }


def earnings_result_blocks_new_position(
    *,
    forecast_status: Any,
    evidence: Any,
    latest_actual: Any,
) -> bool:
    """Fail closed on losses, missing actuals, or severe earnings deterioration."""
    if forecast_status == "loss_forecast":
        return True
    if isinstance(evidence, list) and any(
        (change := _finite_number(item.get("forecast_change_pct"))) is not None
        and change <= SEVERE_EARNINGS_DECLINE_PCT
        for item in evidence
        if isinstance(item, Mapping)
    ):
        return True
    if not isinstance(latest_actual, Mapping) or latest_actual.get(
        "status"
    ) != "positive_profit":
        return True
    return any(
        (change := _finite_number(latest_actual.get(field))) is not None
        and change <= SEVERE_EARNINGS_DECLINE_PCT
        for field in ("revenue_yoy_pct", "net_profit_yoy_pct")
    )


def screen_public_candidate_earnings_risk(
    codes: Any,
    *,
    benchmark_trade_date: Any,
    loader: Optional[ForecastLoader] = None,
    actual_loader: Optional[ActualLoader] = None,
) -> Dict[str, Any]:
    """Screen the bounded rolling pool against current earnings evidence."""
    if not isinstance(codes, list):
        return _invalid_result("codes_invalid")
    if len(codes) > MAX_EARNINGS_SCREEN_CANDIDATES:
        return _invalid_result("too_many_candidates")
    normalized_codes: List[str] = []
    for raw_code in codes:
        code = _normalized_code(raw_code)
        if code is None:
            return _invalid_result("invalid_code")
        if code in normalized_codes:
            return _invalid_result("duplicate_code")
        normalized_codes.append(code)
    try:
        report_period = latest_completed_reporting_period(
            benchmark_trade_date
        )
        actual_report_period = latest_mandatory_actual_reporting_period(
            benchmark_trade_date
        )
    except ValueError:
        return _invalid_result("benchmark_trade_date_invalid")
    benchmark_date = _normalized_date(benchmark_trade_date)
    assert benchmark_date is not None

    effective_loader = loader or _load_earnings_forecasts
    try:
        raw_rows = effective_loader(report_period)
    except Exception as exc:
        return {
            "status": "earnings_forecast_unavailable",
            "source": EARNINGS_FORECAST_SOURCE,
            "report_period": report_period,
            "error_type": type(exc).__name__,
            "results": [],
        }
    rows = _rows_from_loader_payload(raw_rows)
    if rows is None:
        return {
            "status": "earnings_forecast_unavailable",
            "source": EARNINGS_FORECAST_SOURCE,
            "report_period": report_period,
            "error_type": "InvalidProviderPayload",
            "results": [],
        }

    effective_actual_loader = actual_loader or _load_actual_earnings
    try:
        raw_actual_rows = effective_actual_loader(actual_report_period)
    except Exception as exc:
        return {
            "status": "earnings_actual_unavailable",
            "source": EARNINGS_FORECAST_SOURCE,
            "actual_source": EARNINGS_ACTUAL_SOURCE,
            "report_period": report_period,
            "actual_report_period": actual_report_period,
            "error_type": type(exc).__name__,
            "results": [],
        }
    actual_rows = _rows_from_loader_payload(raw_actual_rows)
    if actual_rows is None:
        return {
            "status": "earnings_actual_unavailable",
            "source": EARNINGS_FORECAST_SOURCE,
            "actual_source": EARNINGS_ACTUAL_SOURCE,
            "report_period": report_period,
            "actual_report_period": actual_report_period,
            "error_type": "InvalidProviderPayload",
            "results": [],
        }

    rows_by_code: Dict[str, List[Dict[str, Any]]] = {
        code: [] for code in normalized_codes
    }
    for row in rows:
        code = _normalized_code(row.get("股票代码"))
        if code in rows_by_code:
            rows_by_code[code].append(row)

    actual_rows_by_code: Dict[str, List[Dict[str, Any]]] = {
        code: [] for code in normalized_codes
    }
    for row in actual_rows:
        code = _normalized_code(row.get("股票代码"))
        if code in actual_rows_by_code:
            actual_rows_by_code[code].append(row)

    results: List[Dict[str, Any]] = []
    for code in normalized_codes:
        result = _candidate_result(code, rows_by_code[code])
        latest_actual = _candidate_actual_result(
            actual_report_period,
            actual_rows_by_code[code],
            benchmark_trade_date=benchmark_date,
        )
        result["latest_actual"] = latest_actual
        result["blocks_new_position"] = earnings_result_blocks_new_position(
            forecast_status=result["status"],
            evidence=result["evidence"],
            latest_actual=latest_actual,
        )
        results.append(result)
    blocked_codes = [
        item["code"] for item in results if item["blocks_new_position"]
    ]
    selected_codes = [
        item["code"] for item in results if not item["blocks_new_position"]
    ]
    status_counts = Counter(item["status"] for item in results)
    actual_status_counts = Counter(
        item["latest_actual"]["status"] for item in results
    )
    return {
        "status": "ok",
        "source": EARNINGS_FORECAST_SOURCE,
        "actual_source": EARNINGS_ACTUAL_SOURCE,
        "report_period": report_period,
        "actual_report_period": actual_report_period,
        "screened_count": len(results),
        "blocked_count": len(blocked_codes),
        "selected_count": len(selected_codes),
        "blocked_codes": blocked_codes,
        "selected_codes": selected_codes,
        "status_counts": dict(sorted(status_counts.items())),
        "actual_status_counts": dict(sorted(actual_status_counts.items())),
        "results": results,
    }
