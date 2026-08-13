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


def test_expected_off_session_tracking_result_is_not_audited():
    assert SchedulerService._should_skip_execution_audit(
        "decision_tracking_poller",
        {"status": "disabled_market_phase", "phase": "post_close"},
    )
    assert not SchedulerService._should_skip_execution_audit(
        "decision_tracking_poller",
        {"status": "disabled_session_unavailable"},
    )
    assert not SchedulerService._should_skip_execution_audit(
        "quotes_ingestion_service",
        {"status": "disabled_market_phase"},
    )


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


@pytest.mark.asyncio
async def test_startup_catchup_does_not_duplicate_dispatched_daily_execution():
    scheduler = _scheduler()
    executions = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "job_id": "ai_candidate_daily_research",
                "status": "dispatched",
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
        now=datetime(2026, 8, 13, 4, 15, tzinfo=timezone.utc),
    )

    assert result["scheduled"] is False
    assert result["status"] == "dispatched"
    scheduler.modify_job.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_execution_persists_dispatch_result_as_structured_audit():
    scheduler = _scheduler()
    executions = SimpleNamespace(
        find_one=AsyncMock(return_value=None),
        insert_one=AsyncMock(),
    )
    service = SchedulerService(scheduler)
    service.db = SimpleNamespace(scheduler_executions=executions)
    result = {
        "dispatch_status": "candidate_jobs_started",
        "research_completed": False,
        "active_user_count": 1,
        "started_count": 1,
        "failed_count": 0,
        "jobs": [
            {
                "user_id": "owner-1",
                "job_id": "6a7d2070014a33ad279387f4",
                "status": "queued",
            }
        ],
    }

    await service._record_job_execution(
        job_id="ai_candidate_daily_research",
        status="dispatched",
        scheduled_time=datetime(2026, 8, 13, 1, 40, tzinfo=timezone.utc),
        execution_time=0.02,
        return_value=str(result),
        return_data=result,
        progress=100,
    )

    stored = executions.insert_one.await_args.args[0]
    assert stored["status"] == "dispatched"
    assert stored["outcome_status"] == "candidate_jobs_started"
    assert stored["result"]["research_completed"] is False
    assert stored["result"]["jobs"][0]["job_id"] == (
        "6a7d2070014a33ad279387f4"
    )


@pytest.mark.asyncio
async def test_scheduler_history_links_candidate_dispatch_to_failed_child_job():
    execution = {
        "_id": "execution-1",
        "job_id": "ai_candidate_daily_research",
        "status": "success",
        "scheduled_time": datetime(2026, 8, 13, 9, 40),
        "timestamp": datetime(2026, 8, 13, 9, 40),
    }

    class Cursor:
        def sort(self, *_args):
            return self

        def skip(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def __aiter__(self):
            self._done = False
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return dict(execution)

    executions = SimpleNamespace(find=MagicMock(return_value=Cursor()))
    child_id = "6a7d2070014a33ad279387f4"
    child_jobs = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                sort=lambda *_args: SimpleNamespace(
                    to_list=AsyncMock(
                        return_value=[
                            {
                                "_id": child_id,
                                "user_id": "owner-1",
                                "status": "failed",
                                "created_at": datetime(2026, 8, 13, 1, 40),
                                "completed_at": datetime(2026, 8, 13, 1, 40, 21),
                                "error": {
                                    "code": "candidate_discovery_unavailable",
                                    "stage": "candidate_discovery",
                                },
                            }
                        ]
                    )
                )
            )
        )
    )
    service = SchedulerService(_scheduler())
    service.db = SimpleNamespace(
        scheduler_executions=executions,
        ai_candidate_jobs=child_jobs,
    )

    rows = await service.get_job_executions(
        job_id="ai_candidate_daily_research",
        limit=10,
    )

    assert rows[0]["dispatch_status"] == "dispatched"
    assert rows[0]["research_status"] == "failed"
    assert rows[0]["candidate_jobs"][0]["job_id"] == child_id
    assert rows[0]["candidate_jobs"][0]["stage"] == "candidate_discovery"
    assert rows[0]["candidate_jobs"][0]["error"]["code"] == (
        "candidate_discovery_unavailable"
    )
