from datetime import date, datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.database import get_mongo_db
from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.routers.paper import _detect_market_and_code, _get_last_price
from app.services.holding_ai_advice import build_holding_ai_advice, build_holding_report_advice
from app.services.portfolio_target_analysis import build_target_analysis


router = APIRouter(prefix="/holdings", tags=["holdings"])


class HoldingCreateRequest(BaseModel):
    code: str = Field(..., description="股票代码")
    name: str = ""
    market: Optional[str] = None
    quantity: int = Field(..., gt=0)
    cost_price: float = Field(..., gt=0)
    target_monthly_return_pct: float = Field(default=10.0, gt=0)
    stop_loss_pct: float = Field(default=8.0, gt=0)
    take_profit_pct: Optional[float] = Field(default=None, gt=0)
    manual_stop_loss_price: Optional[float] = Field(default=None, gt=0)
    manual_target_price: Optional[float] = Field(default=None, gt=0)
    manual_sell_price: Optional[float] = Field(default=None, gt=0)
    manual_buy_price: Optional[float] = Field(default=None, gt=0)
    strategy: str = "swing"
    notes: str = ""
    price_plan_notes: str = ""


class HoldingUpdateRequest(BaseModel):
    name: Optional[str] = None
    quantity: Optional[int] = Field(default=None, gt=0)
    cost_price: Optional[float] = Field(default=None, gt=0)
    target_monthly_return_pct: Optional[float] = Field(default=None, gt=0)
    stop_loss_pct: Optional[float] = Field(default=None, gt=0)
    take_profit_pct: Optional[float] = Field(default=None, gt=0)
    manual_stop_loss_price: Optional[float] = Field(default=None, gt=0)
    manual_target_price: Optional[float] = Field(default=None, gt=0)
    manual_sell_price: Optional[float] = Field(default=None, gt=0)
    manual_buy_price: Optional[float] = Field(default=None, gt=0)
    strategy: Optional[str] = None
    notes: Optional[str] = None
    price_plan_notes: Optional[str] = None


class HoldingSettingsUpdateRequest(BaseModel):
    total_assets: Optional[float] = Field(default=None, ge=0, description="账户总资产")


PRICE_PLAN_FIELDS = {
    "manual_stop_loss_price",
    "manual_target_price",
    "manual_sell_price",
    "manual_buy_price",
    "price_plan_notes",
}


def _clean_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = {k: v for k, v in doc.items() if k != "_id"}
    cleaned["id"] = str(doc["_id"])
    return cleaned


async def _enrich_holding(doc: Dict[str, Any]) -> Dict[str, Any]:
    item = _clean_doc(doc)
    if not item.get("name") or item.get("name") == item.get("code"):
        item["name"] = await _resolve_stock_name(item["code"], item.get("market", "CN"))
    current_price = await _get_last_price(item["code"], item.get("market", "CN"))
    item["current_price"] = current_price
    item["analysis"] = build_target_analysis(item, current_price=current_price, as_of=date.today())
    report_advice = await build_holding_report_advice(item)
    if report_advice:
        item["ai_advice"] = report_advice
    return item


def _build_settings_payload(settings: Optional[Dict[str, Any]], total_holding_cost: float = 0.0) -> Dict[str, Any]:
    configured_total_assets = None
    if settings and settings.get("total_assets") is not None:
        configured_total_assets = float(settings.get("total_assets") or 0)

    effective_total_assets = configured_total_assets
    is_auto_total_assets = False
    if effective_total_assets is None:
        effective_total_assets = total_holding_cost
        is_auto_total_assets = True

    return {
        "total_assets": round(effective_total_assets, 2),
        "configured_total_assets": round(configured_total_assets, 2) if configured_total_assets is not None else None,
        "is_auto_total_assets": is_auto_total_assets,
        "updated_at": settings.get("updated_at") if settings else None,
    }


async def _resolve_stock_name(code: str, market: str = "CN") -> str:
    db = get_mongo_db()
    if market in {"HK", "US"}:
        try:
            from app.services.foreign_stock_service import ForeignStockService

            service = ForeignStockService(db=db)
            info = await service.get_basic_info(market, code, force_refresh=False)
            name = info.get("name") or info.get("name_en")
            if name:
                return str(name)
        except Exception:
            pass

    for query in (
        {"code": code, "source": "tushare"},
        {"code": code, "source": "akshare"},
        {"code": code, "source": "baostock"},
        {"code": code},
        {"symbol": code},
    ):
        doc = await db["stock_basic_info"].find_one(query, {"_id": 0, "name": 1})
        if doc and doc.get("name"):
            return str(doc["name"])
    return code


@router.get("/", response_model=dict)
async def list_holdings(current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    cursor = db["user_holdings"].find({"user_id": current_user["id"]}).sort("updated_at", -1)
    docs = await cursor.to_list(None)
    items = [await _enrich_holding(doc) for doc in docs]
    total_holding_cost = sum(float(item.get("cost_price") or 0) * float(item.get("quantity") or 0) for item in items)
    settings = await db["user_holding_settings"].find_one({"user_id": current_user["id"]})
    return ok({"items": items, "settings": _build_settings_payload(settings, total_holding_cost)})


@router.patch("/settings", response_model=dict)
async def update_holding_settings(
    payload: HoldingSettingsUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    db = get_mongo_db()
    updates = payload.model_dump(exclude_unset=True)
    updates["updated_at"] = datetime.utcnow().isoformat()
    await db["user_holding_settings"].update_one(
        {"user_id": current_user["id"]},
        {"$set": {**updates, "user_id": current_user["id"]}},
        upsert=True,
    )
    settings = await db["user_holding_settings"].find_one({"user_id": current_user["id"]})
    return ok({"settings": _build_settings_payload(settings)}, "设置已保存")


@router.post("/", response_model=dict)
async def create_holding(
    payload: HoldingCreateRequest,
    current_user: dict = Depends(get_current_user),
):
    db = get_mongo_db()
    market, normalized_code = _detect_market_and_code(payload.code)
    if payload.market:
        market = payload.market.upper()

    existing = await db["user_holdings"].find_one({
        "user_id": current_user["id"],
        "code": normalized_code,
        "market": market,
    })
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该持仓已存在")

    now = datetime.utcnow().isoformat()
    price_plan_updated_at = now if any(
        value not in (None, "")
        for value in (
            payload.manual_stop_loss_price,
            payload.manual_target_price,
            payload.manual_sell_price,
            payload.manual_buy_price,
            payload.price_plan_notes,
        )
    ) else None
    doc = {
        "user_id": current_user["id"],
        "code": normalized_code,
        "name": payload.name or await _resolve_stock_name(normalized_code, market),
        "market": market,
        "quantity": payload.quantity,
        "cost_price": payload.cost_price,
        "target_monthly_return_pct": payload.target_monthly_return_pct,
        "stop_loss_pct": payload.stop_loss_pct,
        "take_profit_pct": payload.take_profit_pct,
        "manual_stop_loss_price": payload.manual_stop_loss_price,
        "manual_target_price": payload.manual_target_price,
        "manual_sell_price": payload.manual_sell_price,
        "manual_buy_price": payload.manual_buy_price,
        "strategy": payload.strategy,
        "notes": payload.notes,
        "price_plan_notes": payload.price_plan_notes,
        "price_plan_updated_at": price_plan_updated_at,
        "created_at": now,
        "updated_at": now,
    }
    result = await db["user_holdings"].insert_one(doc)
    doc["_id"] = result.inserted_id
    return ok({"item": await _enrich_holding(doc)}, "创建成功")


@router.put("/{holding_id}", response_model=dict)
async def update_holding(
    holding_id: str,
    payload: HoldingUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    db = get_mongo_db()
    try:
        oid = ObjectId(holding_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="持仓ID无效")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有可更新字段")
    updates["updated_at"] = datetime.utcnow().isoformat()
    if PRICE_PLAN_FIELDS.intersection(updates.keys()):
        updates["price_plan_updated_at"] = updates["updated_at"]

    result = await db["user_holdings"].update_one(
        {"_id": oid, "user_id": current_user["id"]},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="持仓不存在")

    doc = await db["user_holdings"].find_one({"_id": oid, "user_id": current_user["id"]})
    return ok({"item": await _enrich_holding(doc)}, "更新成功")


@router.delete("/{holding_id}", response_model=dict)
async def delete_holding(holding_id: str, current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    try:
        oid = ObjectId(holding_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="持仓ID无效")

    result = await db["user_holdings"].delete_one({"_id": oid, "user_id": current_user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="持仓不存在")
    return ok({"id": holding_id}, "删除成功")


@router.post("/{holding_id}/analyze", response_model=dict)
async def analyze_holding(holding_id: str, current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    try:
        oid = ObjectId(holding_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="持仓ID无效")

    doc = await db["user_holdings"].find_one({"_id": oid, "user_id": current_user["id"]})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="持仓不存在")
    return ok({"item": await _enrich_holding(doc)})


@router.post("/{holding_id}/ai-advice", response_model=dict)
async def analyze_holding_with_model(holding_id: str, current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    try:
        oid = ObjectId(holding_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="持仓ID无效")

    doc = await db["user_holdings"].find_one({"_id": oid, "user_id": current_user["id"]})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="持仓不存在")

    item = await _enrich_holding(doc)
    advice = await build_holding_ai_advice(item)
    await db["user_holdings"].update_one(
        {"_id": oid, "user_id": current_user["id"]},
        {"$set": {"ai_advice": advice, "ai_advice_updated_at": advice["generated_at"]}},
    )
    item["ai_advice"] = advice
    return ok({"item": item, "advice": advice}, "AI建议已生成")
