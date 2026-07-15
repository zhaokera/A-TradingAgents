import json
import sys
from datetime import datetime

import pytest
from bson import ObjectId
from typer.testing import CliRunner

import app.services.holdings_cli as holdings_cli_module
from app.services.holdings_cli import (
    CLIError,
    _build_a_share_market_gate,
    _market_session_context,
    build_holdings_payload,
    build_opportunities_payload,
    build_record_sale_payload,
    build_summary_payload,
    build_trades_payload,
    build_users_payload,
    holdings_app,
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

    def insert_one(self, doc):
        inserted = dict(doc)
        inserted.setdefault("_id", ObjectId())
        self.docs.append(inserted)

        class Result:
            inserted_id = inserted["_id"]

        return Result()

    def update_one(self, query, update, upsert=False):
        matched = 0
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                matched = 1
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                break

        if matched == 0 and upsert:
            doc = dict(query)
            doc.update(update.get("$set", {}))
            self.docs.append(doc)

        class Result:
            matched_count = matched

        return Result()

    def delete_one(self, query):
        deleted = 0
        for index, doc in enumerate(self.docs):
            if all(doc.get(key) == value for key, value in query.items()):
                del self.docs[index]
                deleted = 1
                break

        class Result:
            deleted_count = deleted

        return Result()


class FakeDB:
    def __init__(self, users, holdings, settings, reports=None, trades=None):
        self.collections = {
            "users": FakeCollection(users),
            "user_holdings": FakeCollection(holdings),
            "user_holding_settings": FakeCollection(settings),
            "user_holding_trades": FakeCollection(trades or []),
            "analysis_reports": FakeCollection(reports or []),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.fixture(autouse=True)
def disable_tencent_quote_fetch(monkeypatch):
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: None)
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_cn_dividend_calendar_sync",
        lambda code: {
            "ok": True,
            "source": "cninfo_via_akshare",
            "code": code,
            "status": "no_upcoming_corporate_action",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": None,
            "nearest_action": None,
            "is_reference_only": True,
        },
        raising=False,
    )


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


def patch_fresh_cli_market_context(monkeypatch, *, report_actionable=True, technical_plan=None):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "source": "tencent",
            "price": 63.36,
            "close": 63.36,
            "open": 63.0,
            "high": 64.0,
            "low": 62.8,
            "trade_at": "2026-07-13T10:00:00+08:00",
            "trade_date": "2026-07-13",
            "received_at": "2026-07-13T02:00:01Z",
        }
        if str(code).upper() != "SH000001"
        else None,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.assess_cn_quote_freshness",
        lambda quote: {
            "actionable": True,
            "status": "fresh",
            "reason": "fresh test quote",
            "source": "tencent",
            "trade_at": quote.get("trade_at"),
            "trade_date": quote.get("trade_date"),
            "age_seconds": 1,
            "session": "morning",
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_daily_bars_sync",
        lambda code, **kwargs: {
            "ok": True,
            "status": "ok",
            "bars": [
                {
                    "date": f"2026-04-{index + 1:02d}" if index < 30 else f"2026-05-{index - 29:02d}",
                    "open": 63.0,
                    "close": 63.0,
                    "high": 64.0,
                    "low": 62.0,
                }
                for index in range(60)
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.merge_tencent_quote_into_bars",
        lambda bars, quote: {"ok": True, "status": "ok", "merge_action": "append", "bars": bars},
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.build_technical_price_plan",
        lambda bars, current_price=None: technical_plan
        or {
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 61.8,
            "suggested_buy_price": 65.8,
            "suggested_sell_price": 67.0,
            "target_price": 70.0,
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.assess_report_freshness",
        lambda *args, **kwargs: {
            "actionable": report_actionable,
            "status": "fresh_report" if report_actionable else "stale_report",
            "started_sessions_after_report": 1 if report_actionable else 2,
            "calendar_source": "tencent_benchmark",
            "calendar_is_fallback": False,
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "average_pct_chg": 0.1,
            "severe_decline_count": 0,
            "moderate_decline_count": 0,
            "indices": [],
            "reason": "stable test market",
            "is_reference_only": True,
        },
        raising=False,
    )


def test_select_user_by_username():
    user = select_user(make_fake_db(), username="hermes")

    assert user["id"] == "665000000000000000000001"
    assert user["username"] == "hermes"
    assert user["email"] == "hermes@example.com"


def test_select_user_defaults_to_admin_without_selector_when_multiple_users():
    admin = {"_id": ObjectId("665000000000000000000001"), "username": "admin", "email": "admin@example.com"}
    user_b = {"_id": ObjectId("665000000000000000000002"), "username": "b", "email": "b@example.com"}
    db = FakeDB([admin, user_b], [], [])

    user = select_user(db)

    assert user["id"] == "665000000000000000000001"
    assert user["username"] == "admin"


def test_select_user_errors_when_default_admin_is_missing():
    user = {"_id": ObjectId("665000000000000000000001"), "username": "hermes", "email": "hermes@example.com"}
    db = FakeDB([user], [], [])

    with pytest.raises(CLIError) as exc_info:
        select_user(db)

    assert exc_info.value.code == "default_admin_not_found"
    assert exc_info.value.exit_code == 3


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


def test_build_holdings_payload_prefers_tencent_realtime_price_for_cn(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {"close": 1300.0, "source": "tencent"},
    )

    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)

    assert payload["data"]["items"][0]["current_price"] == 1300.0
    assert payload["data"]["items"][0]["analysis"]["current_price"] == 1300.0
    assert payload["data"]["summary"]["known_market_value"] == 130000.0


def test_build_holdings_payload_hides_net_rr_failed_report_from_active_rows(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]

    assert payload["meta"]["schema_version"] == 3
    assert item["quote_snapshot"]["trade_at"] == "2026-07-13T10:00:00+08:00"
    assert item["quote_snapshot"]["freshness"]["status"] == "fresh"
    assert item["technical_price_plan"]["source"] == "tencent_qfq_daily"
    assert item["ai_advice"]["provider"] == "analysis_report"
    assert item["ai_advice"]["stop_loss_price"] is None
    assert item["ai_advice"]["suggested_buy_price"] is None
    assert item["ai_advice"]["suggested_sell_price"] is None
    assert item["ai_advice"]["target_price"] is None
    assert item["ai_advice"]["price_plan_status"] == "net_rr_below_1_5"
    assert item["ai_advice"]["historical_report_price_plan"]["stop_loss_price"] == 62.0
    assert item["ai_advice"]["historical_report_price_plan"]["target_price"] == 70.27

    rows = {row["key"]: row for row in item["price_plan"]["rows"]}
    assert all(row["active_price"] is None for row in rows.values())
    assert all(row["active_source"] == "none" for row in rows.values())
    assert payload["data"]["summary"]["report_price_plan_count"] == 0


def test_build_holdings_payload_prefers_manual_price_plan_over_report_price(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["manual_target_price"] = 88.0

    payload = build_holdings_payload(db, username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]
    rows = {row["key"]: row for row in item["price_plan"]["rows"]}

    assert rows["target"]["manual_price"] == 88.0
    assert rows["target"]["report_price"] == 70.27
    assert rows["target"]["active_price"] == 88.0
    assert rows["target"]["active_source"] == "manual"


def test_build_holdings_payload_replaces_stale_report_prices_with_technical(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        report_actionable=False,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 63.0,
            "suggested_buy_price": 66.0,
            "suggested_sell_price": 68.0,
            "target_price": 72.0,
        },
    )

    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]
    rows = {row["key"]: row for row in item["price_plan"]["rows"]}

    assert item["ai_advice"]["report_freshness"]["status"] == "stale_report"
    assert item["ai_advice"]["target_price"] == 72.0
    assert item["ai_advice"]["historical_report_price_plan"]["target_price"] == 70.27
    assert rows["target"]["active_price"] == 72.0
    assert rows["target"]["active_source"] == "technical"
    assert item["price_plan"]["has_report"] is False
    assert item["price_plan"]["has_technical"] is True


def test_build_holdings_payload_marks_stored_price_display_only():
    payload = build_holdings_payload(make_fake_db(), username="hermes", include_analysis=True)
    item = payload["data"]["items"][0]

    assert item["current_price"] == 1200.0
    assert item["quote_snapshot"]["source"] == "stored_holding"
    assert item["quote_snapshot"]["freshness"]["actionable"] is False
    assert item["technical_price_plan"]["status"] == "quote_not_actionable"
    assert item["ai_advice"]["stop_loss_price"] is None
    assert item["ai_advice"]["historical_report_price_plan"]["stop_loss_price"] == 62.0


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


def test_build_record_sale_payload_closes_holding_and_records_realized_pnl():
    db = make_fake_db()
    db.collections["user_holdings"].docs[0].update(
        {
            "code": "000977",
            "name": "浪潮信息",
            "quantity": 100,
            "cost_price": 64.0,
            "market": "CN",
        }
    )
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10000.0

    payload = build_record_sale_payload(
        db,
        username="hermes",
        code="000977",
        quantity=100,
        sell_price=70.4,
        sold_at="2026-07-07T10:00:00+08:00",
    )

    assert payload["ok"] is True
    sale = payload["data"]["sale"]
    assert sale["code"] == "000977"
    assert sale["quantity"] == 100
    assert sale["sell_price"] == 70.4
    assert sale["gross_amount"] == 7040.0
    assert sale["cost_basis"] == 6400.0
    assert sale["realized_pnl"] == 640.0
    assert sale["realized_pnl_pct"] == 10.0
    assert payload["data"]["remaining_holding"] is None
    assert payload["data"]["settings"]["total_assets"] == 10640.0
    assert all(doc["code"] != "000977" for doc in db.collections["user_holdings"].docs)
    assert db.collections["user_holding_trades"].docs[0]["side"] == "sell"
    assert db.collections["user_holding_trades"].docs[0]["realized_pnl"] == 640.0


def test_build_record_sale_payload_normalizes_time_and_applies_nonzero_fees():
    db = make_fake_db()
    db.collections["user_holdings"].docs[0].update(
        {
            "code": "000977",
            "name": "浪潮信息",
            "quantity": 100,
            "cost_price": 64.0,
            "market": "CN",
        }
    )
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10000.0

    payload = build_record_sale_payload(
        db,
        username="hermes",
        code="000977",
        quantity=100,
        sell_price=70.4,
        fee=10.0,
        sold_at="2026-07-07T10:00:00+08:00",
    )
    sale = payload["data"]["sale"]

    assert sale["sold_at"] == "2026-07-07T02:00:00Z"
    assert sale["effective_at"].isoformat() == "2026-07-07T02:00:00+00:00"
    assert sale["gross_amount"] == 7040.0
    assert sale["total_fees"] == 10.0
    assert sale["net_proceeds"] == 7030.0
    assert sale["realized_pnl"] == 630.0
    assert sale["realized_pnl_pct"] == 9.84
    assert payload["data"]["settings"]["total_assets"] == 10630.0


def test_build_record_sale_payload_auto_assets_include_other_holdings_cost():
    db = make_fake_db()
    db.collections["user_holdings"].docs[0].update(
        {
            "code": "000977",
            "name": "浪潮信息",
            "quantity": 100,
            "cost_price": 64.0,
            "market": "CN",
        }
    )
    db.collections["user_holding_settings"].docs[0].pop("total_assets")

    payload = build_record_sale_payload(
        db,
        username="hermes",
        code="000977",
        quantity=100,
        sell_price=70.4,
    )

    # 6400 sold holding cost + 1800 AAPL cost + 640 realized gain.
    assert payload["data"]["settings"]["total_assets"] == 8840.0


def test_build_record_sale_payload_rejects_malformed_sold_at():
    with pytest.raises(CLIError) as exc_info:
        build_record_sale_payload(
            make_fake_db(),
            username="hermes",
            code="600519",
            quantity=100,
            sell_price=1200.0,
            sold_at="not-a-time",
        )

    assert exc_info.value.code == "invalid_sold_at"


def test_build_trades_payload_returns_recent_holding_trades():
    db = make_fake_db()
    db.collections["user_holding_trades"].docs.append(
        {
            "_id": ObjectId("665000000000000000000401"),
            "user_id": "665000000000000000000001",
            "code": "000977",
            "name": "浪潮信息",
            "market": "CN",
            "side": "sell",
            "quantity": 100,
            "sell_price": 70.4,
            "realized_pnl": 640.0,
            "sold_at": "2026-07-07T10:00:00+08:00",
            "created_at": "2026-07-09T06:06:26Z",
        }
    )

    payload = build_trades_payload(db, username="hermes")

    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["id"] == "665000000000000000000401"
    assert payload["data"]["items"][0]["code"] == "000977"
    assert payload["data"]["items"][0]["realized_pnl"] == 640.0


def test_build_trades_payload_sorts_effective_business_time_before_limit():
    db = make_fake_db()
    user_id = "665000000000000000000001"
    db.collections["user_holding_trades"].docs.extend(
        [
            {
                "_id": ObjectId("665000000000000000000411"),
                "user_id": user_id,
                "code": "EARLY",
                "side": "sell",
                "sold_at": "2026-07-01T10:00:00+08:00",
                "created_at": "2026-07-10T10:00:00Z",
            },
            {
                "_id": ObjectId("665000000000000000000412"),
                "user_id": user_id,
                "code": "LATEST",
                "side": "sell",
                "sold_at": "2026-07-09T10:00:00+08:00",
                "created_at": "2026-07-02T10:00:00Z",
            },
            {
                "_id": ObjectId("665000000000000000000413"),
                "user_id": user_id,
                "code": "LEGACY",
                "side": "sell",
                "created_at": "2026-07-08T10:00:00Z",
            },
        ]
    )

    payload = build_trades_payload(db, username="hermes", limit=1)

    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["code"] == "LATEST"


def test_build_opportunities_payload_includes_recent_trade_context(monkeypatch):
    db = make_fake_db()
    db.collections["user_holding_trades"].docs.append(
        {
            "_id": ObjectId("665000000000000000000401"),
            "user_id": "665000000000000000000001",
            "code": "000977",
            "name": "浪潮信息",
            "market": "CN",
            "side": "sell",
            "quantity": 100,
            "sell_price": 70.4,
            "realized_pnl": 640.0,
            "realized_pnl_pct": 10.0,
            "sold_at": "2026-07-07T10:00:00+08:00",
            "created_at": "2026-07-09T06:06:26Z",
        }
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": "000066",
            "name": "中国长城",
            "price": 19.0,
            "pct_chg": -0.5,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])

    trade_context = payload["data"]["trade_context"]
    assert trade_context["recent_count"] == 1
    assert trade_context["recent_realized_pnl"] == 640.0
    assert trade_context["last_trade"]["code"] == "000977"
    assert trade_context["last_trade"]["sell_price"] == 70.4
    assert payload["data"]["brief"]["recent_trade_summary"] == (
        "最近卖出 000977 浪潮信息，成交价 70.40，已实现盈亏 640.00。"
    )


def test_build_opportunities_payload_includes_recent_sale_cooldown_policy(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T14:00:00+08:00",
            "session": "afternoon",
            "is_trading_hours": True,
            "quote_stale_risk": False,
            "minutes_to_close": 60,
            "is_late_session": False,
            "next_refresh_at": None,
            "next_refresh_session": None,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-07", "2026-07-08", "2026-07-09"],
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    db.collections["user_holding_trades"].docs.append(
        {
            "_id": ObjectId("665000000000000000000401"),
            "user_id": "665000000000000000000001",
            "code": "000977",
            "name": "浪潮信息",
            "market": "CN",
            "side": "sell",
            "quantity": 100,
            "sell_price": 70.4,
            "realized_pnl": 640.0,
            "realized_pnl_pct": 10.0,
            "sold_at": "2026-07-07T10:00:00+08:00",
            "created_at": "2026-07-09T06:06:26Z",
        }
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "浪潮信息",
            "price": 68.8,
            "pct_chg": -1.2,
            "turnover_rate": 7.2,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000977"])
    policy = payload["data"]["brief"]["recent_sale_policy"]

    assert policy == {
        "status": "cooldown",
        "cooldown_active": True,
        "code": "000977",
        "name": "浪潮信息",
        "sold_at": "2026-07-07T10:00:00+08:00",
        "sell_price": 70.4,
        "realized_pnl": 640.0,
        "cooldown_horizon": "未来两个交易日",
        "started_sessions_after_sale": 2,
        "calendar_source": "tencent_benchmark",
        "calendar_is_fallback": False,
        "default_action": "avoid_rebuy_chase",
        "matched_candidate_codes": ["000977"],
        "reentry_requirements": [
            "new_analysis_report",
            "refresh_tencent_quotes",
            "low_divergence_confirmation",
        ],
        "note": "最近已卖出该标的，未来两个交易日不把反手追回作为默认动作；仅供研究参考，不构成投资建议或交易指令。",
    }
    assert {
        "step": "respect_recent_sale_cooldown",
        "status": "required",
        "candidate_codes": ["000977"],
        "note": "最近止盈卖出的标的不作为默认回补对象。",
    } in payload["data"]["brief"]["next_refresh_checklist"]


def test_recent_sale_cooldown_blocks_lots_for_matching_candidate(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 67.0,
            "suggested_buy_price": 69.0,
            "suggested_sell_price": 72.0,
            "target_price": 74.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T14:00:00+08:00",
            "session": "afternoon",
            "is_trading_hours": True,
            "quote_stale_risk": False,
            "minutes_to_close": 60,
            "is_late_session": False,
            "next_refresh_at": None,
            "next_refresh_session": None,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0
    db.collections["user_holding_trades"].docs.append(
        {
            "_id": ObjectId("665000000000000000000401"),
            "user_id": "665000000000000000000001",
            "code": "000977",
            "name": "浪潮信息",
            "market": "CN",
            "side": "sell",
            "quantity": 100,
            "sell_price": 70.4,
            "realized_pnl": 640.0,
            "sold_at": "2026-07-07T10:00:00+08:00",
            "created_at": "2026-07-07T02:00:00Z",
        }
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000977"],
        external_risk_level="green",
    )

    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    assert plan_item["suggested_lots"] == 0
    assert plan_item["risk_gate"] == "blocked_by_recent_sale_cooldown"
    assert "recent_sale_cooldown" in plan_item["failed_gates"]


def test_recent_sale_cooldown_expires_after_two_future_trading_days(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 67.0,
            "suggested_buy_price": 69.0,
            "suggested_sell_price": 72.0,
            "target_price": 74.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T14:00:00+08:00",
            "session": "afternoon",
            "is_trading_hours": True,
            "quote_stale_risk": False,
            "minutes_to_close": 60,
            "is_late_session": False,
            "next_refresh_at": None,
            "next_refresh_session": None,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0
    db.collections["user_holding_trades"].docs.append(
        {
            "_id": ObjectId("665000000000000000000401"),
            "user_id": "665000000000000000000001",
            "code": "000977",
            "name": "浪潮信息",
            "market": "CN",
            "side": "sell",
            "quantity": 100,
            "sell_price": 70.4,
            "realized_pnl": 640.0,
            "sold_at": "2026-07-03T10:00:00+08:00",
            "created_at": "2026-07-03T02:00:00Z",
        }
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000977"],
        external_risk_level="green",
    )

    policy = payload["data"]["brief"]["recent_sale_policy"]
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    assert policy["status"] == "expired"
    assert plan_item["suggested_lots"] > 0
    assert "recent_sale_cooldown" not in plan_item["failed_gates"]


def test_recent_sale_cooldown_blocks_all_matching_recent_sales(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 67.0,
            "suggested_buy_price": 69.0,
            "suggested_sell_price": 72.0,
            "target_price": 74.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T14:00:00+08:00",
            "session": "afternoon",
            "is_trading_hours": True,
            "quote_stale_risk": False,
            "minutes_to_close": 60,
            "is_late_session": False,
            "next_refresh_at": None,
            "next_refresh_session": None,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0
    db.collections["user_holding_trades"].docs.extend(
        [
            {
                "_id": ObjectId("665000000000000000000401"),
                "user_id": "665000000000000000000001",
                "code": "000977",
                "name": "浪潮信息",
                "market": "CN",
                "side": "sell",
                "quantity": 100,
                "sell_price": 70.4,
                "realized_pnl": 640.0,
                "sold_at": "2026-07-07T10:00:00+08:00",
                "created_at": "2026-07-07T02:00:00Z",
            },
            {
                "_id": ObjectId("665000000000000000000402"),
                "user_id": "665000000000000000000001",
                "code": "000066",
                "name": "中国长城",
                "market": "CN",
                "side": "sell",
                "quantity": 100,
                "sell_price": 21.0,
                "realized_pnl": 100.0,
                "sold_at": "2026-07-08T10:00:00+08:00",
                "created_at": "2026-07-08T02:00:00Z",
            },
        ]
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000977", "000066"],
        external_risk_level="green",
    )

    policy = payload["data"]["brief"]["recent_sale_policy"]
    plan = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"]
    assert policy["matched_candidate_codes"] == [item["code"] for item in plan]
    assert all(item["suggested_lots"] == 0 for item in plan)
    assert all("recent_sale_cooldown" in item["failed_gates"] for item in plan)


def test_build_opportunities_payload_includes_external_risk_checklist(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.8, "pct_chg": 2.54, "source": "tencent"},
        "600900": {"code": "600900", "name": "长江电力", "price": 27.77, "pct_chg": -0.22, "source": "tencent"},
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066", "600900"])
    checklist = payload["data"]["brief"]["external_risk_checklist"]

    assert checklist == {
        "status": "requires_current_review",
        "horizon": "未来两个交易日",
        "source_policy": "CLI不内置实时国际新闻，Hermes输出前需核查最新可信来源。",
        "candidate_theme_exposure": ["AI算力/信创", "防守/红利低波"],
        "checks": [
            {
                "key": "global_ai_risk_appetite",
                "status": "required",
                "watch": "隔夜美股AI、半导体、纳指或费半表现。",
                "negative_signal": "AI或芯片主线明显回撤。",
                "effect": "降低AI算力和半导体候选优先级。",
            },
            {
                "key": "us_china_policy",
                "status": "required",
                "watch": "中美关税、出口管制、科技制裁或产业政策更新。",
                "negative_signal": "政策摩擦升级并压制科技硬件风险偏好。",
                "effect": "暂停追高科技硬件候选，优先等待分歧收敛。",
            },
            {
                "key": "oil_geopolitics",
                "status": "required",
                "watch": "原油价格、地缘风险和能源供给扰动。",
                "negative_signal": "油价快速上行或地缘风险升级。",
                "effect": "提高防守候选观察权重，降低进攻仓位。",
            },
            {
                "key": "fx_liquidity",
                "status": "required",
                "watch": "美元指数、离岸人民币和外资风险偏好。",
                "negative_signal": "人民币快速走弱或外资风险偏好下降。",
                "effect": "降低首批资金上限或继续空仓等待。",
            },
        ],
        "default_position_effect": "任一必查项出现负面信号时，维持wait或仅观察防守候选。",
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }
    assert payload["data"]["brief"]["next_refresh_checklist"][1] == {
        "step": "review_external_risks",
        "status": "required",
        "checks": ["global_ai_risk_appetite", "us_china_policy", "oil_geopolitics", "fx_liquidity"],
        "note": "输出前结合最新国际形势复核，不直接由静态候选池决定。",
    }


def test_build_opportunities_payload_includes_a_share_market_checklist(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.8, "pct_chg": 2.54, "source": "tencent"},
        "600900": {"code": "600900", "name": "长江电力", "price": 27.77, "pct_chg": -0.22, "source": "tencent"},
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))
    monkeypatch.setattr(
        "app.services.holdings_cli._build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "indices": [],
            "reason": "stable test market",
            "is_reference_only": True,
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066", "600900"])
    checklist = payload["data"]["brief"]["a_share_market_checklist"]

    assert checklist["status"] == "requires_current_review"
    assert checklist["automatic_gate"]["level"] == "green"
    assert checklist["source_policy"] == "CLI自动使用腾讯主要指数和Mongo全市场行情生成市场门禁；宽度数据不足时仍要求Hermes人工确认。"
    assert [item["key"] for item in checklist["checks"]] == [
        "index_breadth",
        "technology_theme_sustainability",
        "hot_money_chase_risk",
        "market_liquidity",
        "defensive_rotation",
    ]
    assert "不构成投资建议" in checklist["disclaimer"]
    assert payload["data"]["brief"]["next_refresh_checklist"][2] == {
        "step": "review_a_share_market_state",
        "status": "required",
        "checks": [
            "index_breadth",
            "technology_theme_sustainability",
            "hot_money_chase_risk",
            "market_liquidity",
            "defensive_rotation",
        ],
        "note": "先确认A股盘面广度和主线延续性，再评估候选股。",
    }


def test_build_opportunities_payload_includes_candidate_decision_matrix(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {
            "code": "000066",
            "name": "中国长城",
            "price": 19.88,
            "high": 20.0,
            "low": 18.18,
            "turnover_rate": 11.25,
            "pct_chg": 2.95,
            "source": "tencent",
        },
        "600900": {
            "code": "600900",
            "name": "长江电力",
            "price": 27.77,
            "high": 27.85,
            "low": 27.53,
            "turnover_rate": 0.47,
            "pct_chg": -0.22,
            "source": "tencent",
        },
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066", "600900"])
    matrix = payload["data"]["brief"]["candidate_decision_matrix"]

    primary_failed_gates = matrix["rows"][0].pop("failed_gates")
    primary_blocking_gates = matrix["rows"][0].pop("blocking_failed_gates")
    assert primary_blocking_gates
    assert set(primary_blocking_gates).issubset(primary_failed_gates)
    assert matrix["rows"][1].pop("failed_gates") == ["observation_only_fallback"]
    assert matrix["rows"][1].pop("blocking_failed_gates") == [
        "observation_only_fallback"
    ]

    assert matrix == {
        "horizon": "未来两个交易日",
        "default_action": "wait",
        "rows": [
            {
                "code": "000066",
                "name": "中国长城",
                "tier": "primary",
                "decision": "blocked",
                "action": "wait",
                "risk_gate": "blocked_by_divergence",
                "suggested_lots": 0,
                "cash_usage_pct": 18.68,
                "required_confirmations": [
                    "refresh_tencent_quotes",
                    "review_external_risks",
                    "review_a_share_market_state",
                    "turnover_rate_below_10",
                    "intraday_range_below_8",
                    "hold_above_invalidation_price",
                ],
                "reason": "高换手或大振幅说明分歧较强，先等分歧收敛和承接确认。",
                "is_reference_only": True,
            },
            {
                "code": "600900",
                "name": "长江电力",
                "tier": "defensive_fallback",
                "decision": "observe_only",
                "action": "observe",
                "risk_gate": "observation_only",
                "suggested_lots": 0,
                "cash_usage_pct": 26.1,
                "required_confirmations": [
                    "refresh_tencent_quotes",
                    "review_external_risks",
                    "review_a_share_market_state",
                    "low_divergence_support",
                ],
                "reason": "低分歧防守备选，仅用于观察，不构成交易指令。",
                "is_reference_only": True,
            },
        ],
        "disclaimer": "仅供研究参考，不构成投资建议或交易指令。",
    }


def test_defensive_fallback_is_observation_only_in_all_outputs(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 27.0,
            "suggested_buy_price": 28.0,
            "suggested_sell_price": 29.0,
            "target_price": 30.5,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "长江电力",
            "source": "tencent",
            "price": 27.77,
            "close": 27.77,
            "open": 27.72,
            "high": 27.85,
            "low": 27.53,
            "turnover_rate": 0.47,
            "pct_chg": -0.22,
            "trade_at": "2026-07-13T10:00:00+08:00",
            "trade_date": "2026-07-13",
            "received_at": "2026-07-13T02:00:01Z",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600900"],
        external_risk_level="green",
    )

    brief = payload["data"]["brief"]
    plan_item = brief["cash_deployment_plan"]["candidate_lot_plan"][0]
    matrix_row = brief["candidate_decision_matrix"]["rows"][0]

    assert brief["fallback_candidates"][0]["code"] == "600900"
    assert plan_item["risk_gate"] == "observation_only"
    assert plan_item["suggested_lots"] == 0
    assert plan_item["suggested_quantity"] == 0
    assert "observation_only_fallback" in plan_item["failed_gates"]
    assert plan_item["risk_sizing"]["suggested_lots"] == 0
    assert plan_item["risk_sizing"]["suggested_quantity"] == 0
    assert plan_item["risk_sizing"]["trade"] is None
    assert plan_item["risk_sizing"]["blocked_by_hard_gate"] is True
    assert matrix_row["risk_gate"] == "observation_only"
    assert matrix_row["suggested_lots"] == 0


def test_candidate_decision_matrix_keeps_secondary_candidates_visible(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.0, "source": "tencent"},
        "002261": {"code": "002261", "name": "拓维信息", "price": 30.8, "source": "tencent"},
        "000938": {"code": "000938", "name": "紫光股份", "price": 34.5, "source": "tencent"},
        "002185": {"code": "002185", "name": "华天科技", "price": 23.0, "source": "tencent"},
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "002261", "000938", "002185"],
    )
    rows = payload["data"]["brief"]["candidate_decision_matrix"]["rows"]
    secondary = next(row for row in rows if row["code"] == "002185")

    assert secondary["tier"] == "secondary"
    assert secondary["decision"] == "blocked"
    assert secondary["action"] == "wait"
    assert secondary["risk_gate"] == "blocked_by_external_risk"
    assert secondary["suggested_lots"] == 0
    assert "resolve_risk_gate" in secondary["required_confirmations"]


def test_corporate_action_blocks_unadjusted_candidate_price_plan(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "XD中国海",
            "price": 27.65,
            "pre_close": 27.87,
            "high": 27.86,
            "low": 27.5,
            "pct_chg": -0.79,
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["600938"])
    candidate = payload["data"]["candidates"][0]
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    matrix_row = payload["data"]["brief"]["candidate_decision_matrix"]["rows"][0]

    assert candidate["quote"]["corporate_action_marker"] == "XD"
    assert candidate["quote"]["price_plan_adjustment_required"] is True
    assert candidate["triggers"]["status"]["position"] == "price_plan_adjustment_required"
    assert {flag["key"] for flag in candidate["risk_flags"]} == {
        "quote_not_actionable",
        "corporate_action_price_adjustment",
    }
    assert plan_item["risk_gate"] == "blocked_by_price_plan_adjustment"
    assert plan_item["suggested_lots"] == 0
    assert matrix_row["decision"] == "blocked"
    assert matrix_row["risk_gate"] == "blocked_by_price_plan_adjustment"
    assert "recalibrate_price_plan" in matrix_row["required_confirmations"]
    assert payload["data"]["brief"]["fallback_candidates"] == []


def test_upcoming_dividend_outside_horizon_is_exposed_without_blocking(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_cn_dividend_calendar_sync",
        lambda code: {
            "ok": True,
            "source": "cninfo_via_akshare",
            "code": code,
            "status": "upcoming_corporate_action",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": 4,
            "nearest_action": {
                "record_date": "2026-07-16",
                "ex_date": "2026-07-17",
                "cash_dividend_per_share": 0.79,
            },
            "is_reference_only": True,
        },
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600900"],
        external_risk_level="green",
    )
    candidate = payload["data"]["candidates"][0]

    assert candidate["corporate_action"]["status"] == "upcoming_corporate_action"
    assert candidate["corporate_action"]["nearest_action"]["ex_date"] == "2026-07-17"
    assert "corporate_action_price_adjustment" not in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    assert "upcoming_corporate_action" in {flag["key"] for flag in candidate["risk_flags"]}


def test_dividend_inside_horizon_reuses_price_adjustment_hard_gate(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_cn_dividend_calendar_sync",
        lambda code: {
            "ok": True,
            "source": "cninfo_via_akshare",
            "code": code,
            "status": "corporate_action_within_horizon",
            "blocks_new_position": True,
            "price_plan_adjustment_required": True,
            "sessions_until_ex_date": 2,
            "nearest_action": {
                "record_date": "2026-07-16",
                "ex_date": "2026-07-17",
                "cash_dividend_per_share": 0.79,
            },
            "is_reference_only": True,
        },
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600900"],
        external_risk_level="green",
    )
    candidate = payload["data"]["candidates"][0]
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert candidate["corporate_action"]["status"] == "corporate_action_within_horizon"
    assert candidate["triggers"]["status"]["position"] == "price_plan_adjustment_required"
    assert "corporate_action_price_adjustment" in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    assert plan_item["risk_gate"] == "blocked_by_price_plan_adjustment"
    assert plan_item["suggested_lots"] == 0


def test_unavailable_corporate_action_calendar_is_explicit_nonblocking_risk(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_cn_dividend_calendar_sync",
        lambda code: {
            "ok": False,
            "source": "cninfo_via_akshare",
            "code": code,
            "status": "corporate_action_unavailable",
            "blocks_new_position": False,
            "price_plan_adjustment_required": False,
            "sessions_until_ex_date": None,
            "nearest_action": None,
            "reason": "cninfo unavailable",
            "is_reference_only": True,
        },
    )

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600900"],
        external_risk_level="green",
    )
    candidate = payload["data"]["candidates"][0]

    assert candidate["corporate_action"]["status"] == "corporate_action_unavailable"
    assert "corporate_action_data_unavailable" in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    assert "corporate_action_price_adjustment" not in {
        flag["key"] for flag in candidate["risk_flags"]
    }


def test_secondary_corporate_action_candidate_keeps_recalibration_gate(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.0, "source": "tencent"},
        "002261": {"code": "002261", "name": "拓维信息", "price": 30.8, "source": "tencent"},
        "000938": {"code": "000938", "name": "紫光股份", "price": 34.5, "source": "tencent"},
        "600938": {"code": "600938", "name": "XD中国海", "price": 27.65, "source": "tencent"},
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "002261", "000938", "600938"],
    )
    brief = payload["data"]["brief"]
    row = next(item for item in brief["candidate_decision_matrix"]["rows"] if item["code"] == "600938")

    assert row["tier"] == "secondary"
    assert row["decision"] == "blocked"
    assert row["action"] == "wait"
    assert row["risk_gate"] == "blocked_by_price_plan_adjustment"
    assert "recalibrate_price_plan" in row["required_confirmations"]
    assert "recalibrate_corporate_action_price_plans" in [
        item["step"] for item in brief["next_refresh_checklist"]
    ]


def test_secondary_hot_move_candidate_is_explicitly_blocked(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.0, "source": "tencent"},
        "002261": {"code": "002261", "name": "拓维信息", "price": 30.8, "source": "tencent"},
        "000938": {"code": "000938", "name": "紫光股份", "price": 34.5, "source": "tencent"},
        "002185": {
            "code": "002185",
            "name": "华天科技",
            "price": 26.1,
            "pct_chg": 9.99,
            "source": "tencent",
        },
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "002261", "000938", "002185"],
    )
    brief = payload["data"]["brief"]
    row = next(item for item in brief["candidate_decision_matrix"]["rows"] if item["code"] == "002185")

    assert row["tier"] == "secondary"
    assert row["decision"] == "blocked"
    assert row["action"] == "wait"
    assert row["risk_gate"] == "blocked_by_hot_move"
    assert "cooldown_after_hot_move" in row["required_confirmations"]
    assert "avoid_hot_move_chase" in [item["step"] for item in brief["next_refresh_checklist"]]


def test_custom_candidate_without_price_plan_is_blocked(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "自定义候选",
            "price": 10.0,
            "pct_chg": 1.0,
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["601398"])
    candidate = payload["data"]["candidates"][0]
    brief = payload["data"]["brief"]
    plan_item = brief["cash_deployment_plan"]["candidate_lot_plan"][0]
    matrix_row = brief["candidate_decision_matrix"]["rows"][0]

    assert "missing_candidate_price_plan" in [flag["key"] for flag in candidate["risk_flags"]]
    assert plan_item["risk_gate"] == "blocked_by_missing_price_plan"
    assert plan_item["suggested_lots"] == 0
    assert matrix_row["decision"] == "blocked"
    assert matrix_row["risk_gate"] == "blocked_by_missing_price_plan"
    assert "build_candidate_price_plan" in [item["step"] for item in brief["next_refresh_checklist"]]


def test_primary_hot_move_candidate_is_blocked(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "华天科技",
            "price": 26.1,
            "pct_chg": 9.99,
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["002185"])
    brief = payload["data"]["brief"]
    plan_item = brief["cash_deployment_plan"]["candidate_lot_plan"][0]
    matrix_row = brief["candidate_decision_matrix"]["rows"][0]

    assert plan_item["risk_gate"] == "blocked_by_hot_move"
    assert plan_item["suggested_lots"] == 0
    assert matrix_row["decision"] == "blocked"
    assert matrix_row["risk_gate"] == "blocked_by_hot_move"


def test_cash_plan_does_not_use_static_entry_reference_as_hard_gate(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.64,
            "pct_chg": -1.21,
            "turnover_rate": 9.46,
            "high": 20.4,
            "low": 19.15,
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    brief = payload["data"]["brief"]
    plan_item = brief["cash_deployment_plan"]["candidate_lot_plan"][0]
    matrix_row = brief["candidate_decision_matrix"]["rows"][0]

    assert plan_item["entry_policy"]["status"] == "wait"
    assert plan_item["entry_policy"]["reference_trigger_status"] == "wait"
    assert plan_item["risk_gate"] == "blocked_by_external_risk"
    assert plan_item["suggested_lots"] == 0
    assert matrix_row["decision"] == "blocked"
    assert matrix_row["risk_gate"] == "blocked_by_external_risk"
    assert brief["action_bias"]["status"] == "wait"
    assert "entry_condition_not_confirmed" not in plan_item["failed_gates"]
    assert "wait_for_entry_confirmation" not in [
        item["step"] for item in brief["next_refresh_checklist"]
    ]


def test_build_record_sale_payload_rejects_quantity_above_holding():
    db = make_fake_db()

    with pytest.raises(CLIError) as exc_info:
        build_record_sale_payload(
            db,
            username="hermes",
            code="600519",
            quantity=101,
            sell_price=1200.0,
        )

    assert exc_info.value.code == "insufficient_holding_quantity"


def test_build_opportunities_payload_includes_cash_fit_candidates_and_risk_flags(monkeypatch):
    quotes = {
        "000066": {
            "code": "000066",
            "name": "中国长城",
            "price": 19.0,
            "pct_chg": -0.5,
            "high": 19.68,
            "low": 18.18,
            "pre_close": 19.31,
            "amount": 3000000000,
            "turnover_rate": 5.0,
            "volume_ratio": 2.3,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
        "000938": {
            "code": "000938",
            "name": "紫光股份",
            "price": 600.0,
            "pct_chg": 4.1,
            "high": 610.0,
            "low": 580.0,
            "pre_close": 576.0,
            "amount": 9000000000,
            "turnover_rate": 9.8,
            "volume_ratio": 2.9,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(
        make_fake_db(),
        username="hermes",
        candidate_codes=["000066", "000938"],
    )

    assert payload["ok"] is True
    account = payload["data"]["account"]
    assert account["cash_or_unallocated"] == 48200.0
    assert account["estimated_equity"] == 168200.0
    assert account["buy_lot_size"] == 100

    candidates = {item["code"]: item for item in payload["data"]["candidates"]}
    assert candidates["000066"]["quote"]["price"] == 19.0
    assert candidates["000066"]["one_lot_amount"] == 1900.0
    assert candidates["000066"]["cash_usage_pct"] == 3.94
    assert candidates["000066"]["affordable_with_cash"] is True
    assert candidates["000066"]["triggers"]["status"]["position"] == "inside_observation_zone"
    assert candidates["000066"]["triggers"]["status"]["distance_to_breakout_pct"] == 3.58
    assert candidates["000066"]["triggers"]["status"]["distance_to_invalidation_pct"] == -4.32
    assert candidates["000938"]["one_lot_amount"] == 60000.0
    assert candidates["000938"]["cash_usage_pct"] == 124.48
    assert candidates["000938"]["affordable_with_cash"] is False
    assert any(flag["key"] == "insufficient_cash" for flag in candidates["000938"]["risk_flags"])
    assert candidates["000938"]["triggers"]["status"]["position"] == "above_observation_zone"

    assert payload["data"]["holdings_risk"][0]["code"] == "600519"
    assert payload["data"]["holdings_risk"][0]["weight_by_estimated_equity_pct"] == 71.34
    assert any(flag["key"] == "high_single_position_weight" for flag in payload["data"]["risk_flags"])
    assert "不构成投资建议" in payload["data"]["disclaimer"]


def test_build_opportunities_payload_marks_same_theme_concentration(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["code"] = "000977"
    db.collections["user_holdings"].docs[0]["name"] = "浪潮信息"
    db.collections["user_holdings"].docs[0]["cost_price"] = 64.0
    db.collections["user_holdings"].docs[0]["current_price"] = 85.99
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.23,
            "pct_chg": -0.41,
            "trade_date": "2026-07-09",
            "source": "tencent",
        }
        if code == "000066"
        else None,
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])

    assert payload["data"]["candidates"][0]["same_theme_with_holdings"] is True
    assert any(flag["key"] == "same_theme_with_holdings" for flag in payload["data"]["candidates"][0]["risk_flags"])
    assert any(flag["key"] == "technology_concentration" for flag in payload["data"]["risk_flags"])


def test_build_opportunities_payload_marks_low_cash_buffer_and_hot_candidate(monkeypatch):
    db = make_fake_db()
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "高位标的",
            "price": 470.0,
            "pct_chg": 10.0,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["123456"])
    candidate = payload["data"]["candidates"][0]

    assert candidate["cash_usage_pct"] == 97.51
    assert candidate["affordable_with_cash"] is True
    assert any(flag["key"] == "low_cash_buffer" for flag in candidate["risk_flags"])
    assert any(flag["key"] == "limit_up_or_hot_move" for flag in candidate["risk_flags"])


def test_build_opportunities_payload_marks_high_turnover_and_wide_intraday_range(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "高分歧标的",
            "price": 19.88,
            "high": 20.0,
            "low": 18.18,
            "turnover_rate": 11.25,
            "pct_chg": 2.95,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(make_fake_db(), username="hermes", candidate_codes=["000066"])
    candidate = payload["data"]["candidates"][0]
    risk_keys = [flag["key"] for flag in candidate["risk_flags"]]

    assert candidate["quote"]["intraday_range_pct"] == 9.15
    assert "high_turnover" in risk_keys
    assert "wide_intraday_range" in risk_keys
    assert payload["data"]["brief"]["top_candidates"][0]["risk_keys"] == risk_keys
    top_level_risk_keys = [flag["key"] for flag in payload["data"]["risk_flags"]]
    assert "candidate_high_turnover" in top_level_risk_keys
    assert "candidate_wide_intraday_range" in top_level_risk_keys


def test_build_opportunities_payload_marks_below_observation_zone(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 18.0,
            "pct_chg": -6.79,
            "trade_date": "2026-07-09",
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(make_fake_db(), username="hermes", candidate_codes=["000066"])
    status = payload["data"]["candidates"][0]["triggers"]["status"]

    assert status["position"] == "below_observation_zone"
    assert status["distance_to_observation_low_pct"] == 4.44
    assert status["distance_to_invalidation_pct"] == 1.0


def test_build_opportunities_payload_includes_brief_for_hermes(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["code"] = "000977"
    db.collections["user_holdings"].docs[0]["name"] = "浪潮信息"
    db.collections["user_holdings"].docs[0]["current_price"] = 85.99
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.0,
            "pct_chg": -0.5,
            "trade_date": "2026-07-09",
            "source": "tencent",
        }
        if code == "000066"
        else None,
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    brief = payload["data"]["brief"]

    assert "账户配置本金 150000.00" in brief["account_summary"]
    assert "浪潮信息" in brief["holding_priority"]
    assert "优先关注持仓风控" in brief["holding_priority"]
    assert brief["top_candidates"][0]["code"] == "000066"
    assert brief["top_candidates"][0]["position"] == "inside_observation_zone"
    assert brief["top_candidates"][0]["breakout_status"] == "below_breakout"
    assert brief["top_candidates"][0]["distance_to_breakout_pct"] == 3.58
    assert brief["top_candidates"][0]["cash_usage_pct"] == 3.94
    assert "technology_concentration" in brief["risk_keys"]
    assert "仅供研究参考" in brief["disclaimer"]


def test_build_opportunities_payload_includes_watch_plan_for_hermes(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["code"] = "000977"
    db.collections["user_holdings"].docs[0]["name"] = "浪潮信息"
    db.collections["user_holdings"].docs[0]["cost_price"] = 64.0
    db.collections["user_holdings"].docs[0]["current_price"] = 85.99
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.0,
            "pct_chg": -0.5,
            "trade_date": "2026-07-09",
            "source": "tencent",
        }
        if code == "000066"
        else None,
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    watch_plan = payload["data"]["brief"]["watch_plan"]

    assert watch_plan["horizon"] == "未来两个交易日"
    assert watch_plan["holding_focus"]["code"] == "000977"
    assert watch_plan["holding_focus"]["priority"] == "protect_profit"
    assert "不构成投资建议" in watch_plan["holding_focus"]["note"]
    assert watch_plan["candidate_focus"][0]["code"] == "000066"
    assert watch_plan["candidate_focus"][0]["condition"] == "观察区内，重点看承接和量能确认。"
    assert watch_plan["candidate_focus"][0]["avoid"] == "不因接近观察区就自动买入。"


def test_build_opportunities_payload_describes_empty_holdings_as_cash_ready(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.8,
            "pct_chg": 2.54,
            "trade_date": "2026-07-09",
            "source": "tencent",
        }
        if code == "000066"
        else None,
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    brief = payload["data"]["brief"]
    holding_focus = brief["watch_plan"]["holding_focus"]

    assert "当前空仓" in brief["holding_priority"]
    assert "确认持仓数据" not in brief["holding_priority"]
    assert holding_focus["priority"] == "cash_deployment"
    assert "空仓" in holding_focus["watch"]
    assert holding_focus["code"] is None


def test_build_opportunities_payload_includes_cash_deployment_plan(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 19.5,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 21.0,
            "target_price": 21.5,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {"code": "000066", "name": "中国长城", "price": 19.8, "pct_chg": 2.54, "source": "tencent"},
        "002261": {"code": "002261", "name": "拓维信息", "price": 32.0, "pct_chg": 3.76, "source": "tencent"},
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "002261"],
        external_risk_level="green",
    )
    plan = payload["data"]["brief"]["cash_deployment_plan"]

    assert payload["meta"]["schema_version"] == 6
    assert "mongo_market_breadth" in payload["meta"]["source"]
    assert "cninfo_dividend_calendar" in payload["meta"]["source"]
    assert payload["data"]["external_risk_gate"]["level"] == "green"
    assert payload["data"]["external_risk_gate"]["max_new_exposure_amount"] == 2128.0
    assert plan["mode"] == "cash_ready"
    assert plan["cash_available"] == 10640.0
    assert plan["initial_deploy_cap_pct"] == 50.0
    assert plan["initial_deploy_cap_amount"] == 5320.0
    assert plan["reserve_cash_pct"] == 50.0
    assert plan["max_single_candidate_pct"] == 35.0
    assert plan["candidate_lot_plan"][0]["code"] == "000066"
    assert plan["candidate_lot_plan"][0]["suggested_lots"] == 1
    assert plan["candidate_lot_plan"][0]["suggested_quantity"] == 100
    assert plan["candidate_lot_plan"][0]["risk_sizing"]["trade"]["net_reward_risk"] >= 1.5
    assert plan["candidate_lot_plan"][0]["one_lot_amount"] == 1980.0
    assert plan["candidate_lot_plan"][0]["within_single_cap"] is True
    assert plan["candidate_lot_plan"][0]["entry_policy"]["status"] == "conditional_guarded_plan"
    assert plan["candidate_lot_plan"][0]["entry_policy"]["reference_trigger_status"] == "watch_after_breakout"
    assert "当前技术入场价" in plan["candidate_lot_plan"][0]["entry_policy"]["confirm"]
    assert plan["candidate_lot_plan"][0]["entry_policy"]["avoid"] == "尾盘拉升、放量回落或跌回突破价下方时先放弃。"
    assert plan["candidate_lot_plan"][0]["entry_policy"]["technical_entry_price"] == 20.0
    assert plan["candidate_lot_plan"][0]["entry_policy"]["technical_stop_price"] == 19.5
    assert plan["candidate_lot_plan"][0]["entry_policy"]["reference_invalidation_price"] == 18.18
    assert plan["candidate_lot_plan"][1]["code"] == "002261"
    assert plan["candidate_lot_plan"][1]["suggested_lots"] == 0
    assert "external_new_exposure_cap" in plan["candidate_lot_plan"][1]["failed_gates"]
    assert plan["candidate_lot_plan"][1]["one_lot_amount"] == 3200.0
    assert plan["candidate_lot_plan"][1]["within_single_cap"] is True
    assert "不构成投资建议" in plan["note"]


def test_candidate_price_plan_is_non_actionable_when_fee_aware_rr_is_below_threshold(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 20.4,
            "target_price": 20.8,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600123"],
        external_risk_level="green",
    )

    candidate = payload["data"]["candidates"][0]
    guarded_plan = candidate["guarded_price_plan"]
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert guarded_plan["actionable"] is False
    assert guarded_plan["status"] == "net_rr_below_1_5"
    assert guarded_plan["fee_aware_trade"]["net_reward_risk"] < 1.5
    assert "net_rr_below_1_5" in guarded_plan["failed_gates"]
    risk_keys = [flag["key"] for flag in candidate["risk_flags"]]
    assert "technical_plan_not_actionable" in risk_keys
    assert "missing_candidate_price_plan" not in risk_keys
    assert plan_item["risk_gate"] == "blocked_by_technical_plan"
    assert plan_item["suggested_lots"] == 0


def test_opportunities_blocks_new_lots_when_a_share_market_regime_is_red(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 19.5,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 21.0,
            "target_price": 21.5,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-13"],
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "red",
            "new_position_allowed": False,
            "max_new_exposure_multiplier": 0.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "average_pct_chg": -3.02,
            "severe_decline_count": 4,
            "moderate_decline_count": 4,
            "indices": [],
            "reason": "systemic decline",
            "is_reference_only": True,
        },
    )

    def quote_for(code):
        symbol = str(code).upper()
        if symbol == "000066":
            return {
                "code": "000066",
                "name": "中国长城",
                "price": 19.8,
                "pct_chg": 2.54,
                "trade_date": "2026-07-13",
                "source": "tencent",
            }
        return None

    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", quote_for)
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert payload["data"]["a_share_market_gate"]["level"] == "red"
    assert plan_item["suggested_lots"] == 0
    assert plan_item["risk_gate"] == "blocked_by_market_regime"
    assert "a_share_market_gate" in plan_item["failed_gates"]
    assert payload["meta"]["schema_version"] == 6


def test_a_share_market_gate_fetches_four_tencent_major_indices(monkeypatch):
    calls = []
    changes = {
        "SH000001": 0.3,
        "SZ399001": -0.2,
        "SZ399006": 0.1,
        "SH000688": 0.2,
    }

    def fake_quote(symbol):
        calls.append(str(symbol).lower())
        normalized = str(symbol).upper()
        return {
            "code": normalized,
            "name": normalized,
            "pct_chg": changes[normalized],
            "trade_date": "2026-07-13",
            "source": "tencent",
        }

    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", fake_quote)

    result = _build_a_share_market_gate("2026-07-13")

    assert calls == ["sh000001", "sz399001", "sz399006", "sh000688"]
    assert result["level"] == "green"
    assert [item["code"] for item in result["indices"]] == calls
    assert result["breadth_confirmation_required"] is True


def test_a_share_market_gate_infers_benchmark_from_aligned_realtime_indices(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda symbol: {
            "code": str(symbol),
            "name": str(symbol),
            "pct_chg": 0.1,
            "trade_date": "2026-07-13",
            "source": "tencent",
        },
    )

    result = _build_a_share_market_gate(None)

    assert result["status"] == "ok"
    assert result["benchmark_trade_date"] == "2026-07-13"
    assert result["trade_date"] == "2026-07-13"
    assert result["breadth_confirmation_required"] is True


def test_a_share_market_gate_combines_mongo_breadth_with_tencent_indices(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda symbol: {
            "code": str(symbol),
            "name": str(symbol),
            "pct_chg": 0.1,
            "trade_date": "2026-07-13",
            "source": "tencent",
        },
    )
    db = make_fake_db()
    rows = []
    for index in range(150):
        rows.append(
            {
                "code": f"600{index:03d}",
                "pct_chg": 1.0,
                "trade_date": "2026-07-13",
            }
        )
    for index in range(800):
        rows.append(
            {
                "code": f"601{index:03d}",
                "pct_chg": -1.0,
                "trade_date": "2026-07-13",
            }
        )
    for index in range(50):
        rows.append(
            {
                "code": f"603{index:03d}",
                "pct_chg": 0.0,
                "trade_date": "2026-07-13",
            }
        )
    db.collections["market_quotes"] = FakeCollection(rows)

    result = _build_a_share_market_gate("2026-07-13", db=db)

    assert result["index_regime"]["level"] == "green"
    assert result["breadth_regime"]["level"] == "red"
    assert result["level"] == "red"
    assert result["new_position_allowed"] is False


def test_opportunities_passes_current_database_to_market_gate(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-13"],
    )
    seen_databases = []

    def fake_market_gate(benchmark_trade_date, db=None):
        seen_databases.append(db)
        return {
            "status": "ok",
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "indices": [],
            "reason": "stable test market",
            "is_reference_only": True,
        }

    monkeypatch.setattr("app.services.holdings_cli._build_a_share_market_gate", fake_market_gate)
    db = make_fake_db()

    build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600123"],
    )

    assert seen_databases == [db]


def test_opportunities_halves_external_exposure_cap_in_yellow_market(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 9.5,
            "suggested_buy_price": 10.0,
            "suggested_sell_price": 11.5,
            "target_price": 12.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-13"],
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "yellow",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 0.5,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "average_pct_chg": -1.2,
            "severe_decline_count": 0,
            "moderate_decline_count": 2,
            "indices": [],
            "reason": "moderate weakness",
            "is_reference_only": True,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": str(code),
            "name": "低价测试",
            "price": 9.8,
            "pct_chg": 0.5,
            "trade_date": "2026-07-13",
            "source": "tencent",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600123"],
        external_risk_level="green",
    )
    plan = payload["data"]["brief"]["cash_deployment_plan"]

    assert plan["external_new_exposure_amount"] == 2128.0
    assert plan["market_adjusted_new_exposure_cap"] == 1064.0
    assert plan["candidate_lot_plan"][0]["suggested_lots"] == 1


def test_opportunities_discovers_default_candidates_from_latest_mongo_snapshot(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-10", "2026-07-13"],
    )
    db = make_fake_db()
    db.collections["market_quotes"] = FakeCollection(
        [
            {
                "code": "600123",
                "close": 5.78,
                "pct_chg": 1.9,
                "amount": 700,
                "turnover_rate": 1.2,
                "trade_date": "2026-07-13",
            }
        ]
    )
    db.collections["stock_basic_info"] = FakeCollection(
        [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}]
    )

    payload = build_opportunities_payload(db, username="hermes")

    assert payload["data"]["candidate_discovery"]["status"] == "ok"
    assert payload["data"]["candidate_discovery"]["benchmark_trade_date"] == "2026-07-13"
    assert payload["data"]["candidate_discovery"]["selected_bucket_counts"] == {
        "pullback": 0,
        "strength": 1,
    }
    assert [item["code"] for item in payload["data"]["candidates"]] == ["600123"]
    assert payload["data"]["candidates"][0]["discovery"]["trade_date"] == "2026-07-13"
    assert payload["data"]["candidates"][0]["triggers"]["source"] == "mongo_dynamic_discovery"
    assert payload["meta"]["schema_version"] == 6


def test_opportunities_fails_closed_when_default_candidate_snapshot_is_stale(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-13"],
    )
    db = make_fake_db()
    db.collections["market_quotes"] = FakeCollection(
        [
            {
                "code": "600123",
                "close": 5.78,
                "pct_chg": 1.9,
                "amount": 700,
                "trade_date": "2026-07-10",
            }
        ]
    )
    db.collections["stock_basic_info"] = FakeCollection(
        [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}]
    )

    payload = build_opportunities_payload(db, username="hermes")

    assert payload["data"]["candidate_discovery"]["status"] == "stale_quote_universe"
    assert payload["data"]["candidate_discovery"]["latest_quote_trade_date"] == "2026-07-10"
    assert payload["data"]["candidates"] == []


def test_opportunities_manual_candidates_bypass_dynamic_discovery(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        "app.services.holdings_cli._benchmark_session_dates",
        lambda: ["2026-07-13"],
    )

    payload = build_opportunities_payload(
        make_fake_db(),
        username="hermes",
        candidate_codes=["000066"],
    )

    assert payload["data"]["candidate_discovery"]["status"] == "manual_candidates"
    assert payload["data"]["candidate_discovery"]["definitions_count"] == 1
    assert [item["code"] for item in payload["data"]["candidates"]] == ["000066"]


def test_opportunities_defaults_external_risk_to_unknown_and_zero_lots(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 19.5,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 21.0,
            "target_price": 21.5,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert payload["data"]["external_risk_gate"]["level"] == "unknown"
    assert payload["data"]["external_risk_gate"]["max_new_exposure_pct"] == 0.0
    assert plan_item["suggested_lots"] == 0
    assert "external_risk_gate" in plan_item["failed_gates"]


def test_opportunities_deduplicates_candidate_codes_before_shared_budget_sizing(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "sz000066", "000066"],
        external_risk_level="green",
    )

    assert [item["code"] for item in payload["data"]["candidates"]] == ["000066"]
    assert [
        item["code"]
        for item in payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"]
    ] == ["000066"]


def test_opportunities_sum_duplicate_existing_symbol_exposure(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    db = make_fake_db()
    for holding in db.collections["user_holdings"].docs:
        holding.update(
            {
                "code": "000066",
                "name": "中国长城",
                "market": "CN",
                "quantity": 200,
                "cost_price": 20.0,
            }
        )
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )

    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    assert plan_item["risk_sizing"]["constraints"]["existing_symbol_market_value"] == 25344.0
    assert plan_item["suggested_lots"] == 0
    assert "post_trade_symbol_cap" in plan_item["failed_gates"]


def test_opportunities_sizes_all_candidates_but_keeps_top_three_action_focus(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066", "002261", "000938", "002185"],
        external_risk_level="green",
    )

    plan = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"]
    assert [item["code"] for item in plan] == ["000066", "002261", "000938", "002185"]
    assert all("risk_sizing" in item for item in plan)
    assert len(payload["data"]["brief"]["top_candidates"]) == 3
    assert len(payload["data"]["brief"]["watch_plan"]["candidate_focus"]) == 3


def test_static_candidate_trigger_does_not_block_fresh_technical_plan(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "source": "tencent",
            "price": 10.0,
            "trade_at": "2026-07-13T10:00:00+08:00",
            "trade_date": "2026-07-13",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )

    candidate = payload["data"]["candidates"][0]
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    assert candidate["triggers"]["is_reference_only"] is True
    assert candidate["triggers"]["source"] == "configured_historical_reference"
    assert plan_item["suggested_lots"] > 0
    assert "entry_condition_not_confirmed" not in plan_item["failed_gates"]


def test_hard_gate_clears_nested_risk_sizing_recommendation(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "华天科技",
            "source": "tencent",
            "price": 26.1,
            "pct_chg": 9.99,
            "trade_at": "2026-07-13T10:00:00+08:00",
            "trade_date": "2026-07-13",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["002185"],
        external_risk_level="green",
    )

    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]
    assert plan_item["risk_gate"] == "blocked_by_hot_move"
    assert plan_item["suggested_lots"] == 0
    assert plan_item["risk_sizing"]["suggested_lots"] == 0
    assert plan_item["risk_sizing"]["suggested_quantity"] == 0
    assert plan_item["risk_sizing"]["trade"] is None
    assert plan_item["risk_sizing"]["blocked_by_hard_gate"] is True


def test_opportunities_yellow_cap_and_account_loss_budget_fail_closed(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 18.0,
            "suggested_buy_price": 20.0,
            "suggested_sell_price": 23.0,
            "target_price": 24.0,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="yellow",
    )
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert payload["data"]["external_risk_gate"]["max_new_exposure_amount"] == 1276.8
    assert plan_item["suggested_lots"] == 0
    assert "external_new_exposure_cap" in plan_item["failed_gates"]
    assert "account_loss_budget" in plan_item["failed_gates"]
    assert plan_item["blocking_failed_gates"] == [
        "external_new_exposure_cap",
        "account_loss_budget",
    ]
    decision_row = payload["data"]["brief"]["candidate_decision_matrix"]["rows"][0]
    assert "external_new_exposure_cap" in decision_row["failed_gates"]
    assert decision_row["blocking_failed_gates"] == [
        "external_new_exposure_cap",
        "account_loss_budget",
    ]


def test_opportunities_rejects_invalid_external_risk_level():
    with pytest.raises(CLIError) as exc_info:
        build_opportunities_payload(
            make_fake_db(),
            username="hermes",
            candidate_codes=["000066"],
            external_risk_level="orange",
        )

    assert exc_info.value.code == "invalid_external_risk_level"


def test_opportunities_command_returns_json_for_invalid_external_risk(monkeypatch):
    monkeypatch.setattr("app.services.holdings_cli._get_database", make_fake_db)

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--username", "hermes", "--external-risk-level", "orange"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload == {
        "ok": False,
        "error": {
            "code": "invalid_external_risk_level",
            "message": "external risk level must be green, yellow, red, or unknown",
        },
    }


def test_opportunities_command_validates_external_risk_before_database_access(monkeypatch):
    def fail_if_database_is_opened():
        raise AssertionError("database must not be opened for invalid CLI input")

    monkeypatch.setattr("app.services.holdings_cli._get_database", fail_if_database_is_opened)

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--username", "hermes", "--external-risk-level", "orange"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_external_risk_level"


def test_opportunities_help_exposes_external_risk_and_removes_lot_override():
    result = CliRunner().invoke(holdings_app, ["opportunities", "--help"])

    assert result.exit_code == 0
    assert "--external-risk-level" in result.stdout
    assert "--lot-size" not in result.stdout


def test_cli_help_exposes_database_optional_market_status_command():
    result = CliRunner().invoke(holdings_app, ["--help"])

    assert result.exit_code == 0
    assert "market-status" in result.stdout
    assert "无需登录" in result.stdout


def test_summary_command_suppresses_provider_console_noise(monkeypatch):
    monkeypatch.setattr(holdings_cli_module, "_get_database", make_fake_db)

    def noisy_summary(*_args, **_kwargs):
        print("provider progress", file=sys.stderr)
        return {
            "ok": True,
            "data": {"summary": {"total_assets": 10000.0}},
            "meta": {"schema_version": 1},
        }

    monkeypatch.setattr(holdings_cli_module, "build_summary_payload", noisy_summary)

    result = CliRunner().invoke(holdings_app, ["summary"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["data"]["summary"]["total_assets"] == 10000.0


def test_build_market_status_payload_waits_when_mongo_breadth_is_unavailable(monkeypatch):
    seen_databases = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: (_ for _ in ()).throw(
            AssertionError("market-status must not call the historical calendar endpoint")
        ),
    )

    def fake_market_gate(benchmark_trade_date, db=None):
        assert benchmark_trade_date is None
        seen_databases.append(db)
        return {
            "status": "ok",
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
            "benchmark_trade_date": "2026-07-13",
            "trade_date": "2026-07-13",
            "indices": [{"code": "sh000001", "pct_chg": 0.3}],
            "breadth_regime": {
                "status": "market_breadth_unavailable",
                "level": "unknown",
                "source": "mongo.market_quotes",
            },
            "breadth_confirmation_required": True,
            "reason": "指数稳定，但市场宽度仍需确认。",
        }

    monkeypatch.setattr(holdings_cli_module, "_build_a_share_market_gate", fake_market_gate)

    payload = holdings_cli_module.build_market_status_payload(
        db=None,
        database_status={"status": "unavailable", "error_code": "database_error"},
    )

    assert seen_databases == [None]
    assert payload["ok"] is True
    assert payload["data"]["data_completeness"] == "indices_only"
    assert payload["data"]["database"] == {
        "status": "unavailable",
        "error_code": "database_error",
    }
    assert payload["data"]["decision"]["action"] == "wait"
    assert payload["data"]["decision"]["actionable"] is False
    assert payload["data"]["decision"]["reason_code"] == "breadth_confirmation_required"
    assert payload["meta"]["schema_version"] == 1


def test_market_status_command_falls_back_to_indices_when_database_is_down(monkeypatch):
    monkeypatch.setattr(
        holdings_cli_module,
        "_get_database",
        lambda: (_ for _ in ()).throw(
            CLIError("MongoDB connection refused", code="database_error", exit_code=4)
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: ["2026-07-13"],
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "red",
            "new_position_allowed": False,
            "max_new_exposure_multiplier": 0.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "indices": [],
            "breadth_regime": {"status": "market_breadth_unavailable"},
            "breadth_confirmation_required": True,
            "reason": "主要指数出现系统性下跌。",
        },
    )

    result = CliRunner().invoke(holdings_app, ["market-status"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["market_gate"]["level"] == "red"
    assert payload["data"]["database"]["status"] == "unavailable"
    assert payload["data"]["database"]["error_code"] == "database_error"
    assert "connection refused" not in result.stdout.lower()


def test_build_opportunities_payload_requires_quote_refresh_after_close(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {"code": code, "name": "中国长城", "price": 19.8, "pct_chg": 2.54, "source": "tencent"},
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T15:10:00+08:00",
            "session": "closed",
            "is_trading_hours": False,
            "quote_stale_risk": True,
            "minutes_to_close": None,
            "is_late_session": False,
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    plan = payload["data"]["brief"]["cash_deployment_plan"]

    assert plan["requires_quote_refresh"] is True
    assert plan["execution_window"] == "next_trading_session"
    assert plan["plan_status"] == "pending_quote_refresh"
    assert "下一交易时段刷新腾讯行情" in plan["quote_refresh_reason"]
    assert plan["candidate_lot_plan"][0]["activation_condition"] == "refresh_quote_before_action"


def test_cash_deployment_plan_blocks_high_divergence_candidate(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "高分歧标的",
            "price": 19.88,
            "high": 20.0,
            "low": 18.18,
            "turnover_rate": 11.25,
            "pct_chg": 2.95,
            "source": "tencent",
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli._market_session_context",
        lambda: {
            "market": "CN",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-07-09T15:10:00+08:00",
            "session": "closed",
            "is_trading_hours": False,
            "quote_stale_risk": True,
            "minutes_to_close": None,
            "is_late_session": False,
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    plan_item = payload["data"]["brief"]["cash_deployment_plan"]["candidate_lot_plan"][0]

    assert plan_item["suggested_lots"] == 0
    assert plan_item["risk_gate"] == "blocked_by_divergence"
    assert plan_item["activation_condition"] == "wait_for_divergence_cooldown"
    assert "高换手或大振幅" in plan_item["reason"]
    assert plan_item["cooldown_checks"] == {
        "max_turnover_rate": 10.0,
        "max_intraday_range_pct": 8.0,
        "must_refresh_quote": True,
        "must_hold_above_invalidation_price": 18.18,
    }
    assert plan_item["cooldown_evaluation"] == {
        "evaluation_status": "stale_until_refresh",
        "actionable": False,
        "passed": False,
        "failed_checks": ["turnover_rate", "intraday_range_pct"],
        "current_turnover_rate": 11.25,
        "current_intraday_range_pct": 9.15,
        "current_price": 19.88,
    }


def test_build_opportunities_payload_summarizes_wait_bias_when_candidates_blocked(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "高分歧标的",
            "price": 19.88,
            "high": 20.0,
            "low": 18.18,
            "turnover_rate": 11.25,
            "pct_chg": 2.95,
            "source": "tencent",
        },
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    action_bias = payload["data"]["brief"]["action_bias"]

    assert action_bias["status"] == "wait"
    assert action_bias["primary_reason"] == "top_candidates_blocked_by_divergence"
    assert action_bias["next_step"] == "等待分歧收敛并在下一交易时段刷新腾讯行情。"


def test_build_opportunities_payload_includes_low_divergence_fallback_candidates(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10640.0
    quotes = {
        "000066": {
            "code": "000066",
            "name": "中国长城",
            "price": 19.88,
            "high": 20.0,
            "low": 18.18,
            "turnover_rate": 11.25,
            "pct_chg": 2.95,
            "source": "tencent",
        },
        "600900": {
            "code": "600900",
            "name": "长江电力",
            "price": 27.77,
            "high": 27.85,
            "low": 27.53,
            "turnover_rate": 0.47,
            "pct_chg": -0.22,
            "source": "tencent",
        },
    }
    monkeypatch.setattr("app.services.holdings_cli.fetch_tencent_quote_sync", lambda code: quotes.get(code))

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066", "600900"])
    fallback = payload["data"]["brief"]["fallback_candidates"]
    action_bias = payload["data"]["brief"]["action_bias"]

    assert fallback == [
        {
            "code": "600900",
            "name": "长江电力",
            "theme_label": "防守/红利低波",
            "position": "inside_observation_zone",
            "price": 27.77,
            "cash_usage_pct": 26.1,
            "actionable": False,
            "watch_condition": "观察区内，刷新行情后确认低分歧承接。",
            "reason": "低分歧防守备选，仅用于观察，不构成交易指令。",
        }
    ]
    assert action_bias["secondary_focus"] == {
        "status": "observe_fallback_candidates",
        "candidate_codes": ["600900"],
        "note": "防守备选仅用于观察，不构成交易指令。",
    }
    assert payload["data"]["brief"]["next_refresh_checklist"] == [
        {
            "step": "refresh_tencent_quotes",
            "status": "required",
            "note": "下一交易时段先刷新腾讯行情。",
        },
        {
            "step": "review_external_risks",
            "status": "required",
            "checks": ["global_ai_risk_appetite", "us_china_policy", "oil_geopolitics", "fx_liquidity"],
            "note": "输出前结合最新国际形势复核，不直接由静态候选池决定。",
        },
        {
            "step": "review_a_share_market_state",
            "status": "required",
            "checks": [
                "index_breadth",
                "technology_theme_sustainability",
                "hot_money_chase_risk",
                "market_liquidity",
                "defensive_rotation",
            ],
            "note": "先确认A股盘面广度和主线延续性，再评估候选股。",
        },
        {
            "step": "evaluate_primary_cooldown",
            "status": "required",
            "candidate_codes": ["000066"],
            "checks": ["turnover_rate", "intraday_range_pct", "invalidation_price"],
        },
        {
            "step": "observe_fallback_candidates",
            "status": "optional",
            "candidate_codes": ["600900"],
            "note": "仅观察防守备选，不构成交易指令。",
        },
    ]


def test_build_opportunities_payload_marks_candidate_above_breakout(monkeypatch):
    db = make_fake_db()
    db.collections["user_holdings"].docs[0]["code"] = "000977"
    db.collections["user_holdings"].docs[0]["name"] = "浪潮信息"
    db.collections["user_holdings"].docs[0]["cost_price"] = 64.0
    db.collections["user_holdings"].docs[0]["current_price"] = 85.99
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "中国长城",
            "price": 19.8,
            "pct_chg": 2.54,
            "trade_date": "2026-07-09",
            "source": "tencent",
        }
        if code == "000066"
        else None,
    )

    payload = build_opportunities_payload(db, username="hermes", candidate_codes=["000066"])
    candidate = payload["data"]["candidates"][0]
    watch_item = payload["data"]["brief"]["watch_plan"]["candidate_focus"][0]

    assert candidate["triggers"]["status"]["position"] == "above_observation_zone"
    assert candidate["triggers"]["status"]["breakout_status"] == "above_breakout"
    assert watch_item["condition"] == "已站上突破价，观察能否站稳突破位并避免放量回落。"


def test_market_session_context_marks_lunch_break_quotes_as_stale_risk():
    now = datetime(2026, 7, 9, 12, 36)

    context = _market_session_context(now)

    assert context["timezone"] == "Asia/Shanghai"
    assert context["session"] == "lunch_break"
    assert context["is_trading_hours"] is False
    assert context["quote_stale_risk"] is True


def test_market_session_context_marks_late_afternoon_risk():
    now = datetime(2026, 7, 9, 14, 55)

    context = _market_session_context(now)

    assert context["session"] == "afternoon"
    assert context["is_trading_hours"] is True
    assert context["minutes_to_close"] == 5
    assert context["is_late_session"] is True


def test_market_session_context_schedules_next_refresh_after_close():
    now = datetime(2026, 7, 9, 15, 10)

    context = _market_session_context(now)

    assert context["session"] == "closed"
    assert context["next_refresh_at"] == "2026-07-10T09:30:00+08:00"
    assert context["next_refresh_session"] == "next_open"


def test_market_session_context_schedules_next_refresh_after_lunch_break():
    now = datetime(2026, 7, 9, 12, 36)

    context = _market_session_context(now)

    assert context["session"] == "lunch_break"
    assert context["next_refresh_at"] == "2026-07-09T13:00:00+08:00"
    assert context["next_refresh_session"] == "afternoon"
