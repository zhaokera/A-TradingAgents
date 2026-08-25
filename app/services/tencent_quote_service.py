"""Tencent realtime quote service for A-share prices."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

TENCENT_REALTIME_ENDPOINT = "qt.gtimg.cn/q"
TENCENT_REALTIME_URL = f"https://{TENCENT_REALTIME_ENDPOINT}"
TENCENT_HEADERS = {
    "Referer": "https://finance.qq.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
QUOTE_MAX_AGE_SECONDS = 300
QUOTE_MAX_FUTURE_SKEW_SECONDS = 60
TENCENT_QUOTE_BATCH_SIZE = 40
MAX_TENCENT_BATCHED_CODES = 160
TENCENT_HISTORY_CACHE_COLLECTION = "candidate_technical_history_cache"
TENCENT_HISTORY_FETCH_ATTEMPTS = 2
TENCENT_HISTORY_RETRY_SECONDS = 0.25
TENCENT_HISTORY_CACHE_MAX_AGE_SECONDS = 72 * 60 * 60
TENCENT_HISTORY_MAX_BAR_LAG_DAYS = 7
_TENCENT_ASSIGNMENT_PATTERN = re.compile(
    r'(?m)(?:^|;)[ \t]*v_(?P<provider_symbol>(?:sh|sz|bj)[0-9]{6})'
    r'[ \t]*=[ \t]*"(?P<payload>[^"\r\n]*)"[ \t]*(?=;|$)',
    flags=re.IGNORECASE,
)
_TENCENT_MAJOR_INDEX_SYMBOLS = frozenset(
    {"sh000001", "sh000300", "sz399001", "sz399006", "sh000688"}
)


class TencentQuoteInputError(ValueError):
    """Raised when a batch quote request cannot be materialized."""


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text == "-":
                return None
            if text.endswith("%"):
                text = text[:-1]
            number = float(text)
        else:
            number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    number = _safe_float(value)
    if number is None:
        return None
    return int(number)


def normalize_cn_code(code: str) -> str:
    text = str(code or "").strip().lower()
    if text.endswith((".sh", ".sz", ".bj")):
        text = text[:-3]
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text.upper()


def _is_bj_code(code: str) -> bool:
    return code.startswith(("43", "83", "87", "88", "92"))


def to_tencent_symbol(code: str) -> str:
    raw = str(code or "").strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if raw.startswith(prefix):
            return f"{prefix}{normalize_cn_code(raw)}"
    for suffix, prefix in ((".sh", "sh"), (".sz", "sz"), (".bj", "bj")):
        if raw.endswith(suffix):
            return f"{prefix}{normalize_cn_code(raw)}"

    normalized = normalize_cn_code(code)
    if _is_bj_code(normalized):
        return f"bj{normalized}"
    if normalized.startswith(("6", "5", "90")):
        return f"sh{normalized}"
    return f"sz{normalized}"


def _parse_amount(fields: List[str]) -> Optional[float]:
    if len(fields) > 35 and fields[35]:
        parts = fields[35].split("/")
        if len(parts) >= 3:
            precise_amount = _safe_float(parts[2])
            if precise_amount is not None:
                return precise_amount

    amount_wan = _safe_float(fields[37]) if len(fields) > 37 and fields[37] else None
    return amount_wan * 10000 if amount_wan is not None else None


def _yi_to_yuan(value: Any) -> Optional[float]:
    number = _safe_float(value)
    return number * 100000000 if number is not None else None


def _normalize_volume(fields: List[str]) -> Optional[int]:
    if len(fields) <= 6 or not fields[6]:
        return None

    raw_volume = _safe_int(fields[6])
    if raw_volume is None:
        return None

    price = _safe_float(fields[3]) if len(fields) > 3 else None
    turnover_rate = _safe_float(fields[38]) if len(fields) > 38 else None
    circ_mv_yi = _safe_float(fields[44]) if len(fields) > 44 and fields[44] else None
    circ_mv = circ_mv_yi * 100000000 if circ_mv_yi is not None else None

    if price and price > 0 and turnover_rate and turnover_rate > 0 and circ_mv and circ_mv > 0:
        expected_volume = (circ_mv / price) * (turnover_rate / 100)
        if expected_volume > 0:
            hand_to_share_volume = raw_volume * 100
            raw_delta = abs(raw_volume - expected_volume)
            hand_delta = abs(hand_to_share_volume - expected_volume)
            return raw_volume if raw_delta <= hand_delta else hand_to_share_volume

    return raw_volume * 100


def _extract_trade_date(provider_timestamp: Optional[str]) -> Optional[str]:
    if not provider_timestamp:
        return None
    digits = "".join(ch for ch in str(provider_timestamp) if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _parse_provider_trade_at(provider_timestamp: Optional[str]) -> Optional[str]:
    if not provider_timestamp:
        return None
    digits = "".join(ch for ch in str(provider_timestamp) if ch.isdigit())
    if len(digits) < 14:
        return None
    try:
        value = datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=CN_MARKET_TIMEZONE)
    except ValueError:
        return None
    return value.isoformat(timespec="seconds")


def _as_market_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
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
        return parsed.replace(tzinfo=CN_MARKET_TIMEZONE)
    return parsed.astimezone(CN_MARKET_TIMEZONE)


def _cn_session(now: datetime) -> str:
    if now.weekday() >= 5:
        return "closed"
    current = now.time()
    if current >= datetime.strptime("09:30", "%H:%M").time() and current < datetime.strptime("11:30", "%H:%M").time():
        return "morning"
    if current >= datetime.strptime("13:00", "%H:%M").time() and current < datetime.strptime("15:00", "%H:%M").time():
        return "afternoon"
    if current >= datetime.strptime("11:30", "%H:%M").time() and current < datetime.strptime("13:00", "%H:%M").time():
        return "lunch_break"
    return "closed"


def assess_cn_quote_freshness(
    quote: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_age_seconds: int = QUOTE_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = QUOTE_MAX_FUTURE_SKEW_SECONDS,
) -> Dict[str, Any]:
    """Evaluate whether a quote can be used for an A-share sizing decision."""
    local_now = _as_market_datetime(now or datetime.now(CN_MARKET_TIMEZONE))
    assert local_now is not None
    source = str(quote.get("source") or quote.get("data_source") or "unknown").lower()
    provider_updated_dt = _as_market_datetime(
        quote.get("provider_updated_at") or quote.get("trade_at")
    )
    trade_dt = provider_updated_dt
    session = _cn_session(local_now)
    age_seconds_raw = (local_now - trade_dt).total_seconds() if trade_dt else None

    base = {
        "actionable": False,
        "status": "missing_trade_at",
        "reason": "行情缺少提供方快照更新时间，仅用于研究展示。",
        "source": source,
        "trade_at": trade_dt.isoformat(timespec="seconds") if trade_dt else None,
        "provider_updated_at": (
            provider_updated_dt.isoformat(timespec="seconds")
            if provider_updated_dt
            else None
        ),
        "quote_time_semantics": str(
            quote.get("quote_time_semantics")
            or "legacy_provider_time_unverified"
        ),
        "exchange_trade_time_verified": (
            quote.get("exchange_trade_time_verified") is True
        ),
        "trade_date": trade_dt.date().isoformat() if trade_dt else None,
        "age_seconds": int(age_seconds_raw) if age_seconds_raw is not None else None,
        "session": session,
    }
    if trade_dt is None:
        return base
    if source != "tencent":
        return {
            **base,
            "status": "display_only_source",
            "reason": "非腾讯提供方时间未纳入可执行行情门禁，仅用于研究展示。",
        }
    if session not in {"morning", "afternoon"}:
        return {
            **base,
            "status": "off_session",
            "reason": "当前不在A股连续交易时段，行情仅用于研究展示。",
        }
    if trade_dt.date() != local_now.date():
        return {
            **base,
            "status": "previous_trade_date",
            "reason": "腾讯行情不是当前交易日数据，禁止用于仓位计算。",
        }

    if age_seconds_raw is not None and age_seconds_raw < -max_future_skew_seconds:
        return {
            **base,
            "status": "future_trade_at",
            "reason": "腾讯提供方快照更新时间超出允许的时钟偏差，禁止用于仓位计算。",
        }
    if age_seconds_raw is not None and age_seconds_raw > max_age_seconds:
        return {
            **base,
            "status": "stale_trade_at",
            "reason": "腾讯行情超过五分钟时效，禁止用于仓位计算。",
        }
    return {
        **base,
        "actionable": True,
        "status": "fresh",
        "reason": "腾讯提供方快照更新时间在允许时效内。",
    }


def assess_tencent_research_quote_freshness(
    quote: Dict[str, Any],
    *,
    benchmark_trade_date: str,
    now: datetime,
    max_age_seconds: int = QUOTE_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = 120,
) -> Dict[str, Any]:
    """Evaluate Tencent quote completeness for public research coverage."""
    local_now = _as_market_datetime(now)
    provider_updated_dt = _as_market_datetime(
        quote.get("provider_updated_at") or quote.get("trade_at")
    )
    trade_dt = provider_updated_dt
    benchmark_dt = _as_market_datetime(benchmark_trade_date)
    source = str(quote.get("source") or "unknown").strip().lower()
    age_seconds_raw = (
        (local_now - trade_dt).total_seconds()
        if local_now is not None and trade_dt is not None
        else None
    )
    base = {
        "data_complete": False,
        "status": "missing_trade_at",
        "reason": "腾讯行情缺少可解析的提供方快照更新时间。",
        "source": source,
        "trade_at": trade_dt.isoformat(timespec="seconds") if trade_dt else None,
        "provider_updated_at": (
            provider_updated_dt.isoformat(timespec="seconds")
            if provider_updated_dt
            else None
        ),
        "quote_time_semantics": str(
            quote.get("quote_time_semantics")
            or "legacy_provider_time_unverified"
        ),
        "exchange_trade_time_verified": (
            quote.get("exchange_trade_time_verified") is True
        ),
        "trade_date": trade_dt.date().isoformat() if trade_dt else None,
        "benchmark_trade_date": benchmark_dt.date().isoformat() if benchmark_dt else None,
        "age_seconds": int(age_seconds_raw) if age_seconds_raw is not None else None,
        "session": _cn_session(local_now) if local_now else "unknown",
    }
    if local_now is None:
        return {
            **base,
            "status": "invalid_now",
            "reason": "当前时间无法解析，不能验证腾讯研究行情。",
        }
    if source != "tencent":
        return {
            **base,
            "status": "invalid_source",
            "reason": "公开研究时效门禁只接受腾讯行情。",
        }
    if trade_dt is None:
        return base
    if benchmark_dt is None:
        return {
            **base,
            "status": "invalid_benchmark_trade_date",
            "reason": "基准交易日无法解析，不能验证腾讯研究行情。",
        }

    benchmark_date = benchmark_dt.date()
    if benchmark_date > local_now.date():
        return {
            **base,
            "status": "future_benchmark_trade_date",
            "reason": "基准交易日晚于当前日期，不能验证腾讯研究行情。",
        }
    if trade_dt.date() != benchmark_date:
        return {
            **base,
            "status": "trade_date_mismatch",
            "reason": "腾讯行情交易日与基准交易日不一致。",
        }

    close_floor = datetime.combine(
        benchmark_date,
        datetime.strptime("14:55", "%H:%M").time(),
        tzinfo=CN_MARKET_TIMEZONE,
    )
    same_day_closed = (
        benchmark_date == local_now.date()
        and local_now.time() >= datetime.strptime("15:00", "%H:%M").time()
    )
    completed_trade_date = benchmark_date < local_now.date() or same_day_closed
    if completed_trade_date:
        if trade_dt < close_floor:
            return {
                **base,
                "status": "stale_trade_at",
                "reason": "腾讯收盘行情时间早于14:55，研究数据不完整。",
            }
        if age_seconds_raw is not None and age_seconds_raw < -max_future_skew_seconds:
            return {
                **base,
                "status": "future_trade_at",
                "reason": "腾讯提供方快照更新时间超出允许的时钟偏差。",
            }
        return {
            **base,
            "data_complete": True,
            "status": "fresh",
            "reason": "腾讯行情满足已完成交易日研究时效要求。",
        }

    if base["session"] == "lunch_break":
        morning_close_floor = datetime.combine(
            benchmark_date,
            datetime.strptime("11:25", "%H:%M").time(),
            tzinfo=CN_MARKET_TIMEZONE,
        )
        if age_seconds_raw is not None and age_seconds_raw < -max_future_skew_seconds:
            return {
                **base,
                "status": "future_trade_at",
                "reason": "腾讯提供方快照更新时间超出允许的时钟偏差。",
            }
        if trade_dt < morning_close_floor:
            return {
                **base,
                "status": "stale_trade_at",
                "reason": "腾讯午间研究快照早于上午收盘完整性阈值。",
            }
        return {
            **base,
            "data_complete": True,
            "status": "midday_snapshot",
            "reason": "腾讯午间快照覆盖上午交易，仅用于研究复核。",
        }

    if base["session"] not in {"morning", "afternoon"}:
        return {
            **base,
            "status": "off_session",
            "reason": "当前不在连续交易时段，且基准交易日尚未收盘。",
        }
    if age_seconds_raw is not None and age_seconds_raw < -max_future_skew_seconds:
        return {
            **base,
            "status": "future_trade_at",
            "reason": "腾讯提供方快照更新时间超出允许的时钟偏差。",
        }
    if age_seconds_raw is not None and age_seconds_raw > max_age_seconds:
        return {
            **base,
            "status": "stale_trade_at",
            "reason": "腾讯行情超过公开研究允许的五分钟时效。",
        }
    return {
        **base,
        "data_complete": True,
        "status": "fresh",
        "reason": "腾讯行情满足盘中研究时效要求。",
    }


TENCENT_RESEARCH_FRESHNESS_REJECTION_STATUSES = frozenset(
    {
        "future_benchmark_trade_date",
        "future_trade_at",
        "invalid_benchmark_trade_date",
        "invalid_now",
        "invalid_source",
        "missing_trade_at",
        "off_session",
        "stale_trade_at",
        "trade_date_mismatch",
    }
)


def _normalize_bar_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 8:
            return None
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None


def normalize_tencent_daily_bars(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize Tencent/AKShare daily rows and keep the last duplicate date."""
    aliases = {
        "date": ("date", "日期"),
        "open": ("open", "开盘"),
        "close": ("close", "收盘"),
        "high": ("high", "最高"),
        "low": ("low", "最低"),
        "volume": ("volume", "成交量"),
        "amount": ("amount", "成交额"),
    }
    by_date: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue

        def pick(key: str) -> Any:
            return next((raw.get(name) for name in aliases[key] if raw.get(name) not in (None, "")), None)

        bar_date = _normalize_bar_date(pick("date"))
        open_price = _safe_float(pick("open"))
        close_price = _safe_float(pick("close"))
        high_price = _safe_float(pick("high"))
        low_price = _safe_float(pick("low"))
        if (
            not bar_date
            or open_price is None
            or close_price is None
            or high_price is None
            or low_price is None
            or min(open_price, close_price, high_price, low_price) <= 0
            or high_price < low_price
            or not low_price <= open_price <= high_price
            or not low_price <= close_price <= high_price
        ):
            continue
        bar = {
            "date": bar_date,
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
        }
        volume = _safe_float(pick("volume"))
        amount = _safe_float(pick("amount"))
        if volume is not None:
            bar["volume"] = volume
        if amount is not None:
            bar["amount"] = amount
        by_date[bar_date] = bar
    return [by_date[key] for key in sorted(by_date)]


def fetch_tencent_daily_bars_sync(
    code: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_rows: int = 60,
    prefer_cache: bool = False,
    now: Optional[datetime] = None,
    db_factory: Optional[Callable[[], Any]] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Fetch qfq daily bars from AKShare's Tencent history adapter."""
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    local_today = checked_at.astimezone(CN_MARKET_TIMEZONE).date()
    start = (start_date or (local_today - timedelta(days=120)).strftime("%Y%m%d")).replace("-", "")
    end = (end_date or local_today.strftime("%Y%m%d")).replace("-", "")
    symbol = to_tencent_symbol(code)
    normalized_code = normalize_cn_code(code)

    def get_db() -> Any:
        if db_factory is not None:
            return db_factory()
        from app.core.database import get_mongo_db_sync

        return get_mongo_db_sync()

    def cache_error(status: str, reason: str) -> Dict[str, Any]:
        return {
            "provider": "tencent",
            "status": status,
            "error_type": reason,
            "checked_at": checked_at.isoformat(),
        }

    def load_cache(
        provider_error: Optional[Dict[str, Any]],
        *,
        cache_usage: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            row = get_db()[TENCENT_HISTORY_CACHE_COLLECTION].find_one(
                {"_id": f"{normalized_code}:qfq"}
            )
        except Exception:
            return None
        if not isinstance(row, Mapping):
            return None
        cached_at = row.get("checked_at")
        if isinstance(cached_at, str):
            text = cached_at[:-1] + "+00:00" if cached_at.endswith("Z") else cached_at
            try:
                cached_at = datetime.fromisoformat(text)
            except ValueError:
                return None
        if not isinstance(cached_at, datetime):
            return None
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            checked_at - cached_at.astimezone(timezone.utc)
        ).total_seconds()
        cached_bars = normalize_tencent_daily_bars(row.get("bars") or [])
        if (
            age_seconds < 0
            or age_seconds > TENCENT_HISTORY_CACHE_MAX_AGE_SECONDS
            or len(cached_bars) < min_rows
        ):
            return None
        try:
            requested_end = datetime.strptime(end, "%Y%m%d").date()
            latest_bar = date.fromisoformat(cached_bars[-1]["date"])
        except (TypeError, ValueError):
            return None
        bar_lag_days = (requested_end - latest_bar).days
        if bar_lag_days < 0 or bar_lag_days > TENCENT_HISTORY_MAX_BAR_LAG_DAYS:
            return None
        return {
            "ok": True,
            "status": "ok",
            "code": normalized_code,
            "symbol": symbol,
            "source": f"mongo.{TENCENT_HISTORY_CACHE_COLLECTION}",
            "original_source": row.get("source") or "tencent",
            "adjust": "qfq",
            "start_date": start,
            "end_date": end,
            "required_rows": min_rows,
            "available_rows": len(cached_bars),
            "bars": cached_bars,
            "checked_at": cached_at.astimezone(timezone.utc).isoformat(),
            "freshness": "cached_fresh",
            "degraded": True,
            "cache_usage": cache_usage,
            "cache_age_seconds": round(age_seconds, 3),
            "provider_errors": [provider_error] if provider_error else [],
        }

    if prefer_cache:
        cached = load_cache(None, cache_usage="preferred")
        if cached is not None:
            return cached

    bars: List[Dict[str, Any]] = []
    fetch_error: Optional[Exception] = None
    for attempt in range(TENCENT_HISTORY_FETCH_ATTEMPTS):
        try:
            import akshare as ak

            frame = ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            raw_rows = (
                []
                if frame is None or getattr(frame, "empty", False)
                else frame.to_dict("records")
            )
            bars = normalize_tencent_daily_bars(raw_rows)
            fetch_error = None
            break
        except Exception as exc:
            fetch_error = exc
            logger.info(
                "Tencent daily bars failed: code=%s symbol=%s attempt=%s error=%s",
                code,
                symbol,
                attempt + 1,
                exc,
            )
            if attempt + 1 < TENCENT_HISTORY_FETCH_ATTEMPTS:
                sleeper(TENCENT_HISTORY_RETRY_SECONDS)

    if fetch_error is not None:
        provider_error = cache_error("fetch_error", type(fetch_error).__name__)
        cached = load_cache(provider_error, cache_usage="fallback")
        if cached is not None:
            return cached
        return {
            "ok": False,
            "status": "fetch_error",
            "reason": str(fetch_error),
            "code": normalized_code,
            "symbol": symbol,
            "source": "tencent",
            "adjust": "qfq",
            "bars": [],
            "checked_at": checked_at.isoformat(),
            "freshness": "unavailable",
            "degraded": False,
            "provider_errors": [provider_error],
        }

    payload = {
        "ok": len(bars) >= min_rows,
        "status": "ok" if len(bars) >= min_rows else "insufficient_history",
        "code": normalized_code,
        "symbol": symbol,
        "source": "tencent",
        "adjust": "qfq",
        "start_date": start,
        "end_date": end,
        "required_rows": min_rows,
        "available_rows": len(bars),
        "bars": bars,
        "checked_at": checked_at.isoformat(),
        "freshness": "live",
        "degraded": False,
        "provider_errors": [],
    }
    if not payload["ok"]:
        payload["reason"] = f"腾讯前复权日线不足 {min_rows} 条。"
        provider_error = cache_error("insufficient_history", "InsufficientHistory")
        cached = load_cache(provider_error, cache_usage="fallback")
        if cached is not None:
            return cached
        payload["provider_errors"] = [provider_error]
        return payload
    try:
        get_db()[TENCENT_HISTORY_CACHE_COLLECTION].replace_one(
            {"_id": f"{normalized_code}:qfq"},
            {
                "_id": f"{normalized_code}:qfq",
                "code": normalized_code,
                "symbol": symbol,
                "source": "tencent",
                "adjust": "qfq",
                "start_date": start,
                "end_date": end,
                "bars": deepcopy(bars),
                "checked_at": checked_at,
            },
            upsert=True,
        )
    except Exception:
        pass
    return payload


def merge_tencent_quote_into_bars(
    bars: Iterable[Dict[str, Any]],
    quote: Dict[str, Any],
    *,
    max_scale_difference: float = 0.25,
) -> Dict[str, Any]:
    normalized = normalize_tencent_daily_bars(bars)
    quote_date = _normalize_bar_date(quote.get("trade_date"))
    quote_close = _safe_float(quote.get("close") or quote.get("price") or quote.get("current_price"))
    if not normalized or not quote_date or quote_close is None or quote_close <= 0:
        return {
            "ok": False,
            "status": "invalid_merge_input",
            "bars": normalized,
        }

    last = normalized[-1]
    last_close = float(last["close"])
    price_ratio = quote_close / last_close if last_close > 0 else 0
    if abs(price_ratio - 1) > max_scale_difference:
        return {
            "ok": False,
            "status": "price_scale_mismatch",
            "price_ratio": round(price_ratio, 4),
            "bars": normalized,
        }
    if quote_date < last["date"]:
        return {
            "ok": False,
            "status": "out_of_order_quote",
            "bars": normalized,
        }

    base = last if quote_date == last["date"] else {}
    quote_bar = {
        "date": quote_date,
        "open": _safe_float(quote.get("open")) or base.get("open") or quote_close,
        "close": quote_close,
        "high": _safe_float(quote.get("high")) or base.get("high") or quote_close,
        "low": _safe_float(quote.get("low")) or base.get("low") or quote_close,
    }
    volume = _safe_float(quote.get("volume"))
    amount = _safe_float(quote.get("amount"))
    if volume is not None:
        quote_bar["volume"] = volume
    elif base.get("volume") is not None:
        quote_bar["volume"] = base["volume"]
    if amount is not None:
        quote_bar["amount"] = amount
    elif base.get("amount") is not None:
        quote_bar["amount"] = base["amount"]

    merged = normalized[:-1] + [quote_bar] if quote_date == last["date"] else normalized + [quote_bar]
    return {
        "ok": True,
        "status": "ok",
        "merge_action": "replace" if quote_date == last["date"] else "append",
        "price_ratio": round(price_ratio, 4),
        "bars": merged,
    }


def parse_tencent_quote_payload(code: str, payload: str) -> Optional[Dict[str, Any]]:
    content = (payload or "").strip()
    if not content or '=""' in content:
        return None

    data_start = content.find('"')
    data_end = content.rfind('"')
    if data_start == -1 or data_end <= data_start:
        return None

    fields = content[data_start + 1:data_end].split("~")
    if len(fields) < 45:
        return None

    normalized_code = normalize_cn_code(fields[2] or code)
    price = _safe_float(fields[3])
    if price is None or price <= 0:
        return None

    provider_timestamp = fields[30] if len(fields) > 30 and fields[30] else None
    provider_updated_at = _parse_provider_trade_at(provider_timestamp)
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    quote = {
        "code": normalized_code,
        "symbol": normalized_code,
        "name": fields[1] if len(fields) > 1 else "",
        "source": "tencent",
        "data_source": "tencent",
        "close": price,
        "price": price,
        "current_price": price,
        "pct_chg": _safe_float(fields[32]) if len(fields) > 32 else None,
        "change": _safe_float(fields[31]) if len(fields) > 31 else None,
        "volume": _normalize_volume(fields),
        "amount": _parse_amount(fields),
        "open": _safe_float(fields[5]) if len(fields) > 5 else None,
        "high": _safe_float(fields[33]) if len(fields) > 33 else None,
        "low": _safe_float(fields[34]) if len(fields) > 34 else None,
        "pre_close": _safe_float(fields[4]) if len(fields) > 4 else None,
        "turnover_rate": _safe_float(fields[38]) if len(fields) > 38 else None,
        "amplitude": _safe_float(fields[43]) if len(fields) > 43 else None,
        "limit_up": _safe_float(fields[47]) if len(fields) > 47 else None,
        "limit_down": _safe_float(fields[48]) if len(fields) > 48 else None,
        "volume_ratio": _safe_float(fields[49]) if len(fields) > 49 else None,
        "pe_ratio": _safe_float(fields[39]) if len(fields) > 39 else None,
        "pb_ratio": _safe_float(fields[46]) if len(fields) > 46 else None,
        "circ_mv": _yi_to_yuan(fields[44]) if len(fields) > 44 and fields[44] else None,
        "total_mv": _yi_to_yuan(fields[45]) if len(fields) > 45 and fields[45] else None,
        "provider_timestamp": provider_timestamp,
        "provider_updated_at": provider_updated_at,
        "quote_time_semantics": "provider_snapshot_updated_at",
        "exchange_trade_time_verified": False,
        "trade_at_compatibility_alias": True,
        "trade_at": provider_updated_at,
        "trade_date": _extract_trade_date(provider_timestamp),
        "received_at": received_at,
        "updated_at": received_at,
    }
    return {key: value for key, value in quote.items() if value is not None}


def _parse_tencent_assignment(assignment: re.Match[str]) -> Dict[str, Any]:
    provider_symbol = assignment.group("provider_symbol").lower()
    envelope_code = provider_symbol[2:]
    payload = assignment.group("payload")
    fields = payload.split("~") if payload else []
    payload_code = (
        fields[2].strip()
        if len(fields) > 2 and fields[2].strip()
        else None
    )
    identity = {
        "provider_symbol": provider_symbol,
        "envelope_code": envelope_code,
        "payload_code": payload_code,
        "source": "tencent",
    }

    if not payload:
        parse_status = "empty_payload"
    elif len(fields) < 45 or payload_code is None:
        parse_status = "malformed_payload"
    else:
        price = _safe_float(fields[3])
        parse_status = (
            "invalid_price"
            if price is None or price <= 0
            else "ok"
        )

    if parse_status == "ok":
        quote = parse_tencent_quote_payload(envelope_code, assignment.group(0))
        if quote is not None:
            quote.update(identity)
            quote["parse_status"] = "ok"
            return quote
        parse_status = "malformed_payload"

    row: Dict[str, Any] = {
        "code": envelope_code,
        **identity,
        "parse_status": parse_status,
        "close": None,
    }
    amount = _parse_amount(fields)
    if amount is not None:
        row["amount"] = amount
    return row


def parse_tencent_quote_batch_payload(payload: str) -> List[Dict[str, Any]]:
    """Parse Tencent assignments in response order without deduplication."""
    content = payload or ""
    return [
        _parse_tencent_assignment(assignment)
        for assignment in _TENCENT_ASSIGNMENT_PATTERN.finditer(content)
    ]


def _normalize_tencent_request_code(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized_value = value.strip().lower()
    if normalized_value in _TENCENT_MAJOR_INDEX_SYMBOLS:
        return normalized_value
    match = re.fullmatch(
        r"(?:(sh|sz|bj))?([0-9]{6})(?:\.(sh|sz|bj))?",
        normalized_value,
    )
    if match is None:
        return None

    prefix, code, suffix = match.groups()
    if prefix and suffix:
        return None
    if code.startswith("6"):
        exchange = "sh"
    elif code.startswith(("0", "3")):
        exchange = "sz"
    elif code.startswith(("43", "83", "87", "88", "92")):
        exchange = "bj"
    else:
        return None
    explicit_exchange = prefix or suffix
    provider_symbol = f"{explicit_exchange or exchange}{code}"
    if provider_symbol in _TENCENT_MAJOR_INDEX_SYMBOLS:
        return provider_symbol
    return code if explicit_exchange in (None, exchange) else None


def _collect_tencent_request_codes(
    codes: Iterable[str],
    *,
    max_codes: int = TENCENT_QUOTE_BATCH_SIZE,
) -> List[str]:
    if codes is None or isinstance(codes, (str, bytes, bytearray)):
        raise TencentQuoteInputError("invalid_codes")
    try:
        values = list(codes)
    except Exception as exc:
        raise TencentQuoteInputError("invalid_codes") from exc

    requested_codes: List[str] = []
    seen = set()
    for value in values:
        normalized = _normalize_tencent_request_code(value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        requested_codes.append(normalized)
        if len(requested_codes) == max_codes:
            break
    return requested_codes


def fetch_tencent_quotes_sync(
    codes: Iterable[str],
    *,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Fetch up to 40 unique A-share quotes in one Tencent request."""
    try:
        requested_codes = _collect_tencent_request_codes(codes)
    except TencentQuoteInputError:
        return {
            "status": "invalid_request",
            "requested_codes": [],
            "rows": [],
            "error_type": "invalid_codes",
        }

    base = {
        "requested_codes": requested_codes,
        "rows": [],
        "error_type": None,
    }
    if not requested_codes:
        return {"status": "empty", **base}

    symbols = [to_tencent_symbol(code) for code in requested_codes]
    url = f"{TENCENT_REALTIME_URL}={','.join(symbols)}"
    try:
        response = requests.get(url, headers=TENCENT_HEADERS, timeout=timeout)
        response.encoding = "gbk"
    except requests.Timeout as exc:
        logger.info("Tencent batch quote timed out: codes=%s error=%s", requested_codes, exc)
        return {
            "status": "fetch_error",
            **base,
            "error_type": "request_timeout",
        }
    except requests.RequestException as exc:
        logger.info("Tencent batch quote failed: codes=%s error=%s", requested_codes, exc)
        return {
            "status": "fetch_error",
            **base,
            "error_type": "request_failed",
        }
    except Exception:
        logger.exception("Tencent batch quote request failed internally: codes=%s", requested_codes)
        return {
            "status": "internal_error",
            **base,
            "error_type": "request_error",
        }

    if response.status_code != 200:
        logger.info(
            "Tencent batch quote failed: codes=%s status=%s",
            requested_codes,
            response.status_code,
        )
        return {
            "status": "fetch_error",
            **base,
            "error_type": "HTTPError",
            "http_status": response.status_code,
        }
    try:
        rows = parse_tencent_quote_batch_payload(response.text)
    except Exception:
        logger.exception("Tencent batch quote parser failed: codes=%s", requested_codes)
        return {
            "status": "internal_error",
            **base,
            "error_type": "parser_error",
        }
    return {
        "status": "ok",
        **base,
        "rows": rows,
    }


def fetch_tencent_quotes_batched_sync(
    codes: Iterable[str],
    *,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Fetch up to 160 quotes in ordered 40-code batches under one deadline."""
    try:
        requested_codes = _collect_tencent_request_codes(
            codes,
            max_codes=MAX_TENCENT_BATCHED_CODES,
        )
    except TencentQuoteInputError:
        return {
            "status": "invalid_request",
            "requested_codes": [],
            "rows": [],
            "error_type": "invalid_codes",
            "batch_count": 0,
            "completed_batch_count": 0,
        }

    batch_count = math.ceil(len(requested_codes) / TENCENT_QUOTE_BATCH_SIZE)
    base = {
        "requested_codes": requested_codes,
        "rows": [],
        "error_type": None,
        "batch_count": batch_count,
        "completed_batch_count": 0,
    }
    if not requested_codes:
        return {"status": "empty", **base}

    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout_seconds = 0.0
    if not math.isfinite(timeout_seconds):
        timeout_seconds = 0.0
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    rows: List[Dict[str, Any]] = []

    for batch_index, offset in enumerate(
        range(0, len(requested_codes), TENCENT_QUOTE_BATCH_SIZE)
    ):
        batch = requested_codes[offset:offset + TENCENT_QUOTE_BATCH_SIZE]
        remaining_seconds = max(0.0, deadline - time.monotonic())
        if remaining_seconds <= 0:
            return {
                "status": "fetch_error",
                **base,
                "error_type": "request_timeout",
                "completed_batch_count": batch_index,
                "failed_batch_index": batch_index,
            }

        result = fetch_tencent_quotes_sync(batch, timeout=remaining_seconds)
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "ok"
            or result.get("requested_codes") != batch
            or result.get("error_type") is not None
            or not isinstance(result.get("rows"), list)
        ):
            raw_status = result.get("status") if isinstance(result, Mapping) else None
            error_type = (
                result.get("error_type") if isinstance(result, Mapping) else None
            )
            return {
                "status": (
                    raw_status
                    if isinstance(raw_status, str) and raw_status
                    else "internal_error"
                ),
                **base,
                "error_type": (
                    error_type
                    if isinstance(error_type, str) and error_type
                    else "invalid_batch_response"
                ),
                "completed_batch_count": batch_index,
                "failed_batch_index": batch_index,
            }
        rows.extend(deepcopy(result["rows"]))

    return {
        "status": "ok",
        **base,
        "rows": rows,
        "completed_batch_count": batch_count,
    }


def fetch_tencent_quote_sync(code: str, *, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    symbol = to_tencent_symbol(code)
    url = f"{TENCENT_REALTIME_URL}={symbol}"
    started_at = time.time()
    try:
        response = requests.get(url, headers=TENCENT_HEADERS, timeout=timeout)
        response.encoding = "gbk"
        if response.status_code != 200:
            logger.info("Tencent quote failed: code=%s symbol=%s status=%s", code, symbol, response.status_code)
            return None
        quote = parse_tencent_quote_payload(code, response.text)
        if quote:
            logger.info(
                "Tencent quote fetched: code=%s price=%s elapsed=%.2fs",
                quote.get("code"),
                quote.get("close"),
                time.time() - started_at,
            )
        return quote
    except Exception as exc:
        logger.info("Tencent quote failed: code=%s symbol=%s error=%s", code, symbol, exc)
        return None


class TencentQuoteService:
    def __init__(self, ttl_seconds: int = 15) -> None:
        self._ttl = ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get_quote(self, code: str) -> Optional[Dict[str, Any]]:
        raw = str(code or "").strip().lower()
        provider_code = raw if raw in _TENCENT_MAJOR_INDEX_SYMBOLS else normalize_cn_code(code)
        normalized = provider_code if provider_code in _TENCENT_MAJOR_INDEX_SYMBOLS else normalize_cn_code(provider_code)
        now = time.time()
        async with self._lock:
            cached = self._cache.get(normalized)
            if cached and (now - self._cache_ts.get(normalized, 0)) < self._ttl:
                return dict(cached)

        quote = await asyncio.to_thread(fetch_tencent_quote_sync, provider_code)
        if not quote:
            return None

        async with self._lock:
            self._cache[normalized] = dict(quote)
            self._cache_ts[normalized] = time.time()
        return quote

    async def get_quotes(self, codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        requests: List[tuple[str, str, str]] = []
        seen_provider_codes: set[str] = set()
        for value in codes:
            raw = str(value or "").strip().lower()
            provider_code = (
                raw if raw in _TENCENT_MAJOR_INDEX_SYMBOLS else normalize_cn_code(raw)
            )
            if not provider_code or provider_code in seen_provider_codes:
                continue
            seen_provider_codes.add(provider_code)
            result_code = normalize_cn_code(provider_code)
            cache_code = (
                provider_code
                if provider_code in _TENCENT_MAJOR_INDEX_SYMBOLS
                else result_code
            )
            requests.append((result_code, provider_code, cache_code))

        result: Dict[str, Dict[str, Any]] = {}
        missing: List[tuple[str, str, str]] = []
        now = time.time()
        async with self._lock:
            for result_code, provider_code, cache_code in requests:
                cached = self._cache.get(cache_code)
                if cached and (now - self._cache_ts.get(cache_code, 0)) < self._ttl:
                    result[result_code] = dict(cached)
                else:
                    missing.append((result_code, provider_code, cache_code))

        if not missing:
            return result
        batch = await asyncio.to_thread(
            fetch_tencent_quotes_batched_sync,
            [provider_code for _, provider_code, _ in missing],
            timeout=10.0,
        )
        if not isinstance(batch, Mapping) or batch.get("status") != "ok":
            return result
        rows = batch.get("rows")
        if not isinstance(rows, list):
            return result
        rows_by_key: Dict[str, Dict[str, Any]] = {}
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            if row.get("parse_status") not in {None, "ok"}:
                continue
            provider_symbol = str(row.get("provider_symbol") or "").lower()
            key = (
                provider_symbol
                if provider_symbol in _TENCENT_MAJOR_INDEX_SYMBOLS
                else normalize_cn_code(row.get("code"))
            )
            if key:
                rows_by_key[key] = row

        refreshed_at = time.time()
        async with self._lock:
            for result_code, provider_code, cache_code in missing:
                lookup_key = (
                    provider_code
                    if provider_code in _TENCENT_MAJOR_INDEX_SYMBOLS
                    else result_code
                )
                quote = rows_by_key.get(lookup_key)
                if quote is None:
                    continue
                self._cache[cache_code] = dict(quote)
                self._cache_ts[cache_code] = refreshed_at
                result[result_code] = dict(quote)
        return result


_tencent_quote_service: Optional[TencentQuoteService] = None


def get_tencent_quote_service() -> TencentQuoteService:
    global _tencent_quote_service
    if _tencent_quote_service is None:
        _tencent_quote_service = TencentQuoteService()
    return _tencent_quote_service
