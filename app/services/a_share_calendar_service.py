"""A-share trading-day calendar with a cached official-exchange-derived dataset."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.database import get_mongo_db


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
CALENDAR_SOURCE = "akshare_trade_calendar"


class AShareCalendarService:
    def __init__(self, *, cache_max_age_hours: Optional[int] = None) -> None:
        self.db = None
        self.cache_max_age_hours = (
            cache_max_age_hours
            if cache_max_age_hours is not None
            else get_settings().A_SHARE_CALENDAR_CACHE_MAX_AGE_HOURS
        )

    async def _get_db(self):
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    @staticmethod
    def _fetch_dates() -> list[str]:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        if frame is None or frame.empty:
            return []
        column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
        return sorted({str(value)[:10] for value in frame[column].tolist() if value})

    @staticmethod
    def _as_utc_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
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
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _as_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return None

    def _cached_result(
        self,
        cached: Dict[str, Any],
        *,
        key: str,
        now: datetime,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        verified_at = self._as_utc_datetime(cached.get("updated_at"))
        age = now - verified_at if verified_at is not None else None
        source = cached.get("source") or CALENDAR_SOURCE
        cached_trading_day = cached.get("is_trading_day")
        coverage_start = self._as_date(cached.get("coverage_start"))
        coverage_end = self._as_date(cached.get("coverage_end"))
        target = self._as_date(key)
        authoritative = bool(
            source == CALENDAR_SOURCE
            and isinstance(cached_trading_day, bool)
            and verified_at is not None
            and timedelta(0) <= age <= timedelta(hours=self.cache_max_age_hours)
            and coverage_start is not None
            and coverage_end is not None
            and target is not None
            and coverage_start <= target <= coverage_end
        )
        result = {
            "date": key,
            "is_trading_day": (
                cached_trading_day if isinstance(cached_trading_day, bool) else False
            ),
            "status": "verified" if authoritative else "stale",
            "source": source,
            "verified_at": verified_at.isoformat() if verified_at else None,
            "authoritative": authoritative,
            "coverage_start": coverage_start.isoformat() if coverage_start else None,
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
        }
        if reason:
            result["reason"] = reason[:240]
        return result

    async def is_trading_day(self, value: Optional[date] = None) -> Dict[str, Any]:
        target = value or datetime.now(SHANGHAI_TIMEZONE).date()
        db = await self._get_db()
        key = target.isoformat()
        now = datetime.now(timezone.utc)
        cached = await db["a_share_trade_calendar"].find_one({"_id": key})
        if cached:
            cached_result = self._cached_result(cached, key=key, now=now)
            if cached_result["authoritative"]:
                return cached_result
        try:
            dates = await asyncio.wait_for(asyncio.to_thread(self._fetch_dates), timeout=30)
            if not dates:
                raise RuntimeError("empty A-share trade calendar")
            normalized_dates = sorted(
                {
                    parsed.isoformat()
                    for item in dates
                    if (parsed := self._as_date(item)) is not None
                }
            )
            if not normalized_dates:
                raise RuntimeError("empty A-share trade calendar")

            coverage_start = date.fromisoformat(normalized_dates[0])
            coverage_end = date.fromisoformat(normalized_dates[-1])
            if not coverage_start <= target <= coverage_end:
                if cached:
                    result = self._cached_result(
                        cached,
                        key=key,
                        now=now,
                        reason="target_outside_provider_range",
                    )
                else:
                    result = {
                        "date": key,
                        "is_trading_day": target.weekday() < 5,
                        "status": "degraded",
                        "source": "weekday_fallback",
                        "verified_at": None,
                        "authoritative": False,
                        "reason": "target_outside_provider_range",
                    }
                return {
                    **result,
                    "coverage_start": coverage_start.isoformat(),
                    "coverage_end": coverage_end.isoformat(),
                }

            operations = []
            from pymongo import UpdateOne

            date_set = set(normalized_dates)
            persist_start = max(date(target.year, 1, 1), coverage_start)
            persist_end = min(date(target.year, 12, 31), coverage_end)
            current = persist_start

            while current <= persist_end:
                current_key = current.isoformat()
                operations.append(
                    UpdateOne(
                        {"_id": current_key},
                        {
                            "$set": {
                                "is_trading_day": current_key in date_set,
                                "source": CALENDAR_SOURCE,
                                "updated_at": now,
                                "coverage_start": coverage_start.isoformat(),
                                "coverage_end": coverage_end.isoformat(),
                            }
                        },
                        upsert=True,
                    )
                )
                current += timedelta(days=1)
            if operations:
                await db["a_share_trade_calendar"].bulk_write(operations, ordered=False)
            return {
                "date": key,
                "is_trading_day": key in date_set,
                "status": "verified",
                "source": CALENDAR_SOURCE,
                "verified_at": now.isoformat(),
                "authoritative": True,
                "coverage_start": coverage_start.isoformat(),
                "coverage_end": coverage_end.isoformat(),
            }
        except Exception as exc:
            if cached:
                return self._cached_result(cached, key=key, now=now, reason=str(exc))
            return {
                "date": key,
                "is_trading_day": target.weekday() < 5,
                "status": "degraded",
                "source": "weekday_fallback",
                "verified_at": None,
                "authoritative": False,
                "reason": str(exc)[:240],
            }


a_share_calendar_service = AShareCalendarService()
