"""Bounded scheduler runtime for daily decisions and shadow observations."""

from __future__ import annotations

import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from apscheduler.triggers.interval import IntervalTrigger

from app.core.database import get_mongo_db
from app.services.daily_decision_service import daily_decision_service
from app.services.decision_tracking_service import TrackingPoller
from app.services.market_session_policy_service import market_session_policy_service
from app.services.tencent_quote_service import get_tencent_quote_service


logger = logging.getLogger(__name__)
LIVE_PHASES = frozenset({"live_am", "live_pm"})
REQUIRED_TRACKING_INDEXES = {
    "decision_plans": {"uq_decision_plan_id", "ix_decision_plan_active"},
    "decision_outcomes": {"uq_decision_outcome_sequence"},
    "decision_minute_bars": {"uq_decision_minute_bar"},
}


class DecisionSchedulerRuntime:
    """Own scheduler callbacks and fail-closed startup readiness checks."""

    def __init__(
        self,
        *,
        db: Any = None,
        decision_service: Any = daily_decision_service,
        market_session: Any = market_session_policy_service,
        tracking_poller: Any = None,
        max_symbols: int = 50,
    ) -> None:
        self._db = db
        self.decision_service = decision_service
        self.market_session = market_session
        self.max_symbols = int(max_symbols)
        self.tracking_poller = tracking_poller or TrackingPoller(
            db=db,
            quote_fetcher=get_tencent_quote_service(),
            market_session=market_session,
            max_symbols=self.max_symbols,
        )

    async def _get_db(self) -> Any:
        if self._db is None:
            self._db = get_mongo_db()
        if inspect.isawaitable(self._db):
            self._db = await self._db
        return self._db

    async def refresh_active_users(self) -> Dict[str, Any]:
        """Refresh audited decisions during live A-share phases only."""

        try:
            session = self.market_session.classify()
            if inspect.isawaitable(session):
                session = await session
        except Exception:
            return {
                "status": "disabled_session_unavailable",
                "phase": "calendar_unknown",
                "refreshed": 0,
            }
        phase = str((session or {}).get("phase") or "calendar_unknown")
        if phase not in LIVE_PHASES:
            return {"status": "disabled_market_phase", "phase": phase, "refreshed": 0}

        db = await self._get_db()
        cursor = db["users"].find({"is_active": {"$ne": False}}, {"_id": 1})
        users = await cursor.to_list(length=500)
        user_ids = sorted({str(row.get("_id") or "") for row in users if row.get("_id")})
        refreshed = 0
        failed = 0
        for user_id in user_ids:
            try:
                await self.decision_service.today(user_id, refresh=True)
                refreshed += 1
            except Exception:
                failed += 1
                logger.exception("Scheduled decision refresh failed: user=%s", user_id)
        return {
            "status": "refreshed",
            "phase": phase,
            "active_user_count": len(user_ids),
            "refreshed": refreshed,
            "failed": failed,
        }

    async def tracking_readiness(self) -> Dict[str, Any]:
        """Prove indexes, bounded symbols, and lock write access before scheduling."""

        if self.max_symbols <= 0:
            return {"ready": False, "reason": "invalid_symbol_limit"}
        try:
            db = await self._get_db()
            missing: Dict[str, list[str]] = {}
            for collection_name, required in REQUIRED_TRACKING_INDEXES.items():
                information = db[collection_name].index_information()
                if inspect.isawaitable(information):
                    information = await information
                names = set((information or {}).keys())
                absent = sorted(required - names)
                if absent:
                    missing[collection_name] = absent
            if missing:
                return {"ready": False, "reason": "tracking_indexes_missing", "missing": missing}

            symbols = self.tracking_poller._symbols()
            if inspect.isawaitable(symbols):
                symbols = await symbols
            symbol_count = len(symbols or [])
            if symbol_count > self.max_symbols:
                return {
                    "ready": False,
                    "reason": "tracking_symbol_limit_exceeded",
                    "symbol_count": symbol_count,
                    "max_symbols": self.max_symbols,
                }

            readiness_id = f"decision-tracking-readiness:{uuid.uuid4().hex}"
            now = datetime.now(timezone.utc)
            write = await db["job_locks"].update_one(
                {"_id": readiness_id},
                {"$set": {"kind": "readiness", "updated_at": now}},
                upsert=True,
            )
            if not (
                int(getattr(write, "matched_count", 0) or 0) > 0
                or getattr(write, "upserted_id", None) is not None
            ):
                return {"ready": False, "reason": "tracking_lock_write_unavailable"}
            delete = db["job_locks"].delete_one({"_id": readiness_id})
            if inspect.isawaitable(delete):
                await delete
            return {
                "ready": True,
                "reason": None,
                "symbol_count": symbol_count,
                "max_symbols": self.max_symbols,
            }
        except Exception as exc:
            logger.exception("Decision tracking readiness failed")
            return {
                "ready": False,
                "reason": "tracking_readiness_failed",
                "error_type": type(exc).__name__,
            }


async def register_decision_scheduler_jobs(
    scheduler: Any,
    *,
    config: Any,
    runtime: DecisionSchedulerRuntime,
) -> Dict[str, Any]:
    """Register independent refresh and tracking jobs in deterministic order."""

    added: list[str] = []
    if bool(config.DECISION_REFRESH_ENABLED):
        scheduler.add_job(
            runtime.refresh_active_users,
            IntervalTrigger(minutes=5, timezone=config.TIMEZONE),
            id="decision_daily_refresh",
            name="可审计每日决策刷新",
            max_instances=1,
            coalesce=True,
        )
        added.append("decision_daily_refresh")

    readiness: Mapping[str, Any] = {"ready": False, "reason": "tracking_disabled"}
    if bool(config.DECISION_TRACKING_ENABLED):
        readiness = await runtime.tracking_readiness()
        if readiness.get("ready") is True:
            scheduler.add_job(
                runtime.tracking_poller.poll_once,
                IntervalTrigger(seconds=15, timezone=config.TIMEZONE),
                id="decision_tracking_poller",
                name="决策影子交易腾讯行情跟踪",
                max_instances=1,
                coalesce=True,
            )
            added.append("decision_tracking_poller")
        else:
            logger.error("Decision tracking scheduler disabled: %s", dict(readiness))

    return {"added": added, "tracking_readiness": dict(readiness)}


__all__ = [
    "DecisionSchedulerRuntime",
    "REQUIRED_TRACKING_INDEXES",
    "register_decision_scheduler_jobs",
]
