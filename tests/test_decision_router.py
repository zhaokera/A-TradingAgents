from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers import decision as decision_router
from app.routers.auth_db import get_current_user
from app.services.daily_decision_service import DecisionPersistenceError
from app.services.decision_review_service import DecisionReviewError
from app.services.decision_workflow_errors import DecisionWorkflowError


@pytest.fixture
def app_client(monkeypatch):
    service = AsyncMock()
    service.today.return_value = {"decision_id": "d1"}
    service.history.return_value = [{"decision_id": "d1"}]
    review_service = AsyncMock()
    review_service.performance.return_value = {
        "metric_basis": "shadow_trade_v1",
        "overall": {"closed_count": 3},
    }
    monkeypatch.setattr(decision_router, "daily_decision_service", service)
    monkeypatch.setattr(decision_router, "decision_review_service", review_service)
    app = FastAPI()
    app.include_router(decision_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "owner-1",
        "username": "admin",
    }
    return TestClient(app), service, review_service


@pytest.fixture
def workflow_client(monkeypatch):
    baseline_service = AsyncMock()
    baseline_service.today.return_value = {
        "decision_id": "baseline-1",
        "authority": "software_baseline",
    }
    research_service = AsyncMock()
    research_service.today.return_value = {"research_packet_id": "research-1"}
    proposal_service = AsyncMock()
    proposal_service.submit.return_value = {
        "proposal": {"proposal_id": "proposal-1"},
        "validation": {"validation_id": "validation-1", "status": "valid"},
    }
    validation_service = AsyncMock()
    validation_service.validate.return_value = {
        "validation_id": "validation-2",
        "status": "valid",
    }
    confirmation_service = AsyncMock()
    confirmation_service.workspace.return_value = {
        "authority": "codex_validated",
        "is_final_decision": True,
    }
    confirmation_service.confirm.return_value = {
        "confirmation_id": "confirmation-1",
        "accepted": True,
        "execution_status": "not_executed",
    }
    monkeypatch.setattr(
        decision_router, "daily_decision_service", baseline_service
    )
    monkeypatch.setattr(
        decision_router, "decision_research_service", research_service
    )
    monkeypatch.setattr(
        decision_router, "decision_proposal_service", proposal_service
    )
    monkeypatch.setattr(
        decision_router, "decision_validation_service", validation_service
    )
    monkeypatch.setattr(
        decision_router, "decision_confirmation_service", confirmation_service
    )
    app = FastAPI()
    app.include_router(decision_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "owner-1",
        "username": "admin",
    }
    return TestClient(app), {
        "baseline": baseline_service,
        "research": research_service,
        "proposal": proposal_service,
        "validation": validation_service,
        "confirmation": confirmation_service,
    }


def _no_action_proposal():
    return {
        "research_packet_id": "research-1",
        "decision_scope": {
            "max_new_positions": 2,
            "primary_position_count": 1,
        },
        "selections": [],
        "portfolio_rationale": "当前没有满足条件的机会",
        "no_action_reason": "等待更完整的价格与风险信号",
    }


def test_today_uses_authenticated_owner_and_refresh_flag(app_client):
    client, service, _review_service = app_client

    response = client.get(
        "/api/decision/today",
        params={"refresh": "false", "user_id": "attacker"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["decision_id"] == "d1"
    service.today.assert_awaited_once_with("owner-1", refresh=False)


def test_history_uses_authenticated_owner_and_bounded_limit(app_client):
    client, service, _review_service = app_client

    response = client.get("/api/decision/history", params={"limit": 7})

    assert response.status_code == 200
    service.history.assert_awaited_once_with("owner-1", limit=7)
    assert client.get("/api/decision/history", params={"limit": 101}).status_code == 422


def test_today_returns_503_when_snapshot_cannot_be_audited(app_client):
    client, service, _review_service = app_client
    service.today.side_effect = DecisionPersistenceError("mongo unavailable")

    response = client.get("/api/decision/today")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "decision_persistence_unavailable"


def test_history_returns_structured_503_when_mongo_is_unavailable(app_client):
    client, service, _review_service = app_client
    service.history.side_effect = DecisionPersistenceError("mongo unavailable")

    response = client.get("/api/decision/history")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "decision_history_unavailable"


def test_performance_uses_authenticated_owner(app_client):
    client, _service, review_service = app_client

    response = client.get(
        "/api/decision/performance",
        params={"user_id": "attacker"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["metric_basis"] == "shadow_trade_v1"
    review_service.performance.assert_awaited_once_with("owner-1")


def test_performance_returns_structured_503_when_review_is_unavailable(app_client):
    client, _service, review_service = app_client
    review_service.performance.side_effect = DecisionReviewError("mongo unavailable")

    response = client.get("/api/decision/performance")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "decision_performance_unavailable"


def test_research_and_baseline_use_authenticated_owner(workflow_client):
    client, services = workflow_client

    research = client.get(
        "/api/decision/research/today",
        params={"refresh": "false", "user_id": "attacker"},
    )
    baseline = client.get(
        "/api/decision/baseline/today",
        params={"refresh": "true", "user_id": "attacker"},
    )

    assert research.status_code == 200
    assert baseline.status_code == 200
    services["research"].today.assert_awaited_once_with(
        "owner-1", refresh=False
    )
    services["baseline"].today.assert_awaited_once_with(
        "owner-1", refresh=True
    )


def test_submit_and_revalidate_proposal_use_authenticated_owner(workflow_client):
    client, services = workflow_client
    payload = _no_action_proposal()

    submitted = client.post(
        "/api/decision/proposals?user_id=attacker",
        json=payload,
    )
    revalidated = client.post(
        "/api/decision/proposals/proposal-1/validate",
        params={"refresh_quote": "true", "user_id": "attacker"},
    )

    assert submitted.status_code == 200
    assert submitted.json()["data"]["validation"]["status"] == "valid"
    submitted_model = services["proposal"].submit.await_args.args[1]
    assert submitted_model.research_packet_id == "research-1"
    services["proposal"].submit.assert_awaited_once()
    assert services["proposal"].submit.await_args.args[0] == "owner-1"
    services["validation"].validate.assert_awaited_once_with(
        "owner-1",
        "proposal-1",
        refresh_quote=True,
    )
    assert revalidated.json()["data"]["validation_id"] == "validation-2"


def test_final_and_confirmation_are_authenticated_and_never_execute(
    workflow_client,
):
    client, services = workflow_client

    final = client.get(
        "/api/decision/final/today",
        params={"refresh": "true", "user_id": "attacker"},
    )
    confirmed = client.post(
        "/api/decision/proposals/proposal-1/confirm?user_id=attacker",
        json={
            "validation_id": "validation-1",
            "accepted": True,
            "reason": "我已核对并自行确认",
        },
    )

    assert final.status_code == 200
    services["confirmation"].workspace.assert_awaited_once_with(
        "owner-1", refresh=True
    )
    confirmation_model = services["confirmation"].confirm.await_args.args[2]
    assert confirmation_model.accepted is True
    services["confirmation"].confirm.assert_awaited_once()
    assert services["confirmation"].confirm.await_args.args[:2] == (
        "owner-1",
        "proposal-1",
    )
    assert confirmed.json()["data"]["execution_status"] == "not_executed"


@pytest.mark.parametrize(
    ("service_name", "request_method", "path", "error"),
    [
        (
            "research",
            "get",
            "/api/decision/research/today",
            DecisionWorkflowError(
                "research_unavailable",
                "研究包无法持久化",
                status_code=503,
                details={"collection": "decision_research_packets"},
            ),
        ),
        (
            "validation",
            "post",
            "/api/decision/proposals/missing/validate",
            DecisionWorkflowError(
                "decision_proposal_not_found",
                "提案不存在",
                status_code=404,
                details={"proposal_id": "missing"},
            ),
        ),
        (
            "confirmation",
            "post",
            "/api/decision/proposals/proposal-1/confirm",
            DecisionWorkflowError(
                "decision_validation_not_confirmable",
                "提案当前不能确认",
                status_code=409,
                details={"validation_status": "invalid"},
            ),
        ),
    ],
)
def test_workflow_errors_preserve_status_code_message_and_details(
    workflow_client,
    service_name,
    request_method,
    path,
    error,
):
    client, services = workflow_client
    if service_name == "research":
        services[service_name].today.side_effect = error
        response = getattr(client, request_method)(path)
    elif service_name == "validation":
        services[service_name].validate.side_effect = error
        response = getattr(client, request_method)(path)
    else:
        services[service_name].confirm.side_effect = error
        response = getattr(client, request_method)(
            path,
            json={
                "validation_id": "validation-1",
                "accepted": True,
            },
        )

    assert response.status_code == error.status_code
    assert response.json()["detail"] == {
        "code": error.code,
        "message": error.message,
        "details": error.details,
    }


def test_proposal_schema_failures_return_422_without_calling_service(
    workflow_client,
):
    client, services = workflow_client

    response = client.post(
        "/api/decision/proposals",
        json={
            "research_packet_id": "research-1",
            "selections": [],
            "portfolio_rationale": "缺少空仓理由",
        },
    )

    assert response.status_code == 422
    services["proposal"].submit.assert_not_awaited()
