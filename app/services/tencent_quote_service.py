"""Tencent realtime quote service for A-share prices."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
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
    trade_dt = _as_market_datetime(quote.get("trade_at"))
    session = _cn_session(local_now)
    age_seconds_raw = (local_now - trade_dt).total_seconds() if trade_dt else None

    base = {
        "actionable": False,
        "status": "missing_trade_at",
        "reason": "行情缺少提供方成交时间，仅用于研究展示。",
        "source": source,
        "trade_at": trade_dt.isoformat(timespec="seconds") if trade_dt else None,
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
            "reason": "腾讯成交时间超出允许的时钟偏差，禁止用于仓位计算。",
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
        "reason": "腾讯提供方成交时间在允许时效内。",
    }


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
) -> Dict[str, Any]:
    """Fetch qfq daily bars from AKShare's Tencent history adapter."""
    local_today = datetime.now(CN_MARKET_TIMEZONE).date()
    start = (start_date or (local_today - timedelta(days=120)).strftime("%Y%m%d")).replace("-", "")
    end = (end_date or local_today.strftime("%Y%m%d")).replace("-", "")
    symbol = to_tencent_symbol(code)
    try:
        import akshare as ak

        frame = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start,
            end_date=end,
            adjust="qfq",
        )
        raw_rows = [] if frame is None or getattr(frame, "empty", False) else frame.to_dict("records")
        bars = normalize_tencent_daily_bars(raw_rows)
    except Exception as exc:
        logger.info("Tencent daily bars failed: code=%s symbol=%s error=%s", code, symbol, exc)
        return {
            "ok": False,
            "status": "fetch_error",
            "reason": str(exc),
            "code": normalize_cn_code(code),
            "symbol": symbol,
            "source": "tencent",
            "adjust": "qfq",
            "bars": [],
        }

    payload = {
        "ok": len(bars) >= min_rows,
        "status": "ok" if len(bars) >= min_rows else "insufficient_history",
        "code": normalize_cn_code(code),
        "symbol": symbol,
        "source": "tencent",
        "adjust": "qfq",
        "start_date": start,
        "end_date": end,
        "required_rows": min_rows,
        "available_rows": len(bars),
        "bars": bars,
    }
    if not payload["ok"]:
        payload["reason"] = f"腾讯前复权日线不足 {min_rows} 条。"
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
        "volume_ratio": _safe_float(fields[49]) if len(fields) > 49 else None,
        "pe_ratio": _safe_float(fields[39]) if len(fields) > 39 else None,
        "pb_ratio": _safe_float(fields[46]) if len(fields) > 46 else None,
        "circ_mv": _yi_to_yuan(fields[44]) if len(fields) > 44 and fields[44] else None,
        "total_mv": _yi_to_yuan(fields[45]) if len(fields) > 45 and fields[45] else None,
        "provider_timestamp": provider_timestamp,
        "trade_at": _parse_provider_trade_at(provider_timestamp),
        "trade_date": _extract_trade_date(provider_timestamp),
        "received_at": received_at,
        "updated_at": received_at,
    }
    return {key: value for key, value in quote.items() if value is not None}


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
        normalized = normalize_cn_code(code)
        now = time.time()
        async with self._lock:
            cached = self._cache.get(normalized)
            if cached and (now - self._cache_ts.get(normalized, 0)) < self._ttl:
                return dict(cached)

        quote = await asyncio.to_thread(fetch_tencent_quote_sync, normalized)
        if not quote:
            return None

        async with self._lock:
            self._cache[normalized] = dict(quote)
            self._cache_ts[normalized] = time.time()
        return quote

    async def get_quotes(self, codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for code in codes:
            normalized = normalize_cn_code(code)
            quote = await self.get_quote(normalized)
            if quote:
                result[normalized] = quote
        return result


_tencent_quote_service: Optional[TencentQuoteService] = None


def get_tencent_quote_service() -> TencentQuoteService:
    global _tencent_quote_service
    if _tencent_quote_service is None:
        _tencent_quote_service = TencentQuoteService()
    return _tencent_quote_service
