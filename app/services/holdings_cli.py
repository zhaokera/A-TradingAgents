"""Machine-readable holdings CLI for local agents such as Hermes."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import sys
import threading
from collections import Counter
from copy import deepcopy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

import typer
import pymongo
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
    build_pullback_price_plan,
    build_technical_price_plan,
)
from app.services.holding_risk_sizing import (
    apply_net_reward_risk_gate,
    build_external_risk_gate,
    size_ashare_candidate,
)
from app.services.investment_policy import (
    INVESTMENT_OBJECTIVE,
    classify_investment_objective,
)
from app.services.opportunity_market_context import (
    OpportunityMarketContext,
    build_opportunity_market_context,
)
from app.services.portfolio_target_analysis import build_target_analysis
from app.services.public_candidate_deep_check import (
    A_SHARE_STOCK_CODE_PATTERN,
    MAX_PUBLIC_DEEP_CHECK_CANDIDATES,
    MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
    PUBLIC_NOTICE_HARD_RISK_TAGS,
    PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS,
    STRUCTURED_BATCH_SIZE,
    run_public_candidate_structured_batches,
    run_public_candidate_technical_funnel,
    validate_public_earnings_screen_metadata,
    validate_public_technical_screen_metadata,
)
from app.services.public_candidate_discovery_service import (
    PUBLIC_CANDIDATE_REJECTION_KEYS,
    discover_public_candidate_universe,
)
from app.services.public_candidate_earnings_risk import (
    EARNINGS_ACTUAL_SOURCE,
    EARNINGS_FORECAST_SOURCE,
    EARNINGS_REVIEW_SOURCE,
    MAX_EARNINGS_SCREEN_CANDIDATES,
    PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS,
    PUBLIC_EARNINGS_SCREEN_STATUS_KEYS,
    latest_completed_reporting_period,
    latest_mandatory_actual_reporting_period,
    screen_public_candidate_earnings_risk,
)
from app.services.public_candidate_notice_review import (
    MAX_NOTICE_LOOKBACK_CALENDAR_DAYS,
    MAX_NOTICE_REVIEW_CANDIDATES,
    NOTICE_HISTORY_SOURCE,
    NOTICE_LOOKBACK_CALENDAR_DAYS,
    NOTICE_REVIEW_SOURCE,
    review_public_candidate_notice_history,
    review_public_candidate_notices,
    validate_public_candidate_notice_review,
)
from app.services.public_market_breadth import (
    MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO,
    fetch_sina_public_market_breadth,
)
from app.services.research_only_safety import enforce_research_only_safety
from app.services.tencent_quote_service import (
    assess_cn_quote_freshness,
    fetch_tencent_daily_bars_sync,
    fetch_tencent_quote_sync,
    fetch_tencent_quotes_batched_sync,
    merge_tencent_quote_into_bars,
    normalize_cn_code,
)


class CLIError(Exception):
    """Expected CLI error with a stable JSON error code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "cli_error",
        exit_code: int = 2,
        stage: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.stage = stage
        self.details = deepcopy(details) if details else None


class _OpportunityDeadlineInterrupt(BaseException):
    """Cross provider Exception handlers before becoming a CLIError."""


holdings_app = typer.Typer(
    name="holdings",
    help="持仓数据 JSON CLI，供 Hermes/Agent 读取本地持仓 | Holdings JSON CLI for local agents",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

DEFAULT_CLI_USERNAME = "admin"
DEFAULT_BUY_LOT_SIZE = 100
MAX_MANUAL_OPPORTUNITY_CANDIDATES = MAX_EARNINGS_SCREEN_CANDIDATES
CN_MARKET_TIMEZONE = "Asia/Shanghai"
PORTFOLIO_POLICY = INVESTMENT_OBJECTIVE["portfolio"]
DEADLINE_EXPOSURE_BUFFER_PCT = 0.0
DEADLINE_MAX_SINGLE_CANDIDATE_PCT = PORTFOLIO_POLICY[
    "hard_single_symbol_cap_pct"
]
DEADLINE_TOTAL_LOSS_BUDGET_PCT = PORTFOLIO_POLICY[
    "total_new_position_loss_budget_pct"
]
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
_PUBLIC_RESEARCH_FALLBACK_DISCOVERY_STATUSES = frozenset(
    {
        "candidate_discovery_unavailable",
        "quote_universe_empty",
        "stale_quote_universe",
        "quote_universe_too_small",
    }
)
_PUBLIC_TRADE_AT_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_PUBLIC_A_SHARE_CODE_INPUT_PATTERN = re.compile(
    r"(?:\d{1,6}|(?:sh|sz|bj)\d{6}|\d{6}\.(?:sh|sz|bj))",
    re.IGNORECASE,
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


def _serialize_json(payload: Dict[str, Any], *, pretty: bool = False) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        default=_json_default,
        indent=2 if pretty else None,
        sort_keys=pretty,
    )


def _write_serialized_json(output: str, *, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    stream.write(output + "\n")


def _write_json(payload: Dict[str, Any], *, pretty: bool = False, stderr: bool = False) -> None:
    _write_serialized_json(
        _serialize_json(payload, pretty=pretty),
        stderr=stderr,
    )


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


def _connect_cli_database(
    configuration: Mapping[str, Optional[str]],
    *,
    timeout_cap_ms: Optional[int] = None,
) -> Any:
    if timeout_cap_ms is not None and (
        isinstance(timeout_cap_ms, bool)
        or not isinstance(timeout_cap_ms, int)
        or timeout_cap_ms <= 0
    ):
        raise CLIError(
            "MongoDB timeout cap 必须是正整数毫秒值",
            code="mongo_config_invalid",
            exit_code=4,
        )
    cap = timeout_cap_ms
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
    if cap is not None:
        for key in (
            "connectTimeoutMS",
            "socketTimeoutMS",
            "serverSelectionTimeoutMS",
        ):
            configured_timeout = client_options[key]
            client_options[key] = (
                cap
                if configured_timeout == 0
                else min(configured_timeout, cap)
            )
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
    if cap is not None:
        try:
            with pymongo.timeout(cap / 1000):
                client.admin.command("ping")
        except Exception:
            client.close()
            raise
    return client[expected_database]


def _get_database(*, timeout_cap_ms: Optional[int] = None) -> Any:
    configuration = _validate_cli_mongo_configuration()
    expected_database = str(configuration["expected_database"] or "").strip()
    try:
        if timeout_cap_ms is None:
            database = _connect_cli_database(configuration)
        else:
            database = _connect_cli_database(
                configuration,
                timeout_cap_ms=timeout_cap_ms,
            )
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


def _validate_deployment_objective(
    target_exposure_pct: Any,
    deployment_deadline: Any,
    *,
    as_of: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    target_missing = target_exposure_pct in (None, "")
    deadline_missing = deployment_deadline in (None, "")
    if target_missing and deadline_missing:
        return None
    if target_missing or deadline_missing:
        raise CLIError(
            "截止日仓位目标必须同时提供 target-exposure-pct 和 deployment-deadline",
            code="incomplete_deployment_objective",
        )
    try:
        target = float(target_exposure_pct)
    except (TypeError, ValueError) as exc:
        raise CLIError(
            "target-exposure-pct 必须是 0 到 100 之间的数字",
            code="invalid_target_exposure_pct",
        ) from exc
    if not math.isfinite(target) or not 0 < target <= 100:
        raise CLIError(
            "target-exposure-pct 必须大于 0 且不超过 100",
            code="invalid_target_exposure_pct",
        )
    try:
        deadline = date.fromisoformat(str(deployment_deadline).strip())
    except ValueError as exc:
        raise CLIError(
            "deployment-deadline 必须使用 YYYY-MM-DD",
            code="invalid_deployment_deadline",
        ) from exc
    market_date = as_of or datetime.now(ZoneInfo(CN_MARKET_TIMEZONE)).date()
    if deadline < market_date:
        raise CLIError(
            "deployment-deadline 不能早于当前交易日",
            code="deployment_deadline_expired",
        )

    maximum_policy_exposure = float(
        PORTFOLIO_POLICY["green_new_exposure_cap_pct"]
    )
    if target > maximum_policy_exposure:
        raise CLIError(
            f"target-exposure-pct 不能超过当前风险策略上限 {maximum_policy_exposure:.0f}",
            code="target_exposure_exceeds_policy_cap",
        )
    maximum_exposure_pct = min(
        maximum_policy_exposure,
        target + DEADLINE_EXPOSURE_BUFFER_PCT,
    )
    return {
        "mode": "deadline_target",
        "status": "active",
        "target_exposure_pct": round(target, 2),
        "maximum_exposure_pct": round(maximum_exposure_pct, 2),
        "lot_rounding_buffer_pct": round(maximum_exposure_pct - target, 2),
        "deadline": deadline.isoformat(),
        "max_single_candidate_pct": DEADLINE_MAX_SINGLE_CANDIDATE_PCT,
        "total_loss_budget_pct": DEADLINE_TOTAL_LOSS_BUDGET_PCT,
        "soft_constraints": [],
        "hard_constraints": [
            "account_data",
            "external_risk_gate",
            "a_share_market_gate",
            "quote_freshness",
            "earnings_risk_gate",
            "earnings_review_unavailable",
            "corporate_action_price_adjustment",
            "technical_price_plan",
            "trend_recovery_required",
            "limit_up_or_hot_move",
            "high_divergence",
            "recent_sale_cooldown",
        ],
        "is_reference_only": True,
    }


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
    context: Optional[OpportunityMarketContext] = None,
) -> Dict[str, Any]:
    if context is not None:
        if context.index_status != "ok":
            context_status = str(context.index_status or "index_context_unavailable")
            context_error = dict(context.index_error or {})
            index_regime = {
                "status": context_status,
                "level": "unknown",
                "new_position_allowed": False,
                "max_new_exposure_multiplier": 0.0,
                "benchmark_trade_date": None,
                "trade_date": None,
                "indices": [],
                "reason": "命令级主要指数上下文不可用，失败关闭。",
                "context_error": context_error,
                "is_reference_only": True,
            }
            breadth_regime = {
                "status": "not_evaluated",
                "level": "unknown",
                "actionable": False,
                "max_new_exposure_multiplier": None,
                "benchmark_trade_date": None,
                "source": "not_evaluated",
                "reason": "主要指数上下文不可用，未评估市场宽度。",
                "is_reference_only": True,
            }
            combined = combine_a_share_market_regimes(index_regime, breadth_regime)
            combined["mongo_breadth"] = dict(breadth_regime)
            return combined
        index_quotes = [dict(quote) for quote in context.index_quotes]
        resolved_benchmark_trade_date = context.benchmark_trade_date
    else:
        index_quotes = []
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
    mongo_breadth = dict(breadth_regime)
    if breadth_load_error:
        mongo_breadth["assessment_status"] = mongo_breadth.get("status")
        mongo_breadth["status"] = "load_failed"
    if breadth_regime.get("status") != "ok":
        if context is not None:
            public_result = context.ensure_public_snapshot()
        else:
            public_result = fetch_sina_public_market_breadth(
                benchmark_trade_date=resolved_benchmark_trade_date,
            )
        if public_result.get("status") == "ok":
            public_breadth = assess_a_share_market_breadth(
                public_result.get("rows", []),
                benchmark_trade_date=resolved_benchmark_trade_date,
            )
            public_breadth.update(
                {
                    "source": public_result.get("source"),
                    "provider_trade_date": public_result.get("provider_trade_date"),
                    "provider_time": public_result.get("provider_time"),
                }
            )
            for evidence_key in (
                "provider_expected_count",
                "provider_expected_exchange_counts",
                "raw_row_count",
                "unique_row_count",
                "exchange_counts",
                "total_coverage_ratio",
                "exchange_coverage_ratio",
                "excluded_stale_count",
                "excluded_future_time_count",
                "duplicate_count",
            ):
                if evidence_key in public_result:
                    public_breadth[evidence_key] = deepcopy(
                        public_result[evidence_key]
                    )
            if public_result.get("attempt_count") is not None:
                public_breadth["public_snapshot_attempt_count"] = public_result.get(
                    "attempt_count"
                )
            if public_result.get("retried_after_status") is not None:
                public_breadth["retried_after_status"] = public_result.get(
                    "retried_after_status"
                )
            if public_breadth.get("status") == "ok":
                breadth_regime = public_breadth
        else:
            breadth_regime["public_fallback"] = {
                key: value
                for key, value in public_result.items()
                if key != "rows"
            }
    combined = combine_a_share_market_regimes(index_regime, breadth_regime)
    combined["mongo_breadth"] = mongo_breadth
    return combined


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
    benchmark_session_dates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    user = select_user(db, username=username, email=email, user_id=user_id)
    items = _query_holdings(db, user_id=user["id"], code=code, market=market)
    items = [_with_current_price(db, item) for item in items]
    items = [_with_technical_price_plan(item) for item in items]
    if include_analysis:
        items = [_with_analysis(item) for item in items]
    benchmark_dates = (
        _benchmark_session_dates()
        if benchmark_session_dates is None
        else list(benchmark_session_dates)
    )
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


def _manual_candidate_earnings_review(
    definitions: List[Dict[str, Any]],
    *,
    benchmark_trade_date: Optional[str],
) -> Dict[str, Any]:
    codes = [str(definition.get("code") or "") for definition in definitions]
    if len(codes) > MAX_MANUAL_OPPORTUNITY_CANDIDATES:
        raise CLIError(
            f"手工候选最多支持 {MAX_MANUAL_OPPORTUNITY_CANDIDATES} 只",
            code="too_many_manual_candidates",
            stage="earnings_forecast_review",
        )

    try:
        report_period = latest_completed_reporting_period(benchmark_trade_date)
        actual_report_period = latest_mandatory_actual_reporting_period(
            benchmark_trade_date
        )
    except ValueError:
        return {
            "status": "earnings_market_context_unavailable",
            "source": EARNINGS_FORECAST_SOURCE,
            "actual_source": EARNINGS_ACTUAL_SOURCE,
            "report_period": None,
            "actual_report_period": None,
            "error_type": "BenchmarkTradeDateUnavailable",
            "screened_count": 0,
            "blocked_count": 0,
            "selected_count": 0,
            "blocked_codes": [],
            "selected_codes": [],
            "results": [],
        }

    raw_review = screen_public_candidate_earnings_risk(
        codes,
        benchmark_trade_date=benchmark_trade_date,
    )
    raw_status = raw_review.get("status") if isinstance(raw_review, Mapping) else None
    if raw_status != "ok":
        return {
            "status": (
                raw_status
                if isinstance(raw_status, str) and raw_status
                else "earnings_review_unavailable"
            ),
            "source": EARNINGS_FORECAST_SOURCE,
            "actual_source": EARNINGS_ACTUAL_SOURCE,
            "report_period": report_period,
            "actual_report_period": actual_report_period,
            "error_type": (
                raw_review.get("error_type")
                if isinstance(raw_review, Mapping)
                and isinstance(raw_review.get("error_type"), str)
                else "InvalidProviderResponse"
            ),
            "screened_count": 0,
            "blocked_count": 0,
            "selected_count": 0,
            "blocked_codes": [],
            "selected_codes": [],
            "results": [],
        }

    normalized, validation_error = validate_public_earnings_screen_metadata(
        raw_review,
        expected_codes=codes,
        expected_report_period=report_period,
        expected_actual_report_period=actual_report_period,
        benchmark_trade_date=str(benchmark_trade_date),
    )
    if validation_error or normalized is None:
        return {
            "status": "earnings_review_invalid",
            "source": EARNINGS_FORECAST_SOURCE,
            "actual_source": EARNINGS_ACTUAL_SOURCE,
            "report_period": report_period,
            "actual_report_period": actual_report_period,
            "error_type": validation_error or "InvalidEarningsReviewMetadata",
            "screened_count": 0,
            "blocked_count": 0,
            "selected_count": 0,
            "blocked_codes": [],
            "selected_codes": [],
            "results": [],
        }
    return normalized


def _apply_manual_candidate_earnings_gate(
    candidates: List[Dict[str, Any]],
    earnings_review: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    review_ok = earnings_review.get("status") == "ok"
    results_by_code = {
        str(result.get("code")): deepcopy(dict(result))
        for result in earnings_review.get("results", [])
        if isinstance(result, Mapping) and isinstance(result.get("code"), str)
    }
    gated_candidates: List[Dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = deepcopy(raw_candidate)
        code = str(candidate.get("code") or "")
        result = results_by_code.get(code) if review_ok else None
        unavailable = result is None
        blocked = bool(unavailable or result.get("blocks_new_position"))
        latest_actual = (
            result.get("latest_actual")
            if isinstance(result, Mapping)
            and isinstance(result.get("latest_actual"), Mapping)
            else {}
        )
        actual_risk_flags = [
            str(flag)
            for flag in latest_actual.get("risk_flags", [])
            if isinstance(flag, str) and flag
        ]
        blocker = (
            "earnings_review_unavailable" if unavailable else "earnings_risk_gate"
        )
        candidate["earnings_review"] = deepcopy(result) if result is not None else None
        candidate["earnings_gate"] = {
            "status": "unavailable" if unavailable else "blocked" if blocked else "passed",
            "blocks_new_position": blocked,
            "reason_code": blocker if blocked else "earnings_review_passed",
            "forecast_status": result.get("status") if result is not None else None,
            "actual_status": latest_actual.get("status"),
            "actual_risk_flags": actual_risk_flags,
        }
        if blocked:
            guarded_plan = (
                deepcopy(candidate.get("guarded_price_plan"))
                if isinstance(candidate.get("guarded_price_plan"), Mapping)
                else {}
            )
            guarded_plan["reference_actionable"] = bool(
                guarded_plan.get("reference_actionable")
                or guarded_plan.get("actionable")
            )
            guarded_plan["actionable"] = False
            guarded_plan["execution_blocked_by"] = list(
                dict.fromkeys(
                    list(guarded_plan.get("execution_blocked_by") or [])
                    + [blocker]
                )
            )
            guarded_plan["is_reference_only"] = True
            candidate["guarded_price_plan"] = guarded_plan
            risk_flags = list(candidate.get("risk_flags") or [])
            risk_flags.append(
                _risk_flag(
                    blocker,
                    "warning",
                    (
                        "业绩预告或最新实绩触发新仓门禁，仅保留观察。"
                        if not unavailable
                        else "业绩复核不可用，无法证明候选满足新仓条件。"
                    ),
                    forecast_status=(
                        result.get("status") if result is not None else None
                    ),
                    actual_status=latest_actual.get("status"),
                    actual_risk_flags=actual_risk_flags or None,
                    error_type=earnings_review.get("error_type") if unavailable else None,
                )
            )
            candidate["risk_flags"] = risk_flags
        gated_candidates.append(candidate)
    return gated_candidates


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
        "provider_updated_at": quote.get("provider_updated_at"),
        "quote_time_semantics": quote.get("quote_time_semantics"),
        "exchange_trade_time_verified": (
            quote.get("exchange_trade_time_verified") is True
        ),
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
        "volume": _round_number(quote.get("volume")),
        "quote_volume": _round_number(quote.get("quote_volume")),
        "turnover_rate": _round_number(quote.get("turnover_rate")),
        "volume_ratio": _round_number(quote.get("volume_ratio")),
        "pe_ratio": _round_number(quote.get("pe_ratio")),
        "pb_ratio": _round_number(quote.get("pb_ratio")),
        "circ_mv": _round_number(quote.get("circ_mv")),
        "total_mv": _round_number(quote.get("total_mv")),
        "intraday_range_pct": intraday_range_pct,
        "price_plan_adjustment_required": corporate_action_marker is not None,
    }
    if isinstance(quote.get("research_freshness"), Mapping):
        snapshot["research_freshness"] = deepcopy(
            dict(quote["research_freshness"])
        )
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
    allow_reference_price_plan: bool = False,
    quote_snapshots: Optional[Mapping[str, Dict[str, Any]]] = None,
    technical_plan_snapshots: Optional[Mapping[str, Dict[str, Any]]] = None,
    corporate_action_snapshots: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for definition in definitions:
        code = str(definition.get("code") or "").upper()
        if quote_snapshots is not None and code in quote_snapshots:
            injected_quote = quote_snapshots.get(code)
            quote = (
                deepcopy(dict(injected_quote))
                if isinstance(injected_quote, Mapping)
                else {}
            )
        else:
            quote = fetch_tencent_quote_sync(code) or {}
        snapshot = _quote_snapshot(quote, definition)
        objective_profile = classify_investment_objective(
            code,
            snapshot.get("name") or definition.get("name"),
        )
        if definition.get("objective_tier") in {
            "core",
            "related",
            "non_core",
        }:
            objective_profile = {
                key: definition.get(key, value)
                for key, value in objective_profile.items()
            }
        injected_corporate_action = (
            corporate_action_snapshots.get(code)
            if isinstance(corporate_action_snapshots, Mapping)
            else None
        )
        corporate_action = (
            deepcopy(dict(injected_corporate_action))
            if isinstance(injected_corporate_action, Mapping)
            else fetch_cn_dividend_calendar_sync(code)
        )
        upcoming_price_adjustment_required = bool(
            corporate_action.get("price_plan_adjustment_required")
        )
        price = snapshot.get("price")
        technical_plan: Dict[str, Any] = {
            "actionable": False,
            "status": "quote_not_actionable",
            "quote_status": snapshot.get("freshness", {}).get("status"),
        }
        quote_actionable = bool(snapshot.get("freshness", {}).get("actionable"))
        injected_plan = (
            technical_plan_snapshots.get(code)
            if isinstance(technical_plan_snapshots, Mapping)
            else None
        )
        pullback_plan: Optional[Dict[str, Any]] = None
        if isinstance(injected_plan, Mapping):
            technical_plan = deepcopy(dict(injected_plan))
        elif quote_actionable or (allow_reference_price_plan and price is not None):
            history = fetch_tencent_daily_bars_sync(code)
            if history.get("ok"):
                merged = merge_tencent_quote_into_bars(history.get("bars", []), snapshot)
                if merged.get("ok"):
                    technical_plan = build_technical_price_plan(
                        merged.get("bars", []),
                        current_price=price,
                    )
                    pullback_plan = build_pullback_price_plan(
                        merged.get("bars", []),
                        current_price=price,
                    )
                    technical_plan["history_status"] = history.get("status")
                    technical_plan["quote_merge_action"] = merged.get("merge_action")
                    pullback_plan["history_status"] = history.get("status")
                    pullback_plan["quote_merge_action"] = merged.get("merge_action")
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
        if pullback_plan is not None:
            pullback_plan = apply_net_reward_risk_gate(
                pullback_plan,
                quantity=buy_lot_size,
            )
            pullback_trend = (
                pullback_plan.get("trend_context")
                if isinstance(pullback_plan.get("trend_context"), dict)
                else {}
            )
            pullback_ready = bool(
                pullback_plan.get("status") == "ok"
                and pullback_plan.get("actionable") is True
                and pullback_trend.get("recovery_required") is not True
            )
            if not technical_plan.get("actionable") and pullback_ready:
                technical_plan = {
                    **pullback_plan,
                    "alternative_breakout_plan": technical_plan,
                }
            else:
                technical_plan = {
                    **technical_plan,
                    "entry_strategy": "breakout",
                    "alternative_pullback_plan": pullback_plan,
                }
        trend_context = (
            technical_plan.get("trend_context")
            if isinstance(technical_plan.get("trend_context"), dict)
            else {}
        )
        if trend_context.get("recovery_required") is True:
            technical_plan = {
                **technical_plan,
                "actionable": False,
                "status": "trend_recovery_required",
                "reference_actionable": False,
                "failed_gates": list(
                    dict.fromkeys(
                        list(technical_plan.get("failed_gates") or [])
                        + ["trend_recovery_required"]
                    )
                ),
            }
        if allow_reference_price_plan and not quote_actionable:
            fee_aware_trade = technical_plan.get("fee_aware_trade")
            if isinstance(fee_aware_trade, dict):
                fee_aware_trade = dict(fee_aware_trade)
                for order_key in ("entry_order", "stop_order", "target_order"):
                    fee_aware_trade.pop(order_key, None)
                technical_plan = {
                    **technical_plan,
                    "fee_aware_trade": fee_aware_trade,
                }
            execution_blocked_by = ["quote_freshness"]
            if cash is None:
                execution_blocked_by.append("account_data_unavailable")
            if trend_context.get("recovery_required") is True:
                execution_blocked_by.append("trend_recovery_required")
            technical_plan = {
                **technical_plan,
                "reference_actionable": bool(technical_plan.get("actionable")),
                "actionable": False,
                "quote_status": snapshot.get("freshness", {}).get("status"),
                "execution_blocked_by": execution_blocked_by,
                "is_reference_only": True,
            }
        one_lot_amount = round(price * buy_lot_size, 2) if price is not None else None
        affordable = bool(cash is not None and one_lot_amount is not None and one_lot_amount <= cash)
        same_theme = bool(definition.get("theme") in holding_themes)
        cash_after_one_lot = round(cash - one_lot_amount, 2) if affordable and cash is not None and one_lot_amount is not None else None
        cash_usage_pct = round(one_lot_amount / cash * 100, 2) if cash and one_lot_amount is not None else None
        candidate_flags: List[Dict[str, Any]] = []
        if trend_context.get("recovery_required") is True:
            candidate_flags.append(
                _risk_flag(
                    "trend_recovery_required",
                    "warning",
                    "股价处于深回撤且短期均线空头排列，重新站上短期均线前仅观察。",
                    drawdown_from_20d_high_pct=trend_context.get(
                        "drawdown_from_20d_high_pct"
                    ),
                    distance_to_entry_pct=trend_context.get(
                        "distance_to_entry_pct"
                    ),
                )
            )
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
                    "腾讯提供方快照更新时间不满足时效门禁，不能生成仓位数量。",
                    quote_status=snapshot.get("freshness", {}).get("status"),
                )
            )
        elif (
            not technical_plan.get("actionable")
            and trend_context.get("recovery_required") is not True
        ):
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
                **objective_profile,
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
    deployment_objective: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    cash = _round_number(account.get("cash_or_unallocated"))
    market_session = market_session or {}
    requires_quote_refresh = bool(market_session.get("quote_stale_risk"))
    actionable_equity = actionable_equity or {"value": None, "actionable": False}
    equity_value = _round_number(actionable_equity.get("value"))
    current_market_value = _round_number(account.get("known_market_value"))
    if current_market_value is None and not holdings_risk:
        current_market_value = 0.0
    objective = (
        deepcopy(dict(deployment_objective))
        if isinstance(deployment_objective, Mapping)
        and deployment_objective.get("mode") == "deadline_target"
        else None
    )
    deadline_mode = objective is not None
    objective_account_ready = bool(
        deadline_mode
        and cash is not None
        and equity_value is not None
        and current_market_value is not None
    )

    if deadline_mode:
        target_exposure_pct = float(objective.get("target_exposure_pct") or 0)
        maximum_exposure_pct = float(objective.get("maximum_exposure_pct") or 0)
        initial_deploy_cap_pct = maximum_exposure_pct
        max_single_candidate_pct = float(
            objective.get("max_single_candidate_pct")
            or DEADLINE_MAX_SINGLE_CANDIDATE_PCT
        )
        reserve_cash_pct = max(0.0, round(100.0 - maximum_exposure_pct, 2))
        if objective_account_ready:
            target_exposure_amount = round(
                float(equity_value) * target_exposure_pct / 100,
                2,
            )
            maximum_exposure_amount = round(
                float(equity_value) * maximum_exposure_pct / 100,
                2,
            )
            minimum_exposure_gap = max(
                0.0,
                round(target_exposure_amount - float(current_market_value), 2),
            )
            maximum_exposure_gap = (
                max(
                    0.0,
                    round(
                        maximum_exposure_amount - float(current_market_value),
                        2,
                    ),
                )
                if minimum_exposure_gap > 0
                else 0.0
            )
            initial_deploy_cap_amount = min(float(cash), maximum_exposure_gap)
            max_single_candidate_amount = round(
                float(equity_value) * max_single_candidate_pct / 100,
                2,
            )
        else:
            target_exposure_amount = None
            maximum_exposure_amount = None
            minimum_exposure_gap = None
            maximum_exposure_gap = None
            initial_deploy_cap_amount = 0.0
            max_single_candidate_amount = 0.0
    else:
        target_exposure_pct = None
        maximum_exposure_pct = None
        target_exposure_amount = None
        maximum_exposure_amount = None
        minimum_exposure_gap = None
        maximum_exposure_gap = None
        initial_deploy_cap_pct = float(
            PORTFOLIO_POLICY["green_new_exposure_cap_pct"]
        )
        reserve_cash_pct = float(PORTFOLIO_POLICY["reserve_cash_pct"])
        max_single_candidate_pct = float(
            PORTFOLIO_POLICY["hard_single_symbol_cap_pct"]
        )
        initial_deploy_cap_amount = (
            round(cash * initial_deploy_cap_pct / 100, 2)
            if cash is not None
            else None
        )
        max_single_candidate_amount = (
            round(cash * max_single_candidate_pct / 100, 2)
            if cash is not None
            else None
        )
    remaining_initial_cap = initial_deploy_cap_amount
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
    if deadline_mode:
        effective_new_exposure_cap = round(
            min(
                float(initial_deploy_cap_amount or 0),
                external_new_exposure * market_multiplier,
            ),
            2,
        )
        effective_market_multiplier = market_multiplier
        total_loss_budget_pct = float(
            objective.get("total_loss_budget_pct")
            or DEADLINE_TOTAL_LOSS_BUDGET_PCT
        )
    else:
        effective_new_exposure_cap = round(
            external_new_exposure * market_multiplier,
            2,
        )
        effective_market_multiplier = market_multiplier
        total_loss_budget_pct = float(
            PORTFOLIO_POLICY["total_new_position_loss_budget_pct"]
        )
    remaining_new_exposure = effective_new_exposure_cap
    total_loss_budget = (
        round(equity_value * total_loss_budget_pct / 100, 2)
        if equity_value is not None
        else 0.0
    )
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
        blocked_by_earnings_risk = "earnings_risk_gate" in risk_keys
        blocked_by_earnings_review = "earnings_review_unavailable" in risk_keys
        blocked_by_trend_recovery = "trend_recovery_required" in risk_keys
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
        elif blocked_by_earnings_risk:
            risk_gate = "blocked_by_earnings_risk"
        elif blocked_by_earnings_review:
            risk_gate = "blocked_by_earnings_review"
        elif blocked_by_trend_recovery:
            risk_gate = "blocked_by_trend_recovery"
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
        if blocked_by_earnings_risk:
            base_failed_gates.append("earnings_risk_gate")
        if blocked_by_earnings_review:
            base_failed_gates.append("earnings_review_unavailable")
        if blocked_by_trend_recovery:
            base_failed_gates.append("trend_recovery_required")
        if blocked_by_hot_move:
            base_failed_gates.append("limit_up_or_hot_move")
        if blocked_by_divergence:
            base_failed_gates.append("high_divergence")
        if not external_risk_gate.get("actionable"):
            base_failed_gates.append("external_risk_gate")
        if not a_share_market_gate.get("new_position_allowed"):
            base_failed_gates.append("a_share_market_gate")
        if deadline_mode and not objective_account_ready:
            base_failed_gates.append("deployment_objective_account_data")
        quote_freshness = candidate.get("quote", {}).get("freshness", {})
        if not quote_freshness.get("actionable"):
            base_failed_gates.append("quote_freshness")
        candidate_requires_quote_refresh = bool(
            requires_quote_refresh or not quote_freshness.get("actionable")
        )
        guarded_plan = candidate.get("guarded_price_plan") if isinstance(candidate.get("guarded_price_plan"), dict) else {}
        if (
            not guarded_plan.get("actionable")
            and not blocked_by_trend_recovery
        ):
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
            per_position_loss_budget = (
                round(
                    float(equity_value)
                    * float(PORTFOLIO_POLICY["per_position_loss_budget_pct"])
                    / 100,
                    2,
                )
                if equity_value is not None
                else 0.0
            )
            risk_sizing = size_ashare_candidate(
                entry_price=executable["entry"],
                stop_price=executable["stop"],
                target_price=executable["target"],
                actionable_equity=equity_value,
                cash_available=remaining_cash,
                original_cash=cash or 0.0,
                remaining_new_exposure=remaining_new_exposure,
                remaining_initial_deploy=remaining_initial_cap or 0.0,
                remaining_loss_budget=min(
                    remaining_loss_budget,
                    per_position_loss_budget,
                ),
                existing_symbol_market_value=existing_symbol_market_value,
                candidate_cash_cap_amount=max_single_candidate_amount,
                post_trade_symbol_cap_pct=max_single_candidate_pct,
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
            elif "earnings_risk_gate" in failed_gates:
                risk_gate = "blocked_by_earnings_risk"
            elif "earnings_review_unavailable" in failed_gates:
                risk_gate = "blocked_by_earnings_review"
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
            evaluation_status = (
                "stale_until_refresh"
                if candidate_requires_quote_refresh
                else "current"
            )
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
                                    "wait_for_trend_recovery"
                                    if blocked_by_trend_recovery
                                    else (
                                        "cooldown_after_hot_move"
                                        if blocked_by_hot_move
                                        else (
                                            "wait_for_divergence_cooldown"
                                            if blocked_by_divergence
                                            else (
                                                "refresh_quote_before_action"
                                                if candidate_requires_quote_refresh
                                                else "confirm_guarded_technical_entry"
                                            )
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
                                    "股价处于深回撤和短期空头排列，重新站上短期均线前仅观察。"
                                    if blocked_by_trend_recovery
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
                    )
                ),
                **({"cooldown_checks": cooldown_checks} if cooldown_checks else {}),
                **({"cooldown_evaluation": cooldown_evaluation} if cooldown_evaluation else {}),
            }
        )

    deployment_objective_result = None
    if objective is not None:
        planned_new_exposure = round(
            effective_new_exposure_cap - remaining_new_exposure,
            2,
        )
        if objective_account_ready:
            current_exposure_pct = round(
                float(current_market_value) / float(equity_value) * 100,
                2,
            )
            projected_exposure_amount = round(
                float(current_market_value) + planned_new_exposure,
                2,
            )
            projected_exposure_pct = round(
                projected_exposure_amount / float(equity_value) * 100,
                2,
            )
            target_shortfall_amount = max(
                0.0,
                round(float(target_exposure_amount) - projected_exposure_amount, 2),
            )
            target_met = projected_exposure_pct >= float(target_exposure_pct)
            if current_exposure_pct >= float(target_exposure_pct):
                objective_status = "already_met"
            elif target_met:
                objective_status = "planned_target_met"
            else:
                objective_status = "target_shortfall"
        else:
            current_exposure_pct = None
            projected_exposure_amount = None
            projected_exposure_pct = None
            target_shortfall_amount = None
            target_met = False
            objective_status = "account_data_unavailable"
        deployment_objective_result = {
            **objective,
            "status": objective_status,
            "account_data_actionable": objective_account_ready,
            "current_exposure_amount": current_market_value,
            "current_exposure_pct": current_exposure_pct,
            "target_exposure_amount": target_exposure_amount,
            "maximum_exposure_amount": maximum_exposure_amount,
            "minimum_exposure_gap": minimum_exposure_gap,
            "maximum_exposure_gap": maximum_exposure_gap,
            "effective_new_exposure_cap": effective_new_exposure_cap,
            "planned_new_exposure": planned_new_exposure,
            "projected_exposure_amount": projected_exposure_amount,
            "projected_exposure_pct": projected_exposure_pct,
            "target_shortfall_amount": target_shortfall_amount,
            "target_met": target_met,
            "hard_constraint_assessment": {
                "external_risk_level": external_risk_gate.get("level"),
                "external_risk_actionable": external_risk_gate.get("actionable"),
                "a_share_market_level": a_share_market_gate.get("level"),
                "a_share_new_position_allowed": a_share_market_gate.get(
                    "new_position_allowed"
                ),
                "effect": "blocks_new_position_when_gate_is_closed",
            },
        }

    mode = "deadline_target" if deadline_mode else (
        "position_risk_first" if holdings_risk else "cash_ready"
    )
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
        "preferred_single_candidate_pct": float(
            PORTFOLIO_POLICY["preferred_single_symbol_pct"]
        ),
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
        "effective_market_multiplier": effective_market_multiplier,
        "effective_new_exposure_cap": effective_new_exposure_cap,
        "deployment_objective": deployment_objective_result,
        "actionable_equity": actionable_equity,
        "candidate_lot_plan": candidate_lot_plan,
        "investment_objective": {
            "id": INVESTMENT_OBJECTIVE["id"],
            "label": INVESTMENT_OBJECTIVE["label"],
        },
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
    deployment_objective: Optional[Mapping[str, Any]] = None,
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
        deployment_objective=deployment_objective,
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
    context: Optional[OpportunityMarketContext] = None,
    precomputed_manual_earnings_review: Optional[Mapping[str, Any]] = None,
    target_exposure_pct: Optional[float] = None,
    deployment_deadline: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_external_risk_level = _validate_external_risk_level(external_risk_level)
    deployment_objective = _validate_deployment_objective(
        target_exposure_pct,
        deployment_deadline,
        as_of=(context.now.date() if context is not None else None),
    )
    if deployment_objective is not None and not candidate_codes:
        raise CLIError(
            "截止日仓位目标必须显式提供 --candidate-code，避免全市场研究结果被直接转成仓位",
            code="deployment_objective_requires_candidates",
        )
    if buy_lot_size != DEFAULT_BUY_LOT_SIZE:
        raise CLIError(
            "A股仓位计算固定使用100股一手，不支持自定义 lot-size",
            code="invalid_lot_size",
        )
    context_benchmark_trade_date = None
    context_benchmark_dates = None
    if context is not None:
        if context.index_status == "ok" and context.benchmark_trade_date:
            context_benchmark_trade_date = context.benchmark_trade_date
            context_benchmark_dates = [context_benchmark_trade_date]
        else:
            context_benchmark_dates = []
    holdings_payload = build_holdings_payload(
        db,
        username=username,
        email=email,
        user_id=user_id,
        include_analysis=True,
        benchmark_session_dates=context_benchmark_dates,
    )
    data = holdings_payload["data"]
    account = _build_account_payload(data["summary"], data["settings"], buy_lot_size)
    holdings_risk = _build_holdings_risk(data["items"], account.get("estimated_equity"))
    actionable_equity = _resolve_actionable_equity(account, holdings_risk)
    external_risk_gate = build_external_risk_gate(
        normalized_external_risk_level,
        actionable_equity=actionable_equity.get("value"),
    )
    if context is not None:
        benchmark_trade_date = context_benchmark_trade_date
        benchmark_dates = context_benchmark_dates or []
        a_share_market_gate = _build_a_share_market_gate(
            benchmark_trade_date,
            db=db,
            context=context,
        )
    else:
        benchmark_dates = _benchmark_session_dates()
        benchmark_trade_date = max(benchmark_dates) if benchmark_dates else None
        a_share_market_gate = _build_a_share_market_gate(benchmark_trade_date, db=db)
    holding_themes = {risk.get("theme") for risk in holdings_risk if risk.get("theme")}
    manual_earnings_review: Optional[Dict[str, Any]] = None
    if candidate_codes:
        definitions = _candidate_definitions(candidate_codes)
        manual_earnings_review = (
            deepcopy(dict(precomputed_manual_earnings_review))
            if isinstance(precomputed_manual_earnings_review, Mapping)
            else _manual_candidate_earnings_review(
                definitions,
                benchmark_trade_date=benchmark_trade_date,
            )
        )
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
    trade_context = _build_trade_context(db, user_id=data["user"]["id"])
    candidates = _build_opportunity_candidates(
        definitions,
        cash=account.get("cash_or_unallocated"),
        buy_lot_size=buy_lot_size,
        holding_themes=holding_themes,
        allow_reference_price_plan=bool(candidate_codes),
    )
    if manual_earnings_review is not None:
        candidates = _apply_manual_candidate_earnings_gate(
            candidates,
            manual_earnings_review,
        )
    risk_flags = _build_opportunity_risk_flags(holdings_risk, candidates, account)
    market_session = _market_session_context()
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
        deployment_objective,
    )
    breadth_source = str(
        (a_share_market_gate.get("breadth_regime") or {}).get("source") or ""
    )
    breadth_meta_source = (
        "akshare_sina_public_breadth"
        if breadth_source == "akshare.sina.stock_zh_a_spot"
        else "mongo_market_breadth"
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
            "earnings_review": manual_earnings_review,
            "deployment_objective": (
                brief.get("cash_deployment_plan", {}).get(
                    "deployment_objective"
                )
                if deployment_objective is not None
                else None
            ),
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
            "schema_version": 7,
            "source": (
                "mongo.user_holdings+analysis_reports+candidate_discovery+"
                + f"{breadth_meta_source}+tencent_quotes+tencent_major_indices+"
                + (
                    f"{EARNINGS_REVIEW_SOURCE}+"
                    if manual_earnings_review is not None
                    else ""
                )
                + "cninfo_dividend_calendar"
            ),
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def build_research_only_opportunities_payload(
    *,
    candidate_codes: List[str],
    username: Optional[str] = None,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    external_risk_level: Optional[str] = None,
    database_status: Optional[Dict[str, Any]] = None,
    context: Optional[OpportunityMarketContext] = None,
    precomputed_manual_earnings_review: Optional[Mapping[str, Any]] = None,
    target_exposure_pct: Optional[float] = None,
    deployment_deadline: Optional[str] = None,
) -> Dict[str, Any]:
    """Build manual-candidate research output without account or holdings data."""
    normalized_external_risk_level = _validate_external_risk_level(external_risk_level)
    deployment_objective = _validate_deployment_objective(
        target_exposure_pct,
        deployment_deadline,
        as_of=(context.now.date() if context is not None else None),
    )
    if deployment_objective is not None:
        deployment_objective = {
            **deployment_objective,
            "status": "account_data_unavailable",
            "account_data_actionable": False,
            "target_met": False,
            "reason": "数据库不可用，无法核验本金、当前仓位和可用现金，禁止生成仓位数量。",
        }
    definitions = _candidate_definitions(candidate_codes)
    if not definitions:
        raise CLIError(
            "研究模式至少需要一个有效的 --candidate-code",
            code="candidate_codes_required",
        )

    benchmark_trade_date = (
        context.benchmark_trade_date
        if context is not None
        and context.index_status == "ok"
        and context.benchmark_trade_date
        else max(_benchmark_session_dates(), default=None)
    )
    manual_earnings_review = (
        deepcopy(dict(precomputed_manual_earnings_review))
        if isinstance(precomputed_manual_earnings_review, Mapping)
        else _manual_candidate_earnings_review(
            definitions,
            benchmark_trade_date=benchmark_trade_date,
        )
    )

    candidates = _build_opportunity_candidates(
        definitions,
        cash=None,
        buy_lot_size=DEFAULT_BUY_LOT_SIZE,
        holding_themes=set(),
        allow_reference_price_plan=True,
    )
    candidates = _apply_manual_candidate_earnings_gate(
        candidates,
        manual_earnings_review,
    )
    research_candidates = []
    for candidate in candidates:
        research_candidates.append(
            {
                **candidate,
                "affordable_with_cash": None,
                "cash_after_one_lot": None,
                "cash_usage_pct": None,
                "decision": {
                    "action": "observe",
                    "actionable": False,
                    "reason_code": "account_data_unavailable",
                    "suggested_lots": 0,
                    "suggested_quantity": 0,
                },
            }
        )

    effective_database_status = dict(
        database_status or {"status": "unavailable", "error_code": "database_error"}
    )
    if context is None:
        market_status = build_market_status_payload(
            None,
            database_status=effective_database_status,
        )
    else:
        market_status = build_market_status_payload(
            None,
            database_status=effective_database_status,
            context=context,
        )
    external_risk_gate = build_external_risk_gate(
        normalized_external_risk_level,
        actionable_equity=None,
    )
    market_status_source = str(
        (market_status.get("meta") or {}).get("source") or "tencent_major_indices"
    )
    return {
        "ok": True,
        "data": {
            "mode": "research_only",
            "database": effective_database_status,
            "requested_identity": {
                "username": username,
                "email": email,
                "user_id": user_id,
                "resolved": False,
            },
            "account": {
                "status": "unavailable",
                "actionable": False,
                "reason_code": "database_unavailable",
                "configured_total_assets": None,
                "cash_or_unallocated": None,
                "estimated_equity": None,
            },
            "decision": {
                "action": "observe",
                "actionable": False,
                "reason_code": "account_data_unavailable",
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "reason": "数据库不可用，无法核验账户、持仓、现金和近期交易，禁止生成仓位数量。",
            },
            "external_risk_gate": external_risk_gate,
            "market_status": market_status.get("data", {}),
            "candidate_discovery": {
                "status": "manual_candidates",
                "source": "cli.candidate_code",
                "definitions_count": len(definitions),
                "selected_codes": [definition.get("code") for definition in definitions],
            },
            "earnings_review": manual_earnings_review,
            "deployment_objective": deployment_objective,
            "candidates": research_candidates,
            "context": {
                "horizon": "未来两个交易日",
                "quote_source": "tencent",
                "available_data": ["tencent_quote", "technical_price_plan", "corporate_action"],
                "unavailable_data": ["account", "holdings", "cash", "recent_trades", "position_sizing"],
            },
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 7,
            "source": (
                "manual_candidates+tencent_quotes+tencent_daily_bars+"
                f"{EARNINGS_REVIEW_SOURCE}+{market_status_source}+"
                "cninfo_dividend_calendar"
            ),
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def _public_research_candidate_decision(reason_code: str) -> Dict[str, Any]:
    return {
        "action": "observe",
        "actionable": False,
        "reason_code": reason_code,
        "suggested_lots": 0,
        "suggested_quantity": 0,
    }


_PUBLIC_CANDIDATE_DISCOVERY_SCALAR_FIELDS = (
    "mode",
    "status",
    "source",
    "benchmark_trade_date",
    "checked_at",
    "freshness",
    "degraded",
    "cache_age_seconds",
    "attempt_count",
    "provider_health",
    "provider_expected_count",
    "raw_row_count",
    "unique_row_count",
    "universe_count",
    "total_coverage_ratio",
    "eligible_count",
    "public_preselected_count",
    "tencent_requested_count",
    "tencent_minimum_verified_count",
    "tencent_verified_count",
    "tencent_rank_population_count",
    "selected_count",
    "technical_checked_count",
    "technical_deep_check_status",
    "technical_deep_check_error_type",
    "technical_screened_count",
    "technical_passed_count",
    "technical_selected_count",
    "deep_research_selected_count",
    "technical_closest_rejection_count",
    "earnings_screened_count",
    "earnings_blocked_count",
    "earnings_selected_count",
    "earnings_report_period",
    "earnings_actual_report_period",
    "notice_reviewed_count",
    "notice_hard_blocked_count",
    "notice_manual_review_count",
    "permission_prefilter_excluded_count",
)
_PUBLIC_DISCOVERY_REJECTION_KEYS = set(PUBLIC_CANDIDATE_REJECTION_KEYS)
_PUBLIC_DISCOVERY_QUALITY_KEYS = {
    "invalid_volume_ratio",
    "missing_volume_ratio",
    "non_ideal_volume_ratio",
    "reduced_amplitude_quality",
    "reduced_move_quality",
    "reduced_turnover_quality",
}
_PUBLIC_QUOTE_FIELDS = (
    "source",
    "provider_timestamp",
    "trade_at",
    "trade_date",
    "received_at",
    "code",
    "name",
    "price",
    "pct_chg",
    "change",
    "open",
    "high",
    "low",
    "pre_close",
    "amount",
    "volume",
    "quote_volume",
    "turnover_rate",
    "volume_ratio",
    "pe_ratio",
    "pb_ratio",
    "circ_mv",
    "total_mv",
    "intraday_range_pct",
    "price_plan_adjustment_required",
    "corporate_action_marker",
)
_PUBLIC_QUOTE_FRESHNESS_FIELDS = (
    "actionable",
    "status",
    "reason",
    "source",
    "trade_at",
    "trade_date",
    "age_seconds",
    "session",
)
_PUBLIC_RESEARCH_QUOTE_FRESHNESS_FIELDS = (
    "data_complete",
    "status",
    "reason",
    "source",
    "trade_at",
    "provider_updated_at",
    "quote_time_semantics",
    "exchange_trade_time_verified",
    "trade_date",
    "benchmark_trade_date",
    "age_seconds",
    "session",
)
_PUBLIC_DISCOVERY_DEFINITION_SCALAR_FIELDS = (
    "code",
    "name",
    "exchange",
    "theme",
    "theme_label",
    "objective_id",
    "objective_label",
    "objective_tier",
    "objective_tier_label",
    "objective_segment",
    "objective_match_score",
    "objective_reason",
    "price",
    "pct_change",
    "amount",
    "one_lot_amount",
    "bucket",
    "trade_date",
    "amount_percentile",
    "move_quality",
    "public_score",
    "tencent_price",
    "tencent_pct_change",
    "tencent_amount",
    "tencent_trade_at",
    "tencent_source",
    "tencent_bucket",
    "turnover_rate",
    "volume_ratio",
    "amplitude",
    "circ_mv",
    "total_mv",
    "limit_up",
    "tencent_move_quality",
    "turnover_quality",
    "volume_ratio_quality",
    "amplitude_quality",
    "tencent_amount_percentile",
    "tencent_market_cap_percentile",
    "tencent_score",
    "tencent_one_lot_amount",
    "tencent_quality_rank",
    "selection_lane",
    "breakout_price",
    "invalidation_price",
    "note",
)


def _public_scalar(value: Any) -> bool:
    return value is None or isinstance(
        value,
        (str, int, float, Decimal, bool),
    )


def _copy_public_scalar_fields(
    value: Any,
    fields: Iterable[str],
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        field: deepcopy(value[field])
        for field in fields
        if field in value and _public_scalar(value[field])
    }


def _copy_public_scalar_list(value: Any) -> List[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    return [deepcopy(item) for item in value if _public_scalar(item)]


def _sanitize_public_count_mapping(
    value: Any,
    allowed_keys: set[str],
) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): int(count)
        for key, count in value.items()
        if key in allowed_keys
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
    }


def _sanitize_public_candidate_discovery(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        _PUBLIC_CANDIDATE_DISCOVERY_SCALAR_FIELDS,
    )
    if not isinstance(value, Mapping):
        return sanitized

    for field in (
        "provider_expected_exchange_counts",
        "exchange_counts",
        "exchange_coverage_ratio",
    ):
        raw_mapping = value.get(field)
        sanitized[field] = _copy_public_scalar_fields(
            raw_mapping,
            ("sh", "sz", "bj"),
        )
    sanitized["rejection_counts"] = _sanitize_public_count_mapping(
        value.get("rejection_counts"),
        _PUBLIC_DISCOVERY_REJECTION_KEYS,
    )
    sanitized["quality_counts"] = _sanitize_public_count_mapping(
        value.get("quality_counts"),
        _PUBLIC_DISCOVERY_QUALITY_KEYS,
    )
    sanitized["technical_screen_status_counts"] = _sanitize_public_count_mapping(
        value.get("technical_screen_status_counts"),
        set(PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS),
    )
    raw_closest_rejections = value.get("technical_closest_rejections")
    sanitized["technical_closest_rejections"] = (
        [
            _copy_public_scalar_fields(
                item,
                (
                    "code",
                    "name",
                    "status",
                    "net_reward_risk",
                    "min_net_reward_risk",
                    "gap_to_min_net_reward_risk",
                    "tencent_score",
                    "earnings_review_status",
                    "actionable",
                    "is_reference_only",
                ),
            )
            for item in raw_closest_rejections
            if isinstance(item, Mapping)
        ]
        if isinstance(raw_closest_rejections, list)
        else []
    )
    if "permission_prefilter_excluded" in value:
        raw_permission_exclusions = value.get("permission_prefilter_excluded")
        sanitized["permission_prefilter_excluded"] = (
            [
                _copy_public_scalar_fields(
                    item,
                    ("code", "name", "reason_code"),
                )
                for item in raw_permission_exclusions
                if isinstance(item, Mapping)
            ]
            if isinstance(raw_permission_exclusions, list)
            else []
        )
    raw_provider_errors = value.get("provider_errors")
    sanitized["provider_errors"] = (
        [
            _copy_public_scalar_fields(
                item,
                ("provider", "status", "error_type", "checked_at"),
            )
            for item in raw_provider_errors
            if isinstance(item, Mapping)
        ]
        if isinstance(raw_provider_errors, list)
        else []
    )
    sanitized["earnings_screen_status_counts"] = _sanitize_public_count_mapping(
        value.get("earnings_screen_status_counts"),
        set(PUBLIC_EARNINGS_SCREEN_STATUS_KEYS),
    )
    sanitized["earnings_actual_status_counts"] = _sanitize_public_count_mapping(
        value.get("earnings_actual_status_counts"),
        set(PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS),
    )

    earnings_results: List[Dict[str, Any]] = []
    raw_earnings_results = value.get("earnings_screen_results")
    if isinstance(raw_earnings_results, list):
        for raw_result in raw_earnings_results:
            if not isinstance(raw_result, Mapping):
                continue
            result = _copy_public_scalar_fields(
                raw_result,
                (
                    "code",
                    "status",
                    "blocks_new_position",
                    "announcement_date",
                    "reason_summary",
                ),
            )
            result["forecast_types"] = _copy_public_scalar_list(
                raw_result.get("forecast_types")
            )
            result["loss_metrics"] = _copy_public_scalar_list(
                raw_result.get("loss_metrics")
            )
            latest_actual = raw_result.get("latest_actual")
            if isinstance(latest_actual, Mapping):
                result["latest_actual"] = _copy_public_scalar_fields(
                    latest_actual,
                    (
                        "status",
                        "report_period",
                        "announcement_date",
                        "net_profit",
                        "net_profit_yoy_pct",
                        "net_profit_qoq_pct",
                        "revenue",
                        "revenue_yoy_pct",
                        "revenue_qoq_pct",
                        "eps",
                        "book_value_per_share",
                        "roe_pct",
                        "operating_cash_flow_per_share",
                        "gross_margin_pct",
                        "industry",
                    ),
                )
                result["latest_actual"]["risk_flags"] = (
                    _copy_public_scalar_list(
                        latest_actual.get("risk_flags")
                    )
                )
            evidence = raw_result.get("evidence")
            result["evidence"] = [
                _copy_public_scalar_fields(
                    item,
                    (
                        "metric",
                        "forecast_type",
                        "forecast_value",
                        "forecast_change_pct",
                        "forecast_text",
                    ),
                )
                for item in evidence
                if isinstance(item, Mapping)
            ] if isinstance(evidence, list) else []
            earnings_results.append(result)
    sanitized["earnings_screen_results"] = earnings_results

    stage_sources: Dict[str, Dict[str, Any]] = {}
    raw_stage_sources = value.get("stage_sources")
    if isinstance(raw_stage_sources, Mapping):
        for stage in ("public_snapshot", "tencent_verification"):
            if stage in raw_stage_sources:
                raw_stage = raw_stage_sources.get(stage)
                stage_sources[stage] = _copy_public_scalar_fields(
                    raw_stage,
                    (
                        "provider",
                        "status",
                        "checked_at",
                        "freshness",
                        "degraded",
                    ),
                )
                raw_stage_errors = (
                    raw_stage.get("provider_errors")
                    if isinstance(raw_stage, Mapping)
                    else None
                )
                if isinstance(raw_stage_errors, list):
                    stage_sources[stage]["provider_errors"] = [
                        _copy_public_scalar_fields(
                            item,
                            ("provider", "status", "error_type", "checked_at"),
                        )
                        for item in raw_stage_errors
                        if isinstance(item, Mapping)
                    ]
    sanitized["stage_sources"] = stage_sources
    return sanitized


def _sanitize_public_candidate_quote(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(value, _PUBLIC_QUOTE_FIELDS)
    if isinstance(value, Mapping) and isinstance(value.get("freshness"), Mapping):
        sanitized["freshness"] = _copy_public_scalar_fields(
            value.get("freshness"),
            _PUBLIC_QUOTE_FRESHNESS_FIELDS,
        )
    if isinstance(value, Mapping) and isinstance(
        value.get("research_freshness"), Mapping
    ):
        sanitized["research_freshness"] = _copy_public_scalar_fields(
            value.get("research_freshness"),
            _PUBLIC_RESEARCH_QUOTE_FRESHNESS_FIELDS,
        )
    return sanitized


def _sanitize_public_discovery_definition(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        _PUBLIC_DISCOVERY_DEFINITION_SCALAR_FIELDS,
    )
    if isinstance(value, Mapping) and isinstance(
        value.get("observation_zone"), Mapping
    ):
        sanitized["observation_zone"] = _copy_public_scalar_fields(
            value.get("observation_zone"),
            ("low", "high"),
        )
    elif isinstance(value, Mapping) and "observation_zone" in value:
        sanitized["observation_zone"] = None
    return sanitized


def _sanitize_public_research_watch_levels(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        (
            "status",
            "actionable",
            "is_reference_only",
            "current_price",
            "nearest_support",
            "nearest_resistance",
        ),
    )
    if isinstance(value, Mapping):
        for field in (
            "lower_supports",
            "higher_resistances",
            "supports",
            "resistances",
        ):
            if field in value:
                sanitized[field] = _copy_public_scalar_list(value.get(field))
    return sanitized


def _sanitize_public_guarded_price_plan(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        (
            "actionable",
            "status",
            "quote_status",
            "required_rows",
            "available_rows",
            "source",
            "as_of",
            "current_price",
            "stop_loss_price",
            "suggested_buy_price",
            "suggested_sell_price",
            "target_price",
            "history_status",
            "quote_merge_action",
            "price_ratio",
            "reason",
            "min_net_reward_risk",
            "entry_strategy",
            "entry_basis",
            "entry_source",
            "stop_basis",
            "stop_source",
            "target_source",
            "pullback_required",
            "distance_to_entry_pct",
            "max_pullback_distance_pct",
            "reference_actionable",
            "is_reference_only",
        ),
    )
    if not isinstance(value, Mapping):
        return sanitized

    for field in (
        "support_candidates",
        "resistance_candidates",
        "missing_levels",
        "failed_gates",
        "execution_blocked_by",
    ):
        if field in value:
            sanitized[field] = _copy_public_scalar_list(value.get(field))

    nested_scalar_fields = {
        "metrics": (
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "boll_mid",
            "boll_upper",
            "boll_lower",
            "recent_5_low",
            "recent_20_low",
            "recent_20_high",
        ),
        "levels": (
            "reference_support",
            "invalidation_basis",
            "resistance_1",
            "resistance_2",
            "resistance_3",
        ),
        "rounding": (
            "tick",
            "stop_buffer_pct",
            "breakout_buffer_pct",
            "stop_mode",
            "breakout_mode",
            "entry_buffer_pct",
            "entry_mode",
            "default_mode",
        ),
        "fee_aware_trade": (
            "risk_amount",
            "reward_amount",
            "net_reward_risk",
        ),
        "trend_context": (
            "state",
            "recovery_required",
            "bearish_short_term_alignment",
            "drawdown_from_20d_high_pct",
            "distance_to_entry_pct",
            "deep_drawdown_threshold_pct",
        ),
    }
    for field, fields in nested_scalar_fields.items():
        if isinstance(value.get(field), Mapping):
            sanitized[field] = _copy_public_scalar_fields(value.get(field), fields)
    if isinstance(value.get("trend_context"), Mapping):
        allowed_averages = {"ma5", "ma10", "ma20", "ma60"}
        sanitized.setdefault("trend_context", {})["below_key_averages"] = (
            [
                item
                for item in _copy_public_scalar_list(
                    value["trend_context"].get("below_key_averages")
                )
                if item in allowed_averages
            ]
        )
    if isinstance(value.get("research_watch_levels"), Mapping):
        sanitized["research_watch_levels"] = _sanitize_public_research_watch_levels(
            value.get("research_watch_levels")
        )
    if isinstance(value.get("history"), Mapping):
        sanitized["history"] = {
            "historical_volume": _copy_public_scalar_list(
                value["history"].get("historical_volume")
            )
        }
    return sanitized


def _sanitize_public_corporate_action(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        (
            "ok",
            "source",
            "code",
            "status",
            "blocks_new_position",
            "price_plan_adjustment_required",
            "sessions_until_ex_date",
            "reason",
            "is_reference_only",
        ),
    )
    if isinstance(value, Mapping) and isinstance(
        value.get("nearest_action"), Mapping
    ):
        sanitized["nearest_action"] = _copy_public_scalar_fields(
            value.get("nearest_action"),
            (
                "announcement_date",
                "action_type",
                "record_date",
                "ex_date",
                "payment_date",
                "cash_dividend_per_share",
                "description",
                "report_period",
            ),
        )
    elif isinstance(value, Mapping) and "nearest_action" in value:
        sanitized["nearest_action"] = None
    return sanitized


def _sanitize_public_risk_flags(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    fields = (
        "key",
        "level",
        "message",
        "quote_status",
        "plan_status",
        "marker",
        "provider_name",
        "record_date",
        "ex_date",
        "sessions_until_ex_date",
        "reason",
        "cash_usage_pct",
        "cash_after_one_lot",
        "pct_chg",
        "turnover_rate",
        "intraday_range_pct",
        "drawdown_from_20d_high_pct",
        "distance_to_entry_pct",
    )
    return [
        _copy_public_scalar_fields(flag, fields)
        for flag in value
        if isinstance(flag, Mapping)
    ]


def _sanitize_public_triggers(value: Any) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        (
            "source",
            "breakout_price",
            "invalidation_price",
            "note",
            "is_reference_only",
        ),
    )
    if not isinstance(value, Mapping):
        return sanitized
    if isinstance(value.get("observation_zone"), Mapping):
        sanitized["observation_zone"] = _copy_public_scalar_fields(
            value.get("observation_zone"),
            ("low", "high"),
        )
    elif "observation_zone" in value:
        sanitized["observation_zone"] = None
    if isinstance(value.get("status"), Mapping):
        sanitized["status"] = _copy_public_scalar_fields(
            value.get("status"),
            (
                "position",
                "breakout_status",
                "distance_to_observation_low_pct",
                "distance_to_observation_high_pct",
                "distance_to_breakout_pct",
                "distance_to_invalidation_pct",
            ),
        )
    return sanitized


def _sanitize_public_deep_check_candidate(value: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized = _copy_public_scalar_fields(
        value,
        (
            "code",
            "name",
            "theme",
            "theme_label",
            "objective_id",
            "objective_label",
            "objective_tier",
            "objective_tier_label",
            "objective_segment",
            "objective_match_score",
            "objective_reason",
            "buy_lot_size",
            "one_lot_amount",
            "is_reference_only",
            "research_tier",
            "rolling_pool_state",
        ),
    )
    if isinstance(value.get("quote"), Mapping):
        sanitized["quote"] = _sanitize_public_candidate_quote(value.get("quote"))
    if isinstance(value.get("guarded_price_plan"), Mapping):
        sanitized["guarded_price_plan"] = _sanitize_public_guarded_price_plan(
            value.get("guarded_price_plan")
        )
    if isinstance(value.get("corporate_action"), Mapping):
        sanitized["corporate_action"] = _sanitize_public_corporate_action(
            value.get("corporate_action")
        )
    if "risk_flags" in value:
        sanitized["risk_flags"] = _sanitize_public_risk_flags(
            value.get("risk_flags")
        )
    if isinstance(value.get("triggers"), Mapping):
        sanitized["triggers"] = _sanitize_public_triggers(value.get("triggers"))
    structured_review = value.get("structured_review")
    if isinstance(structured_review, Mapping):
        earnings = structured_review.get("earnings")
        notice = structured_review.get("notice")
        sanitized["structured_review"] = {
            "technical": _copy_public_scalar_fields(
                structured_review.get("technical"),
                ("status",),
            ),
            "earnings": _copy_public_scalar_fields(
                earnings,
                (
                    "code",
                    "status",
                    "blocks_new_position",
                    "announcement_date",
                    "reason_summary",
                ),
            ),
            "notice": _copy_public_scalar_fields(
                notice,
                (
                    "code",
                    "name",
                    "status",
                    "total_notice_count",
                    "returned_notice_count",
                    "truncated",
                    "manual_review_required",
                ),
            ),
            "hard_risk_status": structured_review.get("hard_risk_status"),
            "hard_risk_clear": structured_review.get("hard_risk_clear"),
            "hard_risk_reasons": _copy_public_scalar_list(
                structured_review.get("hard_risk_reasons")
            ),
        }
    return sanitized


def _public_discovery_evidence(
    definition: Mapping[str, Any],
    *,
    priority: int,
) -> Dict[str, Any]:
    tencent_evidence = {
        "source": definition.get("tencent_source"),
        "bucket": definition.get("tencent_bucket"),
        "score": definition.get("tencent_score"),
        "price": definition.get("tencent_price"),
        "pct_change": definition.get("tencent_pct_change"),
        "amount": definition.get("tencent_amount"),
        "trade_at": definition.get("tencent_trade_at"),
        "amount_percentile": definition.get(
            "tencent_amount_percentile"
        ),
        "market_cap_percentile": definition.get(
            "tencent_market_cap_percentile"
        ),
        "move_quality": definition.get("tencent_move_quality"),
        "turnover_rate": definition.get("turnover_rate"),
        "turnover_quality": definition.get("turnover_quality"),
        "volume_ratio": definition.get("volume_ratio"),
        "volume_ratio_quality": definition.get("volume_ratio_quality"),
        "amplitude": definition.get("amplitude"),
        "amplitude_quality": definition.get("amplitude_quality"),
        "circ_mv": definition.get("circ_mv"),
        "total_mv": definition.get("total_mv"),
        "limit_up": definition.get("limit_up"),
    }
    optional_selection_fields = {
        "one_lot_amount": definition.get("tencent_one_lot_amount"),
        "quality_rank": definition.get("tencent_quality_rank"),
        "selection_lane": definition.get("selection_lane"),
    }
    if all(value is not None for value in optional_selection_fields.values()):
        tencent_evidence.update(optional_selection_fields)

    return {
        "source": "public_full_market",
        "trade_date": definition.get("trade_date"),
        "public_rank": priority,
        "objective": {
            "id": definition.get("objective_id"),
            "label": definition.get("objective_label"),
            "tier": definition.get("objective_tier"),
            "tier_label": definition.get("objective_tier_label"),
            "segment": definition.get("objective_segment"),
            "match_score": definition.get("objective_match_score"),
            "reason": definition.get("objective_reason"),
        },
        "public": {
            "bucket": definition.get("bucket"),
            "score": definition.get("public_score"),
            "price": definition.get("price"),
            "pct_change": definition.get("pct_change"),
            "amount": definition.get("amount"),
            "one_lot_amount": definition.get("one_lot_amount"),
            "amount_percentile": definition.get("amount_percentile"),
            "move_quality": definition.get("move_quality"),
        },
        "tencent": tencent_evidence,
    }


def _normalize_public_discovery_definitions(
    definitions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized_definitions: List[Dict[str, Any]] = []
    for priority, raw_definition in enumerate(definitions, start=1):
        definition = _sanitize_public_discovery_definition(raw_definition)
        definition["priority"] = priority
        definition["discovery"] = _public_discovery_evidence(
            definition,
            priority=priority,
        )
        normalized_definitions.append(definition)
    return normalized_definitions


def _normalize_public_research_candidates(
    candidates: List[Dict[str, Any]],
    definitions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    definitions_by_code = {
        definition["code"]: definition for definition in definitions
    }
    normalized_candidates: List[Dict[str, Any]] = []
    for candidate in candidates:
        normalized = deepcopy(candidate)
        definition = definitions_by_code[normalized["code"]]
        normalized["priority"] = definition["priority"]
        normalized["discovery"] = deepcopy(definition["discovery"])
        triggers = normalized.get("triggers")
        normalized["triggers"] = {
            **(dict(triggers) if isinstance(triggers, Mapping) else {}),
            "source": "public_full_market",
        }

        guarded_price_plan = normalized.get("guarded_price_plan")
        if isinstance(guarded_price_plan, dict):
            fee_aware_trade = guarded_price_plan.get("fee_aware_trade")
            if isinstance(fee_aware_trade, dict):
                for order_key in ("entry_order", "stop_order", "target_order"):
                    fee_aware_trade.pop(order_key, None)

        normalized.update(
            {
                "affordable_with_cash": None,
                "cash_after_one_lot": None,
                "cash_usage_pct": None,
                "same_theme_with_holdings": None,
                "is_reference_only": True,
                "decision": _public_research_candidate_decision(
                    "public_research_only"
                ),
            }
        )
        normalized_candidates.append(normalized)
    return normalized_candidates


def _raise_candidate_discovery_consistency_error(message: str) -> None:
    raise CLIError(
        message,
        code="candidate_discovery_unavailable",
        exit_code=4,
        stage="candidate_discovery",
    )


def _is_finite_discovery_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_public_nonnegative_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_public_coverage_ratio(value: Any) -> bool:
    return _is_finite_discovery_number(value) and 0 <= float(value) <= 1


def _valid_public_count_mapping(
    value: Any,
    *,
    allowed_keys: set[str],
) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value).issubset(allowed_keys)
        and all(_valid_public_nonnegative_count(count) for count in value.values())
    )


def _valid_public_candidate_discovery_metadata(
    value: Any,
    *,
    discovery_status: str,
    tencent_stage_status: Any,
    raw_definitions: Any,
) -> bool:
    if not isinstance(value, Mapping) or not isinstance(raw_definitions, list):
        return False
    if (
        value.get("mode") != "public_full_market"
        or value.get("status") != discovery_status
        or not isinstance(value.get("source"), str)
        or not str(value.get("source")).strip()
        or not _valid_opportunity_benchmark_trade_date(
            value.get("benchmark_trade_date")
        )
    ):
        return False

    count_fields = (
        "provider_expected_count",
        "raw_row_count",
        "unique_row_count",
        "universe_count",
        "eligible_count",
        "public_preselected_count",
        "tencent_requested_count",
        "tencent_minimum_verified_count",
        "tencent_verified_count",
        "tencent_rank_population_count",
        "selected_count",
        "technical_checked_count",
        "technical_screened_count",
        "technical_passed_count",
        "technical_selected_count",
        "technical_closest_rejection_count",
        "earnings_screened_count",
        "earnings_blocked_count",
        "earnings_selected_count",
    )
    if any(
        field not in value or not _valid_public_nonnegative_count(value.get(field))
        for field in count_fields
    ):
        return False
    if value["provider_expected_count"] <= 0 or value["universe_count"] <= 0:
        return False

    exchanges = {"sh", "sz", "bj"}
    provider_expected_exchange_counts = value.get(
        "provider_expected_exchange_counts"
    )
    exchange_counts = value.get("exchange_counts")
    exchange_coverage_ratio = value.get("exchange_coverage_ratio")
    if (
        not isinstance(provider_expected_exchange_counts, Mapping)
        or set(provider_expected_exchange_counts) != exchanges
        or not all(
            _valid_public_nonnegative_count(count) and count > 0
            for count in provider_expected_exchange_counts.values()
        )
        or not isinstance(exchange_counts, Mapping)
        or set(exchange_counts) != exchanges
        or not all(
            _valid_public_nonnegative_count(count)
            for count in exchange_counts.values()
        )
        or not isinstance(exchange_coverage_ratio, Mapping)
        or set(exchange_coverage_ratio) != exchanges
        or not all(
            _valid_public_coverage_ratio(ratio)
            for ratio in exchange_coverage_ratio.values()
        )
        or not _valid_public_coverage_ratio(value.get("total_coverage_ratio"))
    ):
        return False

    if (
        sum(provider_expected_exchange_counts.values())
        != value["provider_expected_count"]
        or sum(exchange_counts.values()) != value["universe_count"]
        or not math.isclose(
            float(value["total_coverage_ratio"]),
            value["universe_count"] / value["provider_expected_count"],
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        or any(
            not math.isclose(
                float(exchange_coverage_ratio[exchange]),
                exchange_counts[exchange]
                / provider_expected_exchange_counts[exchange],
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            for exchange in exchanges
        )
        or float(value["total_coverage_ratio"])
        < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
        or any(
            float(exchange_coverage_ratio[exchange])
            < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
            for exchange in exchanges
        )
        or value["raw_row_count"] < value["unique_row_count"]
        or value["unique_row_count"] != value["universe_count"]
        or value["selected_count"] != len(raw_definitions)
        or value["technical_checked_count"] != 0
        or value["technical_screened_count"] != 0
        or value["technical_passed_count"] != 0
        or value["technical_selected_count"] != 0
        or value["technical_closest_rejection_count"] != 0
        or value["earnings_screened_count"] != 0
        or value["earnings_blocked_count"] != 0
        or value["earnings_selected_count"] != 0
        or value["public_preselected_count"] > value["eligible_count"]
        or value["tencent_requested_count"]
        != value["public_preselected_count"]
        or value["tencent_minimum_verified_count"]
        != max(
            math.ceil(0.8 * value["tencent_requested_count"]),
            min(20, value["tencent_requested_count"]),
        )
        or value["tencent_minimum_verified_count"]
        > value["tencent_requested_count"]
        or value["tencent_verified_count"] > value["tencent_requested_count"]
        or value["tencent_verified_count"]
        < value["tencent_minimum_verified_count"]
        or value["tencent_rank_population_count"]
        > value["tencent_verified_count"]
        or value["selected_count"] > value["tencent_rank_population_count"]
    ):
        return False
    if discovery_status == "ok" and value["selected_count"] <= 0:
        return False
    if discovery_status == "no_eligible_candidates" and value["selected_count"] != 0:
        return False

    if not _valid_public_count_mapping(
        value.get("rejection_counts"),
        allowed_keys=_PUBLIC_DISCOVERY_REJECTION_KEYS,
    ) or not _valid_public_count_mapping(
        value.get("quality_counts"),
        allowed_keys=_PUBLIC_DISCOVERY_QUALITY_KEYS,
    ) or not _valid_public_count_mapping(
        value.get("technical_screen_status_counts"),
        allowed_keys=set(PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS),
    ) or not _valid_public_count_mapping(
        value.get("earnings_screen_status_counts"),
        allowed_keys=set(PUBLIC_EARNINGS_SCREEN_STATUS_KEYS),
    ) or not _valid_public_count_mapping(
        value.get("earnings_actual_status_counts"),
        allowed_keys=set(PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS),
    ):
        return False
    if (
        value.get("technical_screen_status_counts")
        or value.get("technical_closest_rejections")
        or value.get("earnings_screen_status_counts")
        or value.get("earnings_actual_status_counts")
        or value.get("earnings_screen_results")
        or value.get("earnings_report_period") is not None
        or value.get("earnings_actual_report_period") is not None
    ):
        return False

    stage_sources = value.get("stage_sources")
    public_stage = (
        stage_sources.get("public_snapshot")
        if isinstance(stage_sources, Mapping)
        else None
    )
    tencent_stage = (
        stage_sources.get("tencent_verification")
        if isinstance(stage_sources, Mapping)
        else None
    )
    public_provider = (
        public_stage.get("provider")
        if isinstance(public_stage, Mapping)
        else None
    )
    degraded = value.get("degraded") is True
    provider_errors = value.get("provider_errors")
    provider_errors_valid = bool(
        isinstance(provider_errors, list)
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("provider"), str)
            and isinstance(item.get("status"), str)
            and isinstance(item.get("error_type"), str)
            for item in provider_errors
        )
    )
    return bool(
        isinstance(public_stage, Mapping)
        and public_provider
        in {
            "akshare.sina.stock_zh_a_spot",
            "mongo.candidate_market_snapshots",
            "mongo.market_quotes",
        }
        and public_stage.get("status") == "ok"
        and (
            not degraded
            or (
                provider_errors_valid
                and bool(provider_errors)
                and (
                    (
                        public_provider
                        in {
                            "mongo.candidate_market_snapshots",
                            "mongo.market_quotes",
                        }
                        and value.get("freshness") == "cached_fresh"
                    )
                    or (
                        public_provider == "akshare.sina.stock_zh_a_spot"
                        and value.get("freshness") == "fresh"
                        and public_stage.get("freshness") == "fresh"
                        and public_stage.get("degraded") is True
                        and public_stage.get("provider_errors") == provider_errors
                    )
                )
            )
        )
        and isinstance(tencent_stage, Mapping)
        and tencent_stage.get("provider") == "tencent_batch_quotes"
        and isinstance(tencent_stage.get("status"), str)
        and tencent_stage.get("status") == tencent_stage_status
    )


def _parse_public_trade_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _PUBLIC_TRADE_AT_PATTERN.fullmatch(text) is None:
        return None
    return _parse_trade_datetime(text)


def _same_public_quote_number(
    actual: Any,
    expected: Any,
    *,
    optional: bool = False,
) -> bool:
    if optional and expected is None:
        return actual is None
    return bool(
        _is_finite_discovery_number(actual)
        and _is_finite_discovery_number(expected)
        and math.isclose(
            float(actual),
            float(expected),
            rel_tol=1e-12,
            abs_tol=1e-6,
        )
    )


def _valid_public_discovery_evidence_definition(
    definition: Mapping[str, Any],
    quote: Mapping[str, Any],
    *,
    benchmark_trade_date: str,
) -> bool:
    trade_date = definition.get("trade_date")
    definition_trade_at = _parse_public_trade_at(
        definition.get("tencent_trade_at")
    )
    quote_trade_at = _parse_public_trade_at(quote.get("trade_at"))
    if (
        not _valid_opportunity_benchmark_trade_date(trade_date)
        or trade_date != benchmark_trade_date
        or quote.get("trade_date") != trade_date
        or quote.get("source") != "tencent"
        or definition_trade_at is None
        or quote_trade_at is None
        or definition_trade_at != quote_trade_at
        or definition_trade_at.astimezone(
            ZoneInfo(CN_MARKET_TIMEZONE)
        ).date().isoformat()
        != trade_date
    ):
        return False
    if (
        definition.get("bucket") not in {"strength", "pullback"}
        or definition.get("tencent_bucket") not in {"strength", "pullback"}
        or definition.get("tencent_source") != "tencent_batch_quotes"
    ):
        return False

    positive_fields = (
        "price",
        "amount",
        "one_lot_amount",
        "tencent_price",
        "tencent_amount",
        "circ_mv",
        "total_mv",
    )
    if any(
        not _is_finite_discovery_number(definition.get(field))
        or float(definition[field]) <= 0
        for field in positive_fields
    ):
        return False

    selection_fields = (
        "tencent_one_lot_amount",
        "tencent_quality_rank",
        "selection_lane",
    )
    selection_field_presence = [field in definition for field in selection_fields]
    if any(selection_field_presence):
        quality_rank = definition.get("tencent_quality_rank")
        if (
            not all(selection_field_presence)
            or not _is_finite_discovery_number(
                definition.get("tencent_one_lot_amount")
            )
            or float(definition["tencent_one_lot_amount"]) <= 0
            or not isinstance(quality_rank, int)
            or isinstance(quality_rank, bool)
            or quality_rank < 1
            or definition.get("selection_lane")
            not in {"quality_core", "one_lot_diversity", "quality_fill"}
            or not _same_public_quote_number(
                definition.get("tencent_one_lot_amount"),
                float(definition["tencent_price"]) * DEFAULT_BUY_LOT_SIZE,
            )
        ):
            return False

    unit_interval_fields = (
        "objective_match_score",
        "public_score",
        "amount_percentile",
        "move_quality",
        "tencent_score",
        "tencent_amount_percentile",
        "tencent_market_cap_percentile",
        "tencent_move_quality",
        "turnover_quality",
        "volume_ratio_quality",
        "amplitude_quality",
    )
    if any(
        not _is_finite_discovery_number(definition.get(field))
        or not 0 <= float(definition[field]) <= 1
        for field in unit_interval_fields
    ):
        return False

    if (
        definition.get("objective_id") != INVESTMENT_OBJECTIVE["id"]
        or definition.get("objective_label") != INVESTMENT_OBJECTIVE["label"]
        or definition.get("objective_tier")
        not in {"core", "related", "non_core"}
        or not isinstance(definition.get("objective_tier_label"), str)
        or not isinstance(definition.get("objective_segment"), str)
        or not isinstance(definition.get("objective_reason"), str)
    ):
        return False

    finite_fields = ("pct_change", "tencent_pct_change")
    if any(
        not _is_finite_discovery_number(definition.get(field))
        for field in finite_fields
    ):
        return False

    turnover_rate = definition.get("turnover_rate")
    amplitude = definition.get("amplitude")
    if (
        not _is_finite_discovery_number(turnover_rate)
        or not 0 <= float(turnover_rate) <= 10
        or not _is_finite_discovery_number(amplitude)
        or not 0 <= float(amplitude) <= 8
    ):
        return False

    if "volume_ratio" not in definition or "limit_up" not in definition:
        return False
    volume_ratio = definition.get("volume_ratio")
    if volume_ratio is not None and (
        not _is_finite_discovery_number(volume_ratio)
        or float(volume_ratio) < 0
    ):
        return False
    limit_up = definition.get("limit_up")
    if limit_up is not None and (
        not _is_finite_discovery_number(limit_up)
        or float(limit_up) <= 0
    ):
        return False

    quote_price = quote.get("price")
    if quote_price is None:
        quote_price = quote.get("close")
    required_quote_bindings = (
        (definition.get("tencent_price"), quote_price),
        (definition.get("tencent_pct_change"), quote.get("pct_chg")),
        (definition.get("tencent_amount"), quote.get("amount")),
        (definition.get("turnover_rate"), quote.get("turnover_rate")),
        (definition.get("amplitude"), quote.get("amplitude")),
        (definition.get("circ_mv"), quote.get("circ_mv")),
        (definition.get("total_mv"), quote.get("total_mv")),
    )
    if any(
        not _same_public_quote_number(actual, expected)
        for actual, expected in required_quote_bindings
    ):
        return False
    if (
        not _same_public_quote_number(
            definition.get("one_lot_amount"),
            float(definition["price"]) * DEFAULT_BUY_LOT_SIZE,
        )
        or not _same_public_quote_number(
            definition.get("volume_ratio"),
            quote.get("volume_ratio"),
            optional=True,
        )
        or not _same_public_quote_number(
            definition.get("limit_up"),
            quote.get("limit_up"),
            optional=True,
        )
    ):
        return False
    return True


def _validate_completed_public_discovery(
    discovery_status: str,
    tencent_stage_status: Any,
    raw_definitions: Any,
    raw_quote_map: Any,
    *,
    benchmark_trade_date: Any,
    candidate_discovery: Any,
) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    if not _valid_opportunity_benchmark_trade_date(benchmark_trade_date):
        _raise_candidate_discovery_consistency_error(
            "公开全市场基准交易日无效"
        )
    if not _valid_public_candidate_discovery_metadata(
        candidate_discovery,
        discovery_status=discovery_status,
        tencent_stage_status=tencent_stage_status,
        raw_definitions=raw_definitions,
    ):
        _raise_candidate_discovery_consistency_error(
            "公开全市场候选覆盖证据无效"
        )
    if not isinstance(raw_definitions, list) or not isinstance(
        raw_quote_map,
        Mapping,
    ):
        _raise_candidate_discovery_consistency_error(
            "公开全市场候选发现结果不完整"
        )

    if discovery_status == "no_eligible_candidates":
        if raw_definitions:
            _raise_candidate_discovery_consistency_error(
                "公开全市场无候选状态与候选数据不一致"
            )
        if tencent_stage_status == "not_called_no_preselection":
            if raw_quote_map:
                _raise_candidate_discovery_consistency_error(
                    "公开全市场预筛选为空时不应包含腾讯行情"
                )
        elif tencent_stage_status == "ok":
            for code, raw_quote in raw_quote_map.items():
                if (
                    not isinstance(code, str)
                    or A_SHARE_STOCK_CODE_PATTERN.fullmatch(code) is None
                    or not isinstance(raw_quote, Mapping)
                    or raw_quote.get("code") != code
                ):
                    _raise_candidate_discovery_consistency_error(
                        "公开全市场已验证行情标识不一致"
                    )
        else:
            _raise_candidate_discovery_consistency_error(
                "公开全市场无候选状态与腾讯验证阶段不一致"
            )
        return [], {}

    if tencent_stage_status != "ok":
        _raise_candidate_discovery_consistency_error(
            "公开全市场候选缺少成功的腾讯验证阶段"
        )

    if not 1 <= len(raw_definitions) <= MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES:
        _raise_candidate_discovery_consistency_error(
            "公开全市场候选数量不符合约束"
        )

    definitions: List[Dict[str, Any]] = []
    quote_map: Dict[str, Dict[str, Any]] = {}
    seen_codes = set()
    for raw_definition in raw_definitions:
        if not isinstance(raw_definition, Mapping):
            _raise_candidate_discovery_consistency_error(
                "公开全市场候选定义无效"
            )
        code = raw_definition.get("code")
        if (
            not isinstance(code, str)
            or A_SHARE_STOCK_CODE_PATTERN.fullmatch(code) is None
            or code in seen_codes
        ):
            _raise_candidate_discovery_consistency_error(
                "公开全市场候选代码无效或重复"
            )
        seen_codes.add(code)
        if code not in raw_quote_map:
            _raise_candidate_discovery_consistency_error(
                "公开全市场候选缺少已验证行情"
            )
        raw_quote = raw_quote_map[code]
        if not isinstance(raw_quote, Mapping) or raw_quote.get("code") != code:
            _raise_candidate_discovery_consistency_error(
                "公开全市场候选行情标识不一致"
            )
        if not _valid_public_discovery_evidence_definition(
            raw_definition,
            raw_quote,
            benchmark_trade_date=benchmark_trade_date,
        ):
            _raise_candidate_discovery_consistency_error(
                "公开全市场候选排名证据无效"
            )
        definitions.append(deepcopy(dict(raw_definition)))
        quote_map[code] = deepcopy(dict(raw_quote))
    return definitions, quote_map


def _validated_public_technical_screen(
    value: Any,
    definitions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized, error = validate_public_technical_screen_metadata(
        value,
        expected_definitions=definitions,
    )
    if error or normalized is None:
        raise CLIError(
            "公开候选技术初筛证据无效",
            code="technical_deep_check_failed",
            exit_code=4,
            stage="technical_deep_check",
        )
    return normalized


def _validated_public_earnings_screen(
    value: Any,
    technical_screen: Mapping[str, Any],
    *,
    benchmark_trade_date: str,
) -> Dict[str, Any]:
    expected_codes = technical_screen.get("selected_codes")
    try:
        expected_report_period = latest_completed_reporting_period(
            benchmark_trade_date
        )
        expected_actual_report_period = latest_mandatory_actual_reporting_period(
            benchmark_trade_date
        )
    except ValueError:
        expected_report_period = ""
        expected_actual_report_period = ""
    normalized, error = validate_public_earnings_screen_metadata(
        value,
        expected_codes=(expected_codes if isinstance(expected_codes, list) else []),
        expected_report_period=expected_report_period,
        expected_actual_report_period=expected_actual_report_period,
        benchmark_trade_date=benchmark_trade_date,
    )
    if error or normalized is None:
        raise CLIError(
            "公开候选业绩复核证据无效",
            code="technical_deep_check_failed",
            exit_code=4,
            stage="earnings_forecast_review",
        )
    return normalized


def _ordered_public_deep_check_candidates(
    candidates: Any,
    definitions: List[Dict[str, Any]],
    quote_map: Mapping[str, Dict[str, Any]],
    *,
    selected_codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    expected_codes = (
        list(selected_codes)
        if selected_codes is not None
        else [definition["code"] for definition in definitions]
    )
    if not isinstance(candidates, list) or len(candidates) != len(expected_codes):
        raise CLIError(
            "公开候选技术深检结果与候选发现不一致",
            code="technical_deep_check_failed",
            exit_code=4,
            stage="technical_deep_check",
        )

    candidates_by_code: Dict[str, Dict[str, Any]] = {}
    definitions_by_code = {
        definition["code"]: definition for definition in definitions
    }
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            break
        code = candidate.get("code")
        quote = candidate.get("quote")
        guarded_price_plan = candidate.get("guarded_price_plan")
        definition = definitions_by_code.get(code)
        source_quote = quote_map.get(code) if isinstance(code, str) else None
        expected_quote = (
            _quote_snapshot(dict(source_quote), dict(definition))
            if isinstance(source_quote, Mapping)
            and isinstance(definition, Mapping)
            else None
        )
        definition_trade_at = (
            _parse_public_trade_at(definition.get("tencent_trade_at"))
            if isinstance(definition, Mapping)
            else None
        )
        quote_trade_at = (
            _parse_public_trade_at(quote.get("trade_at"))
            if isinstance(quote, Mapping)
            else None
        )
        if (
            not isinstance(code, str)
            or code in candidates_by_code
            or not isinstance(definition, Mapping)
            or not isinstance(expected_quote, Mapping)
            or not isinstance(candidate.get("name"), str)
            or not str(candidate.get("name")).strip()
            or not isinstance(quote, Mapping)
            or quote.get("source") != "tencent"
            or quote.get("code") != code
            or quote.get("trade_date") != definition.get("trade_date")
            or definition_trade_at is None
            or quote_trade_at is None
            or quote_trade_at != definition_trade_at
            or not _same_public_quote_number(
                quote.get("price"),
                expected_quote.get("price"),
            )
            or not _same_public_quote_number(
                quote.get("amount"),
                expected_quote.get("amount"),
            )
            or not _same_public_quote_number(
                quote.get("volume"),
                expected_quote.get("volume"),
            )
            or not _same_public_quote_number(
                quote.get("quote_volume"),
                expected_quote.get("quote_volume"),
                optional=True,
            )
            or not _same_public_quote_number(
                quote.get("pe_ratio"),
                expected_quote.get("pe_ratio"),
                optional=True,
            )
            or not _same_public_quote_number(
                quote.get("pb_ratio"),
                expected_quote.get("pb_ratio"),
                optional=True,
            )
            or not _same_public_quote_number(
                quote.get("circ_mv"),
                expected_quote.get("circ_mv"),
            )
            or not _same_public_quote_number(
                quote.get("total_mv"),
                expected_quote.get("total_mv"),
            )
            or not _is_finite_discovery_number(quote.get("price"))
            or float(quote["price"]) <= 0
            or not _is_finite_discovery_number(quote.get("amount"))
            or float(quote["amount"]) <= 0
            or not _is_finite_discovery_number(quote.get("volume"))
            or float(quote["volume"]) <= 0
            or not isinstance(guarded_price_plan, Mapping)
            or not isinstance(guarded_price_plan.get("status"), str)
            or not str(guarded_price_plan.get("status")).strip()
            or not isinstance(guarded_price_plan.get("actionable"), bool)
        ):
            break
        candidates_by_code[code] = _sanitize_public_deep_check_candidate(
            candidate
        )
    else:
        if set(candidates_by_code) == set(expected_codes):
            return [candidates_by_code[code] for code in expected_codes]

    raise CLIError(
        "公开候选技术深检结果与候选发现不一致",
        code="technical_deep_check_failed",
        exit_code=4,
        stage="technical_deep_check",
    )


def _build_public_timeout_research_candidates(
    definitions: List[Dict[str, Any]],
    quote_map: Mapping[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    timeout_reason = "技术深检超时，未形成可执行价格计划，仅保留腾讯已验证行情供观察。"
    for raw_definition in definitions:
        definition = deepcopy(dict(raw_definition))
        code = definition["code"]
        raw_quote = quote_map[code]
        snapshot = _quote_snapshot(dict(raw_quote), definition)
        for field in ("volume", "quote_volume"):
            if field in raw_quote:
                snapshot[field] = deepcopy(raw_quote[field])
        snapshot = _sanitize_public_candidate_quote(snapshot)
        candidates.append(
            {
                "code": code,
                "name": snapshot.get("name") or definition.get("name"),
                "theme": definition.get("theme"),
                "theme_label": definition.get("theme_label"),
                "priority": definition.get("priority"),
                "discovery": deepcopy(definition.get("discovery")),
                "quote": snapshot,
                "plan_status": "technical_deep_check_timeout",
                "guarded_price_plan": {
                    "status": "technical_deep_check_timeout",
                    "actionable": False,
                    "reference_actionable": False,
                    "reason": timeout_reason,
                    "execution_blocked_by": [
                        "technical_deep_check_timeout",
                        "account_data_unavailable",
                    ],
                    "is_reference_only": True,
                },
                "risk_status": {
                    "status": "observation_only",
                    "new_position_allowed": False,
                    "reason_code": "technical_deep_check_timeout",
                    "reason": timeout_reason,
                },
                "risk_flags": [
                    {
                        "code": "technical_deep_check_timeout",
                        "severity": "warning",
                        "message": timeout_reason,
                    }
                ],
                "triggers": {
                    "source": "public_full_market",
                    "observation_zone": deepcopy(
                        definition.get("observation_zone")
                    ),
                    "breakout_price": definition.get("breakout_price"),
                    "invalidation_price": definition.get("invalidation_price"),
                    "note": definition.get("note"),
                    "is_reference_only": True,
                },
                "affordable_with_cash": None,
                "cash_after_one_lot": None,
                "cash_usage_pct": None,
                "same_theme_with_holdings": None,
                "is_reference_only": True,
                "decision": _public_research_candidate_decision(
                    "technical_deep_check_timeout"
                ),
            }
        )
    return candidates


def build_public_research_opportunities_payload(
    discovery_result: Mapping[str, Any],
    deep_check_result: Optional[Mapping[str, Any]],
    *,
    external_risk_level: Optional[str] = None,
    database_status: Optional[Dict[str, Any]] = None,
    context: Optional[OpportunityMarketContext] = None,
) -> Dict[str, Any]:
    """Build account-independent output from completed public discovery."""
    if (
        not isinstance(context, OpportunityMarketContext)
        or context.public_snapshot_loaded is not True
        or not isinstance(context.public_snapshot, Mapping)
    ):
        raise CLIError(
            "公开市场上下文未准备完成",
            code="candidate_discovery_unavailable",
            exit_code=4,
            stage="market_context",
        )

    discovery_status = (
        discovery_result.get("status")
        if isinstance(discovery_result, Mapping)
        else None
    )
    raw_candidate_discovery = (
        discovery_result.get("candidate_discovery")
        if isinstance(discovery_result, Mapping)
        else None
    )
    if (
        discovery_status not in {"ok", "no_eligible_candidates"}
        or not isinstance(raw_candidate_discovery, Mapping)
        or raw_candidate_discovery.get("status") != discovery_status
    ):
        stage = (
            discovery_result.get("stage")
            if isinstance(discovery_result, Mapping)
            else None
        )
        raise CLIError(
            "公开全市场候选发现不可用",
            code="candidate_discovery_unavailable",
            exit_code=4,
            stage=str(stage or "candidate_discovery"),
        )

    stage_sources = raw_candidate_discovery.get("stage_sources")
    if not isinstance(stage_sources, Mapping):
        raise CLIError(
            "公开全市场候选覆盖阶段信息不完整",
            code="candidate_discovery_unavailable",
            exit_code=4,
            stage="candidate_discovery",
        )
    tencent_stage = stage_sources.get(
        "tencent_verification"
    )
    tencent_stage_status = (
        tencent_stage.get("status") if isinstance(tencent_stage, Mapping) else None
    )

    raw_definitions = discovery_result.get("definitions")
    raw_quote_map = discovery_result.get("quote_map")
    benchmark_trade_date = raw_candidate_discovery.get(
        "benchmark_trade_date"
    )
    if benchmark_trade_date != context.benchmark_trade_date:
        _raise_candidate_discovery_consistency_error(
            "公开全市场基准交易日与市场上下文不一致"
        )
    definitions, quote_map = _validate_completed_public_discovery(
        discovery_status,
        tencent_stage_status,
        raw_definitions,
        raw_quote_map,
        benchmark_trade_date=benchmark_trade_date,
        candidate_discovery=raw_candidate_discovery,
    )
    candidate_discovery = _sanitize_public_candidate_discovery(
        raw_candidate_discovery
    )
    definitions = _normalize_public_discovery_definitions(definitions)

    technical_status: str
    earnings_status: str
    technical_deep_stage_status: str
    research_candidates: List[Dict[str, Any]]
    if discovery_status == "no_eligible_candidates":
        technical_status = "not_called_no_candidates"
        earnings_status = "not_called_no_candidates"
        technical_deep_stage_status = "not_called_no_candidates"
        research_candidates = []
        candidate_discovery["technical_checked_count"] = 0
    else:
        deep_status = (
            deep_check_result.get("status")
            if isinstance(deep_check_result, Mapping)
            else None
        )
        if deep_status in {
            "ok",
            "daily_structured_analysis_minimum_not_met",
        }:
            raw_technical_screen = deep_check_result.get("technical_screen")
            screened_codes = (
                raw_technical_screen.get("screened_codes")
                if isinstance(raw_technical_screen, Mapping)
                else None
            )
            screened_code_set = (
                set(screened_codes)
                if isinstance(screened_codes, list)
                and all(isinstance(code, str) for code in screened_codes)
                else None
            )
            validation_definitions = (
                [
                    item
                    for item in definitions
                    if item.get("code") in screened_code_set
                ]
                if screened_code_set is not None
                else definitions
            )
            technical_screen = (
                _validated_public_technical_screen(
                    raw_technical_screen,
                    validation_definitions,
                )
                if isinstance(raw_technical_screen, Mapping)
                else None
            )
            raw_earnings_screen = deep_check_result.get("earnings_screen")
            earnings_screen = (
                _validated_public_earnings_screen(
                    raw_earnings_screen,
                    technical_screen,
                    benchmark_trade_date=benchmark_trade_date,
                )
                if isinstance(raw_earnings_screen, Mapping)
                and technical_screen is not None
                else None
            )
            if technical_screen is not None and earnings_screen is None:
                raise CLIError(
                    "公开候选业绩预告筛选证据缺失",
                    code="technical_deep_check_failed",
                    exit_code=4,
                    stage="earnings_forecast_review",
                )
            deep_candidates = _ordered_public_deep_check_candidates(
                deep_check_result.get("candidates"),
                definitions,
                quote_map,
                selected_codes=(
                    technical_screen["selected_codes"]
                    if technical_screen is not None
                    else None
                ),
            )
            technical_status = "ok"
            earnings_status = (
                "ok"
                if earnings_screen is not None
                else "not_called_legacy_deep_check"
            )
            technical_deep_stage_status = (
                "not_called_no_earnings_survivors"
                if earnings_screen is not None
                and earnings_screen["selected_count"] == 0
                else (
                    "daily_structured_analysis_minimum_not_met"
                    if deep_status == "daily_structured_analysis_minimum_not_met"
                    else "ok"
                )
            )
            research_candidates = _normalize_public_research_candidates(
                deep_candidates,
                definitions,
            )
            candidate_discovery["technical_checked_count"] = len(
                research_candidates
            )
            if technical_screen is not None:
                candidate_discovery.update(
                    {
                        "technical_screened_count": technical_screen[
                            "screened_count"
                        ],
                        "technical_passed_count": technical_screen[
                            "passed_count"
                        ],
                        "technical_selected_count": technical_screen[
                            "selected_count"
                        ],
                        "deep_research_selected_count": technical_screen[
                            "deep_research_selected_count"
                        ],
                        "technical_screen_status_counts": deepcopy(
                            technical_screen["status_counts"]
                        ),
                        "technical_closest_rejection_count": technical_screen[
                            "closest_rejection_count"
                        ],
                        "technical_closest_rejections": deepcopy(
                            technical_screen["closest_rejections"]
                        ),
                    }
                )
                candidate_discovery["stage_sources"]["technical_screen"] = {
                    "provider": "tencent_daily_bars",
                    "status": "ok",
                }
            if earnings_screen is not None:
                candidate_discovery.update(
                    {
                        "earnings_screened_count": earnings_screen[
                            "screened_count"
                        ],
                        "earnings_blocked_count": earnings_screen[
                            "blocked_count"
                        ],
                        "earnings_selected_count": earnings_screen[
                            "selected_count"
                        ],
                        "earnings_report_period": earnings_screen[
                            "report_period"
                        ],
                        "earnings_actual_report_period": earnings_screen[
                            "actual_report_period"
                        ],
                        "earnings_screen_status_counts": deepcopy(
                            earnings_screen["status_counts"]
                        ),
                        "earnings_actual_status_counts": deepcopy(
                            earnings_screen["actual_status_counts"]
                        ),
                        "earnings_screen_results": deepcopy(
                            earnings_screen["results"]
                        ),
                    }
                )
                candidate_discovery["stage_sources"][
                    "earnings_forecast_review"
                ] = {
                    "provider": EARNINGS_REVIEW_SOURCE,
                    "status": "ok",
                }
            raw_notice_review = deep_check_result.get("notice_review")
            notice_review = (
                raw_notice_review
                if isinstance(raw_notice_review, Mapping)
                else {}
            )
            notice_status = str(notice_review.get("status") or "not_requested")
            notice_results = [
                item
                for item in notice_review.get("results", [])
                if isinstance(item, Mapping)
            ]
            candidate_discovery.update(
                {
                    "notice_reviewed_count": int(
                        notice_review.get("reviewed_count") or 0
                    ),
                    "notice_hard_blocked_count": sum(
                        bool(
                            set(item.get("attention_tags") or []).intersection(
                                PUBLIC_NOTICE_HARD_RISK_TAGS
                            )
                        )
                        for item in notice_results
                    ),
                    "notice_manual_review_count": int(
                        notice_review.get("manual_review_code_count") or 0
                    ),
                }
            )
            candidate_discovery["stage_sources"]["recent_notice_review"] = {
                "provider": NOTICE_REVIEW_SOURCE,
                "status": notice_status,
                "error_type": notice_review.get("error_type"),
            }
            pipeline_metrics = deep_check_result.get("pipeline_metrics")
            candidate_discovery["pipeline_metrics"] = (
                deepcopy(dict(pipeline_metrics))
                if isinstance(pipeline_metrics, Mapping)
                else {}
            )
            raw_daily_analysis = deep_check_result.get("daily_analysis")
            if isinstance(raw_daily_analysis, Mapping):
                candidate_discovery["daily_structured_analysis"] = deepcopy(
                    dict(raw_daily_analysis)
                )
            raw_batch_audit = deep_check_result.get("batch_audit")
            if isinstance(raw_batch_audit, Mapping):
                candidate_discovery["structured_batch_audit"] = deepcopy(
                    dict(raw_batch_audit)
                )
        elif deep_status == "technical_deep_check_timeout":
            timeout_mode = deep_check_result.get("mode")
            if timeout_mode in {"technical_funnel", "structured_batches"}:
                raw_batch_audit = deep_check_result.get("batch_audit")
                if (
                    timeout_mode == "structured_batches"
                    and isinstance(raw_batch_audit, Mapping)
                ):
                    candidate_discovery["structured_batch_audit"] = deepcopy(
                        dict(raw_batch_audit)
                    )
                    candidate_discovery["stage_sources"][
                        "structured_batches"
                    ] = {
                        "provider": "bounded_structured_batch_pipeline",
                        "status": "timeout",
                    }
                raise CLIError(
                    (
                        "公开候选结构化分析批次超时，未返回不完整候选"
                        if timeout_mode == "structured_batches"
                        else "公开候选技术初筛超时，未返回不完整候选"
                    ),
                    code="technical_deep_check_timeout",
                    exit_code=4,
                    stage="technical_deep_check",
                    details={
                        "stage": "technical_deep_check",
                        "candidate_discovery": deepcopy(candidate_discovery),
                    },
                )
            technical_status = "timeout"
            earnings_status = "not_called_technical_timeout"
            technical_deep_stage_status = "timeout"
            research_candidates = _build_public_timeout_research_candidates(
                definitions,
                quote_map,
            )
            candidate_discovery["technical_checked_count"] = 0
        else:
            deep_error_type = (
                deep_check_result.get("error_type")
                if isinstance(deep_check_result, Mapping)
                else None
            )
            error_code = (
                str(deep_error_type or deep_status)
                if isinstance(deep_error_type or deep_status, str)
                and (deep_error_type or deep_status)
                else "technical_deep_check_failed"
            )
            error_stage = (
                "earnings_forecast_review"
                if error_code
                in {
                    "EarningsForecastFetchError",
                    "EarningsActualFetchError",
                    "EarningsForecastScreenError",
                    "InvalidEarningsScreenMetadata",
                }
                else "technical_deep_check"
            )
            candidate_discovery["technical_deep_check_status"] = str(
                deep_status or "technical_deep_check_failed"
            )
            candidate_discovery["technical_deep_check_error_type"] = error_code
            raw_batch_audit = (
                deep_check_result.get("batch_audit")
                if isinstance(deep_check_result, Mapping)
                else None
            )
            if isinstance(raw_batch_audit, Mapping):
                candidate_discovery["structured_batch_audit"] = deepcopy(
                    dict(raw_batch_audit)
                )
            raise CLIError(
                (
                    "公开候选业绩复核不可用"
                    if error_stage == "earnings_forecast_review"
                    else "公开候选技术深检不可用"
                ),
                code=error_code,
                exit_code=4,
                stage=error_stage,
                details={
                    "stage": error_stage,
                    "candidate_discovery": deepcopy(candidate_discovery),
                },
            )

    if "earnings_forecast_review" not in candidate_discovery["stage_sources"]:
        candidate_discovery["stage_sources"]["earnings_forecast_review"] = {
            "provider": EARNINGS_REVIEW_SOURCE,
            "status": earnings_status,
        }
    if "recent_notice_review" not in candidate_discovery["stage_sources"]:
        candidate_discovery["stage_sources"]["recent_notice_review"] = {
            "provider": NOTICE_REVIEW_SOURCE,
            "status": "not_called_no_candidates",
        }
    candidate_discovery["stage_sources"]["technical_deep_check"] = {
        "provider": (
            "cninfo_dividend_calendar"
            if "technical_screen" in candidate_discovery["stage_sources"]
            else "tencent_daily_bars"
        ),
        "status": technical_deep_stage_status,
    }
    if "structured_batch_audit" in candidate_discovery:
        candidate_discovery["stage_sources"]["structured_batches"] = {
            "provider": "bounded_structured_batch_pipeline",
            "status": (
                "ok"
                if deep_status == "ok"
                else "daily_structured_analysis_minimum_not_met"
            ),
            "completed_batch_count": candidate_discovery[
                "structured_batch_audit"
            ].get("completed_batch_count"),
            "failed_batch_count": candidate_discovery[
                "structured_batch_audit"
            ].get("failed_batch_count"),
            "retry_count": candidate_discovery[
                "structured_batch_audit"
            ].get("retry_count"),
        }

    normalized_external_risk_level = _validate_external_risk_level(
        external_risk_level
    )
    external_risk_gate = build_external_risk_gate(
        normalized_external_risk_level,
        actionable_equity=None,
    )
    effective_database_status = deepcopy(
        database_status
        or {
            "status": "unavailable",
            "error_code": "database_error",
        }
    )
    market_status = build_market_status_payload(
        None,
        database_status=deepcopy(effective_database_status),
        context=context,
    )

    discovery_source = str(
        candidate_discovery.get("source") or "public_full_market"
    )
    meta_sources = [discovery_source]
    if technical_status == "ok":
        meta_sources.append("tencent_daily_bars")
    if earnings_status == "ok":
        meta_sources.append(EARNINGS_FORECAST_SOURCE)
        meta_sources.append(EARNINGS_ACTUAL_SOURCE)
    if technical_deep_stage_status == "ok":
        meta_sources.append("cninfo_dividend_calendar")
    meta_source = "+".join(meta_sources)
    available_data = ["public_full_market_snapshot"]
    tencent_stage = candidate_discovery["stage_sources"].get(
        "tencent_verification"
    )
    if (
        isinstance(tencent_stage, Mapping)
        and tencent_stage.get("status") == "ok"
    ):
        available_data.append("tencent_verified_quotes")
    if technical_status == "ok":
        available_data.append("technical_price_plan")
    if candidate_discovery.get("technical_closest_rejection_count", 0) > 0:
        available_data.append("technical_closest_rejections")
    if earnings_status == "ok":
        available_data.append("earnings_forecast_review")
        available_data.append("latest_actual_earnings")
    notice_stage = candidate_discovery["stage_sources"].get(
        "recent_notice_review"
    )
    if isinstance(notice_stage, Mapping) and notice_stage.get("status") == "ok":
        available_data.append("recent_notice_review")

    payload = {
        "ok": True,
        "data": {
            "mode": "research_only",
            "database": effective_database_status,
            "account": {
                "status": "unavailable",
                "actionable": False,
                "reason_code": "public_research_mode",
                "configured_total_assets": None,
                "cash_or_unallocated": None,
                "estimated_equity": None,
            },
            "decision": {
                **_public_research_candidate_decision(
                    "public_full_market_research_only"
                ),
                "reason": "公开全市场模式不读取或推断账户、持仓和现金，禁止生成仓位数量。",
            },
            "external_risk_gate": external_risk_gate,
            "investment_objective": {
                "id": INVESTMENT_OBJECTIVE["id"],
                "label": INVESTMENT_OBJECTIVE["label"],
                "description": INVESTMENT_OBJECTIVE["description"],
            },
            "market_status": deepcopy(
                market_status.get("data", {})
                if isinstance(market_status, Mapping)
                else {}
            ),
            "candidate_discovery": candidate_discovery,
            "candidates": research_candidates,
            "context": {
                "horizon": "未来两个交易日",
                "source": "public_full_market",
                "quote_source": discovery_source,
                "technical_deep_check_status": technical_status,
                "earnings_forecast_review_status": earnings_status,
                "recent_notice_review_status": (
                    notice_stage.get("status")
                    if isinstance(notice_stage, Mapping)
                    else "not_called"
                ),
                "available_data": available_data,
                "unavailable_data": [
                    "account",
                    "holdings",
                    "cash",
                    "recent_trades",
                    "position_sizing",
                ],
            },
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 7,
            "source": meta_source,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }
    return enforce_research_only_safety(payload)


_PUBLIC_ACCOUNT_SINGLE_SYMBOL_CAP_PCT = float(
    PORTFOLIO_POLICY["hard_single_symbol_cap_pct"]
)
_ACCOUNT_HOLDING_CONTEXT_FIELDS = (
    "code",
    "name",
    "market",
    "theme",
    "quantity",
    "cost_price",
    "current_price",
    "market_value",
    "profit_loss",
    "profit_loss_pct",
    "weight_by_estimated_equity_pct",
    "valuation_actionable",
    "is_reference_only",
)
_RECENT_TRADE_CONTEXT_FIELDS = (
    "code",
    "name",
    "market",
    "side",
    "quantity",
    "cost_price",
    "sell_price",
    "realized_pnl",
    "effective_at",
    "sold_at",
    "traded_at",
    "created_at",
)


def _public_account_context_source(
    mongo_payload: Any,
) -> Optional[Dict[str, Any]]:
    if not isinstance(mongo_payload, Mapping):
        return None
    data = mongo_payload.get("data")
    if not isinstance(data, Mapping):
        return None
    account = data.get("account")
    holdings_risk = data.get("holdings_risk")
    actionable_equity = data.get("actionable_equity")
    if (
        not isinstance(account, Mapping)
        or not isinstance(holdings_risk, list)
        or not isinstance(actionable_equity, Mapping)
    ):
        return None

    cash = _round_number(account.get("cash_or_unallocated"))
    equity = _round_number(actionable_equity.get("value"))
    if equity is None:
        equity = _round_number(account.get("estimated_equity"))
    if cash is None or cash < 0 or equity is None or equity <= 0:
        return None

    normalized_holdings = []
    for raw_holding in holdings_risk:
        if not isinstance(raw_holding, Mapping):
            return None
        holding = _copy_public_scalar_fields(
            raw_holding,
            _ACCOUNT_HOLDING_CONTEXT_FIELDS,
        )
        code = holding.get("code")
        if not isinstance(code, str) or not code.strip():
            return None
        risk_flags = raw_holding.get("risk_flags")
        holding["risk_keys"] = [
            str(flag.get("key"))
            for flag in risk_flags
            if isinstance(flag, Mapping)
            and isinstance(flag.get("key"), str)
            and flag.get("key")
        ] if isinstance(risk_flags, list) else []
        normalized_holdings.append(holding)

    trade_context = data.get("trade_context")
    policy_recent_trades: List[Dict[str, Any]] = []
    recent_trade_context = {
        "recent_count": 0,
        "last_trade": None,
        "recent_realized_pnl": 0.0,
        "is_reference_only": True,
    }
    if isinstance(trade_context, Mapping):
        raw_recent_trades = trade_context.get("recent_trades")
        if isinstance(raw_recent_trades, list):
            policy_recent_trades = [
                _copy_public_scalar_fields(
                    raw_trade,
                    _RECENT_TRADE_CONTEXT_FIELDS,
                )
                for raw_trade in raw_recent_trades
                if isinstance(raw_trade, Mapping)
            ]
        recent_count = trade_context.get("recent_count")
        if isinstance(recent_count, int) and not isinstance(recent_count, bool):
            recent_trade_context["recent_count"] = max(0, recent_count)
        recent_realized_pnl = _round_number(
            trade_context.get("recent_realized_pnl")
        )
        if recent_realized_pnl is not None:
            recent_trade_context["recent_realized_pnl"] = recent_realized_pnl
        last_trade = trade_context.get("last_trade")
        if isinstance(last_trade, Mapping):
            recent_trade_context["last_trade"] = _copy_public_scalar_fields(
                last_trade,
                _RECENT_TRADE_CONTEXT_FIELDS,
            )
            if not policy_recent_trades:
                policy_recent_trades = [
                    deepcopy(recent_trade_context["last_trade"])
                ]

    return {
        "account": {
            "status": "available",
            "actionable": False,
            "reason_code": "public_candidates_account_fit_only",
            "configured_total_assets": _round_number(
                account.get("configured_total_assets")
            ),
            "cash_or_unallocated": cash,
            "estimated_equity": equity,
            "buy_lot_size": int(account.get("buy_lot_size") or DEFAULT_BUY_LOT_SIZE),
            "holding_count": len(normalized_holdings),
        },
        "holdings": normalized_holdings,
        "recent_trade_context": recent_trade_context,
        "trade_context_for_policy": {
            "recent_trades": policy_recent_trades,
            "last_trade": deepcopy(recent_trade_context["last_trade"]),
        },
        "external_risk_gate": (
            dict(data.get("external_risk_gate"))
            if isinstance(data.get("external_risk_gate"), Mapping)
            else {}
        ),
        "a_share_market_gate": (
            dict(data.get("a_share_market_gate"))
            if isinstance(data.get("a_share_market_gate"), Mapping)
            else {}
        ),
    }


def _candidate_public_account_fit(
    candidate: Mapping[str, Any],
    *,
    account_context: Mapping[str, Any],
    recent_sale_cooldown_codes: set[str],
) -> Dict[str, Any]:
    account = account_context["account"]
    holdings = account_context["holdings"]
    cash = float(account["cash_or_unallocated"])
    equity = float(account["estimated_equity"])
    one_lot_amount = _round_number(candidate.get("one_lot_amount"))
    code = str(candidate.get("code") or "")

    same_symbol_holdings = [
        holding for holding in holdings if holding.get("code") == code
    ]
    existing_values = [
        _round_number(holding.get("market_value"))
        for holding in same_symbol_holdings
    ]
    existing_symbol_market_value = (
        round(sum(float(value) for value in existing_values), 2)
        if all(
            holding.get("valuation_actionable") is True
            for holding in same_symbol_holdings
        )
        and all(value is not None for value in existing_values)
        else None
    )
    if not same_symbol_holdings:
        existing_symbol_market_value = 0.0

    single_symbol_cap_amount = round(
        equity * _PUBLIC_ACCOUNT_SINGLE_SYMBOL_CAP_PCT / 100,
        2,
    )
    preferred_single_symbol_pct = float(
        PORTFOLIO_POLICY["preferred_single_symbol_pct"]
    )
    preferred_single_symbol_amount = round(
        equity * preferred_single_symbol_pct / 100,
        2,
    )
    post_trade_symbol_market_value = (
        round(existing_symbol_market_value + one_lot_amount, 2)
        if existing_symbol_market_value is not None and one_lot_amount is not None
        else None
    )
    cash_affordable = bool(
        one_lot_amount is not None and one_lot_amount <= cash
    )
    within_single_symbol_cap = bool(
        post_trade_symbol_market_value is not None
        and post_trade_symbol_market_value <= single_symbol_cap_amount
    )
    passes_account_size_checks = bool(
        cash_affordable and within_single_symbol_cap
    )

    guarded_price_plan = candidate.get("guarded_price_plan")
    fee_aware_trade = (
        guarded_price_plan.get("fee_aware_trade")
        if isinstance(guarded_price_plan, Mapping)
        else None
    )
    one_lot_planned_loss = _round_number(
        fee_aware_trade.get("risk_amount")
        if isinstance(fee_aware_trade, Mapping)
        else None
    )
    per_position_loss_budget_pct = float(
        PORTFOLIO_POLICY["per_position_loss_budget_pct"]
    )
    per_position_loss_budget_amount = round(
        equity * per_position_loss_budget_pct / 100,
        2,
    )
    within_per_position_loss_budget = (
        one_lot_planned_loss <= per_position_loss_budget_amount
        if one_lot_planned_loss is not None
        else None
    )

    blocking_reasons: List[str] = []
    if one_lot_amount is None or existing_symbol_market_value is None:
        blocking_reasons.append("account_fit_data_incomplete")
    else:
        if not cash_affordable:
            blocking_reasons.append("insufficient_cash")
        if not within_single_symbol_cap:
            blocking_reasons.append("post_trade_symbol_cap")
        if within_per_position_loss_budget is False:
            blocking_reasons.append("one_lot_loss_budget")

    risk_flags = candidate.get("risk_flags")
    trend_recovery_required = bool(
        isinstance(risk_flags, list)
        and any(
            isinstance(flag, Mapping)
            and flag.get("key") == "trend_recovery_required"
            for flag in risk_flags
        )
    )
    if (
        (
            not isinstance(guarded_price_plan, Mapping)
            or guarded_price_plan.get("status") != "ok"
        )
        and not trend_recovery_required
    ):
        blocking_reasons.append("technical_price_plan")
    corporate_action = candidate.get("corporate_action")
    if isinstance(corporate_action, Mapping) and (
        corporate_action.get("blocks_new_position") is True
        or corporate_action.get("price_plan_adjustment_required") is True
    ):
        blocking_reasons.append("corporate_action")
    external_risk_gate = account_context.get("external_risk_gate")
    if not isinstance(external_risk_gate, Mapping) or not external_risk_gate.get(
        "actionable"
    ):
        blocking_reasons.append("external_risk_gate")
    a_share_market_gate = account_context.get("a_share_market_gate")
    if not isinstance(a_share_market_gate, Mapping) or (
        a_share_market_gate.get("new_position_allowed") is not True
    ):
        blocking_reasons.append("a_share_market_gate")
    if code in recent_sale_cooldown_codes:
        blocking_reasons.append("recent_sale_cooldown")
    if trend_recovery_required:
        blocking_reasons.append("trend_recovery_required")
    blocking_reasons.append("public_research_only")

    return {
        "status": "available",
        "reference_basis": "tencent_spot_one_lot",
        "one_lot_amount": one_lot_amount,
        "cash_available": round(cash, 2),
        "equity_value": round(equity, 2),
        "single_symbol_cap_pct": _PUBLIC_ACCOUNT_SINGLE_SYMBOL_CAP_PCT,
        "single_symbol_cap_amount": single_symbol_cap_amount,
        "preferred_single_symbol_pct": preferred_single_symbol_pct,
        "preferred_single_symbol_amount": preferred_single_symbol_amount,
        "existing_symbol_market_value": existing_symbol_market_value,
        "post_trade_symbol_market_value": post_trade_symbol_market_value,
        "one_lot_cash_usage_pct": (
            round(one_lot_amount / cash * 100, 2)
            if one_lot_amount is not None and cash > 0
            else None
        ),
        "one_lot_equity_pct": (
            round(one_lot_amount / equity * 100, 2)
            if one_lot_amount is not None and equity > 0
            else None
        ),
        "post_trade_symbol_pct": (
            round(post_trade_symbol_market_value / equity * 100, 2)
            if post_trade_symbol_market_value is not None and equity > 0
            else None
        ),
        "cash_affordable": cash_affordable,
        "within_single_symbol_cap": within_single_symbol_cap,
        "within_preferred_single_symbol": bool(
            post_trade_symbol_market_value is not None
            and post_trade_symbol_market_value <= preferred_single_symbol_amount
        ),
        "one_lot_planned_loss": one_lot_planned_loss,
        "one_lot_planned_loss_pct": (
            round(one_lot_planned_loss / equity * 100, 2)
            if one_lot_planned_loss is not None and equity > 0
            else None
        ),
        "per_position_loss_budget_pct": per_position_loss_budget_pct,
        "per_position_loss_budget_amount": per_position_loss_budget_amount,
        "within_per_position_loss_budget": within_per_position_loss_budget,
        "passes_account_size_checks": passes_account_size_checks,
        "passes_account_risk_checks": bool(
            passes_account_size_checks
            and within_per_position_loss_budget is not False
        ),
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "actionable": False,
        "suggested_lots": 0,
        "suggested_quantity": 0,
    }


def build_account_context_public_research_payload(
    public_payload: Mapping[str, Any],
    mongo_payload: Mapping[str, Any],
    *,
    as_of: Any = None,
    benchmark_session_dates: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """Add trusted account-fit evidence to public candidates without sizing."""
    payload = deepcopy(dict(public_payload))
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("mode") != "research_only":
        return payload
    account_context = _public_account_context_source(mongo_payload)
    if account_context is None:
        return payload

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return payload
    candidate_copies = []
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, Mapping):
            return deepcopy(dict(public_payload))
        candidate_copies.append(deepcopy(dict(raw_candidate)))

    recent_sale_policy = _build_recent_sale_policy(
        account_context["trade_context_for_policy"],
        candidate_copies,
        as_of=as_of,
        benchmark_session_dates=benchmark_session_dates,
    )
    recent_sale_cooldown_codes = {
        str(code or "").upper()
        for code in recent_sale_policy.get("matched_candidate_codes", [])
        if recent_sale_policy.get("status") == "cooldown"
    }

    normalized_candidates = []
    for candidate in candidate_copies:
        candidate["account_fit"] = _candidate_public_account_fit(
            candidate,
            account_context=account_context,
            recent_sale_cooldown_codes=recent_sale_cooldown_codes,
        )
        guarded_price_plan = candidate.get("guarded_price_plan")
        if isinstance(guarded_price_plan, dict):
            execution_blocked_by = guarded_price_plan.get(
                "execution_blocked_by"
            )
            if isinstance(execution_blocked_by, list):
                guarded_price_plan["execution_blocked_by"] = [
                    blocker
                    for blocker in execution_blocked_by
                    if blocker != "account_data_unavailable"
                ]
        normalized_candidates.append(candidate)

    data["mode"] = "account_context_research_only"
    data["account"] = account_context["account"]
    data["holdings_context"] = {
        "status": "available",
        "holding_count": len(account_context["holdings"]),
        "items": account_context["holdings"],
        "is_reference_only": True,
    }
    data["recent_trade_context"] = account_context["recent_trade_context"]
    data["recent_sale_policy"] = recent_sale_policy
    data["candidates"] = normalized_candidates
    decision = (
        dict(data.get("decision"))
        if isinstance(data.get("decision"), Mapping)
        else {}
    )
    decision.update(
        {
            "action": "observe",
            "actionable": False,
            "reason_code": "public_candidates_account_fit_only",
            "suggested_lots": 0,
            "suggested_quantity": 0,
            "reason": "公开候选已结合真实账户做一手资金适配，但仍禁止生成仓位数量。",
        }
    )
    data["decision"] = decision

    context = (
        dict(data.get("context"))
        if isinstance(data.get("context"), Mapping)
        else {}
    )
    available_data = list(context.get("available_data") or [])
    for item in (
        "account_context",
        "holdings_context",
        "cash_fit",
        "recent_trades",
    ):
        if item not in available_data:
            available_data.append(item)
    unavailable_data = [
        item
        for item in list(context.get("unavailable_data") or [])
        if item not in {"account", "holdings", "cash", "recent_trades"}
    ]
    if "position_sizing" not in unavailable_data:
        unavailable_data.append("position_sizing")
    context["available_data"] = available_data
    context["unavailable_data"] = unavailable_data
    data["context"] = context

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        payload["meta"] = meta
    meta["schema_version"] = 8
    source = str(meta.get("source") or "public_full_market")
    if "mongo_account_context" not in source.split("+"):
        meta["source"] = f"{source}+mongo_account_context"
    return enforce_research_only_safety(payload)


def build_market_status_payload(
    db: Any = None,
    *,
    database_status: Optional[Dict[str, Any]] = None,
    context: Optional[OpportunityMarketContext] = None,
    retry_public_timeout: bool = False,
) -> Dict[str, Any]:
    """Build a login-free A-share market gate, with optional Mongo breadth."""
    effective_context = context or build_opportunity_market_context()
    market_gate = _build_a_share_market_gate(
        None,
        db=db,
        context=effective_context,
    )
    market_session = _market_session_context(effective_context.now)
    cached_public_snapshot = effective_context.public_snapshot or {}
    if (
        retry_public_timeout
        and cached_public_snapshot.get("status") == "public_breadth_timeout"
    ):
        effective_context.retry_public_snapshot_once_if_timeout()
        market_gate = _build_a_share_market_gate(
            None,
            db=db,
            context=effective_context,
        )
    breadth_regime = (
        market_gate.get("breadth_regime")
        if isinstance(market_gate.get("breadth_regime"), dict)
        else {}
    )
    effective_database_status = dict(
        database_status
        or ({"status": "connected"} if db is not None else {"status": "not_configured"})
    )
    mongo_breadth = (
        market_gate.get("mongo_breadth")
        if isinstance(market_gate.get("mongo_breadth"), dict)
        else {}
    )
    breadth_load_error = breadth_regime.get("load_error") or mongo_breadth.get("load_error")
    if breadth_load_error:
        effective_database_status = {
            "status": "unavailable",
            "error_code": "database_error",
            "error_type": breadth_load_error,
        }

    breadth_source = str(breadth_regime.get("source") or "")
    if breadth_regime.get("status") == "ok" and breadth_source == "akshare.sina.stock_zh_a_spot":
        data_completeness = "indices_and_public_breadth"
    elif breadth_regime.get("status") == "ok":
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

    if data_completeness == "indices_and_public_breadth":
        payload_source = "tencent_major_indices+akshare_sina_public_breadth"
    else:
        payload_source = "tencent_major_indices+optional_mongo_market_breadth"

    return {
        "ok": True,
        "data": {
            "market": "CN",
            "market_session": market_session,
            "market_gate": market_gate,
            "decision": decision,
            "data_completeness": data_completeness,
            "database": effective_database_status,
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": payload_source,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def _normalize_public_a_share_codes(
    codes: Any,
    *,
    max_codes: int,
    required_error_code: str,
    invalid_error_code: str,
    too_many_error_code: str,
    stage: str,
) -> List[str]:
    if not isinstance(codes, list):
        raise CLIError(
            "至少提供一个 A 股代码",
            code=required_error_code,
            exit_code=2,
            stage=stage,
        )
    normalized_codes: List[str] = []
    for raw_code in codes:
        raw_text = str(raw_code or "").strip()
        code = normalize_cn_code(raw_text)
        if (
            _PUBLIC_A_SHARE_CODE_INPUT_PATTERN.fullmatch(raw_text) is None
            or not code
            or A_SHARE_STOCK_CODE_PATTERN.fullmatch(code) is None
        ):
            raise CLIError(
                f"无效的 A 股代码: {raw_code}",
                code=invalid_error_code,
                exit_code=2,
                stage=stage,
            )
        if code not in normalized_codes:
            normalized_codes.append(code)
    if not normalized_codes:
        raise CLIError(
            "至少提供一个 A 股代码",
            code=required_error_code,
            exit_code=2,
            stage=stage,
        )
    if len(normalized_codes) > max_codes:
        raise CLIError(
            f"单次最多查询 {max_codes} 只股票",
            code=too_many_error_code,
            exit_code=2,
            stage=stage,
        )
    return normalized_codes


def _normalize_public_earnings_codes(codes: Any) -> List[str]:
    return _normalize_public_a_share_codes(
        codes,
        max_codes=MAX_EARNINGS_SCREEN_CANDIDATES,
        required_error_code="earnings_codes_required",
        invalid_error_code="invalid_earnings_code",
        too_many_error_code="too_many_earnings_codes",
        stage="earnings_forecast_review",
    )


def _normalize_public_notice_codes(codes: Any) -> List[str]:
    return _normalize_public_a_share_codes(
        codes,
        max_codes=MAX_NOTICE_REVIEW_CANDIDATES,
        required_error_code="notice_codes_required",
        invalid_error_code="invalid_notice_code",
        too_many_error_code="too_many_notice_codes",
        stage="recent_notice_review",
    )


def _normalize_notice_lookback_days(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_NOTICE_LOOKBACK_CALENDAR_DAYS
    ):
        raise CLIError(
            f"公告回看天数必须在 1 到 {MAX_NOTICE_LOOKBACK_CALENDAR_DAYS} 之间",
            code="invalid_notice_lookback_days",
            exit_code=2,
            stage="recent_notice_review",
        )
    return value


def build_public_candidate_earnings_payload(
    codes: Any,
    *,
    context: OpportunityMarketContext,
    screener: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a login-free, research-only earnings review for A-share codes."""
    normalized_codes = _normalize_public_earnings_codes(codes)
    benchmark_trade_date = context.benchmark_trade_date
    if not _valid_opportunity_benchmark_trade_date(benchmark_trade_date):
        raise CLIError(
            "腾讯市场上下文缺少有效基准交易日",
            code="earnings_market_context_unavailable",
            exit_code=4,
            stage="tencent_market_context",
        )
    effective_screener = screener or screen_public_candidate_earnings_risk
    try:
        raw_screen = effective_screener(
            normalized_codes,
            benchmark_trade_date=benchmark_trade_date,
        )
    except Exception as exc:
        raise CLIError(
            "公开业绩复核失败",
            code="EarningsForecastScreenError",
            exit_code=4,
            stage="earnings_forecast_review",
            details={"error_type": type(exc).__name__},
        ) from exc

    raw_status = (
        raw_screen.get("status")
        if isinstance(raw_screen, Mapping)
        else None
    )
    if raw_status != "ok":
        error_code = (
            "EarningsForecastFetchError"
            if raw_status == "earnings_forecast_unavailable"
            else "EarningsActualFetchError"
            if raw_status == "earnings_actual_unavailable"
            else "EarningsForecastScreenError"
        )
        raise CLIError(
            "公开业绩数据源或结果不可用",
            code=error_code,
            exit_code=4,
            stage="earnings_forecast_review",
            details={
                "provider_status": raw_status,
                "source": (
                    raw_screen.get("source")
                    if isinstance(raw_screen, Mapping)
                    else None
                ),
                "actual_source": (
                    raw_screen.get("actual_source")
                    if isinstance(raw_screen, Mapping)
                    else None
                ),
                "report_period": (
                    raw_screen.get("report_period")
                    if isinstance(raw_screen, Mapping)
                    else None
                ),
                "actual_report_period": (
                    raw_screen.get("actual_report_period")
                    if isinstance(raw_screen, Mapping)
                    else None
                ),
                "error_type": (
                    raw_screen.get("error_type")
                    if isinstance(raw_screen, Mapping)
                    else "InvalidProviderPayload"
                ),
            },
        )

    expected_report_period = latest_completed_reporting_period(
        benchmark_trade_date
    )
    expected_actual_report_period = latest_mandatory_actual_reporting_period(
        benchmark_trade_date
    )
    earnings_screen, validation_error = (
        validate_public_earnings_screen_metadata(
            raw_screen,
            expected_codes=normalized_codes,
            expected_report_period=expected_report_period,
            expected_actual_report_period=expected_actual_report_period,
            benchmark_trade_date=benchmark_trade_date,
        )
    )
    if validation_error or earnings_screen is None:
        raise CLIError(
            "公开业绩复核证据无效",
            code="InvalidEarningsScreenMetadata",
            exit_code=4,
            stage="earnings_forecast_review",
        )

    payload = {
        "ok": True,
        "data": {
            "mode": "public_research_only",
            "benchmark_trade_date": benchmark_trade_date,
            "earnings_review": earnings_screen,
            "decision": {
                "action": "observe",
                "actionable": False,
                "reason_code": "earnings_evidence_only",
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "reason": "业绩证据不能单独构成股票候选或交易条件。",
            },
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": EARNINGS_REVIEW_SOURCE,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }
    return enforce_research_only_safety(payload)


def build_public_candidate_notice_payload(
    codes: Any,
    *,
    context: OpportunityMarketContext,
    lookback_calendar_days: int = NOTICE_LOOKBACK_CALENDAR_DAYS,
    reviewer: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a login-free, research-only recent-announcement review."""
    normalized_codes = _normalize_public_notice_codes(codes)
    normalized_lookback_days = _normalize_notice_lookback_days(
        lookback_calendar_days
    )
    benchmark_trade_date = context.benchmark_trade_date
    if not _valid_opportunity_benchmark_trade_date(benchmark_trade_date):
        raise CLIError(
            "腾讯市场上下文缺少有效基准交易日",
            code="notice_market_context_unavailable",
            exit_code=4,
            stage="tencent_market_context",
        )
    if not isinstance(context.now, datetime):
        raise CLIError(
            "腾讯市场上下文缺少有效本地时间",
            code="notice_market_context_unavailable",
            exit_code=4,
            stage="tencent_market_context",
        )
    market_timezone = ZoneInfo(CN_MARKET_TIMEZONE)
    local_now = (
        context.now.replace(tzinfo=market_timezone)
        if context.now.tzinfo is None
        else context.now.astimezone(market_timezone)
    )
    end_date = local_now.date()
    start_date = end_date - timedelta(
        days=normalized_lookback_days - 1
    )
    expected_source = (
        NOTICE_REVIEW_SOURCE
        if normalized_lookback_days == NOTICE_LOOKBACK_CALENDAR_DAYS
        else NOTICE_HISTORY_SOURCE
    )
    try:
        if reviewer is not None:
            reviewer_kwargs: Dict[str, Any] = {"as_of_date": end_date}
            if normalized_lookback_days != NOTICE_LOOKBACK_CALENDAR_DAYS:
                reviewer_kwargs["lookback_calendar_days"] = (
                    normalized_lookback_days
                )
            raw_review = reviewer(normalized_codes, **reviewer_kwargs)
        elif normalized_lookback_days == NOTICE_LOOKBACK_CALENDAR_DAYS:
            raw_review = review_public_candidate_notices(
                normalized_codes,
                as_of_date=end_date,
            )
        else:
            raw_review = review_public_candidate_notice_history(
                normalized_codes,
                as_of_date=end_date,
                lookback_calendar_days=normalized_lookback_days,
            )
    except Exception as exc:
        raise CLIError(
            "近期公告核查失败",
            code="NoticeReviewError",
            exit_code=4,
            stage="recent_notice_review",
            details={"error_type": type(exc).__name__},
        ) from exc

    raw_status = (
        raw_review.get("status")
        if isinstance(raw_review, Mapping)
        else None
    )
    if raw_status != "ok":
        error_code = (
            "NoticeReviewFetchError"
            if raw_status == "notice_source_unavailable"
            else "NoticeReviewError"
        )
        raise CLIError(
            "近期公告数据源或结果不可用",
            code=error_code,
            exit_code=4,
            stage="recent_notice_review",
            details={
                "provider_status": raw_status,
                "source": (
                    raw_review.get("source")
                    if isinstance(raw_review, Mapping)
                    else None
                ),
                "start_date": (
                    raw_review.get("start_date")
                    if isinstance(raw_review, Mapping)
                    else None
                ),
                "end_date": (
                    raw_review.get("end_date")
                    if isinstance(raw_review, Mapping)
                    else None
                ),
                **(
                    {"failed_date": raw_review.get("failed_date")}
                    if isinstance(raw_review, Mapping)
                    and raw_review.get("failed_date") is not None
                    else {}
                ),
                **(
                    {"failed_code": raw_review.get("failed_code")}
                    if isinstance(raw_review, Mapping)
                    and raw_review.get("failed_code") is not None
                    else {}
                ),
                "error_type": (
                    raw_review.get("error_type")
                    if isinstance(raw_review, Mapping)
                    else "InvalidProviderPayload"
                ),
            },
        )

    notice_review, validation_error = (
        validate_public_candidate_notice_review(
            raw_review,
            expected_codes=normalized_codes,
            expected_start_date=start_date,
            expected_end_date=end_date,
            expected_lookback_calendar_days=normalized_lookback_days,
            expected_source=expected_source,
        )
    )
    if validation_error or notice_review is None:
        raise CLIError(
            "近期公告核查证据无效",
            code="InvalidNoticeReviewMetadata",
            exit_code=4,
            stage="recent_notice_review",
        )

    payload = {
        "ok": True,
        "data": {
            "mode": "public_research_only",
            "benchmark_trade_date": benchmark_trade_date,
            "market_session": _market_session_context(context.now),
            "notice_review": notice_review,
            "decision": {
                "action": "observe",
                "actionable": False,
                "reason_code": "recent_notice_evidence_only",
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "reason": "公告标题和标签仅用于人工核查，不能自动构成候选、阻断或交易条件。",
            },
            "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
        },
        "meta": {
            "schema_version": 1,
            "source": expected_source,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }
    return enforce_research_only_safety(payload)


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


def _require_opportunity_time(
    context: OpportunityMarketContext,
    *,
    stage: str,
) -> None:
    if context.remaining_seconds() <= 0:
        raise CLIError(
            "opportunities command deadline exceeded",
            code="stage_timeout",
            exit_code=4,
            stage=stage,
        )


def _supports_opportunity_interval_timer() -> bool:
    return (
        threading.current_thread() is threading.main_thread()
        and all(
            hasattr(signal, name)
            for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
        )
    )


@contextmanager
def _opportunity_wall_clock_guard(
    context: OpportunityMarketContext,
    *,
    stage: str,
):
    if not isinstance(context, OpportunityMarketContext):
        yield
        return

    _require_opportunity_time(context, stage=stage)
    if not _supports_opportunity_interval_timer():
        yield
        _require_opportunity_time(context, stage=stage)
        return

    signal_number = signal.SIGALRM
    timer_kind = signal.ITIMER_REAL
    try:
        previous_handler = signal.getsignal(signal_number)
        previous_delay, _previous_interval = signal.getitimer(timer_kind)
    except (OSError, RuntimeError, ValueError):
        yield
        _require_opportunity_time(context, stage=stage)
        return
    if previous_delay > 0:
        yield
        _require_opportunity_time(context, stage=stage)
        return

    remaining_seconds = context.remaining_seconds()
    if remaining_seconds <= 0:
        _require_opportunity_time(context, stage=stage)

    deadline_state = {
        "triggered": False,
        "interrupt_raised": False,
        "cleanup_started": False,
    }

    def raise_stage_timeout(_signum: int, _frame: Any) -> None:
        deadline_state["triggered"] = True
        if deadline_state["interrupt_raised"]:
            return
        deadline_state["interrupt_raised"] = True
        raise _OpportunityDeadlineInterrupt

    handler_restore_required = False
    timer_started = False

    def cleanup() -> Optional[BaseException]:
        nonlocal handler_restore_required, timer_started
        deadline_state["cleanup_started"] = True
        cleanup_error: Optional[BaseException] = None
        if timer_started:
            try:
                signal.setitimer(timer_kind, 0.0)
            except _OpportunityDeadlineInterrupt:
                raise
            except BaseException as exc:
                cleanup_error = exc
            else:
                timer_started = False
        if handler_restore_required:
            try:
                signal.signal(signal_number, previous_handler)
            except _OpportunityDeadlineInterrupt:
                raise
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            else:
                handler_restore_required = False
        return cleanup_error

    setup_error: Optional[BaseException] = None
    pending_error: Optional[BaseException] = None
    cleanup_error: Optional[BaseException] = None
    deadline_interrupt: Optional[_OpportunityDeadlineInterrupt] = None
    try:
        try:
            handler_restore_required = True
            signal.signal(signal_number, raise_stage_timeout)
            signal.setitimer(timer_kind, remaining_seconds)
            timer_started = True
        except _OpportunityDeadlineInterrupt:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            setup_error = exc
        except BaseException as exc:
            pending_error = exc
        else:
            try:
                yield
            except _OpportunityDeadlineInterrupt:
                raise
            except BaseException as exc:
                pending_error = exc
        finally:
            cleanup_error = cleanup()
    except _OpportunityDeadlineInterrupt as exc:
        deadline_interrupt = exc
        cleanup_error = cleanup()

    if deadline_state["triggered"] or deadline_interrupt is not None:
        timeout_error = CLIError(
            "opportunities command deadline exceeded",
            code="stage_timeout",
            exit_code=4,
            stage=stage,
        )
        if deadline_interrupt is not None:
            raise timeout_error from deadline_interrupt
        raise timeout_error
    if setup_error is not None:
        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        _require_opportunity_time(context, stage=stage)
        yield
        _require_opportunity_time(context, stage=stage)
        return
    if pending_error is not None:
        raise pending_error.with_traceback(pending_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    _require_opportunity_time(context, stage=stage)


def _run_opportunity_sync_builder(
    context: OpportunityMarketContext,
    builder: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    with _opportunity_wall_clock_guard(context, stage="orchestration"):
        return builder()


def _opportunity_candidate_discovery_status(payload: Any) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    candidate_discovery = data.get("candidate_discovery")
    if not isinstance(candidate_discovery, Mapping):
        return None
    status = candidate_discovery.get("status")
    return status if isinstance(status, str) else None


def _should_fallback_to_public_research(payload: Any) -> bool:
    return (
        _opportunity_candidate_discovery_status(payload)
        in _PUBLIC_RESEARCH_FALLBACK_DISCOVERY_STATUSES
    )


def _raise_public_discovery_unavailable(discovery_result: Any) -> None:
    stage = (
        discovery_result.get("stage")
        if isinstance(discovery_result, Mapping)
        else None
    )
    raw_candidate_discovery = (
        discovery_result.get("candidate_discovery")
        if isinstance(discovery_result, Mapping)
        else None
    )
    candidate_discovery = (
        deepcopy(dict(raw_candidate_discovery))
        if isinstance(raw_candidate_discovery, Mapping)
        else {"status": "candidate_discovery_unavailable"}
    )
    raise CLIError(
        "公开全市场候选发现不可用",
        code="candidate_discovery_unavailable",
        exit_code=4,
        details={
            "stage": str(stage or "candidate_discovery"),
            "candidate_discovery": candidate_discovery,
        },
    )


def _valid_opportunity_benchmark_trade_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _unavailable_tencent_market_context_discovery(
    context: OpportunityMarketContext,
) -> Dict[str, Any]:
    raw_index_status = context.index_status
    index_status = (
        raw_index_status
        if isinstance(raw_index_status, str) and raw_index_status
        else "index_context_unavailable"
    )
    if index_status == "ok":
        stage_status = "benchmark_trade_date_unavailable"
        context_error: Any = {
            "status": stage_status,
            "stage": "tencent_market_context",
            "index_status": index_status,
            "benchmark_trade_date": context.benchmark_trade_date,
        }
    else:
        stage_status = index_status
        context_error = deepcopy(context.index_error)

    empty_counts = {exchange: 0 for exchange in ("sh", "sz", "bj")}
    candidate_discovery = {
        "mode": "public_full_market",
        "status": "candidate_discovery_unavailable",
        "source": "tencent_batch_quotes",
        "benchmark_trade_date": None,
        "provider_expected_count": 0,
        "provider_expected_exchange_counts": dict(empty_counts),
        "raw_row_count": 0,
        "unique_row_count": 0,
        "universe_count": 0,
        "exchange_counts": dict(empty_counts),
        "total_coverage_ratio": 0.0,
        "exchange_coverage_ratio": {
            exchange: 0.0 for exchange in empty_counts
        },
        "eligible_count": 0,
        "public_preselected_count": 0,
        "tencent_requested_count": 0,
        "tencent_minimum_verified_count": 0,
        "tencent_verified_count": 0,
        "tencent_rank_population_count": 0,
        "selected_count": 0,
        "technical_checked_count": 0,
        "rejection_counts": {},
        "quality_counts": {},
        "stage_sources": {
            "tencent_market_context": {
                "provider": "tencent_batch_quotes",
                "status": stage_status,
                "error": context_error,
            },
            "public_snapshot": {
                "provider": "akshare.sina.stock_zh_a_spot",
                "status": "not_called_tencent_market_context_unavailable",
            },
            "tencent_verification": {
                "provider": "tencent_batch_quotes",
                "status": "not_called_tencent_market_context_unavailable",
            },
        },
    }
    return {
        "status": "candidate_discovery_unavailable",
        "stage": "tencent_market_context",
        "definitions": [],
        "quote_map": {},
        "candidate_discovery": candidate_discovery,
    }


def _orchestrate_public_full_market_research_payload(
    *,
    context: OpportunityMarketContext,
    external_risk_level: Optional[str] = None,
    database_status: Optional[Dict[str, Any]] = None,
    discovery_state: Optional[Dict[str, Any]] = None,
    excluded_code_reasons: Optional[Mapping[str, str]] = None,
    board_exclusion_reasons: Optional[Mapping[str, str]] = None,
    star_market_exclusion_reason: Optional[str] = None,
    research_progress_callback: Optional[
        Callable[[Dict[str, Any]], None]
    ] = None,
    resume_checkpoint: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one deadline-bounded public discovery workflow for opportunities."""
    if context.index_status != "ok" or not _valid_opportunity_benchmark_trade_date(
        context.benchmark_trade_date
    ):
        discovery_result = _unavailable_tencent_market_context_discovery(context)
        if discovery_state is not None:
            discovery_state["result"] = discovery_result
        _raise_public_discovery_unavailable(discovery_result)

    _require_opportunity_time(context, stage="sina_public_snapshot")
    public_snapshot = context.ensure_public_snapshot()
    _require_opportunity_time(context, stage="sina_public_snapshot")

    tencent_batch_called = False
    tencent_deadline_expired = False

    def fetch_candidate_quotes(codes: Iterable[str]) -> Dict[str, Any]:
        nonlocal tencent_batch_called, tencent_deadline_expired
        requested_codes = list(codes)
        if tencent_batch_called:
            raise RuntimeError("public candidate Tencent batch already called")
        tencent_batch_called = True
        timeout_seconds = context.stage_timeout("tencent_candidate_review")
        if timeout_seconds <= 0:
            tencent_deadline_expired = True
            return {
                "status": "stage_timeout",
                "requested_codes": requested_codes,
                "rows": [],
                "error_type": "CommandDeadlineExceeded",
            }
        result = fetch_tencent_quotes_batched_sync(
            requested_codes,
            timeout=timeout_seconds,
        )
        if context.remaining_seconds() <= 0:
            tencent_deadline_expired = True
        return result

    _require_opportunity_time(context, stage="candidate_discovery")
    discovery_kwargs: Dict[str, Any] = {
        "fetch_quotes": fetch_candidate_quotes,
        "now": context.now,
    }
    if excluded_code_reasons:
        discovery_kwargs["excluded_code_reasons"] = excluded_code_reasons
    if board_exclusion_reasons:
        discovery_kwargs["board_exclusion_reasons"] = board_exclusion_reasons
    if star_market_exclusion_reason:
        discovery_kwargs[
            "star_market_exclusion_reason"
        ] = star_market_exclusion_reason
    discovery_result = discover_public_candidate_universe(
        public_snapshot,
        **discovery_kwargs,
    )
    if discovery_state is not None:
        discovery_state["result"] = discovery_result
    if tencent_deadline_expired:
        _require_opportunity_time(context, stage="tencent_candidate_review")
    _require_opportunity_time(context, stage="candidate_discovery")

    discovery_status = (
        discovery_result.get("status")
        if isinstance(discovery_result, Mapping)
        else None
    )
    if discovery_status not in {"ok", "no_eligible_candidates"}:
        _raise_public_discovery_unavailable(discovery_result)

    deep_check_result: Optional[Dict[str, Any]] = None
    if discovery_status == "ok":
        candidate_discovery = discovery_result.get("candidate_discovery")
        stage_sources = (
            candidate_discovery.get("stage_sources")
            if isinstance(candidate_discovery, Mapping)
            else None
        )
        tencent_stage = (
            stage_sources.get("tencent_verification")
            if isinstance(stage_sources, Mapping)
            else None
        )
        tencent_stage_status = (
            tencent_stage.get("status")
            if isinstance(tencent_stage, Mapping)
            else None
        )
        benchmark_trade_date = (
            candidate_discovery.get("benchmark_trade_date")
            if isinstance(candidate_discovery, Mapping)
            else None
        )
        if benchmark_trade_date != context.benchmark_trade_date:
            _raise_candidate_discovery_consistency_error(
                "公开全市场基准交易日与市场上下文不一致"
            )
        definitions, selected_quote_map = _validate_completed_public_discovery(
            discovery_status,
            tencent_stage_status,
            discovery_result.get("definitions"),
            discovery_result.get("quote_map"),
            benchmark_trade_date=benchmark_trade_date,
            candidate_discovery=candidate_discovery,
        )
        definitions = _normalize_public_discovery_definitions(definitions)
        _require_opportunity_time(context, stage="candidate_discovery")
        _require_opportunity_time(context, stage="technical_deep_inspection")
        remaining_seconds = context.stage_timeout("technical_deep_inspection")
        deep_check_result = (
            run_public_candidate_structured_batches(
                definitions,
                selected_quote_map,
                benchmark_trade_date=benchmark_trade_date,
                command_remaining_seconds=remaining_seconds,
                progress_callback=research_progress_callback,
                resume_checkpoint=resume_checkpoint,
            )
            if len(definitions) > STRUCTURED_BATCH_SIZE
            else run_public_candidate_technical_funnel(
                definitions,
                selected_quote_map,
                benchmark_trade_date=benchmark_trade_date,
                command_remaining_seconds=remaining_seconds,
            )
        )
        if isinstance(deep_check_result, dict):
            candidate_discovery["technical_deep_check_status"] = str(
                deep_check_result.get("status") or "invalid_result"
            )
            if deep_check_result.get("error_type") is not None:
                candidate_discovery["technical_deep_check_error_type"] = str(
                    deep_check_result.get("error_type")
                )
            batch_audit = deep_check_result.get("batch_audit")
            if isinstance(batch_audit, Mapping):
                candidate_discovery["structured_batch_audit"] = deepcopy(
                    dict(batch_audit)
                )
            daily_analysis = deep_check_result.get("daily_analysis")
            if isinstance(daily_analysis, dict):
                eligible_count = int(candidate_discovery.get("eligible_count") or 0)
                public_preselected_count = len(definitions)
                input_exhausted = public_preselected_count >= eligible_count
                daily_analysis["input_exhausted"] = input_exhausted
                daily_analysis["researchable_input_count"] = eligible_count
                daily_analysis["ranked_input_count"] = public_preselected_count
                candidate_discovery["daily_structured_analysis"] = deepcopy(
                    daily_analysis
                )
                candidate_discovery["structured_batch_audit"] = deepcopy(
                    deep_check_result.get("batch_audit") or {}
                )
                if (
                    daily_analysis.get("minimum_met") is not True
                    and not input_exhausted
                ):
                    deep_check_result = {
                        "status": "technical_deep_check_failed",
                        "error_type": "SupplementalUniverseTruncated",
                        "candidates": [],
                        "daily_analysis": deepcopy(daily_analysis),
                        "batch_audit": deepcopy(
                            deep_check_result.get("batch_audit") or {}
                        ),
                    }
        _require_opportunity_time(context, stage="technical_deep_inspection")

    _require_opportunity_time(context, stage="orchestration")
    payload = build_public_research_opportunities_payload(
        discovery_result,
        deep_check_result,
        external_risk_level=external_risk_level,
        database_status=database_status,
        context=context,
    )
    _require_opportunity_time(context, stage="orchestration")
    return payload


def _public_discovery_failure_details(
    exc: CLIError,
    discovery_result: Any,
) -> Dict[str, Any]:
    raw_candidate_discovery = (
        discovery_result.get("candidate_discovery")
        if isinstance(discovery_result, Mapping)
        else None
    )
    exception_discovery = (
        exc.details.get("candidate_discovery")
        if isinstance(exc.details, Mapping)
        else None
    )
    if isinstance(exception_discovery, Mapping):
        raw_candidate_discovery = {
            **(
                dict(raw_candidate_discovery)
                if isinstance(raw_candidate_discovery, Mapping)
                else {}
            ),
            **dict(exception_discovery),
        }
    candidate_discovery = (
        deepcopy(dict(raw_candidate_discovery))
        if isinstance(raw_candidate_discovery, Mapping)
        else {"status": "candidate_discovery_unavailable"}
    )

    details_stage = (
        exc.details.get("stage")
        if isinstance(exc.details, Mapping)
        else None
    )
    discovery_stage = (
        discovery_result.get("stage")
        if isinstance(discovery_result, Mapping)
        else None
    )
    return {
        "stage": str(
            details_stage
            or exc.stage
            or discovery_stage
            or "candidate_discovery"
        ),
        "candidate_discovery": candidate_discovery,
    }


def _build_public_full_market_research_payload(
    *,
    context: OpportunityMarketContext,
    external_risk_level: Optional[str] = None,
    database_status: Optional[Dict[str, Any]] = None,
    excluded_code_reasons: Optional[Mapping[str, str]] = None,
    board_exclusion_reasons: Optional[Mapping[str, str]] = None,
    star_market_exclusion_reason: Optional[str] = None,
    research_progress_callback: Optional[
        Callable[[Dict[str, Any]], None]
    ] = None,
    resume_checkpoint: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    discovery_state: Dict[str, Any] = {}
    try:
        return _orchestrate_public_full_market_research_payload(
            context=context,
            external_risk_level=external_risk_level,
            database_status=database_status,
            discovery_state=discovery_state,
            excluded_code_reasons=excluded_code_reasons,
            board_exclusion_reasons=board_exclusion_reasons,
            star_market_exclusion_reason=star_market_exclusion_reason,
            research_progress_callback=research_progress_callback,
            resume_checkpoint=resume_checkpoint,
        )
    except CLIError as exc:
        if exc.code == "TechnicalHistoryFetchError":
            raise CLIError(
                exc.message,
                code=exc.code,
                exit_code=exc.exit_code,
                stage=exc.stage,
                details=_public_discovery_failure_details(
                    exc,
                    discovery_state.get("result"),
                ),
            ) from exc
        if exc.code in {
            "stage_timeout",
            "technical_deep_check_timeout",
            "invalid_external_risk_level",
        }:
            raise
        raise CLIError(
            "公开全市场候选发现不可用",
            code="candidate_discovery_unavailable",
            exit_code=4,
            details=_public_discovery_failure_details(
                exc,
                discovery_state.get("result"),
            ),
        ) from exc


def run_public_full_market_research(
    *,
    external_risk_level: Optional[str] = None,
    excluded_code_reasons: Optional[Mapping[str, str]] = None,
    board_exclusion_reasons: Optional[Mapping[str, str]] = None,
    star_market_exclusion_reason: Optional[str] = None,
    research_progress_callback: Optional[
        Callable[[Dict[str, Any]], None]
    ] = None,
    resume_checkpoint: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the account-independent full-market research workflow.

    This is the public application entrypoint for callers that need the same
    bounded Tencent/Sina research pipeline as the holdings CLI without parsing
    CLI output or accessing account data.
    """
    context = build_opportunity_market_context()
    return _build_public_full_market_research_payload(
        context=context,
        external_risk_level=external_risk_level,
        excluded_code_reasons=excluded_code_reasons,
        board_exclusion_reasons=board_exclusion_reasons,
        star_market_exclusion_reason=star_market_exclusion_reason,
        research_progress_callback=research_progress_callback,
        resume_checkpoint=resume_checkpoint,
        database_status={
            "status": "not_required",
            "reason_code": "public_research_mode",
        },
    )


def _cli_error_payload(
    exc: CLIError,
    *,
    include_stage: bool = False,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": exc.code, "message": exc.message}
    details_has_stage = isinstance(exc.details, Mapping) and "stage" in exc.details
    if include_stage and exc.stage is not None and not details_has_stage:
        error["stage"] = exc.stage
    if exc.details:
        error["details"] = deepcopy(exc.details)
    return {"ok": False, "error": error}


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
            _cli_error_payload(exc),
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


def _optional_market_database(
    *,
    timeout_cap_ms: Optional[int] = None,
) -> tuple[Any, Dict[str, Any]]:
    try:
        if timeout_cap_ms is None:
            database = _get_database()
        else:
            database = _get_database(timeout_cap_ms=timeout_cap_ms)
        return database, {"status": "connected"}
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
    help="无需登录输出A股市场门禁 JSON；Mongo宽度不可用时限时尝试新浪公共宽度",
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
                retry_public_timeout=True,
            )
        _write_json(payload, pretty=pretty)
    except CLIError as exc:
        _write_json(
            _cli_error_payload(exc),
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc


@holdings_app.command(
    name="earnings",
    help="无需登录批量核对 A 股业绩预告和最新强制披露实绩，最多 8 只",
)
def earnings_command(
    codes: Optional[List[str]] = typer.Option(
        None,
        "--code",
        help="A 股代码，可重复传入，最多 8 只",
    ),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    try:
        normalized_codes = _normalize_public_earnings_codes(codes)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            context = build_opportunity_market_context()
            _require_opportunity_time(context, stage="tencent_market_context")
            with _opportunity_wall_clock_guard(
                context,
                stage="earnings_forecast_review",
            ):
                payload = build_public_candidate_earnings_payload(
                    normalized_codes,
                    context=context,
                )
            _require_opportunity_time(
                context,
                stage="earnings_forecast_review",
            )
            serialized_payload = _serialize_json(payload, pretty=pretty)
            _require_opportunity_time(
                context,
                stage="earnings_forecast_review",
            )
        _write_serialized_json(serialized_payload)
    except CLIError as exc:
        _write_json(
            _cli_error_payload(exc, include_stage=True),
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc


@holdings_app.command(
    name="notices",
    help="无需登录批量核对 A 股最近 7 个自然日公告，最多 8 只",
)
def notices_command(
    codes: Optional[List[str]] = typer.Option(
        None,
        "--code",
        help="A 股代码，可重复传入，最多 8 只",
    ),
    lookback_days: int = typer.Option(
        NOTICE_LOOKBACK_CALENDAR_DAYS,
        "--lookback-days",
        help="公告回看自然日，1-90；超过 7 天改用个股公告接口",
    ),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    try:
        normalized_codes = _normalize_public_notice_codes(codes)
        normalized_lookback_days = _normalize_notice_lookback_days(
            lookback_days
        )
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            context = build_opportunity_market_context()
            _require_opportunity_time(context, stage="tencent_market_context")
            with _opportunity_wall_clock_guard(
                context,
                stage="recent_notice_review",
            ):
                payload = build_public_candidate_notice_payload(
                    normalized_codes,
                    context=context,
                    lookback_calendar_days=normalized_lookback_days,
                )
            _require_opportunity_time(
                context,
                stage="recent_notice_review",
            )
            serialized_payload = _serialize_json(payload, pretty=pretty)
            _require_opportunity_time(
                context,
                stage="recent_notice_review",
            )
        _write_serialized_json(serialized_payload)
    except CLIError as exc:
        _write_json(
            _cli_error_payload(exc, include_stage=True),
            pretty=pretty,
            stderr=True,
        )
        raise typer.Exit(exc.exit_code) from exc


@holdings_app.command(
    name="opportunities",
    help=(
        "输出未来两日观察池 JSON；不传候选时优先使用 Mongo；"
        "Mongo 不可用，或行情候选池不可用、为空、过期、覆盖不足时"
        "自动执行公开全市场研究"
    ),
)
def opportunities_command(
    username: Optional[str] = typer.Option(None, "--username", help="登录用户名"),
    email: Optional[str] = typer.Option(None, "--email", help="登录邮箱"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="用户 ObjectId"),
    candidate_codes: Optional[List[str]] = typer.Option(
        None,
        "--candidate-code",
        help="手工候选路径，可重复传入，最多 8 只",
    ),
    external_risk_level: Optional[str] = typer.Option(
        None,
        "--external-risk-level",
        help="外部风险等级 green/yellow/red；不传按 unknown 0% 处理",
    ),
    target_exposure_pct: Optional[float] = typer.Option(
        None,
        "--target-exposure-pct",
        help="显式截止日目标仓位百分比；必须与 --deployment-deadline 一起使用",
    ),
    deployment_deadline: Optional[str] = typer.Option(
        None,
        "--deployment-deadline",
        help="仓位目标截止日，格式 YYYY-MM-DD；必须显式提供候选代码",
    ),
    pretty: bool = typer.Option(False, "--pretty", help="格式化 JSON 输出"),
) -> None:
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            _validate_external_risk_level(external_risk_level)
            deployment_objective = _validate_deployment_objective(
                target_exposure_pct,
                deployment_deadline,
            )
            if deployment_objective is not None and not candidate_codes:
                raise CLIError(
                    "截止日仓位目标必须显式提供至少一个 --candidate-code",
                    code="deployment_objective_requires_candidates",
                )
            context = build_opportunity_market_context()
            _require_opportunity_time(context, stage="tencent_market_context")
            precomputed_manual_earnings_review = None
            if candidate_codes:
                manual_definitions = _candidate_definitions(candidate_codes)
                precomputed_manual_earnings_review = _run_opportunity_sync_builder(
                    context,
                    lambda: _manual_candidate_earnings_review(
                        manual_definitions,
                        benchmark_trade_date=getattr(
                            context,
                            "benchmark_trade_date",
                            None,
                        ),
                    ),
                )
            mongo_timeout_seconds = context.stage_timeout("mongo")
            if mongo_timeout_seconds <= 0:
                database, database_status = None, {
                    "status": "unavailable",
                    "error_code": "stage_timeout",
                    "stage": "mongo",
                    "reason": "command_deadline_exceeded",
                }
            else:
                mongo_timeout_ms = min(
                    5000,
                    max(1, int(mongo_timeout_seconds * 1000)),
                )
                database, database_status = _optional_market_database(
                    timeout_cap_ms=mongo_timeout_ms,
                )
            _require_opportunity_time(context, stage="mongo")
            if database is not None:
                try:
                    payload = _run_opportunity_sync_builder(
                        context,
                        lambda: build_opportunities_payload(
                            database,
                            username=username,
                            email=email,
                            user_id=user_id,
                            candidate_codes=candidate_codes,
                            buy_lot_size=DEFAULT_BUY_LOT_SIZE,
                            external_risk_level=external_risk_level,
                            context=context,
                            precomputed_manual_earnings_review=(
                                precomputed_manual_earnings_review
                            ),
                            target_exposure_pct=target_exposure_pct,
                            deployment_deadline=deployment_deadline,
                        ),
                    )
                except PyMongoError:
                    failed_database_status = {
                        "status": "unavailable",
                        "error_code": "database_error",
                    }
                    if candidate_codes:
                        payload = _run_opportunity_sync_builder(
                            context,
                            lambda: build_research_only_opportunities_payload(
                                candidate_codes=candidate_codes,
                                username=username,
                                email=email,
                                user_id=user_id,
                                external_risk_level=external_risk_level,
                                database_status=failed_database_status,
                                context=context,
                                precomputed_manual_earnings_review=(
                                    precomputed_manual_earnings_review
                                ),
                                target_exposure_pct=target_exposure_pct,
                                deployment_deadline=deployment_deadline,
                            ),
                        )
                    else:
                        payload = _run_opportunity_sync_builder(
                            context,
                            lambda: _build_public_full_market_research_payload(
                                context=context,
                                external_risk_level=external_risk_level,
                                database_status=failed_database_status,
                            ),
                        )
                else:
                    if (
                        not candidate_codes
                        and _should_fallback_to_public_research(payload)
                    ):
                        mongo_account_payload = payload
                        public_payload = _run_opportunity_sync_builder(
                            context,
                            lambda: _build_public_full_market_research_payload(
                                context=context,
                                external_risk_level=external_risk_level,
                                database_status={"status": "connected"},
                            ),
                        )
                        payload = build_account_context_public_research_payload(
                            public_payload,
                            mongo_account_payload,
                            as_of=context.now,
                            benchmark_session_dates=(
                                [context.benchmark_trade_date]
                                if context.benchmark_trade_date
                                else None
                            ),
                        )
            elif candidate_codes:
                payload = _run_opportunity_sync_builder(
                    context,
                    lambda: build_research_only_opportunities_payload(
                        candidate_codes=candidate_codes,
                        username=username,
                        email=email,
                        user_id=user_id,
                        external_risk_level=external_risk_level,
                        database_status=database_status,
                        context=context,
                        precomputed_manual_earnings_review=(
                            precomputed_manual_earnings_review
                        ),
                        target_exposure_pct=target_exposure_pct,
                        deployment_deadline=deployment_deadline,
                    ),
                )
            else:
                payload = _run_opportunity_sync_builder(
                    context,
                    lambda: _build_public_full_market_research_payload(
                        context=context,
                        external_risk_level=external_risk_level,
                        database_status=database_status,
                    ),
                )
            _require_opportunity_time(context, stage="orchestration")
            serialized_payload = _serialize_json(payload, pretty=pretty)
            _require_opportunity_time(context, stage="orchestration")
        _write_serialized_json(serialized_payload)
    except CLIError as exc:
        _write_json(
            _cli_error_payload(exc, include_stage=True),
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
