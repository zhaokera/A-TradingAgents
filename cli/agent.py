"""Unified JSON CLI for local agents such as Hermes."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from click.exceptions import ClickException

from cli.agent_client import (
    DEFAULT_CLI_USERNAME,
    DEFAULT_SESSION_DAYS,
    MAX_SESSION_DAYS,
    AgentCLIError,
    build_api_client,
    delete_session,
    load_session,
    login_session,
    normalize_api_url,
    parse_json_object,
    resolve_session_file,
)


DEFAULT_NOTICE_LOOKBACK_DAYS = 7
MAX_NOTICE_LOOKBACK_DAYS = 90


@dataclass(frozen=True)
class RootOptions:
    api_url: Optional[str]
    username: Optional[str]
    password: Optional[str]
    session_days: int
    session_file: Optional[str]
    pretty: bool
    allow_remote_api: bool
    timeout_seconds: float


app = typer.Typer(
    name="agentctl",
    help="A-TradingAgents 本地 Agent JSON CLI",
    no_args_is_help=True,
    add_completion=True,
)
candidates_app = typer.Typer(help="AI 研究候选", no_args_is_help=True)
auth_app = typer.Typer(help="账号密码登录与 CLI 会话管理", no_args_is_help=True)
account_app = typer.Typer(help="当前账户交易权限", no_args_is_help=True)
holdings_app = typer.Typer(
    help="持仓数据 JSON CLI，所有命令均使用账号密码会话",
    no_args_is_help=True,
)
favorites_app = typer.Typer(help="自选股管理", no_args_is_help=True)
analysis_app = typer.Typer(help="单股、批量分析与任务管理", no_args_is_help=True)
reports_app = typer.Typer(help="分析报告管理", no_args_is_help=True)
screening_app = typer.Typer(help="规则筛选", no_args_is_help=True)
stocks_app = typer.Typer(help="股票搜索、行情、基本面、K线与新闻", no_args_is_help=True)
profile_app = typer.Typer(help="当前用户资料与偏好", no_args_is_help=True)
notifications_app = typer.Typer(help="通知管理", no_args_is_help=True)
briefing_app = typer.Typer(help="每日账户、市场与候选简报", no_args_is_help=True)
decision_app = typer.Typer(help="可审计的每日决策包", no_args_is_help=True)
admin_app = typer.Typer(help="受保护的系统管理 API", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(account_app, name="account")
app.add_typer(holdings_app, name="holdings")
app.add_typer(candidates_app, name="candidates")
app.add_typer(favorites_app, name="favorites")
app.add_typer(analysis_app, name="analysis")
app.add_typer(reports_app, name="reports")
app.add_typer(screening_app, name="screening")
app.add_typer(stocks_app, name="stocks")
app.add_typer(profile_app, name="profile")
app.add_typer(notifications_app, name="notifications")
app.add_typer(briefing_app, name="briefing")
app.add_typer(decision_app, name="decision")
app.add_typer(admin_app, name="admin")


def _write_json(payload: Dict[str, Any], *, pretty: bool, stderr: bool = False) -> None:
    output = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        default=str,
    )
    stream = sys.stderr if stderr else sys.stdout
    stream.write(output + "\n")
    stream.flush()


def _root_options(ctx: typer.Context) -> RootOptions:
    root = ctx.find_root()
    if not isinstance(root.obj, RootOptions):
        raise AgentCLIError("CLI 上下文未初始化", code="cli_context_missing")
    return root.obj


def _require_confirm(confirm: bool, action: str) -> None:
    if not confirm:
        raise AgentCLIError(
            f"{action} 会修改系统状态，必须显式传入 --confirm",
            code="confirmation_required",
        )


def _compact(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _request_api(
    ctx: typer.Context,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    require_admin: bool = False,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    options = _root_options(ctx)
    _identity, client = build_api_client(
        api_url=options.api_url,
        username=options.username,
        password=options.password,
        session_days=options.session_days,
        session_file=options.session_file,
        require_admin=require_admin,
        allow_remote=options.allow_remote_api,
        timeout_seconds=options.timeout_seconds,
    )
    with client:
        return client.request(
            method,
            path,
            params=params,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )


def _call_api(
    ctx: typer.Context,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    require_admin: bool = False,
    timeout_seconds: Optional[float] = None,
) -> None:
    options = _root_options(ctx)
    try:
        result = _request_api(
            ctx,
            method,
            path,
            params=params,
            payload=payload,
            require_admin=require_admin,
            timeout_seconds=timeout_seconds,
        )
        _write_json(result, pretty=options.pretty)
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


def _decision_payload(response: Dict[str, Any]) -> Dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise AgentCLIError(
            "今日决策响应缺少 data 对象",
            code="invalid_decision_response",
            exit_code=4,
        )
    return data


def _decision_item_summary(item: Any) -> Dict[str, Any]:
    value = item if isinstance(item, dict) else {}
    identity = value.get("identity") if isinstance(value.get("identity"), dict) else {}
    plans = value.get("plans") if isinstance(value.get("plans"), dict) else {}
    short_plan = plans.get("short") if isinstance(plans.get("short"), dict) else {}
    quote = value.get("quote") if isinstance(value.get("quote"), dict) else {}
    allocation = value.get("allocation") if isinstance(value.get("allocation"), dict) else {}
    planned_loss = (
        value.get("planned_loss") if isinstance(value.get("planned_loss"), dict) else {}
    )
    profile = value.get("profile") if isinstance(value.get("profile"), dict) else {}
    return {
        "code": identity.get("code"),
        "name": identity.get("name"),
        "action": value.get("action"),
        "reason_codes": value.get("reason_codes") or [],
        "objective_segment": identity.get("objective_segment"),
        "objective_match_score": identity.get("objective_match_score"),
        "price": quote.get("price"),
        "current_price": quote.get("price"),
        "current_price_trade_at": quote.get("trade_at"),
        "quote_source": quote.get("source"),
        "quote_status": quote.get("status"),
        "quote_checked_at": quote.get("quote_checked_at"),
        "entry_price": short_plan.get("entry_price"),
        "entry_reference_price": short_plan.get("entry_price"),
        "order_limit_price": (
            value.get("execution", {}).get("order_limit_price")
            if isinstance(value.get("execution"), dict)
            else None
        ),
        "execution_status": (
            value.get("execution", {}).get("status")
            if isinstance(value.get("execution"), dict)
            else "research_only"
        ),
        "stop_price": short_plan.get("stop_price"),
        "target_price": short_plan.get("target_price"),
        "plan_status": short_plan.get("entry_status"),
        "quantity": allocation.get("quantity", 0),
        "amount": allocation.get("amount", 0.0),
        "position_pct": allocation.get("position_pct", 0.0),
        "planned_loss_amount": planned_loss.get("amount", 0.0),
        "planned_loss_pct_of_assets": planned_loss.get("pct_of_assets", 0.0),
        "profile_status": profile.get("status"),
        "profile_confidence": profile.get("confidence"),
        "plan_id": value.get("plan_id"),
    }


def _decision_projection(data: Dict[str, Any], *, view: str) -> Dict[str, Any]:
    normalized_view = view.strip().lower()
    if normalized_view not in {"full", "summary", "actionable"}:
        raise AgentCLIError(
            "--view 必须是 full、summary 或 actionable",
            code="invalid_decision_view",
        )
    if normalized_view == "full":
        return data

    buckets = ("buy_now", "condition_order", "wait", "avoid")
    selected = buckets if normalized_view == "summary" else buckets[:2]
    projection: Dict[str, Any] = {
        "decision_id": data.get("decision_id"),
        "revision": data.get("revision"),
        "decision_date": data.get("decision_date"),
        "market_phase": data.get("market_phase"),
        "as_of": data.get("as_of"),
        "candidate_run_id": data.get("candidate_run_id"),
        "candidate_research": data.get("candidate_research"),
        "data_quality": data.get("data_quality"),
        "briefing_as_of": data.get("briefing_as_of"),
        "account": data.get("account"),
        "execution_capabilities": data.get("execution_capabilities"),
        "market": data.get("market"),
        "rolling_pool": data.get("rolling_pool"),
        "summary": data.get("summary"),
        "rule_version": data.get("rule_version"),
        "material_hash": data.get("material_hash"),
        "authority": data.get("authority"),
        "is_final_decision": data.get("is_final_decision"),
        "view": normalized_view,
    }
    for bucket in selected:
        items = data.get(bucket) if isinstance(data.get(bucket), list) else []
        projection[bucket] = [_decision_item_summary(item) for item in items]
    return projection


def _parse_payload(value: Optional[str], name: str = "--payload-json") -> Dict[str, Any]:
    return parse_json_object(value, option_name=name)


@app.callback()
def configure(
    ctx: typer.Context,
    api_url: Optional[str] = typer.Option(
        None,
        "--api-url",
        envvar="A_TRADINGAGENTS_API_URL",
        help="后端地址，默认 http://localhost:8331",
    ),
    username: Optional[str] = typer.Option(
        None,
        "--username",
        envvar="A_TRADINGAGENTS_USERNAME",
        help="登录用户名，默认 admin",
    ),
    password: Optional[str] = typer.Option(
        None,
        "--password",
        envvar="A_TRADINGAGENTS_PASSWORD",
        help="登录密码；建议通过环境变量提供",
    ),
    session_days: int = typer.Option(
        DEFAULT_SESSION_DAYS,
        "--session-days",
        min=DEFAULT_SESSION_DAYS,
        max=MAX_SESSION_DAYS,
        help="CLI 访问会话有效天数",
    ),
    session_file: Optional[str] = typer.Option(
        None,
        "--session-file",
        envvar="A_TRADINGAGENTS_SESSION_FILE",
        help="本机会话文件路径",
    ),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
    allow_remote_api: bool = typer.Option(
        False,
        "--allow-remote-api",
        help="允许连接非本机 API；默认禁止发送管理令牌到远端",
    ),
    timeout_seconds: float = typer.Option(60.0, "--timeout", min=1.0, max=1800.0),
) -> None:
    ctx.obj = RootOptions(
        api_url=api_url,
        username=username,
        password=password,
        session_days=session_days,
        session_file=session_file,
        pretty=pretty,
        allow_remote_api=allow_remote_api,
        timeout_seconds=timeout_seconds,
    )


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    password: Optional[str] = typer.Option(
        None,
        "--password",
        envvar="A_TRADINGAGENTS_PASSWORD",
        help="登录密码；建议通过环境变量提供",
    ),
    session_days: Optional[int] = typer.Option(
        None,
        "--session-days",
        min=DEFAULT_SESSION_DAYS,
        max=MAX_SESSION_DAYS,
        help="有效期，默认 7 天",
    ),
) -> None:
    """使用账号密码登录并保存本机会话。"""
    options = _root_options(ctx)
    try:
        session = login_session(
            api_url=options.api_url,
            username=username or options.username or DEFAULT_CLI_USERNAME,
            password=password or options.password,
            session_days=session_days or options.session_days,
            session_file=options.session_file,
            allow_remote=options.allow_remote_api,
            timeout_seconds=options.timeout_seconds,
        )
        _write_json(
            {
                "ok": True,
                "data": {
                    **session.public_payload(),
                    "session_file": str(resolve_session_file(options.session_file)),
                },
                "message": "登录成功",
            },
            pretty=options.pretty,
        )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """查看本地登录状态，不输出任何令牌。"""
    options = _root_options(ctx)
    try:
        api_url = normalize_api_url(
            options.api_url,
            allow_remote=options.allow_remote_api,
        )
        session = load_session(options.session_file)
        expected_username = options.username or DEFAULT_CLI_USERNAME
        matches = bool(
            session
            and session.api_url == api_url
            and session.username == expected_username
        )
        data: Dict[str, Any] = {
            "authenticated": bool(matches and session and session.refresh_is_valid()),
            "api_url": api_url,
            "username": expected_username,
            "session_file": str(resolve_session_file(options.session_file)),
        }
        if matches and session is not None:
            data.update(session.public_payload())
        _write_json({"ok": True, "data": data}, pretty=options.pretty)
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@auth_app.command("logout")
def auth_logout(ctx: typer.Context) -> None:
    """删除本机保存的 CLI 会话。"""
    options = _root_options(ctx)
    try:
        removed = delete_session(options.session_file)
        _write_json(
            {
                "ok": True,
                "data": {
                    "logged_out": True,
                    "session_removed": removed,
                    "session_file": str(resolve_session_file(options.session_file)),
                },
                "message": "已退出 CLI 会话",
            },
            pretty=options.pretty,
        )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@account_app.command("permissions")
def account_permissions(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/holdings/market-permissions")


@account_app.command("set-permission")
def account_set_permission(
    ctx: typer.Context,
    market: str = typer.Option(
        ...,
        "--market",
        help=(
            "star_market、chi_next_market 或 "
            "beijing_stock_exchange"
        ),
    ),
    state: str = typer.Option(
        ...,
        "--state",
        help="allowed、denied 或 unverified",
    ),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    market_aliases = {
        "star": "star_market",
        "star_market": "star_market",
        "chinext": "chi_next_market",
        "gem": "chi_next_market",
        "chi_next_market": "chi_next_market",
        "bse": "beijing_stock_exchange",
        "beijing_stock_exchange": "beijing_stock_exchange",
    }
    permission_key = market_aliases.get(market.strip().lower())
    if permission_key is None:
        raise typer.BadParameter("不支持的市场权限")
    normalized_state = state.strip().lower()
    if normalized_state not in {"allowed", "denied", "unverified"}:
        raise typer.BadParameter(
            "权限状态必须是 allowed、denied 或 unverified"
        )
    try:
        _require_confirm(confirm, "更新账户交易权限")
    except AgentCLIError as exc:
        _write_json(
            exc.payload(),
            pretty=_root_options(ctx).pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        "PATCH",
        f"/api/holdings/market-permissions/{permission_key}",
        payload={"state": normalized_state},
    )


@app.command("health")
def health(ctx: typer.Context) -> None:
    """检查正在运行的后端。"""
    _call_api(ctx, "GET", "/api/health")


@app.command("capabilities")
def capabilities(ctx: typer.Context) -> None:
    """输出 Hermes 可直接调用的稳定能力清单。"""
    _write_json(
        {
            "ok": True,
            "data": {
                "contract": "json-v1",
                "top_level": [
                    "health",
                    "doctor",
                    "capabilities",
                    "dashboard",
                    "version",
                ],
                "groups": {
                    "auth": ["login", "status", "logout"],
                    "account": ["permissions", "set-permission"],
                    "holdings": [
                        "list",
                        "summary",
                        "get",
                        "trades",
                        "record-sale",
                        "create",
                        "update",
                        "delete",
                        "settings",
                        "analyze",
                        "ai-advice",
                        "market-status",
                        "earnings",
                        "notices",
                        "opportunities",
                    ],
                    "candidates": [
                        "run",
                        "status",
                        "latest",
                        "performance",
                        "add-favorites",
                    ],
                    "favorites": ["list", "add", "update", "remove", "tags", "sync"],
                    "stocks": ["search", "info", "quote", "fundamentals", "kline", "news"],
                    "screening": ["fields", "industries", "run"],
                    "analysis": [
                        "start",
                        "batch",
                        "list",
                        "status",
                        "result",
                        "details",
                        "batch-status",
                        "cancel",
                        "delete",
                    ],
                    "reports": ["list", "get", "content", "download", "delete"],
                    "profile": ["get", "update"],
                    "notifications": ["list", "unread-count", "read", "read-all"],
                    "briefing": ["today"],
                    "decision": [
                        "research",
                        "baseline",
                        "propose",
                        "validate",
                        "final",
                        "confirm",
                        "today",
                        "explain",
                        "history",
                        "performance",
                    ],
                    "admin": ["routes", "call", "raw"],
                },
                "safety": {
                    "local_api_only_by_default": True,
                    "admin_mutations_require_confirm": True,
                    "permission_updates_require_confirm": True,
                    "deletions_require_confirm": True,
                    "initial_password_login_required": True,
                    "minimum_session_days": DEFAULT_SESSION_DAYS,
                    "session_tokens_are_never_printed": True,
                },
                "fallback": {
                    "command": "admin raw",
                    "scope": "authenticated JSON endpoints under /api/",
                },
                "decision_contract": {
                    "views": ["full", "summary", "actionable"],
                    "buckets": ["buy_now", "condition_order", "wait", "avoid"],
                    "single_symbol_command": "decision explain --code CODE",
                    "readiness_command": "doctor",
                    "execution": "docker_backend_api_only",
                },
            },
        },
        pretty=_root_options(ctx).pretty,
    )


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """验证另一个 Agent 完成决策所需的只读 API 契约。"""
    options = _root_options(ctx)
    checks = (
        ("backend_health", "GET", "/api/health", None),
        ("authenticated_identity", "GET", "/api/auth/me", None),
        (
            "holdings_snapshot",
            "GET",
            "/api/holdings/snapshot",
            {"summary_only": "true"},
        ),
        ("briefing_today", "GET", "/api/briefing/today", {"refresh": "false"}),
        ("decision_today", "GET", "/api/decision/today", {"refresh": "false"}),
        (
            "decision_research",
            "GET",
            "/api/decision/research/today",
            {"refresh": "false"},
        ),
        (
            "decision_final",
            "GET",
            "/api/decision/final/today",
            {"refresh": "false"},
        ),
        ("decision_history", "GET", "/api/decision/history", {"limit": 1}),
        ("decision_performance", "GET", "/api/decision/performance", None),
        (
            "candidate_latest",
            "GET",
            "/api/screening/ai-candidates/latest",
            {"refresh": "false"},
        ),
        ("favorites", "GET", "/api/favorites/", None),
        (
            "reports",
            "GET",
            "/api/reports/list",
            {"page": 1, "page_size": 1},
        ),
    )
    results: List[Dict[str, Any]] = []
    try:
        _identity, client = build_api_client(
            api_url=options.api_url,
            username=options.username,
            password=options.password,
            session_days=options.session_days,
            session_file=options.session_file,
            allow_remote=options.allow_remote_api,
            timeout_seconds=options.timeout_seconds,
        )
        with client:
            for name, method, path, params in checks:
                try:
                    response = client.request(
                        method,
                        path,
                        params=params,
                        timeout_seconds=max(options.timeout_seconds, 180.0),
                    )
                    data = response.get("data")
                    contract_ok = response.get("ok") is True
                    if name == "decision_today":
                        contract_ok = contract_ok and isinstance(data, dict) and all(
                            isinstance(data.get(bucket), list)
                            for bucket in ("buy_now", "condition_order", "wait", "avoid")
                        )
                    elif name == "decision_research":
                        contract_ok = (
                            contract_ok
                            and isinstance(data, dict)
                            and bool(data.get("research_packet_id"))
                            and isinstance(data.get("candidates"), list)
                            and isinstance(data.get("hard_risk_policy"), dict)
                        )
                    elif name == "decision_final":
                        contract_ok = (
                            contract_ok
                            and isinstance(data, dict)
                            and data.get("authority")
                            in {"software_baseline", "codex_validated"}
                            and isinstance(data.get("is_final_decision"), bool)
                        )
                    results.append(
                        {
                            "name": name,
                            "status": "ok" if contract_ok else "invalid_contract",
                            "path": path,
                        }
                    )
                except AgentCLIError as exc:
                    results.append(
                        {
                            "name": name,
                            "status": "failed",
                            "path": path,
                            "error": exc.payload()["error"],
                        }
                    )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc

    ready = all(item["status"] == "ok" for item in results)
    _write_json(
        {
            "ok": ready,
            "data": {
                "ready_for_decision_agent": ready,
                "api_url": normalize_api_url(
                    options.api_url,
                    allow_remote=options.allow_remote_api,
                ),
                "checks": results,
                "passed": sum(item["status"] == "ok" for item in results),
                "total": len(results),
            },
        },
        pretty=options.pretty,
        stderr=not ready,
    )
    if not ready:
        raise typer.Exit(4)


@app.command("dashboard")
def dashboard(ctx: typer.Context) -> None:
    """返回与 Web 仪表板一致的最近任务、自选股和市场快讯。"""
    options = _root_options(ctx)
    try:
        _identity, client = build_api_client(
            api_url=options.api_url,
            username=options.username,
            password=options.password,
            session_days=options.session_days,
            session_file=options.session_file,
            allow_remote=options.allow_remote_api,
            timeout_seconds=options.timeout_seconds,
        )
        with client:
            favorites = client.request("GET", "/api/favorites/")
            tasks = client.request(
                "GET",
                "/api/analysis/tasks",
                params={"limit": 10, "offset": 0},
            )
            news = client.request(
                "GET",
                "/api/news-data/latest",
                params={"limit": 10, "hours_back": 24},
            )
        favorite_items = favorites.get("data") if isinstance(favorites.get("data"), list) else []
        task_data = tasks.get("data") if isinstance(tasks.get("data"), dict) else {}
        task_items = task_data.get("tasks") if isinstance(task_data.get("tasks"), list) else []
        _write_json(
            {
                "ok": True,
                "data": {
                    "summary": {
                        "analysis_tasks": task_data.get("total", len(task_items)),
                        "completed_tasks": sum(
                            1 for item in task_items if item.get("status") == "completed"
                        ),
                        "favorites": len(favorite_items),
                    },
                    "recent_tasks": task_items,
                    "favorites": favorite_items,
                    "market_news": news.get("data"),
                },
            },
            pretty=options.pretty,
        )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@briefing_app.command("today")
def briefing_today(
    ctx: typer.Context,
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
) -> None:
    """一次返回账户、持仓、宏观门控、组合候选和自选生命周期。"""
    _call_api(
        ctx,
        "GET",
        "/api/briefing/today",
        params={"refresh": str(refresh).lower()},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("research")
def decision_research(
    ctx: typer.Context,
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
) -> None:
    """获取 Codex 决策使用的不可变事实与风险研究包。"""
    _call_api(
        ctx,
        "GET",
        "/api/decision/research/today",
        params={"refresh": str(refresh).lower()},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("baseline")
def decision_baseline(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh/--no-refresh"),
) -> None:
    """读取仅供对照、降级和回测的软件四分类基线。"""
    _call_api(
        ctx,
        "GET",
        "/api/decision/baseline/today",
        params={"refresh": str(refresh).lower()},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("propose")
def decision_propose(
    ctx: typer.Context,
    payload_json: str = typer.Option(..., "--payload-json"),
) -> None:
    """提交严格 JSON Codex 提案并执行首次确定性硬风控校验。"""
    try:
        payload = _parse_payload(payload_json, "--payload-json")
    except AgentCLIError as exc:
        _write_json(
            exc.payload(),
            pretty=_root_options(ctx).pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        "POST",
        "/api/decision/proposals",
        payload=payload,
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("validate")
def decision_validate(
    ctx: typer.Context,
    proposal_id: str = typer.Option(..., "--proposal-id"),
    refresh_quote: bool = typer.Option(
        False,
        "--refresh-quote/--no-refresh-quote",
    ),
) -> None:
    """按最新时间敏感数据重新校验一个已保存提案。"""
    _call_api(
        ctx,
        "POST",
        f"/api/decision/proposals/{proposal_id}/validate",
        params={"refresh_quote": str(refresh_quote).lower()},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("final")
def decision_final(
    ctx: typer.Context,
    refresh: bool = typer.Option(False, "--refresh/--no-refresh"),
) -> None:
    """读取研究包、软件基线、Codex 提案、校验与人工确认状态。"""
    _call_api(
        ctx,
        "GET",
        "/api/decision/final/today",
        params={"refresh": str(refresh).lower()},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@decision_app.command("confirm")
def decision_confirm(
    ctx: typer.Context,
    proposal_id: str = typer.Option(..., "--proposal-id"),
    validation_id: str = typer.Option(..., "--validation-id"),
    accept: bool = typer.Option(False, "--accept"),
    reject: bool = typer.Option(False, "--reject"),
    reason: Optional[str] = typer.Option(None, "--reason"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """由用户显式接受或拒绝提案；只记录确认，不执行证券交易。"""
    try:
        if accept == reject:
            raise AgentCLIError(
                "必须且只能指定 --accept 或 --reject",
                code="confirmation_choice_invalid",
            )
        _require_confirm(confirm, "记录 Codex 决策确认")
    except AgentCLIError as exc:
        _write_json(
            exc.payload(),
            pretty=_root_options(ctx).pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        "POST",
        f"/api/decision/proposals/{proposal_id}/confirm",
        payload=_compact(
            {
                "validation_id": validation_id,
                "accepted": accept,
                "reason": reason,
            }
        ),
    )


@decision_app.command("today")
def decision_today(
    ctx: typer.Context,
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
    view: str = typer.Option(
        "full",
        "--view",
        help="full=完整包，summary=四类精简，actionable=仅立即/条件单",
    ),
) -> None:
    """返回当前四分类决策包并确保快照已持久化。"""
    options = _root_options(ctx)
    try:
        response = _request_api(
            ctx,
            "GET",
            "/api/decision/today",
            params={"refresh": str(refresh).lower()},
            timeout_seconds=max(options.timeout_seconds, 180.0),
        )
        response["data"] = _decision_projection(
            _decision_payload(response),
            view=view,
        )
        _write_json(response, pretty=options.pretty)
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@decision_app.command("explain")
def decision_explain(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    refresh: bool = typer.Option(False, "--refresh/--no-refresh"),
) -> None:
    """返回今日决策中单只股票的完整证据、价格计划和组合影响。"""
    options = _root_options(ctx)
    normalized_code = re.sub(r"\D", "", code)
    try:
        response = _request_api(
            ctx,
            "GET",
            "/api/decision/today",
            params={"refresh": str(refresh).lower()},
            timeout_seconds=max(options.timeout_seconds, 180.0),
        )
        data = _decision_payload(response)
        match: Optional[Dict[str, Any]] = None
        matched_bucket: Optional[str] = None
        available_codes: List[str] = []
        for bucket in ("buy_now", "condition_order", "wait", "avoid"):
            items = data.get(bucket) if isinstance(data.get(bucket), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
                item_code = re.sub(r"\D", "", str(identity.get("code") or ""))
                if item_code:
                    available_codes.append(item_code)
                if item_code == normalized_code:
                    match = item
                    matched_bucket = bucket
                    break
            if match is not None:
                break
        if match is None:
            raise AgentCLIError(
                "该股票不在今日决策包中",
                code="decision_symbol_not_found",
                details={
                    "code": normalized_code or code,
                    "available_codes": sorted(set(available_codes)),
                },
            )
        response["data"] = {
            "decision_id": data.get("decision_id"),
            "revision": data.get("revision"),
            "decision_date": data.get("decision_date"),
            "market_phase": data.get("market_phase"),
            "as_of": data.get("as_of"),
            "bucket": matched_bucket,
            "item": match,
        }
        _write_json(response, pretty=options.pretty)
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@decision_app.command("history")
def decision_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", min=1, max=100),
) -> None:
    """返回当前账号最近的不可变决策快照。"""
    _call_api(
        ctx,
        "GET",
        "/api/decision/history",
        params={"limit": limit},
    )


@decision_app.command("performance")
def decision_performance(ctx: typer.Context) -> None:
    """返回真实触发影子交易的绩效与受限校准状态。"""
    _call_api(ctx, "GET", "/api/decision/performance")


@app.command("version")
def version(ctx: typer.Context) -> None:
    """输出 CLI 和产品版本。"""
    version_files = (
        Path(__file__).resolve().parents[1] / "VERSION",
        Path.cwd() / "VERSION",
        Path("/app/VERSION"),
    )
    version_file = next((candidate for candidate in version_files if candidate.is_file()), None)
    product_version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file is not None
        else "unknown"
    )
    _write_json(
        {
            "ok": True,
            "data": {
                "cli": "agentctl",
                "product_version": product_version,
                "contract": "json-v1",
            },
        },
        pretty=_root_options(ctx).pretty,
    )


# AI candidates


@candidates_app.command("run")
def candidates_run(
    ctx: typer.Context,
    max_candidates: int = typer.Option(100, "--max-candidates", min=1, max=100),
    wait: bool = typer.Option(True, "--wait/--no-wait"),
) -> None:
    options = _root_options(ctx)
    try:
        response = _request_api(
            ctx,
            "POST",
            "/api/screening/ai-candidates/run",
            payload={"max_candidates": max_candidates},
        )
        job = response.get("data") if isinstance(response.get("data"), dict) else {}
        if not wait or not job.get("job_id"):
            _write_json(response, pretty=options.pretty)
            return
        deadline = time.monotonic() + max(options.timeout_seconds, 360.0)
        while job.get("status") in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise AgentCLIError(
                    "AI候选分析仍在后台运行",
                    code="candidate_job_wait_timeout",
                    details={"job_id": job.get("job_id")},
                )
            time.sleep(2)
            polled = _request_api(
                ctx,
                "GET",
                f"/api/screening/ai-candidates/jobs/{job['job_id']}",
            )
            job = polled.get("data") if isinstance(polled.get("data"), dict) else {}
        if job.get("status") == "failed":
            error = job.get("error") if isinstance(job.get("error"), dict) else {}
            raise AgentCLIError(
                str(error.get("message") or "AI候选分析失败"),
                code=str(error.get("code") or "candidate_research_failed"),
                details={"job_id": job.get("job_id"), "stage": error.get("stage")},
            )
        _write_json(
            {
                "ok": True,
                "status_code": 200,
                "data": job.get("result"),
                "message": "AI候选分析完成",
            },
            pretty=options.pretty,
        )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@candidates_app.command("latest")
def candidates_latest(
    ctx: typer.Context,
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/screening/ai-candidates/latest",
        params={"refresh": str(refresh).lower()},
    )


@candidates_app.command("status")
def candidates_status(
    ctx: typer.Context,
    job_id: str = typer.Option(..., "--job-id"),
) -> None:
    _call_api(ctx, "GET", f"/api/screening/ai-candidates/jobs/{job_id}")


@candidates_app.command("performance")
def candidates_performance(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/screening/ai-candidates/performance")


@candidates_app.command("add-favorites")
def candidates_add_favorites(
    ctx: typer.Context,
    run_id: str = typer.Option(..., "--run-id"),
    codes: List[str] = typer.Option(..., "--code", help="可重复，必须属于该候选批次"),
) -> None:
    _call_api(
        ctx,
        "POST",
        f"/api/screening/ai-candidates/{run_id}/favorites",
        payload={"codes": codes},
    )


# Favorites


@favorites_app.command("list")
def favorites_list(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/favorites/")


@favorites_app.command("add")
def favorites_add(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    name: str = typer.Option(..., "--name"),
    market: str = typer.Option("A股", "--market"),
    tags: Optional[List[str]] = typer.Option(None, "--tag"),
    notes: str = typer.Option("", "--notes"),
    alert_high: Optional[float] = typer.Option(None, "--alert-high"),
    alert_low: Optional[float] = typer.Option(None, "--alert-low"),
) -> None:
    _call_api(
        ctx,
        "POST",
        "/api/favorites/",
        payload={
            "stock_code": code,
            "stock_name": name,
            "market": market,
            "tags": tags or [],
            "notes": notes,
            "alert_price_high": alert_high,
            "alert_price_low": alert_low,
        },
    )


@favorites_app.command("update")
def favorites_update(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    tags: Optional[List[str]] = typer.Option(None, "--tag"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    alert_high: Optional[float] = typer.Option(None, "--alert-high"),
    alert_low: Optional[float] = typer.Option(None, "--alert-low"),
) -> None:
    payload = _compact(
        {
            "tags": tags,
            "notes": notes,
            "alert_price_high": alert_high,
            "alert_price_low": alert_low,
        }
    )
    if not payload:
        raise typer.BadParameter("至少提供一个更新字段")
    _call_api(ctx, "PUT", f"/api/favorites/{code}", payload=payload)


@favorites_app.command("remove")
def favorites_remove(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "移除自选股")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(ctx, "DELETE", f"/api/favorites/{code}")


@favorites_app.command("tags")
def favorites_tags(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/favorites/tags")


@favorites_app.command("sync")
def favorites_sync(
    ctx: typer.Context,
    source: str = typer.Option("tushare", "--source"),
) -> None:
    _call_api(
        ctx,
        "POST",
        "/api/favorites/sync-realtime",
        payload={"data_source": source},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@holdings_app.command("list")
def holdings_list(
    ctx: typer.Context,
    code: Optional[str] = typer.Option(None, "--code"),
    market: Optional[str] = typer.Option(None, "--market"),
    analysis: bool = typer.Option(True, "--analysis/--no-analysis"),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/holdings/snapshot",
        params=_compact(
            {
                "code": code,
                "market": market,
                "analysis": str(analysis).lower(),
            }
        ),
    )


@holdings_app.command("get")
def holdings_get(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    market: Optional[str] = typer.Option(None, "--market"),
    analysis: bool = typer.Option(True, "--analysis/--no-analysis"),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/holdings/snapshot",
        params=_compact(
            {
                "code": code,
                "market": market,
                "analysis": str(analysis).lower(),
            }
        ),
    )


@holdings_app.command("summary")
def holdings_summary(ctx: typer.Context) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/holdings/snapshot",
        params={"summary_only": "true"},
    )


@holdings_app.command("trades")
def holdings_trades(
    ctx: typer.Context,
    code: Optional[str] = typer.Option(None, "--code"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/holdings/trades",
        params=_compact({"code": code, "limit": limit}),
    )


@holdings_app.command("record-sale")
def holdings_record_sale(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    quantity: int = typer.Option(..., "--quantity", min=1),
    sell_price: float = typer.Option(..., "--sell-price", min=0.0001),
    market: Optional[str] = typer.Option(None, "--market"),
    fee: float = typer.Option(0.0, "--fee", min=0),
    sold_at: Optional[str] = typer.Option(None, "--sold-at"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "记录真实卖出")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        "POST",
        "/api/holdings/record-sale",
        payload=_compact(
            {
                "code": code,
                "quantity": quantity,
                "sell_price": sell_price,
                "market": market,
                "fee": fee,
                "sold_at": sold_at,
            }
        ),
    )


@holdings_app.command("market-status")
def holdings_market_status(ctx: typer.Context) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/holdings/research/market-status",
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@holdings_app.command("earnings")
def holdings_earnings(
    ctx: typer.Context,
    codes: Optional[List[str]] = typer.Option(None, "--code"),
) -> None:
    if not codes:
        raise typer.BadParameter("至少提供一个 --code")
    _call_api(
        ctx,
        "POST",
        "/api/holdings/research/earnings",
        payload={"codes": codes},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@holdings_app.command("notices")
def holdings_notices(
    ctx: typer.Context,
    codes: Optional[List[str]] = typer.Option(None, "--code"),
    lookback_days: int = typer.Option(
        DEFAULT_NOTICE_LOOKBACK_DAYS,
        "--lookback-days",
        min=1,
        max=MAX_NOTICE_LOOKBACK_DAYS,
    ),
) -> None:
    if not codes:
        raise typer.BadParameter("至少提供一个 --code")
    _call_api(
        ctx,
        "POST",
        "/api/holdings/research/notices",
        payload={"codes": codes, "lookback_days": lookback_days},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@holdings_app.command("opportunities")
def holdings_opportunities(
    ctx: typer.Context,
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
) -> None:
    """Compatibility alias for the same candidate contract used by Web."""
    _call_api(
        ctx,
        "GET",
        "/api/screening/ai-candidates/latest",
        params={"refresh": str(refresh).lower()},
    )


@holdings_app.command("create")
def holdings_create(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    quantity: int = typer.Option(..., "--quantity", min=1),
    cost_price: float = typer.Option(..., "--cost-price", min=0.0001),
    name: str = typer.Option("", "--name"),
    market: Optional[str] = typer.Option(None, "--market"),
    target_monthly_return_pct: float = typer.Option(10.0, "--monthly-target", min=0.01),
    stop_loss_pct: float = typer.Option(8.0, "--stop-loss-pct", min=0.01),
    take_profit_pct: Optional[float] = typer.Option(None, "--take-profit-pct", min=0.01),
    strategy: str = typer.Option("swing", "--strategy"),
    notes: str = typer.Option("", "--notes"),
) -> None:
    _call_api(
        ctx,
        "POST",
        "/api/holdings/",
        payload=_compact(
            {
                "code": code,
                "name": name,
                "market": market,
                "quantity": quantity,
                "cost_price": cost_price,
                "target_monthly_return_pct": target_monthly_return_pct,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "strategy": strategy,
                "notes": notes,
            }
        ),
    )


@holdings_app.command("update")
def holdings_update(
    ctx: typer.Context,
    holding_id: str = typer.Option(..., "--holding-id"),
    quantity: Optional[int] = typer.Option(None, "--quantity", min=1),
    cost_price: Optional[float] = typer.Option(None, "--cost-price", min=0.0001),
    target_monthly_return_pct: Optional[float] = typer.Option(None, "--monthly-target", min=0.01),
    stop_loss_pct: Optional[float] = typer.Option(None, "--stop-loss-pct", min=0.01),
    take_profit_pct: Optional[float] = typer.Option(None, "--take-profit-pct", min=0.01),
    manual_stop_loss_price: Optional[float] = typer.Option(None, "--stop-price", min=0.0001),
    manual_target_price: Optional[float] = typer.Option(None, "--target-price", min=0.0001),
    manual_sell_price: Optional[float] = typer.Option(None, "--sell-price", min=0.0001),
    manual_buy_price: Optional[float] = typer.Option(None, "--buy-price", min=0.0001),
    strategy: Optional[str] = typer.Option(None, "--strategy"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    price_plan_notes: Optional[str] = typer.Option(None, "--price-plan-notes"),
) -> None:
    payload = _compact(
        {
            "quantity": quantity,
            "cost_price": cost_price,
            "target_monthly_return_pct": target_monthly_return_pct,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "manual_stop_loss_price": manual_stop_loss_price,
            "manual_target_price": manual_target_price,
            "manual_sell_price": manual_sell_price,
            "manual_buy_price": manual_buy_price,
            "strategy": strategy,
            "notes": notes,
            "price_plan_notes": price_plan_notes,
        }
    )
    if not payload:
        raise typer.BadParameter("至少提供一个更新字段")
    _call_api(ctx, "PUT", f"/api/holdings/{holding_id}", payload=payload)


@holdings_app.command("delete")
def holdings_delete(
    ctx: typer.Context,
    holding_id: str = typer.Option(..., "--holding-id"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "删除持仓")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(ctx, "DELETE", f"/api/holdings/{holding_id}")


@holdings_app.command("settings")
def holdings_settings(
    ctx: typer.Context,
    total_assets: float = typer.Option(..., "--total-assets", min=0),
) -> None:
    _call_api(
        ctx,
        "PATCH",
        "/api/holdings/settings",
        payload={"total_assets": total_assets},
    )


@holdings_app.command("analyze")
def holdings_analyze(
    ctx: typer.Context,
    holding_id: str = typer.Option(..., "--holding-id"),
) -> None:
    _call_api(ctx, "POST", f"/api/holdings/{holding_id}/analyze", payload={})


@holdings_app.command("ai-advice")
def holdings_ai_advice(
    ctx: typer.Context,
    holding_id: str = typer.Option(..., "--holding-id"),
) -> None:
    _call_api(
        ctx,
        "POST",
        f"/api/holdings/{holding_id}/ai-advice",
        payload={},
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 300.0),
    )


def _analysis_parameters(
    *,
    market_type: str,
    analysis_date: Optional[str],
    research_depth: str,
    analysts: Optional[List[str]],
    custom_prompt: Optional[str],
    include_sentiment: bool,
    include_risk: bool,
    quick_model: Optional[str],
    deep_model: Optional[str],
    holding_json: Optional[str],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "market_type": market_type,
        "research_depth": research_depth,
        "selected_analysts": analysts or ["market", "fundamentals", "news", "social"],
        "include_sentiment": include_sentiment,
        "include_risk": include_risk,
        "language": "zh-CN",
    }
    if analysis_date:
        params["analysis_date"] = analysis_date
    if custom_prompt:
        params["custom_prompt"] = custom_prompt
    if quick_model:
        params["quick_analysis_model"] = quick_model
    if deep_model:
        params["deep_analysis_model"] = deep_model
    if holding_json:
        params["holding"] = _parse_payload(holding_json, "--holding-json")
    return params


# Analysis and tasks


@analysis_app.command("start")
def analysis_start(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    market_type: str = typer.Option("A股", "--market-type"),
    analysis_date: Optional[str] = typer.Option(None, "--analysis-date"),
    research_depth: str = typer.Option("标准", "--research-depth"),
    analysts: Optional[List[str]] = typer.Option(None, "--analyst"),
    custom_prompt: Optional[str] = typer.Option(None, "--custom-prompt"),
    include_sentiment: bool = typer.Option(True, "--sentiment/--no-sentiment"),
    include_risk: bool = typer.Option(True, "--risk/--no-risk"),
    quick_model: Optional[str] = typer.Option(None, "--quick-model"),
    deep_model: Optional[str] = typer.Option(None, "--deep-model"),
    holding_json: Optional[str] = typer.Option(None, "--holding-json"),
) -> None:
    params = _analysis_parameters(
        market_type=market_type,
        analysis_date=analysis_date,
        research_depth=research_depth,
        analysts=analysts,
        custom_prompt=custom_prompt,
        include_sentiment=include_sentiment,
        include_risk=include_risk,
        quick_model=quick_model,
        deep_model=deep_model,
        holding_json=holding_json,
    )
    _call_api(ctx, "POST", "/api/analysis/single", payload={"symbol": code, "parameters": params})


@analysis_app.command("batch")
def analysis_batch(
    ctx: typer.Context,
    codes: List[str] = typer.Option(..., "--code", help="可重复，最多 10 只"),
    title: str = typer.Option("CLI 批量分析", "--title"),
    description: Optional[str] = typer.Option(None, "--description"),
    market_type: str = typer.Option("A股", "--market-type"),
    research_depth: str = typer.Option("标准", "--research-depth"),
    analysts: Optional[List[str]] = typer.Option(None, "--analyst"),
) -> None:
    if len(codes) > 10:
        raise typer.BadParameter("批量分析最多支持 10 只股票")
    params = _analysis_parameters(
        market_type=market_type,
        analysis_date=None,
        research_depth=research_depth,
        analysts=analysts,
        custom_prompt=None,
        include_sentiment=True,
        include_risk=True,
        quick_model=None,
        deep_model=None,
        holding_json=None,
    )
    _call_api(
        ctx,
        "POST",
        "/api/analysis/batch",
        payload=_compact(
            {"title": title, "description": description, "symbols": codes, "parameters": params}
        ),
    )


@analysis_app.command("list")
def analysis_list(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/analysis/tasks",
        params={"status": status, "limit": limit, "offset": offset},
    )


@analysis_app.command("status")
def analysis_status(ctx: typer.Context, task_id: str = typer.Option(..., "--task-id")) -> None:
    _call_api(ctx, "GET", f"/api/analysis/tasks/{task_id}/status")


@analysis_app.command("result")
def analysis_result(ctx: typer.Context, task_id: str = typer.Option(..., "--task-id")) -> None:
    _call_api(ctx, "GET", f"/api/analysis/tasks/{task_id}/result")


@analysis_app.command("details")
def analysis_details(ctx: typer.Context, task_id: str = typer.Option(..., "--task-id")) -> None:
    _call_api(ctx, "GET", f"/api/analysis/tasks/{task_id}/details")


@analysis_app.command("batch-status")
def analysis_batch_status(ctx: typer.Context, batch_id: str = typer.Option(..., "--batch-id")) -> None:
    _call_api(ctx, "GET", f"/api/analysis/batches/{batch_id}")


@analysis_app.command("cancel")
def analysis_cancel(
    ctx: typer.Context,
    task_id: str = typer.Option(..., "--task-id"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "取消分析任务")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(ctx, "POST", f"/api/analysis/tasks/{task_id}/cancel", payload={})


@analysis_app.command("delete")
def analysis_delete(
    ctx: typer.Context,
    task_id: str = typer.Option(..., "--task-id"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "删除分析任务")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(ctx, "DELETE", f"/api/analysis/tasks/{task_id}")


# Reports


@reports_app.command("list")
def reports_list(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(20, "--page-size", min=1, max=100),
    keyword: Optional[str] = typer.Option(None, "--keyword"),
    market: Optional[str] = typer.Option(None, "--market"),
    code: Optional[str] = typer.Option(None, "--code"),
    start_date: Optional[str] = typer.Option(None, "--start-date"),
    end_date: Optional[str] = typer.Option(None, "--end-date"),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/reports/list",
        params={
            "page": page,
            "page_size": page_size,
            "search_keyword": keyword,
            "market_filter": market,
            "stock_code": code,
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@reports_app.command("get")
def reports_get(ctx: typer.Context, report_id: str = typer.Option(..., "--report-id")) -> None:
    _call_api(ctx, "GET", f"/api/reports/{report_id}/detail")


@reports_app.command("content")
def reports_content(
    ctx: typer.Context,
    report_id: str = typer.Option(..., "--report-id"),
    module: str = typer.Option(..., "--module"),
) -> None:
    _call_api(ctx, "GET", f"/api/reports/{report_id}/content/{module}")


@reports_app.command("download")
def reports_download(
    ctx: typer.Context,
    report_id: str = typer.Option(..., "--report-id"),
    format_name: str = typer.Option("markdown", "--format"),
    output: Optional[Path] = typer.Option(None, "--output"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    options = _root_options(ctx)
    if format_name not in {"markdown", "json", "pdf", "docx"}:
        raise typer.BadParameter("format 必须是 markdown/json/pdf/docx")
    try:
        _identity, client = build_api_client(
            api_url=options.api_url,
            username=options.username,
            password=options.password,
            session_days=options.session_days,
            session_file=options.session_file,
            allow_remote=options.allow_remote_api,
            timeout_seconds=options.timeout_seconds,
        )
        with client:
            content, headers = client.download(
                f"/api/reports/{report_id}/download",
                params={"format": format_name},
                timeout_seconds=max(options.timeout_seconds, 300.0),
            )
        destination = output
        if destination is None:
            disposition = headers.get("content-disposition", "")
            match = re.search(r"filename=\"?([^\";]+)", disposition)
            fallback_extension = {"markdown": "md", "json": "json", "pdf": "pdf", "docx": "docx"}[format_name]
            destination = Path(match.group(1) if match else f"report-{report_id}.{fallback_extension}")
        destination = destination.expanduser().resolve()
        if destination.exists() and not overwrite:
            raise AgentCLIError(
                "输出文件已存在；使用 --overwrite 覆盖",
                code="output_exists",
                details={"output": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        _write_json(
            {
                "ok": True,
                "data": {
                    "report_id": report_id,
                    "format": format_name,
                    "output": str(destination),
                    "bytes": len(content),
                },
            },
            pretty=options.pretty,
        )
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=options.pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc


@reports_app.command("delete")
def reports_delete(
    ctx: typer.Context,
    report_id: str = typer.Option(..., "--report-id"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    try:
        _require_confirm(confirm, "删除分析报告")
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(exc.exit_code) from exc
    _call_api(ctx, "DELETE", f"/api/reports/{report_id}")


# Screening and stock data


@screening_app.command("fields")
def screening_fields(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/screening/fields")


@screening_app.command("industries")
def screening_industries(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/screening/industries")


@screening_app.command("run")
def screening_run(
    ctx: typer.Context,
    conditions_json: str = typer.Option("{}", "--conditions-json"),
    order_by_json: Optional[str] = typer.Option(None, "--order-by-json"),
    market: str = typer.Option("CN", "--market"),
    date: Optional[str] = typer.Option(None, "--date"),
    adj: str = typer.Option("qfq", "--adj"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
) -> None:
    conditions = _parse_payload(conditions_json, "--conditions-json")
    order_by = None
    if order_by_json:
        try:
            order_by = json.loads(order_by_json)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"order-by-json 无效: {exc.msg}") from exc
        if not isinstance(order_by, list):
            raise typer.BadParameter("order-by-json 必须是数组")
    _call_api(
        ctx,
        "POST",
        "/api/screening/run",
        payload={
            "market": market,
            "date": date,
            "adj": adj,
            "conditions": conditions,
            "order_by": order_by,
            "limit": limit,
            "offset": offset,
        },
    )


@stocks_app.command("search")
def stocks_search(
    ctx: typer.Context,
    keyword: str = typer.Option(..., "--keyword"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    _call_api(ctx, "GET", "/api/stock-data/search", params={"keyword": keyword, "limit": limit})


@stocks_app.command("info")
def stocks_info(ctx: typer.Context, code: str = typer.Option(..., "--code")) -> None:
    _call_api(ctx, "GET", f"/api/stock-data/combined/{code}")


@stocks_app.command("quote")
def stocks_quote(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    force_refresh: bool = typer.Option(False, "--force-refresh"),
) -> None:
    _call_api(
        ctx,
        "GET",
        f"/api/stocks/{code}/quote",
        params={"force_refresh": force_refresh},
    )


@stocks_app.command("fundamentals")
def stocks_fundamentals(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    source: Optional[str] = typer.Option(None, "--source"),
    force_refresh: bool = typer.Option(False, "--force-refresh"),
) -> None:
    _call_api(
        ctx,
        "GET",
        f"/api/stocks/{code}/fundamentals",
        params={"source": source, "force_refresh": force_refresh},
    )


@stocks_app.command("kline")
def stocks_kline(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    period: str = typer.Option("day", "--period"),
    limit: int = typer.Option(120, "--limit", min=1, max=2000),
    adj: str = typer.Option("none", "--adj"),
    force_refresh: bool = typer.Option(False, "--force-refresh"),
) -> None:
    _call_api(
        ctx,
        "GET",
        f"/api/stocks/{code}/kline",
        params={"period": period, "limit": limit, "adj": adj, "force_refresh": force_refresh},
    )


@stocks_app.command("news")
def stocks_news(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    days: int = typer.Option(30, "--days", min=1, max=365),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    announcements: bool = typer.Option(True, "--announcements/--no-announcements"),
) -> None:
    _call_api(
        ctx,
        "GET",
        f"/api/stocks/{code}/news",
        params={"days": days, "limit": limit, "include_announcements": announcements},
    )


# Current user and notifications


@profile_app.command("get")
def profile_get(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/auth/me")


@profile_app.command("update")
def profile_update(
    ctx: typer.Context,
    email: Optional[str] = typer.Option(None, "--email"),
    language: Optional[str] = typer.Option(None, "--language"),
    preferences_json: Optional[str] = typer.Option(None, "--preferences-json"),
) -> None:
    payload = _compact(
        {
            "email": email,
            "language": language,
            "preferences": (
                _parse_payload(preferences_json, "--preferences-json")
                if preferences_json
                else None
            ),
        }
    )
    if not payload:
        raise typer.BadParameter("至少提供一个更新字段")
    _call_api(ctx, "PUT", "/api/auth/me", payload=payload)


@notifications_app.command("list")
def notifications_list(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status"),
    notification_type: Optional[str] = typer.Option(None, "--type"),
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(20, "--page-size", min=1, max=100),
) -> None:
    _call_api(
        ctx,
        "GET",
        "/api/notifications",
        params={
            "status": status,
            "type": notification_type,
            "page": page,
            "page_size": page_size,
        },
    )


@notifications_app.command("unread-count")
def notifications_unread_count(ctx: typer.Context) -> None:
    _call_api(ctx, "GET", "/api/notifications/unread_count")


@notifications_app.command("read")
def notifications_read(
    ctx: typer.Context,
    notification_id: str = typer.Option(..., "--notification-id"),
) -> None:
    _call_api(ctx, "POST", f"/api/notifications/{notification_id}/read", payload={})


@notifications_app.command("read-all")
def notifications_read_all(ctx: typer.Context) -> None:
    _call_api(ctx, "POST", "/api/notifications/read_all", payload={})


# Admin action registry. All mutations require an explicit --confirm.


ADMIN_ACTIONS: Dict[str, Dict[str, tuple[str, str]]] = {
    "config": {
        "system": ("GET", "/api/config/system"),
        "settings": ("GET", "/api/config/settings"),
        "settings-meta": ("GET", "/api/config/settings/meta"),
        "update-settings": ("PUT", "/api/config/settings"),
        "providers": ("GET", "/api/config/llm/providers"),
        "datasources": ("GET", "/api/config/datasource"),
        "models": ("GET", "/api/config/model-catalog"),
        "databases": ("GET", "/api/config/database"),
        "reload": ("POST", "/api/config/reload"),
    },
    "sync": {
        "status": ("GET", "/api/sync/multi-source/status"),
        "sources": ("GET", "/api/sync/multi-source/sources/status"),
        "current-source": ("GET", "/api/sync/multi-source/sources/current"),
        "recommendations": ("GET", "/api/sync/multi-source/recommendations"),
        "history": ("GET", "/api/sync/multi-source/history"),
        "run": ("POST", "/api/sync/multi-source/stock_basics/run"),
        "test-sources": ("POST", "/api/sync/multi-source/test-sources"),
        "clear-cache": ("DELETE", "/api/sync/multi-source/cache"),
    },
    "cache": {
        "stats": ("GET", "/api/cache/stats"),
        "details": ("GET", "/api/cache/details"),
        "backend": ("GET", "/api/cache/backend-info"),
        "cleanup": ("DELETE", "/api/cache/cleanup"),
        "clear": ("DELETE", "/api/cache/clear"),
    },
    "database": {
        "status": ("GET", "/api/system/database/status"),
        "stats": ("GET", "/api/system/database/stats"),
        "backups": ("GET", "/api/system/database/backups"),
        "test": ("POST", "/api/system/database/test"),
        "backup": ("POST", "/api/system/database/backup"),
        "cleanup": ("POST", "/api/system/database/cleanup"),
        "cleanup-analysis": ("POST", "/api/system/database/cleanup/analysis"),
        "cleanup-logs": ("POST", "/api/system/database/cleanup/logs"),
    },
    "scheduler": {
        "jobs": ("GET", "/api/scheduler/jobs"),
        "stats": ("GET", "/api/scheduler/stats"),
        "health": ("GET", "/api/scheduler/health"),
        "history": ("GET", "/api/scheduler/history"),
        "executions": ("GET", "/api/scheduler/executions"),
        "job": ("GET", "/api/scheduler/jobs/{id}"),
        "trigger": ("POST", "/api/scheduler/jobs/{id}/trigger"),
        "pause": ("POST", "/api/scheduler/jobs/{id}/pause"),
        "resume": ("POST", "/api/scheduler/jobs/{id}/resume"),
        "job-history": ("GET", "/api/scheduler/jobs/{id}/history"),
        "cancel-execution": ("POST", "/api/scheduler/executions/{id}/cancel"),
        "delete-execution": ("DELETE", "/api/scheduler/executions/{id}"),
    },
    "logs": {
        "operations": ("GET", "/api/system/logs/list"),
        "operation-stats": ("GET", "/api/system/logs/stats"),
        "files": ("GET", "/api/system/system-logs/files"),
        "statistics": ("GET", "/api/system/system-logs/statistics"),
        "clear-operations": ("POST", "/api/system/logs/clear"),
        "delete-file": ("DELETE", "/api/system/system-logs/files/{id}"),
    },
    "usage": {
        "records": ("GET", "/api/usage/records"),
        "statistics": ("GET", "/api/usage/statistics"),
        "provider-cost": ("GET", "/api/usage/cost/by-provider"),
        "model-cost": ("GET", "/api/usage/cost/by-model"),
        "daily-cost": ("GET", "/api/usage/cost/daily"),
        "cleanup": ("DELETE", "/api/usage/records/old"),
    },
}


@admin_app.command("routes")
def admin_routes(ctx: typer.Context) -> None:
    routes = []
    for resource, actions in ADMIN_ACTIONS.items():
        for action, (method, path) in actions.items():
            routes.append(
                {
                    "resource": resource,
                    "action": action,
                    "method": method,
                    "path": path,
                    "requires_confirm": method not in {"GET", "HEAD"},
                    "requires_id": "{id}" in path,
                }
            )
    _write_json(
        {"ok": True, "data": {"routes": routes, "count": len(routes)}},
        pretty=_root_options(ctx).pretty,
    )


@admin_app.command("call")
def admin_call(
    ctx: typer.Context,
    resource: str = typer.Option(..., "--resource"),
    action: str = typer.Option(..., "--action"),
    resource_id: Optional[str] = typer.Option(None, "--id"),
    payload_json: Optional[str] = typer.Option(None, "--payload-json"),
    query_json: Optional[str] = typer.Option(None, "--query-json"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    route = ADMIN_ACTIONS.get(resource, {}).get(action)
    if route is None:
        error = AgentCLIError(
            "未知管理动作；先运行 agentctl admin routes",
            code="unknown_admin_action",
            details={"resource": resource, "action": action},
        )
        _write_json(error.payload(), pretty=_root_options(ctx).pretty, stderr=True)
        raise typer.Exit(error.exit_code)
    method, path = route
    if "{id}" in path:
        if not resource_id:
            raise typer.BadParameter("该动作必须提供 --id")
        path = path.replace("{id}", resource_id)
    if method not in {"GET", "HEAD"}:
        try:
            _require_confirm(confirm, f"管理动作 {resource}/{action}")
        except AgentCLIError as exc:
            _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
            raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        method,
        path,
        params=_parse_payload(query_json, "--query-json"),
        payload=_parse_payload(payload_json, "--payload-json") if method not in {"GET", "HEAD"} else None,
        require_admin=True,
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


@admin_app.command("raw")
def admin_raw(
    ctx: typer.Context,
    method: str = typer.Option(..., "--method"),
    path: str = typer.Option(..., "--path"),
    payload_json: Optional[str] = typer.Option(None, "--payload-json"),
    query_json: Optional[str] = typer.Option(None, "--query-json"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    normalized_method = method.upper()
    if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise typer.BadParameter("method 必须是 GET/POST/PUT/PATCH/DELETE")
    if not path.startswith("/api/") or ".." in path:
        raise typer.BadParameter("path 必须是 /api/ 下的绝对路径")
    if normalized_method != "GET":
        try:
            _require_confirm(confirm, f"原始管理请求 {normalized_method} {path}")
        except AgentCLIError as exc:
            _write_json(exc.payload(), pretty=_root_options(ctx).pretty, stderr=True)
            raise typer.Exit(exc.exit_code) from exc
    _call_api(
        ctx,
        normalized_method,
        path,
        params=_parse_payload(query_json, "--query-json"),
        payload=_parse_payload(payload_json, "--payload-json") if normalized_method != "GET" else None,
        require_admin=True,
        timeout_seconds=max(_root_options(ctx).timeout_seconds, 180.0),
    )


def _run(args: Optional[List[str]] = None) -> None:
    try:
        result = app(args=args, standalone_mode=False)
    except AgentCLIError as exc:
        _write_json(exc.payload(), pretty=False, stderr=True)
        raise SystemExit(exc.exit_code) from exc
    except ClickException as exc:
        _write_json(
            {
                "ok": False,
                "error": {"code": "invalid_cli_arguments", "message": exc.format_message()},
            },
            pretty=False,
            stderr=True,
        )
        raise SystemExit(exc.exit_code) from exc
    except Exception as exc:
        _write_json(
            {
                "ok": False,
                "error": {
                    "code": "internal_cli_error",
                    "message": "CLI 执行失败",
                    "details": {"error_type": type(exc).__name__},
                },
            },
            pretty=False,
            stderr=True,
        )
        raise SystemExit(1) from exc
    if isinstance(result, int) and result != 0:
        raise SystemExit(result)


def main() -> None:
    _run()


def holdings_main() -> None:
    """Compatibility entrypoint that keeps holdings behind unified auth."""
    value_options = {
        "--api-url",
        "--username",
        "--password",
        "--session-days",
        "--session-file",
        "--timeout",
    }
    flag_options = {"--pretty", "--allow-remote-api"}
    root_args: List[str] = []
    command_args: List[str] = []
    args = list(sys.argv[1:])
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in flag_options:
            root_args.append(argument)
        elif argument in value_options:
            root_args.append(argument)
            if index + 1 < len(args):
                index += 1
                root_args.append(args[index])
        elif any(argument.startswith(f"{option}=") for option in value_options):
            root_args.append(argument)
        else:
            command_args.append(argument)
        index += 1
    _run([*root_args, "holdings", *command_args])


if __name__ == "__main__":
    main()
