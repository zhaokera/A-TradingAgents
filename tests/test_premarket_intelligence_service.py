from datetime import datetime, timezone

import pytest

from app.services.premarket_intelligence_service import (
    PremarketIntelligenceService,
)


NOW = datetime(2026, 7, 28, 0, 30, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *_args):
        return self

    async def to_list(self, *, length):
        return self.rows[:length]


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, _query, _projection):
        return _Cursor(self.rows)


class _DB:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return _Collection(self.collections.get(name, []))


def _macro():
    return {
        "status": "ok",
        "source": "yfinance_official_market_symbols",
        "checked_at": NOW.isoformat(),
        "snapshot": {
            "sp500": 6400,
            "sp500_change_pct": 0.7,
            "nasdaq": 22000,
            "nasdaq_change_pct": 1.2,
            "semiconductor": 6100,
            "semiconductor_change_pct": 1.5,
            "vix": 17,
            "usdcnh": 7.18,
            "oil": 72,
            "oil_change_pct": -0.2,
            "gold": 2400,
            "gold_change_pct": 0.9,
            "copper": 5.2,
            "copper_change_pct": 1.1,
        },
    }


@pytest.mark.asyncio
async def test_premarket_intelligence_is_auditable_and_maps_sector_impacts():
    db = _DB(
        {
            "premarket_events": [
                {
                    "title": "美联储议息会议",
                    "event_at": "2026-07-28T18:00:00+00:00",
                    "importance": "high",
                    "source": "calendar-cache",
                    "updated_at": NOW,
                }
            ],
            "stock_news": [
                {
                    "code": "601138",
                    "title": "算力基础设施政策更新",
                    "summary": "政策继续支持算力基础设施。",
                    "published_at": "2026-07-28T00:10:00+00:00",
                    "source": "news-cache",
                }
            ],
        }
    )
    service = PremarketIntelligenceService(now_factory=lambda: NOW)

    result = await service.build(
        db=db,
        macro=_macro(),
        candidates=[{"code": "601138"}],
        favorites=[{"stock_code": "000977"}],
        now=NOW,
    )

    assert result["status"] == "ok"
    assert result["cross_assets"]["status"] == "ok"
    assert len(result["cross_assets"]["items"]) == 8
    for item in result["cross_assets"]["items"]:
        assert item["source"]
        assert item["data_at"]
        assert item["checked_at"]
        assert item["expires_at"]
        assert item["status"] == "ok"
        assert isinstance(item["provider_errors"], list)
    semiconductor = next(
        item
        for item in result["cross_assets"]["items"]
        if item["key"] == "semiconductor"
    )
    assert semiconductor["impact"]["signal"] == "positive"
    assert "科技" in semiconductor["impact"]["affected_sectors"]
    assert result["important_events"]["items"][0]["title"] == "美联储议息会议"
    tracked = result["tracked_stock_overnight_news"]
    assert tracked["candidate_codes"] == ["601138"]
    assert tracked["favorite_codes"] == ["000977"]
    assert tracked["items"][0]["code"] == "601138"
    policy_impact = next(
        item
        for item in result["impact_mapping"]
        if item["scope"] == "domestic_tech_policy"
    )
    assert policy_impact["signal"] == "positive"
    assert "算力" in policy_impact["affected_sectors"]
    stock_impact = next(
        item
        for item in result["impact_mapping"]
        if item["scope"] == "tracked_stock_news"
    )
    assert stock_impact["signal"] == "positive"


@pytest.mark.asyncio
async def test_premarket_intelligence_marks_missing_sources_incomplete():
    service = PremarketIntelligenceService(now_factory=lambda: NOW)

    result = await service.build(
        db=_DB({}),
        macro={
            "status": "unavailable",
            "source": "unavailable",
            "checked_at": NOW.isoformat(),
            "snapshot": {},
        },
        candidates=[{"code": "601138"}],
        favorites=[],
        now=NOW,
    )

    assert result["status"] == "degraded"
    assert result["cross_assets"]["status"] == "incomplete"
    assert result["important_events"]["status"] == "incomplete"
    assert result["domestic_tech_policy"]["status"] == "incomplete"
    assert result["tracked_stock_overnight_news"]["status"] == "incomplete"
    assert result["provider_errors"]
    assert all(
        item["status"] == "unavailable"
        for item in result["cross_assets"]["items"]
    )
