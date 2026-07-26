"""Command-scoped market data and deadline state for opportunities."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from app.services.public_market_breadth import fetch_sina_public_market_snapshot
from app.services.tencent_quote_service import fetch_tencent_quotes_sync


logger = logging.getLogger(__name__)


OPPORTUNITY_COMMAND_TIMEOUT_SECONDS = 90.0
OPPORTUNITY_STAGE_TIMEOUT_SECONDS = {
    "mongo": 5.0,
    "tencent_market_context": 10.0,
    "sina_public_snapshot": 25.0,
    "tencent_candidate_review": 10.0,
    "technical_deep_inspection": 50.0,
    "orchestration": 5.0,
}
A_SHARE_MAJOR_INDEX_SYMBOLS = (
    "sh000001",
    "sz399001",
    "sz399006",
    "sh000688",
)
CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
TENCENT_WORKER_STDERR_LOG_LIMIT = 512


SnapshotFetcher = Callable[..., Dict[str, Any]]
QuoteFetcher = Callable[..., Dict[str, Any]]


def _stage_timeout_result(
    stage: str,
    *,
    include_rows: bool = False,
    timeout_seconds: float = 0.0,
    reason: str = "command_deadline_exceeded",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "stage_timeout",
        "stage": stage,
        "reason": reason,
        "timeout_seconds": timeout_seconds,
    }
    if include_rows:
        result["rows"] = []
    return result


def _is_finite_int_or_float(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


@dataclass
class OpportunityMarketContext:
    now: datetime
    started_at: float
    deadline_at: float
    index_quotes: List[Dict[str, Any]]
    benchmark_trade_date: Optional[str]
    public_snapshot: Optional[Dict[str, Any]] = None
    public_snapshot_loaded: bool = False
    public_snapshot_retry_count: int = 0
    index_status: str = "not_loaded"
    index_error: Optional[Dict[str, Any]] = None
    monotonic: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )
    public_snapshot_fetcher: Optional[SnapshotFetcher] = field(
        default=None,
        repr=False,
        compare=False,
    )

    def remaining_seconds(self) -> float:
        return max(0.0, float(self.deadline_at) - float(self.monotonic()))

    def stage_timeout(self, stage: str) -> float:
        try:
            stage_limit = OPPORTUNITY_STAGE_TIMEOUT_SECONDS[stage]
        except KeyError as exc:
            raise ValueError(f"unknown opportunity stage: {stage}") from exc
        return min(stage_limit, self.remaining_seconds())

    def _cache_public_snapshot(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.public_snapshot = deepcopy(result)
        return deepcopy(self.public_snapshot)

    def ensure_public_snapshot(self) -> Dict[str, Any]:
        if self.public_snapshot_loaded:
            if self.public_snapshot is None:
                self.public_snapshot = {
                    "status": "public_snapshot_unavailable",
                    "rows": [],
                }
            return deepcopy(self.public_snapshot)

        self.public_snapshot_loaded = True
        timeout_seconds = self.stage_timeout("sina_public_snapshot")
        if timeout_seconds <= 0:
            return self._cache_public_snapshot(
                _stage_timeout_result(
                    "sina_public_snapshot",
                    include_rows=True,
                )
            )

        fetcher = self.public_snapshot_fetcher or fetch_sina_public_market_snapshot
        try:
            result = fetcher(
                benchmark_trade_date=self.benchmark_trade_date,
                timeout_seconds=timeout_seconds,
                now=self.now,
            )
        except Exception as exc:
            logger.exception("Public market snapshot fetch failed unexpectedly")
            result = {
                "status": "public_breadth_fetch_failed",
                "source": "akshare.sina.stock_zh_a_spot",
                "error_type": type(exc).__name__,
                "rows": [],
            }
        if self.remaining_seconds() <= 0:
            result = _stage_timeout_result(
                "sina_public_snapshot",
                include_rows=True,
            )
        if not isinstance(result, dict) or not result:
            result = {
                "status": "public_breadth_fetch_failed",
                "source": "akshare.sina.stock_zh_a_spot",
                "error_type": "InvalidFetcherPayload",
                "rows": [],
            }
        return self._cache_public_snapshot(result)

    def retry_public_snapshot_once_if_timeout(self) -> Dict[str, Any]:
        """Retry one transient Sina worker timeout without reopening other failures."""
        current = self.ensure_public_snapshot()
        if (
            current.get("status") != "public_breadth_timeout"
            or self.public_snapshot_retry_count >= 1
        ):
            return current

        self.public_snapshot_retry_count += 1
        self.public_snapshot = None
        self.public_snapshot_loaded = False
        result = self.ensure_public_snapshot()
        result["attempt_count"] = self.public_snapshot_retry_count + 1
        result["retried_after_status"] = "public_breadth_timeout"
        return self._cache_public_snapshot(result)


def _validate_index_result(
    result: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[List[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    if "error_type" not in result or result.get("error_type") is not None:
        return [], "index_response_invalid", {"reason": "error_type_not_null"}
    requested_codes = result.get("requested_codes")
    if requested_codes != list(A_SHARE_MAJOR_INDEX_SYMBOLS):
        return [], "index_requested_codes_mismatch", {
            "requested_codes": requested_codes,
        }
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) != len(A_SHARE_MAJOR_INDEX_SYMBOLS):
        return [], "index_quote_count_mismatch", {
            "row_count": len(rows) if isinstance(rows, list) else None,
        }

    ordered: List[Dict[str, Any]] = []
    trade_dates: List[date] = []
    for symbol, row in zip(A_SHARE_MAJOR_INDEX_SYMBOLS, rows):
        if not isinstance(row, Mapping):
            return [], "index_quote_identity_mismatch", {"symbol": symbol}
        if row.get("provider_symbol") != symbol:
            return [], "index_quote_identity_mismatch", {"symbol": symbol}
        if row.get("parse_status") != "ok":
            return [], "index_quote_parse_failed", {"symbol": symbol}
        pct_chg = row.get("pct_chg")
        if not _is_finite_int_or_float(pct_chg):
            return [], "index_quote_change_invalid", {"symbol": symbol}
        expected_code = symbol[2:]
        for key in ("code", "envelope_code", "payload_code"):
            value = row.get(key)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9]{6}", value) is None
                or value != expected_code
            ):
                return [], "index_quote_identity_mismatch", {
                    "symbol": symbol,
                    "field": key,
                }
        trade_date_value = row.get("trade_date")
        if trade_date_value in (None, ""):
            return [], "index_trade_date_missing", {"symbol": symbol}
        if (
            not isinstance(trade_date_value, str)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", trade_date_value)
            is None
        ):
            return [], "index_trade_date_invalid", {"symbol": symbol}
        try:
            parsed_trade_date = date.fromisoformat(trade_date_value)
        except ValueError:
            return [], "index_trade_date_invalid", {"symbol": symbol}
        trade_dates.append(parsed_trade_date)
        quote = dict(row)
        quote["requested_symbol"] = symbol
        ordered.append(quote)

    unique_trade_dates = sorted(set(trade_dates))
    if len(unique_trade_dates) != 1:
        return [], "index_trade_date_mismatch", {
            "trade_dates": [item.isoformat() for item in unique_trade_dates],
        }
    local_now = now.astimezone(CN_MARKET_TIMEZONE) if now.tzinfo else now.replace(
        tzinfo=CN_MARKET_TIMEZONE
    )
    if unique_trade_dates[0] > local_now.date():
        return [], "index_trade_date_in_future", {
            "trade_date": unique_trade_dates[0].isoformat(),
            "local_date": local_now.date().isoformat(),
        }
    return ordered, None, {"trade_date": unique_trade_dates[0].isoformat()}


def _set_index_failure(
    context: OpportunityMarketContext,
    status: str,
    **details: Any,
) -> OpportunityMarketContext:
    context.index_quotes = []
    context.index_status = status
    context.benchmark_trade_date = None
    context.index_error = {
        "status": status,
        "stage": "tencent_market_context",
        **details,
    }
    return context


def _build_tencent_worker_command(*, timeout_seconds: float) -> List[str]:
    return [
        sys.executable,
        "-m",
        "app.services.opportunity_market_context",
        "--tencent-worker",
        "--timeout-seconds",
        str(timeout_seconds),
    ]


def _truncate_worker_stderr(stderr: Any) -> str:
    text = str(stderr or "").strip()
    if len(text) <= TENCENT_WORKER_STDERR_LOG_LIMIT:
        return text
    return f"{text[:TENCENT_WORKER_STDERR_LOG_LIMIT]}...[truncated]"


def fetch_tencent_market_context_bounded(
    *,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Fetch the fixed major-index batch in a bounded child process."""
    effective_timeout = max(0.0, float(timeout_seconds))
    command = _build_tencent_worker_command(timeout_seconds=effective_timeout)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            **_stage_timeout_result(
                "tencent_market_context",
                include_rows=True,
                timeout_seconds=effective_timeout,
                reason="stage_timeout_exceeded",
            ),
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
        }
    except OSError as exc:
        logger.exception("Tencent market context worker failed to start")
        return {
            "status": "index_fetch_failed",
            "stage": "tencent_market_context",
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
            "rows": [],
            "error_type": type(exc).__name__,
        }
    if completed.returncode != 0:
        logger.error(
            "Tencent market context worker exited with nonzero status "
            "returncode=%s stderr=%s",
            completed.returncode,
            _truncate_worker_stderr(completed.stderr),
        )
        return {
            "status": "index_fetch_failed",
            "stage": "tencent_market_context",
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
            "rows": [],
            "error_type": "WorkerProcessError",
            "worker_exit_code": completed.returncode,
        }
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {
            "status": "index_fetch_failed",
            "stage": "tencent_market_context",
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
            "rows": [],
            "error_type": "InvalidWorkerOutput",
        }
    if not isinstance(payload, dict):
        return {
            "status": "index_fetch_failed",
            "stage": "tencent_market_context",
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
            "rows": [],
            "error_type": "InvalidWorkerPayload",
        }
    if payload.get("status") != "ok":
        logger.warning(
            "Tencent market context worker returned provider failure "
            "status=%s error_type=%s stderr=%s",
            payload.get("status"),
            payload.get("error_type"),
            _truncate_worker_stderr(completed.stderr),
        )
    return payload


def build_opportunity_market_context(
    *,
    now: Optional[datetime] = None,
    monotonic: Callable[[], float] = time.monotonic,
    quote_fetcher: Optional[QuoteFetcher] = None,
    public_snapshot_fetcher: Optional[SnapshotFetcher] = None,
) -> OpportunityMarketContext:
    started_at = float(monotonic())
    context = OpportunityMarketContext(
        now=now or datetime.now(CN_MARKET_TIMEZONE),
        started_at=started_at,
        deadline_at=started_at + OPPORTUNITY_COMMAND_TIMEOUT_SECONDS,
        index_quotes=[],
        benchmark_trade_date=None,
        monotonic=monotonic,
        public_snapshot_fetcher=public_snapshot_fetcher,
    )
    timeout_seconds = context.stage_timeout("tencent_market_context")
    if timeout_seconds <= 0:
        context.index_status = "stage_timeout"
        context.index_error = _stage_timeout_result("tencent_market_context")
        return context

    try:
        if quote_fetcher is None:
            result = fetch_tencent_market_context_bounded(
                timeout_seconds=timeout_seconds,
            )
        else:
            result = quote_fetcher(
                A_SHARE_MAJOR_INDEX_SYMBOLS,
                timeout=timeout_seconds,
            )
    except Exception as exc:
        logger.exception("Tencent market context fetch failed")
        return _set_index_failure(
            context,
            "index_fetch_failed",
            error_type=type(exc).__name__,
        )
    if context.remaining_seconds() <= 0:
        context.index_status = "stage_timeout"
        context.index_error = _stage_timeout_result("tencent_market_context")
        return context
    if not isinstance(result, Mapping):
        return _set_index_failure(
            context,
            "index_fetch_failed",
            error_type="InvalidFetcherPayload",
        )
    if result.get("status") == "stage_timeout":
        return _set_index_failure(
            context,
            "stage_timeout",
            reason=result.get("reason"),
            timeout_seconds=result.get("timeout_seconds"),
        )
    if result.get("status") != "ok":
        return _set_index_failure(
            context,
            "index_fetch_failed",
            provider_status=result.get("status"),
            error_type=result.get("error_type"),
        )

    index_quotes, validation_status, validation_details = _validate_index_result(
        result,
        now=context.now,
    )
    if validation_status:
        return _set_index_failure(
            context,
            validation_status,
            **validation_details,
        )

    context.index_quotes = index_quotes
    context.index_status = "ok"
    context.benchmark_trade_date = validation_details["trade_date"]
    return context


def _worker_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--tencent-worker", action="store_true")
    parser.add_argument("--timeout-seconds", required=True, type=float)
    args = parser.parse_args(argv)
    if not args.tencent_worker:
        return 2
    try:
        result = fetch_tencent_quotes_sync(
            A_SHARE_MAJOR_INDEX_SYMBOLS,
            timeout=args.timeout_seconds,
        )
    except Exception as exc:
        logger.exception("Tencent market context worker fetch failed")
        result = {
            "status": "index_fetch_failed",
            "requested_codes": list(A_SHARE_MAJOR_INDEX_SYMBOLS),
            "rows": [],
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
