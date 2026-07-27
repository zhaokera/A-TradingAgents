import asyncio
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.database import get_mongo_db, get_mongo_db_sync
from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.routers.paper import _detect_market_and_code, _get_last_price
from app.services.holding_ai_advice import (
    apply_holding_price_guardrails,
    build_holding_ai_advice,
    build_holding_report_advice,
)
from app.services.holding_price_guardrails import build_technical_price_plan
from app.services.portfolio_target_analysis import build_target_analysis
from app.services.tencent_quote_service import (
    assess_cn_quote_freshness,
    fetch_tencent_daily_bars_sync,
    get_tencent_quote_service,
    merge_tencent_quote_into_bars,
)


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


class HoldingSaleRequest(BaseModel):
    code: str
    quantity: int = Field(..., gt=0)
    sell_price: float = Field(..., gt=0)
    market: Optional[str] = None
    fee: float = Field(default=0.0, ge=0)
    sold_at: Optional[str] = None


class HoldingResearchRequest(BaseModel):
    codes: List[str] = Field(..., min_length=1, max_length=8)


class HoldingNoticeResearchRequest(HoldingResearchRequest):
    lookback_days: int = Field(default=7, ge=1, le=90)


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


async def _fetch_benchmark_session_dates() -> list[str]:
    benchmark_result = await asyncio.to_thread(
        fetch_tencent_daily_bars_sync,
        "sh000001",
        min_rows=2,
    )
    if not benchmark_result.get("ok"):
        return []
    return sorted(
        {
            str(bar.get("date"))
            for bar in benchmark_result.get("bars", [])
            if bar.get("date")
        }
    )


async def _enrich_holding(
    doc: Dict[str, Any],
    *,
    benchmark_session_dates: Optional[list[str]] = None,
) -> Dict[str, Any]:
    item = _clean_doc(doc)
    if not item.get("name") or item.get("name") == item.get("code"):
        item["name"] = await _resolve_stock_name(item["code"], item.get("market", "CN"))
    market = str(item.get("market") or "CN").upper()
    current_price = None
    quote_snapshot: Dict[str, Any] = {}
    technical_price_plan: Dict[str, Any] = {
        "actionable": False,
        "status": "unsupported_market",
    }
    holding_benchmark_session_dates: list[str] = []

    if market == "CN":
        quote = await get_tencent_quote_service().get_quote(item["code"])
        if quote:
            quote_snapshot = dict(quote)
            quote_snapshot["freshness"] = assess_cn_quote_freshness(quote)
            for field_name in ("close", "price", "current_price"):
                try:
                    candidate_price = float(quote.get(field_name) or 0)
                except (TypeError, ValueError):
                    candidate_price = 0
                if candidate_price > 0:
                    current_price = candidate_price
                    break

        if current_price is None:
            current_price = await _get_last_price(item["code"], market)
            quote_snapshot = {
                "source": "display_fallback",
                "price": current_price,
            }
            quote_snapshot["freshness"] = assess_cn_quote_freshness(quote_snapshot)

        holding_benchmark_session_dates = (
            list(benchmark_session_dates)
            if benchmark_session_dates is not None
            else await _fetch_benchmark_session_dates()
        )
        quote_trade_date = quote_snapshot.get("trade_date")
        if quote_trade_date and quote_trade_date not in holding_benchmark_session_dates:
            holding_benchmark_session_dates.append(quote_trade_date)
        holding_benchmark_session_dates.sort()

        if quote_snapshot.get("freshness", {}).get("actionable"):
            history_result = await asyncio.to_thread(fetch_tencent_daily_bars_sync, item["code"])
            if history_result.get("ok"):
                merge_result = merge_tencent_quote_into_bars(history_result.get("bars", []), quote_snapshot)
                if merge_result.get("ok"):
                    technical_price_plan = build_technical_price_plan(
                        merge_result.get("bars", []),
                        current_price=current_price,
                    )
                    technical_price_plan["history_status"] = history_result.get("status")
                    technical_price_plan["quote_merge_action"] = merge_result.get("merge_action")
                else:
                    technical_price_plan = {
                        "actionable": False,
                        "status": merge_result.get("status") or "quote_merge_failed",
                        "history_status": history_result.get("status"),
                    }
            else:
                technical_price_plan = {
                    "actionable": False,
                    "status": history_result.get("status") or "history_unavailable",
                    "history_reason": history_result.get("reason"),
                }
        else:
            technical_price_plan = {
                "actionable": False,
                "status": "quote_not_actionable",
                "quote_status": quote_snapshot.get("freshness", {}).get("status"),
            }
    else:
        current_price = await _get_last_price(item["code"], market)
        quote_snapshot = {
            "source": "display_fallback",
            "price": current_price,
            "freshness": {
                "actionable": False,
                "status": "unsupported_market",
                "reason": "当前价格计划门禁仅支持A股腾讯行情。",
            },
        }

    item["current_price"] = current_price
    item["quote_snapshot"] = quote_snapshot
    item["technical_price_plan"] = technical_price_plan
    item["benchmark_session_dates"] = holding_benchmark_session_dates
    item["guardrail_as_of"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    item["analysis"] = build_target_analysis(item, current_price=current_price, as_of=date.today())
    report_advice = await build_holding_report_advice(item)
    if report_advice:
        item["ai_advice"] = report_advice
    elif isinstance(item.get("ai_advice"), dict):
        item["ai_advice"] = apply_holding_price_guardrails(
            item["ai_advice"],
            item,
            historical_price_plan_key="historical_model_price_plan",
        )
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


def _run_legacy_account_builder(
    builder_name: str,
    *,
    user_id: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run mature account builders inside the backend process only."""
    from app.services import holdings_cli

    builder = getattr(holdings_cli, builder_name)
    return builder(
        get_mongo_db_sync(),
        user_id=user_id,
        **kwargs,
    )


def _run_legacy_research_builder(
    builder_name: str,
    *,
    codes: Optional[List[str]] = None,
    lookback_days: int = 7,
) -> Dict[str, Any]:
    """Run public research builders in the Docker backend process."""
    from app.services import holdings_cli

    if builder_name == "market_status":
        return holdings_cli.build_market_status_payload(
            get_mongo_db_sync(),
            database_status={"status": "connected"},
            retry_public_timeout=True,
        )

    context = holdings_cli.build_opportunity_market_context()
    if builder_name == "earnings":
        return holdings_cli.build_public_candidate_earnings_payload(
            codes,
            context=context,
        )
    if builder_name == "notices":
        return holdings_cli.build_public_candidate_notice_payload(
            codes,
            context=context,
            lookback_calendar_days=lookback_days,
        )
    raise ValueError(f"unsupported holdings research builder: {builder_name}")


def _research_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload.get("data") or {})
    if isinstance(payload.get("meta"), dict):
        data["meta"] = payload["meta"]
    return ok(data)


def _raise_research_http_error(exc: Exception) -> None:
    from app.services.holdings_cli import CLIError

    if not isinstance(exc, CLIError):
        raise exc
    detail: Dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.stage:
        detail["stage"] = exc.stage
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    ) from exc


@router.get("/snapshot", response_model=dict)
async def get_holdings_snapshot(
    code: Optional[str] = None,
    market: Optional[str] = None,
    analysis: bool = True,
    summary_only: bool = False,
    current_user: dict = Depends(get_current_user),
):
    builder_name = "build_summary_payload" if summary_only else "build_holdings_payload"
    kwargs = {} if summary_only else {
        "code": code,
        "market": market,
        "include_analysis": analysis,
    }
    payload = await run_in_threadpool(
        _run_legacy_account_builder,
        builder_name,
        user_id=current_user["id"],
        **kwargs,
    )
    return ok(payload["data"])


@router.get("/research/market-status", response_model=dict)
async def get_holding_market_status(
    _current_user: dict = Depends(get_current_user),
):
    try:
        payload = await run_in_threadpool(
            _run_legacy_research_builder,
            "market_status",
        )
    except Exception as exc:
        _raise_research_http_error(exc)
    return _research_response(payload)


@router.post("/research/earnings", response_model=dict)
async def review_holding_earnings(
    request: HoldingResearchRequest,
    _current_user: dict = Depends(get_current_user),
):
    try:
        payload = await run_in_threadpool(
            _run_legacy_research_builder,
            "earnings",
            codes=request.codes,
        )
    except Exception as exc:
        _raise_research_http_error(exc)
    return _research_response(payload)


@router.post("/research/notices", response_model=dict)
async def review_holding_notices(
    request: HoldingNoticeResearchRequest,
    _current_user: dict = Depends(get_current_user),
):
    try:
        payload = await run_in_threadpool(
            _run_legacy_research_builder,
            "notices",
            codes=request.codes,
            lookback_days=request.lookback_days,
        )
    except Exception as exc:
        _raise_research_http_error(exc)
    return _research_response(payload)


@router.get("/trades", response_model=dict)
async def list_holding_trades(
    code: Optional[str] = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1 到 500 之间")
    payload = await run_in_threadpool(
        _run_legacy_account_builder,
        "build_trades_payload",
        user_id=current_user["id"],
        code=code,
        limit=limit,
    )
    return ok(payload["data"])


@router.post("/record-sale", response_model=dict)
async def record_holding_sale(
    request: HoldingSaleRequest,
    current_user: dict = Depends(get_current_user),
):
    try:
        payload = await run_in_threadpool(
            _run_legacy_account_builder,
            "build_record_sale_payload",
            user_id=current_user["id"],
            **request.model_dump(),
        )
    except Exception as exc:
        from app.services.holdings_cli import CLIError

        if isinstance(exc, CLIError):
            raise HTTPException(status_code=400, detail=exc.message) from exc
        raise
    return ok(payload["data"], "卖出记录已保存")


@router.get("/", response_model=dict)
async def list_holdings(current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    cursor = db["user_holdings"].find({"user_id": current_user["id"]}).sort("updated_at", -1)
    docs = await cursor.to_list(None)
    benchmark_session_dates = (
        await _fetch_benchmark_session_dates()
        if any(str(doc.get("market") or "CN").upper() == "CN" for doc in docs)
        else []
    )
    items = await asyncio.gather(
        *(
            _enrich_holding(
                doc,
                benchmark_session_dates=benchmark_session_dates,
            )
            for doc in docs
        )
    )
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
