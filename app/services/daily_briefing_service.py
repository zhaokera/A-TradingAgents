"""One read model for the Web dashboard and Hermes daily briefing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from app.core.database import get_mongo_db
from app.services.ai_candidate_service import ai_candidate_service
from app.services.favorites_service import favorites_service
from app.services.global_macro_risk_service import global_macro_risk_service
from app.services.notifications_service import get_notifications_service
from app.services.premarket_intelligence_service import (
    premarket_intelligence_service,
)
from app.services.tencent_quote_service import get_tencent_quote_service


class DailyBriefingService:
    def __init__(self, *, premarket_service: Any = None) -> None:
        self.premarket_service = (
            premarket_service or premarket_intelligence_service
        )

    async def build(self, user_id: str, *, refresh: bool = True) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        db = get_mongo_db()
        candidate_run = await ai_candidate_service.latest(
            str(user_id), refresh_quotes=refresh
        )
        holdings = await db["user_holdings"].find(
            {"user_id": str(user_id)},
            {"_id": 0, "code": 1, "name": 1, "quantity": 1, "cost_price": 1},
        ).to_list(length=100)
        quote_map = await get_tencent_quote_service().get_quotes(
            [str(item.get("code") or "") for item in holdings]
        ) if holdings else {}
        holding_items = []
        holding_market_value = 0.0
        holding_cost = 0.0
        for item in holdings:
            code = str(item.get("code") or "")
            quantity = int(item.get("quantity") or 0)
            cost_price = float(item.get("cost_price") or 0)
            quote = quote_map.get(code) or {}
            current_price = float(
                quote.get("price") or quote.get("close") or cost_price or 0
            )
            market_value = round(quantity * current_price, 2)
            cost_value = round(quantity * cost_price, 2)
            holding_market_value += market_value
            holding_cost += cost_value
            holding_items.append(
                {
                    **item,
                    "current_price": current_price,
                    "market_value": market_value,
                    "unrealized_pnl": round(market_value - cost_value, 2),
                    "quote_source": quote.get("source"),
                    "quote_trade_at": quote.get("trade_at"),
                }
            )
        favorites = await favorites_service.get_user_favorites(str(user_id))
        lifecycle_counts: Dict[str, int] = {}
        for favorite in favorites:
            state = str(favorite.get("lifecycle_state") or "manual")
            lifecycle_counts[state] = lifecycle_counts.get(state, 0) + 1
        unread_count = await get_notifications_service().unread_count(str(user_id))
        if candidate_run:
            macro = (candidate_run.get("market") or {}).get("macro_risk") or {}
            account = candidate_run.get("account") or {}
            portfolio_plan = candidate_run.get("portfolio_plan") or {}
        else:
            if getattr(global_macro_risk_service, "db", None) is None:
                global_macro_risk_service.db = db
            macro = await global_macro_risk_service.get_current()
            settings = await db["user_holding_settings"].find_one(
                {"user_id": str(user_id)}
            )
            total_assets = float((settings or {}).get("total_assets") or holding_market_value)
            account = {
                "total_assets": round(total_assets, 2),
                "available_cash": round(max(0.0, total_assets - holding_market_value), 2),
                "current_exposure_pct": round(
                    holding_market_value / total_assets * 100, 2
                ) if total_assets else 0.0,
            }
            portfolio_plan = {}
        candidates = (
            candidate_run.get("candidates", [])
            if isinstance(candidate_run, Mapping)
            else []
        )
        allocated_research = [
            item
            for item in candidates
            if isinstance(item, Mapping)
            and (item.get("portfolio_allocation") or {}).get("status") == "allocated"
        ]
        executable = [
            item
            for item in candidates
            if isinstance(item, Mapping)
            and item.get("execution_actionable") is True
        ]
        premarket = await self.premarket_service.build(
            db=db,
            macro=macro,
            candidates=[
                item for item in candidates if isinstance(item, Mapping)
            ],
            favorites=[
                item for item in favorites if isinstance(item, Mapping)
            ],
            now=now,
        )
        return {
            "as_of": now.isoformat(),
            "premarket_intelligence": premarket,
            "account": account,
            "holdings": {
                "count": len(holding_items),
                "market_value": round(holding_market_value, 2),
                "cost_value": round(holding_cost, 2),
                "unrealized_pnl": round(holding_market_value - holding_cost, 2),
                "items": holding_items,
            },
            "market": {
                "domestic_regime": (candidate_run or {}).get("market", {}).get("domestic_regime")
                if candidate_run
                else None,
                "combined_regime": (candidate_run or {}).get("market", {}).get("regime")
                if candidate_run
                else macro.get("regime"),
                "macro_risk": macro,
            },
            "candidate_run": {
                "run_id": (candidate_run or {}).get("run_id"),
                "generated_at": (candidate_run or {}).get("generated_at"),
                "candidate_count": len(candidates),
                "executable_count": len(executable),
                "executable_candidates": executable,
                "allocated_research_count": len(allocated_research),
                "allocated_research_candidates": allocated_research,
                "portfolio_plan": portfolio_plan,
            },
            "favorites": {
                "count": len(favorites),
                "lifecycle_counts": lifecycle_counts,
            },
            "notifications": {"unread_count": unread_count},
        }


daily_briefing_service = DailyBriefingService()
