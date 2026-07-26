import asyncio


def test_quotes_service_prefers_tencent_and_backfills_only_missing_codes(monkeypatch):
    import app.services.quotes_service as quotes_module

    class FakeTencentService:
        async def get_quotes(self, codes):
            assert codes == ["000977", "600000"]
            return {
                "000977": {
                    "code": "000977",
                    "close": 63.5,
                    "source": "tencent",
                }
            }

    monkeypatch.setattr(
        quotes_module,
        "get_tencent_quote_service",
        lambda: FakeTencentService(),
    )
    service = quotes_module.QuotesService(ttl_seconds=30)
    monkeypatch.setattr(
        service,
        "_fetch_spot_akshare",
        lambda: {
            "000977": {"close": 60.0, "source": "akshare"},
            "600000": {"close": 9.8, "source": "akshare"},
        },
    )

    result = asyncio.run(service.get_quotes(["sz000977", "600000.SH"]))

    assert result["000977"]["close"] == 63.5
    assert result["000977"]["source"] == "tencent"
    assert result["600000"]["close"] == 9.8
    assert result["600000"]["source"] == "akshare"


def test_paper_cn_price_uses_tencent_before_database(monkeypatch):
    import app.routers.paper as paper_module
    import app.services.tencent_quote_service as tencent_module

    class ForbiddenDatabase:
        def __getitem__(self, name):
            raise AssertionError(f"database fallback should not be read: {name}")

    class FakeTencentService:
        async def get_quote(self, code):
            assert code == "000977"
            return {"code": code, "close": 63.5, "source": "tencent"}

    monkeypatch.setattr(paper_module, "get_mongo_db", lambda: ForbiddenDatabase())
    monkeypatch.setattr(
        tencent_module,
        "get_tencent_quote_service",
        lambda: FakeTencentService(),
    )

    price = asyncio.run(paper_module._get_last_price("000977", "CN"))

    assert price == 63.5


def test_stocks_quote_overlays_tencent_on_cached_document(monkeypatch):
    import app.core.unified_config as unified_config_module
    import app.routers.stocks as stocks_module
    import app.services.tencent_quote_service as tencent_module

    class FakeCollection:
        def __init__(self, name):
            self.name = name

        async def find_one(self, query, projection=None):
            if self.name == "market_quotes":
                return {
                    "code": "000977",
                    "close": 60.0,
                    "pct_chg": -2.0,
                    "amount": 1.0,
                    "source": "mongo",
                }
            if self.name == "stock_basic_info":
                return {
                    "code": "000977",
                    "name": "浪潮信息",
                    "market": "A股",
                }
            raise AssertionError(f"unexpected collection: {self.name}")

    class FakeDatabase:
        def __getitem__(self, name):
            return FakeCollection(name)

    class FakeConfigManager:
        async def get_data_source_configs_async(self):
            return []

    class FakeTencentService:
        async def get_quote(self, code):
            assert code == "000977"
            return {
                "code": code,
                "name": "浪潮信息",
                "close": 63.5,
                "pct_chg": 1.2,
                "pre_close": 62.75,
                "high": 64.0,
                "low": 62.8,
                "trade_date": "2026-07-13",
                "trade_at": "2026-07-13T10:00:00+08:00",
                "updated_at": "2026-07-13T02:00:01Z",
                "source": "tencent",
            }

    monkeypatch.setattr(stocks_module, "get_mongo_db", lambda: FakeDatabase())
    monkeypatch.setattr(
        unified_config_module,
        "UnifiedConfigManager",
        FakeConfigManager,
    )
    monkeypatch.setattr(
        tencent_module,
        "get_tencent_quote_service",
        lambda: FakeTencentService(),
    )

    response = asyncio.run(
        stocks_module.get_quote("000977", current_user={"id": "admin"})
    )

    assert response["success"] is True
    assert response["data"]["price"] == 63.5
    assert response["data"]["change_percent"] == 1.2
    assert response["data"]["source"] == "tencent"
