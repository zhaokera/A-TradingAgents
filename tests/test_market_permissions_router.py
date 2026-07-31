from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers import holdings as holdings_router
from app.routers.auth_db import get_current_user


@pytest.fixture
def app_client(monkeypatch):
    service = AsyncMock()
    candidate_service = AsyncMock()
    service.get.return_value = {
        "market_permissions": {
            "chi_next_market": {
                "verified": True,
                "tradable": False,
                "eligible": False,
                "reason_code": "permission_denied",
                "source": "user_confirmed",
            }
        }
    }
    service.update.return_value = {
        "updated_permission": {
            "permission_key": "chi_next_market",
            "verified": True,
            "tradable": False,
            "eligible": False,
            "reason_code": "permission_denied",
            "source": "user_confirmed",
        }
    }
    candidate_service.reconcile_user_governance.return_value = {
        "status": "reconciled",
        "active_run_count": 1,
        "excluded_count": 1,
        "auto_favorites": {"removed_codes": ["300450"]},
    }
    monkeypatch.setattr(
        holdings_router,
        "market_permission_service",
        service,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_router,
        "ai_candidate_service",
        candidate_service,
        raising=False,
    )
    app = FastAPI()
    app.include_router(holdings_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "owner-1",
        "username": "admin",
    }
    return TestClient(app), service, candidate_service


def test_get_market_permissions_uses_authenticated_owner(app_client):
    client, service, _candidate_service = app_client

    response = client.get(
        "/api/holdings/market-permissions",
        params={"user_id": "attacker"},
    )

    assert response.status_code == 200
    service.get.assert_awaited_once_with("owner-1")
    permission = response.json()["data"]["market_permissions"][
        "chi_next_market"
    ]
    assert permission["reason_code"] == "permission_denied"


def test_update_market_permission_uses_authenticated_owner(app_client):
    client, service, candidate_service = app_client

    response = client.patch(
        "/api/holdings/market-permissions/chi_next_market",
        params={"user_id": "attacker"},
        json={"state": "denied"},
    )

    assert response.status_code == 200
    service.update.assert_awaited_once_with(
        "owner-1",
        username="admin",
        permission_key="chi_next_market",
        state="denied",
    )
    candidate_service.reconcile_user_governance.assert_awaited_once_with(
        "owner-1"
    )
    assert response.json()["data"]["updated_permission"]["source"] == (
        "user_confirmed"
    )
    assert response.json()["data"]["governance_reconciliation"][
        "excluded_count"
    ] == 1


def test_market_permission_endpoint_rejects_invalid_key(app_client):
    client, service, _candidate_service = app_client
    service.update.side_effect = ValueError("unsupported market permission")

    response = client.patch(
        "/api/holdings/market-permissions/main_board",
        json={"state": "denied"},
    )

    assert response.status_code == 422


def test_market_permission_endpoint_requires_authentication():
    app = FastAPI()
    app.include_router(holdings_router.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/holdings/market-permissions")

    assert response.status_code == 401
