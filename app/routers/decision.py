"""Authenticated daily decision packet endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.response import ok
from app.models.decision import (
    CodexDecisionProposalInput,
    DecisionConfirmationInput,
)
from app.routers.auth_db import get_current_user
from app.services.daily_decision_service import (
    DecisionPersistenceError,
    daily_decision_service,
)
from app.services.decision_confirmation_service import (
    decision_confirmation_service,
)
from app.services.decision_proposal_service import decision_proposal_service
from app.services.decision_research_service import decision_research_service
from app.services.decision_review_service import (
    DecisionReviewError,
    decision_review_service,
)
from app.services.decision_validation_service import decision_validation_service
from app.services.decision_workflow_errors import DecisionWorkflowError


router = APIRouter(prefix="/decision", tags=["decision"])


def _raise_workflow_http_error(exc: DecisionWorkflowError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    ) from exc


@router.get("/today", response_model=dict)
async def get_today_decision(
    refresh: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await daily_decision_service.today(
            str(current_user["id"]),
            refresh=refresh,
        )
    except DecisionPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "decision_persistence_unavailable",
                "message": str(exc),
            },
        ) from exc
    return ok(data)


@router.get("/research/today", response_model=dict)
async def get_today_research_packet(
    refresh: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_research_service.today(
            str(current_user["id"]),
            refresh=refresh,
        )
    except DecisionWorkflowError as exc:
        _raise_workflow_http_error(exc)
    except DecisionPersistenceError as exc:
        _raise_workflow_http_error(
            DecisionWorkflowError(
                "research_persistence_unavailable",
                str(exc),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        )
    return ok(data)


@router.get("/baseline/today", response_model=dict)
async def get_today_software_baseline(
    refresh: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await daily_decision_service.today(
            str(current_user["id"]),
            refresh=refresh,
        )
    except DecisionPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "decision_baseline_unavailable",
                "message": str(exc),
                "details": {},
            },
        ) from exc
    return ok(data)


@router.post("/proposals", response_model=dict)
async def submit_codex_decision_proposal(
    proposal: CodexDecisionProposalInput,
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_proposal_service.submit(
            str(current_user["id"]),
            proposal,
        )
    except DecisionWorkflowError as exc:
        _raise_workflow_http_error(exc)
    return ok(data)


@router.post("/proposals/{proposal_id}/validate", response_model=dict)
async def validate_codex_decision_proposal(
    proposal_id: str,
    refresh_quote: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_validation_service.validate(
            str(current_user["id"]),
            proposal_id,
            refresh_quote=refresh_quote,
        )
    except DecisionWorkflowError as exc:
        _raise_workflow_http_error(exc)
    return ok(data)


@router.get("/final/today", response_model=dict)
async def get_today_final_decision(
    refresh: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_confirmation_service.workspace(
            str(current_user["id"]),
            refresh=refresh,
        )
    except DecisionWorkflowError as exc:
        _raise_workflow_http_error(exc)
    except DecisionPersistenceError as exc:
        _raise_workflow_http_error(
            DecisionWorkflowError(
                "decision_workspace_unavailable",
                str(exc),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        )
    return ok(data)


@router.post("/proposals/{proposal_id}/confirm", response_model=dict)
async def confirm_codex_decision_proposal(
    proposal_id: str,
    confirmation: DecisionConfirmationInput,
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_confirmation_service.confirm(
            str(current_user["id"]),
            proposal_id,
            confirmation,
        )
    except DecisionWorkflowError as exc:
        _raise_workflow_http_error(exc)
    return ok(data)


@router.get("/history", response_model=dict)
async def get_decision_history(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await daily_decision_service.history(
            str(current_user["id"]),
            limit=limit,
        )
    except DecisionPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "decision_history_unavailable",
                "message": str(exc),
            },
        ) from exc
    return ok(data)


@router.get("/performance", response_model=dict)
async def get_decision_performance(
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await decision_review_service.performance(str(current_user["id"]))
    except DecisionReviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "decision_performance_unavailable",
                "message": str(exc),
            },
        ) from exc
    return ok(data)
