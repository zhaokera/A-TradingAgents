"""Deterministic freshness and price-plan guardrails for real holdings."""

from __future__ import annotations

import statistics
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from app.services.tencent_quote_service import normalize_tencent_daily_bars


CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
PRICE_TICK = Decimal("0.01")
REPORT_PRICE_MAX_DIVERGENCE = 0.10


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _round_metric(value: float) -> float:
    return round(float(value), 4)


def _round_tick(value: float, mode: str = "half_up") -> float:
    rounding = {
        "floor": ROUND_FLOOR,
        "ceiling": ROUND_CEILING,
        "half_up": ROUND_HALF_UP,
    }[mode]
    return float(Decimal(str(value)).quantize(PRICE_TICK, rounding=rounding))


def _ordered_distinct(values: Iterable[float]) -> List[float]:
    return sorted({_round_metric(value) for value in values})


def _build_research_watch_levels(
    *,
    current_price: Optional[float],
    support_candidates: Iterable[float],
    resistance_candidates: Iterable[float],
) -> Dict[str, Any]:
    supports = sorted({_round_metric(value) for value in support_candidates}, reverse=True)
    resistances = sorted({_round_metric(value) for value in resistance_candidates})
    return {
        "status": "reference_only",
        "actionable": False,
        "is_reference_only": True,
        "current_price": _round_tick(current_price) if current_price is not None else None,
        "nearest_support": supports[0] if supports else None,
        "lower_supports": supports[1:],
        "nearest_resistance": resistances[0] if resistances else None,
        "higher_resistances": resistances[1:],
        "supports": supports,
        "resistances": resistances,
    }


def build_technical_price_plan(
    bars: Iterable[Dict[str, Any]],
    *,
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Build reproducible support/resistance references from 60+ qfq bars."""
    normalized = normalize_tencent_daily_bars(bars)
    if len(normalized) < 60:
        reference_price = _number(current_price)
        if reference_price is None and normalized:
            reference_price = _number(normalized[-1].get("close"))
        research_watch_levels = _build_research_watch_levels(
            current_price=reference_price,
            support_candidates=[],
            resistance_candidates=[],
        )
        return {
            "actionable": False,
            "status": "insufficient_history",
            "required_rows": 60,
            "available_rows": len(normalized),
            "source": "tencent_qfq_daily",
            "research_watch_levels": {
                **research_watch_levels,
                "status": "unavailable_insufficient_history",
            },
        }

    closes = [float(item["close"]) for item in normalized]
    lows = [float(item["low"]) for item in normalized]
    highs = [float(item["high"]) for item in normalized]
    current = _number(current_price) or closes[-1]
    last20 = closes[-20:]
    boll_mid = statistics.mean(last20)
    boll_std = statistics.stdev(last20)
    metrics = {
        "ma5": _round_metric(statistics.mean(closes[-5:])),
        "ma10": _round_metric(statistics.mean(closes[-10:])),
        "ma20": _round_metric(boll_mid),
        "ma60": _round_metric(statistics.mean(closes[-60:])),
        "boll_mid": _round_metric(boll_mid),
        "boll_upper": _round_metric(boll_mid + 2 * boll_std),
        "boll_lower": _round_metric(boll_mid - 2 * boll_std),
        "recent_5_low": _round_metric(min(lows[-5:])),
        "recent_20_low": _round_metric(min(lows[-20:])),
        "recent_20_high": _round_metric(max(highs[-20:])),
    }

    support_candidates = _ordered_distinct(
        value
        for value in (
            metrics["ma5"],
            metrics["ma10"],
            metrics["ma20"],
            metrics["ma60"],
            metrics["boll_lower"],
            metrics["recent_5_low"],
            metrics["recent_20_low"],
        )
        if value < current
    )
    resistance_candidates = _ordered_distinct(
        value
        for value in (
            metrics["ma5"],
            metrics["ma10"],
            metrics["ma20"],
            metrics["ma60"],
            metrics["boll_mid"],
            metrics["boll_upper"],
            metrics["recent_20_high"],
        )
        if value > current
    )

    missing_levels: List[str] = []
    if not support_candidates:
        missing_levels.append("support")
    for index in range(3):
        if len(resistance_candidates) <= index:
            missing_levels.append(f"resistance_{index + 1}")
    if missing_levels:
        return {
            "actionable": False,
            "status": "insufficient_ordered_levels",
            "source": "tencent_qfq_daily",
            "metrics": metrics,
            "support_candidates": support_candidates,
            "resistance_candidates": resistance_candidates,
            "missing_levels": missing_levels,
            "research_watch_levels": _build_research_watch_levels(
                current_price=current,
                support_candidates=support_candidates,
                resistance_candidates=resistance_candidates,
            ),
        }

    invalidation_basis = min(metrics["boll_lower"], metrics["recent_20_low"])
    resistance_1, resistance_2, resistance_3 = resistance_candidates[:3]
    stop_loss = _round_tick(invalidation_basis * 0.995, "floor")
    breakout = _round_tick(resistance_1 * 1.003, "ceiling")
    sell_price = _round_tick(resistance_2, "half_up")
    target = _round_tick(resistance_3, "half_up")
    failed_gates = []
    if not stop_loss < breakout:
        failed_gates.append("stop_not_below_entry")
    if not breakout < target:
        failed_gates.append("target_not_above_entry")
    if sell_price and not breakout < sell_price:
        failed_gates.append("sell_not_above_entry")

    return {
        "actionable": not failed_gates,
        "status": "ok" if not failed_gates else "invalid_price_ordering",
        "source": "tencent_qfq_daily",
        "as_of": normalized[-1]["date"],
        "current_price": _round_tick(current),
        "metrics": metrics,
        "levels": {
            "reference_support": max(support_candidates),
            "invalidation_basis": _round_metric(invalidation_basis),
            "resistance_1": resistance_1,
            "resistance_2": resistance_2,
            "resistance_3": resistance_3,
        },
        "stop_loss_price": stop_loss,
        "suggested_buy_price": breakout,
        "suggested_sell_price": sell_price,
        "target_price": target,
        "failed_gates": failed_gates,
        "research_watch_levels": _build_research_watch_levels(
            current_price=current,
            support_candidates=support_candidates,
            resistance_candidates=resistance_candidates,
        ),
        "rounding": {
            "tick": 0.01,
            "stop_buffer_pct": 0.5,
            "breakout_buffer_pct": 0.3,
            "stop_mode": "ROUND_FLOOR",
            "breakout_mode": "ROUND_CEILING",
            "default_mode": "ROUND_HALF_UP",
        },
    }


def _market_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
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


def _date_value(value: Any) -> Optional[date]:
    parsed = _market_datetime(value)
    return parsed.date() if parsed else None


def _current_session_started(as_of: datetime) -> bool:
    return as_of.weekday() < 5 and as_of.time() >= time(9, 30)


def _weekday_sessions_after(report_date: date, as_of: datetime) -> int:
    count = 0
    cursor = report_date + timedelta(days=1)
    while cursor <= as_of.date():
        if cursor.weekday() < 5 and (cursor < as_of.date() or _current_session_started(as_of)):
            count += 1
        cursor += timedelta(days=1)
    return count


def _expected_weekday_calendar_dates(report_date: date, as_of: datetime) -> set[date]:
    """Return the conservative weekday coverage required to trust a session list."""
    expected: set[date] = set()
    cursor = report_date
    while cursor <= as_of.date():
        if cursor.weekday() < 5 and (
            cursor == report_date
            or cursor < as_of.date()
            or _current_session_started(as_of)
        ):
            expected.add(cursor)
        cursor += timedelta(days=1)
    return expected


def assess_report_freshness(
    report_analysis_date: Any,
    *,
    as_of: Optional[datetime] = None,
    benchmark_session_dates: Optional[Iterable[Any]] = None,
    max_started_sessions: int = 1,
) -> Dict[str, Any]:
    local_now = _market_datetime(as_of or datetime.now(CN_MARKET_TIMEZONE))
    assert local_now is not None
    report_date = _date_value(report_analysis_date)
    if report_date is None:
        return {
            "actionable": False,
            "status": "missing_report_date",
            "calendar_source": "none",
            "calendar_is_fallback": True,
            "started_sessions_after_report": None,
        }
    if report_date > local_now.date():
        return {
            "actionable": False,
            "status": "future_report_date",
            "report_date": report_date.isoformat(),
            "calendar_source": "none",
            "calendar_is_fallback": True,
            "started_sessions_after_report": None,
        }

    normalized_sessions = sorted(
        {
            item
            for value in (benchmark_session_dates or [])
            for item in [_date_value(value)]
            if item is not None
        }
    )
    expected_calendar_dates = _expected_weekday_calendar_dates(report_date, local_now)
    benchmark_calendar_complete = bool(normalized_sessions) and expected_calendar_dates.issubset(
        set(normalized_sessions)
    )
    if benchmark_calendar_complete:
        started_sessions = sum(
            1
            for session_date in normalized_sessions
            if session_date > report_date
            and session_date <= local_now.date()
            and (session_date < local_now.date() or _current_session_started(local_now))
        )
        calendar_source = "tencent_benchmark"
        fallback = False
        fallback_reason = None
    else:
        started_sessions = _weekday_sessions_after(report_date, local_now)
        calendar_source = "weekday_fallback"
        fallback = True
        fallback_reason = (
            "incomplete_benchmark_calendar"
            if normalized_sessions
            else "benchmark_calendar_unavailable"
        )

    actionable = started_sessions <= max_started_sessions
    return {
        "actionable": actionable,
        "status": "fresh_report" if actionable else "stale_report",
        "report_date": report_date.isoformat(),
        "as_of": local_now.isoformat(timespec="seconds"),
        "started_sessions_after_report": started_sessions,
        "max_started_sessions": max_started_sessions,
        "calendar_source": calendar_source,
        "calendar_is_fallback": fallback,
        "calendar_fallback_reason": fallback_reason,
    }


def assess_recent_sale_cooldown(
    sold_at: Any,
    *,
    as_of: Optional[datetime] = None,
    benchmark_session_dates: Optional[Iterable[Any]] = None,
    cooldown_sessions: int = 2,
) -> Dict[str, Any]:
    """Assess the post-sale no-rebuy window using exchange-session semantics."""
    window = assess_report_freshness(
        sold_at,
        as_of=as_of,
        benchmark_session_dates=benchmark_session_dates,
        max_started_sessions=cooldown_sessions,
    )
    active = bool(window.get("actionable"))
    return {
        "active": active,
        "status": "cooldown" if active else "expired",
        "sold_date": window.get("report_date"),
        "as_of": window.get("as_of"),
        "started_sessions_after_sale": window.get("started_sessions_after_report"),
        "cooldown_sessions": cooldown_sessions,
        "calendar_source": window.get("calendar_source"),
        "calendar_is_fallback": window.get("calendar_is_fallback"),
        "calendar_fallback_reason": window.get("calendar_fallback_reason"),
    }


def resolve_guarded_price_plan(
    *,
    manual_plan: Optional[Dict[str, Any]],
    report_plan: Optional[Dict[str, Any]],
    technical_plan: Optional[Dict[str, Any]],
    report_freshness: Optional[Dict[str, Any]],
    max_report_divergence: float = REPORT_PRICE_MAX_DIVERGENCE,
) -> Dict[str, Any]:
    manual = manual_plan if isinstance(manual_plan, dict) else {}
    report = report_plan if isinstance(report_plan, dict) else {}
    technical = technical_plan if isinstance(technical_plan, dict) else {}
    freshness = report_freshness if isinstance(report_freshness, dict) else {}
    fields = ("stop_loss_price", "suggested_buy_price", "suggested_sell_price", "target_price")
    resolved: Dict[str, Optional[float]] = {}
    sources: Dict[str, str] = {}
    rejected_report_fields: List[str] = []

    historical_report = {field: _number(report.get(field)) for field in fields}
    for field in fields:
        manual_value = _number(manual.get(field))
        report_value = _number(report.get(field))
        technical_value = _number(technical.get(field))
        if manual_value is not None:
            resolved[field] = manual_value
            sources[field] = "manual"
            continue
        report_is_valid = (
            report_value is not None
            and technical_value is not None
            and bool(freshness.get("actionable"))
            and abs(report_value - technical_value) / technical_value <= max_report_divergence
        )
        if report_is_valid:
            resolved[field] = report_value
            sources[field] = "report"
        else:
            if report_value is not None:
                rejected_report_fields.append(field)
            resolved[field] = technical_value
            sources[field] = "technical" if technical_value is not None else "none"

    entry = resolved["suggested_buy_price"]
    stop = resolved["stop_loss_price"]
    target = resolved["target_price"]
    failed_gates: List[str] = []
    if not technical.get("actionable"):
        failed_gates.append("technical_plan_not_actionable")
    if entry is None:
        failed_gates.append("missing_entry")
    if stop is None:
        failed_gates.append("missing_stop")
    if target is None:
        failed_gates.append("missing_target")
    if stop is not None and entry is not None and not stop < entry:
        failed_gates.append("stop_not_below_entry")
    if target is not None and entry is not None and not target > entry:
        failed_gates.append("target_not_above_entry")

    return {
        **resolved,
        "actionable": not failed_gates,
        "status": "ok" if not failed_gates else "invalid_price_ordering",
        "sources": sources,
        "executable_tuple": {"entry": entry, "stop": stop, "target": target},
        "failed_gates": failed_gates,
        "rejected_report_fields": rejected_report_fields,
        "report_freshness": freshness,
        "historical_report_price_plan": historical_report,
        "technical_price_plan": technical,
        "manual_price_plan": {field: _number(manual.get(field)) for field in fields},
        "is_reference_only": True,
    }


def calculate_net_reward_risk(
    *,
    entry_total_cost: float,
    stop_net_proceeds: float,
    target_net_proceeds: float,
) -> Dict[str, Optional[float]]:
    risk = round(float(entry_total_cost) - float(stop_net_proceeds), 2)
    reward = round(float(target_net_proceeds) - float(entry_total_cost), 2)
    ratio = round(reward / risk, 4) if risk > 0 else None
    return {
        "risk_amount": risk,
        "reward_amount": reward,
        "net_reward_risk": ratio,
    }
