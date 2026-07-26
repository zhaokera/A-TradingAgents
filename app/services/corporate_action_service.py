"""Upcoming corporate-action metadata for A-share risk checks."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _date_value(value: Any) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nat", "nan", "none", "<na>"}:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (AttributeError, TypeError, ValueError):
            return None
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> Optional[float]:
    if value is None or not str(value).strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _local_datetime(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(CN_MARKET_TIMEZONE)
    if current.tzinfo is None:
        return current.replace(tzinfo=CN_MARKET_TIMEZONE)
    return current.astimezone(CN_MARKET_TIMEZONE)


def _weekday_sessions_until(start_date: date, end_date: date) -> int:
    sessions = 0
    cursor = start_date + timedelta(days=1)
    while cursor <= end_date:
        if cursor.weekday() < 5:
            sessions += 1
        cursor += timedelta(days=1)
    return sessions


def _normalize_action(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ex_date = _date_value(row.get("除权日"))
    if ex_date is None:
        return None
    announcement_date = _date_value(row.get("实施方案公告日期"))
    record_date = _date_value(row.get("股权登记日"))
    payment_date = _date_value(row.get("派息日"))
    cash_per_ten_shares = _positive_float(row.get("派息比例"))
    return {
        "announcement_date": announcement_date.isoformat() if announcement_date else None,
        "action_type": str(row.get("分红类型") or "").strip() or None,
        "record_date": record_date.isoformat() if record_date else None,
        "ex_date": ex_date.isoformat(),
        "payment_date": payment_date.isoformat() if payment_date else None,
        "cash_dividend_per_share": (
            round(cash_per_ten_shares / 10, 6) if cash_per_ten_shares is not None else None
        ),
        "description": str(row.get("实施方案分红说明") or "").strip() or None,
        "report_period": str(row.get("报告时间") or "").strip() or None,
    }


def assess_cn_dividend_actions(
    rows: Iterable[Dict[str, Any]],
    *,
    as_of: Optional[datetime] = None,
    horizon_sessions: int = 2,
    warning_sessions: int = 5,
) -> Dict[str, Any]:
    local_now = _local_datetime(as_of)
    normalized = [action for row in rows for action in [_normalize_action(row)] if action is not None]
    normalized.sort(key=lambda item: item["ex_date"])
    upcoming = [item for item in normalized if date.fromisoformat(item["ex_date"]) >= local_now.date()]
    nearest = upcoming[0] if upcoming else None
    if nearest is None:
        return {
            "status": "no_upcoming_corporate_action",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": None,
            "nearest_action": None,
            "is_reference_only": True,
        }

    ex_date = date.fromisoformat(nearest["ex_date"])
    sessions_until_ex_date = _weekday_sessions_until(local_now.date(), ex_date)
    if ex_date == local_now.date():
        status = "corporate_action_today"
        blocks_new_position = True
    elif sessions_until_ex_date <= horizon_sessions:
        status = "corporate_action_within_horizon"
        blocks_new_position = True
    elif sessions_until_ex_date <= warning_sessions:
        status = "upcoming_corporate_action"
        blocks_new_position = False
    else:
        status = "scheduled_corporate_action"
        blocks_new_position = False

    return {
        "status": status,
        "blocks_new_position": blocks_new_position,
        "price_plan_adjustment_required": blocks_new_position,
        "sessions_until_ex_date": sessions_until_ex_date,
        "nearest_action": nearest,
        "is_reference_only": True,
    }


def fetch_cn_dividend_calendar_sync(
    code: str,
    *,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    normalized_code = re.sub(r"^(SH|SZ|BJ)", "", str(code or "").strip().upper())
    if not re.fullmatch(r"\d{6}", normalized_code):
        return {
            "ok": False,
            "source": "cninfo_via_akshare",
            "code": normalized_code or None,
            "status": "invalid_cn_code",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": None,
            "nearest_action": None,
            "reason": "expected a six-digit A-share code",
            "is_reference_only": True,
        }

    last_error: Optional[Exception] = None
    assessment: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        try:
            import akshare as ak

            frame = ak.stock_dividend_cninfo(symbol=normalized_code)
            rows = frame.to_dict("records")
            assessment = assess_cn_dividend_actions(rows, as_of=as_of)
            break
        except (KeyError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                continue
            break
        except Exception as exc:
            last_error = exc
            break
    if assessment is None:
        assert last_error is not None
        return {
            "ok": False,
            "source": "cninfo_via_akshare",
            "code": normalized_code,
            "status": "corporate_action_unavailable",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": None,
            "nearest_action": None,
            "reason": str(last_error),
            "is_reference_only": True,
        }

    return {
        "ok": True,
        "source": "cninfo_via_akshare",
        "code": normalized_code,
        **assessment,
    }
