from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers import decision as decision_router
from app.routers.auth_db import get_current_user
from app.services.daily_decision_service import DecisionPersistenceError


@pytest.fixture
def app_client(monkeypatch):
    service = AsyncMock()
    service.today.return_value = {"decision_id": "d1"}
    service.history.return_value = [{"decision_id": "d1"}]
    monkeypatch.setattr(decision_router, "daily_decision_service", service)
    app = FastAPI()
    app.include_router(decision_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "owner-1",
        "username": "admin",
    }
    return TestClient(app), service


def test_today_uses_authenticated_owner_and_refresh_flag(app_client):
    client, service = app_client

    response = client.get(
        "/api/decision/today",
        params={"refresh": "false", "user_id": "attacker"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["decision_id"] == "d1"
    service.today.assert_awaited_once_with("owner-1", refresh=False)


def test_history_uses_authenticated_owner_and_bounded_limit(app_client):
    client, service = app_client

    response = client.get("/api/decision/history", params={"limit": 7})

    assert response.status_code == 200
    service.history.assert_awaited_once_with("owner-1", limit=7)
    assert client.get("/api/decision/history", params={"limit": 101}).status_code == 422


def test_today_returns_503_when_snapshot_cannot_be_audited(app_client):
    client, service = app_client
    service.today.side_effect = DecisionPersistenceError("mongo unavailable")

    response = client.get("/api/decision/today")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "decision_persistence_unavailable"


def test_history_returns_structured_503_when_mongo_is_unavailable(app_client):
    client, service = app_client
    service.history.side_effect = DecisionPersistenceError("mongo unavailable")

    response = client.get("/api/decision/history")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "decision_history_unavailable"
