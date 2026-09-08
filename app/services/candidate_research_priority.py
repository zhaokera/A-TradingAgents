"""Account upper bounds for research ordering, never execution permission."""

from typing import Any, Mapping
import math
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

from app.services.investment_policy import build_dynamic_portfolio_policy
from app.services.portfolio_diversification_service import (
    INDUSTRY_EXPOSURE_CAP_PCT,
    PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
    THEME_EXPOSURE_CAP_PCT,
)


def research_account_fit(price: Any, code: str, account: Mapping[str, Any]) -> dict:
    try:
        assets = float(account.get("total_assets") or 0)
        cash = float(account.get("available_cash") or 0)
        entry = float(price)
        if not all(math.isfinite(v) for v in (assets, cash, entry)) or assets <= 0 or entry <= 0:
            raise ValueError("invalid account or price")
    except (TypeError, ValueError):
        return {"status": "unverified", "execution_authorized": False}
    policy = build_dynamic_portfolio_policy(
        total_assets=assets, current_exposure_pct=account.get("current_exposure_pct", 0))
    # Even before taxonomy is resolved, no industry can exceed this ceiling.
    # Existing holdings and the actual market regime can only reduce it.
    cap = min(INDUSTRY_EXPOSURE_CAP_PCT, PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
              THEME_EXPOSURE_CAP_PCT, policy["hard_single_symbol_cap_pct"],
              policy["available_new_exposure_pct"])
    holdings = account.get("holding_values") or {}
    symbol_room = max(0.0, assets * policy["hard_single_symbol_cap_pct"] / 100
                      - float(holdings.get(code) or 0))
    maximum = max(0.0, min(cash, assets * cap / 100, symbol_room))
    amount = round(entry * 100, 2)
    fee = max(5.0, amount * 0.0003)
    blocked = amount > maximum or amount + fee > cash
    return {
        "status": "one_lot_above_research_ceiling" if blocked else "requires_plan_review",
        "basis": "optimistic_existing_policy_upper_bound",
        "one_lot_amount": amount,
        "estimated_buy_fee": round(fee, 2),
        "maximum_new_amount": maximum,
        "industry_cap_pct": INDUSTRY_EXPOSURE_CAP_PCT,
        "theme_cap_pct": THEME_EXPOSURE_CAP_PCT,
        "provider_sector_cap_pct": PROVIDER_SECTOR_EXPOSURE_CAP_PCT,
        "single_symbol_cap_pct": policy["hard_single_symbol_cap_pct"],
        "stop_loss_budget_amount": float(
            (Decimal(str(assets)) * Decimal(str(policy["per_position_loss_budget_pct"])) / 100)
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        ),
        "requires_stop_risk_and_portfolio_review": True,
        "existing_holdings_recheck_required": bool(holdings),
        "execution_authorized": False,
    }


def research_account_priority(candidate: Mapping[str, Any]) -> int:
    audit = candidate.get("research_account_fit") or {}
    return 1 if audit.get("status") in {"one_lot_above_research_ceiling", "one_lot_above_stop_budget"} else 0


def research_plan_account_fit(candidate: Mapping[str, Any], definition: Mapping[str, Any]) -> dict:
    audit = deepcopy(definition.get("research_account_fit") or {})
    plan = candidate.get("price_plan") or candidate.get("guarded_price_plan") or {}
    try:
        entry = float(plan.get("entry_price") or plan.get("suggested_buy_price"))
        stop = float(plan.get("stop_price") or plan.get("stop_loss_price"))
        if not (math.isfinite(entry) and math.isfinite(stop) and 0 < stop < entry and audit):
            return audit
        amount = entry * 100
        loss = round((entry - stop) * 100, 2)
        audit.update(one_lot_amount=round(amount, 2), one_lot_stop_loss=loss,
                     status="requires_plan_review")
        if amount > audit["maximum_new_amount"]:
            audit["status"] = "one_lot_above_research_ceiling"
        elif loss > audit["stop_loss_budget_amount"]:
            audit["status"] = "one_lot_above_stop_budget"
    except (TypeError, ValueError, KeyError):
        pass
    return audit
