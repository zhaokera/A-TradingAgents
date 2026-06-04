"""持仓分析辅助逻辑。"""

from __future__ import annotations

from math import floor
from typing import Any, Dict, Optional, Tuple


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _round_lot(shares: float) -> int:
    if shares <= 0:
        return 0
    if shares >= 100:
        return int(floor(shares / 100) * 100)
    return int(floor(shares))


def _decision_action(decision: Optional[Dict[str, Any]]) -> str:
    if not isinstance(decision, dict):
        return "持有"
    return str(decision.get("action") or "持有")


def _risk_score(decision: Optional[Dict[str, Any]]) -> float:
    if not isinstance(decision, dict):
        return 0.0
    try:
        return float(decision.get("risk_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _choose_sell_range(
    profit_loss_pct: Optional[float],
    take_profit_price: Optional[float],
    stop_loss_price: Optional[float],
    current_price: Optional[float],
    action: str,
    risk_score: float,
) -> Tuple[str, float, float, str]:
    if current_price and stop_loss_price and current_price <= stop_loss_price:
        return "触发止损", 0.7, 1.0, "高"

    if current_price and take_profit_price and current_price >= take_profit_price:
        return "触发止盈", 0.5, 0.8, "中"

    if current_price and take_profit_price:
        distance_to_take_profit = (take_profit_price - current_price) / current_price
        if 0 <= distance_to_take_profit <= 0.03:
            return "接近止盈", 0.3, 0.5, "中"

    if current_price and stop_loss_price:
        distance_from_stop_loss = (current_price - stop_loss_price) / current_price
        if 0 <= distance_from_stop_loss <= 0.03:
            return "接近止损", 0.3, 0.6, "高"

    if profit_loss_pct is not None and profit_loss_pct <= -8:
        return "亏损控制", 0.3, 0.6, "高"

    if "卖" in action or "减" in action:
        return "模型偏谨慎", 0.3, 0.5, "中"

    if profit_loss_pct is not None and profit_loss_pct >= 15 and risk_score >= 0.45:
        return "分批止盈", 0.2, 0.4, "中"

    if "买" in action or "增" in action:
        return "继续观察", 0.0, 0.1, "低"

    return "持仓观察", 0.0, 0.2, "低"


def build_holding_analysis(
    holding: Optional[Dict[str, Any]],
    decision: Optional[Dict[str, Any]],
    current_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """根据持仓输入和模型结论生成确定性的持仓参考。"""
    if not holding:
        return None

    cost_price = _to_float(holding.get("cost_price"))
    shares = _to_float(holding.get("shares"))
    take_profit_price = _to_float(holding.get("take_profit_price"))
    stop_loss_price = _to_float(holding.get("stop_loss_price"))
    current = _to_float(current_price) or _to_float(holding.get("current_price"))

    if not cost_price or not shares:
        return None

    effective_price = current or cost_price
    market_value = effective_price * shares
    cost_value = cost_price * shares
    profit_loss = market_value - cost_value if current else None
    profit_loss_pct = ((effective_price - cost_price) / cost_price) * 100 if current else None

    action = _decision_action(decision)
    risk_score = _risk_score(decision)
    status, sell_ratio_min, sell_ratio_max, risk_level = _choose_sell_range(
        profit_loss_pct,
        take_profit_price,
        stop_loss_price,
        current,
        action,
        risk_score,
    )

    sell_shares_min = _round_lot(shares * sell_ratio_min)
    sell_shares_max = _round_lot(shares * sell_ratio_max)

    distance_to_take_profit_pct = None
    if current and take_profit_price:
        distance_to_take_profit_pct = ((take_profit_price - current) / current) * 100

    distance_to_stop_loss_pct = None
    if current and stop_loss_price:
        distance_to_stop_loss_pct = ((current - stop_loss_price) / current) * 100

    return {
        "enabled": True,
        "status": status,
        "risk_level": risk_level,
        "cost_price": round(cost_price, 4),
        "shares": int(shares),
        "current_price": round(current, 4) if current else None,
        "market_value": round(market_value, 2) if current else None,
        "profit_loss": round(profit_loss, 2) if profit_loss is not None else None,
        "profit_loss_pct": round(profit_loss_pct, 2) if profit_loss_pct is not None else None,
        "take_profit_price": round(take_profit_price, 4) if take_profit_price else None,
        "stop_loss_price": round(stop_loss_price, 4) if stop_loss_price else None,
        "distance_to_take_profit_pct": round(distance_to_take_profit_pct, 2) if distance_to_take_profit_pct is not None else None,
        "distance_to_stop_loss_pct": round(distance_to_stop_loss_pct, 2) if distance_to_stop_loss_pct is not None else None,
        "sell_ratio_min": sell_ratio_min,
        "sell_ratio_max": sell_ratio_max,
        "sell_ratio_text": f"{int(sell_ratio_min * 100)}%-{int(sell_ratio_max * 100)}%",
        "sell_shares_min": sell_shares_min,
        "sell_shares_max": sell_shares_max,
        "sell_shares_text": f"{sell_shares_min}-{sell_shares_max}股",
        "suggestion": _build_suggestion(status, sell_ratio_min, sell_ratio_max),
        "is_reference_only": True,
    }


def _build_suggestion(status: str, sell_ratio_min: float, sell_ratio_max: float) -> str:
    if sell_ratio_max <= 0:
        return "当前规则未给出卖出比例，可继续跟踪价格、风险评分和后续分析结论。"
    if status in {"触发止损", "接近止损", "亏损控制"}:
        return f"风险优先，参考卖出 {int(sell_ratio_min * 100)}%-{int(sell_ratio_max * 100)}%，先控制回撤。"
    if status in {"触发止盈", "接近止盈", "分批止盈"}:
        return f"以分批落袋为主，参考卖出 {int(sell_ratio_min * 100)}%-{int(sell_ratio_max * 100)}%，保留剩余仓位观察。"
    return f"结合模型倾向可参考卖出 {int(sell_ratio_min * 100)}%-{int(sell_ratio_max * 100)}%，避免一次性处理。"


def format_holding_analysis_markdown(analysis: Dict[str, Any]) -> str:
    if not analysis:
        return ""

    lines = [
        "### 持仓分析",
        f"- 状态：{analysis.get('status', '持仓观察')}",
        f"- 参考卖出比例：{analysis.get('sell_ratio_text', '0%-0%')}",
        f"- 参考卖出数量：{analysis.get('sell_shares_text', '0-0股')}",
    ]

    if analysis.get("current_price") is not None:
        lines.append(f"- 当前参考价：{analysis['current_price']}元")
    if analysis.get("profit_loss") is not None:
        lines.append(f"- 持仓盈亏：{analysis['profit_loss']}元（{analysis['profit_loss_pct']}%）")
    if analysis.get("distance_to_take_profit_pct") is not None:
        lines.append(f"- 距止盈价：{analysis['distance_to_take_profit_pct']}%")
    if analysis.get("distance_to_stop_loss_pct") is not None:
        lines.append(f"- 距止损价：{analysis['distance_to_stop_loss_pct']}%")
    if analysis.get("suggestion"):
        lines.append(f"- 操作参考：{analysis['suggestion']}")

    lines.append("- 提醒：该比例为仓位管理参考，不构成投资建议或交易指令。")
    return "\n".join(lines)
