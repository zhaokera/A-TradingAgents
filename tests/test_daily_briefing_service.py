from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_briefing_service import DailyBriefingService


class _Cursor:
    async def to_list(self, *, length):
        assert length == 100
        return []


class _HoldingsCollection:
    def find(self, *_args, **_kwargs):
        return _Cursor()


class _Database:
    def __getitem__(self, name):
        assert name == "user_holdings"
        return _HoldingsCollection()


@pytest.mark.asyncio
async def test_briefing_exposes_full_rolling_pool_summary(monkeypatch):
    candidates = [
        {
            "code": f"{600000 + index:06d}",
            "name": f"候选{index}",
            "rolling_pool_state": "current" if index < 18 else "aging",
            "research_tier": "deep" if index < 15 else "structured",
            "rank": index + 1,
            "rank_score": 100 - index,
            "objective_tier": "core",
            "actionability": "watch_trigger",
            "execution_actionable": False,
            "structured_review": {"hard_risk_status": "clear"},
        }
        for index in range(20)
    ]
    monkeypatch.setattr(
        "app.services.daily_briefing_service.ai_candidate_service.latest",
        AsyncMock(
            return_value={
                "run_id": "run-100",
                "generated_at": "2026-08-25T01:40:00+00:00",
                "candidates": candidates,
                "account": {"total_assets": 100000, "available_cash": 100000},
                "market": {},
                "portfolio_plan": {},
                "rolling_pool": {
                    "capacity": 100,
                    "total_count": 20,
                    "current_count": 18,
                    "aging_count": 2,
                    "deep_count": 15,
                    "structured_count": 5,
                },
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.daily_briefing_service.get_mongo_db", lambda: _Database()
    )
    monkeypatch.setattr(
        "app.services.daily_briefing_service.favorites_service.get_user_favorites",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.daily_briefing_service.get_notifications_service",
        lambda: SimpleNamespace(unread_count=AsyncMock(return_value=0)),
    )
    premarket = SimpleNamespace(build=AsyncMock(return_value={"status": "ok"}))

    result = await DailyBriefingService(premarket_service=premarket).build(
        "user-1", refresh=False
    )

    rolling_pool = result["candidate_run"]["rolling_pool"]
    assert rolling_pool["capacity"] == 100
    assert rolling_pool["total_count"] == 20
    assert rolling_pool["current_count"] == 18
    assert rolling_pool["aging_count"] == 2
    assert len(rolling_pool["candidates"]) == 20
    assert rolling_pool["candidates"][15]["research_tier"] == "structured"

