"""Machine-readable holdings CLI for local agents such as Hermes."""

from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

import typer
from click import ClickException as PublicClickException
from bson import ObjectId
from dotenv import dotenv_values
from pymongo import DESCENDING, MongoClient
from pymongo.errors import PyMongoError

try:
    from typer._click.exceptions import ClickException as TyperClickException
except ImportError:  # Typer versions before the vendored Click runtime.
    TyperClickException = PublicClickException

from app.services.a_share_market_regime import (
    assess_a_share_market_breadth,
    assess_a_share_market_regime,
    combine_a_share_market_regimes,
)
from app.services.holding_ai_advice import (
    apply_holding_price_guardrails,
    extract_report_price_plan,
    parse_report_recommendation,
)
from app.services.corporate_action_service import fetch_cn_dividend_calendar_sync
from app.services.candidate_discovery_service import discover_dynamic_candidate_universe
from app.services.holding_price_guardrails import (
    assess_recent_sale_cooldown,
    assess_report_freshness,
    build_technical_price_plan,
)
from app.services.holding_risk_sizing import (
    apply_net_reward_risk_gate,
    build_external_risk_gate,
    size_ashare_candidate,
)
from app.services.portfolio_target_analysis import build_target_analysis
from app.services.tencent_quote_service import (
    assess_cn_quote_freshness,
    fetch_tencent_daily_bars_sync,
    fetch_tencent_quote_sync,
    merge_tencent_quote_into_bars,
    normalize_cn_code,
)


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

DEFAULT_CLI_USERNAME = "admin"
DEFAULT_BUY_LOT_SIZE = 100
CN_MARKET_TIMEZONE = "Asia/Shanghai"
A_SHARE_REGIME_INDEX_SYMBOLS = ("sh000001", "sz399001", "sz399006", "sh000688")
DEFAULT_OPPORTUNITY_CANDIDATES = [
    {
        "code": "000066",
        "name": "中国长城",
        "theme": "ai_compute",
        "theme_label": "AI算力/信创",
        "priority": 1,
        "observation_zone": {"low": 18.8, "high": 19.3},
        "breakout_price": 19.68,
        "invalidation_price": 18.18,
        "note": "现金压力较小，观察信创/国产算力方向承接。",
    },
    {
        "code": "002261",
        "name": "拓维信息",
        "theme": "ai_compute",
        "theme_label": "AI算力/国产生态",
        "priority": 2,
        "observation_zone": {"low": 30.5, "high": 31.2},
        "breakout_price": 31.72,
        "invalidation_price": 29.68,
        "note": "AI生态弹性标的，先看分歧后的承接质量。",
    },
    {
        "code": "000938",
        "name": "紫光股份",
        "theme": "ai_compute",
        "theme_label": "AI算力/网络设备",
        "priority": 3,
        "observation_zone": {"low": 34.0, "high": 35.0},
        "breakout_price": 35.33,
        "invalidation_price": 32.82,
        "note": "主线强但一手接近占满当前现金，避免现金过度集中。",
    },
    {
        "code": "002185",
        "name": "华天科技",
        "theme": "semiconductor",
        "theme_label": "半导体封测",
        "priority": 4,
        "observation_zone": {"low": 22.8, "high": 23.3},
        "breakout_price": 23.73,
        "invalidation_price": 21.41,
        "note": "涨停后只看分歧承接，不把高开追涨作为观察条件。",
    },
    {
        "code": "600938",
        "name": "中国海油",
        "theme": "defensive_energy",
        "theme_label": "防守/能源",
        "priority": 5,
        "observation_zone": {"low": 28.6, "high": 29.2},
        "breakout_price": 29.24,
        "invalidation_price": 28.0,
        "note": "科技主线分歧或油价风险升温时的防守备选。",
    },
    {
        "code": "600900",
        "name": "长江电力",
        "theme": "defensive_yield",
        "theme_label": "防守/红利低波",
        "priority": 6,
        "observation_zone": {"low": 27.5, "high": 27.8},
        "breakout_price": 28.0,
        "invalidation_price": 27.2,
        "note": "低波防守备选，不作为进攻标的。",
    },
]
HOLDING_THEME_BY_CODE = {
    "000977": "ai_compute",
    "000066": "ai_compute",
    "000938": "ai_compute",
    "002261": "ai_compute",
    "002185": "semiconductor",
    "600938": "defensive_energy",
    "601857": "defensive_energy",
    "600900": "defensive_yield",
}


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


def _validate_cli_mongo_configuration(
    *,
    cwd: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Optional[str]]:
    working_directory = Path(cwd or Path.cwd()).resolve()
    environment = environ if environ is not None else os.environ
    environment_host = str(environment.get("MONGODB_HOST") or "").strip()
    environment_database = str(environment.get("MONGODB_DATABASE") or "").strip()
    if environment_host and environment_database:
        return {
            "source": "process_environment",
            "path": None,
            "expected_database": environment_database,
        }

    env_file = working_directory / ".env"
    repo_markers = (
        working_directory / "pyproject.toml",
        working_directory / "app" / "services" / "holdings_cli.py",
    )
    if env_file.is_file() and all(marker.is_file() for marker in repo_markers):
        file_values = dotenv_values(env_file)
        file_host = str(file_values.get("MONGODB_HOST") or "").strip()
        file_database = str(file_values.get("MONGODB_DATABASE") or "").strip()
        if file_host and file_database:
            return {
                "source": "cwd_env_file",
                "path": str(env_file),
                "expected_database": file_database,
            }

    raise CLIError(
        "仓库外运行 holdings 数据命令时，必须显式设置 MONGODB_HOST 和 MONGODB_DATABASE",
        code="mongo_config_required",
        exit_code=4,
    )


def _mongo_connection_values(
    configuration: Mapping[str, Optional[str]],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    source = configuration.get("source")
    if source == "process_environment":
        raw_values: Mapping[str, Any] = environ if environ is not None else os.environ
    elif source == "cwd_env_file":
        env_path = str(configuration.get("path") or "").strip()
        if not env_path:
            raise CLIError("MongoDB .env 路径缺失", code="mongo_config_invalid", exit_code=4)
        raw_values = dotenv_values(env_path)
    else:
        raise CLIError("MongoDB 配置来源无效", code="mongo_config_invalid", exit_code=4)
    return {
        str(key): str(value)
        for key, value in raw_values.items()
        if value is not None
    }


def _resolve_cli_mongo_host(
    host: str,
    *,
    configuration: Mapping[str, Optional[str]],
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    normalized_host = str(host or "").strip()
    environment = environ if environ is not None else os.environ
    in_container = str(environment.get("DOCKER_CONTAINER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if (
        configuration.get("source") == "cwd_env_file"
        and normalized_host.lower() == "mongodb"
        and not in_container
    ):
        return "127.0.0.1"
    return normalized_host


def _mongo_int(values: Mapping[str, str], key: str, default: int) -> int:
    raw_value = values.get(key)
    if raw_value in (None, ""):
        return default
    try:
        parsed = int(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise CLIError(
            f"MongoDB 配置 {key} 必须是整数",
            code="mongo_config_invalid",
            exit_code=4,
        ) from exc
    if parsed < 0:
        raise CLIError(
            f"MongoDB 配置 {key} 不能小于 0",
            code="mongo_config_invalid",
            exit_code=4,
        )
    return parsed


def _connect_cli_database(configuration: Mapping[str, Optional[str]]) -> Any:
    values = _mongo_connection_values(configuration)
    expected_database = str(configuration.get("expected_database") or "").strip()
    host = _resolve_cli_mongo_host(
        str(values.get("MONGODB_HOST") or ""),
        configuration=configuration,
    )
    configured_database = str(values.get("MONGODB_DATABASE") or "").strip()
    if not host or configured_database != expected_database:
        raise CLIError(
            "MongoDB 主机或数据库与已验证配置不一致",
            code="mongo_config_mismatch",
            exit_code=4,
        )

    client_options: Dict[str, Any] = {
        "host": host,
        "port": _mongo_int(values, "MONGODB_PORT", 27017),
        "maxPoolSize": _mongo_int(values, "MONGO_MAX_CONNECTIONS", 100),
        "minPoolSize": _mongo_int(values, "MONGO_MIN_CONNECTIONS", 10),
        "connectTimeoutMS": _mongo_int(values, "MONGO_CONNECT_TIMEOUT_MS", 30000),
        "socketTimeoutMS": _mongo_int(values, "MONGO_SOCKET_TIMEOUT_MS", 60000),
        "serverSelectionTimeoutMS": _mongo_int(
            values,
            "MONGO_SERVER_SELECTION_TIMEOUT_MS",
            5000,
        ),
    }
    username = str(values.get("MONGODB_USERNAME") or "").strip()
    password = str(values.get("MONGODB_PASSWORD") or "").strip()
    if username and password:
        client_options.update(
            {
                "username": username,
                "password": password,
                "authSource": str(values.get("MONGODB_AUTH_SOURCE") or "admin").strip(),
            }
        )
    client = MongoClient(**client_options)
    return client[expected_database]


def _get_database() -> Any:
    configuration = _validate_cli_mongo_configuration()
    expected_database = str(configuration["expected_database"] or "").strip()
    try:
        database = _connect_cli_database(configuration)
    except CLIError:
        raise
    except Exception as exc:  # pragma: no cover - covered by command integration in real runtime.
        raise CLIError(f"MongoDB 连接失败: {exc}", code="database_error", exit_code=4) from exc

    resolved_database = str(getattr(database, "name", "") or "").strip()
    if resolved_database != expected_database:
        raise CLIError(
            f"MongoDB 配置不一致: 期望 {expected_database}，实际连接 {resolved_database or 'unknown'}",
            code="mongo_config_mismatch",
            exit_code=4,
        )
    return database


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
    """Resolve one local user. Without a selector, CLI automation runs as admin."""
    if allow_env and _selector_count(username, email, user_id) == 0:
        env_selector = _selector_from_env()
        username = env_selector["username"]
        email = env_selector["email"]
        user_id = env_selector["user_id"]

    if _selector_count(username, email, user_id) > 1:
        raise CLIError("只能提供 username、email、user-id 其中一个用户选择器", code="ambiguous_selector")

    defaulted_to_admin = False
    if _selector_count(username, email, user_id) == 0:
        username = DEFAULT_CLI_USERNAME
        defaulted_to_admin = True

    if user_id:
        if not ObjectId.is_valid(user_id):
            raise CLIError(f"user-id 不是有效 ObjectId: {user_id}", code="invalid_user_id")
        user = db["users"].find_one({"_id": ObjectId(user_id)})
    elif username:
        user = db["users"].find_one({"username": username})
    elif email:
        user = db["users"].find_one({"email": email})
    else:  # pragma: no cover - selector count guarantees this branch is unreachable.
        user = None

    if not user:
        if defaulted_to_admin:
            raise CLIError(
                "默认 CLI 用户 admin 不存在，请先初始化 admin 用户，或显式传 --username、--email、--user-id",
                code="default_admin_not_found",
                exit_code=3,
            )
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


def _round_number(value: Any, digits: int = 2) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


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
    reference_source: str = "report",
) -> Dict[str, Any]:
    manual = _normalize_price(manual_price)
    report = _normalize_price(report_price)
    active = manual if manual is not None else report
    if manual is not None:
        active_source = "manual"
    elif report is not None:
        active_source = reference_source if reference_source in {"report", "technical"} else "report"
    else:
        active_source = "none"

    return {
        "key": key,
        "label": label,
        "tone": tone,
        "manual_price": manual,
        "report_price": report,
        "reference_source": active_source if manual is None else reference_source,
        "active_price": active,
        "active_source": active_source,
        "distance_pct": _price_distance_pct(active, current_price),
    }


def _with_price_plan(item: Dict[str, Any]) -> Dict[str, Any]:
    advice = item.get("ai_advice") if isinstance(item.get("ai_advice"), dict) else {}
    guardrail = advice.get("price_plan_guardrail") if isinstance(advice.get("price_plan_guardrail"), dict) else {}
    guarded_sources = guardrail.get("sources") if isinstance(guardrail.get("sources"), dict) else {}
    historical_report = (
        guardrail.get("historical_report_price_plan")
        if isinstance(guardrail.get("historical_report_price_plan"), dict)
        else {}
    )
    technical_plan = guardrail.get("technical_price_plan") if isinstance(guardrail.get("technical_price_plan"), dict) else {}
    rejected_report_fields = set(guardrail.get("rejected_report_fields") or [])
    report_is_fresh = bool(guardrail.get("report_freshness", {}).get("actionable"))

    def reference(field_name: str) -> tuple[Any, str]:
        source = guarded_sources.get(field_name, "report")
        if source != "manual":
            return advice.get(field_name), source
        report_value = historical_report.get(field_name)
        if report_is_fresh and field_name not in rejected_report_fields and report_value is not None:
            return report_value, "report"
        technical_value = technical_plan.get(field_name)
        if technical_value is not None:
            return technical_value, "technical"
        return None, "none"

    current_price = _normalize_price(item.get("current_price"))
    stop_reference, stop_source = reference("stop_loss_price")
    target_reference, target_source = reference("target_price")
    sell_reference, sell_source = reference("suggested_sell_price")
    buy_reference, buy_source = reference("suggested_buy_price")
    rows = [
        _build_price_plan_row(
            key="stop",
            label="止损",
            tone="danger",
            manual_price=item.get("manual_stop_loss_price"),
            report_price=stop_reference,
            current_price=current_price,
            reference_source=stop_source,
        ),
        _build_price_plan_row(
            key="target",
            label="目标",
            tone="success",
            manual_price=item.get("manual_target_price"),
            report_price=target_reference,
            current_price=current_price,
            reference_source=target_source,
        ),
        _build_price_plan_row(
            key="sell",
            label="卖出",
            tone="warning",
            manual_price=item.get("manual_sell_price"),
            report_price=sell_reference,
            current_price=current_price,
            reference_source=sell_source,
        ),
        _build_price_plan_row(
            key="buy",
            label="追入",
            tone="info",
            manual_price=item.get("manual_buy_price"),
            report_price=buy_reference,
            current_price=current_price,
            reference_source=buy_source,
        ),
    ]
    item["price_plan"] = {
        "rows": rows,
        "has_manual": any(row["manual_price"] is not None for row in rows),
        "has_report": any(row["report_price"] is not None and row["active_source"] == "report" for row in rows),
        "has_technical": any(row["report_price"] is not None and row["active_source"] == "technical" for row in rows),
        "has_active": any(row["active_price"] is not None for row in rows),
        "notes": item.get("price_plan_notes") or "",
        "updated_at": item.get("price_plan_updated_at"),
        "is_reference_only": True,
    }
    return item


def _resolve_quote_snapshot(db: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    code = str(item.get("code") or "").upper()
    market = str(item.get("market") or "CN").upper()
    if not code:
        return {
            "source": "missing_code",
            "price": None,
            "freshness": {"actionable": False, "status": "missing_code"},
        }

    if market == "CN":
        quote = fetch_tencent_quote_sync(code)
        if quote:
            snapshot = dict(quote)
            snapshot["freshness"] = assess_cn_quote_freshness(snapshot)
            for field_name in ("close", "price", "current_price"):
                price = _normalize_price(quote.get(field_name))
                if price is not None:
                    snapshot["price"] = price
                    snapshot.setdefault("close", price)
                    return snapshot

    stored_price = _normalize_price(item.get("current_price"))
    if stored_price is not None:
        snapshot = {"source": "stored_holding", "price": stored_price, "close": stored_price}
        snapshot["freshness"] = assess_cn_quote_freshness(snapshot)
        return snapshot

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
                    snapshot = {
                        "source": f"mongo.{collection_name}",
                        "price": price,
                        "close": price,
                    }
                    snapshot["freshness"] = assess_cn_quote_freshness(snapshot)
                    return snapshot

    return {
        "source": "unavailable",
        "price": None,
        "freshness": {"actionable": False, "status": "price_unavailable"},
    }


def _resolve_current_price(db: Any, item: Dict[str, Any]) -> Optional[float]:
    return _normalize_price(_resolve_quote_snapshot(db, item).get("price"))


def _with_current_price(db: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _resolve_quote_snapshot(db, item)
    item["quote_snapshot"] = snapshot
    current_price = _normalize_price(snapshot.get("price"))
    if current_price is not None:
        item["current_price"] = current_price
    return item


def _benchmark_session_dates() -> List[str]:
    result = fetch_tencent_daily_bars_sync("sh000001", min_rows=2)
    if not result.get("ok"):
        return []
    return sorted({str(bar.get("date")) for bar in result.get("bars", []) if bar.get("date")})


def _build_a_share_market_gate(
    benchmark_trade_date: Optional[str],
    *,
    db: Any = None,
) -> Dict[str, Any]:
    index_quotes: List[Dict[str, Any]] = []
    for symbol in A_SHARE_REGIME_INDEX_SYMBOLS:
        quote = fetch_tencent_quote_sync(symbol)
        if not quote:
            continue
        snapshot = dict(quote)
        snapshot["requested_symbol"] = symbol
        index_quotes.append(snapshot)
    resolved_benchmark_trade_date = benchmark_trade_date
    if not resolved_benchmark_trade_date:
        provider_trade_dates = sorted(
            {
                str(quote.get("trade_date"))
                for quote in index_quotes
                if quote.get("trade_date")
            }
        )
        if provider_trade_dates:
            resolved_benchmark_trade_date = provider_trade_dates[-1]
    index_regime = assess_a_share_market_regime(
        index_quotes,
        benchmark_trade_date=resolved_benchmark_trade_date,
    )

    market_quotes: List[Dict[str, Any]] = []
    breadth_load_error: Optional[str] = None
    if db is not None:
        projection = {
            "_id": 0,
            "code": 1,
            "name": 1,
            "pct_chg": 1,
            "trade_date": 1,
        }
        try:
            market_quotes = [dict(row) for row in db["market_quotes"].find({}, projection)]
        except Exception as exc:
            breadth_load_error = type(exc).__name__

    breadth_regime = assess_a_share_market_breadth(
        market_quotes,
        benchmark_trade_date=resolved_benchmark_trade_date,
    )
    breadth_regime["source"] = "mongo.market_quotes"
    if breadth_load_error:
        breadth_regime["load_error"] = breadth_load_error
    return combine_a_share_market_regimes(index_regime, breadth_regime)


def _with_technical_price_plan(item: Dict[str, Any]) -> Dict[str, Any]:
    market = str(item.get("market") or "CN").upper()
    quote_snapshot = item.get("quote_snapshot") if isinstance(item.get("quote_snapshot"), dict) else {}
    freshness = quote_snapshot.get("freshness") if isinstance(quote_snapshot.get("freshness"), dict) else {}
    if market != "CN":
        item["technical_price_plan"] = {"actionable": False, "status": "unsupported_market"}
        return item
    if not freshness.get("actionable"):
        item["technical_price_plan"] = {
            "actionable": False,
            "status": "quote_not_actionable",
            "quote_status": freshness.get("status"),
        }
        return item

    history = fetch_tencent_daily_bars_sync(str(item.get("code") or ""))
    if not history.get("ok"):
        item["technical_price_plan"] = {
            "actionable": False,
            "status": history.get("status") or "history_unavailable",
            "reason": history.get("reason"),
        }
        return item
    merged = merge_tencent_quote_into_bars(history.get("bars", []), quote_snapshot)
    if not merged.get("ok"):
        item["technical_price_plan"] = {
            "actionable": False,
            "status": merged.get("status") or "quote_merge_failed",
            "price_ratio": merged.get("price_ratio"),
        }
        return item

    plan = build_technical_price_plan(merged.get("bars", []), current_price=item.get("current_price"))
    plan["history_status"] = history.get("status")
    plan["quote_merge_action"] = merged.get("merge_action")
    item["technical_price_plan"] = plan
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
        "created_at": doc.get("created_at"),
        "model_info": doc.get("model_info"),
        "recommendation": doc.get("recommendation") or "",
        "decision": decision,
        "price_plan": extract_report_price_plan(reports),
    }


def _build_report_advice(
    db: Any,
    item: Dict[str, Any],
    *,
    benchmark_session_dates: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    report_meta = _latest_report_meta(db, str(item.get("code") or ""))
    if not report_meta:
        return None

    recommendation = report_meta.get("recommendation") or ""
    decision = report_meta.get("decision") if isinstance(report_meta.get("decision"), dict) else {}
    report_price_plan = report_meta.get("price_plan") if isinstance(report_meta.get("price_plan"), dict) else {}
    if not recommendation and not decision and not report_price_plan:
        return None

    advice = parse_report_recommendation(
        recommendation,
        current_price=item.get("current_price"),
        decision=decision,
        price_plan=report_price_plan,
    )
    report_freshness = assess_report_freshness(
        report_meta.get("analysis_date") or report_meta.get("created_at"),
        as_of=datetime.now(ZoneInfo(CN_MARKET_TIMEZONE)),
        benchmark_session_dates=benchmark_session_dates,
    )
    advice = apply_holding_price_guardrails(
        advice,
        item,
        report_plan=report_price_plan,
        report_freshness=report_freshness,
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


def _with_report_advice(
    db: Any,
    item: Dict[str, Any],
    *,
    benchmark_session_dates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    advice = _build_report_advice(db, item, benchmark_session_dates=benchmark_session_dates)
    if advice:
        item["ai_advice"] = advice
    elif isinstance(item.get("ai_advice"), dict):
        item["ai_advice"] = apply_holding_price_guardrails(
            item["ai_advice"],
            item,
            historical_price_plan_key="historical_model_price_plan",
        )
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
    technical_price_plan_count = sum(1 for item in items if item.get("price_plan", {}).get("has_technical"))
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
        "technical_price_plan_count": technical_price_plan_count,
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
    items = [_with_technical_price_plan(item) for item in items]
    if include_analysis:
        items = [_with_analysis(item) for item in items]
    benchmark_dates = _benchmark_session_dates()
    for item in items:
        quote_trade_date = item.get("quote_snapshot", {}).get("trade_date")
        if quote_trade_date and quote_trade_date not in benchmark_dates:
            benchmark_dates.append(quote_trade_date)
    benchmark_dates.sort()
    items = [
        _with_price_plan(
            _with_report_advice(db, item, benchmark_session_dates=benchmark_dates)
        )
        for item in items
    ]

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
            "schema_version": 3,
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


def _parse_trade_datetime(value: Any, *, assume_market_timezone: bool = True) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo(CN_MARKET_TIMEZONE) if assume_market_timezone else timezone.utc
        )
    return parsed.astimezone(timezone.utc)


def _normalize_sale_timestamp(value: Optional[str]) -> tuple[str, datetime]:
    if value in (None, ""):
        effective_at = datetime.now(timezone.utc)
    else:
        effective_at = _parse_trade_datetime(value)
        if effective_at is None:
            raise CLIError("sold-at 必须是有效 ISO 时间", code="invalid_sold_at")
    canonical = effective_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    return canonical, effective_at


def _trade_effective_datetime(trade: Dict[str, Any]) -> datetime:
    for field_name in ("effective_at", "sold_at", "created_at"):
        parsed = _parse_trade_datetime(
            trade.get(field_name),
            assume_market_timezone=field_name == "sold_at",
        )
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _sorted_trade_docs(docs: Iterable[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    ordered = sorted(
        (dict(doc) for doc in docs),
        key=_trade_effective_datetime,
        reverse=True,
    )
    return ordered[:limit]


def build_record_sale_payload(
    db: Any,
    *,
    code: str,
    quantity: int,
    sell_price: float,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    market: Optional[str] = None,
    fee: float = 0.0,
    sold_at: Optional[str] = None,
) -> Dict[str, Any]:
    user = select_user(db, username=username, email=email, user_id=user_id)
    normalized_code = str(code or "").strip().upper()
    normalized_market = str(market or "").strip().upper() or None
    if not normalized_code:
        raise CLIError("股票代码不能为空", code="invalid_code")
    if quantity <= 0:
        raise CLIError("卖出数量必须大于 0", code="invalid_quantity")
    if sell_price <= 0:
        raise CLIError("卖出价格必须大于 0", code="invalid_sell_price")
    if fee < 0:
        raise CLIError("费用不能小于 0", code="invalid_fee")

    query = {"user_id": user["id"], "code": normalized_code}
    if normalized_market:
        query["market"] = normalized_market
    holding = db["user_holdings"].find_one(query)
    if not holding:
        raise CLIError("未找到对应当前持仓，无法记录卖出", code="holding_not_found", exit_code=3)
    total_holding_cost_before_sale = sum(
        _holding_cost(item)
        for item in _iter_docs(db["user_holdings"].find({"user_id": user["id"]}))
    )

    holding_quantity = int(holding.get("quantity") or 0)
    if quantity > holding_quantity:
        raise CLIError(
            f"卖出数量超过当前持仓：持仓 {holding_quantity}，卖出 {quantity}",
            code="insufficient_holding_quantity",
        )

    cost_price = float(holding.get("cost_price") or 0)
    gross_amount = round(float(sell_price) * quantity, 2)
    cost_basis = round(cost_price * quantity, 2)
    total_fees = round(float(fee or 0), 2)
    net_proceeds = round(gross_amount - total_fees, 2)
    realized_pnl = round(net_proceeds - cost_basis, 2)
    realized_pnl_pct = round(realized_pnl / cost_basis * 100, 2) if cost_basis > 0 else None
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    sold_timestamp, effective_at = _normalize_sale_timestamp(sold_at)
    market_value = normalized_market or str(holding.get("market") or "CN").upper()
    holding_id = str(holding.get("_id") or holding.get("id") or "")

    trade_doc = {
        "user_id": user["id"],
        "holding_id": holding_id,
        "code": normalized_code,
        "name": holding.get("name") or normalized_code,
        "market": market_value,
        "side": "sell",
        "quantity": quantity,
        "sell_price": round(float(sell_price), 4),
        "gross_amount": gross_amount,
        "cost_price": round(cost_price, 4),
        "cost_basis": cost_basis,
        "fee": total_fees,
        "total_fees": total_fees,
        "net_proceeds": net_proceeds,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
        "sold_at": sold_timestamp,
        "effective_at": effective_at,
        "created_at": now,
        "is_reference_only": True,
    }
    result = db["user_holding_trades"].insert_one(trade_doc)
    trade_doc["_id"] = result.inserted_id

    remaining_quantity = holding_quantity - quantity
    remaining_holding = None
    if remaining_quantity > 0:
        db["user_holdings"].update_one(
            {"_id": holding["_id"], "user_id": user["id"]},
            {"$set": {"quantity": remaining_quantity, "updated_at": now}},
        )
        remaining_holding = _clean_doc({**holding, "quantity": remaining_quantity, "updated_at": now})
    else:
        db["user_holdings"].delete_one({"_id": holding["_id"], "user_id": user["id"]})

    settings_doc = db["user_holding_settings"].find_one({"user_id": user["id"]})
    previous_settings = _build_settings_payload(settings_doc, total_holding_cost_before_sale)
    previous_total_assets = float(previous_settings.get("total_assets") or 0)
    updated_total_assets = round(previous_total_assets + realized_pnl, 2)
    db["user_holding_settings"].update_one(
        {"user_id": user["id"]},
        {
            "$set": {
                "user_id": user["id"],
                "total_assets": updated_total_assets,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    settings = _build_settings_payload(db["user_holding_settings"].find_one({"user_id": user["id"]}))

    return {
        "ok": True,
        "data": {
            "user": user,
            "sale": _clean_doc(trade_doc),
            "remaining_holding": remaining_holding,
            "settings": settings,
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": "mongo.user_holdings+user_holding_trades",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def build_trades_payload(
    db: Any,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    code: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    user = select_user(db, username=username, email=email, user_id=user_id)
    query: Dict[str, Any] = {"user_id": user["id"]}
    if code:
        query["code"] = str(code).upper()
    cursor = db["user_holding_trades"].find(query)
    items = [_clean_doc(doc) for doc in _sorted_trade_docs(cursor, limit=limit)]
    return {
        "ok": True,
        "data": {
            "user": user,
            "items": items,
            "count": len(items),
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": "mongo.user_holding_trades",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def _recent_trades(db: Any, *, user_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    cursor = db["user_holding_trades"].find({"user_id": user_id})
    return [_clean_doc(doc) for doc in _sorted_trade_docs(cursor, limit=limit)]


def _build_trade_context(db: Any, *, user_id: str, limit: int = 5) -> Dict[str, Any]:
    trades = _recent_trades(db, user_id=user_id, limit=limit)
    realized_pnl = sum(float(trade.get("realized_pnl") or 0) for trade in trades)
    return {
        "recent_trades": trades,
        "recent_count": len(trades),
        "last_trade": trades[0] if trades else None,
        "recent_realized_pnl": round(realized_pnl, 2),
        "is_reference_only": True,
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def _estimated_equity(summary: Dict[str, Any]) -> Optional[float]:
    cash = _round_number(summary.get("cash_or_unallocated"))
    known_market_value = _round_number(summary.get("known_market_value"))
    if cash is not None and known_market_value is not None:
        return round(cash + known_market_value, 2)
    return _round_number(summary.get("total_assets"))


def _theme_for_code(code: Any) -> Optional[str]:
    return HOLDING_THEME_BY_CODE.get(str(code or "").upper())


def _risk_flag(key: str, level: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload = {"key": key, "level": level, "message": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _build_account_payload(summary: Dict[str, Any], settings: Dict[str, Any], buy_lot_size: int) -> Dict[str, Any]:
    estimated_equity = _estimated_equity(summary)
    return {
        "configured_total_assets": _round_number(settings.get("configured_total_assets")),
        "total_assets": _round_number(settings.get("total_assets")),
        "cash_or_unallocated": _round_number(summary.get("cash_or_unallocated")),
        "known_market_value": _round_number(summary.get("known_market_value")),
        "known_profit_loss": _round_number(summary.get("known_profit_loss")),
        "known_profit_loss_pct": _round_number(summary.get("known_profit_loss_pct")),
        "estimated_equity": estimated_equity,
        "buy_lot_size": buy_lot_size,
        "is_reference_only": True,
    }


def _build_holdings_risk(items: List[Dict[str, Any]], estimated_equity: Optional[float]) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    for item in items:
        market_value = _known_market_value(item)
        cost = _holding_cost(item)
        profit_loss = market_value - cost if market_value is not None else None
        profit_loss_pct = profit_loss / cost * 100 if profit_loss is not None and cost > 0 else None
        weight = market_value / estimated_equity * 100 if market_value is not None and estimated_equity else None
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        price_plan = item.get("price_plan") if isinstance(item.get("price_plan"), dict) else {}
        quote_snapshot = item.get("quote_snapshot") if isinstance(item.get("quote_snapshot"), dict) else {}
        quote_freshness = quote_snapshot.get("freshness") if isinstance(quote_snapshot.get("freshness"), dict) else {}
        valuation_actionable = bool(quote_freshness.get("actionable") and market_value is not None)
        item_flags: List[Dict[str, Any]] = []

        if weight is not None and weight >= 60:
            item_flags.append(
                _risk_flag(
                    "high_single_position_weight",
                    "warning",
                    "单只持仓占估算权益比例较高，优先关注回撤风险。",
                    weight_pct=round(weight, 2),
                )
            )

        progress = _round_number(analysis.get("monthly_target_progress_pct"))
        if progress is not None and progress >= 100:
            item_flags.append(
                _risk_flag(
                    "monthly_target_reached",
                    "info",
                    "月目标已达成，后续重点是保护已有浮盈。",
                    progress_pct=progress,
                )
            )

        if not price_plan.get("has_manual"):
            item_flags.append(
                _risk_flag(
                    "no_manual_price_plan",
                    "info",
                    "尚未设置手动价格计划，当前主要依赖报告抽取价位。",
                )
            )

        risks.append(
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "market": item.get("market"),
                "theme": _theme_for_code(item.get("code")),
                "quantity": item.get("quantity"),
                "cost_price": _round_number(item.get("cost_price"), 4),
                "current_price": _round_number(item.get("current_price"), 4),
                "market_value": _round_number(market_value),
                "profit_loss": _round_number(profit_loss),
                "profit_loss_pct": _round_number(profit_loss_pct),
                "weight_by_estimated_equity_pct": _round_number(weight),
                "analysis_action": analysis.get("action"),
                "analysis_status": analysis.get("status"),
                "target_monthly_return_pct": _round_number(item.get("target_monthly_return_pct")),
                "monthly_target_progress_pct": progress,
                "price_plan": price_plan,
                "quote_freshness": quote_freshness,
                "valuation_actionable": valuation_actionable,
                "risk_flags": item_flags,
                "is_reference_only": True,
            }
        )
    return risks


def _resolve_actionable_equity(
    account: Dict[str, Any],
    holdings_risk: List[Dict[str, Any]],
) -> Dict[str, Any]:
    configured = _round_number(account.get("configured_total_assets"))
    if configured is not None and configured > 0:
        return {
            "value": configured,
            "status": "configured_total_assets",
            "actionable": True,
            "reason": "使用用户已配置总资产作为风险预算分母。",
        }
    if holdings_risk and all(item.get("valuation_actionable") for item in holdings_risk):
        cash = _round_number(account.get("cash_or_unallocated")) or 0.0
        market_value = sum(float(item.get("market_value") or 0) for item in holdings_risk)
        return {
            "value": round(cash + market_value, 2),
            "status": "fresh_mark_to_market",
            "actionable": True,
            "reason": "全部持仓具有可执行腾讯行情，使用现金加新鲜市值。",
        }
    return {
        "value": None,
        "status": "incomplete_actionable_valuation",
        "actionable": False,
        "reason": "未配置总资产且持仓估值不完整，禁止生成仓位数量。",
    }


def _candidate_definitions(candidate_codes: Optional[List[str]]) -> List[Dict[str, Any]]:
    defaults_by_code = {item["code"]: dict(item) for item in DEFAULT_OPPORTUNITY_CANDIDATES}
    if not candidate_codes:
        return []

    definitions: List[Dict[str, Any]] = []
    seen_codes = set()
    for raw_code in candidate_codes:
        code = normalize_cn_code(str(raw_code or ""))
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        definitions.append(
            defaults_by_code.get(
                code,
                {
                    "code": code,
                    "name": code,
                    "theme": _theme_for_code(code) or "custom",
                    "theme_label": "自定义观察",
                    "priority": 99,
                    "observation_zone": None,
                    "breakout_price": None,
                    "invalidation_price": None,
                    "note": "自定义候选，仅补充实时行情和资金约束。",
                },
            )
        )
    return definitions


def _corporate_action_marker(name: Any) -> Optional[str]:
    normalized = str(name or "").strip().upper()
    for marker in ("XD", "XR", "DR"):
        if normalized.startswith(marker):
            return marker
    return None


def _has_complete_candidate_price_plan(definition: Dict[str, Any]) -> bool:
    observation_zone = definition.get("observation_zone")
    if not isinstance(observation_zone, dict):
        return False
    required_prices = (
        observation_zone.get("low"),
        observation_zone.get("high"),
        definition.get("breakout_price"),
        definition.get("invalidation_price"),
    )
    return all(_normalize_price(value) is not None for value in required_prices)


def _has_complete_technical_price_plan(price_plan: Dict[str, Any]) -> bool:
    required_prices = (
        price_plan.get("stop_loss_price"),
        price_plan.get("suggested_buy_price"),
        price_plan.get("target_price"),
    )
    return all(_normalize_price(value) is not None for value in required_prices)


def _quote_snapshot(quote: Dict[str, Any], definition: Dict[str, Any]) -> Dict[str, Any]:
    price = _round_number(quote.get("price") or quote.get("close") or quote.get("current_price"), 4)
    high = _round_number(quote.get("high"), 4)
    low = _round_number(quote.get("low"), 4)
    provider_name = quote.get("name") or definition.get("name")
    corporate_action_marker = _corporate_action_marker(provider_name)
    intraday_range_pct = (
        round((high - low) / price * 100, 2)
        if high is not None and low is not None and price is not None and price > 0 and high >= low
        else None
    )
    snapshot = {
        "source": quote.get("source") or quote.get("data_source"),
        "provider_timestamp": quote.get("provider_timestamp"),
        "trade_at": quote.get("trade_at"),
        "trade_date": quote.get("trade_date"),
        "received_at": quote.get("received_at") or quote.get("updated_at"),
        "code": quote.get("code") or definition.get("code"),
        "name": provider_name,
        "price": price,
        "pct_chg": _round_number(quote.get("pct_chg")),
        "change": _round_number(quote.get("change")),
        "open": _round_number(quote.get("open"), 4),
        "high": high,
        "low": low,
        "pre_close": _round_number(quote.get("pre_close"), 4),
        "amount": _round_number(quote.get("amount")),
        "turnover_rate": _round_number(quote.get("turnover_rate")),
        "volume_ratio": _round_number(quote.get("volume_ratio")),
        "intraday_range_pct": intraday_range_pct,
        "price_plan_adjustment_required": corporate_action_marker is not None,
    }
    snapshot["freshness"] = assess_cn_quote_freshness(snapshot)
    if corporate_action_marker:
        snapshot["corporate_action_marker"] = corporate_action_marker
    return snapshot


def _distance_pct(target_price: Optional[float], current_price: Optional[float]) -> Optional[float]:
    if target_price is None or current_price is None or current_price <= 0:
        return None
    return round((target_price - current_price) / current_price * 100, 2)


def _trigger_status(
    definition: Dict[str, Any],
    current_price: Optional[float],
    *,
    price_plan_adjustment_required: bool = False,
) -> Dict[str, Any]:
    observation_zone = definition.get("observation_zone") if isinstance(definition.get("observation_zone"), dict) else {}
    observation_low = _round_number(observation_zone.get("low"), 4)
    observation_high = _round_number(observation_zone.get("high"), 4)
    breakout_price = _round_number(definition.get("breakout_price"), 4)
    invalidation_price = _round_number(definition.get("invalidation_price"), 4)

    if price_plan_adjustment_required:
        return {
            "position": "price_plan_adjustment_required",
            "breakout_status": "price_plan_adjustment_required",
            "distance_to_observation_low_pct": None,
            "distance_to_observation_high_pct": None,
            "distance_to_breakout_pct": None,
            "distance_to_invalidation_pct": None,
        }

    position = "unknown"
    if current_price is not None and observation_low is not None and observation_high is not None:
        if observation_low <= current_price <= observation_high:
            position = "inside_observation_zone"
        elif current_price < observation_low:
            position = "below_observation_zone"
        else:
            position = "above_observation_zone"

    breakout_status = "unknown"
    if current_price is not None and breakout_price is not None:
        breakout_status = "above_breakout" if current_price >= breakout_price else "below_breakout"

    return {
        "position": position,
        "breakout_status": breakout_status,
        "distance_to_observation_low_pct": _distance_pct(observation_low, current_price),
        "distance_to_observation_high_pct": _distance_pct(observation_high, current_price),
        "distance_to_breakout_pct": _distance_pct(breakout_price, current_price),
        "distance_to_invalidation_pct": _distance_pct(invalidation_price, current_price),
    }


def _build_opportunity_candidates(
    definitions: List[Dict[str, Any]],
    *,
    cash: Optional[float],
    buy_lot_size: int,
    holding_themes: set,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for definition in definitions:
        code = str(definition.get("code") or "").upper()
        quote = fetch_tencent_quote_sync(code) or {}
        snapshot = _quote_snapshot(quote, definition)
        corporate_action = fetch_cn_dividend_calendar_sync(code)
        upcoming_price_adjustment_required = bool(
            corporate_action.get("price_plan_adjustment_required")
        )
        price = snapshot.get("price")
        technical_plan: Dict[str, Any] = {
            "actionable": False,
            "status": "quote_not_actionable",
            "quote_status": snapshot.get("freshness", {}).get("status"),
        }
        if snapshot.get("freshness", {}).get("actionable"):
            history = fetch_tencent_daily_bars_sync(code)
            if history.get("ok"):
                merged = merge_tencent_quote_into_bars(history.get("bars", []), snapshot)
                if merged.get("ok"):
                    technical_plan = build_technical_price_plan(
                        merged.get("bars", []),
                        current_price=price,
                    )
                    technical_plan["history_status"] = history.get("status")
                    technical_plan["quote_merge_action"] = merged.get("merge_action")
                else:
                    technical_plan = {
                        "actionable": False,
                        "status": merged.get("status") or "quote_merge_failed",
                        "price_ratio": merged.get("price_ratio"),
                    }
            else:
                technical_plan = {
                    "actionable": False,
                    "status": history.get("status") or "history_unavailable",
                    "reason": history.get("reason"),
                }
        technical_plan = apply_net_reward_risk_gate(
            technical_plan,
            quantity=buy_lot_size,
        )
        one_lot_amount = round(price * buy_lot_size, 2) if price is not None else None
        affordable = bool(cash is not None and one_lot_amount is not None and one_lot_amount <= cash)
        same_theme = bool(definition.get("theme") in holding_themes)
        cash_after_one_lot = round(cash - one_lot_amount, 2) if affordable and cash is not None and one_lot_amount is not None else None
        cash_usage_pct = round(one_lot_amount / cash * 100, 2) if cash and one_lot_amount is not None else None
        candidate_flags: List[Dict[str, Any]] = []
        if (
            not technical_plan.get("actionable")
            and not _has_complete_candidate_price_plan(definition)
            and not _has_complete_technical_price_plan(technical_plan)
        ):
            candidate_flags.append(
                _risk_flag(
                    "missing_candidate_price_plan",
                    "warning",
                    "候选股缺少完整观察区、突破价或失效价，不能生成仓位参考。",
                )
            )
        if not snapshot.get("freshness", {}).get("actionable"):
            candidate_flags.append(
                _risk_flag(
                    "quote_not_actionable",
                    "warning",
                    "腾讯提供方成交时间不满足时效门禁，不能生成仓位数量。",
                    quote_status=snapshot.get("freshness", {}).get("status"),
                )
            )
        elif not technical_plan.get("actionable"):
            candidate_flags.append(
                _risk_flag(
                    "technical_plan_not_actionable",
                    "warning",
                    "腾讯前复权日线未形成可执行价格计划，不能生成仓位数量。",
                    plan_status=technical_plan.get("status"),
                )
            )
        if snapshot.get("price_plan_adjustment_required") or upcoming_price_adjustment_required:
            nearest_action = (
                corporate_action.get("nearest_action")
                if isinstance(corporate_action.get("nearest_action"), dict)
                else {}
            )
            candidate_flags.append(
                _risk_flag(
                    "corporate_action_price_adjustment",
                    "warning",
                    (
                        "未来两个交易日内存在除权除息事件，价格计划需按新口径校准。"
                        if upcoming_price_adjustment_required
                        else "行情处于除权除息口径，静态观察区、突破价和失效价需要复权校准。"
                    ),
                    marker=snapshot.get("corporate_action_marker"),
                    provider_name=snapshot.get("name"),
                    record_date=nearest_action.get("record_date"),
                    ex_date=nearest_action.get("ex_date"),
                    sessions_until_ex_date=corporate_action.get("sessions_until_ex_date"),
                )
            )
        elif corporate_action.get("status") == "upcoming_corporate_action":
            nearest_action = corporate_action.get("nearest_action") or {}
            candidate_flags.append(
                _risk_flag(
                    "upcoming_corporate_action",
                    "info",
                    "未来五个交易日内存在公司行动，暂不阻断当前两日观察，但需在除权前重算价格计划。",
                    record_date=nearest_action.get("record_date"),
                    ex_date=nearest_action.get("ex_date"),
                    sessions_until_ex_date=corporate_action.get("sessions_until_ex_date"),
                )
            )
        elif corporate_action.get("status") == "corporate_action_unavailable":
            candidate_flags.append(
                _risk_flag(
                    "corporate_action_data_unavailable",
                    "info",
                    "公司行动日历暂不可用，当前结果不能证明未来没有除权除息事件。",
                    reason=corporate_action.get("reason"),
                )
            )
        if cash_usage_pct is not None and not affordable:
            candidate_flags.append(
                _risk_flag(
                    "insufficient_cash",
                    "warning",
                    "一手金额超过当前可用现金。",
                    cash_usage_pct=cash_usage_pct,
                )
            )
        elif cash_usage_pct is not None and cash_usage_pct >= 90:
            candidate_flags.append(
                _risk_flag(
                    "low_cash_buffer",
                    "warning",
                    "买入一手后现金缓冲很低。",
                    cash_usage_pct=cash_usage_pct,
                    cash_after_one_lot=cash_after_one_lot,
                )
            )
        if same_theme:
            candidate_flags.append(
                _risk_flag(
                    "same_theme_with_holdings",
                    "warning",
                    "候选股与现有持仓属于同一主题，可能放大同向波动。",
                )
            )
        if snapshot.get("pct_chg") is not None and snapshot["pct_chg"] >= 9.8:
            candidate_flags.append(
                _risk_flag(
                    "limit_up_or_hot_move",
                    "warning",
                    "候选股当日涨幅接近或达到涨停，只适合观察分歧承接。",
                    pct_chg=snapshot.get("pct_chg"),
                )
            )
        if snapshot.get("turnover_rate") is not None and snapshot["turnover_rate"] >= 10:
            candidate_flags.append(
                _risk_flag(
                    "high_turnover",
                    "warning",
                    "候选股换手率偏高，说明分歧较强，需确认承接后再评估。",
                    turnover_rate=snapshot.get("turnover_rate"),
                )
            )
        if snapshot.get("intraday_range_pct") is not None and snapshot["intraday_range_pct"] >= 8:
            candidate_flags.append(
                _risk_flag(
                    "wide_intraday_range",
                    "warning",
                    "候选股日内振幅偏大，需警惕冲高回落和追高风险。",
                    intraday_range_pct=snapshot.get("intraday_range_pct"),
                )
            )
        candidates.append(
            {
                "code": code,
                "name": snapshot.get("name") or definition.get("name"),
                "theme": definition.get("theme"),
                "theme_label": definition.get("theme_label"),
                "priority": definition.get("priority"),
                "discovery": definition.get("discovery"),
                "quote": snapshot,
                "buy_lot_size": buy_lot_size,
                "one_lot_amount": one_lot_amount,
                "cash_usage_pct": cash_usage_pct,
                "affordable_with_cash": affordable,
                "cash_after_one_lot": cash_after_one_lot,
                "same_theme_with_holdings": same_theme,
                "guarded_price_plan": technical_plan,
                "corporate_action": corporate_action,
                "risk_flags": candidate_flags,
                "triggers": {
                    "source": (
                        "mongo_dynamic_discovery"
                        if definition.get("discovery")
                        else "configured_historical_reference"
                    ),
                    "observation_zone": definition.get("observation_zone"),
                    "breakout_price": definition.get("breakout_price"),
                    "invalidation_price": definition.get("invalidation_price"),
                    "status": _trigger_status(
                        definition,
                        price,
                        price_plan_adjustment_required=bool(
                            snapshot.get("price_plan_adjustment_required")
                            or upcoming_price_adjustment_required
                        ),
                    ),
                    "note": definition.get("note"),
                    "is_reference_only": True,
                },
                "is_reference_only": True,
            }
        )
    return sorted(candidates, key=lambda item: item.get("priority") or 99)


def _build_opportunity_risk_flags(
    holdings_risk: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    account: Dict[str, Any],
) -> List[Dict[str, Any]]:
    flags: List[Dict[str, Any]] = []
    seen = set()

    def add(flag: Dict[str, Any]) -> None:
        key = flag.get("key")
        if key in seen:
            return
        seen.add(key)
        flags.append(flag)

    for item in holdings_risk:
        for flag in item.get("risk_flags", []):
            add(flag)

    if any(candidate.get("same_theme_with_holdings") for candidate in candidates):
        add(
            _risk_flag(
                "technology_concentration",
                "warning",
                "候选股与现有持仓存在同主题暴露，新增前需先评估科技仓位集中度。",
            )
        )

    high_turnover_candidates = [
        candidate.get("code")
        for candidate in candidates
        if any(flag.get("key") == "high_turnover" for flag in candidate.get("risk_flags", []))
    ]
    if high_turnover_candidates:
        add(
            _risk_flag(
                "candidate_high_turnover",
                "warning",
                "候选池存在高换手标的，说明分歧较强，需等待承接确认。",
                candidate_codes=high_turnover_candidates,
            )
        )

    wide_range_candidates = [
        candidate.get("code")
        for candidate in candidates
        if any(flag.get("key") == "wide_intraday_range" for flag in candidate.get("risk_flags", []))
    ]
    if wide_range_candidates:
        add(
            _risk_flag(
                "candidate_wide_intraday_range",
                "warning",
                "候选池存在日内大振幅标的，需警惕冲高回落和追高风险。",
                candidate_codes=wide_range_candidates,
            )
        )

    corporate_action_candidates = [
        candidate.get("code")
        for candidate in candidates
        if any(
            flag.get("key") == "corporate_action_price_adjustment"
            for flag in candidate.get("risk_flags", [])
        )
    ]
    if corporate_action_candidates:
        add(
            _risk_flag(
                "candidate_price_plan_adjustment_required",
                "warning",
                "候选池存在除权除息标的，旧价格计划需复权校准后才能评估。",
                candidate_codes=corporate_action_candidates,
            )
        )

    missing_price_plan_candidates = [
        candidate.get("code")
        for candidate in candidates
        if any(flag.get("key") == "missing_candidate_price_plan" for flag in candidate.get("risk_flags", []))
    ]
    if missing_price_plan_candidates:
        add(
            _risk_flag(
                "candidate_price_plan_required",
                "warning",
                "候选池存在缺少完整价格计划的标的，不能生成仓位参考。",
                candidate_codes=missing_price_plan_candidates,
            )
        )

    cash = account.get("cash_or_unallocated")
    affordable_count = sum(1 for candidate in candidates if candidate.get("affordable_with_cash"))
    if cash is not None and affordable_count < len(candidates):
        add(
            _risk_flag(
                "limited_cash",
                "info",
                "部分候选股一手金额超过当前可用现金，需先过滤资金不可达标的。",
                affordable_count=affordable_count,
                candidate_count=len(candidates),
            )
        )

    return flags


def _candidate_watch_condition(candidate: Dict[str, Any]) -> str:
    status = candidate.get("triggers", {}).get("status", {})
    if status.get("position") == "price_plan_adjustment_required":
        return "处于除权除息口径，先复权校准观察区、突破价和失效价。"
    breakout_status = status.get("breakout_status")
    if breakout_status == "above_breakout":
        return "已站上突破价，观察能否站稳突破位并避免放量回落。"

    position = status.get("position")
    if position == "inside_observation_zone":
        return "观察区内，重点看承接和量能确认。"
    if position == "below_observation_zone":
        return "低于观察区，先等重新站回观察区。"
    if position == "above_observation_zone":
        return "高于观察区，等回踩承接或突破确认。"
    return "行情或观察区数据不完整，先补齐数据。"


def _build_watch_plan(
    holdings_risk: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    primary_holding = max(
        holdings_risk,
        key=lambda item: item.get("weight_by_estimated_equity_pct") or 0,
        default=None,
    )
    if primary_holding:
        holding_focus = {
            "code": primary_holding.get("code"),
            "name": primary_holding.get("name"),
            "priority": (
                "protect_profit"
                if (primary_holding.get("monthly_target_progress_pct") or 0) >= 100
                else "monitor_position_risk"
            ),
            "watch": "先看持仓是否出现高位分歧、放量回落或跌破报告/手动价格计划。",
            "note": "仅供研究参考，不构成投资建议或交易指令。",
        }
    else:
        holding_focus = {
            "code": None,
            "name": None,
            "priority": "cash_deployment",
            "watch": "当前空仓，重点观察候选股是否满足突破确认、回踩承接和资金分批部署条件。",
            "note": "仅供研究参考，不构成投资建议或交易指令。",
        }

    candidate_focus = [
        {
            "code": candidate.get("code"),
            "name": candidate.get("name"),
            "condition": _candidate_watch_condition(candidate),
            "avoid": "不因接近观察区就自动买入。",
            "risk_keys": [flag.get("key") for flag in candidate.get("risk_flags", [])],
        }
        for candidate in candidates[:3]
    ]

    return {
        "horizon": "未来两个交易日",
        "holding_focus": holding_focus,
        "candidate_focus": candidate_focus,
    }


def _build_cash_deployment_plan(
    account: Dict[str, Any],
    holdings_risk: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    market_session: Optional[Dict[str, Any]] = None,
    external_risk_gate: Optional[Dict[str, Any]] = None,
    a_share_market_gate: Optional[Dict[str, Any]] = None,
    actionable_equity: Optional[Dict[str, Any]] = None,
    recent_sale_policy: Optional[Dict[str, Any]] = None,
    observation_only_codes: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    cash = _round_number(account.get("cash_or_unallocated"))
    market_session = market_session or {}
    requires_quote_refresh = bool(market_session.get("quote_stale_risk"))
    initial_deploy_cap_pct = 50.0
    reserve_cash_pct = 50.0
    max_single_candidate_pct = 35.0
    initial_deploy_cap_amount = round(cash * initial_deploy_cap_pct / 100, 2) if cash is not None else None
    max_single_candidate_amount = round(cash * max_single_candidate_pct / 100, 2) if cash is not None else None
    remaining_initial_cap = initial_deploy_cap_amount
    actionable_equity = actionable_equity or {"value": None, "actionable": False}
    equity_value = _round_number(actionable_equity.get("value"))
    external_risk_gate = external_risk_gate or build_external_risk_gate(
        "unknown",
        actionable_equity=equity_value,
    )
    a_share_market_gate = a_share_market_gate or {
        "status": "market_data_unavailable",
        "level": "unknown",
        "new_position_allowed": False,
        "max_new_exposure_multiplier": 0.0,
        "reason": "A股主要指数状态未确认，失败关闭。",
    }
    market_multiplier = _round_number(
        a_share_market_gate.get("max_new_exposure_multiplier"),
        4,
    )
    market_multiplier = min(max(market_multiplier or 0.0, 0.0), 1.0)
    external_new_exposure = _round_number(external_risk_gate.get("max_new_exposure_amount")) or 0.0
    remaining_new_exposure = round(external_new_exposure * market_multiplier, 2)
    total_loss_budget = round(equity_value * 0.0075, 2) if equity_value is not None else 0.0
    remaining_loss_budget = total_loss_budget
    remaining_cash = cash or 0.0
    planned_symbol_market_values: Dict[str, float] = {}
    recent_sale_cooldown_codes = {
        str(code or "").upper()
        for code in (recent_sale_policy or {}).get("matched_candidate_codes", [])
        if (recent_sale_policy or {}).get("status") == "cooldown"
    }
    observation_only_code_set = {
        str(code or "").upper()
        for code in (observation_only_codes or [])
        if str(code or "").strip()
    }

    candidate_lot_plan: List[Dict[str, Any]] = []
    for candidate in candidates:
        one_lot_amount = _round_number(candidate.get("one_lot_amount"))
        triggers = candidate.get("triggers") if isinstance(candidate.get("triggers"), dict) else {}
        status = triggers.get("status") if isinstance(triggers.get("status"), dict) else {}
        breakout_status = status.get("breakout_status")
        if breakout_status == "above_breakout":
            reference_entry_status = "watch_after_breakout"
        elif status.get("position") == "inside_observation_zone":
            reference_entry_status = "watch_support"
        else:
            reference_entry_status = "wait"
        within_single_cap = (
            cash is not None
            and one_lot_amount is not None
            and max_single_candidate_amount is not None
            and one_lot_amount <= max_single_candidate_amount
        )
        has_initial_capacity = (
            remaining_initial_cap is not None
            and one_lot_amount is not None
            and one_lot_amount <= remaining_initial_cap
        )
        risk_keys = {flag.get("key") for flag in candidate.get("risk_flags", [])}
        blocked_by_price_plan_adjustment = "corporate_action_price_adjustment" in risk_keys
        blocked_by_missing_price_plan = "missing_candidate_price_plan" in risk_keys
        blocked_by_hot_move = "limit_up_or_hot_move" in risk_keys
        blocked_by_divergence = bool({"high_turnover", "wide_intraday_range"} & risk_keys)
        candidate_code_upper = str(candidate.get("code") or "").upper()
        blocked_by_recent_sale = candidate_code_upper in recent_sale_cooldown_codes
        blocked_by_observation_only = candidate_code_upper in observation_only_code_set
        if blocked_by_observation_only:
            risk_gate = "observation_only"
        elif blocked_by_recent_sale:
            risk_gate = "blocked_by_recent_sale_cooldown"
        elif blocked_by_price_plan_adjustment:
            risk_gate = "blocked_by_price_plan_adjustment"
        elif blocked_by_missing_price_plan:
            risk_gate = "blocked_by_missing_price_plan"
        elif blocked_by_hot_move:
            risk_gate = "blocked_by_hot_move"
        elif blocked_by_divergence:
            risk_gate = "blocked_by_divergence"
        else:
            risk_gate = "pass"
        base_failed_gates: List[str] = []
        if blocked_by_observation_only:
            base_failed_gates.append("observation_only_fallback")
        if blocked_by_recent_sale:
            base_failed_gates.append("recent_sale_cooldown")
        if blocked_by_price_plan_adjustment:
            base_failed_gates.append("corporate_action_price_adjustment")
        if blocked_by_missing_price_plan:
            base_failed_gates.append("missing_candidate_price_plan")
        if blocked_by_hot_move:
            base_failed_gates.append("limit_up_or_hot_move")
        if blocked_by_divergence:
            base_failed_gates.append("high_divergence")
        if not external_risk_gate.get("actionable"):
            base_failed_gates.append("external_risk_gate")
        if not a_share_market_gate.get("new_position_allowed"):
            base_failed_gates.append("a_share_market_gate")
        quote_freshness = candidate.get("quote", {}).get("freshness", {})
        if not quote_freshness.get("actionable"):
            base_failed_gates.append("quote_freshness")
        guarded_plan = candidate.get("guarded_price_plan") if isinstance(candidate.get("guarded_price_plan"), dict) else {}
        if not guarded_plan.get("actionable"):
            base_failed_gates.append("technical_price_plan")

        existing_holdings = [
            item
            for item in holdings_risk
            if item.get("code") == candidate.get("code")
        ]
        if not existing_holdings:
            existing_symbol_market_value: Optional[float] = 0.0
        elif all(
            item.get("valuation_actionable") and item.get("market_value") is not None
            for item in existing_holdings
        ):
            existing_symbol_market_value = round(
                sum(float(item.get("market_value") or 0) for item in existing_holdings),
                2,
            )
        else:
            existing_symbol_market_value = None
        candidate_code = str(candidate.get("code") or "")
        if existing_symbol_market_value is not None:
            existing_symbol_market_value += planned_symbol_market_values.get(candidate_code, 0.0)

        executable = {
            "entry": _normalize_price(guarded_plan.get("suggested_buy_price")),
            "stop": _normalize_price(guarded_plan.get("stop_loss_price")),
            "target": _normalize_price(guarded_plan.get("target_price")),
        }
        entry_status = (
            "conditional_guarded_plan"
            if guarded_plan.get("actionable")
            else reference_entry_status
        )
        confirm_text = (
            "仅在腾讯实时价格接近当前技术入场价、且未跌破技术止损位时重新评估。"
            if guarded_plan.get("actionable")
            else "当前技术价格计划不可执行，先刷新行情和日线。"
        )
        if all(executable.values()):
            risk_sizing = size_ashare_candidate(
                entry_price=executable["entry"],
                stop_price=executable["stop"],
                target_price=executable["target"],
                actionable_equity=equity_value,
                cash_available=remaining_cash,
                original_cash=cash or 0.0,
                remaining_new_exposure=remaining_new_exposure,
                remaining_initial_deploy=remaining_initial_cap or 0.0,
                remaining_loss_budget=remaining_loss_budget,
                existing_symbol_market_value=existing_symbol_market_value,
            )
        else:
            risk_sizing = {
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "trade": None,
                "failed_gates": ["incomplete_executable_price_tuple"],
                "blocking_failed_gates": ["incomplete_executable_price_tuple"],
            }
        failed_gates = list(dict.fromkeys(base_failed_gates + list(risk_sizing.get("failed_gates") or [])))
        blocking_failed_gates = list(
            dict.fromkeys(
                base_failed_gates
                + list(risk_sizing.get("blocking_failed_gates") or [])
            )
        )
        suggested_lots = 0 if base_failed_gates else int(risk_sizing.get("suggested_lots") or 0)
        suggested_quantity = suggested_lots * 100
        if base_failed_gates:
            risk_sizing = {
                **risk_sizing,
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "trade": None,
                "failed_gates": failed_gates,
                "blocked_by_hard_gate": True,
            }
        else:
            risk_sizing = {
                **risk_sizing,
                "blocked_by_hard_gate": False,
            }
        if suggested_lots:
            selected_trade = risk_sizing.get("trade") or {}
            buy_cost = float(selected_trade.get("entry_order", {}).get("total_cost") or 0)
            planned_loss = float(selected_trade.get("risk_amount") or 0)
            remaining_cash = round(remaining_cash - buy_cost, 2)
            remaining_new_exposure = round(remaining_new_exposure - buy_cost, 2)
            remaining_initial_cap = round((remaining_initial_cap or 0) - buy_cost, 2)
            remaining_loss_budget = round(remaining_loss_budget - planned_loss, 2)
            planned_symbol_market_values[candidate_code] = round(
                planned_symbol_market_values.get(candidate_code, 0.0)
                + float(executable["entry"] or 0) * suggested_quantity,
                2,
            )
        elif risk_gate == "pass" and failed_gates:
            if "external_risk_gate" in failed_gates:
                risk_gate = "blocked_by_external_risk"
            elif "a_share_market_gate" in failed_gates:
                risk_gate = "blocked_by_market_regime"
            elif "quote_freshness" in failed_gates:
                risk_gate = "blocked_by_quote_freshness"
            elif "technical_price_plan" in failed_gates:
                risk_gate = "blocked_by_technical_plan"
            else:
                risk_gate = "blocked_by_risk_budget"

        cooldown_checks = (
            {
                "max_turnover_rate": 10.0,
                "max_intraday_range_pct": 8.0,
                "must_refresh_quote": True,
                "must_hold_above_invalidation_price": triggers.get("invalidation_price"),
            }
            if blocked_by_divergence
            else None
        )
        quote = candidate.get("quote") if isinstance(candidate.get("quote"), dict) else {}
        cooldown_evaluation = None
        if cooldown_checks:
            failed_checks = []
            turnover_rate = quote.get("turnover_rate")
            intraday_range_pct = quote.get("intraday_range_pct")
            current_price = quote.get("price")
            invalidation_price = cooldown_checks.get("must_hold_above_invalidation_price")
            if turnover_rate is not None and turnover_rate >= cooldown_checks["max_turnover_rate"]:
                failed_checks.append("turnover_rate")
            if intraday_range_pct is not None and intraday_range_pct >= cooldown_checks["max_intraday_range_pct"]:
                failed_checks.append("intraday_range_pct")
            if current_price is not None and invalidation_price is not None and current_price <= invalidation_price:
                failed_checks.append("invalidation_price")
            evaluation_status = "stale_until_refresh" if requires_quote_refresh else "current"
            cooldown_evaluation = {
                "evaluation_status": evaluation_status,
                "actionable": evaluation_status == "current" and not failed_checks,
                "passed": not failed_checks,
                "failed_checks": failed_checks,
                "current_turnover_rate": turnover_rate,
                "current_intraday_range_pct": intraday_range_pct,
                "current_price": current_price,
            }

        candidate_lot_plan.append(
            {
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "one_lot_amount": one_lot_amount,
                "cash_usage_pct": candidate.get("cash_usage_pct"),
                "breakout_status": breakout_status,
                "entry_policy": {
                    "status": entry_status,
                    "reference_trigger_status": reference_entry_status,
                    "confirm": confirm_text,
                    "avoid": "尾盘拉升、放量回落或跌回突破价下方时先放弃。",
                    "technical_entry_price": executable.get("entry"),
                    "technical_stop_price": executable.get("stop"),
                    "technical_target_price": executable.get("target"),
                    "reference_breakout_price": triggers.get("breakout_price"),
                    "reference_invalidation_price": triggers.get("invalidation_price"),
                },
                "within_single_cap": within_single_cap,
                "suggested_lots": suggested_lots,
                "suggested_quantity": suggested_quantity,
                "risk_gate": risk_gate,
                "failed_gates": failed_gates,
                "blocking_failed_gates": blocking_failed_gates,
                "risk_sizing": risk_sizing,
                "executable_price_tuple": executable,
                "activation_condition": (
                    "observe_only"
                    if blocked_by_observation_only
                    else (
                        "wait_until_recent_sale_cooldown_expires"
                        if blocked_by_recent_sale
                        else (
                            "recalibrate_price_plan_after_corporate_action"
                            if blocked_by_price_plan_adjustment
                            else (
                                "build_candidate_price_plan"
                                if blocked_by_missing_price_plan
                                else (
                                    "cooldown_after_hot_move"
                                    if blocked_by_hot_move
                                    else (
                                        "wait_for_divergence_cooldown"
                                        if blocked_by_divergence
                                        else (
                                            "refresh_quote_before_action"
                                            if requires_quote_refresh
                                            else "confirm_guarded_technical_entry"
                                        )
                                    )
                                )
                            )
                        )
                    )
                ),
                "reason": (
                    "低分歧防守备选仅用于观察，不生成建议仓位。"
                    if blocked_by_observation_only
                    else (
                        "该标的刚完成卖出，冷静期内不生成反手回补数量。"
                        if blocked_by_recent_sale
                        else (
                            "除权除息导致价格口径变化，先复权校准观察区、突破价和失效价。"
                            if blocked_by_price_plan_adjustment
                            else (
                                "缺少完整价格计划，先生成观察区、突破价和失效价。"
                                if blocked_by_missing_price_plan
                                else (
                                    "涨幅接近或达到涨停，禁止追高，等待热度降温。"
                                    if blocked_by_hot_move
                                    else (
                                            "高换手或大振幅说明分歧较强，先等分歧收敛和承接确认。"
                                            if blocked_by_divergence
                                            else (
                                                "满足资金与风险上限，仅在当前技术价格计划条件确认后作为分批参考。"
                                                if suggested_lots
                                                else "不满足首批资金、单票上限或现金可达条件，先观察。"
                                            )
                                    )
                                )
                            )
                        )
                    )
                ),
                **({"cooldown_checks": cooldown_checks} if cooldown_checks else {}),
                **({"cooldown_evaluation": cooldown_evaluation} if cooldown_evaluation else {}),
            }
        )

    mode = "position_risk_first" if holdings_risk else "cash_ready"
    if requires_quote_refresh:
        plan_status = "pending_quote_refresh"
        execution_window = "next_trading_session"
        quote_refresh_reason = "当前不在交易时段，需在下一交易时段刷新腾讯行情后再评估。"
    elif market_session.get("is_late_session"):
        plan_status = "late_session_observation"
        execution_window = "current_session_late"
        quote_refresh_reason = "当前接近收盘，尾盘价格只适合作为观察参考。"
    else:
        plan_status = "realtime_observation"
        execution_window = "current_session"
        quote_refresh_reason = "当前处于交易时段，仍需结合实时成交和盘口承接动态复核。"

    return {
        "mode": mode,
        "cash_available": cash,
        "plan_status": plan_status,
        "execution_window": execution_window,
        "requires_quote_refresh": requires_quote_refresh,
        "quote_refresh_reason": quote_refresh_reason,
        "initial_deploy_cap_pct": initial_deploy_cap_pct,
        "initial_deploy_cap_amount": initial_deploy_cap_amount,
        "reserve_cash_pct": reserve_cash_pct,
        "max_single_candidate_pct": max_single_candidate_pct,
        "max_single_candidate_amount": max_single_candidate_amount,
        "remaining_initial_cap": remaining_initial_cap,
        "remaining_new_exposure": remaining_new_exposure,
        "total_loss_budget": total_loss_budget,
        "remaining_loss_budget": remaining_loss_budget,
        "external_risk_gate": external_risk_gate,
        "a_share_market_gate": a_share_market_gate,
        "external_new_exposure_amount": external_new_exposure,
        "market_adjusted_new_exposure_cap": round(
            external_new_exposure * market_multiplier,
            2,
        ),
        "actionable_equity": actionable_equity,
        "candidate_lot_plan": candidate_lot_plan,
        "note": "仓位计划仅用于研究和资金约束参考，不构成投资建议或交易指令。",
    }


def _with_secondary_focus(action_bias: Dict[str, Any], fallback_candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not fallback_candidates:
        return action_bias
    return {
        **action_bias,
        "secondary_focus": {
            "status": "observe_fallback_candidates",
            "candidate_codes": [candidate.get("code") for candidate in fallback_candidates],
            "note": "防守备选仅用于观察，不构成交易指令。",
        },
    }


def _build_action_bias(
    cash_deployment_plan: Dict[str, Any],
    fallback_candidates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    lot_plan = cash_deployment_plan.get("candidate_lot_plan") or []
    top_plan = lot_plan[:3]
    fallback_candidates = fallback_candidates or []
    all_top_blocked_by_divergence = bool(top_plan) and all(
        item.get("risk_gate") == "blocked_by_divergence" for item in top_plan
    )
    all_top_blocked_by_price_plan_adjustment = bool(top_plan) and all(
        item.get("risk_gate") == "blocked_by_price_plan_adjustment" for item in top_plan
    )
    all_top_blocked_by_missing_price_plan = bool(top_plan) and all(
        item.get("risk_gate") == "blocked_by_missing_price_plan" for item in top_plan
    )
    all_top_blocked_by_hot_move = bool(top_plan) and all(
        item.get("risk_gate") == "blocked_by_hot_move" for item in top_plan
    )
    all_top_blocked_by_entry_condition = bool(top_plan) and all(
        item.get("risk_gate") == "blocked_by_entry_condition" for item in top_plan
    )
    all_top_blocked = bool(top_plan) and all(
        str(item.get("risk_gate") or "").startswith("blocked_by_") for item in top_plan
    )
    has_suggested_lots = any((item.get("suggested_lots") or 0) > 0 for item in top_plan)

    if all_top_blocked_by_price_plan_adjustment:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "price_plan_adjustment_required",
                "next_step": "先按除权除息口径复权校准价格计划，再重新评估。",
            },
            fallback_candidates,
        )
    if all_top_blocked_by_missing_price_plan:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "candidate_price_plan_required",
                "next_step": "先生成完整观察区、突破价和失效价，再重新评估。",
            },
            fallback_candidates,
        )
    if all_top_blocked_by_hot_move:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "top_candidates_blocked_by_hot_move",
                "next_step": "禁止追涨，等待热度降温并重新进入可观察区间。",
            },
            fallback_candidates,
        )
    if all_top_blocked_by_divergence:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "top_candidates_blocked_by_divergence",
                "next_step": "等待分歧收敛并在下一交易时段刷新腾讯行情。",
            },
            fallback_candidates,
        )
    if all_top_blocked_by_entry_condition:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "entry_confirmation_required",
                "next_step": "等待候选进入观察区或确认突破后再评估。",
            },
            fallback_candidates,
        )
    if all_top_blocked:
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "top_candidates_blocked_by_risk_gates",
                "next_step": "先完成各候选风险门槛要求，再重新评估。",
            },
            fallback_candidates,
        )
    if cash_deployment_plan.get("requires_quote_refresh"):
        return _with_secondary_focus(
            {
                "status": "wait",
                "primary_reason": "quote_refresh_required",
                "next_step": "下一交易时段刷新腾讯行情后再重新评估。",
            },
            fallback_candidates,
        )
    if has_suggested_lots:
        return _with_secondary_focus(
            {
                "status": "conditional_watch",
                "primary_reason": "cash_fit_candidates_available",
                "next_step": "只在实时承接确认后再评估分批仓位。",
            },
            fallback_candidates,
        )
    return _with_secondary_focus(
        {
            "status": "observe",
            "primary_reason": "no_candidate_passed_cash_plan",
            "next_step": "继续观察候选股是否回到可评估区间。",
        },
        fallback_candidates,
    )


def _build_fallback_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fallback: List[Dict[str, Any]] = []
    for candidate in candidates:
        risk_flags = candidate.get("risk_flags") or []
        status = candidate.get("triggers", {}).get("status", {})
        theme = candidate.get("theme")
        is_defensive = theme in {"defensive_energy", "defensive_yield"}
        disqualifying_keys = {
            "limit_up_or_hot_move",
            "high_turnover",
            "wide_intraday_range",
            "corporate_action_price_adjustment",
            "missing_candidate_price_plan",
        }
        low_divergence = not any(flag.get("key") in disqualifying_keys for flag in risk_flags)
        observable = status.get("position") in {"inside_observation_zone", "below_observation_zone"}
        if not (low_divergence and candidate.get("affordable_with_cash") and is_defensive and observable):
            continue
        watch_condition = (
            "观察区内，刷新行情后确认低分歧承接。"
            if status.get("position") == "inside_observation_zone"
            else "低于观察区，先等重新站回观察区。"
        )
        fallback.append(
            {
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "theme_label": candidate.get("theme_label"),
                "position": status.get("position"),
                "price": candidate.get("quote", {}).get("price"),
                "cash_usage_pct": candidate.get("cash_usage_pct"),
                "actionable": False,
                "watch_condition": watch_condition,
                "reason": "低分歧防守备选，仅用于观察，不构成交易指令。",
            }
        )
    return fallback[:3]


def _build_candidate_decision_matrix(
    cash_deployment_plan: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    fallback_candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fallback_codes = {candidate.get("code") for candidate in fallback_candidates}
    planned_codes = {
        candidate.get("code")
        for candidate in cash_deployment_plan.get("candidate_lot_plan", [])
    }
    rows: List[Dict[str, Any]] = []

    for index, candidate in enumerate(cash_deployment_plan.get("candidate_lot_plan", [])):
        if candidate.get("code") in fallback_codes:
            continue

        risk_gate = candidate.get("risk_gate")
        suggested_lots = candidate.get("suggested_lots") or 0
        required_confirmations = [
            "refresh_tencent_quotes",
            "review_external_risks",
            "review_a_share_market_state",
        ]
        if risk_gate == "blocked_by_price_plan_adjustment":
            decision = "blocked"
            action = "wait"
            required_confirmations.extend(["recalibrate_price_plan", "new_analysis_report"])
        elif risk_gate == "blocked_by_missing_price_plan":
            decision = "blocked"
            action = "wait"
            required_confirmations.extend(
                ["build_observation_zone", "set_breakout_price", "set_invalidation_price"]
            )
        elif risk_gate == "blocked_by_hot_move":
            decision = "blocked"
            action = "wait"
            required_confirmations.extend(["cooldown_after_hot_move", "reenter_observation_zone"])
        elif risk_gate == "blocked_by_divergence":
            decision = "blocked"
            action = "wait"
            required_confirmations.extend(
                [
                    "turnover_rate_below_10",
                    "intraday_range_below_8",
                    "hold_above_invalidation_price",
                ]
            )
        elif risk_gate == "blocked_by_entry_condition":
            decision = "blocked"
            action = "wait"
            required_confirmations.append("enter_observation_zone_or_confirm_breakout")
        elif str(risk_gate or "").startswith("blocked_by_"):
            decision = "blocked"
            action = "wait"
            required_confirmations.append("resolve_risk_gate")
        elif suggested_lots > 0:
            decision = "conditional_watch"
            action = "evaluate_after_confirmations"
            required_confirmations.extend(["realtime_support", "hold_above_invalidation_price"])
        else:
            decision = "observe_only"
            action = "observe"
            required_confirmations.append("cash_and_position_limits")

        rows.append(
            {
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "tier": "primary" if index < 3 else "secondary",
                "decision": decision,
                "action": action,
                "risk_gate": risk_gate,
                "suggested_lots": suggested_lots,
                "cash_usage_pct": candidate.get("cash_usage_pct"),
                "failed_gates": list(candidate.get("failed_gates") or []),
                "blocking_failed_gates": list(
                    candidate.get("blocking_failed_gates") or []
                ),
                "required_confirmations": required_confirmations,
                "reason": candidate.get("reason"),
                "is_reference_only": True,
            }
        )

    represented_codes = planned_codes | fallback_codes
    for candidate in candidates:
        if candidate.get("code") in represented_codes:
            continue
        risk_keys = {flag.get("key") for flag in candidate.get("risk_flags", [])}
        price_plan_adjustment_required = "corporate_action_price_adjustment" in risk_keys
        if price_plan_adjustment_required:
            decision = "blocked"
            action = "wait"
            risk_gate = "price_plan_adjustment_required"
            required_confirmations = [
                "refresh_tencent_quotes",
                "review_external_risks",
                "review_a_share_market_state",
                "recalibrate_price_plan",
                "new_analysis_report",
            ]
            reason = "除权除息后旧价格计划不可直接使用，需先复权校准。"
        elif {"high_turnover", "wide_intraday_range"} & risk_keys:
            decision = "blocked"
            action = "wait"
            risk_gate = "blocked_by_divergence"
            required_confirmations = [
                "refresh_tencent_quotes",
                "review_external_risks",
                "review_a_share_market_state",
                "turnover_rate_below_10",
                "intraday_range_below_8",
            ]
            reason = "高换手或大振幅说明分歧较强，先等分歧收敛。"
        elif "limit_up_or_hot_move" in risk_keys:
            decision = "blocked"
            action = "wait"
            risk_gate = "blocked_by_hot_move"
            required_confirmations = [
                "refresh_tencent_quotes",
                "review_external_risks",
                "review_a_share_market_state",
                "cooldown_after_hot_move",
                "reenter_observation_zone",
            ]
            reason = "涨幅接近或达到涨停，禁止追高，等待热度降温和分歧承接。"
        elif {"insufficient_cash", "low_cash_buffer"} & risk_keys:
            decision = "blocked"
            action = "wait"
            risk_gate = "blocked_by_cash_constraints"
            required_confirmations = [
                "refresh_tencent_quotes",
                "review_external_risks",
                "review_a_share_market_state",
                "cash_and_position_limits",
            ]
            reason = "当前现金约束不支持该候选，先保持观察。"
        else:
            decision = "observe_only"
            action = "observe"
            risk_gate = "not_ranked_for_cash_plan"
            required_confirmations = [
                "refresh_tencent_quotes",
                "review_external_risks",
                "review_a_share_market_state",
                "enter_top_priority",
            ]
            reason = "未进入首批现金计划，仅保留为次级观察候选。"
        rows.append(
            {
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "tier": "secondary",
                "decision": decision,
                "action": action,
                "risk_gate": risk_gate,
                "suggested_lots": 0,
                "cash_usage_pct": candidate.get("cash_usage_pct"),
                "failed_gates": sorted(risk_keys) if risk_keys else [risk_gate],
                "blocking_failed_gates": (
                    sorted(risk_keys) if risk_keys else [risk_gate]
                ),
                "required_confirmations": required_confirmations,
                "reason": reason,
                "is_reference_only": True,
            }
        )

    for candidate in fallback_candidates:
        rows.append(
            {
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "tier": "defensive_fallback",
                "decision": "observe_only",
                "action": "observe",
                "risk_gate": "observation_only",
                "suggested_lots": 0,
                "cash_usage_pct": candidate.get("cash_usage_pct"),
                "failed_gates": ["observation_only_fallback"],
                "blocking_failed_gates": ["observation_only_fallback"],
                "required_confirmations": [
                    "refresh_tencent_quotes",
                    "review_external_risks",
                    "review_a_share_market_state",
                    "low_divergence_support",
                ],
                "reason": candidate.get("reason"),
                "is_reference_only": True,
            }
        )

    return {
        "horizon": "未来两个交易日",
        "default_action": "wait",
        "rows": rows,
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def _build_next_refresh_checklist(
    cash_deployment_plan: Dict[str, Any],
    fallback_candidates: List[Dict[str, Any]],
    recent_sale_policy: Optional[Dict[str, Any]] = None,
    external_risk_checklist: Optional[Dict[str, Any]] = None,
    a_share_market_checklist: Optional[Dict[str, Any]] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    checklist = [
        {
            "step": "refresh_tencent_quotes",
            "status": "required",
            "note": "下一交易时段先刷新腾讯行情。",
        }
    ]
    if external_risk_checklist and external_risk_checklist.get("status") == "requires_current_review":
        checklist.append(
            {
                "step": "review_external_risks",
                "status": "required",
                "checks": [item.get("key") for item in external_risk_checklist.get("checks", [])],
                "note": "输出前结合最新国际形势复核，不直接由静态候选池决定。",
            }
        )
    if a_share_market_checklist and a_share_market_checklist.get("status") in {
        "requires_current_review",
        "blocked_by_market_regime",
    }:
        checklist.append(
            {
                "step": "review_a_share_market_state",
                "status": "required",
                "checks": [item.get("key") for item in a_share_market_checklist.get("checks", [])],
                "note": "先确认A股盘面广度和主线延续性，再评估候选股。",
            }
        )
    if recent_sale_policy and recent_sale_policy.get("status") == "cooldown":
        checklist.append(
            {
                "step": "respect_recent_sale_cooldown",
                "status": "required",
                "candidate_codes": recent_sale_policy.get("matched_candidate_codes")
                or [recent_sale_policy.get("code")],
                "note": "最近止盈卖出的标的不作为默认回补对象。",
            }
        )
    missing_price_plan_codes = [
        candidate.get("code")
        for candidate in candidates or []
        if any(flag.get("key") == "missing_candidate_price_plan" for flag in candidate.get("risk_flags", []))
    ]
    if missing_price_plan_codes:
        checklist.append(
            {
                "step": "build_candidate_price_plan",
                "status": "required",
                "candidate_codes": missing_price_plan_codes,
                "checks": ["observation_zone", "breakout_price", "invalidation_price"],
                "note": "价格计划不完整时不能生成仓位参考。",
            }
        )
    price_plan_adjustment_codes = [
        candidate.get("code")
        for candidate in candidates or []
        if any(
            flag.get("key") == "corporate_action_price_adjustment"
            for flag in candidate.get("risk_flags", [])
        )
    ]
    if price_plan_adjustment_codes:
        checklist.append(
            {
                "step": "recalibrate_corporate_action_price_plans",
                "status": "required",
                "candidate_codes": price_plan_adjustment_codes,
                "checks": ["corporate_action_marker", "adjusted_observation_zone", "new_analysis_report"],
                "note": "除权除息后旧价格计划不可直接使用。",
            }
        )
    hot_move_codes = [
        candidate.get("code")
        for candidate in candidates or []
        if any(flag.get("key") == "limit_up_or_hot_move" for flag in candidate.get("risk_flags", []))
    ]
    if hot_move_codes:
        checklist.append(
            {
                "step": "avoid_hot_move_chase",
                "status": "required",
                "candidate_codes": hot_move_codes,
                "checks": ["pct_chg", "cooldown_after_hot_move", "reenter_observation_zone"],
                "note": "接近或达到涨停的候选只观察，不追高。",
            }
        )
    entry_confirmation_codes = [
        item.get("code")
        for item in cash_deployment_plan.get("candidate_lot_plan", [])
        if item.get("risk_gate") == "blocked_by_entry_condition"
    ]
    if entry_confirmation_codes:
        checklist.append(
            {
                "step": "wait_for_entry_confirmation",
                "status": "required",
                "candidate_codes": entry_confirmation_codes,
                "checks": ["observation_zone", "breakout_status", "realtime_support"],
                "note": "未进入观察区且未确认突破时不生成手数参考。",
            }
        )
    primary_codes = [
        item.get("code")
        for item in cash_deployment_plan.get("candidate_lot_plan", [])
        if item.get("risk_gate") == "blocked_by_divergence"
    ]
    if primary_codes:
        checklist.append(
            {
                "step": "evaluate_primary_cooldown",
                "status": "required",
                "candidate_codes": primary_codes,
                "checks": ["turnover_rate", "intraday_range_pct", "invalidation_price"],
            }
        )
    fallback_codes = [candidate.get("code") for candidate in fallback_candidates]
    if fallback_codes:
        checklist.append(
            {
                "step": "observe_fallback_candidates",
                "status": "optional",
                "candidate_codes": fallback_codes,
                "note": "仅观察防守备选，不构成交易指令。",
            }
        )
    return checklist


def _build_a_share_market_checklist(
    a_share_market_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    market_gate = a_share_market_gate or {}
    return {
        "status": (
            "blocked_by_market_regime"
            if not market_gate.get("new_position_allowed")
            else "requires_current_review"
        ),
        "horizon": "未来两个交易日",
        "source_policy": "CLI自动使用腾讯主要指数和Mongo全市场行情生成市场门禁；宽度数据不足时仍要求Hermes人工确认。",
        "automatic_gate": market_gate,
        "checks": [
            {
                "key": "index_breadth",
                "status": "required",
                "watch": "上证、深成指、创业板、科创50与黄白线/涨跌家数。",
                "negative_signal": "指数上涨但多数个股下跌，或黄白线明显分化。",
                "effect": "降低追涨权重，等待候选股回踩或分歧收敛。",
            },
            {
                "key": "technology_theme_sustainability",
                "status": "required",
                "watch": "AI算力、半导体、信创主线的龙头封单、回封和板块扩散。",
                "negative_signal": "主线龙头开板回落或后排快速退潮。",
                "effect": "AI/半导体候选保持wait，防止主线高潮后接力。",
            },
            {
                "key": "hot_money_chase_risk",
                "status": "required",
                "watch": "涨停数、炸板率、高开低走和高换手标的。",
                "negative_signal": "炸板率升高或高位股集体冲高回落。",
                "effect": "禁止把突破或涨停当成直接买入信号。",
            },
            {
                "key": "market_liquidity",
                "status": "required",
                "watch": "两市成交额、量能相对前一日变化和缩量/放量质量。",
                "negative_signal": "缩量上涨或放量滞涨。",
                "effect": "首批资金上限继续保守，必要时保持空仓。",
            },
            {
                "key": "defensive_rotation",
                "status": "optional",
                "watch": "能源、电力、高股息和低波红利相对强弱。",
                "negative_signal": "防守板块同步走弱且科技分歧放大。",
                "effect": "防守候选也只观察，不主动提高仓位。",
            },
        ],
        "default_position_effect": "盘面广度或主线延续性未确认时，维持wait；仅在实时盘面和候选股同时确认后再评估仓位。",
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def _build_external_risk_checklist(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    theme_labels = sorted(
        {
            str(candidate.get("theme_label"))
            for candidate in candidates
            if candidate.get("theme_label")
        }
    )
    return {
        "status": "requires_current_review",
        "horizon": "未来两个交易日",
        "source_policy": "CLI不内置实时国际新闻，Hermes输出前需核查最新可信来源。",
        "candidate_theme_exposure": theme_labels,
        "checks": [
            {
                "key": "global_ai_risk_appetite",
                "status": "required",
                "watch": "隔夜美股AI、半导体、纳指或费半表现。",
                "negative_signal": "AI或芯片主线明显回撤。",
                "effect": "降低AI算力和半导体候选优先级。",
            },
            {
                "key": "us_china_policy",
                "status": "required",
                "watch": "中美关税、出口管制、科技制裁或产业政策更新。",
                "negative_signal": "政策摩擦升级并压制科技硬件风险偏好。",
                "effect": "暂停追高科技硬件候选，优先等待分歧收敛。",
            },
            {
                "key": "oil_geopolitics",
                "status": "required",
                "watch": "原油价格、地缘风险和能源供给扰动。",
                "negative_signal": "油价快速上行或地缘风险升级。",
                "effect": "提高防守候选观察权重，降低进攻仓位。",
            },
            {
                "key": "fx_liquidity",
                "status": "required",
                "watch": "美元指数、离岸人民币和外资风险偏好。",
                "negative_signal": "人民币快速走弱或外资风险偏好下降。",
                "effect": "降低首批资金上限或继续空仓等待。",
            },
        ],
        "default_position_effect": "任一必查项出现负面信号时，维持wait或仅观察防守候选。",
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def _build_recent_sale_policy(
    trade_context: Optional[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    *,
    as_of: Any = None,
    benchmark_session_dates: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    context = trade_context or {}
    recent_trades = [
        trade
        for trade in (context.get("recent_trades") or [])
        if isinstance(trade, dict) and trade.get("side") == "sell"
    ]
    last_trade = context.get("last_trade") or {}
    if not recent_trades and last_trade.get("side") == "sell":
        recent_trades = [last_trade]
    if not recent_trades:
        return {
            "status": "none",
            "cooldown_active": False,
            "note": "最近无止盈卖出冷静期约束。",
        }

    last_trade = recent_trades[0]
    code = str(last_trade.get("code") or "").upper()
    assessments: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for trade in recent_trades:
        sale_time = (
            trade.get("effective_at")
            or trade.get("sold_at")
            or trade.get("created_at")
        )
        assessments.append(
            (
                trade,
                assess_recent_sale_cooldown(
                    sale_time,
                    as_of=as_of,
                    benchmark_session_dates=benchmark_session_dates,
                ),
            )
        )
    active_codes = {
        str(trade.get("code") or "").upper()
        for trade, assessment in assessments
        if assessment.get("active")
    }
    matched_candidate_codes = [
        str(candidate.get("code") or "").upper()
        for candidate in candidates
        if str(candidate.get("code") or "").upper() in active_codes
    ]
    cooldown_freshness = assessments[0][1]
    cooldown_active = bool(active_codes)
    return {
        "status": "cooldown" if cooldown_active else "expired",
        "cooldown_active": cooldown_active,
        "code": code,
        "name": last_trade.get("name"),
        "sold_at": last_trade.get("sold_at"),
        "sell_price": _round_number(last_trade.get("sell_price")),
        "realized_pnl": _round_number(last_trade.get("realized_pnl")),
        "cooldown_horizon": "未来两个交易日",
        "started_sessions_after_sale": cooldown_freshness.get("started_sessions_after_sale"),
        "calendar_source": cooldown_freshness.get("calendar_source"),
        "calendar_is_fallback": cooldown_freshness.get("calendar_is_fallback"),
        "default_action": "avoid_rebuy_chase",
        "matched_candidate_codes": matched_candidate_codes,
        "reentry_requirements": [
            "new_analysis_report",
            "refresh_tencent_quotes",
            "low_divergence_confirmation",
        ],
        "note": (
            "最近已卖出该标的，未来两个交易日不把反手追回作为默认动作；仅供研究参考，不构成投资建议或交易指令。"
            if cooldown_active
            else "该标的最近一次卖出的两交易日冷静期已结束；重新关注仍需刷新行情与分析报告，仅供研究参考，不构成投资建议或交易指令。"
        ),
    }


def _build_opportunity_brief(
    account: Dict[str, Any],
    holdings_risk: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    risk_flags: List[Dict[str, Any]],
    market_session: Optional[Dict[str, Any]] = None,
    trade_context: Optional[Dict[str, Any]] = None,
    external_risk_gate: Optional[Dict[str, Any]] = None,
    a_share_market_gate: Optional[Dict[str, Any]] = None,
    actionable_equity: Optional[Dict[str, Any]] = None,
    benchmark_session_dates: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    configured_assets = account.get("configured_total_assets")
    cash = account.get("cash_or_unallocated")
    equity = account.get("estimated_equity")
    account_summary = (
        f"账户配置本金 {configured_assets:.2f}，可用现金 {cash:.2f}，估算权益 {equity:.2f}。"
        if configured_assets is not None and cash is not None and equity is not None
        else "账户关键资金数据不完整，请优先查看 account 字段。"
    )

    primary_holding = max(
        holdings_risk,
        key=lambda item: item.get("weight_by_estimated_equity_pct") or 0,
        default=None,
    )
    if primary_holding:
        holding_priority = (
            f"优先关注持仓风控：{primary_holding.get('code')} {primary_holding.get('name')} "
            f"仓位约 {primary_holding.get('weight_by_estimated_equity_pct')}%，"
            f"浮盈 {primary_holding.get('profit_loss')}，"
            f"月目标进度 {primary_holding.get('monthly_target_progress_pct')}%。"
        )
    else:
        holding_priority = "当前空仓，可用现金优先用于等待候选股确认，不需要处理持仓风控。"

    top_candidates = [
        {
            "code": candidate.get("code"),
            "name": candidate.get("name"),
            "theme_label": candidate.get("theme_label"),
            "position": candidate.get("triggers", {}).get("status", {}).get("position"),
            "breakout_status": candidate.get("triggers", {}).get("status", {}).get("breakout_status"),
            "distance_to_breakout_pct": candidate.get("triggers", {}).get("status", {}).get("distance_to_breakout_pct"),
            "price": candidate.get("quote", {}).get("price"),
            "cash_usage_pct": candidate.get("cash_usage_pct"),
            "risk_keys": [flag.get("key") for flag in candidate.get("risk_flags", [])],
        }
        for candidate in candidates[:3]
    ]
    last_trade = (trade_context or {}).get("last_trade") or {}
    recent_trade_summary = "最近无持仓交易流水。"
    if last_trade.get("side") == "sell":
        sell_price = float(last_trade.get("sell_price") or 0)
        realized_pnl = float(last_trade.get("realized_pnl") or 0)
        recent_trade_summary = (
            f"最近卖出 {last_trade.get('code')} {last_trade.get('name')}，"
            f"成交价 {sell_price:.2f}，已实现盈亏 {realized_pnl:.2f}。"
        )

    recent_sale_policy = _build_recent_sale_policy(
        trade_context,
        candidates,
        as_of=market_session.get("local_time"),
        benchmark_session_dates=benchmark_session_dates,
    )
    fallback_candidates = _build_fallback_candidates(candidates)
    cash_deployment_plan = _build_cash_deployment_plan(
        account,
        holdings_risk,
        candidates,
        market_session,
        external_risk_gate=external_risk_gate,
        a_share_market_gate=a_share_market_gate,
        actionable_equity=actionable_equity,
        recent_sale_policy=recent_sale_policy,
        observation_only_codes=[candidate.get("code") for candidate in fallback_candidates],
    )
    candidate_decision_matrix = _build_candidate_decision_matrix(
        cash_deployment_plan,
        candidates,
        fallback_candidates,
    )
    external_risk_checklist = _build_external_risk_checklist(candidates)
    a_share_market_checklist = _build_a_share_market_checklist(a_share_market_gate)

    return {
        "account_summary": account_summary,
        "holding_priority": holding_priority,
        "recent_trade_summary": recent_trade_summary,
        "recent_sale_policy": recent_sale_policy,
        "external_risk_checklist": external_risk_checklist,
        "a_share_market_checklist": a_share_market_checklist,
        "action_bias": _build_action_bias(cash_deployment_plan, fallback_candidates),
        "candidate_decision_matrix": candidate_decision_matrix,
        "top_candidates": top_candidates,
        "fallback_candidates": fallback_candidates,
        "next_refresh_checklist": _build_next_refresh_checklist(
            cash_deployment_plan,
            fallback_candidates,
            recent_sale_policy,
            external_risk_checklist,
            a_share_market_checklist,
            candidates,
        ),
        "watch_plan": _build_watch_plan(holdings_risk, candidates),
        "cash_deployment_plan": cash_deployment_plan,
        "risk_keys": [flag.get("key") for flag in risk_flags],
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def _next_weekday_morning(local_now: datetime) -> datetime:
    next_day = local_now + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day.replace(hour=9, minute=30, second=0, microsecond=0)


def _market_session_context(now: Optional[datetime] = None) -> Dict[str, Any]:
    tz = ZoneInfo(CN_MARKET_TIMEZONE)
    if now is None:
        local_now = datetime.now(timezone.utc).astimezone(tz)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=tz)
    else:
        local_now = now.astimezone(tz)

    local_time = local_now.time()
    is_weekday = local_now.weekday() < 5
    if not is_weekday:
        session = "closed"
    elif time(9, 30) <= local_time < time(11, 30):
        session = "morning"
    elif time(11, 30) <= local_time < time(13, 0):
        session = "lunch_break"
    elif time(13, 0) <= local_time < time(15, 0):
        session = "afternoon"
    elif local_time < time(9, 30):
        session = "pre_open"
    else:
        session = "closed"

    is_trading_hours = session in {"morning", "afternoon"}
    minutes_to_close = None
    if is_weekday and local_time < time(15, 0):
        close_dt = local_now.replace(hour=15, minute=0, second=0, microsecond=0)
        minutes_to_close = max(0, int((close_dt - local_now).total_seconds() // 60))
    is_late_session = session == "afternoon" and minutes_to_close is not None and minutes_to_close <= 15

    next_refresh_dt = None
    next_refresh_session = None
    if session == "pre_open":
        next_refresh_dt = local_now.replace(hour=9, minute=30, second=0, microsecond=0)
        next_refresh_session = "open"
    elif session == "lunch_break":
        next_refresh_dt = local_now.replace(hour=13, minute=0, second=0, microsecond=0)
        next_refresh_session = "afternoon"
    elif not is_trading_hours:
        next_refresh_dt = _next_weekday_morning(local_now)
        next_refresh_session = "next_open"

    return {
        "market": "CN",
        "timezone": CN_MARKET_TIMEZONE,
        "local_time": local_now.isoformat(timespec="seconds"),
        "session": session,
        "is_trading_hours": is_trading_hours,
        "quote_stale_risk": not is_trading_hours,
        "minutes_to_close": minutes_to_close,
        "is_late_session": is_late_session,
        "next_refresh_at": next_refresh_dt.isoformat(timespec="seconds") if next_refresh_dt else None,
        "next_refresh_session": next_refresh_session,
    }


def build_opportunities_payload(
    db: Any,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    candidate_codes: Optional[List[str]] = None,
    buy_lot_size: int = DEFAULT_BUY_LOT_SIZE,
    external_risk_level: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_external_risk_level = _validate_external_risk_level(external_risk_level)
    if buy_lot_size != DEFAULT_BUY_LOT_SIZE:
        raise CLIError(
            "A股仓位计算固定使用100股一手，不支持自定义 lot-size",
            code="invalid_lot_size",
        )
    holdings_payload = build_holdings_payload(
        db,
        username=username,
        email=email,
        user_id=user_id,
        include_analysis=True,
    )
    data = holdings_payload["data"]
    account = _build_account_payload(data["summary"], data["settings"], buy_lot_size)
    holdings_risk = _build_holdings_risk(data["items"], account.get("estimated_equity"))
    actionable_equity = _resolve_actionable_equity(account, holdings_risk)
    external_risk_gate = build_external_risk_gate(
        normalized_external_risk_level,
        actionable_equity=actionable_equity.get("value"),
    )
    benchmark_dates = _benchmark_session_dates()
    benchmark_trade_date = max(benchmark_dates) if benchmark_dates else None
    a_share_market_gate = _build_a_share_market_gate(benchmark_trade_date, db=db)
    holding_themes = {risk.get("theme") for risk in holdings_risk if risk.get("theme")}
    if candidate_codes:
        definitions = _candidate_definitions(candidate_codes)
        candidate_discovery = {
            "status": "manual_candidates",
            "source": "cli.candidate_code",
            "definitions_count": len(definitions),
            "selected_codes": [definition.get("code") for definition in definitions],
        }
    else:
        discovery_result = discover_dynamic_candidate_universe(
            db,
            benchmark_trade_date=benchmark_trade_date,
            cash_available=account.get("cash_or_unallocated"),
        )
        definitions = list(discovery_result.pop("definitions", []))
        candidate_discovery = {
            **discovery_result,
            "definitions_count": len(definitions),
            "selected_codes": [definition.get("code") for definition in definitions],
        }
    candidates = _build_opportunity_candidates(
        definitions,
        cash=account.get("cash_or_unallocated"),
        buy_lot_size=buy_lot_size,
        holding_themes=holding_themes,
    )
    risk_flags = _build_opportunity_risk_flags(holdings_risk, candidates, account)
    market_session = _market_session_context()
    trade_context = _build_trade_context(db, user_id=data["user"]["id"])
    brief = _build_opportunity_brief(
        account,
        holdings_risk,
        candidates,
        risk_flags,
        market_session,
        trade_context,
        external_risk_gate,
        a_share_market_gate,
        actionable_equity,
        benchmark_dates,
    )

    return {
        "ok": True,
        "data": {
            "user": data["user"],
            "brief": brief,
            "account": account,
            "actionable_equity": actionable_equity,
            "external_risk_gate": external_risk_gate,
            "a_share_market_gate": a_share_market_gate,
            "trade_context": trade_context,
            "holdings_risk": holdings_risk,
            "candidate_discovery": candidate_discovery,
            "candidates": candidates,
            "risk_flags": risk_flags,
            "context": {
                "horizon": "未来两个交易日",
                "quote_source": "tencent",
                "market_session": market_session,
                "cash_rule": "按当前持仓成本和配置总资产估算可用现金；按一手股数校验资金可达性。",
                "global_risk_notes": [
                    "海外利率、关税、能源价格会影响高估值科技股风险偏好。",
                    "候选池只用于观察和研究，不能替代独立投资决策。",
                ],
            },
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 6,
            "source": (
                "mongo.user_holdings+analysis_reports+candidate_discovery+"
                "mongo_market_breadth+tencent_quotes+tencent_major_indices+"
                "cninfo_dividend_calendar"
            ),
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def build_market_status_payload(
    db: Any = None,
    *,
    database_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a login-free A-share market gate, with optional Mongo breadth."""
    market_gate = _build_a_share_market_gate(None, db=db)
    breadth_regime = (
        market_gate.get("breadth_regime")
        if isinstance(market_gate.get("breadth_regime"), dict)
        else {}
    )
    effective_database_status = dict(
        database_status
        or ({"status": "connected"} if db is not None else {"status": "not_configured"})
    )
    if breadth_regime.get("load_error"):
        effective_database_status = {
            "status": "unavailable",
            "error_code": "database_error",
            "error_type": breadth_regime.get("load_error"),
        }

    if breadth_regime.get("status") == "ok":
        data_completeness = "indices_and_breadth"
    elif market_gate.get("indices") or market_gate.get("index_regime", {}).get("indices"):
        data_completeness = "indices_only"
    else:
        data_completeness = "unavailable"

    if market_gate.get("status") != "ok":
        decision = {
            "action": "wait",
            "actionable": False,
            "reason_code": "market_data_unavailable",
            "reason": "主要指数数据未通过完整性校验，不能据此评估新仓。",
        }
    elif not market_gate.get("new_position_allowed"):
        decision = {
            "action": "wait",
            "actionable": False,
            "reason_code": "market_regime_blocks_new_positions",
            "reason": market_gate.get("reason") or "市场门禁禁止新增仓位。",
        }
    elif market_gate.get("breadth_confirmation_required"):
        decision = {
            "action": "wait",
            "actionable": False,
            "reason_code": "breadth_confirmation_required",
            "reason": "指数门禁未阻止新仓，但缺少有效全市场宽度，先等待涨跌家数确认。",
        }
    else:
        decision = {
            "action": "evaluate_candidates",
            "actionable": True,
            "reason_code": "market_gate_clear",
            "reason": "指数和市场宽度均通过门禁，可继续评估个股条件。",
        }

    return {
        "ok": True,
        "data": {
            "market": "CN",
            "market_gate": market_gate,
            "decision": decision,
            "data_completeness": data_completeness,
            "database": effective_database_status,
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": "tencent_major_indices+optional_mongo_market_breadth",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


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


def _validate_external_risk_level(level: Optional[str]) -> str:
    try:
        return str(build_external_risk_gate(level, actionable_equity=None)["level"])
    except ValueError as exc:
        raise CLIError(str(exc), code="invalid_external_risk_level") from exc


def _run_json(
    builder,
    *,
    pretty: bool = False,
    preflight: Optional[Callable[[], None]] = None,
) -> None:
    try:
        if preflight is not None:
            preflight()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
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


def _optional_market_database() -> tuple[Any, Dict[str, Any]]:
    try:
        return _get_database(), {"status": "connected"}
    except CLIError as exc:
        return None, {"status": "unavailable", "error_code": exc.code}
    except PyMongoError:
        return None, {"status": "unavailable", "error_code": "database_error"}
    except Exception:
        return None, {"status": "unavailable", "error_code": "database_error"}


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


@holdings_app.command(name="record-sale", help="记录真实持仓卖出，写入流水并更新当前持仓/总资产")
def record_sale_command(
    code: str = typer.Option(..., "--code", help="股票代码"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="卖出数量"),
    sell_price: float = typer.Option(..., "--sell-price", min=0.0001, help="卖出成交价"),
    market: Optional[str] = typer.Option(None, "--market", help="市场，如 CN/HK/US"),
    fee: float = typer.Option(0.0, "--fee", min=0, help="手续费、税费等总费用"),
    sold_at: Optional[str] = typer.Option(None, "--sold-at", help="成交时间 ISO 字符串"),
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_record_sale_payload(
            db,
            username=username,
            email=email,
            user_id=user_id,
            code=code,
            market=market,
            quantity=quantity,
            sell_price=sell_price,
            fee=fee,
            sold_at=sold_at,
        ),
        pretty=pretty,
    )


@holdings_app.command(name="trades", help="输出真实持仓交易流水 JSON，包含卖出实现盈亏")
def trades_command(
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    code: Optional[str] = typer.Option(None, "--code", help="只返回指定股票代码"),
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="最多返回流水数"),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_trades_payload(
            db,
            username=username,
            email=email,
            user_id=user_id,
            code=code,
            limit=limit,
        ),
        pretty=pretty,
    )


@holdings_app.command(
    name="market-status",
    help="无需登录输出A股市场门禁 JSON；Mongo不可用时降级为腾讯指数结果",
)
def market_status_command(
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    database, database_status = _optional_market_database()
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            payload = build_market_status_payload(
                database,
                database_status=database_status,
            )
        _write_json(payload, pretty=pretty)
    except CLIError as exc:
        _write_json(
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc


@holdings_app.command(name="opportunities", help="输出未来两日观察池 JSON，包含现金约束、腾讯行情和风险标签")
def opportunities_command(
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    candidate_codes: Optional[List[str]] = typer.Option(
        None,
        "--candidate-code",
        help="自定义候选股票代码，可重复传入；不传则从最新 Mongo 行情动态初筛",
    ),
    external_risk_level: Optional[str] = typer.Option(
        None,
        "--external-risk-level",
        help="外部风险等级 green/yellow/red；不传按 unknown 0% 处理",
    ),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    _run_json(
        lambda db: build_opportunities_payload(
            db,
            username=username,
            email=email,
            user_id=user_id,
            candidate_codes=candidate_codes,
            buy_lot_size=DEFAULT_BUY_LOT_SIZE,
            external_risk_level=external_risk_level,
        ),
        pretty=pretty,
        preflight=lambda: _validate_external_risk_level(external_risk_level),
    )


def main() -> None:
    if len(sys.argv) == 1:
        holdings_app()
        return

    try:
        exit_code = holdings_app(standalone_mode=False)
    except (PublicClickException, TyperClickException) as exc:
        _write_json(
            {
                "ok": False,
                "error": {
                    "code": "invalid_cli_arguments",
                    "message": exc.format_message(),
                },
            },
            stderr=True,
        )
        raise SystemExit(exc.exit_code) from exc

    if isinstance(exit_code, int) and exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
