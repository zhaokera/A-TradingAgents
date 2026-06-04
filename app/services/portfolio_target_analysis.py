"""持仓目标进度分析。"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from math import floor
from typing import Any, Dict, Optional, Tuple


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _lot_floor(shares: float) -> int:
    if shares <= 0:
        return 0
    if shares >= 100:
        return int(floor(shares / 100) * 100)
    return int(floor(shares))


def _remaining_days_in_month(as_of: date) -> int:
    return max(1, monthrange(as_of.year, as_of.month)[1] - as_of.day + 1)


def _choose_action(
    profit_loss_pct: float,
    target_monthly_return_pct: float,
    stop_loss_pct: float,
    required_daily_return_pct: float,
) -> Tuple[str, str, float, float, str]:
    if profit_loss_pct <= -abs(stop_loss_pct):
        return "触发止损", "sell", 0.5, 1.0, "已跌破止损线，先控制回撤。"

    if profit_loss_pct >= target_monthly_return_pct:
        return "目标已达成", "sell", 0.3, 0.6, "月度目标已经达成，优先分批锁定收益。"

    if profit_loss_pct >= target_monthly_return_pct * 0.8:
        return "接近目标", "sell", 0.2, 0.4, "接近月度目标，可先卖出一部分降低回撤风险。"

    if profit_loss_pct < 0:
        return "目标落后且亏损", "hold", 0.0, 0.0, "当前亏损，不建议为追目标盲目补仓。"

    if required_daily_return_pct > 1.5:
        return "目标压力偏高", "hold", 0.0, 0.0, "剩余时间要求的日均收益偏高，避免追涨。"

    return "目标落后", "hold", 0.0, 0.0, "尚未达到月度目标，继续观察趋势和风险。"


def build_target_analysis(
    holding: Dict[str, Any],
    current_price: Optional[float],
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """根据持仓和月收益目标生成仓位管理参考。"""
    as_of = as_of or date.today()
    quantity = _to_int(holding.get("quantity"))
    cost_price = _to_float(holding.get("cost_price"), 0.0) or 0.0
    current = _to_float(current_price, cost_price) or cost_price
    target_monthly_return_pct = _to_float(holding.get("target_monthly_return_pct"), 10.0) or 10.0
    stop_loss_pct = _to_float(holding.get("stop_loss_pct"), 8.0) or 8.0

    cost_value = cost_price * quantity
    market_value = current * quantity
    profit_loss = market_value - cost_value
    profit_loss_pct = ((current - cost_price) / cost_price * 100) if cost_price > 0 else 0.0
    remaining_days = _remaining_days_in_month(as_of)
    remaining_target_pct = max(0.0, target_monthly_return_pct - profit_loss_pct)
    required_daily_return_pct = remaining_target_pct / remaining_days
    monthly_target_progress_pct = (
        profit_loss_pct / target_monthly_return_pct * 100
        if target_monthly_return_pct > 0
        else 0.0
    )

    status, action, ratio_min, ratio_max, reason = _choose_action(
        profit_loss_pct,
        target_monthly_return_pct,
        stop_loss_pct,
        required_daily_return_pct,
    )
    shares_min = _lot_floor(quantity * ratio_min)
    shares_max = _lot_floor(quantity * ratio_max)

    return {
        "status": status,
        "action": action,
        "reason": reason,
        "quantity": quantity,
        "cost_price": round(cost_price, 4),
        "current_price": round(current, 4) if current_price is not None else None,
        "market_value": round(market_value, 2) if current_price is not None else None,
        "profit_loss": round(profit_loss, 2) if current_price is not None else None,
        "profit_loss_pct": round(profit_loss_pct, 2) if current_price is not None else None,
        "target_monthly_return_pct": round(target_monthly_return_pct, 2),
        "monthly_target_progress_pct": round(monthly_target_progress_pct, 2),
        "remaining_days_in_month": remaining_days,
        "required_daily_return_pct": round(required_daily_return_pct, 2),
        "suggested_ratio_min": ratio_min,
        "suggested_ratio_max": ratio_max,
        "suggested_ratio_text": f"{int(ratio_min * 100)}%-{int(ratio_max * 100)}%",
        "suggested_shares_min": shares_min,
        "suggested_shares_max": shares_max,
        "suggested_shares_text": f"{shares_min}-{shares_max}股",
        "is_reference_only": True,
    }
