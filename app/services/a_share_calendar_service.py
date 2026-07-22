"""A-share trading-day calendar with a cached official-exchange-derived dataset."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.config import get_settings
from app.core.database import get_mongo_db


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
        source = cached.get("source") or "akshare_trade_calendar"
        authoritative = bool(
            source == "akshare_trade_calendar"
            and verified_at is not None
            and timedelta(0) <= age <= timedelta(hours=self.cache_max_age_hours)
        )
        result = {
            "date": key,
            "is_trading_day": bool(cached.get("is_trading_day")),
            "status": "verified" if authoritative else "stale",
            "source": source,
            "verified_at": verified_at.isoformat() if verified_at else None,
            "authoritative": authoritative,
        }
        if reason:
            result["reason"] = reason[:240]
        return result

    async def is_trading_day(self, value: Optional[date] = None) -> Dict[str, Any]:
        target = value or datetime.now().date()
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
            operations = []
            from pymongo import UpdateOne

            date_set = set(dates)
            year_start = date(target.year, 1, 1)
            year_end = date(target.year, 12, 31)
            current = year_start

            while current <= year_end:
                current_key = current.isoformat()
                operations.append(
                    UpdateOne(
                        {"_id": current_key},
                        {
                            "$set": {
                                "is_trading_day": current_key in date_set,
                                "source": "akshare_trade_calendar",
                                "updated_at": now,
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
                "source": "akshare_trade_calendar",
                "verified_at": now.isoformat(),
                "authoritative": True,
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
