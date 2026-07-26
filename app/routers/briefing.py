from fastapi import APIRouter, Depends, Query

from app.routers.auth_db import get_current_user
from app.services.daily_briefing_service import daily_briefing_service


router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.get("/today", response_model=dict)
async def get_today_briefing(
    refresh: bool = Query(default=True),
    user: dict = Depends(get_current_user),
):
    data = await daily_briefing_service.build(str(user["id"]), refresh=refresh)
    return {"success": True, "data": data, "message": "ok"}
