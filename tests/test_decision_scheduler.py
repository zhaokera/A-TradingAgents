from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.database import create_database_indexes
from app.services.decision_scheduler_service import register_decision_scheduler_jobs


class FakeCollection:
    def __init__(self):
        self.indexes = []

    async def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))


class FakeDB(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = FakeCollection()
        return super().__getitem__(key)


def _index(collection, name):
    return next(kwargs for _keys, kwargs in collection.indexes if kwargs.get("name") == name)


def test_decision_runtime_flags_are_independent_and_bounded(monkeypatch):
    monkeypatch.setenv("DECISION_REFRESH_ENABLED", "false")
    monkeypatch.setenv("DECISION_TRACKING_ENABLED", "true")
    monkeypatch.setenv("DECISION_TRACKING_MAX_SYMBOLS", "25")

    configured = Settings(_env_file=None)

    assert configured.DECISION_REFRESH_ENABLED is False
    assert configured.DECISION_TRACKING_ENABLED is True
    assert configured.DECISION_TRACKING_MAX_SYMBOLS == 25


def test_codex_decision_authority_and_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("DECISION_AUTHORITY_MODE", "codex_validated")
    monkeypatch.setenv("MARKET_RED_BLOCKS_NEW_POSITIONS", "true")
    monkeypatch.setenv("CODEX_DECISION_MAX_NEW_POSITIONS", "3")
    monkeypatch.setenv("CODEX_DECISION_PRIMARY_POSITION_COUNT", "1")
    monkeypatch.setenv("CODEX_DECISION_VALIDATION_TTL_SECONDS", "75")

    configured = Settings(_env_file=None)

    assert configured.DECISION_AUTHORITY_MODE == "codex_validated"
    assert configured.MARKET_RED_BLOCKS_NEW_POSITIONS is True
    assert configured.CODEX_DECISION_MAX_NEW_POSITIONS == 3
    assert configured.CODEX_DECISION_PRIMARY_POSITION_COUNT == 1
    assert configured.CODEX_DECISION_VALIDATION_TTL_SECONDS == 75


@pytest.mark.asyncio
async def test_decision_indexes_enforce_append_only_identities():
    db = FakeDB()

    await create_database_indexes(db)

    assert _index(db["daily_decisions"], "uq_daily_decision_revision")["unique"] is True
    assert _index(db["daily_decisions"], "uq_daily_decision_material")["unique"] is True
    assert _index(db["decision_plans"], "uq_decision_plan_id")["unique"] is True
    assert _index(db["decision_outcomes"], "uq_decision_outcome_sequence")["unique"] is True
    assert _index(db["decision_minute_bars"], "uq_decision_minute_bar")["unique"] is True
    assert _index(db["stock_company_profiles"], "stock_company_profiles_code_unique")["unique"] is True
    assert _index(db["decision_calibration_versions"], "uq_decision_calibration_proposal")["unique"] is True
    assert _index(db["decision_research_packets"], "uq_decision_research_packet_id")["unique"] is True
    assert _index(db["decision_research_packets"], "uq_decision_research_baseline")["unique"] is True
    assert _index(db["codex_decision_proposals"], "uq_codex_decision_proposal_id")["unique"] is True
    assert _index(db["codex_decision_proposals"], "uq_codex_decision_proposal_hash")["unique"] is True
    assert _index(db["decision_validations"], "uq_decision_validation_id")["unique"] is True
    assert _index(db["decision_confirmations"], "uq_decision_confirmation_id")["unique"] is True


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


class FakeRuntime:
    def __init__(self, *, ready=True):
        self.ready = ready
        self.readiness_calls = 0

        class Poller:
            async def poll_once(self):
                return None

        self.tracking_poller = Poller()

    async def refresh_active_users(self):
        return None

    async def tracking_readiness(self):
        self.readiness_calls += 1
        return {"ready": self.ready, "reason": None if self.ready else "not_ready"}


@pytest.mark.asyncio
async def test_scheduler_registers_refresh_first_and_bounded_tracking_job():
    scheduler = FakeScheduler()
    runtime = FakeRuntime()
    config = type(
        "Config",
        (),
        {
            "DECISION_REFRESH_ENABLED": True,
            "DECISION_TRACKING_ENABLED": True,
            "TIMEZONE": "Asia/Shanghai",
        },
    )()

    result = await register_decision_scheduler_jobs(
        scheduler, config=config, runtime=runtime
    )

    assert [job["id"] for job in scheduler.jobs] == [
        "decision_daily_refresh",
        "decision_tracking_poller",
    ]
    tracking = scheduler.jobs[1]
    assert tracking["max_instances"] == 1
    assert tracking["coalesce"] is True
    assert tracking["trigger"].interval.total_seconds() == 15
    assert result["tracking_readiness"]["ready"] is True


@pytest.mark.asyncio
async def test_disabled_or_unready_tracking_never_leaves_a_scheduler_job():
    scheduler = FakeScheduler()
    runtime = FakeRuntime(ready=False)
    config = type(
        "Config",
        (),
        {
            "DECISION_REFRESH_ENABLED": False,
            "DECISION_TRACKING_ENABLED": True,
            "TIMEZONE": "Asia/Shanghai",
        },
    )()

    result = await register_decision_scheduler_jobs(
        scheduler, config=config, runtime=runtime
    )

    assert scheduler.jobs == []
    assert result["tracking_readiness"]["reason"] == "not_ready"

    runtime = FakeRuntime()
    config.DECISION_TRACKING_ENABLED = False
    await register_decision_scheduler_jobs(scheduler, config=config, runtime=runtime)
    assert scheduler.jobs == []
    assert runtime.readiness_calls == 0
