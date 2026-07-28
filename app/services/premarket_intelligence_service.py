"""Auditable premarket intelligence for the daily briefing."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional


CROSS_ASSET_DEFINITIONS = (
    ("sp500", "标普500", "^GSPC", "sp500_change_pct"),
    ("nasdaq", "纳斯达克综合", "^IXIC", "nasdaq_change_pct"),
    ("semiconductor", "费城半导体", "^SOX", "semiconductor_change_pct"),
    ("vix", "VIX", "^VIX", None),
    ("usdcnh", "美元兑离岸人民币", "USDCNH=X", None),
    ("oil", "WTI原油", "CL=F", "oil_change_pct"),
    ("gold", "黄金", "GC=F", "gold_change_pct"),
    ("copper", "铜", "HG=F", "copper_change_pct"),
)
TECH_POLICY_KEYWORDS = (
    "科技",
    "人工智能",
    "算力",
    "半导体",
    "机器人",
    "工业软件",
    "新质生产力",
    "专精特新",
)
POSITIVE_EVENT_KEYWORDS = (
    "支持",
    "加快",
    "增长",
    "突破",
    "中标",
    "回购",
    "增持",
    "上调",
)
NEGATIVE_EVENT_KEYWORDS = (
    "风险",
    "下降",
    "处罚",
    "减持",
    "亏损",
    "终止",
    "调查",
    "下调",
)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _datetime(value: Any) -> Optional[datetime]:
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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    parsed = _datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _normalise_code(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _provider_error(provider: str, code: str, checked_at: datetime) -> Dict[str, Any]:
    return {
        "provider": provider,
        "code": code,
        "checked_at": checked_at.isoformat(),
    }


def _event_signal(item: Mapping[str, Any]) -> str:
    text = f"{item.get('title') or ''} {item.get('summary') or ''}"
    positive = any(keyword in text for keyword in POSITIVE_EVENT_KEYWORDS)
    negative = any(keyword in text for keyword in NEGATIVE_EVENT_KEYWORDS)
    if positive and not negative:
        return "positive"
    if negative and not positive:
        return "negative"
    return "neutral"


def _cross_asset_impact(
    key: str,
    *,
    value: Optional[float],
    change_pct: Optional[float],
) -> Dict[str, Any]:
    signal = "neutral"
    sectors: list[str] = []
    reason = "变动不足以形成明确方向映射"
    if key in {"sp500", "nasdaq", "semiconductor"} and change_pct is not None:
        sectors = ["科技", "半导体", "算力"]
        if change_pct >= 0.5:
            signal, reason = "positive", "隔夜风险偏好与科技映射偏强"
        elif change_pct <= -0.5:
            signal, reason = "negative", "隔夜科技与风险资产承压"
    elif key == "vix" and value is not None:
        sectors = ["全市场", "高波动成长"]
        if value >= 25:
            signal, reason = "negative", "VIX高位抬升风险预算压力"
        elif value <= 18:
            signal, reason = "positive", "VIX处于相对低位"
    elif key == "usdcnh" and value is not None:
        sectors = ["外资敏感", "高估值成长"]
        if value >= 7.3:
            signal, reason = "negative", "离岸人民币偏弱"
        elif value <= 7.15:
            signal, reason = "positive", "离岸人民币相对稳定"
    elif key == "oil" and change_pct is not None:
        sectors = ["能源", "化工", "航空运输"]
        if change_pct >= 1:
            signal, reason = "positive", "原油上涨利好上游、增加下游成本压力"
        elif change_pct <= -1:
            signal, reason = "negative", "原油回落压制上游景气映射"
    elif key == "gold" and change_pct is not None:
        sectors = ["黄金", "贵金属"]
        if change_pct >= 0.8:
            signal, reason = "positive", "黄金上涨强化贵金属映射"
        elif change_pct <= -0.8:
            signal, reason = "negative", "黄金回落削弱贵金属映射"
    elif key == "copper" and change_pct is not None:
        sectors = ["铜", "电网设备", "新能源"]
        if change_pct >= 0.8:
            signal, reason = "positive", "铜价上涨映射资源和电气化需求"
        elif change_pct <= -0.8:
            signal, reason = "negative", "铜价走弱映射工业需求预期降温"
    return {
        "signal": signal,
        "affected_sectors": sectors,
        "reason": reason,
    }


class PremarketIntelligenceService:
    """Combine cached cross-asset data and local event/news evidence."""

    def __init__(self, *, now_factory: Any = None) -> None:
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    @staticmethod
    async def _rows(
        db: Any,
        collection_name: str,
        query: Mapping[str, Any],
        *,
        length: int,
    ) -> list[Dict[str, Any]]:
        try:
            cursor = db[collection_name].find(dict(query), {"_id": 0})
            if hasattr(cursor, "sort"):
                cursor = cursor.sort("published_at", -1)
            rows = await cursor.to_list(length=length)
        except Exception:
            return []
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    @staticmethod
    def _cross_assets(
        macro: Mapping[str, Any],
        *,
        checked_at: datetime,
        expires_at: datetime,
    ) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        snapshot = macro.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        source = str(macro.get("source") or "global_macro_risk")
        data_at = _iso(macro.get("checked_at")) or checked_at.isoformat()
        items: list[Dict[str, Any]] = []
        errors: list[Dict[str, Any]] = []
        for key, label, symbol, change_key in CROSS_ASSET_DEFINITIONS:
            value = _finite(snapshot.get(key))
            change_pct = _finite(snapshot.get(change_key)) if change_key else None
            item_errors: list[Dict[str, Any]] = []
            status = "ok"
            if value is None:
                status = "unavailable"
                item_errors.append(
                    _provider_error(source, f"{key}_missing", checked_at)
                )
                errors.extend(item_errors)
            impact = _cross_asset_impact(
                key,
                value=value,
                change_pct=change_pct,
            )
            items.append(
                {
                    "key": key,
                    "label": label,
                    "symbol": symbol,
                    "value": value,
                    "change_pct": change_pct,
                    "source": source,
                    "data_at": data_at,
                    "checked_at": checked_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "status": status,
                    "provider_errors": item_errors,
                    "impact": impact,
                }
            )
        return items, errors

    @staticmethod
    def _news_item(
        row: Mapping[str, Any],
        *,
        checked_at: datetime,
        expires_at: datetime,
    ) -> Optional[Dict[str, Any]]:
        title = str(row.get("title") or row.get("headline") or "").strip()
        summary = str(
            row.get("summary") or row.get("content") or row.get("description") or ""
        ).strip()
        if not title and not summary:
            return None
        code = _normalise_code(row.get("code") or row.get("stock_code") or row.get("symbol"))
        published_at = (
            _iso(
                row.get("published_at")
                or row.get("publish_time")
                or row.get("datetime")
                or row.get("created_at")
            )
            or checked_at.isoformat()
        )
        return {
            "code": code or None,
            "title": title[:200] or None,
            "summary": summary[:500] or None,
            "source": str(row.get("source") or row.get("data_source") or "mongo.stock_news"),
            "data_at": published_at,
            "checked_at": checked_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "ok",
            "provider_errors": [],
        }

    async def build(
        self,
        *,
        db: Any,
        macro: Mapping[str, Any],
        candidates: Iterable[Mapping[str, Any]],
        favorites: Iterable[Mapping[str, Any]],
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        checked_at = now or self._now_factory()
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        expires_at = checked_at + timedelta(minutes=30)
        overnight_start = checked_at - timedelta(hours=18)

        cross_assets, provider_errors = self._cross_assets(
            macro,
            checked_at=checked_at,
            expires_at=expires_at,
        )

        event_rows = await self._rows(
            db,
            "premarket_events",
            {
                "event_at": {
                    "$gte": overnight_start,
                    "$lte": checked_at + timedelta(days=1),
                }
            },
            length=50,
        )
        important_events = [
            {
                "title": str(row.get("title") or row.get("name") or "")[:200],
                "event_at": _iso(row.get("event_at") or row.get("published_at")),
                "region": str(row.get("region") or "global"),
                "importance": str(row.get("importance") or "unknown"),
                "source": str(row.get("source") or "mongo.premarket_events"),
                "data_at": (
                    _iso(
                        row.get("updated_at")
                        or row.get("created_at")
                        or row.get("event_at")
                        or row.get("published_at")
                    )
                    or checked_at.isoformat()
                ),
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "status": "ok",
                "provider_errors": [],
            }
            for row in event_rows
            if str(row.get("title") or row.get("name") or "").strip()
        ]
        event_errors = (
            []
            if important_events
            else [
                _provider_error(
                    "mongo.premarket_events",
                    "important_event_calendar_incomplete",
                    checked_at,
                )
            ]
        )
        provider_errors.extend(event_errors)

        policy_rows = await self._rows(
            db,
            "stock_news",
            {
                "published_at": {"$gte": overnight_start},
                "$or": [
                    {"title": {"$regex": "|".join(TECH_POLICY_KEYWORDS)}},
                    {"content": {"$regex": "|".join(TECH_POLICY_KEYWORDS)}},
                ],
            },
            length=30,
        )
        policy_items = [
            item
            for row in policy_rows
            if (
                item := self._news_item(
                    row,
                    checked_at=checked_at,
                    expires_at=expires_at,
                )
            )
        ]
        policy_errors = (
            []
            if policy_items
            else [
                _provider_error(
                    "mongo.stock_news",
                    "domestic_tech_policy_incomplete",
                    checked_at,
                )
            ]
        )
        provider_errors.extend(policy_errors)

        candidate_codes = {
            code
            for item in candidates
            if (code := _normalise_code(item.get("code")))
        }
        favorite_codes = {
            code
            for item in favorites
            if (
                code := _normalise_code(
                    item.get("code") or item.get("stock_code") or item.get("symbol")
                )
            )
        }
        tracked_codes = sorted(candidate_codes | favorite_codes)
        stock_rows = (
            await self._rows(
                db,
                "stock_news",
                {
                    "published_at": {"$gte": overnight_start},
                    "$or": [
                        {"code": {"$in": tracked_codes}},
                        {"stock_code": {"$in": tracked_codes}},
                        {"symbol": {"$in": tracked_codes}},
                    ],
                },
                length=100,
            )
            if tracked_codes
            else []
        )
        stock_items = [
            item
            for row in stock_rows
            if (
                item := self._news_item(
                    row,
                    checked_at=checked_at,
                    expires_at=expires_at,
                )
            )
        ]
        stock_errors = (
            []
            if stock_items or not tracked_codes
            else [
                _provider_error(
                    "mongo.stock_news",
                    "tracked_stock_overnight_news_incomplete",
                    checked_at,
                )
            ]
        )
        provider_errors.extend(stock_errors)

        impact_mapping = [
            {
                "scope": "cross_asset",
                "key": item["key"],
                "signal": item["impact"]["signal"],
                "affected_sectors": deepcopy(item["impact"]["affected_sectors"]),
                "affected_codes": [],
                "reason": item["impact"]["reason"],
                "source": item["source"],
                "data_at": item["data_at"],
                "checked_at": item["checked_at"],
                "expires_at": item["expires_at"],
                "status": item["status"],
                "provider_errors": deepcopy(item["provider_errors"]),
            }
            for item in cross_assets
        ]
        impact_mapping.extend(
            {
                "scope": "domestic_tech_policy",
                "key": item.get("code") or "policy",
                "signal": _event_signal(item),
                "affected_sectors": [
                    keyword
                    for keyword in TECH_POLICY_KEYWORDS
                    if keyword in f"{item.get('title') or ''} {item.get('summary') or ''}"
                ],
                "affected_codes": [item["code"]] if item.get("code") else [],
                "reason": item.get("title") or item.get("summary"),
                "source": item["source"],
                "data_at": item["data_at"],
                "checked_at": item["checked_at"],
                "expires_at": item["expires_at"],
                "status": item["status"],
                "provider_errors": deepcopy(item["provider_errors"]),
            }
            for item in policy_items
        )
        impact_mapping.extend(
            {
                "scope": "tracked_stock_news",
                "key": item.get("code") or "market",
                "signal": _event_signal(item),
                "affected_sectors": [],
                "affected_codes": [item["code"]] if item.get("code") else [],
                "reason": item.get("title") or item.get("summary"),
                "source": item["source"],
                "data_at": item["data_at"],
                "checked_at": item["checked_at"],
                "expires_at": item["expires_at"],
                "status": item["status"],
                "provider_errors": deepcopy(item["provider_errors"]),
            }
            for item in stock_items
        )

        section_statuses = [
            "ok" if all(item["status"] == "ok" for item in cross_assets) else "incomplete",
            "ok" if important_events else "incomplete",
            "ok" if policy_items else "incomplete",
            "ok" if stock_items or not tracked_codes else "incomplete",
        ]
        overall_status = "ok" if all(status == "ok" for status in section_statuses) else "degraded"
        return {
            "status": overall_status,
            "checked_at": checked_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "overnight_window_start": overnight_start.isoformat(),
            "provider_errors": provider_errors,
            "cross_assets": {
                "status": section_statuses[0],
                "source": str(macro.get("source") or "global_macro_risk"),
                "data_at": _iso(macro.get("checked_at")),
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "provider_errors": [
                    error
                    for item in cross_assets
                    for error in item["provider_errors"]
                ],
                "items": cross_assets,
            },
            "important_events": {
                "status": section_statuses[1],
                "source": "mongo.premarket_events",
                "data_at": max(
                    (item.get("data_at") for item in important_events if item.get("data_at")),
                    default=None,
                ),
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "provider_errors": event_errors,
                "items": important_events,
            },
            "domestic_tech_policy": {
                "status": section_statuses[2],
                "source": "mongo.stock_news",
                "data_at": max(
                    (item.get("data_at") for item in policy_items if item.get("data_at")),
                    default=None,
                ),
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "provider_errors": policy_errors,
                "items": policy_items,
            },
            "tracked_stock_overnight_news": {
                "status": section_statuses[3],
                "source": "mongo.stock_news",
                "data_at": max(
                    (item.get("data_at") for item in stock_items if item.get("data_at")),
                    default=None,
                ),
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "provider_errors": stock_errors,
                "candidate_codes": sorted(candidate_codes),
                "favorite_codes": sorted(favorite_codes),
                "items": stock_items,
            },
            "impact_mapping": impact_mapping,
        }


premarket_intelligence_service = PremarketIntelligenceService()
