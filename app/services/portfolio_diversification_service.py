"""Portfolio-level concentration, valuation, and correlation allocation gates."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo


THEME_EXPOSURE_CAP_PCT = 35.0
PROVIDER_SECTOR_EXPOSURE_CAP_PCT = 40.0
INDUSTRY_EXPOSURE_CAP_PCT = 30.0
PAIRWISE_CORRELATION_CAP = 0.80
CORRELATION_SESSION_COUNT = 60
MIN_CORRELATION_OVERLAP = 40
MAX_ZERO_RETURN_RATIO = 0.20
LIVE_PHASES = frozenset({"live_am", "live_pm"})
LIVE_HOLDING_QUOTE_MAX_AGE_SECONDS = 300
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
VALUATION_SESSION_PHASES = frozenset(
    {"pre_open", "live_am", "midday_break", "live_pm", "post_close"}
)

HistoryLoader = Callable[[str], Awaitable[Any] | Any]


def _finite_number(value: Any, *, minimum: Optional[float] = None) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        return None
    return parsed


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    matches = (
        re.fullmatch(r"(?:SH|SZ)\.(\d{1,6})", text),
        re.fullmatch(r"(\d{1,6})\.(?:SH|SZ)", text),
        re.fullmatch(r"(?:SH|SZ)(\d{1,6})", text),
        re.fullmatch(r"(\d{1,6})", text),
    )
    for match in matches:
        if match:
            return match.group(1).zfill(6)
    return text


def _parse_datetime(value: Any) -> Optional[datetime]:
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
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed


def _expected_valuation_phases(market_phase: str) -> frozenset[str]:
    if market_phase == "closed_day":
        return frozenset({"post_close", "closed_day"})
    if market_phase in VALUATION_SESSION_PHASES:
        return frozenset({market_phase})
    return frozenset({market_phase}) if market_phase else frozenset()


def _close_series(bars: Any) -> Dict[str, float]:
    if isinstance(bars, Mapping):
        bars = bars.get("bars")
    if not isinstance(bars, Iterable) or isinstance(bars, (str, bytes, Mapping)):
        return {}
    series: Dict[str, float] = {}
    for raw in bars:
        if not isinstance(raw, Mapping):
            continue
        date_value = raw.get("date") or raw.get("trade_date")
        date_text = str(date_value or "").strip()[:10]
        close = _finite_number(raw.get("close"), minimum=0.0)
        if not date_text or close is None or close <= 0:
            continue
        series[date_text] = close
    return dict(sorted(series.items()))


def _simple_returns(bars: Any) -> Dict[str, float]:
    closes = _close_series(bars)
    result: Dict[str, float] = {}
    previous: Optional[float] = None
    for trade_date, close in closes.items():
        if previous is not None and previous > 0:
            result[trade_date] = close / previous - 1.0
        previous = close
    return result


def calculate_return_correlation(left_bars: Any, right_bars: Any) -> Dict[str, Any]:
    """Calculate Pearson correlation over the last 60 overlapping qfq returns."""

    left = _simple_returns(left_bars)
    right = _simple_returns(right_bars)
    overlap_dates = sorted(set(left).intersection(right))[-CORRELATION_SESSION_COUNT:]
    overlap = len(overlap_dates)
    base = {
        "basis": "unavailable",
        "overlap": overlap,
        "value": None,
        "sessions_requested": CORRELATION_SESSION_COUNT,
    }
    if overlap < MIN_CORRELATION_OVERLAP:
        return {**base, "reason": "insufficient_overlap"}

    left_values = [left[date] for date in overlap_dates]
    right_values = [right[date] for date in overlap_dates]
    left_zero_ratio = sum(value == 0 for value in left_values) / overlap
    right_zero_ratio = sum(value == 0 for value in right_values) / overlap
    ratio_audit = {
        "zero_return_ratio_left": left_zero_ratio,
        "zero_return_ratio_right": right_zero_ratio,
    }
    if (
        left_zero_ratio > MAX_ZERO_RETURN_RATIO
        or right_zero_ratio > MAX_ZERO_RETURN_RATIO
    ):
        return {**base, **ratio_audit, "reason": "excessive_zero_returns"}

    left_mean = sum(left_values) / overlap
    right_mean = sum(right_values) / overlap
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_values, right_values)
    )
    left_variance = sum((value - left_mean) ** 2 for value in left_values)
    right_variance = sum((value - right_mean) ** 2 for value in right_values)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator <= 0 or not math.isfinite(denominator):
        return {**base, **ratio_audit, "reason": "constant_return_series"}

    value = max(-1.0, min(1.0, covariance / denominator))
    return {
        "basis": "empirical_qfq_60_sessions",
        "overlap": overlap,
        "value": round(value, 12),
        "sessions_requested": CORRELATION_SESSION_COUNT,
        **ratio_audit,
    }


def taxonomy_fallback_correlation(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> Dict[str, Any]:
    left_industry = str(left.get("industry") or "").strip()
    right_industry = str(right.get("industry") or "").strip()
    left_theme = str(left.get("objective_segment") or "").strip()
    right_theme = str(right.get("objective_segment") or "").strip()
    if left_industry and left_industry == right_industry:
        value = 1.0
    elif left_theme and left_theme == right_theme:
        value = 0.85
    else:
        value = 0.50
    return {"basis": "taxonomy_fallback", "overlap": 0, "value": value}


async def _default_history_loader(code: str) -> Dict[str, Any]:
    from app.services.tencent_quote_service import fetch_tencent_daily_bars_sync

    return await asyncio.to_thread(
        fetch_tencent_daily_bars_sync,
        code,
        min_rows=CORRELATION_SESSION_COUNT + 1,
    )


class PortfolioDiversificationService:
    """Allocate ranked candidates only when all shared portfolio gates pass."""

    def __init__(self, *, history_loader: Optional[HistoryLoader] = None) -> None:
        self.history_loader = history_loader or _default_history_loader

    async def _history(
        self,
        code: str,
        cache: Dict[str, Dict[str, Any]],
        *,
        cutoff_date: str,
        include_cutoff: bool,
    ) -> Dict[str, Any]:
        if code in cache:
            return cache[code]
        try:
            payload = self.history_loader(code)
            if inspect.isawaitable(payload):
                payload = await payload
        except Exception as exc:
            payload = {"ok": False, "bars": [], "reason": type(exc).__name__}
        if not isinstance(payload, Mapping):
            payload = {"ok": False, "bars": [], "reason": "invalid_history_payload"}
        result = dict(payload)
        bars = result.get("bars")
        if not isinstance(bars, list):
            result["bars"] = []
        elif str(result.get("adjust") or "").strip().lower() != "qfq":
            result["bars"] = []
            result["history_unavailable_reason"] = "non_qfq_history"
        else:
            result["bars"] = [
                dict(bar)
                for bar in bars
                if isinstance(bar, Mapping)
                and (
                    str(bar.get("date") or bar.get("trade_date") or "")[:10]
                    <= cutoff_date
                    if include_cutoff
                    else str(bar.get("date") or bar.get("trade_date") or "")[:10]
                    < cutoff_date
                )
            ]
        cache[code] = result
        return result

    @staticmethod
    def _holding_audit(
        raw: Mapping[str, Any], *, total_assets: float, market_phase: str, as_of: datetime
    ) -> Dict[str, Any]:
        holding = dict(raw)
        code = _normalize_code(holding.get("code") or holding.get("stock_code"))
        quantity = _finite_number(
            holding.get("quantity", holding.get("shares")), minimum=0.0
        )
        audit: Dict[str, Any] = {
            "code": code,
            "quantity": quantity,
            "market_value": None,
            "objective_segment": holding.get("objective_segment"),
            "provider_sector": holding.get("provider_sector"),
            "industry": holding.get("industry"),
            "quote_trade_at": holding.get("quote_trade_at"),
            "valuation_phase": holding.get("valuation_phase"),
            "expected_valuation_phases": sorted(
                _expected_valuation_phases(market_phase)
            ),
            "total_assets_denominator": holding.get("total_assets_denominator"),
            "quote_age_seconds": None,
            "valid": True,
            "reason_codes": [],
        }
        if quantity is None or quantity <= 0:
            return audit

        market_value = _finite_number(holding.get("market_value"))
        if market_value is None:
            current_price = _finite_number(
                holding.get("current_price", holding.get("current")), minimum=0.0
            )
            if current_price is not None:
                market_value = quantity * current_price
        audit["market_value"] = market_value
        if market_value is None:
            audit["reason_codes"].append("holding_valuation_missing")
        elif market_value <= 0:
            audit["reason_codes"].append("holding_valuation_invalid")

        if not str(holding.get("objective_segment") or "").strip():
            audit["reason_codes"].append("holding_taxonomy_missing")
        if not str(holding.get("provider_sector") or "").strip():
            audit["reason_codes"].append("holding_taxonomy_missing")
        if not str(holding.get("industry") or "").strip():
            audit["reason_codes"].append("holding_taxonomy_missing")

        quote_trade_at = _parse_datetime(holding.get("quote_trade_at"))
        if quote_trade_at is None:
            audit["reason_codes"].append("holding_quote_trade_at_missing")
        else:
            age = (as_of.astimezone(timezone.utc) - quote_trade_at.astimezone(timezone.utc)).total_seconds()
            audit["quote_trade_at"] = quote_trade_at.isoformat(timespec="seconds")
            audit["quote_age_seconds"] = int(age) if age.is_integer() else age
            if market_phase in LIVE_PHASES and (
                age < 0 or age > LIVE_HOLDING_QUOTE_MAX_AGE_SECONDS
            ):
                audit["reason_codes"].append("holding_quote_stale")

        valuation_phase = str(holding.get("valuation_phase") or "").strip()
        if not valuation_phase:
            audit["reason_codes"].append("holding_valuation_phase_missing")
        elif valuation_phase not in _expected_valuation_phases(market_phase):
            audit["reason_codes"].append("holding_valuation_phase_mismatch")

        denominator = _finite_number(
            holding.get("total_assets_denominator"), minimum=0.0
        )
        audit["total_assets_denominator"] = denominator
        if denominator is None or denominator <= 0:
            audit["reason_codes"].append("holding_denominator_missing")
        elif not math.isclose(denominator, total_assets, rel_tol=0.0, abs_tol=0.01):
            audit["reason_codes"].append("holding_denominator_mismatch")

        audit["reason_codes"] = list(dict.fromkeys(audit["reason_codes"]))
        audit["valid"] = not audit["reason_codes"]
        return audit

    async def _correlation_audit(
        self,
        candidate: Mapping[str, Any],
        comparisons: Iterable[Mapping[str, Any]],
        history_cache: Dict[str, Dict[str, Any]],
        *,
        cutoff_date: str,
        include_cutoff: bool,
    ) -> Dict[str, Any]:
        code = _normalize_code(candidate.get("code"))
        candidate_history = await self._history(
            code,
            history_cache,
            cutoff_date=cutoff_date,
            include_cutoff=include_cutoff,
        )
        items: List[Dict[str, Any]] = []
        for compared in comparisons:
            compared_code = _normalize_code(compared.get("code"))
            if not compared_code or compared_code == code:
                continue
            compared_history = await self._history(
                compared_code,
                history_cache,
                cutoff_date=cutoff_date,
                include_cutoff=include_cutoff,
            )
            unavailable_reason = (
                candidate_history.get("history_unavailable_reason")
                or compared_history.get("history_unavailable_reason")
            )
            empirical = (
                {
                    "basis": "unavailable",
                    "overlap": 0,
                    "value": None,
                    "reason": unavailable_reason,
                }
                if unavailable_reason
                else calculate_return_correlation(
                    candidate_history.get("bars"), compared_history.get("bars")
                )
            )
            if empirical["basis"] == "unavailable":
                selected = taxonomy_fallback_correlation(candidate, compared)
            else:
                selected = empirical
            items.append(
                {
                    "compared_symbol": compared_code,
                    "correlation_basis": selected["basis"],
                    "overlap": selected["overlap"],
                    "value": selected["value"],
                    "empirical_unavailable_reason": (
                        empirical.get("reason")
                        if empirical["basis"] == "unavailable"
                        else None
                    ),
                }
            )

        blocking = next(
            (
                item
                for item in items
                if float(item["value"]) > PAIRWISE_CORRELATION_CAP + 1e-12
            ),
            None,
        )
        max_item = max(items, key=lambda item: float(item["value"]), default=None)
        return {
            "cap": PAIRWISE_CORRELATION_CAP,
            "comparisons": items,
            "compared_symbols": [item["compared_symbol"] for item in items],
            "blocking_pair": blocking,
            "max_pair": max_item,
        }

    async def allocate(
        self,
        candidates: Iterable[Mapping[str, Any]],
        *,
        holdings: Iterable[Mapping[str, Any]],
        total_assets: Any,
        available_cash: Any,
        policy: Mapping[str, Any],
        market_phase: str,
        as_of: Optional[datetime] = None,
        lot_size: int = 100,
    ) -> Dict[str, Any]:
        assets = _finite_number(total_assets, minimum=0.0)
        cash = _finite_number(available_cash, minimum=0.0)
        exposure_pct = _finite_number(policy.get("available_new_exposure_pct"), minimum=0.0)
        loss_budget_pct = _finite_number(
            policy.get("total_new_position_loss_budget_pct"), minimum=0.0
        )
        hard_symbol_cap_pct = _finite_number(
            policy.get("hard_single_symbol_cap_pct"), minimum=0.0
        )
        policy_valid = bool(
            assets is not None
            and assets > 0
            and cash is not None
            and exposure_pct is not None
            and loss_budget_pct is not None
            and hard_symbol_cap_pct is not None
            and isinstance(lot_size, int)
            and not isinstance(lot_size, bool)
            and lot_size > 0
        )
        if not policy_valid:
            assets = assets or 0.0
            cash = cash or 0.0
            exposure_pct = loss_budget_pct = hard_symbol_cap_pct = 0.0

        effective_as_of = as_of or datetime.now(timezone.utc)
        if effective_as_of.tzinfo is None:
            effective_as_of = effective_as_of.replace(tzinfo=SHANGHAI_TIMEZONE)
        raw_holdings = [dict(item) for item in holdings if isinstance(item, Mapping)]
        holding_audits = [
            self._holding_audit(
                item,
                total_assets=float(assets),
                market_phase=str(market_phase or ""),
                as_of=effective_as_of,
            )
            for item in raw_holdings
        ]
        positive_holding_audits = [
            item for item in holding_audits if (item.get("quantity") or 0) > 0
        ]
        holding_blocker = next(
            (
                reason
                for item in positive_holding_audits
                for reason in item["reason_codes"]
            ),
            None,
        )

        caps = {
            "theme": THEME_EXPOSURE_CAP_PCT,
            "provider_sector": PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
            "industry": INDUSTRY_EXPOSURE_CAP_PCT,
        }
        field_map = {
            "theme": "objective_segment",
            "provider_sector": "provider_sector",
            "industry": "industry",
        }
        ledgers: Dict[str, Dict[str, float]] = {key: {} for key in caps}
        symbol_ledger: Dict[str, float] = {}
        comparison_entities: List[Dict[str, Any]] = []
        for raw, audit in zip(raw_holdings, holding_audits):
            if (audit.get("quantity") or 0) <= 0 or not audit.get("valid"):
                continue
            market_value = float(audit["market_value"])
            code = str(audit["code"])
            symbol_ledger[code] = symbol_ledger.get(code, 0.0) + market_value
            entity = {
                "code": code,
                "objective_segment": raw.get("objective_segment"),
                "provider_sector": raw.get("provider_sector"),
                "industry": raw.get("industry"),
            }
            comparison_entities.append(entity)
            for dimension, source_field in field_map.items():
                value = str(raw.get(source_field) or "").strip()
                ledgers[dimension][value] = ledgers[dimension].get(value, 0.0) + market_value

        indexed_candidates = [
            (index, dict(item))
            for index, item in enumerate(candidates)
            if isinstance(item, Mapping)
        ]

        def candidate_order(item: tuple[int, Dict[str, Any]]) -> tuple[float, float, str, int]:
            index, candidate = item
            rank = _finite_number(candidate.get("rank"))
            score = _finite_number(candidate.get("rank_score"))
            return (
                rank if rank is not None else float(index + 1),
                -(score if score is not None else 0.0),
                _normalize_code(candidate.get("code")),
                index,
            )

        ordered = sorted(indexed_candidates, key=candidate_order)
        capital_budget = min(float(cash), float(assets) * float(exposure_pct) / 100)
        loss_budget = float(assets) * float(loss_budget_pct) / 100
        remaining_capital = capital_budget
        remaining_loss = loss_budget
        history_cache: Dict[str, Dict[str, Any]] = {}
        allocations: List[Dict[str, Any]] = []

        for ordinal, (_, candidate) in enumerate(ordered, 1):
            code = _normalize_code(candidate.get("code"))
            raw_rank = _finite_number(candidate.get("rank"))
            plan = candidate.get("price_plan")
            sizing = candidate.get("position_sizing")
            plan = plan if isinstance(plan, Mapping) else {}
            sizing = sizing if isinstance(sizing, Mapping) else {}
            entry = _finite_number(plan.get("entry_price"), minimum=0.0)
            stop = _finite_number(plan.get("stop_price"), minimum=0.0)
            suggested = _finite_number(sizing.get("suggested_quantity"), minimum=0.0)
            base: Dict[str, Any] = {
                **deepcopy(candidate),
                "rank": int(raw_rank) if raw_rank is not None else ordinal,
                "code": code,
                "status": "wait",
                "reason": "price_plan_or_account_unavailable",
                "reason_codes": ["price_plan_or_account_unavailable"],
                "quantity": 0,
                "amount": 0.0,
                "position_pct": 0.0,
                "planned_loss_amount": 0.0,
                "planned_loss_pct_of_assets": 0.0,
                "holding_valuation_audit": deepcopy(holding_audits),
            }

            taxonomy = {
                dimension: str(candidate.get(source_field) or "").strip()
                for dimension, source_field in field_map.items()
            }
            exposure_before = {
                dimension: ledgers[dimension].get(value, 0.0)
                for dimension, value in taxonomy.items()
            }
            base["exposure_audit"] = {
                dimension: {
                    "taxonomy_value": taxonomy[dimension] or None,
                    "before_amount": round(exposure_before[dimension], 2),
                    "before_pct": round(exposure_before[dimension] / float(assets) * 100, 2)
                    if assets
                    else 0.0,
                    "after_amount": round(exposure_before[dimension], 2),
                    "after_pct": round(exposure_before[dimension] / float(assets) * 100, 2)
                    if assets
                    else 0.0,
                    "cap_pct": caps[dimension],
                }
                for dimension in caps
            }
            symbol_before = symbol_ledger.get(code, 0.0)
            base["symbol_exposure_audit"] = {
                "before_amount": round(symbol_before, 2),
                "after_amount": round(symbol_before, 2),
                "after_pct": round(symbol_before / float(assets) * 100, 2) if assets else 0.0,
                "cap_pct": float(hard_symbol_cap_pct),
            }
            base["correlation_audit"] = {
                "cap": PAIRWISE_CORRELATION_CAP,
                "comparisons": [],
                "compared_symbols": [],
                "blocking_pair": None,
                "max_pair": None,
            }
            base.update(
                correlation_basis="not_applicable",
                compared_symbols=[],
                correlation_overlap=0,
                correlation_value=None,
            )

            if not policy_valid:
                base.update(reason="invalid_portfolio_policy", reason_codes=["invalid_portfolio_policy"])
                allocations.append(base)
                continue
            if holding_blocker:
                base.update(reason=holding_blocker, reason_codes=[holding_blocker])
                allocations.append(base)
                continue
            if any(not value for value in taxonomy.values()):
                base.update(reason="candidate_taxonomy_missing", reason_codes=["candidate_taxonomy_missing"])
                allocations.append(base)
                continue
            if (
                entry is None
                or stop is None
                or entry <= 0
                or stop <= 0
                or stop >= entry
                or suggested is None
                or suggested <= 0
            ):
                allocations.append(base)
                continue

            suggested_quantity = math.floor(suggested / lot_size) * lot_size
            if suggested_quantity <= 0:
                base.update(reason="one_lot_unaffordable", reason_codes=["one_lot_unaffordable"])
                allocations.append(base)
                continue

            one_lot_amount = entry * lot_size
            one_lot_loss = (entry - stop) * lot_size
            quantity_caps: Dict[str, int] = {
                "suggested_quantity": suggested_quantity,
                "capital": math.floor(remaining_capital / entry / lot_size) * lot_size,
                "loss": math.floor(remaining_loss / (entry - stop) / lot_size) * lot_size,
                "hard_single_symbol": math.floor(
                    max(0.0, float(assets) * float(hard_symbol_cap_pct) / 100 - symbol_before)
                    / entry
                    / lot_size
                )
                * lot_size,
            }
            for dimension, cap_pct in caps.items():
                room = max(
                    0.0,
                    float(assets) * cap_pct / 100 - exposure_before[dimension],
                )
                quantity_caps[dimension] = math.floor(room / entry / lot_size) * lot_size
            quantity = min(quantity_caps.values())
            if quantity <= 0:
                if quantity_caps["hard_single_symbol"] <= 0:
                    reason = "hard_single_symbol_cap"
                elif quantity_caps["capital"] <= 0:
                    reason = "shared_capital_budget_exhausted"
                elif quantity_caps["loss"] <= 0:
                    reason = "shared_loss_budget_exhausted"
                elif any(quantity_caps[dimension] <= 0 for dimension in caps):
                    reason = "concentration_limit"
                else:
                    reason = "one_lot_unaffordable"
                base.update(
                    reason=reason,
                    reason_codes=[reason],
                    one_lot_amount=round(one_lot_amount, 2),
                    one_lot_planned_loss=round(one_lot_loss, 2),
                    quantity_caps=quantity_caps,
                )
                allocations.append(base)
                continue

            proposed_amount = quantity * entry
            for dimension in caps:
                audit = base["exposure_audit"][dimension]
                audit["proposed_after_amount"] = round(exposure_before[dimension] + proposed_amount, 2)
                audit["proposed_after_pct"] = round(
                    (exposure_before[dimension] + proposed_amount) / float(assets) * 100,
                    2,
                )

            correlation = await self._correlation_audit(
                candidate,
                comparison_entities,
                history_cache,
                cutoff_date=effective_as_of.date().isoformat(),
                include_cutoff=str(market_phase or "") == "post_close",
            )
            base["correlation_audit"] = correlation
            max_pair = correlation["max_pair"]
            base.update(
                correlation_basis=(max_pair or {}).get("correlation_basis", "not_applicable"),
                compared_symbols=correlation["compared_symbols"],
                correlation_overlap=(max_pair or {}).get("overlap", 0),
                correlation_value=(max_pair or {}).get("value"),
            )
            if correlation["blocking_pair"] is not None:
                base.update(reason="correlation_limit", reason_codes=["correlation_limit"])
                allocations.append(base)
                continue

            amount = round(proposed_amount, 2)
            planned_loss = round(quantity * (entry - stop), 2)
            remaining_capital = max(0.0, remaining_capital - amount)
            remaining_loss = max(0.0, remaining_loss - planned_loss)
            symbol_ledger[code] = symbol_before + amount
            base["symbol_exposure_audit"].update(
                after_amount=round(symbol_before + amount, 2),
                after_pct=round((symbol_before + amount) / float(assets) * 100, 2),
            )
            for dimension, value in taxonomy.items():
                ledgers[dimension][value] = exposure_before[dimension] + amount
                base["exposure_audit"][dimension].update(
                    after_amount=round(exposure_before[dimension] + amount, 2),
                    after_pct=round(
                        (exposure_before[dimension] + amount) / float(assets) * 100,
                        2,
                    ),
                )
            comparison_entities.append(
                {
                    "code": code,
                    "objective_segment": candidate.get("objective_segment"),
                    "provider_sector": candidate.get("provider_sector"),
                    "industry": candidate.get("industry"),
                }
            )
            base.update(
                status="allocated",
                reason="allocated",
                reason_codes=["allocated"],
                quantity=quantity,
                amount=amount,
                position_pct=round(amount / float(assets) * 100, 2),
                planned_loss_amount=planned_loss,
                planned_loss_pct_of_assets=round(planned_loss / float(assets) * 100, 2),
                quantity_caps=quantity_caps,
            )
            allocations.append(base)

        allocated = [item for item in allocations if item["status"] == "allocated"]
        allocated_amount = round(sum(float(item["amount"]) for item in allocated), 2)
        total_planned_loss = round(
            sum(float(item["planned_loss_amount"]) for item in allocated), 2
        )
        return {
            "status": "allocated" if allocated else "no_executable_position",
            "policy": {
                **deepcopy(dict(policy)),
                "theme_exposure_cap_pct": THEME_EXPOSURE_CAP_PCT,
                "provider_sector_exposure_cap_pct": PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
                "industry_exposure_cap_pct": INDUSTRY_EXPOSURE_CAP_PCT,
                "pairwise_correlation_cap": PAIRWISE_CORRELATION_CAP,
            },
            "effective_limits": {
                "source": "decision_loop_v1_constants",
                "theme_exposure_cap_pct": THEME_EXPOSURE_CAP_PCT,
                "provider_sector_exposure_cap_pct": PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
                "industry_exposure_cap_pct": INDUSTRY_EXPOSURE_CAP_PCT,
                "pairwise_correlation_cap": PAIRWISE_CORRELATION_CAP,
            },
            "holding_valuation_audit": holding_audits,
            "capital_budget": round(capital_budget, 2),
            "allocated_amount": allocated_amount,
            "remaining_capital": round(max(0.0, capital_budget - allocated_amount), 2),
            "allocated_exposure_pct": round(allocated_amount / float(assets) * 100, 2)
            if assets
            else 0.0,
            "loss_budget": round(loss_budget, 2),
            "total_planned_loss": total_planned_loss,
            "remaining_loss_budget": round(max(0.0, loss_budget - total_planned_loss), 2),
            "total_planned_loss_pct": round(total_planned_loss / float(assets) * 100, 2)
            if assets
            else 0.0,
            "allocated_position_count": len(allocated),
            "watch_only_count": len(allocations) - len(allocated),
            "exposure_ledgers": {
                dimension: {key: round(value, 2) for key, value in values.items()}
                for dimension, values in ledgers.items()
            },
            "allocations": allocations,
        }


portfolio_diversification_service = PortfolioDiversificationService()
