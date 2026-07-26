"""Fail-closed A-share session and actionable quote policy."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from app.services.a_share_calendar_service import AShareCalendarService


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
LIVE_PHASES = frozenset({"live_am", "live_pm"})
QUOTE_MAX_AGE_SECONDS = 90


def _as_shanghai_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed.astimezone(SHANGHAI_TIMEZONE)


def _trading_day_phase(local_now: datetime) -> str:
    current = local_now.time().replace(tzinfo=None)
    if current < time(9, 30):
        return "pre_open"
    if current < time(11, 30):
        return "live_am"
    if current < time(13, 0):
        return "midday_break"
    if current < time(15, 0):
        return "live_pm"
    return "post_close"


class MarketSessionPolicyService:
    def __init__(
        self,
        *,
        calendar: Optional[AShareCalendarService] = None,
        quote_max_age_seconds: int = QUOTE_MAX_AGE_SECONDS,
    ) -> None:
        self.calendar = calendar or AShareCalendarService()
        self.quote_max_age_seconds = quote_max_age_seconds

    async def classify(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        local_now = _as_shanghai_datetime(now or datetime.now(SHANGHAI_TIMEZONE))
        if local_now is None:
            raise ValueError("now must be a datetime")

        try:
            calendar = await self.calendar.is_trading_day(local_now.date())
        except Exception as exc:
            calendar = {
                "date": local_now.date().isoformat(),
                "is_trading_day": False,
                "status": "degraded",
                "source": "calendar_unavailable",
                "verified_at": None,
                "authoritative": False,
                "reason": str(exc)[:240],
            }
        authoritative = calendar.get("authoritative") is True
        is_trading_day = bool(calendar.get("is_trading_day"))
        if not authoritative:
            phase = "calendar_unknown"
        elif not is_trading_day:
            phase = "closed_day"
        else:
            phase = _trading_day_phase(local_now)

        buy_now_allowed = phase in LIVE_PHASES
        return {
            "phase": phase,
            "is_trading_day": is_trading_day,
            "buy_now_allowed": buy_now_allowed,
            "quote_freshness_required_seconds": (
                self.quote_max_age_seconds if buy_now_allowed else 0
            ),
            "timezone": "Asia/Shanghai",
            "classified_at": local_now.isoformat(),
            "calendar_source": calendar.get("source"),
            "calendar_verified_at": calendar.get("verified_at"),
            "calendar_authoritative": authoritative,
            "calendar": dict(calendar),
        }

    async def quote_status(
        self,
        quote: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
        session: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if session is not None and now is None:
            local_now = _as_shanghai_datetime(session.get("classified_at"))
        else:
            local_now = _as_shanghai_datetime(now or datetime.now(SHANGHAI_TIMEZONE))
        if local_now is None:
            raise ValueError("now must be a datetime")

        classification = (
            dict(session)
            if session is not None
            else await self.classify(now=local_now)
        )
        phase = str(classification.get("phase") or "calendar_unknown")
        source = str(quote.get("source") or "unknown").strip().lower()
        trade_at = _as_shanghai_datetime(quote.get("trade_at"))
        age_seconds = (local_now - trade_at).total_seconds() if trade_at else None
        serialized_age = (
            int(age_seconds)
            if age_seconds is not None and age_seconds.is_integer()
            else age_seconds
        )
        result = {
            "actionable": False,
            "status": "missing_trade_at",
            "source": source,
            "phase": phase,
            "trade_at": trade_at.isoformat(timespec="seconds") if trade_at else None,
            "trade_date": trade_at.date().isoformat() if trade_at else None,
            "age_seconds": serialized_age,
            "max_age_seconds": self.quote_max_age_seconds,
        }

        if phase == "calendar_unknown":
            return {**result, "status": "calendar_unknown"}
        if phase == "post_close":
            is_final_close = bool(
                source == "tencent"
                and trade_at is not None
                and trade_at.date() == local_now.date()
                and trade_at.time().replace(tzinfo=None) >= time(15, 0)
                and age_seconds is not None
                and age_seconds >= 0
            )
            if is_final_close:
                return {**result, "status": "final_close_observation"}
            return {**result, "status": "not_live_session"}
        if phase not in LIVE_PHASES:
            return {**result, "status": "not_live_session"}
        if source != "tencent":
            return {**result, "status": "unsupported_source"}
        if trade_at is None:
            return result
        if trade_at.date() != local_now.date():
            return {**result, "status": "wrong_trade_date"}
        if age_seconds is not None and age_seconds < 0:
            return {**result, "status": "future_trade_at"}
        if age_seconds is not None and age_seconds > self.quote_max_age_seconds:
            return {**result, "status": "stale_trade_at"}
        return {**result, "actionable": True, "status": "fresh"}


market_session_policy_service = MarketSessionPolicyService()
