from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scheduler_service import SchedulerService


def _scheduler():
    job = SimpleNamespace(name="AI候选每日全市场重分析")
    scheduler = MagicMock()
    scheduler.get_job.return_value = job
    return scheduler


@pytest.mark.asyncio
async def test_startup_catchup_schedules_missed_daily_job_with_audit():
    scheduler = _scheduler()
    executions = SimpleNamespace(find_one=AsyncMock(return_value=None))
    history = SimpleNamespace(insert_one=AsyncMock())
    service = SchedulerService(scheduler)
    service.db = SimpleNamespace(
        scheduler_executions=executions,
        scheduler_history=history,
    )
    now = datetime(2026, 7, 29, 4, 15, tzinfo=timezone.utc)

    result = await service.schedule_daily_catchup_if_missed(
        "ai_candidate_daily_research",
        hour=9,
        minute=40,
        now=now,
    )

    assert result["scheduled"] is True
    assert result["reason"] == "missed_daily_run_catchup"
    scheduler.modify_job.assert_called_once()
    call = scheduler.modify_job.call_args
    assert call.args == ("ai_candidate_daily_research",)
    assert call.kwargs["next_run_time"] == now
    history.insert_one.assert_awaited_once()
    assert scheduler.add_listener.call_count == 3


@pytest.mark.asyncio
async def test_startup_catchup_does_not_duplicate_existing_daily_execution():
    scheduler = _scheduler()
    executions = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "job_id": "ai_candidate_daily_research",
                "status": "success",
            }
        )
    )
    service = SchedulerService(scheduler)
    service.db = SimpleNamespace(
        scheduler_executions=executions,
        scheduler_history=SimpleNamespace(insert_one=AsyncMock()),
    )

    result = await service.schedule_daily_catchup_if_missed(
        "ai_candidate_daily_research",
        hour=9,
        minute=40,
        now=datetime(2026, 7, 29, 4, 15, tzinfo=timezone.utc),
    )

    assert result == {
        "scheduled": False,
        "reason": "daily_execution_already_recorded",
        "status": "success",
    }
    scheduler.modify_job.assert_not_called()
