"""Fee-aware A-share risk sizing for holdings CLI reference plans."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from app.services.holding_price_guardrails import calculate_net_reward_risk


CENT = Decimal("0.01")
COMMISSION_RATE = Decimal("0.0003")
MIN_COMMISSION = Decimal("5")
STAMP_DUTY_RATE = Decimal("0.0005")
TRANSFER_FEE_RATE = Decimal("0.00001")
SLIPPAGE_RATE = Decimal("0.0005")
LOT_SIZE = 100
MIN_NET_REWARD_RISK = Decimal("1.5")

EXTERNAL_RISK_CAPS = {
    "green": Decimal("0.20"),
    "yellow": Decimal("0.12"),
    "red": Decimal("0"),
    "unknown": Decimal("0"),
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _float(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def _order_estimate(reference_price: float, quantity: int, side: str) -> Dict[str, Any]:
    reference = _decimal(reference_price)
    slippage_multiplier = Decimal("1") + SLIPPAGE_RATE if side == "buy" else Decimal("1") - SLIPPAGE_RATE
    execution = reference * slippage_multiplier
    gross = _money(execution * quantity)
    commission = max(_money(gross * COMMISSION_RATE), MIN_COMMISSION)
    transfer_fee = _money(gross * TRANSFER_FEE_RATE)
    stamp_duty = _money(gross * STAMP_DUTY_RATE) if side == "sell" else Decimal("0")
    total_fees = _money(commission + transfer_fee + stamp_duty)
    total_cost = _money(gross + total_fees) if side == "buy" else None
    net_proceeds = _money(gross - total_fees) if side == "sell" else None
    return {
        "side": side,
        "reference_price": float(reference),
        "execution_price": round(float(execution), 4),
        "quantity": quantity,
        "gross_amount": _float(gross),
        "commission": _float(commission),
        "transfer_fee": _float(transfer_fee),
        "stamp_duty": _float(stamp_duty),
        "total_fees": _float(total_fees),
        "total_cost": _float(total_cost),
        "net_proceeds": _float(net_proceeds),
    }


def evaluate_ashare_trade(
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
    quantity: int,
) -> Dict[str, Any]:
    if quantity <= 0 or quantity % LOT_SIZE != 0:
        raise ValueError("A-share quantity must be a positive 100-share lot multiple")
    if not (0 < stop_price < entry_price < target_price):
        raise ValueError("expected stop < entry < target")

    entry_order = _order_estimate(entry_price, quantity, "buy")
    stop_order = _order_estimate(stop_price, quantity, "sell")
    target_order = _order_estimate(target_price, quantity, "sell")
    rr = calculate_net_reward_risk(
        entry_total_cost=entry_order["total_cost"],
        stop_net_proceeds=stop_order["net_proceeds"],
        target_net_proceeds=target_order["net_proceeds"],
    )
    return {
        "entry_order": entry_order,
        "stop_order": stop_order,
        "target_order": target_order,
        **rr,
    }


def apply_net_reward_risk_gate(
    price_plan: Dict[str, Any],
    *,
    quantity: int = LOT_SIZE,
) -> Dict[str, Any]:
    """Attach a conservative one-lot net RR check to a guarded price plan."""
    guarded = dict(price_plan or {})
    failed_gates = list(guarded.get("failed_gates") or [])
    if not guarded.get("actionable"):
        guarded["failed_gates"] = list(dict.fromkeys(failed_gates))
        return guarded

    entry = guarded.get("suggested_buy_price")
    stop = guarded.get("stop_loss_price")
    target = guarded.get("target_price")
    try:
        trade = evaluate_ashare_trade(
            entry_price=float(entry),
            stop_price=float(stop),
            target_price=float(target),
            quantity=quantity,
        )
    except (TypeError, ValueError):
        failed_gates.append("invalid_price_ordering")
        guarded.update(
            {
                "actionable": False,
                "status": "invalid_price_ordering",
                "fee_aware_trade": None,
                "failed_gates": list(dict.fromkeys(failed_gates)),
            }
        )
        return guarded

    guarded["fee_aware_trade"] = trade
    guarded["min_net_reward_risk"] = float(MIN_NET_REWARD_RISK)
    if float(trade.get("net_reward_risk") or 0) < float(MIN_NET_REWARD_RISK):
        failed_gates.append("net_rr_below_1_5")
        guarded["actionable"] = False
        guarded["status"] = "net_rr_below_1_5"
    guarded["failed_gates"] = list(dict.fromkeys(failed_gates))
    return guarded


def build_external_risk_gate(level: Optional[str], *, actionable_equity: Optional[float]) -> Dict[str, Any]:
    normalized = str(level or "unknown").strip().lower()
    if normalized not in EXTERNAL_RISK_CAPS:
        raise ValueError("external risk level must be green, yellow, red, or unknown")
    cap = EXTERNAL_RISK_CAPS[normalized]
    equity = _decimal(actionable_equity) if actionable_equity is not None and actionable_equity > 0 else None
    amount = _money(equity * cap) if equity is not None else Decimal("0")
    actionable = cap > 0 and equity is not None
    reasons = {
        "green": "外部风险为绿色，新仓总额上限为可执行权益的20%。",
        "yellow": "外部风险为黄色，新仓总额上限降至可执行权益的12%。",
        "red": "外部风险为红色，禁止生成新仓数量。",
        "unknown": "外部风险未确认，按0%上限失败关闭。",
    }
    if equity is None:
        reason = "缺少可执行权益快照，禁止生成新仓数量。"
    else:
        reason = reasons[normalized]
    return {
        "level": normalized,
        "actionable": actionable,
        "max_new_exposure_pct": float(cap * 100),
        "max_new_exposure_amount": _float(amount),
        "actionable_equity": float(equity) if equity is not None else None,
        "reason": reason,
    }


def size_ashare_candidate(
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
    actionable_equity: Optional[float],
    cash_available: float,
    original_cash: float,
    remaining_new_exposure: float,
    remaining_initial_deploy: float,
    remaining_loss_budget: float,
    existing_symbol_market_value: Optional[float],
) -> Dict[str, Any]:
    equity = float(actionable_equity or 0)
    candidate_cash_cap = round(float(original_cash) * 0.35, 2)
    post_trade_symbol_cap = round(equity * 0.20, 2) if equity > 0 else 0.0
    constraints = {
        "cash_available": round(float(cash_available), 2),
        "candidate_cash_cap": candidate_cash_cap,
        "remaining_new_exposure": round(float(remaining_new_exposure), 2),
        "remaining_initial_deploy": round(float(remaining_initial_deploy), 2),
        "remaining_loss_budget": round(float(remaining_loss_budget), 2),
        "post_trade_symbol_cap": post_trade_symbol_cap,
        "existing_symbol_market_value": (
            round(float(existing_symbol_market_value), 2)
            if existing_symbol_market_value is not None
            else None
        ),
    }
    common = {
        "lot_size": LOT_SIZE,
        "min_net_reward_risk": float(MIN_NET_REWARD_RISK),
        "risk_budget_amount": round(float(remaining_loss_budget), 2),
        "constraints": constraints,
    }

    if equity <= 0 or existing_symbol_market_value is None:
        failed = ["actionable_equity_unavailable"]
        if existing_symbol_market_value is None:
            failed.append("same_symbol_exposure_unavailable")
        return {
            **common,
            "suggested_lots": 0,
            "suggested_quantity": 0,
            "trade": None,
            "failed_gates": failed,
            "blocking_failed_gates": failed,
        }

    approximate_one_lot = max(entry_price * LOT_SIZE, 0.01)
    maximum_lots = max(1, int(float(cash_available) / approximate_one_lot) + 1)
    best_lots = 0
    best_trade = None
    all_failures: list[str] = []
    closest_failures: Optional[list[str]] = None
    for lots in range(1, maximum_lots + 1):
        quantity = lots * LOT_SIZE
        try:
            trade = evaluate_ashare_trade(
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                quantity=quantity,
            )
        except ValueError:
            return {
                **common,
                "suggested_lots": 0,
                "suggested_quantity": 0,
                "trade": None,
                "failed_gates": ["invalid_price_ordering"],
                "blocking_failed_gates": ["invalid_price_ordering"],
            }

        buy_cost = float(trade["entry_order"]["total_cost"])
        reference_market_value = round(entry_price * quantity, 2)
        failures = []
        if float(trade["net_reward_risk"] or 0) < float(MIN_NET_REWARD_RISK):
            failures.append("net_rr_below_1_5")
        if buy_cost > cash_available:
            failures.append("insufficient_cash_with_buy_costs")
        if buy_cost > candidate_cash_cap:
            failures.append("candidate_cash_cap")
        if buy_cost > remaining_new_exposure:
            failures.append("external_new_exposure_cap")
        if buy_cost > remaining_initial_deploy:
            failures.append("initial_deploy_cap")
        if float(existing_symbol_market_value) + reference_market_value > post_trade_symbol_cap:
            failures.append("post_trade_symbol_cap")
        if float(trade["risk_amount"]) > remaining_loss_budget:
            failures.append("account_loss_budget")
        if failures:
            for failure in failures:
                if failure not in all_failures:
                    all_failures.append(failure)
            if closest_failures is None or len(failures) < len(closest_failures):
                closest_failures = list(failures)
            continue
        best_lots = lots
        best_trade = trade

    return {
        **common,
        "suggested_lots": best_lots,
        "suggested_quantity": best_lots * LOT_SIZE,
        "trade": best_trade,
        "failed_gates": [] if best_lots else all_failures,
        "blocking_failed_gates": (
            [] if best_lots else (closest_failures or ["no_feasible_lot_size"])
        ),
    }
