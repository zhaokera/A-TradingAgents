from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import stocks as stocks_router
from app.routers.auth_db import get_current_user
from app.routers.stocks import _merge_same_day_tencent_quote


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(stocks_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "test",
        "username": "test",
        "is_admin": True,
        "roles": ["admin"],
    }
    with TestClient(app) as test_client:
        yield test_client


def test_kline_never_injects_stale_quote_as_current_trading_day():
    items = [
        {
            "time": "2026-07-24",
            "open": 29.5,
            "high": 30.2,
            "low": 29.2,
            "close": 30.06,
            "volume": 1000,
            "amount": 30_000,
        }
    ]

    merged, status = _merge_same_day_tencent_quote(
        items,
        {
            "source": "tencent",
            "trade_date": "2026-07-24",
            "open": 29.5,
            "high": 30.2,
            "low": 29.2,
            "close": 30.06,
            "volume": 1000,
            "amount": 30_000,
        },
        expected_trade_date="2026-07-27",
    )

    assert status == "wrong_quote_trade_date"
    assert merged == items


def test_force_refresh_qfq_uses_direct_tencent_daily_history(client):
    result = {
        "ok": True,
        "status": "ok",
        "source": "tencent",
        "adjust": "qfq",
        "bars": [
            {
                "date": "2026-07-24",
                "open": 29.5,
                "high": 30.2,
                "low": 29.2,
                "close": 30.06,
            },
            {
                "date": "2026-07-27",
                "open": 30.5,
                "high": 31.6,
                "low": 30.2,
                "close": 31.23,
            },
        ],
    }

    with patch(
        "app.routers.stocks.fetch_tencent_daily_bars_sync",
        return_value=result,
    ):
        response = client.get(
            "/api/stocks/002625/kline",
            params={
                "period": "day",
                "limit": 2,
                "adj": "qfq",
                "force_refresh": True,
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "tencent_qfq_daily"
    assert [item["time"] for item in data["items"]] == [
        "2026-07-24",
        "2026-07-27",
    ]
    assert data["items"][-1]["close"] == 31.23
