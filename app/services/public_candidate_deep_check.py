"""Bounded technical deep checks for public research candidates."""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.public_candidate_discovery_service import (
    MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
)
from app.services.investment_policy import objective_tier_rank
from app.services.public_candidate_earnings_risk import (
    EARNINGS_ACTUAL_SOURCE,
    EARNINGS_FORECAST_SOURCE,
    PUBLIC_ACTUAL_EARNINGS_RISK_FLAGS,
    PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS,
    PUBLIC_EARNINGS_SCREEN_STATUS_KEYS,
    RELEVANT_FORECAST_METRICS,
    earnings_result_blocks_new_position,
    latest_completed_reporting_period,
    latest_mandatory_actual_reporting_period,
    screen_public_candidate_earnings_risk,
)
from app.services.public_candidate_notice_review import (
    NOTICE_LOOKBACK_CALENDAR_DAYS,
    NOTICE_REVIEW_SOURCE,
    review_public_candidate_notices,
    validate_public_candidate_notice_review,
)


logger = logging.getLogger(__name__)


MAX_PUBLIC_ROLLING_POOL_CANDIDATES = 100
MAX_PUBLIC_DEEP_RESEARCH_CANDIDATES = 15
# Backward-compatible import for callers that use this as a manual batch bound.
MAX_PUBLIC_DEEP_CHECK_CANDIDATES = MAX_PUBLIC_ROLLING_POOL_CANDIDATES
MAX_PUBLIC_TECHNICAL_CLOSEST_REJECTIONS = 5
PUBLIC_MIN_NET_REWARD_RISK = 1.5
TECHNICAL_DEEP_CHECK_TIMEOUT_SECONDS = 35.0
TECHNICAL_FUNNEL_TIMEOUT_SECONDS = 50.0
TECHNICAL_SCREEN_WORKERS = 6
MIN_TECHNICAL_HISTORY_COVERAGE_RATIO = 0.9
WORKER_STDERR_LOG_LIMIT = 512
A_SHARE_STOCK_CODE_PATTERN = re.compile(
    r"(?:[036][0-9]{5}|(?:43|83|87|88|92)[0-9]{4})"
)


Runner = Callable[..., Any]
CandidateBuilder = Callable[..., List[Dict[str, Any]]]
TechnicalScreener = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
EarningsScreener = Callable[..., Dict[str, Any]]
NoticeScreener = Callable[..., Dict[str, Any]]
CorporateActionLoader = Callable[[str], Dict[str, Any]]
PUBLIC_NOTICE_HARD_RISK_TAGS = frozenset(
    {"risk_warning", "sanctions_or_trade_restrictions"}
)

PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS = frozenset(
    {
        "fetch_error",
        "history_unavailable",
        "insufficient_history",
        "insufficient_ordered_levels",
        "invalid_merge_input",
        "invalid_price_ordering",
        "invalid_price_plan",
        "net_rr_below_1_5",
        "ok",
        "out_of_order_quote",
        "price_scale_mismatch",
        "quote_merge_failed",
        "technical_screen_internal_error",
        "technical_screen_invalid_result",
        "trend_recovery_required",
    }
)
FATAL_TECHNICAL_SCREEN_STATUS_KEYS = frozenset(
    {
        "technical_screen_internal_error",
        "technical_screen_invalid_result",
    }
)
TECHNICAL_HISTORY_UNAVAILABLE_STATUS_KEYS = frozenset(
    {"fetch_error", "history_unavailable"}
)


def _timeout_result() -> Dict[str, Any]:
    return {
        "status": "technical_deep_check_timeout",
        "candidates": [],
    }


def _invalid_input_result(error_type: str) -> Dict[str, Any]:
    return {
        "status": "technical_deep_check_invalid_input",
        "candidates": [],
        "error_type": error_type,
    }


def _failure_result(error_type: str) -> Dict[str, Any]:
    return {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": error_type,
    }


def _validate_inputs(
    definitions: Any,
    quote_map: Any,
    *,
    max_candidates: int = MAX_PUBLIC_DEEP_CHECK_CANDIDATES,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Dict[str, Any]]], Optional[str]]:
    if not isinstance(definitions, list):
        return None, None, "definitions_invalid"
    if len(definitions) > max_candidates:
        return None, None, "too_many_candidates"
    if not isinstance(quote_map, Mapping):
        return None, None, "quote_map_invalid"

    selected_definitions: List[Dict[str, Any]] = []
    selected_quotes: Dict[str, Dict[str, Any]] = {}
    seen_codes = set()
    for definition in definitions:
        if not isinstance(definition, Mapping):
            return None, None, "definition_invalid"
        code = definition.get("code")
        if (
            not isinstance(code, str)
            or A_SHARE_STOCK_CODE_PATTERN.fullmatch(code) is None
        ):
            return None, None, "invalid_code"
        if code in seen_codes:
            return None, None, "duplicate_code"
        seen_codes.add(code)
        if code not in quote_map:
            return None, None, "quote_missing"
        quote = quote_map.get(code)
        if not isinstance(quote, Mapping):
            return None, None, "quote_invalid"
        selected_definitions.append(deepcopy(dict(definition)))
        selected_quotes[code] = deepcopy(dict(quote))

    return selected_definitions, selected_quotes, None


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _screen_candidate_technical_plan(
    definition: Dict[str, Any],
    quote: Dict[str, Any],
) -> Dict[str, Any]:
    from app.services.holding_price_guardrails import (
        build_pullback_price_plan,
        build_technical_price_plan,
    )
    from app.services.holding_risk_sizing import apply_net_reward_risk_gate
    from app.services.tencent_quote_service import (
        fetch_tencent_daily_bars_sync,
        merge_tencent_quote_into_bars,
    )

    code = definition["code"]
    current_price = _finite_number(
        quote.get("close") or quote.get("price") or quote.get("current_price")
    )
    history = fetch_tencent_daily_bars_sync(code, prefer_cache=True)
    history_evidence = {
        "source": history.get("source"),
        "checked_at": history.get("checked_at"),
        "freshness": history.get("freshness"),
        "degraded": history.get("degraded") is True,
        "provider_errors": list(history.get("provider_errors") or []),
    }
    if history.get("ok") is not True:
        raw_status = history.get("status")
        status = (
            raw_status
            if raw_status in {"fetch_error", "insufficient_history"}
            else "history_unavailable"
        )
        return {
            "code": code,
            "status": status,
            "passed": False,
            "fatal_error": False,
            "guarded_price_plan": {
                "status": status,
                "actionable": False,
                "reason": history.get("reason"),
                "history_evidence": history_evidence,
            },
        }

    merged = merge_tencent_quote_into_bars(history.get("bars", []), quote)
    if merged.get("ok") is not True:
        raw_status = merged.get("status")
        status = (
            raw_status
            if raw_status
            in {"invalid_merge_input", "out_of_order_quote", "price_scale_mismatch"}
            else "quote_merge_failed"
        )
        return {
            "code": code,
            "status": status,
            "passed": False,
            "fatal_error": False,
            "guarded_price_plan": {
                "status": status,
                "actionable": False,
                "price_ratio": merged.get("price_ratio"),
            },
        }

    breakout_plan = build_technical_price_plan(
        merged.get("bars", []),
        current_price=current_price,
    )
    pullback_plan = build_pullback_price_plan(
        merged.get("bars", []),
        current_price=current_price,
    )

    def guard_plan(value: Dict[str, Any]) -> Dict[str, Any]:
        guarded = dict(value)
        guarded["history_status"] = history.get("status")
        guarded["history_evidence"] = history_evidence
        guarded["quote_merge_action"] = merged.get("merge_action")
        guarded = apply_net_reward_risk_gate(guarded, quantity=100)
        trend_context = (
            guarded.get("trend_context")
            if isinstance(guarded.get("trend_context"), Mapping)
            else {}
        )
        if trend_context.get("recovery_required") is True:
            guarded = {
                **guarded,
                "actionable": False,
                "status": "trend_recovery_required",
                "failed_gates": list(
                    dict.fromkeys(
                        list(guarded.get("failed_gates") or [])
                        + ["trend_recovery_required"]
                    )
                ),
            }
        return guarded

    breakout_plan = guard_plan(breakout_plan)
    pullback_plan = guard_plan(pullback_plan)
    if breakout_plan.get("status") == "ok" and breakout_plan.get("actionable") is True:
        plan = {
            **breakout_plan,
            "entry_strategy": "breakout",
            "alternative_pullback_plan": pullback_plan,
        }
    elif pullback_plan.get("status") == "ok" and pullback_plan.get("actionable") is True:
        plan = {
            **pullback_plan,
            "alternative_breakout_plan": breakout_plan,
        }
    else:
        plan = {
            **breakout_plan,
            "entry_strategy": "breakout",
            "alternative_pullback_plan": pullback_plan,
        }

    raw_status = plan.get("status")
    status = (
        raw_status
        if raw_status in PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS
        else "invalid_price_plan"
    )
    plan["status"] = status
    passed = bool(status == "ok" and plan.get("actionable") is True)
    return {
        "code": code,
        "status": status,
        "passed": passed,
        "fatal_error": False,
        "guarded_price_plan": plan,
    }


def _validate_candidates(
    candidates: Any,
    expected_codes: Sequence[str],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(candidates, list):
        return None, "InvalidWorkerCandidates"
    if len(candidates) != len(expected_codes):
        return None, "WorkerCandidateMismatch"

    normalized: List[Dict[str, Any]] = []
    candidate_codes: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return None, "InvalidWorkerCandidates"
        code = candidate.get("code")
        if not isinstance(code, str):
            return None, "WorkerCandidateMismatch"
        candidate_codes.append(code)
        normalized.append(deepcopy(dict(candidate)))
    if len(set(candidate_codes)) != len(candidate_codes):
        return None, "WorkerCandidateMismatch"
    if set(candidate_codes) != set(expected_codes):
        return None, "WorkerCandidateMismatch"
    return normalized, None


def _build_worker_command() -> List[str]:
    return [
        sys.executable,
        "-m",
        "app.services.public_candidate_deep_check",
        "--worker",
    ]


def _truncate_worker_stderr(stderr: Any) -> str:
    text = str(stderr or "").strip()
    if len(text) <= WORKER_STDERR_LOG_LIMIT:
        return text
    return f"{text[:WORKER_STDERR_LOG_LIMIT]}...[truncated]"


def _parse_remaining_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalized_benchmark_trade_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _empty_earnings_screen(benchmark_trade_date: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "source": EARNINGS_FORECAST_SOURCE,
        "actual_source": EARNINGS_ACTUAL_SOURCE,
        "report_period": latest_completed_reporting_period(
            benchmark_trade_date
        ),
        "actual_report_period": latest_mandatory_actual_reporting_period(
            benchmark_trade_date
        ),
        "screened_count": 0,
        "blocked_count": 0,
        "selected_count": 0,
        "blocked_codes": [],
        "selected_codes": [],
        "status_counts": {},
        "actual_status_counts": {},
        "results": [],
    }


def run_public_candidate_deep_check(
    definitions: Any,
    quote_map: Any,
    *,
    command_remaining_seconds: Any,
    runner: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Run technical checks in a child process bounded by the command deadline."""
    selected_definitions, selected_quotes, validation_error = _validate_inputs(
        definitions,
        quote_map,
    )
    if validation_error:
        return _invalid_input_result(validation_error)

    remaining_seconds = _parse_remaining_seconds(command_remaining_seconds)
    if remaining_seconds is None:
        return _invalid_input_result("command_remaining_invalid")
    effective_timeout = min(
        TECHNICAL_DEEP_CHECK_TIMEOUT_SECONDS,
        max(0.0, remaining_seconds),
    )
    if effective_timeout <= 0:
        return _timeout_result()

    selected_definitions = selected_definitions or []
    selected_quotes = selected_quotes or {}
    if not selected_definitions:
        return {"status": "ok", "candidates": []}

    try:
        worker_input = json.dumps(
            {
                "definitions": selected_definitions,
                "quote_map": selected_quotes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return _invalid_input_result("input_not_json_serializable")

    process_runner = runner or subprocess.run
    try:
        completed = process_runner(
            _build_worker_command(),
            input=worker_input,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _timeout_result()
    except OSError as exc:
        logger.exception("Public candidate deep-check worker failed to start")
        return _failure_result(type(exc).__name__)
    except Exception as exc:
        logger.exception("Public candidate deep-check runner failed")
        return _failure_result(type(exc).__name__)

    if getattr(completed, "returncode", None) != 0:
        logger.error(
            "Public candidate deep-check worker exited with nonzero status "
            "returncode=%s stderr=%s",
            getattr(completed, "returncode", None),
            _truncate_worker_stderr(getattr(completed, "stderr", "")),
        )
        return _failure_result("WorkerProcessError")

    output_lines = [
        line.strip()
        for line in str(getattr(completed, "stdout", "") or "").splitlines()
        if line.strip()
    ]
    if not output_lines:
        return _failure_result("InvalidWorkerOutput")
    try:
        payload = json.loads(output_lines[-1])
    except (TypeError, json.JSONDecodeError):
        return _failure_result("InvalidWorkerOutput")
    if not isinstance(payload, Mapping):
        return _failure_result("InvalidWorkerPayload")
    if payload.get("status") != "ok":
        error_type = payload.get("error_type")
        return _failure_result(
            error_type if isinstance(error_type, str) and error_type else "WorkerFailure"
        )

    candidates, candidate_error = _validate_candidates(
        payload.get("candidates"),
        [item["code"] for item in selected_definitions],
    )
    if candidate_error:
        return _failure_result(candidate_error)
    return {
        "status": "ok",
        "candidates": candidates or [],
    }


def _valid_closest_rejection(
    value: Any,
    *,
    definitions_by_code: Mapping[str, Mapping[str, Any]],
    selected_codes: set[str],
) -> bool:
    required_keys = {
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
    }
    if not isinstance(value, Mapping) or set(value) != required_keys:
        return False
    code = value.get("code")
    definition = definitions_by_code.get(code) if isinstance(code, str) else None
    if definition is None or code in selected_codes:
        return False
    expected_name = (
        definition.get("name")
        if isinstance(definition.get("name"), str)
        and definition.get("name")
        else code
    )
    net_reward_risk = _finite_number(value.get("net_reward_risk"))
    minimum = _finite_number(value.get("min_net_reward_risk"))
    gap = _finite_number(value.get("gap_to_min_net_reward_risk"))
    tencent_score = _finite_number(value.get("tencent_score"))
    expected_score = _finite_number(definition.get("tencent_score")) or 0.0
    return bool(
        value.get("name") == expected_name
        and value.get("status") == "net_rr_below_1_5"
        and net_reward_risk is not None
        and net_reward_risk < PUBLIC_MIN_NET_REWARD_RISK
        and minimum == PUBLIC_MIN_NET_REWARD_RISK
        and gap is not None
        and math.isclose(
            gap,
            round(PUBLIC_MIN_NET_REWARD_RISK - net_reward_risk, 4),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and tencent_score is not None
        and math.isclose(
            tencent_score,
            expected_score,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        and value.get("earnings_review_status") == "not_reviewed"
        and value.get("actionable") is False
        and value.get("is_reference_only") is True
    )


def validate_public_technical_screen_metadata(
    value: Any,
    *,
    expected_definitions: Sequence[Mapping[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    expected_codes = [definition.get("code") for definition in expected_definitions]
    definitions_by_code = {
        str(definition.get("code")): definition
        for definition in expected_definitions
        if isinstance(definition.get("code"), str)
    }
    if (
        len(expected_codes) != len(definitions_by_code)
        or any(not isinstance(code, str) for code in expected_codes)
    ):
        return None, "InvalidTechnicalScreenMetadata"
    if not isinstance(value, Mapping) or value.get("status") != "ok":
        return None, "InvalidTechnicalScreenMetadata"

    count_fields = (
        "screened_count",
        "passed_count",
        "selected_count",
        "closest_rejection_count",
    )
    if any(
        not isinstance(value.get(field), int)
        or isinstance(value.get(field), bool)
        or value[field] < 0
        for field in count_fields
    ):
        return None, "InvalidTechnicalScreenMetadata"
    deep_research_selected_count = value.get(
        "deep_research_selected_count",
        min(value["selected_count"], MAX_PUBLIC_DEEP_RESEARCH_CANDIDATES),
    )
    if (
        value["screened_count"] != len(expected_codes)
        or value["passed_count"] > value["screened_count"]
        or value["selected_count"]
        != min(value["passed_count"], MAX_PUBLIC_ROLLING_POOL_CANDIDATES)
        or not isinstance(deep_research_selected_count, int)
        or isinstance(deep_research_selected_count, bool)
        or deep_research_selected_count < 0
        or deep_research_selected_count
        > min(value["selected_count"], MAX_PUBLIC_DEEP_RESEARCH_CANDIDATES)
    ):
        return None, "InvalidTechnicalScreenMetadata"

    selected_codes = value.get("selected_codes")
    deep_research_selected_codes = value.get(
        "deep_research_selected_codes",
        list(selected_codes[:deep_research_selected_count]),
    )
    if (
        not isinstance(selected_codes, list)
        or len(selected_codes) != value["selected_count"]
        or len(set(selected_codes)) != len(selected_codes)
        or any(
            not isinstance(code, str) or code not in expected_codes
            for code in selected_codes
        )
    ):
        return None, "InvalidTechnicalScreenMetadata"
    if (
        not isinstance(deep_research_selected_codes, list)
        or len(deep_research_selected_codes)
        != deep_research_selected_count
        or len(set(deep_research_selected_codes))
        != len(deep_research_selected_codes)
        or any(code not in selected_codes for code in deep_research_selected_codes)
    ):
        return None, "InvalidTechnicalScreenMetadata"

    status_counts = value.get("status_counts")
    if (
        not isinstance(status_counts, Mapping)
        or not set(status_counts).issubset(PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS)
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for count in status_counts.values()
        )
        or sum(status_counts.values()) != value["screened_count"]
        or status_counts.get("ok", 0) != value["passed_count"]
    ):
        return None, "InvalidTechnicalScreenMetadata"

    closest_rejections = value.get("closest_rejections")
    expected_rejection_count = min(
        status_counts.get("net_rr_below_1_5", 0),
        MAX_PUBLIC_TECHNICAL_CLOSEST_REJECTIONS,
    )
    if (
        value["closest_rejection_count"] != expected_rejection_count
        or not isinstance(closest_rejections, list)
        or len(closest_rejections) != expected_rejection_count
        or len(
            {
                item.get("code")
                for item in closest_rejections
                if isinstance(item, Mapping)
            }
        )
        != expected_rejection_count
        or any(
            not _valid_closest_rejection(
                item,
                definitions_by_code=definitions_by_code,
                selected_codes=set(selected_codes),
            )
            for item in closest_rejections
        )
    ):
        return None, "InvalidTechnicalScreenMetadata"

    def rejection_order(item: Mapping[str, Any]) -> tuple:
        definition = definitions_by_code[str(item["code"])]
        one_lot_amount = _finite_number(
            definition.get("tencent_one_lot_amount")
        )
        return (
            -float(item["net_reward_risk"]),
            -float(item["tencent_score"]),
            one_lot_amount if one_lot_amount is not None else math.inf,
            str(item["code"]),
        )

    if closest_rejections != sorted(closest_rejections, key=rejection_order):
        return None, "InvalidTechnicalScreenMetadata"

    return {
        "status": "ok",
        "screened_count": value["screened_count"],
        "passed_count": value["passed_count"],
        "selected_count": value["selected_count"],
        "selected_codes": list(selected_codes),
        "deep_research_selected_count": deep_research_selected_count,
        "deep_research_selected_codes": list(deep_research_selected_codes),
        "status_counts": {
            str(key): int(count)
            for key, count in sorted(status_counts.items())
            if count
        },
        "closest_rejection_count": value["closest_rejection_count"],
        "closest_rejections": [
            deepcopy(dict(item)) for item in closest_rejections
        ],
    }, None


def _valid_unique_string_list(value: Any) -> bool:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        return False
    return len(value) == len(set(value))


def _valid_earnings_evidence(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    required_keys = {
        "metric",
        "forecast_type",
        "forecast_value",
        "forecast_change_pct",
        "forecast_text",
    }
    for item in value:
        if not isinstance(item, Mapping) or set(item) != required_keys:
            return False
        if item.get("metric") not in RELEVANT_FORECAST_METRICS:
            return False
        for field in ("forecast_type", "forecast_text"):
            field_value = item.get(field)
            if field_value is not None and not isinstance(field_value, str):
                return False
        for field in ("forecast_value", "forecast_change_pct"):
            field_value = item.get(field)
            if field_value is not None and _finite_number(field_value) is None:
                return False
    return True


def _valid_latest_actual_earnings(
    value: Any,
    *,
    expected_report_period: str,
    benchmark_trade_date: str,
) -> bool:
    required_keys = {
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
        "risk_flags",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required_keys
        or value.get("status") not in PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS
        or value.get("report_period") != expected_report_period
    ):
        return False
    risk_flags = value.get("risk_flags")
    if (
        not _valid_unique_string_list(risk_flags)
        or not set(risk_flags).issubset(PUBLIC_ACTUAL_EARNINGS_RISK_FLAGS)
    ):
        return False
    numeric_fields = (
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
    )
    if any(
        value.get(field) is not None
        and _finite_number(value.get(field)) is None
        for field in numeric_fields
    ):
        return False
    industry = value.get("industry")
    if industry is not None and (not isinstance(industry, str) or not industry):
        return False

    status = value["status"]
    if status == "actual_missing":
        missing_flags = {"actual_report_missing", "actual_net_profit_missing"}
        return bool(
            value.get("announcement_date") is None
            and all(value.get(field) is None for field in numeric_fields)
            and industry is None
            and len(risk_flags) == 1
            and risk_flags[0] in missing_flags
        )

    announcement_date = _normalized_benchmark_trade_date(
        value.get("announcement_date")
    )
    if (
        announcement_date is None
        or announcement_date > benchmark_trade_date
        or _finite_number(value.get("net_profit")) is None
    ):
        return False
    net_profit = float(value["net_profit"])
    if (status == "positive_profit") != (net_profit > 0):
        return False
    expected_flag_state = {
        "actual_net_loss": net_profit < 0,
        "severe_revenue_contraction": (
            value.get("revenue_yoy_pct") is not None
            and float(value["revenue_yoy_pct"]) <= -30
        ),
        "net_profit_yoy_decline": (
            value.get("net_profit_yoy_pct") is not None
            and float(value["net_profit_yoy_pct"]) < 0
        ),
        "negative_operating_cash_flow": (
            value.get("operating_cash_flow_per_share") is not None
            and float(value["operating_cash_flow_per_share"]) < 0
        ),
    }
    return all(
        (flag in risk_flags) == expected
        for flag, expected in expected_flag_state.items()
    ) and not set(risk_flags).intersection(
        {"actual_report_missing", "actual_net_profit_missing"}
    )


def validate_public_earnings_screen_metadata(
    value: Any,
    *,
    expected_codes: Sequence[str],
    expected_report_period: str,
    expected_actual_report_period: str,
    benchmark_trade_date: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if (
        not isinstance(value, Mapping)
        or value.get("status") != "ok"
        or value.get("source") != EARNINGS_FORECAST_SOURCE
        or value.get("actual_source") != EARNINGS_ACTUAL_SOURCE
        or value.get("report_period") != expected_report_period
        or value.get("actual_report_period") != expected_actual_report_period
    ):
        return None, "InvalidEarningsScreenMetadata"

    count_fields = ("screened_count", "blocked_count", "selected_count")
    if any(
        not isinstance(value.get(field), int)
        or isinstance(value.get(field), bool)
        or value[field] < 0
        for field in count_fields
    ):
        return None, "InvalidEarningsScreenMetadata"
    if (
        value["screened_count"] != len(expected_codes)
        or value["blocked_count"] + value["selected_count"]
        != value["screened_count"]
    ):
        return None, "InvalidEarningsScreenMetadata"

    blocked_codes = value.get("blocked_codes")
    selected_codes = value.get("selected_codes")
    if (
        not _valid_unique_string_list(blocked_codes)
        or not _valid_unique_string_list(selected_codes)
        or len(blocked_codes) != value["blocked_count"]
        or len(selected_codes) != value["selected_count"]
        or set(blocked_codes).intersection(selected_codes)
        or set(blocked_codes).union(selected_codes) != set(expected_codes)
    ):
        return None, "InvalidEarningsScreenMetadata"

    results = value.get("results")
    if not isinstance(results, list) or len(results) != len(expected_codes):
        return None, "InvalidEarningsScreenMetadata"
    result_codes: List[str] = []
    normalized_results: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, Mapping):
            return None, "InvalidEarningsScreenMetadata"
        code = item.get("code")
        status = item.get("status")
        blocks_new_position = item.get("blocks_new_position")
        announcement_date = item.get("announcement_date")
        forecast_types = item.get("forecast_types")
        loss_metrics = item.get("loss_metrics")
        reason_summary = item.get("reason_summary")
        evidence = item.get("evidence")
        latest_actual = item.get("latest_actual")
        if (
            not isinstance(code, str)
            or status not in PUBLIC_EARNINGS_SCREEN_STATUS_KEYS
            or not isinstance(blocks_new_position, bool)
            or not _valid_latest_actual_earnings(
                latest_actual,
                expected_report_period=expected_actual_report_period,
                benchmark_trade_date=benchmark_trade_date,
            )
            or blocks_new_position
            != earnings_result_blocks_new_position(
                forecast_status=status,
                evidence=evidence,
                latest_actual=latest_actual,
            )
            or (
                announcement_date is not None
                and _normalized_benchmark_trade_date(announcement_date) is None
            )
            or not _valid_unique_string_list(forecast_types)
            or not _valid_unique_string_list(loss_metrics)
            or any(metric not in RELEVANT_FORECAST_METRICS for metric in loss_metrics)
            or (reason_summary is not None and not isinstance(reason_summary, str))
            or not _valid_earnings_evidence(evidence)
            or (
                status == "no_forecast"
                and any(
                    (
                        announcement_date is not None,
                        bool(forecast_types),
                        bool(loss_metrics),
                        reason_summary is not None,
                        bool(evidence),
                    )
                )
            )
            or (status == "loss_forecast" and not loss_metrics)
        ):
            return None, "InvalidEarningsScreenMetadata"
        result_codes.append(code)
        normalized_results.append(deepcopy(dict(item)))
    if result_codes != list(expected_codes):
        return None, "InvalidEarningsScreenMetadata"

    derived_blocked_codes = [
        item["code"]
        for item in normalized_results
        if item["blocks_new_position"]
    ]
    derived_selected_codes = [
        item["code"]
        for item in normalized_results
        if not item["blocks_new_position"]
    ]
    if (
        list(blocked_codes) != derived_blocked_codes
        or list(selected_codes) != derived_selected_codes
    ):
        return None, "InvalidEarningsScreenMetadata"

    status_counts = value.get("status_counts")
    expected_status_counts = Counter(
        item["status"] for item in normalized_results
    )
    if (
        not isinstance(status_counts, Mapping)
        or not set(status_counts).issubset(PUBLIC_EARNINGS_SCREEN_STATUS_KEYS)
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for count in status_counts.values()
        )
        or dict(status_counts) != dict(expected_status_counts)
    ):
        return None, "InvalidEarningsScreenMetadata"

    actual_status_counts = value.get("actual_status_counts")
    expected_actual_status_counts = Counter(
        item["latest_actual"]["status"] for item in normalized_results
    )
    if (
        not isinstance(actual_status_counts, Mapping)
        or not set(actual_status_counts).issubset(
            PUBLIC_ACTUAL_EARNINGS_STATUS_KEYS
        )
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for count in actual_status_counts.values()
        )
        or dict(actual_status_counts) != dict(expected_actual_status_counts)
    ):
        return None, "InvalidEarningsScreenMetadata"

    return {
        "status": "ok",
        "source": EARNINGS_FORECAST_SOURCE,
        "actual_source": EARNINGS_ACTUAL_SOURCE,
        "report_period": expected_report_period,
        "actual_report_period": expected_actual_report_period,
        "screened_count": value["screened_count"],
        "blocked_count": value["blocked_count"],
        "selected_count": value["selected_count"],
        "blocked_codes": list(blocked_codes),
        "selected_codes": list(selected_codes),
        "status_counts": dict(sorted(status_counts.items())),
        "actual_status_counts": dict(sorted(actual_status_counts.items())),
        "results": normalized_results,
    }, None


def run_public_candidate_technical_funnel(
    definitions: Any,
    quote_map: Any,
    *,
    benchmark_trade_date: Any,
    command_remaining_seconds: Any,
    runner: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Screen the bounded universe and retain up to 100 structured candidates."""
    selected_definitions, selected_quotes, validation_error = _validate_inputs(
        definitions,
        quote_map,
        max_candidates=MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
    )
    if validation_error:
        return _invalid_input_result(validation_error)
    normalized_trade_date = _normalized_benchmark_trade_date(
        benchmark_trade_date
    )
    if normalized_trade_date is None:
        return _invalid_input_result("benchmark_trade_date_invalid")

    remaining_seconds = _parse_remaining_seconds(command_remaining_seconds)
    if remaining_seconds is None:
        return _invalid_input_result("command_remaining_invalid")
    effective_timeout = min(
        TECHNICAL_FUNNEL_TIMEOUT_SECONDS,
        max(0.0, remaining_seconds),
    )
    if effective_timeout <= 0:
        return {**_timeout_result(), "mode": "technical_funnel"}

    selected_definitions = selected_definitions or []
    selected_quotes = selected_quotes or {}
    if not selected_definitions:
        return {
            "status": "ok",
            "candidates": [],
            "technical_screen": {
                "status": "ok",
                "screened_count": 0,
                "passed_count": 0,
                "selected_count": 0,
                "selected_codes": [],
                "deep_research_selected_count": 0,
                "deep_research_selected_codes": [],
                "status_counts": {},
                "closest_rejection_count": 0,
                "closest_rejections": [],
            },
            "earnings_screen": _empty_earnings_screen(
                normalized_trade_date
            ),
        }

    try:
        worker_input = json.dumps(
            {
                "mode": "technical_funnel",
                "benchmark_trade_date": normalized_trade_date,
                "enable_notice_review": True,
                "definitions": selected_definitions,
                "quote_map": selected_quotes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return _invalid_input_result("input_not_json_serializable")

    process_runner = runner or subprocess.run
    try:
        completed = process_runner(
            _build_worker_command(),
            input=worker_input,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {**_timeout_result(), "mode": "technical_funnel"}
    except OSError as exc:
        logger.exception("Public candidate technical-funnel worker failed to start")
        return _failure_result(type(exc).__name__)
    except Exception as exc:
        logger.exception("Public candidate technical-funnel runner failed")
        return _failure_result(type(exc).__name__)

    if getattr(completed, "returncode", None) != 0:
        logger.error(
            "Public candidate technical-funnel worker exited with nonzero status "
            "returncode=%s stderr=%s",
            getattr(completed, "returncode", None),
            _truncate_worker_stderr(getattr(completed, "stderr", "")),
        )
        return _failure_result("WorkerProcessError")

    output_lines = [
        line.strip()
        for line in str(getattr(completed, "stdout", "") or "").splitlines()
        if line.strip()
    ]
    if not output_lines:
        return _failure_result("InvalidWorkerOutput")
    try:
        payload = json.loads(output_lines[-1])
    except (TypeError, json.JSONDecodeError):
        return _failure_result("InvalidWorkerOutput")
    if not isinstance(payload, Mapping):
        return _failure_result("InvalidWorkerPayload")
    if payload.get("status") != "ok":
        error_type = payload.get("error_type")
        return _failure_result(
            error_type
            if isinstance(error_type, str) and error_type
            else "WorkerFailure"
        )

    technical_screen, screen_error = validate_public_technical_screen_metadata(
        payload.get("technical_screen"),
        expected_definitions=selected_definitions,
    )
    if screen_error:
        return _failure_result(screen_error)
    assert technical_screen is not None
    expected_report_period = latest_completed_reporting_period(
        normalized_trade_date
    )
    expected_actual_report_period = latest_mandatory_actual_reporting_period(
        normalized_trade_date
    )
    earnings_screen, earnings_error = validate_public_earnings_screen_metadata(
        payload.get("earnings_screen"),
        expected_codes=technical_screen["selected_codes"],
        expected_report_period=expected_report_period,
        expected_actual_report_period=expected_actual_report_period,
        benchmark_trade_date=normalized_trade_date,
    )
    if earnings_error:
        return _failure_result(earnings_error)
    assert earnings_screen is not None
    candidates, candidate_error = _validate_candidates(
        payload.get("candidates"),
        technical_screen["selected_codes"],
    )
    if candidate_error:
        return _failure_result(candidate_error)
    raw_notice_review = payload.get("notice_review")
    if (
        isinstance(raw_notice_review, Mapping)
        and raw_notice_review.get("status") == "ok"
    ):
        benchmark_date = date.fromisoformat(normalized_trade_date)
        notice_review, notice_error = validate_public_candidate_notice_review(
            raw_notice_review,
            expected_codes=technical_screen["selected_codes"],
            expected_start_date=(
                benchmark_date - timedelta(days=NOTICE_LOOKBACK_CALENDAR_DAYS - 1)
            ),
            expected_end_date=benchmark_date,
        )
        if notice_error or notice_review is None:
            return _failure_result(notice_error or "InvalidNoticeReviewMetadata")
    elif isinstance(raw_notice_review, Mapping) and raw_notice_review.get(
        "status"
    ) in {"unavailable", "not_requested"}:
        notice_review = {
            "status": raw_notice_review.get("status"),
            "source": str(raw_notice_review.get("source") or NOTICE_REVIEW_SOURCE),
            "error_type": raw_notice_review.get("error_type"),
            "results": [],
        }
    else:
        notice_review = {
            "status": "not_requested",
            "source": NOTICE_REVIEW_SOURCE,
            "results": [],
        }
    return {
        "status": "ok",
        "candidates": candidates or [],
        "technical_screen": technical_screen,
        "earnings_screen": earnings_screen,
        "notice_review": deepcopy(notice_review),
        "pipeline_metrics": deepcopy(
            payload.get("pipeline_metrics")
            if isinstance(payload.get("pipeline_metrics"), Mapping)
            else {}
        ),
    }


def _normalized_technical_screen_result(
    value: Any,
    *,
    expected_code: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "code": expected_code,
            "status": "technical_screen_invalid_result",
            "passed": False,
            "fatal_error": True,
            "guarded_price_plan": {
                "status": "technical_screen_invalid_result",
                "actionable": False,
            },
        }
    status = value.get("status")
    plan = value.get("guarded_price_plan")
    passed = value.get("passed")
    fatal_error = value.get("fatal_error")
    if (
        value.get("code") != expected_code
        or status not in PUBLIC_TECHNICAL_SCREEN_STATUS_KEYS
        or not isinstance(passed, bool)
        or not isinstance(fatal_error, bool)
        or not isinstance(plan, Mapping)
        or plan.get("status") != status
        or not isinstance(plan.get("actionable"), bool)
        or passed != (status == "ok")
        or plan.get("actionable") is not passed
        or fatal_error != (status in FATAL_TECHNICAL_SCREEN_STATUS_KEYS)
    ):
        return {
            "code": expected_code,
            "status": "technical_screen_invalid_result",
            "passed": False,
            "fatal_error": True,
            "guarded_price_plan": {
                "status": "technical_screen_invalid_result",
                "actionable": False,
            },
        }

    fee_aware_trade = plan.get("fee_aware_trade")
    net_reward_risk = (
        _finite_number(fee_aware_trade.get("net_reward_risk"))
        if isinstance(fee_aware_trade, Mapping)
        else None
    )
    if passed:
        if (
            net_reward_risk is None
            or net_reward_risk < PUBLIC_MIN_NET_REWARD_RISK
        ):
            return {
                "code": expected_code,
                "status": "technical_screen_invalid_result",
                "passed": False,
                "fatal_error": True,
                "guarded_price_plan": {
                    "status": "technical_screen_invalid_result",
                    "actionable": False,
                },
            }
    elif status == "net_rr_below_1_5" and (
        net_reward_risk is None
        or net_reward_risk >= PUBLIC_MIN_NET_REWARD_RISK
    ):
        return {
            "code": expected_code,
            "status": "technical_screen_invalid_result",
            "passed": False,
            "fatal_error": True,
            "guarded_price_plan": {
                "status": "technical_screen_invalid_result",
                "actionable": False,
            },
        }
    return deepcopy(dict(value))


def _technical_result_selection_key(
    item: Mapping[str, Any],
    *,
    definitions_by_code: Mapping[str, Mapping[str, Any]],
    quote_map: Mapping[str, Mapping[str, Any]],
) -> tuple:
    code = str(item["code"])
    definition = definitions_by_code[code]
    fee_aware_trade = item["guarded_price_plan"]["fee_aware_trade"]
    net_reward_risk = float(fee_aware_trade["net_reward_risk"])
    tencent_score = _finite_number(definition.get("tencent_score")) or 0.0
    one_lot_amount = _finite_number(
        definition.get("tencent_one_lot_amount")
    )
    if one_lot_amount is None:
        price = _finite_number(
            quote_map[code].get("close") or quote_map[code].get("price")
        )
        one_lot_amount = price * 100 if price is not None else math.inf
    return (
        objective_tier_rank(definition.get("objective_tier")),
        -net_reward_risk,
        -tencent_score,
        one_lot_amount,
        code,
    )


def _technical_closest_rejections(
    screen_results: Sequence[Mapping[str, Any]],
    *,
    definitions_by_code: Mapping[str, Mapping[str, Any]],
    quote_map: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    rejected = [
        item
        for item in screen_results
        if item.get("status") == "net_rr_below_1_5"
    ]
    closest = sorted(
        rejected,
        key=lambda item: _technical_result_selection_key(
            item,
            definitions_by_code=definitions_by_code,
            quote_map=quote_map,
        ),
    )[:MAX_PUBLIC_TECHNICAL_CLOSEST_REJECTIONS]
    snapshots: List[Dict[str, Any]] = []
    for item in closest:
        code = str(item["code"])
        definition = definitions_by_code[code]
        fee_aware_trade = item["guarded_price_plan"]["fee_aware_trade"]
        net_reward_risk = float(fee_aware_trade["net_reward_risk"])
        name = definition.get("name")
        snapshots.append(
            {
                "code": code,
                "name": name if isinstance(name, str) and name else code,
                "status": "net_rr_below_1_5",
                "net_reward_risk": net_reward_risk,
                "min_net_reward_risk": PUBLIC_MIN_NET_REWARD_RISK,
                "gap_to_min_net_reward_risk": round(
                    PUBLIC_MIN_NET_REWARD_RISK - net_reward_risk,
                    4,
                ),
                "tencent_score": (
                    _finite_number(definition.get("tencent_score")) or 0.0
                ),
                "earnings_review_status": "not_reviewed",
                "actionable": False,
                "is_reference_only": True,
            }
        )
    return snapshots


def _run_technical_funnel_worker_payload(
    payload: Any,
    *,
    technical_screener: Optional[TechnicalScreener] = None,
    earnings_screener: Optional[EarningsScreener] = None,
    notice_screener: Optional[NoticeScreener] = None,
    corporate_action_loader: Optional[CorporateActionLoader] = None,
    candidate_builder: Optional[CandidateBuilder] = None,
) -> Dict[str, Any]:
    pipeline_started = time.perf_counter()
    if not isinstance(payload, Mapping):
        return _invalid_input_result("payload_invalid")
    definitions, quote_map, validation_error = _validate_inputs(
        payload.get("definitions"),
        payload.get("quote_map"),
        max_candidates=MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
    )
    if validation_error:
        return _invalid_input_result(validation_error)
    benchmark_trade_date = _normalized_benchmark_trade_date(
        payload.get("benchmark_trade_date")
    )
    if benchmark_trade_date is None:
        return _invalid_input_result("benchmark_trade_date_invalid")

    definitions = definitions or []
    quote_map = quote_map or {}
    if not definitions:
        return {
            "status": "ok",
            "candidates": [],
            "technical_screen": {
                "status": "ok",
                "screened_count": 0,
                "passed_count": 0,
                "selected_count": 0,
                "selected_codes": [],
                "deep_research_selected_count": 0,
                "deep_research_selected_codes": [],
                "status_counts": {},
                "closest_rejection_count": 0,
                "closest_rejections": [],
            },
            "earnings_screen": _empty_earnings_screen(
                benchmark_trade_date
            ),
        }

    screener = technical_screener or _screen_candidate_technical_plan

    def screen_one(definition: Dict[str, Any]) -> Dict[str, Any]:
        code = definition["code"]
        try:
            raw_result = screener(definition, quote_map[code])
        except Exception:
            logger.exception("Public candidate technical screen failed: code=%s", code)
            raw_result = {
                "code": code,
                "status": "technical_screen_internal_error",
                "passed": False,
                "fatal_error": True,
                "guarded_price_plan": {
                    "status": "technical_screen_internal_error",
                    "actionable": False,
                },
            }
        return _normalized_technical_screen_result(
            raw_result,
            expected_code=code,
        )

    technical_started = time.perf_counter()
    worker_count = min(TECHNICAL_SCREEN_WORKERS, len(definitions))
    with ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:
        screen_results = list(executor.map(screen_one, definitions))
    technical_seconds = time.perf_counter() - technical_started

    if any(item["fatal_error"] for item in screen_results):
        return _failure_result("TechnicalScreenError")

    unavailable_history_count = sum(
        item["status"] in TECHNICAL_HISTORY_UNAVAILABLE_STATUS_KEYS
        for item in screen_results
    )
    history_coverage_ratio = (
        (len(screen_results) - unavailable_history_count) / len(screen_results)
        if screen_results
        else 1.0
    )
    if history_coverage_ratio < MIN_TECHNICAL_HISTORY_COVERAGE_RATIO:
        return _failure_result("TechnicalHistoryFetchError")

    passing_results = [item for item in screen_results if item["passed"]]
    definitions_by_code = {item["code"]: item for item in definitions}
    selected_results = sorted(
        passing_results,
        key=lambda item: _technical_result_selection_key(
            item,
            definitions_by_code=definitions_by_code,
            quote_map=quote_map,
        ),
    )[
        :MAX_PUBLIC_ROLLING_POOL_CANDIDATES
    ]
    closest_rejections = _technical_closest_rejections(
        screen_results,
        definitions_by_code=definitions_by_code,
        quote_map=quote_map,
    )
    technical_selected_codes = [item["code"] for item in selected_results]
    expected_report_period = latest_completed_reporting_period(
        benchmark_trade_date
    )
    expected_actual_report_period = latest_mandatory_actual_reporting_period(
        benchmark_trade_date
    )
    if technical_selected_codes:
        effective_earnings_screener = (
            earnings_screener or screen_public_candidate_earnings_risk
        )
        earnings_started = time.perf_counter()
        try:
            raw_earnings_screen = effective_earnings_screener(
                technical_selected_codes,
                benchmark_trade_date=benchmark_trade_date,
            )
        except Exception:
            logger.exception("Public candidate earnings screen failed")
            return _failure_result("EarningsForecastFetchError")
        earnings_seconds = time.perf_counter() - earnings_started
        raw_earnings_status = (
            raw_earnings_screen.get("status")
            if isinstance(raw_earnings_screen, Mapping)
            else None
        )
        if raw_earnings_status != "ok":
            error_type = (
                "EarningsForecastFetchError"
                if raw_earnings_status == "earnings_forecast_unavailable"
                else "EarningsActualFetchError"
                if raw_earnings_status == "earnings_actual_unavailable"
                else "EarningsForecastScreenError"
            )
            return _failure_result(error_type)
        normalized_earnings_screen, earnings_error = (
            validate_public_earnings_screen_metadata(
                raw_earnings_screen,
                expected_codes=technical_selected_codes,
                expected_report_period=expected_report_period,
                expected_actual_report_period=expected_actual_report_period,
                benchmark_trade_date=benchmark_trade_date,
            )
        )
        if earnings_error:
            return _failure_result("EarningsForecastScreenError")
        assert normalized_earnings_screen is not None
        earnings_screen = normalized_earnings_screen
    else:
        earnings_seconds = 0.0
        earnings_screen = _empty_earnings_screen(benchmark_trade_date)

    notice_review: Dict[str, Any] = {
        "status": "not_requested",
        "source": NOTICE_REVIEW_SOURCE,
        "results": [],
    }
    notice_seconds = 0.0
    if payload.get("enable_notice_review") is True and technical_selected_codes:
        effective_notice_screener = (
            notice_screener or review_public_candidate_notices
        )
        benchmark_date = date.fromisoformat(benchmark_trade_date)
        notice_started = time.perf_counter()
        try:
            raw_notice_review = effective_notice_screener(
                technical_selected_codes,
                as_of_date=benchmark_date,
            )
        except Exception as exc:
            logger.exception("Public candidate notice review failed")
            raw_notice_review = {
                "status": "notice_source_unavailable",
                "source": NOTICE_REVIEW_SOURCE,
                "error_type": type(exc).__name__,
            }
        if (
            isinstance(raw_notice_review, Mapping)
            and raw_notice_review.get("status") == "ok"
        ):
            normalized_notice, notice_error = (
                validate_public_candidate_notice_review(
                    raw_notice_review,
                    expected_codes=technical_selected_codes,
                    expected_start_date=(
                        benchmark_date
                        - timedelta(days=NOTICE_LOOKBACK_CALENDAR_DAYS - 1)
                    ),
                    expected_end_date=benchmark_date,
                )
            )
            if notice_error or normalized_notice is None:
                notice_review = {
                    "status": "unavailable",
                    "source": NOTICE_REVIEW_SOURCE,
                    "error_type": notice_error or "InvalidNoticeReviewMetadata",
                    "results": [],
                }
            else:
                notice_review = normalized_notice
        else:
            notice_review = {
                "status": "unavailable",
                "source": NOTICE_REVIEW_SOURCE,
                "error_type": (
                    raw_notice_review.get("error_type")
                    if isinstance(raw_notice_review, Mapping)
                    else "InvalidNoticeReviewMetadata"
                ),
                "results": [],
            }
        notice_seconds = time.perf_counter() - notice_started

    earnings_selected_codes = earnings_screen["selected_codes"]
    selected_codes = technical_selected_codes
    selected_definitions = [definitions_by_code[code] for code in selected_codes]
    selected_results_by_code = {
        item["code"]: item for item in selected_results
    }
    technical_plan_snapshots = {
        code: deepcopy(
            selected_results_by_code[code]["guarded_price_plan"]
        )
        for code in selected_codes
    }

    corporate_action_snapshots: Optional[Dict[str, Dict[str, Any]]] = None
    corporate_action_seconds = 0.0
    corporate_action_calls = 0
    if selected_codes and (
        candidate_builder is None or corporate_action_loader is not None
    ):
        if corporate_action_loader is None:
            from app.services.corporate_action_service import (
                fetch_cn_dividend_calendar_sync,
            )

            corporate_action_loader = fetch_cn_dividend_calendar_sync

        corporate_action_started = time.perf_counter()

        def load_corporate_action(code: str) -> tuple[str, Dict[str, Any]]:
            assert corporate_action_loader is not None
            try:
                value = corporate_action_loader(code)
            except Exception as exc:
                logger.exception(
                    "Public candidate corporate-action review failed: code=%s",
                    code,
                )
                value = {
                    "ok": False,
                    "source": "cninfo_via_akshare",
                    "code": code,
                    "status": "corporate_action_unavailable",
                    "blocks_new_position": False,
                    "price_plan_adjustment_required": False,
                    "nearest_action": None,
                    "reason": type(exc).__name__,
                    "is_reference_only": True,
                }
            return code, deepcopy(dict(value)) if isinstance(value, Mapping) else {}

        with ThreadPoolExecutor(
            max_workers=min(TECHNICAL_SCREEN_WORKERS, len(selected_codes))
        ) as executor:
            corporate_action_snapshots = dict(
                executor.map(load_corporate_action, selected_codes)
            )
        corporate_action_calls = len(selected_codes)
        corporate_action_seconds = time.perf_counter() - corporate_action_started

    candidate_build_started = time.perf_counter()
    if not selected_codes:
        candidates = []
    else:
        if candidate_builder is None:
            from app.services.holdings_cli import _build_opportunity_candidates

            candidate_builder = _build_opportunity_candidates
        try:
            candidates = candidate_builder(
                selected_definitions,
                cash=None,
                buy_lot_size=100,
                holding_themes=set(),
                allow_reference_price_plan=True,
                quote_snapshots={code: quote_map[code] for code in selected_codes},
                technical_plan_snapshots=technical_plan_snapshots,
                corporate_action_snapshots=corporate_action_snapshots,
            )
        except Exception as exc:
            logger.exception("Public candidate survivor deep check failed")
            return _failure_result(type(exc).__name__)

    candidate_build_seconds = time.perf_counter() - candidate_build_started
    normalized, candidate_error = _validate_candidates(candidates, selected_codes)
    if candidate_error:
        return _failure_result(candidate_error)
    earnings_results = {
        item["code"]: item
        for item in earnings_screen.get("results", [])
        if isinstance(item, Mapping) and isinstance(item.get("code"), str)
    }
    notice_results = {
        item["code"]: item
        for item in notice_review.get("results", [])
        if isinstance(item, Mapping) and isinstance(item.get("code"), str)
    }
    notice_unavailable = notice_review.get("status") == "unavailable"
    notice_blocked_codes = {
        code
        for code, item in notice_results.items()
        if set(item.get("attention_tags") or []).intersection(
            PUBLIC_NOTICE_HARD_RISK_TAGS
        )
    }
    deep_research_selected_codes = [
        code
        for code in earnings_selected_codes
        if code not in notice_blocked_codes
    ][:MAX_PUBLIC_DEEP_RESEARCH_CANDIDATES]
    deep_codes = set(deep_research_selected_codes)
    for candidate in normalized or []:
        code = candidate["code"]
        earnings = deepcopy(earnings_results.get(code) or {})
        notice = deepcopy(notice_results.get(code) or {})
        earnings_blocked = code in set(earnings_screen["blocked_codes"])
        notice_blocked = code in notice_blocked_codes
        hard_risk_reasons = []
        if earnings_blocked:
            hard_risk_reasons.append("earnings_risk_blocked")
        if notice_blocked:
            hard_risk_reasons.append("notice_risk_blocked")
        if notice_unavailable:
            hard_risk_reasons.append("notice_evidence_unavailable")
        blocked = bool(hard_risk_reasons)
        candidate["research_tier"] = "deep" if code in deep_codes else "structured"
        candidate["rolling_pool_state"] = "current"
        candidate["structured_review"] = {
            "technical": {"status": "passed"},
            "earnings": earnings,
            "notice": notice,
            "hard_risk_status": "blocked" if blocked else "clear",
            "hard_risk_clear": not blocked,
            "hard_risk_reasons": hard_risk_reasons,
        }
        if blocked:
            flags = candidate.get("risk_flags")
            flags = list(flags) if isinstance(flags, list) else []
            flags.append(
                {
                    "code": hard_risk_reasons[0],
                    "severity": "blocked",
                    "message": "结构化硬风险复核阻止进入执行层。",
                }
            )
            candidate["risk_flags"] = flags
    status_counts = Counter(item["status"] for item in screen_results)
    technical_cache_hit_count = sum(
        bool(item.get("cache_hit"))
        or bool(
            isinstance(item.get("guarded_price_plan"), Mapping)
            and isinstance(
                item["guarded_price_plan"].get("history_evidence"), Mapping
            )
            and item["guarded_price_plan"]["history_evidence"].get("freshness")
            in {"cache", "cached", "cached_fresh"}
        )
        for item in screen_results
    )
    return {
        "status": "ok",
        "candidates": normalized or [],
        "technical_screen": {
            "status": "ok",
            "screened_count": len(screen_results),
            "passed_count": len(passing_results),
            "selected_count": len(technical_selected_codes),
            "selected_codes": technical_selected_codes,
            "deep_research_selected_count": len(deep_research_selected_codes),
            "deep_research_selected_codes": deep_research_selected_codes,
            "status_counts": dict(sorted(status_counts.items())),
            "closest_rejection_count": len(closest_rejections),
            "closest_rejections": closest_rejections,
        },
        "earnings_screen": earnings_screen,
        "notice_review": notice_review,
        "pipeline_metrics": {
            "rolling_pool_capacity": MAX_PUBLIC_ROLLING_POOL_CANDIDATES,
            "deep_research_capacity": MAX_PUBLIC_DEEP_RESEARCH_CANDIDATES,
            "technical_input_count": len(definitions),
            "technical_worker_count": worker_count,
            "technical_data_calls": len(definitions),
            "technical_cache_hit_count": technical_cache_hit_count,
            "earnings_batch_calls": 1 if technical_selected_codes else 0,
            "notice_batch_calls": (
                1
                if payload.get("enable_notice_review") is True
                and technical_selected_codes
                else 0
            ),
            "candidate_build_calls": 1 if selected_codes else 0,
            "corporate_action_calls": corporate_action_calls,
            "technical_seconds": round(technical_seconds, 6),
            "earnings_seconds": round(earnings_seconds, 6),
            "notice_seconds": round(notice_seconds, 6),
            "candidate_build_seconds": round(candidate_build_seconds, 6),
            "corporate_action_seconds": round(corporate_action_seconds, 6),
            "total_seconds": round(time.perf_counter() - pipeline_started, 6),
        },
    }


def _run_worker_payload(
    payload: Any,
    *,
    candidate_builder: Optional[CandidateBuilder] = None,
) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        return _invalid_input_result("payload_invalid")
    definitions, quote_map, validation_error = _validate_inputs(
        payload.get("definitions"),
        payload.get("quote_map"),
    )
    if validation_error:
        return _invalid_input_result(validation_error)

    definitions = definitions or []
    quote_map = quote_map or {}
    if not definitions:
        return {"status": "ok", "candidates": []}
    if candidate_builder is None:
        from app.services.holdings_cli import _build_opportunity_candidates

        candidate_builder = _build_opportunity_candidates

    try:
        candidates = candidate_builder(
            definitions,
            cash=None,
            buy_lot_size=100,
            holding_themes=set(),
            allow_reference_price_plan=True,
            quote_snapshots=quote_map,
        )
    except Exception as exc:
        logger.exception("Public candidate technical deep check failed")
        return _failure_result(type(exc).__name__)

    normalized, candidate_error = _validate_candidates(
        candidates,
        [item["code"] for item in definitions],
    )
    if candidate_error:
        return _failure_result(candidate_error)
    return {
        "status": "ok",
        "candidates": normalized or [],
    }


def _worker_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args(argv)
    if not args.worker:
        return 2

    try:
        payload = json.loads(sys.stdin.read())
    except (TypeError, json.JSONDecodeError):
        result = _invalid_input_result("InvalidWorkerInput")
    else:
        captured_stdout = io.StringIO()
        with redirect_stdout(captured_stdout):
            result = (
                _run_technical_funnel_worker_payload(payload)
                if isinstance(payload, Mapping)
                and payload.get("mode") == "technical_funnel"
                else _run_worker_payload(payload)
            )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
