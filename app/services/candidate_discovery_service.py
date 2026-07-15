"""Fail-closed dynamic candidate discovery for the holdings CLI."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from math import ceil, isfinite
import re
from typing import Any, Dict, Iterable, List, Optional


_SUPPORTED_CODE_PATTERN = re.compile(r"^[036]\d{5}$")
_SELECTION_BUCKETS = ("strength", "pullback")
_BASIC_SOURCE_PRIORITY = {
    "tushare": 0,
    "multi_source": 1,
    "akshare": 2,
    "baostock": 3,
}


def _normalized_code(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-6:] if len(digits) >= 6 else ""


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _empty_result(status: str, benchmark_trade_date: Optional[str], **extra: Any) -> Dict[str, Any]:
    empty_bucket_counts = {bucket: 0 for bucket in sorted(_SELECTION_BUCKETS)}
    return {
        "status": status,
        "definitions": [],
        "benchmark_trade_date": benchmark_trade_date,
        "latest_quote_trade_date": extra.pop("latest_quote_trade_date", None),
        "rejection_counts": {},
        "eligible_bucket_counts": dict(empty_bucket_counts),
        "selected_bucket_counts": dict(empty_bucket_counts),
        **extra,
    }


def _ranking_key(item: Dict[str, Any]) -> tuple:
    return (
        -item["pct_chg"],
        -item["amount"],
        item["one_lot_amount"],
        item["code"],
    )


def _basic_source_key(row: Dict[str, Any]) -> tuple:
    source = str(row.get("source") or row.get("data_source") or "").strip().lower()
    return (
        _BASIC_SOURCE_PRIORITY.get(source, len(_BASIC_SOURCE_PRIORITY)),
        source,
        str(row.get("name") or ""),
        str(row.get("industry") or ""),
    )


def _bucket_slots(limit: int) -> List[str]:
    strength_remaining = ceil(limit * 0.75)
    pullback_remaining = limit - strength_remaining
    if pullback_remaining <= 0:
        return ["strength"] * strength_remaining

    slots: List[str] = []
    initial_strength = min(2, strength_remaining)
    slots.extend(["strength"] * initial_strength)
    strength_remaining -= initial_strength
    slots.append("pullback")
    pullback_remaining -= 1

    while pullback_remaining > 0:
        strength_count = min(
            ceil(strength_remaining / (pullback_remaining + 1)),
            strength_remaining,
        )
        slots.extend(["strength"] * strength_count)
        strength_remaining -= strength_count
        slots.append("pullback")
        pullback_remaining -= 1

    slots.extend(["strength"] * strength_remaining)
    return slots


def _select_bucketed_candidates(
    eligible: List[Dict[str, Any]],
    *,
    limit: int,
    rejection_counts: Counter[str],
) -> tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    by_bucket = {
        bucket: [item for item in eligible if item["selection_bucket"] == bucket]
        for bucket in _SELECTION_BUCKETS
    }
    offsets: Counter[str] = Counter()
    industry_counts: Counter[str] = Counter()
    selected: List[Dict[str, Any]] = []

    def take_next(bucket: str) -> Optional[Dict[str, Any]]:
        bucket_rows = by_bucket[bucket]
        while offsets[bucket] < len(bucket_rows):
            item = bucket_rows[offsets[bucket]]
            offsets[bucket] += 1
            if industry_counts[item["industry"]] >= 2:
                rejection_counts["industry_cap"] += 1
                continue
            industry_counts[item["industry"]] += 1
            return item
        return None

    for bucket in _bucket_slots(limit):
        item = take_next(bucket)
        if item is not None:
            selected.append(item)

    remaining = [
        item
        for bucket, bucket_rows in by_bucket.items()
        for item in bucket_rows[offsets[bucket] :]
    ]
    remaining.sort(key=_ranking_key)
    for item in remaining:
        if len(selected) >= limit:
            break
        if industry_counts[item["industry"]] >= 2:
            rejection_counts["industry_cap"] += 1
            continue
        industry_counts[item["industry"]] += 1
        selected.append(item)

    return selected, by_bucket


def rank_dynamic_candidate_universe(
    quotes: Iterable[Dict[str, Any]],
    basics: Iterable[Dict[str, Any]],
    *,
    benchmark_trade_date: Optional[str],
    cash_available: float,
    limit: int = 8,
) -> Dict[str, Any]:
    benchmark_date = _date_text(benchmark_trade_date)
    if not benchmark_date:
        return _empty_result("benchmark_calendar_unavailable", None)

    quote_rows = [dict(row) for row in quotes if isinstance(row, dict)]
    quote_dates = [date_value for row in quote_rows if (date_value := _date_text(row.get("trade_date")))]
    if not quote_rows or not quote_dates:
        return _empty_result("quote_universe_empty", benchmark_date)

    latest_quote_date = max(quote_dates)
    if latest_quote_date != benchmark_date:
        return _empty_result(
            "stale_quote_universe",
            benchmark_date,
            latest_quote_trade_date=latest_quote_date,
        )

    available_cash = _number(cash_available)
    if available_cash is None or available_cash <= 0:
        return _empty_result(
            "cash_unavailable",
            benchmark_date,
            latest_quote_trade_date=latest_quote_date,
        )

    benchmark_code_counts = Counter(
        code
        for row in quote_rows
        if _date_text(row.get("trade_date")) == benchmark_date
        and _SUPPORTED_CODE_PATTERN.fullmatch(code := _normalized_code(row.get("code")))
    )
    duplicate_quote_codes = {
        code for code, count in benchmark_code_counts.items() if count > 1
    }
    reported_duplicate_codes = set()
    basic_rows = [dict(row) for row in basics if isinstance(row, dict)]
    basic_by_code: Dict[str, Dict[str, Any]] = {}
    for basic_row in basic_rows:
        basic_code = _normalized_code(basic_row.get("code"))
        current = basic_by_code.get(basic_code)
        if basic_code and (
            current is None or _basic_source_key(basic_row) < _basic_source_key(current)
        ):
            basic_by_code[basic_code] = basic_row
    special_treatment_codes = {
        code
        for row in basic_rows
        if (code := _normalized_code(row.get("code")))
        and (
            "ST" in str(row.get("name") or "").upper()
            or "退" in str(row.get("name") or "")
        )
    }
    basic_turnover_by_code: Dict[str, float] = {}
    invalid_basic_turnover_codes = set()
    for basic_row in basic_rows:
        basic_code = _normalized_code(basic_row.get("code"))
        raw_basic_turnover = basic_row.get("turnover_rate")
        basic_turnover = _number(raw_basic_turnover)
        if basic_code and raw_basic_turnover is not None and basic_turnover is None:
            invalid_basic_turnover_codes.add(basic_code)
        if basic_code and basic_turnover is not None:
            basic_turnover_by_code[basic_code] = max(
                basic_turnover_by_code.get(basic_code, basic_turnover),
                basic_turnover,
            )
    rejection_counts: Counter[str] = Counter()
    eligible: List[Dict[str, Any]] = []

    for row in quote_rows:
        code = _normalized_code(row.get("code"))
        if not _SUPPORTED_CODE_PATTERN.fullmatch(code):
            rejection_counts["unsupported_code"] += 1
            continue
        if _date_text(row.get("trade_date")) != benchmark_date:
            rejection_counts["stale_quote"] += 1
            continue
        if code in duplicate_quote_codes:
            if code not in reported_duplicate_codes:
                rejection_counts["duplicate_quote"] += 1
                reported_duplicate_codes.add(code)
            continue

        basic = basic_by_code.get(code, {})
        quote_name = str(row.get("name") or "").strip()
        name = str(basic.get("name") or quote_name or code).strip()
        if (
            code in special_treatment_codes
            or "ST" in quote_name.upper()
            or "退" in quote_name
            or "ST" in name.upper()
            or "退" in name
        ):
            rejection_counts["special_treatment"] += 1
            continue

        close = _number(row.get("close"))
        pct_chg = _number(row.get("pct_chg"))
        amount = _number(row.get("amount"))
        if close is None or close <= 0 or pct_chg is None or amount is None or amount <= 0:
            rejection_counts["invalid_quote"] += 1
            continue
        if pct_chg > 5 or pct_chg < -3:
            rejection_counts["hot_move"] += 1
            continue

        raw_turnover_rate = row.get("turnover_rate")
        turnover_rate = _number(raw_turnover_rate)
        if raw_turnover_rate is not None and turnover_rate is None:
            rejection_counts["invalid_quote"] += 1
            continue
        if turnover_rate is None:
            if code in invalid_basic_turnover_codes:
                rejection_counts["invalid_basic"] += 1
                continue
            turnover_rate = basic_turnover_by_code.get(code)
        if turnover_rate is not None and turnover_rate > 10:
            rejection_counts["high_turnover"] += 1
            continue

        one_lot_amount = round(close * 100, 2)
        if one_lot_amount > available_cash:
            rejection_counts["unaffordable"] += 1
            continue

        industry = str(basic.get("industry") or row.get("industry") or "未分类").strip() or "未分类"
        eligible.append(
            {
                "code": code,
                "name": name,
                "industry": industry,
                "close": close,
                "pct_chg": pct_chg,
                "amount": amount,
                "turnover_rate": turnover_rate,
                "one_lot_amount": one_lot_amount,
                "selection_bucket": "strength" if pct_chg >= 0 else "pullback",
            }
        )

    eligible.sort(key=_ranking_key)
    candidate_limit = max(1, int(limit or 8))
    selected, eligible_by_bucket = _select_bucketed_candidates(
        eligible,
        limit=candidate_limit,
        rejection_counts=rejection_counts,
    )

    definitions: List[Dict[str, Any]] = []
    for item in selected:
        definitions.append(
            {
                "code": item["code"],
                "name": item["name"],
                "theme": f"industry:{item['industry']}",
                "theme_label": item["industry"],
                "priority": len(definitions) + 1,
                "note": "来自最新 Mongo 行情快照，仍需腾讯行情和技术价格计划复核。",
                "discovery": {
                    "source": "mongo_market_quotes",
                    "trade_date": benchmark_date,
                    "close": round(item["close"], 4),
                    "pct_chg": round(item["pct_chg"], 4),
                    "amount": round(item["amount"], 2),
                    "turnover_rate": round(item["turnover_rate"], 4)
                    if item["turnover_rate"] is not None
                    else None,
                    "one_lot_amount": item["one_lot_amount"],
                    "selection_bucket": item["selection_bucket"],
                },
            }
        )

    eligible_bucket_counts = {
        bucket: len(eligible_by_bucket[bucket]) for bucket in sorted(_SELECTION_BUCKETS)
    }
    selected_counter = Counter(item["selection_bucket"] for item in selected)
    selected_bucket_counts = {
        bucket: selected_counter[bucket] for bucket in sorted(_SELECTION_BUCKETS)
    }

    return {
        "status": "ok" if definitions else "no_eligible_candidates",
        "definitions": definitions,
        "benchmark_trade_date": benchmark_date,
        "latest_quote_trade_date": latest_quote_date,
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "eligible_before_industry_cap": len(eligible),
        "eligible_bucket_counts": eligible_bucket_counts,
        "selected_bucket_counts": selected_bucket_counts,
        "selected_count": len(definitions),
    }


def discover_dynamic_candidate_universe(
    db: Any,
    *,
    benchmark_trade_date: Optional[str],
    cash_available: float,
    limit: int = 8,
) -> Dict[str, Any]:
    source = "mongo.market_quotes+stock_basic_info"
    quote_projection = {
        "_id": 0,
        "code": 1,
        "name": 1,
        "close": 1,
        "pct_chg": 1,
        "amount": 1,
        "turnover_rate": 1,
        "trade_date": 1,
    }
    basic_projection = {
        "_id": 0,
        "code": 1,
        "name": 1,
        "industry": 1,
        "turnover_rate": 1,
        "source": 1,
        "data_source": 1,
    }
    try:
        quotes = [dict(row) for row in db["market_quotes"].find({}, quote_projection)]
        basics = [dict(row) for row in db["stock_basic_info"].find({}, basic_projection)]
    except Exception as exc:
        empty_bucket_counts = {bucket: 0 for bucket in sorted(_SELECTION_BUCKETS)}
        return {
            "status": "candidate_discovery_unavailable",
            "definitions": [],
            "benchmark_trade_date": _date_text(benchmark_trade_date),
            "latest_quote_trade_date": None,
            "rejection_counts": {},
            "eligible_bucket_counts": dict(empty_bucket_counts),
            "selected_bucket_counts": dict(empty_bucket_counts),
            "source": source,
            "reason": type(exc).__name__,
        }

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date=benchmark_trade_date,
        cash_available=cash_available,
        limit=limit,
    )
    result["source"] = source
    result["quote_count"] = len(quotes)
    result["basic_count"] = len(basics)
    return result
