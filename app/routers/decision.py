"""Authenticated daily decision packet endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.services.daily_decision_service import (
    DecisionPersistenceError,
    daily_decision_service,
)


router = APIRouter(prefix="/decision", tags=["decision"])


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
