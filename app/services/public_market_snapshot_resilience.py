"""Bounded public-market snapshot retries and auditable Mongo fallbacks."""

from __future__ import annotations

import math
import re
import time
from copy import deepcopy
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from app.services.a_share_market_regime import MIN_BREADTH_UNIVERSE_SIZE
from app.services.public_market_breadth import (
    MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO,
    SINA_BREADTH_SOURCE,
)


CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
SNAPSHOT_COLLECTION = "candidate_market_snapshots"
PROVIDER_HEALTH_COLLECTION = "candidate_data_source_health"
SNAPSHOT_ID_PREFIX = "public_full_market"
PROVIDER_HEALTH_ID = "public_full_market_snapshot"
MAX_FETCH_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.25
PROVIDER_FAILURE_COOLDOWN_SECONDS = 60
LIVE_CACHE_MAX_AGE_SECONDS = 30 * 60
MIDDAY_CACHE_MAX_AGE_SECONDS = 2 * 60 * 60
OFF_HOURS_CACHE_MAX_AGE_SECONDS = 18 * 60 * 60
_EXCHANGES = ("sh", "sz", "bj")
_CODE_PATTERN = re.compile(r"^[0-9]{6}$")


def _exchange_for_code(code: str) -> Optional[str]:
    if re.fullmatch(r"6[0-9]{5}", code):
        return "sh"
    if re.fullmatch(r"[03][0-9]{5}", code):
        return "sz"
    if re.fullmatch(r"(?:43|83|87|88|92)[0-9]{4}", code):
        return "bj"
    return None


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9]{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _local_now(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TIMEZONE)
    return value.astimezone(CN_TIMEZONE)


def _cache_max_age_seconds(now: datetime) -> int:
    local = _local_now(now)
    if (
        local.weekday() < 5
        and clock_time(11, 30) < local.time() < clock_time(13, 0)
    ):
        return MIDDAY_CACHE_MAX_AGE_SECONDS
    is_live = local.weekday() < 5 and (
        clock_time(9, 15) <= local.time() <= clock_time(11, 30)
        or clock_time(13, 0) <= local.time() <= clock_time(15, 10)
    )
    return LIVE_CACHE_MAX_AGE_SECONDS if is_live else OFF_HOURS_CACHE_MAX_AGE_SECONDS


def _provider_error(
    *,
    status: Any,
    error_type: Any,
    checked_at: datetime,
    source: Any = None,
) -> Dict[str, Any]:
    return {
        "provider": str(source or SINA_BREADTH_SOURCE),
        "status": str(status or "public_snapshot_unavailable"),
        "error_type": str(error_type or "ProviderUnavailable"),
        "checked_at": checked_at.isoformat(),
    }


def _snapshot_is_complete(
    payload: Mapping[str, Any],
    *,
    benchmark_trade_date: str,
) -> bool:
    rows = payload.get("rows")
    expected = payload.get("provider_expected_count")
    expected_counts = payload.get("provider_expected_exchange_counts")
    return bool(
        payload.get("status") == "ok"
        and payload.get("benchmark_trade_date") == benchmark_trade_date
        and payload.get("provider_trade_date") == benchmark_trade_date
        and isinstance(rows, list)
        and len(rows) >= MIN_BREADTH_UNIVERSE_SIZE
        and isinstance(expected, int)
        and expected > 0
        and isinstance(expected_counts, Mapping)
        and all(
            isinstance(expected_counts.get(exchange), int)
            and expected_counts.get(exchange) > 0
            for exchange in _EXCHANGES
        )
    )


class PublicMarketSnapshotResilience:
    """Fetch a complete snapshot or fail closed with explicit provider evidence."""

    def __init__(
        self,
        *,
        db_factory: Optional[Callable[[], Any]] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db_factory = db_factory
        self._sleeper = sleeper

    def _db(self) -> Any:
        if self._db_factory is not None:
            return self._db_factory()
        from app.core.database import get_mongo_db_sync

        return get_mongo_db_sync()

    @staticmethod
    def _snapshot_id(benchmark_trade_date: str) -> str:
        return f"{SNAPSHOT_ID_PREFIX}:{benchmark_trade_date}"

    def _load_health(self) -> Optional[Dict[str, Any]]:
        try:
            row = self._db()[PROVIDER_HEALTH_COLLECTION].find_one(
                {"_id": PROVIDER_HEALTH_ID}
            )
        except Exception:
            return None
        return dict(row) if isinstance(row, Mapping) else None

    def _save_health(
        self,
        *,
        status: str,
        checked_at: datetime,
        provider_errors: list[Dict[str, Any]],
    ) -> None:
        document = {
            "status": status,
            "checked_at": checked_at,
            "provider_errors": deepcopy(provider_errors),
        }
        try:
            self._db()[PROVIDER_HEALTH_COLLECTION].update_one(
                {"_id": PROVIDER_HEALTH_ID},
                {"$set": document},
                upsert=True,
            )
        except Exception:
            return

    def _provider_in_cooldown(self, *, now: datetime) -> tuple[bool, list[Dict[str, Any]]]:
        health = self._load_health()
        if not health or health.get("status") == "ok":
            return False, []
        checked_at = _datetime(health.get("checked_at"))
        if checked_at is None:
            return False, []
        age = (now.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()
        errors = health.get("provider_errors")
        normalized_errors = (
            [dict(item) for item in errors if isinstance(item, Mapping)]
            if isinstance(errors, list)
            else []
        )
        return 0 <= age <= PROVIDER_FAILURE_COOLDOWN_SECONDS, normalized_errors

    def _save_complete_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        benchmark_trade_date: str,
        checked_at: datetime,
    ) -> None:
        document = {
            "_id": self._snapshot_id(benchmark_trade_date),
            "benchmark_trade_date": benchmark_trade_date,
            "checked_at": checked_at,
            "source": str(payload.get("source") or SINA_BREADTH_SOURCE),
            "payload": deepcopy(dict(payload)),
        }
        try:
            self._db()[SNAPSHOT_COLLECTION].replace_one(
                {"_id": document["_id"]},
                document,
                upsert=True,
            )
        except Exception:
            return

    def _load_complete_snapshot(
        self,
        *,
        benchmark_trade_date: str,
        now: datetime,
        provider_errors: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            row = self._db()[SNAPSHOT_COLLECTION].find_one(
                {"_id": self._snapshot_id(benchmark_trade_date)}
            )
        except Exception:
            return None
        if not isinstance(row, Mapping):
            return None
        checked_at = _datetime(row.get("checked_at"))
        payload = row.get("payload")
        if checked_at is None or not isinstance(payload, Mapping):
            return None
        age_seconds = (
            now.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
        ).total_seconds()
        if (
            age_seconds < 0
            or age_seconds > _cache_max_age_seconds(now)
            or not _snapshot_is_complete(
                payload,
                benchmark_trade_date=benchmark_trade_date,
            )
        ):
            return None
        result = deepcopy(dict(payload))
        result.update(
            {
                "source": "mongo.candidate_market_snapshots",
                "original_source": row.get("source"),
                "checked_at": checked_at.isoformat(),
                "freshness": "cached_fresh",
                "degraded": True,
                "cache_age_seconds": round(age_seconds, 3),
                "provider_errors": deepcopy(provider_errors),
            }
        )
        return result

    def _load_market_quotes_snapshot(
        self,
        *,
        benchmark_trade_date: str,
        now: datetime,
        provider_errors: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        db = self._db()
        compact_date = benchmark_trade_date.replace("-", "")
        projection = {
            "_id": 0,
            "code": 1,
            "name": 1,
            "close": 1,
            "pct_chg": 1,
            "amount": 1,
            "trade_date": 1,
            "updated_at": 1,
        }
        try:
            raw_rows = list(
                db["market_quotes"].find(
                    {"trade_date": {"$in": [benchmark_trade_date, compact_date]}},
                    projection,
                )
            )
            basic_rows = list(
                db["stock_basic_info"].find(
                    {},
                    {"_id": 0, "code": 1, "name": 1},
                )
            )
        except Exception:
            return None

        names: Dict[str, str] = {}
        expected_codes = set()
        expected_counts = {exchange: 0 for exchange in _EXCHANGES}
        for raw in basic_rows:
            code = str(raw.get("code") or "").strip()
            exchange = _exchange_for_code(code)
            if not exchange or code in expected_codes:
                continue
            expected_codes.add(code)
            expected_counts[exchange] += 1
            name = str(raw.get("name") or "").strip()
            if name:
                names[code] = name
        if (
            len(expected_codes) < MIN_BREADTH_UNIVERSE_SIZE
            or any(expected_counts[exchange] <= 0 for exchange in _EXCHANGES)
        ):
            return None

        rows: list[Dict[str, Any]] = []
        seen = set()
        exchange_counts = {exchange: 0 for exchange in _EXCHANGES}
        latest_updated_at: Optional[datetime] = None
        for raw in raw_rows:
            code = str(raw.get("code") or "").strip()
            exchange = _exchange_for_code(code)
            close = _finite(raw.get("close"))
            pct_chg = _finite(raw.get("pct_chg"))
            amount = _finite(raw.get("amount"))
            if (
                not exchange
                or code in seen
                or close is None
                or close <= 0
                or pct_chg is None
                or amount is None
                or amount <= 0
            ):
                continue
            seen.add(code)
            exchange_counts[exchange] += 1
            updated_at = _datetime(raw.get("updated_at"))
            if updated_at is not None and (
                latest_updated_at is None or updated_at > latest_updated_at
            ):
                latest_updated_at = updated_at
            rows.append(
                {
                    "code": code,
                    "name": str(raw.get("name") or names.get(code) or code),
                    "exchange": exchange,
                    "close": close,
                    "pct_chg": pct_chg,
                    "amount": amount,
                    "trade_date": benchmark_trade_date,
                }
            )
        expected_total = len(expected_codes)
        total_ratio = len(rows) / expected_total
        exchange_ratios = {
            exchange: exchange_counts[exchange] / expected_counts[exchange]
            for exchange in _EXCHANGES
        }
        if (
            len(rows) < MIN_BREADTH_UNIVERSE_SIZE
            or total_ratio < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
            or any(
                exchange_ratios[exchange] < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
                for exchange in _EXCHANGES
            )
            or latest_updated_at is None
        ):
            return None
        age_seconds = (
            now.astimezone(timezone.utc)
            - latest_updated_at.astimezone(timezone.utc)
        ).total_seconds()
        if age_seconds < 0 or age_seconds > _cache_max_age_seconds(now):
            return None
        return {
            "status": "ok",
            "source": "mongo.market_quotes",
            "benchmark_trade_date": benchmark_trade_date,
            "provider_trade_date": benchmark_trade_date,
            "provider_time": None,
            "provider_expected_count": expected_total,
            "provider_expected_exchange_counts": expected_counts,
            "raw_row_count": len(raw_rows),
            "unique_row_count": len(rows),
            "universe_count": len(rows),
            "universe_size": len(rows),
            "exchange_counts": exchange_counts,
            "total_coverage_ratio": total_ratio,
            "exchange_coverage_ratio": exchange_ratios,
            "rows": rows,
            "checked_at": latest_updated_at.isoformat(),
            "freshness": "cached_fresh",
            "degraded": True,
            "cache_age_seconds": round(age_seconds, 3),
            "provider_errors": deepcopy(provider_errors),
        }

    def _fallback(
        self,
        *,
        benchmark_trade_date: str,
        now: datetime,
        provider_errors: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        cached = self._load_complete_snapshot(
            benchmark_trade_date=benchmark_trade_date,
            now=now,
            provider_errors=provider_errors,
        )
        if cached is not None:
            return cached
        try:
            return self._load_market_quotes_snapshot(
                benchmark_trade_date=benchmark_trade_date,
                now=now,
                provider_errors=provider_errors,
            )
        except Exception:
            return None

    def fetch(
        self,
        *,
        fetcher: Callable[..., Dict[str, Any]],
        benchmark_trade_date: Optional[str],
        timeout_seconds: float,
        now: datetime,
        remaining_seconds: Callable[[], float],
    ) -> Dict[str, Any]:
        benchmark = _date_text(benchmark_trade_date)
        checked_at = now.astimezone(timezone.utc)
        if benchmark is None:
            return {
                "status": "public_snapshot_unavailable",
                "source": SINA_BREADTH_SOURCE,
                "benchmark_trade_date": None,
                "checked_at": checked_at.isoformat(),
                "freshness": "unavailable",
                "degraded": True,
                "provider_errors": [
                    _provider_error(
                        status="invalid_benchmark_trade_date",
                        error_type="InvalidBenchmarkTradeDate",
                        checked_at=checked_at,
                    )
                ],
                "rows": [],
            }

        in_cooldown, provider_errors = self._provider_in_cooldown(now=checked_at)
        if in_cooldown:
            fallback = self._fallback(
                benchmark_trade_date=benchmark,
                now=checked_at,
                provider_errors=provider_errors,
            )
            if fallback is not None:
                fallback["provider_health"] = "cooldown"
                return fallback
            return {
                "status": "public_snapshot_unavailable",
                "source": SINA_BREADTH_SOURCE,
                "benchmark_trade_date": benchmark,
                "checked_at": checked_at.isoformat(),
                "freshness": "unavailable",
                "degraded": True,
                "provider_health": "cooldown",
                "provider_errors": provider_errors,
                "rows": [],
            }

        attempts = 0
        last_result: Dict[str, Any] = {}
        provider_errors = []
        while attempts < MAX_FETCH_ATTEMPTS:
            available = min(float(timeout_seconds), float(remaining_seconds()))
            attempts_left = MAX_FETCH_ATTEMPTS - attempts
            attempt_timeout = available / attempts_left if attempts_left > 1 else available
            if attempt_timeout <= 0:
                break
            attempts += 1
            try:
                raw = fetcher(
                    benchmark_trade_date=benchmark,
                    timeout_seconds=attempt_timeout,
                    now=now,
                )
                last_result = dict(raw) if isinstance(raw, Mapping) else {
                    "status": "public_breadth_fetch_failed",
                    "error_type": "InvalidFetcherPayload",
                    "rows": [],
                }
            except Exception as exc:
                last_result = {
                    "status": "public_breadth_fetch_failed",
                    "source": SINA_BREADTH_SOURCE,
                    "error_type": type(exc).__name__,
                    "rows": [],
                }
            attempt_checked_at = checked_at
            if _snapshot_is_complete(
                last_result,
                benchmark_trade_date=benchmark,
            ):
                result = deepcopy(last_result)
                result.update(
                    {
                        "checked_at": attempt_checked_at.isoformat(),
                        "freshness": "fresh",
                        "degraded": attempts > 1,
                        "attempt_count": attempts,
                        "provider_errors": deepcopy(provider_errors),
                    }
                )
                self._save_complete_snapshot(
                    result,
                    benchmark_trade_date=benchmark,
                    checked_at=attempt_checked_at,
                )
                self._save_health(
                    status="ok",
                    checked_at=attempt_checked_at,
                    provider_errors=provider_errors,
                )
                return result
            provider_errors.append(
                _provider_error(
                    status=last_result.get("status"),
                    error_type=last_result.get("error_type"),
                    checked_at=attempt_checked_at,
                    source=last_result.get("source"),
                )
            )
            if attempts < MAX_FETCH_ATTEMPTS and remaining_seconds() > RETRY_BACKOFF_SECONDS:
                self._sleeper(RETRY_BACKOFF_SECONDS)

        final_checked_at = checked_at
        self._save_health(
            status="unavailable",
            checked_at=final_checked_at,
            provider_errors=provider_errors,
        )
        fallback = self._fallback(
            benchmark_trade_date=benchmark,
            now=final_checked_at,
            provider_errors=provider_errors,
        )
        if fallback is not None:
            fallback["attempt_count"] = attempts
            return fallback
        return {
            "status": "public_snapshot_unavailable",
            "source": str(last_result.get("source") or SINA_BREADTH_SOURCE),
            "benchmark_trade_date": benchmark,
            "checked_at": final_checked_at.isoformat(),
            "freshness": "unavailable",
            "degraded": True,
            "attempt_count": attempts,
            "provider_errors": provider_errors,
            "rows": [],
        }
