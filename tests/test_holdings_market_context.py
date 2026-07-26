import asyncio

from bson import ObjectId

from app.routers.holdings import _enrich_holding, list_holdings


def _bars(code):
    closes = [120.0] * 55 + [95.0, 96.0, 97.0, 98.0, 100.0]
    return [
        {
            "date": f"2026-04-{index + 1:02d}" if index < 30 else f"2026-05-{index - 29:02d}",
            "open": close,
            "close": close,
            "high": close + 1,
            "low": close - 1,
        }
        for index, close in enumerate(closes)
    ]


def test_enrich_holding_preserves_tencent_snapshot_and_technical_plan(monkeypatch):
    class FakeQuoteService:
        async def get_quote(self, code):
            return {
                "code": code,
                "source": "tencent",
                "price": 100.0,
                "close": 100.0,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "trade_at": "2026-07-10T10:00:00+08:00",
                "trade_date": "2026-07-10",
                "received_at": "2026-07-10T02:00:01Z",
            }

    monkeypatch.setattr(
        "app.routers.holdings.get_tencent_quote_service",
        lambda: FakeQuoteService(),
        raising=False,
    )
    monkeypatch.setattr(
        "app.routers.holdings.assess_cn_quote_freshness",
        lambda quote: {
            "actionable": True,
            "status": "fresh",
            "reason": "fresh test quote",
            "trade_at": quote["trade_at"],
            "trade_date": quote["trade_date"],
            "source": "tencent",
            "age_seconds": 1,
            "session": "morning",
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.routers.holdings.fetch_tencent_daily_bars_sync",
        lambda code, **kwargs: {
            "ok": True,
            "status": "ok",
            "code": code,
            "bars": _bars(code),
        },
        raising=False,
    )

    async def no_report(item):
        assert item["quote_snapshot"]["freshness"]["actionable"] is True
        assert item["technical_price_plan"]["actionable"] is True
        return None

    monkeypatch.setattr("app.routers.holdings.build_holding_report_advice", no_report)

    item = asyncio.run(
        _enrich_holding(
            {
                "_id": ObjectId("665000000000000000000101"),
                "user_id": "665000000000000000000001",
                "code": "000977",
                "name": "浪潮信息",
                "market": "CN",
                "quantity": 100,
                "cost_price": 64.0,
            }
        )
    )

    assert item["current_price"] == 100.0
    assert item["quote_snapshot"]["trade_at"] == "2026-07-10T10:00:00+08:00"
    assert item["quote_snapshot"]["freshness"]["status"] == "fresh"
    assert item["technical_price_plan"]["source"] == "tencent_qfq_daily"
    assert "2026-07-10" in item["benchmark_session_dates"]


def test_enrich_holding_keeps_fallback_price_display_only(monkeypatch):
    class EmptyQuoteService:
        async def get_quote(self, code):
            return None

    monkeypatch.setattr(
        "app.routers.holdings.get_tencent_quote_service",
        lambda: EmptyQuoteService(),
        raising=False,
    )

    async def fallback_price(code, market):
        return 88.0

    async def no_report(item):
        return None

    monkeypatch.setattr("app.routers.holdings._get_last_price", fallback_price)
    monkeypatch.setattr("app.routers.holdings.build_holding_report_advice", no_report)

    item = asyncio.run(
        _enrich_holding(
            {
                "_id": ObjectId("665000000000000000000101"),
                "user_id": "665000000000000000000001",
                "code": "000977",
                "name": "浪潮信息",
                "market": "CN",
                "quantity": 100,
                "cost_price": 64.0,
            }
        )
    )

    assert item["current_price"] == 88.0
    assert item["quote_snapshot"]["source"] == "display_fallback"
    assert item["quote_snapshot"]["freshness"]["actionable"] is False
    assert item["technical_price_plan"]["status"] == "quote_not_actionable"


def test_enrich_holding_guards_persisted_model_prices_when_report_is_missing(monkeypatch):
    class FakeQuoteService:
        async def get_quote(self, code):
            return {
                "code": code,
                "source": "tencent",
                "price": 100.0,
                "close": 100.0,
                "trade_at": "2026-07-13T10:00:00+08:00",
                "trade_date": "2026-07-13",
            }

    monkeypatch.setattr(
        "app.routers.holdings.get_tencent_quote_service",
        lambda: FakeQuoteService(),
    )
    monkeypatch.setattr(
        "app.routers.holdings.assess_cn_quote_freshness",
        lambda quote: {"actionable": True, "status": "fresh"},
    )
    monkeypatch.setattr(
        "app.routers.holdings.fetch_tencent_daily_bars_sync",
        lambda code, **kwargs: {"ok": True, "status": "ok", "bars": _bars(code)},
    )
    monkeypatch.setattr(
        "app.routers.holdings.build_technical_price_plan",
        lambda bars, current_price: {
            "actionable": True,
            "status": "ok",
            "stop_loss_price": 96.0,
            "suggested_buy_price": 101.0,
            "suggested_sell_price": 108.0,
            "target_price": 112.0,
        },
    )

    async def no_report(item):
        return None

    monkeypatch.setattr("app.routers.holdings.build_holding_report_advice", no_report)

    item = asyncio.run(
        _enrich_holding(
            {
                "_id": ObjectId("665000000000000000000101"),
                "user_id": "665000000000000000000001",
                "code": "000977",
                "name": "浪潮信息",
                "market": "CN",
                "quantity": 100,
                "cost_price": 64.0,
                "ai_advice": {
                    "action": "sell",
                    "stop_loss_price": 30.0,
                    "suggested_buy_price": 31.0,
                    "suggested_sell_price": 32.0,
                    "target_price": 33.0,
                },
            }
        )
    )

    assert item["ai_advice"]["stop_loss_price"] == 96.0
    assert item["ai_advice"]["suggested_buy_price"] == 101.0
    assert item["ai_advice"]["target_price"] == 112.0
    assert item["ai_advice"]["historical_model_price_plan"]["target_price"] == 33.0


def test_list_holdings_fetches_benchmark_once_and_enriches_concurrently(monkeypatch):
    docs = [
        {
            "_id": ObjectId("665000000000000000000101"),
            "user_id": "user-1",
            "code": "000977",
            "market": "CN",
            "quantity": 100,
            "cost_price": 64.0,
        },
        {
            "_id": ObjectId("665000000000000000000102"),
            "user_id": "user-1",
            "code": "000066",
            "market": "CN",
            "quantity": 100,
            "cost_price": 20.0,
        },
    ]

    class FakeCursor:
        def sort(self, *_args):
            return self

        async def to_list(self, _length):
            return docs

    class FakeHoldings:
        def find(self, _query):
            return FakeCursor()

    class FakeSettings:
        async def find_one(self, _query):
            return None

    class FakeDatabase:
        def __getitem__(self, name):
            return {
                "user_holdings": FakeHoldings(),
                "user_holding_settings": FakeSettings(),
            }[name]

    benchmark_calls = 0
    active = 0
    max_active = 0
    seen_benchmarks = []

    async def fake_benchmark_dates():
        nonlocal benchmark_calls
        benchmark_calls += 1
        return ["2026-07-10", "2026-07-13"]

    async def fake_enrich(doc, *, benchmark_session_dates=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        seen_benchmarks.append(list(benchmark_session_dates or []))
        await asyncio.sleep(0)
        active -= 1
        return dict(doc)

    monkeypatch.setattr("app.routers.holdings.get_mongo_db", lambda: FakeDatabase())
    monkeypatch.setattr("app.routers.holdings._fetch_benchmark_session_dates", fake_benchmark_dates)
    monkeypatch.setattr("app.routers.holdings._enrich_holding", fake_enrich)

    asyncio.run(list_holdings(current_user={"id": "user-1"}))

    assert benchmark_calls == 1
    assert max_active == 2
    assert seen_benchmarks == [
        ["2026-07-10", "2026-07-13"],
        ["2026-07-10", "2026-07-13"],
    ]
