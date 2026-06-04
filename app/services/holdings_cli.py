"""Machine-readable holdings CLI for local agents such as Hermes."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

import typer
from bson import ObjectId
from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from app.core.database import get_mongo_db_sync
from app.services.holding_ai_advice import extract_report_price_plan, parse_report_recommendation
from app.services.portfolio_target_analysis import build_target_analysis


class CLIError(Exception):
    """Expected CLI error with a stable JSON error code."""

    def __init__(self, message: str, *, code: str = "cli_error", exit_code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code


holdings_app = typer.Typer(
    name="holdings",
    help="持仓数据 JSON CLI，供 Hermes/Agent 读取本地持仓 | Holdings JSON CLI for local agents",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _write_json(payload: Dict[str, Any], *, pretty: bool = False, stderr: bool = False) -> None:
    output = json.dumps(
        payload,
        ensure_ascii=False,
        default=_json_default,
        indent=2 if pretty else None,
        sort_keys=pretty,
    )
    stream = sys.stderr if stderr else sys.stdout
    stream.write(output + "\n")


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


def _get_database() -> Any:
    try:
        return get_mongo_db_sync()
    except Exception as exc:  # pragma: no cover - covered by command integration in real runtime.
        raise CLIError(f"MongoDB 连接失败: {exc}", code="database_error", exit_code=4) from exc


def _clean_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = {key: value for key, value in doc.items() if key != "_id"}
    if "_id" in doc:
        cleaned["id"] = str(doc["_id"])
    return cleaned


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = _clean_doc(user)
    return {
        "id": str(cleaned.get("id") or cleaned.get("_id") or ""),
        "username": cleaned.get("username"),
        "email": cleaned.get("email"),
        "is_active": cleaned.get("is_active"),
        "is_admin": cleaned.get("is_admin", False),
        "created_at": cleaned.get("created_at"),
        "last_login": cleaned.get("last_login"),
    }


def _selector_from_env() -> Dict[str, Optional[str]]:
    return {
        "username": os.getenv("TRADINGAGENTS_HERMES_USERNAME") or os.getenv("TRADINGAGENTS_USERNAME"),
        "email": os.getenv("TRADINGAGENTS_HERMES_EMAIL") or os.getenv("TRADINGAGENTS_EMAIL"),
        "user_id": os.getenv("TRADINGAGENTS_HERMES_USER_ID") or os.getenv("TRADINGAGENTS_USER_ID"),
    }


def _selector_count(username: Optional[str], email: Optional[str], user_id: Optional[str]) -> int:
    return sum(1 for value in (username, email, user_id) if value)


def select_user(
    db: Any,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    allow_env: bool = True,
) -> Dict[str, Any]:
    """Resolve one local user. If no selector is supplied, auto-select only one-user DBs."""
    if allow_env and _selector_count(username, email, user_id) == 0:
        env_selector = _selector_from_env()
        username = env_selector["username"]
        email = env_selector["email"]
        user_id = env_selector["user_id"]

    if _selector_count(username, email, user_id) > 1:
        raise CLIError("只能提供 username、email、user-id 其中一个用户选择器", code="ambiguous_selector")

    if user_id:
        if not ObjectId.is_valid(user_id):
            raise CLIError(f"user-id 不是有效 ObjectId: {user_id}", code="invalid_user_id")
        user = db["users"].find_one({"_id": ObjectId(user_id)})
    elif username:
        user = db["users"].find_one({"username": username})
    elif email:
        user = db["users"].find_one({"email": email})
    else:
        users = list(db["users"].find({}).limit(2))
        if not users:
            raise CLIError("本地 users 集合中没有用户", code="user_not_found", exit_code=3)
        if len(users) > 1:
            raise CLIError(
                "检测到多个用户，请传 --username、--email 或 --user-id 明确选择",
                code="user_selector_required",
            )
        user = users[0]

    if not user:
        raise CLIError("用户不存在，请先用 holdings users 查看可用用户", code="user_not_found", exit_code=3)

    return _public_user(user)


def _iter_docs(cursor: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(doc) for doc in cursor]


def _holding_cost(item: Dict[str, Any]) -> float:
    return float(item.get("cost_price") or 0) * float(item.get("quantity") or 0)


def _known_market_value(item: Dict[str, Any]) -> Optional[float]:
    current_price = item.get("current_price")
    if current_price is None or current_price == "":
        return None
    return float(current_price) * float(item.get("quantity") or 0)


def _find_one_safe(db: Any, collection_name: str, query: Dict[str, Any], projection: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    try:
        return db[collection_name].find_one(query, projection)
    except Exception:
        return None


def _normalize_price(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    if normalized <= 0:
        return None
    return round(normalized, 4)


def _price_distance_pct(active_price: Optional[float], current_price: Optional[float]) -> Optional[float]:
    if active_price is None or current_price is None or current_price <= 0:
        return None
    return round((active_price - current_price) / current_price * 100, 2)


def _build_price_plan_row(
    *,
    key: str,
    label: str,
    tone: str,
    manual_price: Any,
    report_price: Any,
    current_price: Optional[float],
) -> Dict[str, Any]:
    manual = _normalize_price(manual_price)
    report = _normalize_price(report_price)
    active = manual if manual is not None else report
    if manual is not None:
        active_source = "manual"
    elif report is not None:
        active_source = "report"
    else:
        active_source = "none"

    return {
        "key": key,
        "label": label,
        "tone": tone,
        "manual_price": manual,
        "report_price": report,
        "active_price": active,
        "active_source": active_source,
        "distance_pct": _price_distance_pct(active, current_price),
    }


def _with_price_plan(item: Dict[str, Any]) -> Dict[str, Any]:
    advice = item.get("ai_advice") if isinstance(item.get("ai_advice"), dict) else {}
    current_price = _normalize_price(item.get("current_price"))
    rows = [
        _build_price_plan_row(
            key="stop",
            label="止损",
            tone="danger",
            manual_price=item.get("manual_stop_loss_price"),
            report_price=advice.get("stop_loss_price"),
            current_price=current_price,
        ),
        _build_price_plan_row(
            key="target",
            label="目标",
            tone="success",
            manual_price=item.get("manual_target_price"),
            report_price=advice.get("target_price"),
            current_price=current_price,
        ),
        _build_price_plan_row(
            key="sell",
            label="卖出",
            tone="warning",
            manual_price=item.get("manual_sell_price"),
            report_price=advice.get("suggested_sell_price"),
            current_price=current_price,
        ),
        _build_price_plan_row(
            key="buy",
            label="追入",
            tone="info",
            manual_price=item.get("manual_buy_price"),
            report_price=advice.get("suggested_buy_price"),
            current_price=current_price,
        ),
    ]
    item["price_plan"] = {
        "rows": rows,
        "has_manual": any(row["manual_price"] is not None for row in rows),
        "has_report": any(row["report_price"] is not None for row in rows),
        "has_active": any(row["active_price"] is not None for row in rows),
        "notes": item.get("price_plan_notes") or "",
        "updated_at": item.get("price_plan_updated_at"),
        "is_reference_only": True,
    }
    return item


def _resolve_current_price(db: Any, item: Dict[str, Any]) -> Optional[float]:
    stored_price = _normalize_price(item.get("current_price"))
    if stored_price is not None:
        return stored_price

    code = str(item.get("code") or "").upper()
    market = str(item.get("market") or "CN").upper()
    if not code:
        return None

    if market == "CN":
        for collection_name, field_names in (
            ("market_quotes", ("close", "price", "current_price")),
            ("stock_basic_info", ("current_price", "close", "price")),
        ):
            doc = _find_one_safe(
                db,
                collection_name,
                {"$or": [{"code": code}, {"symbol": code}]},
                {field: 1 for field in field_names},
            )
            if not doc:
                continue
            for field_name in field_names:
                price = _normalize_price(doc.get(field_name))
                if price is not None:
                    return price

    return None


def _with_current_price(db: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    current_price = _resolve_current_price(db, item)
    if current_price is not None:
        item["current_price"] = current_price
    return item


def _latest_report_meta(db: Any, code: str) -> Optional[Dict[str, Any]]:
    doc = db["analysis_reports"].find_one(
        {"stock_symbol": str(code).upper()},
        sort=[("created_at", DESCENDING)],
    )
    if not doc:
        return None

    reports = doc.get("reports") if isinstance(doc.get("reports"), dict) else {}
    decision = doc.get("decision") if isinstance(doc.get("decision"), dict) else {}
    return {
        "id": str(doc.get("_id")),
        "analysis_id": doc.get("analysis_id"),
        "task_id": doc.get("task_id"),
        "analysis_date": doc.get("analysis_date"),
        "model_info": doc.get("model_info"),
        "recommendation": doc.get("recommendation") or "",
        "decision": decision,
        "price_plan": extract_report_price_plan(reports),
    }


def _build_report_advice(db: Any, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    report_meta = _latest_report_meta(db, str(item.get("code") or ""))
    if not report_meta:
        return None

    recommendation = report_meta.get("recommendation") or ""
    decision = report_meta.get("decision") if isinstance(report_meta.get("decision"), dict) else {}
    if not recommendation and not decision:
        return None

    advice = parse_report_recommendation(
        recommendation,
        current_price=item.get("current_price"),
        decision=decision,
        price_plan=report_meta.get("price_plan"),
    )
    advice.update(
        {
            "model_name": str(report_meta.get("model_info") or "analysis_report"),
            "provider": "analysis_report",
            "based_on_report": {k: v for k, v in report_meta.items() if k not in {"recommendation", "decision"}},
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
    )
    return advice


def _with_report_advice(db: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    advice = _build_report_advice(db, item)
    if advice:
        item["ai_advice"] = advice
    return item


def _build_summary(items: List[Dict[str, Any]], settings: Dict[str, Any]) -> Dict[str, Any]:
    total_cost = sum(_holding_cost(item) for item in items)
    known_pairs = [
        (_holding_cost(item), market_value)
        for item in items
        for market_value in [_known_market_value(item)]
        if market_value is not None
    ]
    known_cost = sum(cost for cost, _ in known_pairs) if known_pairs else None
    known_values = [value for _, value in known_pairs]
    known_market_value = sum(known_values) if known_values else None
    known_profit_loss = (
        known_market_value - known_cost
        if known_market_value is not None and known_cost is not None
        else None
    )
    known_profit_loss_pct = (
        known_profit_loss / known_cost * 100
        if known_profit_loss is not None and known_cost and known_cost > 0
        else None
    )

    markets: Dict[str, Dict[str, Any]] = {}
    for item in items:
        market = str(item.get("market") or "UNKNOWN").upper()
        bucket = markets.setdefault(market, {"holding_count": 0, "total_cost": 0.0})
        bucket["holding_count"] += 1
        bucket["total_cost"] += _holding_cost(item)

    for bucket in markets.values():
        bucket["total_cost"] = round(bucket["total_cost"], 2)

    total_assets = float(settings.get("total_assets") or 0)
    configured_total_assets = settings.get("configured_total_assets")
    manual_price_plan_count = sum(1 for item in items if item.get("price_plan", {}).get("has_manual"))
    report_price_plan_count = sum(1 for item in items if item.get("price_plan", {}).get("has_report"))
    active_price_plan_count = sum(1 for item in items if item.get("price_plan", {}).get("has_active"))
    return {
        "holding_count": len(items),
        "total_cost": round(total_cost, 2),
        "known_cost": round(known_cost, 2) if known_cost is not None else None,
        "known_market_value": round(known_market_value, 2) if known_market_value is not None else None,
        "known_profit_loss": round(known_profit_loss, 2) if known_profit_loss is not None else None,
        "known_profit_loss_pct": round(known_profit_loss_pct, 2) if known_profit_loss_pct is not None else None,
        "total_assets": round(total_assets, 2),
        "configured_total_assets": configured_total_assets,
        "is_auto_total_assets": settings.get("is_auto_total_assets"),
        "cash_or_unallocated": round(total_assets - total_cost, 2),
        "manual_price_plan_count": manual_price_plan_count,
        "report_price_plan_count": report_price_plan_count,
        "active_price_plan_count": active_price_plan_count,
        "markets": markets,
    }


def _with_analysis(item: Dict[str, Any]) -> Dict[str, Any]:
    current_price = item.get("current_price")
    item["analysis"] = build_target_analysis(item, current_price=current_price, as_of=date.today())
    return item


def _query_holdings(
    db: Any,
    *,
    user_id: str,
    code: Optional[str] = None,
    market: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if code:
        query["code"] = code.upper()
    if market:
        query["market"] = market.upper()
    cursor = db["user_holdings"].find(query).sort("updated_at", DESCENDING)
    return [_clean_doc(doc) for doc in _iter_docs(cursor)]


def build_holdings_payload(
    db: Any,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    code: Optional[str] = None,
    market: Optional[str] = None,
    include_analysis: bool = True,
) -> Dict[str, Any]:
    user = select_user(db, username=username, email=email, user_id=user_id)
    items = _query_holdings(db, user_id=user["id"], code=code, market=market)
    items = [_with_current_price(db, item) for item in items]
    if include_analysis:
        items = [_with_analysis(item) for item in items]
    items = [_with_price_plan(_with_report_advice(db, item)) for item in items]

    total_holding_cost = sum(_holding_cost(item) for item in items)
    settings_doc = db["user_holding_settings"].find_one({"user_id": user["id"]})
    settings = _build_settings_payload(settings_doc, total_holding_cost)
    summary = _build_summary(items, settings)

    return {
        "ok": True,
        "data": {
            "user": user,
            "items": items,
            "settings": settings,
            "summary": summary,
        },
        "meta": {
            "schema_version": 2,
            "source": "mongo.user_holdings+analysis_reports",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def build_summary_payload(
    db: Any,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_holdings_payload(
        db,
        username=username,
        email=email,
        user_id=user_id,
        include_analysis=False,
    )
    data = payload["data"]
    payload["data"] = {
        "user": data["user"],
        "settings": data["settings"],
        "summary": data["summary"],
    }
    return payload


def build_users_payload(db: Any, *, limit: int = 100) -> Dict[str, Any]:
    cursor = db["users"].find({}).sort("created_at", DESCENDING).limit(limit)
    users = [_public_user(user) for user in _iter_docs(cursor)]
    return {
        "ok": True,
        "data": {
            "count": len(users),
            "users": users,
        },
        "meta": {
            "schema_version": 1,
            "source": "mongo.users",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def _run_json(builder, *, pretty: bool = False) -> None:
    try:
        payload = builder(_get_database())
        _write_json(payload, pretty=pretty)
    except CLIError as exc:
        _write_json(
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc
    except PyMongoError as exc:
        _write_json(
            {"ok": False, "error": {"code": "database_error", "message": str(exc)}},
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(4) from exc


@holdings_app.command(name="users", help="列出本地用户，便于选择 --user-id/--username")
def users_command(
    limit: int = typer.Option(100, "--limit", min=1, max=500, help="最多返回用户数"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(lambda db: build_users_payload(db, limit=limit), pretty=pretty)


@holdings_app.command(name="list", help="输出持仓明细 JSON")
def list_command(
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    code: Optional[str] = typer.Option(None, "--code", help="只返回指定股票代码"),
    market: Optional[str] = typer.Option(None, "--market", help="只返回指定市场，如 CN/HK/US"),
    analysis: bool = typer.Option(True, "--analysis/--no-analysis", help="是否附带目标进度分析"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_holdings_payload(
            db,
            username=username,
            email=email,
            user_id=user_id,
            code=code,
            market=market,
            include_analysis=analysis,
        ),
        pretty=pretty,
    )


@holdings_app.command(name="get", help="按股票代码输出单只持仓 JSON")
def get_command(
    code: str = typer.Option(..., "--code", help="股票代码"),
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    market: Optional[str] = typer.Option(None, "--market", help="市场，如 CN/HK/US"),
    analysis: bool = typer.Option(True, "--analysis/--no-analysis", help="是否附带目标进度分析"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_holdings_payload(
            db,
            username=username,
            email=email,
            user_id=user_id,
            code=code,
            market=market,
            include_analysis=analysis,
        ),
        pretty=pretty,
    )


@holdings_app.command(name="summary", help="输出持仓汇总 JSON，不包含明细 items")
def summary_command(
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_summary_payload(db, username=username, email=email, user_id=user_id),
        pretty=pretty,
    )


def main() -> None:
    holdings_app()


if __name__ == "__main__":
    main()
