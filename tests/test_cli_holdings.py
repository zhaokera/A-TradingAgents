from datetime import datetime

import pytest
from bson import ObjectId

from app.services.holdings_cli import (
    CLIError,
    build_holdings_payload,
    build_summary_payload,
    build_users_payload,
    select_user,
)


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction):
        reverse = direction < 0
        self.docs.sort(key=lambda doc: doc.get(key) or "", reverse=reverse)
        return self

    def limit(self, limit):
        self.docs = self.docs[:limit]
        return self

    def __iter__(self):
        return iter(self.docs)


class FakeCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, projection=None, sort=None):
        docs = list(self.docs)
        if sort:
            for key, direction in reversed(sort):
                docs.sort(key=lambda doc: doc.get(key) or "", reverse=direction < 0)

        for doc in docs:
            if all(doc.get(key) == value for key, value in query.items()):
                if projection:
                    return {key: doc[key] for key, include in projection.items() if include and key in doc}
                return dict(doc)
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        matched = []
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                if projection:
                    projected = {}
                    for key, include in projection.items():
                        if include and key in doc:
                            projected[key] = doc[key]
                    matched.append(projected)
                else:
                    matched.append(dict(doc))
        return FakeCursor(matched)


class FakeDB:
    def __init__(self, users, holdings, settings, reports=None):
        self.collections = {
            "users": FakeCollection(users),
            "user_holdings": FakeCollection(holdings),
            "user_holding_settings": FakeCollection(settings),
            "analysis_reports": FakeCollection(reports or []),
        }

    def __getitem__(self, name):
        return self.collections[name]


def make_fake_db():
    user_id = ObjectId("665000000000000000000001")
    return FakeDB(
        users=[
            {
                "_id": user_id,
                "username": "hermes",
                "email": "hermes@example.com",
                "is_active": True,
            }
        ],
        holdings=[
            {
                "_id": ObjectId("665000000000000000000101"),
                "user_id": str(user_id),
                "code": "600519",
                "name": "贵州茅台",
                "market": "CN",
                "quantity": 100,
                "cost_price": 1000.0,
                "current_price": 1200.0,
                "target_monthly_return_pct": 10.0,
                "stop_loss_pct": 8.0,
                "updated_at": "2026-06-04T08:00:00",
            },
            {
                "_id": ObjectId("665000000000000000000102"),
                "user_id": str(user_id),
                "code": "AAPL",
                "name": "Apple",
                "market": "US",
                "quantity": 10,
                "cost_price": 180.0,
                "updated_at": "2026-06-04T07:00:00",
            },
        ],
        settings=[
            {
                "_id": ObjectId("665000000000000000000201"),
                "user_id": str(user_id),
                "total_assets": 150000.0,
                "updated_at": datetime(2026, 6, 4, 8, 30),
            }
        ],
        reports=[
            {
                "_id": ObjectId("665000000000000000000301"),
                "stock_symbol": "600519",
                "analysis_id": "600519_20260604",
                "analysis_date": "2026-06-04",
                "model_info": {"provider": "analysis_report"},
                "recommendation": "投资建议：卖出。目标价格：32.0元。决策依据：估值偏高。",
                "decision": {"action": "卖出", "target_price": 32.0, "confidence": 0.95},
                "reports": {
                    "market_report": """
                    ### 3. 关键价格区间

                    | 价格类型 | 具体价格（¥） | 说明 |
                    |---------|-------------|------|
                    | **强支撑位** | 62.33 | 布林带下轨 |
                    | **第一压力位** | 65.40 - 65.81 | MA5 与 MA60 密集区 |
                    | **第二压力位** | 67.42 | MA10 |
                    | **强压力位** | 70.27 | MA20 及布林带中轨 |
                    | **突破买入价** | 66.00 | 放量站上 MA60 可轻仓试多 |
                    | **跌破卖出价** | 62.00 | 有效跌破布林带下轨坚决止损 |
                    """,
                },
                "created_at": datetime(2026, 6, 4, 9, 0),
            }
        ],
    )


def test_select_user_by_username():
    user = select_user(make_fake_db(), username="hermes")

    assert user["id"] == "665000000000000000000001"
    assert user["username"] == "hermes"
    assert user["email"] == "hermes@example.com"


def test_select_user_requires_selector_when_multiple_users():
    user_a = {"_id": ObjectId("665000000000000000000001"), "username": "a", "email": "a@example.com"}
    user_b = {"_id": ObjectId("665000000000000000000002"), "username": "b", "email": "b@example.com"}
    db = FakeDB([user_a, user_b], [], [])

    with pytest.raises(CLIError) as exc_info:
        select_user(db)

    assert exc_info.value.exit_code == 2


def test_build_users_payload_serializes_user_ids():
    payload = build_users_payload(make_fake_db())

    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["users"][0]["id"] == "665000000000000000000001"


def test_build_holdings_payload_contains_items_settings_and_summary():
    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)

    assert payload["ok"] is True
    assert payload["data"]["user"]["username"] == "hermes"
    assert payload["data"]["items"][0]["id"] == "665000000000000000000101"
    assert payload["data"]["items"][0]["analysis"]["current_price"] == 1200.0
    assert payload["data"]["items"][1]["analysis"]["current_price"] is None
    assert payload["data"]["settings"]["total_assets"] == 150000.0
    assert payload["data"]["summary"]["holding_count"] == 2
    assert payload["data"]["summary"]["total_cost"] == 101800.0
    assert payload["data"]["summary"]["configured_total_assets"] == 150000.0


def test_build_holdings_payload_includes_report_advice_and_price_plan_rows():
    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]

    assert payload["meta"]["schema_version"] == 2
    assert item["ai_advice"]["provider"] == "analysis_report"
    assert item["ai_advice"]["stop_loss_price"] == 62.0
    assert item["ai_advice"]["suggested_buy_price"] == 66.0
    assert item["ai_advice"]["suggested_sell_price"] == 67.42
    assert item["ai_advice"]["target_price"] == 70.27

    rows = {row["key"]: row for row in item["price_plan"]["rows"]}
    assert rows["stop"]["active_price"] == 62.0
    assert rows["stop"]["active_source"] == "report"
    assert rows["target"]["active_price"] == 70.27
    assert rows["sell"]["active_price"] == 67.42
    assert rows["buy"]["active_price"] == 66.0
    assert payload["data"]["summary"]["report_price_plan_count"] == 1


def test_build_holdings_payload_prefers_manual_price_plan_over_report_price():
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["manual_target_price"] = 88.0

    payload = build_holdings_payload(db, username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]
    rows = {row["key"]: row for row in item["price_plan"]["rows"]}

    assert rows["target"]["manual_price"] == 88.0
    assert rows["target"]["report_price"] == 70.27
    assert rows["target"]["active_price"] == 88.0
    assert rows["target"]["active_source"] == "manual"


def test_build_holdings_payload_can_filter_by_code_and_market():
    payload = build_holdings_payload(make_fake_db(), username="hermes", code="aapl", market="us")

    assert [item["code"] for item in payload["data"]["items"]] == ["AAPL"]
    assert payload["data"]["summary"]["holding_count"] == 1


def test_build_summary_payload_only_returns_aggregate_data():
    payload = build_summary_payload(make_fake_db(), username="hermes")

    assert payload["ok"] is True
    assert "items" not in payload["data"]
    assert payload["data"]["summary"]["holding_count"] == 2
    assert payload["data"]["summary"]["cash_or_unallocated"] == 48200.0
