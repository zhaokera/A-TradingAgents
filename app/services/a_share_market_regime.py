"""A-share index regime gate used by the holdings opportunity CLI."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Dict, Iterable, List, Optional


MIN_BREADTH_UNIVERSE_SIZE = 500


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blocked_result(status: str, benchmark_trade_date: Optional[str], **extra: Any) -> Dict[str, Any]:
    return {
        "status": status,
        "level": "unknown",
        "new_position_allowed": False,
        "max_new_exposure_multiplier": 0.0,
        "benchmark_trade_date": benchmark_trade_date,
        "trade_date": extra.pop("trade_date", None),
        "indices": extra.pop("indices", []),
        "is_reference_only": True,
        **extra,
    }


def assess_a_share_market_regime(
    index_quotes: Iterable[Dict[str, Any]],
    *,
    benchmark_trade_date: Optional[str],
) -> Dict[str, Any]:
    benchmark_date = _date_text(benchmark_trade_date)
    if not benchmark_date:
        return _blocked_result("benchmark_calendar_unavailable", None)

    normalized: List[Dict[str, Any]] = []
    for quote in index_quotes:
        if not isinstance(quote, dict):
            continue
        pct_chg = _number(quote.get("pct_chg"))
        trade_date = _date_text(quote.get("trade_date"))
        if pct_chg is None or not trade_date:
            continue
        normalized.append(
            {
                "code": str(quote.get("requested_symbol") or quote.get("code") or ""),
                "name": quote.get("name"),
                "pct_chg": round(pct_chg, 2),
                "trade_date": trade_date,
                "source": quote.get("source") or quote.get("data_source"),
            }
        )

    if len(normalized) < 3:
        return _blocked_result(
            "market_data_unavailable",
            benchmark_date,
            indices=normalized,
            reason="至少需要三个主要指数的有效腾讯行情。",
        )

    dates = {item["trade_date"] for item in normalized}
    latest_date = max(dates)
    if dates != {benchmark_date}:
        return _blocked_result(
            "stale_market_data",
            benchmark_date,
            trade_date=latest_date,
            indices=normalized,
            reason="主要指数行情未全部对齐最新腾讯基准交易日。",
        )

    changes = [item["pct_chg"] for item in normalized]
    average = round(sum(changes) / len(changes), 2)
    severe_decline_count = sum(1 for value in changes if value <= -2.0)
    moderate_decline_count = sum(1 for value in changes if value <= -1.0)
    if severe_decline_count >= 2 or average <= -2.0:
        level = "red"
        multiplier = 0.0
        allowed = False
        reason = "主要指数出现系统性下跌，新仓风险预算归零。"
    elif moderate_decline_count >= 1 or average <= -1.0:
        level = "yellow"
        multiplier = 0.5
        allowed = True
        reason = "主要指数偏弱，外部风险允许额度减半。"
    else:
        level = "green"
        multiplier = 1.0
        allowed = True
        reason = "主要指数未触发系统性下跌门槛。"

    return {
        "status": "ok",
        "level": level,
        "new_position_allowed": allowed,
        "max_new_exposure_multiplier": multiplier,
        "benchmark_trade_date": benchmark_date,
        "trade_date": benchmark_date,
        "average_pct_chg": average,
        "severe_decline_count": severe_decline_count,
        "moderate_decline_count": moderate_decline_count,
        "indices": normalized,
        "reason": reason,
        "is_reference_only": True,
    }


def assess_a_share_market_breadth(
    market_quotes: Iterable[Dict[str, Any]],
    *,
    benchmark_trade_date: Optional[str],
) -> Dict[str, Any]:
    benchmark_date = _date_text(benchmark_trade_date)
    if not benchmark_date:
        return {
            "status": "benchmark_calendar_unavailable",
            "level": "unknown",
            "actionable": False,
            "max_new_exposure_multiplier": None,
            "benchmark_trade_date": None,
            "is_reference_only": True,
        }

    normalized: List[Dict[str, Any]] = []
    for quote in market_quotes:
        if not isinstance(quote, dict):
            continue
        code_digits = re.sub(r"\D", "", str(quote.get("code") or quote.get("symbol") or ""))
        code = code_digits[-6:] if len(code_digits) >= 6 else ""
        pct_chg = _number(quote.get("pct_chg"))
        trade_date = _date_text(quote.get("trade_date"))
        if not code or pct_chg is None or not trade_date:
            continue
        normalized.append(
            {
                "code": code,
                "name": str(quote.get("name") or ""),
                "pct_chg": pct_chg,
                "trade_date": trade_date,
            }
        )

    all_normalized = normalized
    normalized = [item for item in all_normalized if item["trade_date"] == benchmark_date]
    excluded_stale_count = len(all_normalized) - len(normalized)
    if all_normalized and not normalized:
        return {
            "status": "stale_market_breadth",
            "level": "unknown",
            "actionable": False,
            "max_new_exposure_multiplier": None,
            "benchmark_trade_date": benchmark_date,
            "trade_date": max(item["trade_date"] for item in all_normalized),
            "universe_size": 0,
            "excluded_stale_count": excluded_stale_count,
            "reason": "全市场行情没有对齐最新腾讯基准交易日。",
            "is_reference_only": True,
        }

    if len(normalized) < MIN_BREADTH_UNIVERSE_SIZE:
        return {
            "status": "market_breadth_unavailable",
            "level": "unknown",
            "actionable": False,
            "max_new_exposure_multiplier": None,
            "benchmark_trade_date": benchmark_date,
            "trade_date": benchmark_date if normalized else None,
            "universe_size": len(normalized),
            "excluded_stale_count": excluded_stale_count,
            "minimum_universe_size": MIN_BREADTH_UNIVERSE_SIZE,
            "reason": "有效全市场行情数量不足，保留指数门禁并要求人工确认市场宽度。",
            "is_reference_only": True,
        }

    def is_limit_down_like(item: Dict[str, Any]) -> bool:
        code = item["code"]
        name = item["name"].upper()
        if "ST" in name:
            threshold = -4.8
        elif code.startswith(("300", "688")):
            threshold = -19.5
        elif code.startswith(("4", "8")):
            threshold = -29.5
        else:
            threshold = -9.5
        return item["pct_chg"] <= threshold

    universe_size = len(normalized)
    advancer_count = sum(1 for item in normalized if item["pct_chg"] > 0)
    decliner_count = sum(1 for item in normalized if item["pct_chg"] < 0)
    unchanged_count = universe_size - advancer_count - decliner_count
    deep_decline_count = sum(1 for item in normalized if item["pct_chg"] <= -7.0)
    limit_down_like_count = sum(1 for item in normalized if is_limit_down_like(item))
    decliner_ratio = round(decliner_count / universe_size * 100, 2)
    deep_decline_ratio = round(deep_decline_count / universe_size * 100, 2)
    limit_down_like_ratio = round(
        limit_down_like_count / universe_size * 100,
        2,
    )

    if decliner_ratio >= 75 or deep_decline_ratio >= 5 or limit_down_like_count >= 80:
        level = "red"
        multiplier = 0.0
        risk_triggers = [
            key
            for key, triggered in (
                ("decliner_ratio", decliner_ratio >= 75),
                ("deep_decline_ratio", deep_decline_ratio >= 5),
                ("limit_down_like_count", limit_down_like_count >= 80),
            )
            if triggered
        ]
        reason = (
            "整体下跌比例未触发门槛，但个股深跌尾部风险显著，新仓风险预算归零。"
            if "decliner_ratio" not in risk_triggers
            else "市场宽度显著恶化，新仓风险预算归零。"
        )
    elif decliner_ratio >= 60 or deep_decline_ratio >= 2 or limit_down_like_count >= 25:
        level = "yellow"
        multiplier = 0.5
        risk_triggers = [
            key
            for key, triggered in (
                ("decliner_ratio", decliner_ratio >= 60),
                ("deep_decline_ratio", deep_decline_ratio >= 2),
                ("limit_down_like_count", limit_down_like_count >= 25),
            )
            if triggered
        ]
        reason = (
            "整体下跌比例未触发门槛，但个股深跌尾部风险偏高，新仓风险预算减半。"
            if "decliner_ratio" not in risk_triggers
            else "市场宽度偏弱，新仓风险预算减半。"
        )
    else:
        level = "green"
        multiplier = 1.0
        risk_triggers = []
        reason = "市场宽度未触发系统性风险门槛。"

    return {
        "status": "ok",
        "level": level,
        "actionable": True,
        "max_new_exposure_multiplier": multiplier,
        "benchmark_trade_date": benchmark_date,
        "trade_date": benchmark_date,
        "universe_size": universe_size,
        "excluded_stale_count": excluded_stale_count,
        "advancer_count": advancer_count,
        "decliner_count": decliner_count,
        "unchanged_count": unchanged_count,
        "decliner_ratio_pct": decliner_ratio,
        "deep_decline_count": deep_decline_count,
        "deep_decline_ratio_pct": deep_decline_ratio,
        "limit_down_like_count": limit_down_like_count,
        "limit_down_like_ratio_pct": limit_down_like_ratio,
        "risk_triggers": risk_triggers,
        "reason": reason,
        "is_reference_only": True,
    }


def combine_a_share_market_regimes(
    index_regime: Dict[str, Any],
    breadth_regime: Dict[str, Any],
) -> Dict[str, Any]:
    index = dict(index_regime or {})
    breadth = dict(breadth_regime or {})
    combined = {
        **index,
        "index_regime": index,
        "breadth_regime": breadth,
        "breadth_confirmation_required": breadth.get("status") != "ok",
    }
    if index.get("status") != "ok":
        combined["reason"] = index.get("reason") or "主要指数状态不可用，失败关闭。"
        return combined
    if breadth.get("status") != "ok":
        combined["reason"] = (
            f"{index.get('reason') or ''} 市场宽度不可用，仍需人工确认涨跌家数。"
        ).strip()
        return combined

    multiplier = min(
        float(index.get("max_new_exposure_multiplier") or 0),
        float(breadth.get("max_new_exposure_multiplier") or 0),
    )
    if multiplier <= 0:
        level = "red"
    elif multiplier < 1:
        level = "yellow"
    else:
        level = "green"
    combined.update(
        {
            "status": "ok",
            "level": level,
            "new_position_allowed": multiplier > 0,
            "max_new_exposure_multiplier": multiplier,
            "breadth_confirmation_required": False,
            "reason": (
                f"指数门禁：{index.get('reason') or '无'} "
                f"市场宽度：{breadth.get('reason') or '无'}"
            ),
        }
    )
    return combined
