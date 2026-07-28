"""Cached international market risk gate for candidate exposure policy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from app.core.database import get_mongo_db


MACRO_CACHE_ID = "global_macro_risk_v1"


def score_macro_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate observable cross-asset moves into a bounded risk regime."""

    factors = []
    score = 0
    rules = (
        ("vix", 25.0, 2, "VIX高于25"),
        ("vix", 32.0, 2, "VIX高于32"),
        ("nasdaq_change_pct", -1.5, 1, "纳指显著下跌", "le"),
        ("sp500_change_pct", -1.5, 1, "标普显著下跌", "le"),
        ("usdcnh", 7.35, 1, "离岸人民币偏弱"),
    )
    for rule in rules:
        key, threshold, points, label, *direction = rule
        try:
            value = float(snapshot.get(key))
        except (TypeError, ValueError):
            continue
        hit = value <= threshold if direction and direction[0] == "le" else value >= threshold
        if hit:
            score += points
            factors.append({"key": key, "value": value, "signal": label})
    regime = "red" if score >= 4 else "yellow" if score >= 2 else "green"
    return {
        "status": "ok",
        "regime": regime,
        "score": min(score, 7),
        "factors": factors,
        "snapshot": dict(snapshot),
    }


class GlobalMacroRiskService:
    SYMBOLS = {
        "vix": "^VIX",
        "nasdaq": "^IXIC",
        "sp500": "^GSPC",
        "semiconductor": "^SOX",
        "usdcnh": "USDCNH=X",
        "oil": "CL=F",
        "gold": "GC=F",
        "copper": "HG=F",
    }

    def __init__(self, fetcher: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.db = None
        self._fetcher = fetcher or self._fetch_with_yfinance

    async def _get_db(self):
        if self.db is None:
            self.db = get_mongo_db()
        return self.db

    @classmethod
    def _fetch_with_yfinance(cls) -> Dict[str, Any]:
        import yfinance as yf

        result: Dict[str, Any] = {}
        for key, symbol in cls.SYMBOLS.items():
            frame = yf.download(
                symbol,
                period="5d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
                timeout=8,
            )
            if frame is None or frame.empty:
                continue
            closes = frame["Close"].dropna()
            if hasattr(closes, "columns"):
                closes = closes.iloc[:, 0]
            if closes.empty:
                continue
            latest = float(closes.iloc[-1])
            previous = float(closes.iloc[-2]) if len(closes) >= 2 else latest
            result[key] = round(latest, 4)
            if key in {
                "nasdaq",
                "sp500",
                "semiconductor",
                "oil",
                "gold",
                "copper",
            } and previous:
                result[f"{key}_change_pct"] = round(
                    (latest - previous) / previous * 100, 2
                )
        return result

    async def get_current(self, *, force: bool = False) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        db = await self._get_db()
        cached = await db["market_risk_snapshots"].find_one({"_id": MACRO_CACHE_ID})
        if (
            not force
            and cached
            and isinstance(cached.get("expires_at"), datetime)
            and cached["expires_at"].replace(
                tzinfo=cached["expires_at"].tzinfo or timezone.utc
            ) > now
        ):
            return {key: value for key, value in cached.items() if key != "_id"}
        try:
            snapshot = await asyncio.wait_for(
                asyncio.to_thread(self._fetcher), timeout=45
            )
            if not snapshot:
                raise RuntimeError("global market snapshot is empty")
            result = score_macro_snapshot(snapshot)
            result.update(
                checked_at=now,
                expires_at=now + timedelta(minutes=30),
                source="yfinance_official_market_symbols",
            )
        except Exception as exc:
            if cached:
                stale = {key: value for key, value in cached.items() if key != "_id"}
                stale.update(status="stale", stale_reason=str(exc)[:240])
                return stale
            result = {
                "status": "unavailable",
                "regime": "yellow",
                "score": None,
                "factors": [],
                "snapshot": {},
                "checked_at": now,
                "expires_at": now + timedelta(minutes=10),
                "source": "unavailable",
                "reason": str(exc)[:240],
            }
        await db["market_risk_snapshots"].update_one(
            {"_id": MACRO_CACHE_ID}, {"$set": result}, upsert=True
        )
        return result


global_macro_risk_service = GlobalMacroRiskService()
