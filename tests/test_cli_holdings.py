import json
import logging
import signal
import subprocess
import sys
import textwrap
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pymongo
from bson import ObjectId
from click import unstyle
from typer.testing import CliRunner

import app.services.holdings_cli as holdings_cli_module
import app.services.opportunity_market_context as opportunity_context_module
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
from app.services.opportunity_market_context import (
    OpportunityMarketContext,
    build_opportunity_market_context,
)
from app.services.tencent_quote_service import parse_tencent_quote_batch_payload


REAL_BOUNDED_TENCENT_FETCHER = (
    opportunity_context_module.fetch_tencent_market_context_bounded
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
        "app.services.opportunity_market_context.fetch_tencent_quotes_sync",
        lambda codes, *, timeout: {
            "status": "fetch_error",
            "requested_codes": list(codes),
            "rows": [],
            "error_type": "disabled_for_tests",
        },
    )
    monkeypatch.setattr(
        "app.services.opportunity_market_context.fetch_tencent_market_context_bounded",
        lambda *, timeout_seconds: {
            "status": "fetch_error",
            "requested_codes": list(
                holdings_cli_module.A_SHARE_REGIME_INDEX_SYMBOLS
            ),
            "rows": [],
            "error_type": "disabled_for_tests",
        },
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.opportunity_market_context.fetch_sina_public_market_snapshot",
        lambda **_kwargs: {
            "status": "public_breadth_fetch_disabled_for_tests",
            "source": "akshare.sina.stock_zh_a_spot",
            "rows": [],
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_sina_public_market_breadth",
        lambda **_kwargs: {
            "status": "public_breadth_fetch_disabled_for_tests",
            "source": "akshare.sina.stock_zh_a_spot",
            "rows": [],
        },
    )
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
    monkeypatch.setattr(
        "app.services.holdings_cli.screen_public_candidate_earnings_risk",
        lambda codes, *, benchmark_trade_date: _make_public_earnings_screen(
            list(codes),
            benchmark_trade_date=benchmark_trade_date,
        ),
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


def make_opportunity_market_context(
    *,
    public_snapshot_fetcher=None,
    monotonic=lambda: 0.0,
):
    index_quotes = [
        {
            "code": symbol[2:],
            "provider_symbol": symbol,
            "requested_symbol": symbol,
            "parse_status": "ok",
            "pct_chg": 0.1,
            "trade_date": "2026-07-17",
            "source": "tencent",
        }
        for symbol in holdings_cli_module.A_SHARE_REGIME_INDEX_SYMBOLS
    ]
    return OpportunityMarketContext(
        now=datetime(2026, 7, 17, 10, 0),
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=index_quotes,
        benchmark_trade_date="2026-07-17",
        index_status="ok",
        monotonic=monotonic,
        public_snapshot_fetcher=public_snapshot_fetcher,
    )


def make_hydrated_opportunity_market_context(*, public_snapshot_fetcher=None):
    context = make_opportunity_market_context(
        public_snapshot_fetcher=public_snapshot_fetcher,
    )
    context.public_snapshot_loaded = True
    context.public_snapshot = {
        "status": "ok",
        "source": "akshare.sina.stock_zh_a_spot",
        "provider_trade_date": "2026-07-17",
        "provider_time": "2026-07-17T10:00:00+08:00",
        "rows": [
            {
                "code": f"600{index:03d}",
                "name": f"市场样本{index}",
                "pct_chg": 0.1,
                "trade_date": "2026-07-17",
            }
            for index in range(500)
        ],
    }
    return context


def _make_tencent_index_assignment(symbol, *, pct_chg="0.10"):
    fields = ["0"] * 50
    fields[1] = symbol
    fields[2] = symbol[2:]
    fields[3] = "100.00"
    fields[4] = "99.90"
    fields[5] = "99.95"
    fields[30] = "20260717100000"
    fields[31] = "0.10"
    fields[32] = pct_chg
    return f'v_{symbol}="{"~".join(fields)}";'


def _patch_structured_tencent_worker_failure(monkeypatch):
    stderr_handlers = []

    def fake_run(command, **_kwargs):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("worker-log: %(message)s"))
        opportunity_context_module.logger.addHandler(handler)
        stderr_handlers.append(handler)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "index_fetch_failed",
                    "requested_codes": list(
                        holdings_cli_module.A_SHARE_REGIME_INDEX_SYMBOLS
                    ),
                    "rows": [],
                    "error_type": "RuntimeError",
                }
            ),
            stderr="worker provider failure",
        )

    monkeypatch.setattr(opportunity_context_module.logger, "level", logging.WARNING)
    monkeypatch.setattr(opportunity_context_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        opportunity_context_module,
        "fetch_tencent_market_context_bounded",
        REAL_BOUNDED_TENCENT_FETCHER,
    )
    return stderr_handlers


def patch_fresh_cli_market_context(
    monkeypatch,
    *,
    report_actionable=True,
    technical_plan=None,
    pullback_plan=None,
):
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
        "app.services.holdings_cli.build_pullback_price_plan",
        lambda bars, current_price=None: pullback_plan
        or {
            "actionable": False,
            "status": "pullback_too_far",
            "entry_strategy": "pullback",
            "failed_gates": ["pullback_too_far"],
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


def test_research_candidates_keep_reference_technical_plan_off_session(monkeypatch):
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda code: {"code": code, "name": "中国电信", "price": 5.84, "source": "tencent"},
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "assess_cn_quote_freshness",
        lambda _snapshot: {
            "actionable": False,
            "status": "off_session",
            "reason": "午间休市，仅供研究展示。",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_daily_bars_sync",
        lambda _code: {"ok": True, "status": "ok", "bars": [{"date": "2026-07-14"}]},
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "merge_tencent_quote_into_bars",
        lambda bars, _snapshot: {"ok": True, "bars": bars, "merge_action": "append"},
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_technical_price_plan",
        lambda _bars, current_price: {
            "actionable": True,
            "status": "ok",
            "current_price": current_price,
            "stop_loss_price": 5.6,
            "suggested_buy_price": 5.84,
            "target_price": 6.3,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "apply_net_reward_risk_gate",
        lambda plan, quantity: {**plan, "fee_aware_quantity": quantity},
    )

    candidates = holdings_cli_module._build_opportunity_candidates(
        holdings_cli_module._candidate_definitions(["601728"]),
        cash=None,
        buy_lot_size=100,
        holding_themes=set(),
        allow_reference_price_plan=True,
    )

    plan = candidates[0]["guarded_price_plan"]
    assert plan["suggested_buy_price"] == 5.84
    assert plan["reference_actionable"] is True
    assert plan["actionable"] is False
    assert plan["execution_blocked_by"] == ["quote_freshness", "account_data_unavailable"]
    assert plan["quote_status"] == "off_session"


def test_account_manual_candidate_keeps_reference_technical_plan_off_session(
    monkeypatch,
):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 60.0,
            "suggested_buy_price": 63.0,
            "suggested_sell_price": 68.0,
            "target_price": 70.0,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "assess_cn_quote_freshness",
        lambda quote: {
            "actionable": False,
            "status": "off_session",
            "reason": "休市行情仅供下一交易日研究。",
            "source": "tencent",
            "trade_at": quote.get("trade_at"),
            "trade_date": quote.get("trade_date"),
            "age_seconds": 3600,
            "session": "closed",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10685.41

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["601728"],
        external_risk_level="red",
    )

    candidate = payload["data"]["candidates"][0]
    plan = candidate["guarded_price_plan"]
    assert plan["reference_actionable"] is True
    assert plan["actionable"] is False
    assert plan["quote_status"] == "off_session"
    assert plan["execution_blocked_by"] == ["quote_freshness"]
    assert plan["suggested_buy_price"] == 63.0
    assert plan["stop_loss_price"] == 60.0
    assert plan["target_price"] == 70.0
    assert not {
        "entry_order",
        "stop_order",
        "target_order",
    }.intersection(plan["fee_aware_trade"])
    assert "missing_candidate_price_plan" not in {
        flag["key"] for flag in candidate["risk_flags"]
    }

    lot_plan = payload["data"]["brief"]["cash_deployment_plan"][
        "candidate_lot_plan"
    ][0]
    assert lot_plan["executable_price_tuple"] == {
        "entry": 63.0,
        "stop": 60.0,
        "target": 70.0,
    }
    assert lot_plan["activation_condition"] == "refresh_quote_before_action"
    assert lot_plan["suggested_lots"] == 0
    assert "quote_freshness" in lot_plan["blocking_failed_gates"]
    assert "external_risk_gate" in lot_plan["blocking_failed_gates"]


def test_opportunity_candidates_reuse_injected_quote_without_single_quote_fetch(
    monkeypatch,
):
    definitions = holdings_cli_module._candidate_definitions(["601728"])
    quote_snapshots = {
        "601728": {
            "code": "601728",
            "name": "中国电信",
            "close": 5.84,
            "pct_chg": 0.35,
            "source": "tencent_batch_quotes",
            "provider_metadata": {"request_id": "keep-me"},
        }
    }
    original_quote_snapshots = deepcopy(quote_snapshots)
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _code: (_ for _ in ()).throw(
            AssertionError("an injected quote must not be fetched again")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "assess_cn_quote_freshness",
        lambda _snapshot: {"actionable": False, "status": "off_session"},
    )

    candidates = holdings_cli_module._build_opportunity_candidates(
        definitions,
        cash=None,
        buy_lot_size=100,
        holding_themes=set(),
        quote_snapshots=quote_snapshots,
    )

    assert candidates[0]["quote"]["price"] == 5.84
    assert candidates[0]["quote"]["source"] == "tencent_batch_quotes"
    assert quote_snapshots == original_quote_snapshots


@pytest.mark.parametrize("injected_quote", [{}, None, "invalid-row"])
def test_opportunity_candidates_do_not_refetch_present_invalid_injected_quote(
    monkeypatch,
    injected_quote,
):
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _code: (_ for _ in ()).throw(
            AssertionError("a present quote-map key must never fall back to a live fetch")
        ),
    )

    candidates = holdings_cli_module._build_opportunity_candidates(
        holdings_cli_module._candidate_definitions(["601728"]),
        cash=None,
        buy_lot_size=100,
        holding_themes=set(),
        quote_snapshots={"601728": injected_quote},
    )

    assert candidates[0]["quote"]["price"] is None
    assert candidates[0]["quote"]["freshness"]["actionable"] is False


@pytest.mark.parametrize(
    "quote_snapshots",
    [None, {"600000": {"code": "600000", "close": 10.0}}],
)
def test_opportunity_candidates_keep_legacy_fetch_when_injected_code_is_absent(
    monkeypatch,
    quote_snapshots,
):
    calls = []

    def fake_fetch(code):
        calls.append(code)
        return {
            "code": code,
            "name": "中国电信",
            "close": 5.84,
            "source": "tencent",
        }

    monkeypatch.setattr(holdings_cli_module, "fetch_tencent_quote_sync", fake_fetch)
    monkeypatch.setattr(
        holdings_cli_module,
        "assess_cn_quote_freshness",
        lambda _snapshot: {"actionable": False, "status": "off_session"},
    )

    candidates = holdings_cli_module._build_opportunity_candidates(
        holdings_cli_module._candidate_definitions(["601728"]),
        cash=None,
        buy_lot_size=100,
        holding_themes=set(),
        quote_snapshots=quote_snapshots,
    )

    assert calls == ["601728"]
    assert candidates[0]["quote"]["price"] == 5.84


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

    assert payload["meta"]["schema_version"] == 7
    assert "mongo_market_breadth" in payload["meta"]["source"]
    assert "cninfo_dividend_calendar" in payload["meta"]["source"]
    assert payload["data"]["external_risk_gate"]["level"] == "green"
    assert payload["data"]["external_risk_gate"]["max_new_exposure_amount"] == 6384.0
    assert plan["mode"] == "cash_ready"
    assert plan["cash_available"] == 10640.0
    assert plan["initial_deploy_cap_pct"] == 60.0
    assert plan["initial_deploy_cap_amount"] == 6384.0
    assert plan["reserve_cash_pct"] == 40.0
    assert plan["max_single_candidate_pct"] == 40.0
    assert plan["preferred_single_candidate_pct"] == 35.0
    assert plan["total_loss_budget"] == 212.8
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
    assert plan["candidate_lot_plan"][1]["suggested_lots"] == 1
    assert plan["candidate_lot_plan"][1]["one_lot_amount"] == 3200.0
    assert plan["candidate_lot_plan"][1]["within_single_cap"] is True
    assert "不构成投资建议" in plan["note"]


def test_build_opportunities_payload_reports_public_breadth_source(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, db=None: {
            "status": "ok",
            "level": "yellow",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 0.5,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "indices": [],
            "breadth_regime": {
                "status": "ok",
                "level": "yellow",
                "source": "akshare.sina.stock_zh_a_spot",
            },
            "breadth_confirmation_required": False,
            "reason": "市场宽度偏弱，新仓风险预算减半。",
        },
    )

    payload = build_opportunities_payload(
        make_fake_db(),
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="red",
    )

    assert "akshare_sina_public_breadth" in payload["meta"]["source"]
    assert "mongo_market_breadth" not in payload["meta"]["source"]


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


def test_candidate_uses_fee_aware_pullback_when_breakout_rr_is_too_low(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 60.0,
            "suggested_buy_price": 64.0,
            "suggested_sell_price": 64.4,
            "target_price": 64.8,
        },
        pullback_plan={
            "actionable": True,
            "status": "ok",
            "entry_strategy": "pullback",
            "source": "tencent_qfq_daily",
            "current_price": 63.36,
            "stop_loss_price": 61.5,
            "suggested_buy_price": 63.0,
            "suggested_sell_price": 64.5,
            "target_price": 66.5,
            "entry_basis": 62.8,
            "entry_source": "nearest_support_with_buffer",
            "stop_basis": 61.8,
            "stop_source": "next_support_below_2pct",
            "target_source": "observed_resistance",
            "pullback_required": True,
            "distance_to_entry_pct": -0.57,
            "max_pullback_distance_pct": 3.0,
            "trend_context": {"state": "healthy", "recovery_required": False},
            "failed_gates": [],
            "is_reference_only": True,
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

    assert guarded_plan["actionable"] is True
    assert guarded_plan["status"] == "ok"
    assert guarded_plan["entry_strategy"] == "pullback"
    assert guarded_plan["entry_source"] == "nearest_support_with_buffer"
    assert guarded_plan["pullback_required"] is True
    assert guarded_plan["fee_aware_trade"]["net_reward_risk"] >= 1.5
    assert "technical_plan_not_actionable" not in {
        flag["key"] for flag in candidate["risk_flags"]
    }


def test_deadline_objective_cannot_bypass_hard_market_risk_gates(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 61.5,
            "suggested_buy_price": 63.0,
            "suggested_sell_price": 64.5,
            "target_price": 66.5,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, db=None, context=None: {
            "status": "ok",
            "level": "red",
            "new_position_allowed": False,
            "max_new_exposure_multiplier": 0.0,
            "benchmark_trade_date": benchmark_trade_date,
            "trade_date": benchmark_trade_date,
            "indices": [],
            "reason": "test market risk",
            "is_reference_only": True,
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600123", "600124", "600125", "600126"],
        external_risk_level="red",
        target_exposure_pct=60.0,
        deployment_deadline="2099-01-01",
    )

    plan = payload["data"]["brief"]["cash_deployment_plan"]
    objective = plan["deployment_objective"]
    lots = plan["candidate_lot_plan"]

    assert plan["mode"] == "deadline_target"
    assert plan["effective_new_exposure_cap"] == 0.0
    assert plan["total_loss_budget"] == 2000.0
    assert objective["status"] == "target_shortfall"
    assert objective["target_met"] is False
    assert objective["projected_exposure_pct"] == 0.0
    assert sum(item["suggested_lots"] for item in lots) == 0
    assert all("external_risk_gate" in item["failed_gates"] for item in lots)
    assert all("a_share_market_gate" in item["failed_gates"] for item in lots)
    assert all(
        item["risk_sizing"]["constraints"]["post_trade_symbol_cap_pct"] == 40.0
        for item in lots
    )


def test_deadline_objective_treats_sixty_percent_as_cap_not_quota():
    candidates = []
    for code, name, entry, stop, target in (
        ("601688", "华泰证券", 20.32, 19.52, 22.01),
        ("600547", "山东黄金", 24.37, 23.11, 27.15),
        ("600028", "中国石化", 5.07, 4.91, 5.61),
    ):
        candidates.append(
            {
                "code": code,
                "name": name,
                "one_lot_amount": round(entry * 100, 2),
                "cash_usage_pct": round(entry * 100 / 10685.41 * 100, 2),
                "triggers": {"status": {}},
                "risk_flags": [],
                "quote": {"freshness": {"actionable": True}},
                "guarded_price_plan": {
                    "actionable": True,
                    "status": "ok",
                    "entry_strategy": "pullback",
                    "suggested_buy_price": entry,
                    "stop_loss_price": stop,
                    "target_price": target,
                },
            }
        )
    objective = holdings_cli_module._validate_deployment_objective(
        60.0,
        "2026-07-21",
        as_of=datetime(2026, 7, 20).date(),
    )

    plan = holdings_cli_module._build_cash_deployment_plan(
        {
            "cash_or_unallocated": 10685.41,
            "known_market_value": 0.0,
        },
        [],
        candidates,
        {"quote_stale_risk": False, "is_late_session": False},
        external_risk_gate={
            "level": "green",
            "actionable": True,
            "max_new_exposure_amount": 6411.25,
        },
        a_share_market_gate={
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
        },
        actionable_equity={"value": 10685.41, "actionable": True},
        deployment_objective=objective,
    )

    lot_plan = plan["candidate_lot_plan"]
    assert any(item["suggested_lots"] > 0 for item in lot_plan)
    assert plan["deployment_objective"]["status"] == "target_shortfall"
    assert plan["deployment_objective"]["projected_exposure_pct"] <= 60.0
    assert all(
        item["risk_sizing"].get("trade") is None
        or item["risk_sizing"]["trade"]["risk_amount"] <= 106.86
        for item in lot_plan
    )


def test_deadline_objective_uses_unified_fee_aware_loss_budget():
    candidates = []
    for code, name, entry, stop, target in (
        ("601688", "华泰证券", 20.33, 19.52, 22.01),
        ("600547", "山东黄金", 24.39, 23.11, 27.15),
        ("600104", "上汽集团", 10.52, 10.03, 11.63),
    ):
        candidates.append(
            {
                "code": code,
                "name": name,
                "one_lot_amount": round(entry * 100, 2),
                "cash_usage_pct": round(entry * 100 / 10685.41 * 100, 2),
                "triggers": {"status": {}},
                "risk_flags": [],
                "quote": {"freshness": {"actionable": True}},
                "guarded_price_plan": {
                    "actionable": True,
                    "status": "ok",
                    "entry_strategy": "pullback",
                    "suggested_buy_price": entry,
                    "stop_loss_price": stop,
                    "target_price": target,
                },
            }
        )
    objective = holdings_cli_module._validate_deployment_objective(
        60.0,
        "2026-07-21",
        as_of=datetime(2026, 7, 20).date(),
    )

    plan = holdings_cli_module._build_cash_deployment_plan(
        {
            "cash_or_unallocated": 10685.41,
            "known_market_value": 0.0,
        },
        [],
        candidates,
        {"quote_stale_risk": False, "is_late_session": False},
        external_risk_gate={
            "level": "green",
            "actionable": True,
            "max_new_exposure_amount": 6411.25,
        },
        a_share_market_gate={
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
        },
        actionable_equity={"value": 10685.41, "actionable": True},
        deployment_objective=objective,
    )

    lot_plan = plan["candidate_lot_plan"]
    assert any(item["suggested_lots"] > 0 for item in lot_plan)
    assert plan["total_loss_budget"] == 213.71
    assert plan["deployment_objective"]["projected_exposure_pct"] <= 60.0
    assert plan["remaining_loss_budget"] >= 0.0


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
    assert payload["meta"]["schema_version"] == 7


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


def test_a_share_market_gate_reuses_context_indices_and_public_snapshot(monkeypatch):
    public_calls = []
    rows = [
        {
            "code": f"600{index:03d}",
            "name": f"sample-{index}",
            "pct_chg": 1.0,
            "trade_date": "2026-07-17",
        }
        for index in range(500)
    ]

    def fake_public_snapshot(**kwargs):
        public_calls.append(kwargs)
        return {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-17",
            "provider_time": "14:30:00",
            "rows": rows,
        }

    context = make_opportunity_market_context(
        public_snapshot_fetcher=fake_public_snapshot
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("context market gate must not fetch single index quotes")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("context market gate must use the cached snapshot")
        ),
    )

    first = _build_a_share_market_gate(None, context=context)
    second = _build_a_share_market_gate(None, context=context)

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert first["benchmark_trade_date"] == "2026-07-17"
    assert first["breadth_regime"]["source"] == "akshare.sina.stock_zh_a_spot"
    assert len(public_calls) == 1


def test_a_share_market_gate_fails_closed_for_failed_index_context(monkeypatch):
    public_calls = []

    def unexpected_public_snapshot(**kwargs):
        public_calls.append(kwargs)
        return {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-17",
            "rows": [],
        }

    context = make_opportunity_market_context(
        public_snapshot_fetcher=unexpected_public_snapshot
    )
    context.index_status = "index_fetch_failed"
    context.index_error = {
        "status": "index_fetch_failed",
        "stage": "tencent_market_context",
        "error_type": "TimeoutError",
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("failed context must not retry index quotes")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed context must not use the legacy breadth fetcher")
        ),
    )

    result = _build_a_share_market_gate(None, context=context)

    assert result["status"] == "index_fetch_failed"
    assert result["index_regime"]["status"] == "index_fetch_failed"
    assert result["new_position_allowed"] is False
    assert result["max_new_exposure_multiplier"] == 0.0
    assert public_calls == []
    assert json.loads(json.dumps(result))["status"] == "index_fetch_failed"


def test_a_share_market_gate_fails_closed_when_parsed_index_change_is_missing(
    monkeypatch,
):
    symbols = holdings_cli_module.A_SHARE_REGIME_INDEX_SYMBOLS
    raw_payload = "".join(
        _make_tencent_index_assignment(
            symbol,
            pct_chg="-" if symbol == "sz399006" else "0.10",
        )
        for symbol in symbols
    )
    rows = parse_tencent_quote_batch_payload(raw_payload)
    assert "pct_chg" not in rows[2]
    context = build_opportunity_market_context(
        now=datetime(2026, 7, 17, 10, 0),
        monotonic=lambda: 0.0,
        quote_fetcher=lambda _codes, *, timeout: {
            "status": "ok",
            "requested_codes": list(symbols),
            "rows": rows,
            "error_type": None,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("invalid context must not retry index quotes")
        ),
    )

    result = _build_a_share_market_gate(None, context=context)

    assert context.index_status == "index_quote_change_invalid"
    assert context.index_quotes == []
    assert context.benchmark_trade_date is None
    assert result["status"] == "index_quote_change_invalid"
    assert result["new_position_allowed"] is False
    assert result["max_new_exposure_multiplier"] == 0.0
    assert result["indices"] == []


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


def test_a_share_market_gate_uses_public_sina_breadth_when_mongo_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda symbol: {
            "code": str(symbol),
            "name": str(symbol),
            "pct_chg": 0.1,
            "trade_date": "2026-07-15",
            "source": "tencent",
        },
    )
    rows = [
        {
            "code": f"600{index:03d}",
            "name": f"样本{index}",
            "pct_chg": 1.0 if index < 150 else -1.0,
            "trade_date": "2026-07-15",
        }
        for index in range(950)
    ]
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-15",
            "provider_time": "13:30:00",
            "universe_size": len(rows),
            "provider_expected_count": 1000,
            "provider_expected_exchange_counts": {
                "sh": 400,
                "sz": 500,
                "bj": 100,
            },
            "raw_row_count": 950,
            "unique_row_count": 950,
            "exchange_counts": {"sh": 380, "sz": 475, "bj": 95},
            "total_coverage_ratio": 0.95,
            "exchange_coverage_ratio": {
                "sh": 0.95,
                "sz": 0.95,
                "bj": 0.95,
            },
            "excluded_stale_count": 0,
            "duplicate_count": 0,
            "rows": rows,
        },
    )

    result = _build_a_share_market_gate("2026-07-15", db=None)

    assert result["breadth_regime"]["source"] == "akshare.sina.stock_zh_a_spot"
    assert result["breadth_regime"]["level"] == "red"
    assert result["breadth_regime"]["provider_time"] == "13:30:00"
    assert result["breadth_regime"]["provider_expected_count"] == 1000
    assert result["breadth_regime"]["provider_expected_exchange_counts"] == {
        "sh": 400,
        "sz": 500,
        "bj": 100,
    }
    assert result["breadth_regime"]["raw_row_count"] == 950
    assert result["breadth_regime"]["unique_row_count"] == 950
    assert result["breadth_regime"]["exchange_counts"] == {
        "sh": 380,
        "sz": 475,
        "bj": 95,
    }
    assert result["breadth_regime"]["total_coverage_ratio"] == 0.95
    assert result["breadth_regime"]["exchange_coverage_ratio"] == {
        "sh": 0.95,
        "sz": 0.95,
        "bj": 0.95,
    }
    assert result["level"] == "red"


def test_a_share_market_gate_does_not_call_sina_when_mongo_breadth_is_valid(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda symbol: {
            "code": str(symbol),
            "name": str(symbol),
            "pct_chg": 0.1,
            "trade_date": "2026-07-15",
            "source": "tencent",
        },
    )
    db = make_fake_db()
    db.collections["market_quotes"] = FakeCollection(
        [
            {
                "code": f"600{index:03d}",
                "name": f"样本{index}",
                "pct_chg": 1.0,
                "trade_date": "2026-07-15",
            }
            for index in range(500)
        ]
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid Mongo breadth must skip the public fallback")
        ),
    )

    result = _build_a_share_market_gate("2026-07-15", db=db)

    assert result["breadth_regime"]["status"] == "ok"
    assert result["breadth_regime"]["source"] == "mongo.market_quotes"


def test_a_share_market_gate_keeps_confirmation_required_when_sina_times_out(monkeypatch):
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda symbol: {
            "code": str(symbol),
            "name": str(symbol),
            "pct_chg": 0.1,
            "trade_date": "2026-07-15",
            "source": "tencent",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: {
            "status": "public_breadth_timeout",
            "source": "akshare.sina.stock_zh_a_spot",
            "timeout_seconds": 25.0,
            "rows": [],
        },
    )

    result = _build_a_share_market_gate("2026-07-15", db=None)

    assert result["level"] == "green"
    assert result["breadth_confirmation_required"] is True
    assert result["breadth_regime"]["status"] == "market_breadth_unavailable"
    assert result["breadth_regime"]["public_fallback"]["status"] == "public_breadth_timeout"
    assert result["breadth_regime"]["public_fallback"]["timeout_seconds"] == 25.0


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


def test_opportunities_with_context_skips_benchmark_fetch_and_reuses_market_context(
    monkeypatch,
):
    patch_fresh_cli_market_context(monkeypatch)
    context = make_opportunity_market_context()
    seen = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: (_ for _ in ()).throw(
            AssertionError("context benchmark must replace historical calendar fetch")
        ),
    )

    def fake_market_gate(benchmark_trade_date, *, db=None, context=None):
        seen.append(
            {
                "benchmark_trade_date": benchmark_trade_date,
                "db": db,
                "context": context,
            }
        )
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

    monkeypatch.setattr(holdings_cli_module, "_build_a_share_market_gate", fake_market_gate)
    db = make_fake_db()

    build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["600123"],
        context=context,
    )

    assert seen == [
        {
            "benchmark_trade_date": "2026-07-17",
            "db": db,
            "context": context,
        }
    ]


def test_opportunities_ignores_unverified_benchmark_date_from_failed_context(
    monkeypatch,
):
    patch_fresh_cli_market_context(monkeypatch)
    context = make_opportunity_market_context()
    context.index_status = "index_trade_date_mismatch"
    context.index_error = {
        "status": "index_trade_date_mismatch",
        "stage": "tencent_market_context",
    }
    seen = {"holdings_dates": [], "gate_dates": []}
    original_build_holdings_payload = holdings_cli_module.build_holdings_payload

    def capture_holdings_dates(*args, **kwargs):
        seen["holdings_dates"].append(kwargs.get("benchmark_session_dates"))
        return original_build_holdings_payload(*args, **kwargs)

    def fake_market_gate(benchmark_trade_date, *, db=None, context=None):
        seen["gate_dates"].append(benchmark_trade_date)
        return {
            "status": "index_trade_date_mismatch",
            "level": "unknown",
            "new_position_allowed": False,
            "max_new_exposure_multiplier": 0.0,
            "benchmark_trade_date": None,
            "trade_date": None,
            "indices": [],
            "reason": "failed test context",
            "is_reference_only": True,
        }

    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: (_ for _ in ()).throw(
            AssertionError("failed context must not fetch the historical benchmark")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_holdings_payload",
        capture_holdings_dates,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        fake_market_gate,
    )

    payload = build_opportunities_payload(
        make_fake_db(),
        username="hermes",
        candidate_codes=["600123"],
        context=context,
    )

    assert payload["data"]["a_share_market_gate"]["status"] == "index_trade_date_mismatch"
    assert seen == {"holdings_dates": [[]], "gate_dates": [None]}


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

    assert plan["external_new_exposure_amount"] == 6384.0
    assert plan["market_adjusted_new_exposure_cap"] == 3192.0
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
    assert payload["meta"]["schema_version"] == 7


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


def test_manual_candidate_earnings_gate_blocks_account_sizing(monkeypatch):
    patch_fresh_cli_market_context(
        monkeypatch,
        technical_plan={
            "actionable": True,
            "status": "ok",
            "source": "tencent_qfq_daily",
            "stop_loss_price": 60.0,
            "suggested_buy_price": 63.0,
            "suggested_sell_price": 68.0,
            "target_price": 70.0,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: ["2026-07-17"],
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "screen_public_candidate_earnings_risk",
        lambda codes, *, benchmark_trade_date: _make_public_earnings_screen(
            list(codes),
            blocked_codes=("000066",),
        ),
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 100_000.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )

    assert payload["data"]["earnings_review"]["blocked_codes"] == ["000066"]
    candidate = payload["data"]["candidates"][0]
    assert candidate["earnings_gate"] == {
        "status": "blocked",
        "blocks_new_position": True,
        "reason_code": "earnings_risk_gate",
        "forecast_status": "loss_forecast",
        "actual_status": "positive_profit",
        "actual_risk_flags": [],
    }
    assert candidate["guarded_price_plan"]["actionable"] is False
    assert candidate["guarded_price_plan"]["reference_actionable"] is True
    assert "earnings_risk_gate" in candidate["guarded_price_plan"][
        "execution_blocked_by"
    ]
    assert "earnings_risk_gate" in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    lot_plan = payload["data"]["brief"]["cash_deployment_plan"][
        "candidate_lot_plan"
    ][0]
    assert lot_plan["risk_gate"] == "blocked_by_earnings_risk"
    assert "earnings_risk_gate" in lot_plan["blocking_failed_gates"]
    assert lot_plan["suggested_lots"] == 0
    assert lot_plan["suggested_quantity"] == 0


def test_manual_candidate_earnings_unavailable_fails_closed(monkeypatch):
    patch_fresh_cli_market_context(monkeypatch)
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: ["2026-07-17"],
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "screen_public_candidate_earnings_risk",
        lambda _codes, *, benchmark_trade_date: {
            "status": "earnings_actual_unavailable",
            "source": holdings_cli_module.EARNINGS_FORECAST_SOURCE,
            "actual_source": holdings_cli_module.EARNINGS_ACTUAL_SOURCE,
            "report_period": "20260630",
            "actual_report_period": "20260331",
            "error_type": "TimeoutError",
            "results": [],
        },
    )

    payload = build_opportunities_payload(
        make_fake_db(),
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )

    assert payload["data"]["earnings_review"]["status"] == (
        "earnings_actual_unavailable"
    )
    candidate = payload["data"]["candidates"][0]
    assert candidate["earnings_review"] is None
    assert candidate["earnings_gate"]["status"] == "unavailable"
    assert candidate["earnings_gate"]["blocks_new_position"] is True
    assert "earnings_review_unavailable" in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    lot_plan = payload["data"]["brief"]["cash_deployment_plan"][
        "candidate_lot_plan"
    ][0]
    assert lot_plan["risk_gate"] == "blocked_by_earnings_review"
    assert "earnings_review_unavailable" in lot_plan["blocking_failed_gates"]
    assert lot_plan["suggested_quantity"] == 0


def test_manual_candidates_reject_more_than_earnings_review_capacity():
    with pytest.raises(CLIError) as exc_info:
        build_opportunities_payload(
            make_fake_db(),
            username="hermes",
            candidate_codes=[f"600{index:03d}" for index in range(9)],
        )

    assert exc_info.value.code == "too_many_manual_candidates"


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
    assert plan_item["suggested_lots"] == 4
    assert plan_item["risk_sizing"]["constraints"]["post_trade_symbol_cap_pct"] == 40.0


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

    assert payload["data"]["external_risk_gate"]["max_new_exposure_amount"] == 3192.0
    assert plan_item["suggested_lots"] == 0
    assert "account_loss_budget" in plan_item["failed_gates"]
    assert plan_item["blocking_failed_gates"] == ["account_loss_budget"]
    decision_row = payload["data"]["brief"]["candidate_decision_matrix"]["rows"][0]
    assert "account_loss_budget" in decision_row["failed_gates"]
    assert decision_row["blocking_failed_gates"] == ["account_loss_budget"]


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


def test_cli_error_payload_uses_details_stage_as_the_only_stage_location():
    payload = holdings_cli_module._cli_error_payload(
        CLIError(
            "公开链路失败",
            code="candidate_discovery_unavailable",
            exit_code=4,
            stage="technical_deep_check",
            details={
                "stage": "candidate_discovery",
                "candidate_discovery": {
                    "status": "candidate_discovery_unavailable"
                },
            },
        ),
        include_stage=True,
    )

    assert payload["error"]["details"]["stage"] == "candidate_discovery"
    assert "stage" not in payload["error"]


def test_opportunities_command_emits_only_error_json_when_context_worker_warns(
    monkeypatch,
    tmp_path,
):
    stderr_handlers = _patch_structured_tencent_worker_failure(monkeypatch)
    file_handler = logging.FileHandler(
        tmp_path / "opportunity-context.log",
        encoding="utf-8",
    )
    opportunity_context_module.logger.addHandler(file_handler)
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            CLIError(
                "公开全市场候选发现不可用",
                code="candidate_discovery_unavailable",
                exit_code=4,
                details={
                    "stage": "tencent_market_context",
                    "candidate_discovery": {
                        "status": "candidate_discovery_unavailable"
                    },
                },
            )
        ),
    )

    try:
        result = CliRunner().invoke(holdings_app, ["opportunities"])
        file_handler_was_preserved = (
            file_handler in opportunity_context_module.logger.handlers
        )
        file_handler.flush()
        file_log = (tmp_path / "opportunity-context.log").read_text(
            encoding="utf-8"
        )
    finally:
        opportunity_context_module.logger.removeHandler(file_handler)
        file_handler.close()
        for handler in stderr_handlers:
            opportunity_context_module.logger.removeHandler(handler)
            handler.close()

    assert result.exit_code == 4
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == (
        "candidate_discovery_unavailable"
    )
    assert file_handler_was_preserved is True
    assert "worker returned provider failure" in file_log


def test_opportunities_manual_fallback_keeps_worker_warning_out_of_stderr(
    monkeypatch,
):
    stderr_handlers = _patch_structured_tencent_worker_failure(monkeypatch)
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )

    def fake_research_builder(**_kwargs):
        print("research builder diagnostic", file=sys.stderr)
        return {
            "ok": True,
            "data": {"mode": "research_only", "candidates": []},
            "meta": {"schema_version": 6},
        }

    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        fake_research_builder,
    )

    try:
        result = CliRunner().invoke(
            holdings_app,
            ["opportunities", "--candidate-code", "601728"],
        )
    finally:
        for handler in stderr_handlers:
            opportunity_context_module.logger.removeHandler(handler)
            handler.close()

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["mode"] == "research_only"
    assert payload["meta"]["schema_version"] == 6


def test_opportunities_command_creates_context_before_mongo_and_passes_it_to_full_builder(
    monkeypatch,
):
    context = make_opportunity_market_context()
    events = []
    database = object()

    def fake_build_context():
        events.append("context")
        return context

    def fake_optional_database(*, timeout_cap_ms=None):
        events.append(("mongo", timeout_cap_ms))
        return database, {"status": "connected"}

    def fake_build_payload(db, **kwargs):
        events.append(("full", db, kwargs.get("context")))
        return {"ok": True, "data": {}, "meta": {"schema_version": 6}}

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        fake_build_context,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        fake_optional_database,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        fake_build_payload,
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 0
    assert events == [
        "context",
        ("mongo", 5000),
        ("full", database, context),
    ]


@pytest.mark.parametrize(
    ("remaining_values", "expected_stage", "expected_events"),
    [
        ([0.0], "tencent_market_context", []),
        ([10.0, 0.0], "mongo", [("mongo", 5000)]),
        (
            [10.0, 10.0, 0.0],
            "orchestration",
            [("mongo", 5000), "builder"],
        ),
        (
            [10.0, 10.0, 10.0, 0.0],
            "orchestration",
            [("mongo", 5000), "builder"],
        ),
    ],
)
def test_opportunities_command_rejects_success_after_each_deadline_checkpoint(
    monkeypatch,
    remaining_values,
    expected_stage,
    expected_events,
):
    class DeadlineContext:
        def __init__(self):
            self.values = iter(remaining_values)

        def remaining_seconds(self):
            return next(self.values)

        def stage_timeout(self, stage):
            assert stage == "mongo"
            return 5.0

    context = DeadlineContext()
    events = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            events.append(("mongo", timeout_cap_ms)) or object(),
            {"status": "connected"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda *_args, **_kwargs: (
            events.append("builder")
            or {"ok": True, "data": {}, "meta": {"schema_version": 6}}
        ),
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 4
    assert events == expected_events
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "stage_timeout",
            "message": "opportunities command deadline exceeded",
            "stage": expected_stage,
        },
    }


@pytest.mark.parametrize(
    ("blocking_builder", "database_available", "arguments"),
    [
        (
            "build_opportunities_payload",
            True,
            ["opportunities", "--candidate-code", "601728"],
        ),
        (
            "build_research_only_opportunities_payload",
            False,
            ["opportunities", "--candidate-code", "601728"],
        ),
        (
            "_build_public_full_market_research_payload",
            False,
            ["opportunities"],
        ),
    ],
)
def test_opportunities_command_interrupts_blocking_sync_builders_at_deadline(
    monkeypatch,
    blocking_builder,
    database_available,
    arguments,
):
    required_signal_api = ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer")
    if not all(hasattr(signal, name) for name in required_signal_api):
        pytest.skip("hard wall-clock guard requires POSIX interval timers")

    builder_started = []

    def build_short_deadline_context():
        started_at = time.monotonic()
        context = make_opportunity_market_context(monotonic=time.monotonic)
        context.started_at = started_at
        context.deadline_at = started_at + 0.1
        return context

    def unexpected_builder(*_args, **_kwargs):
        raise AssertionError("command selected the wrong opportunities builder")

    def blocking_sync_builder(*_args, **_kwargs):
        builder_started.append(True)
        try:
            time.sleep(1.0)
        except Exception:
            time.sleep(1.0)
        return {"ok": True, "data": {}, "meta": {"schema_version": 7}}

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        build_short_deadline_context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            object() if database_available else None,
            (
                {"status": "connected"}
                if database_available
                else {"status": "unavailable", "error_code": "database_error"}
            ),
        ),
    )
    for builder_name in (
        "build_opportunities_payload",
        "build_research_only_opportunities_payload",
        "_build_public_full_market_research_payload",
    ):
        monkeypatch.setattr(
            holdings_cli_module,
            builder_name,
            unexpected_builder,
        )
    monkeypatch.setattr(
        holdings_cli_module,
        blocking_builder,
        blocking_sync_builder,
    )

    started_at = time.monotonic()
    result = CliRunner().invoke(holdings_app, arguments)
    elapsed = time.monotonic() - started_at

    assert builder_started == [True]
    assert elapsed < 0.6
    assert result.exit_code == 4
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "stage_timeout",
            "message": "opportunities command deadline exceeded",
            "stage": "orchestration",
        },
    }


def test_opportunity_guard_does_not_take_over_active_host_timer():
    required_signal_api = ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer")
    if not all(hasattr(signal, name) for name in required_signal_api):
        pytest.skip("host timer isolation probe requires POSIX interval timers")

    script = textwrap.dedent(
        """
        import json
        import signal
        import time
        from datetime import datetime

        from app.services.holdings_cli import _opportunity_wall_clock_guard
        from app.services.opportunity_market_context import OpportunityMarketContext

        event_order = []

        def host_handler(_signum, _frame):
            event_order.append("host_timer")

        signal.signal(signal.SIGALRM, host_handler)
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        started_at = time.monotonic()
        context = OpportunityMarketContext(
            now=datetime.now(),
            started_at=started_at,
            deadline_at=started_at + 1.0,
            index_quotes=[],
            benchmark_trade_date=None,
            monotonic=time.monotonic,
        )
        outcome = {"status": "ok"}
        handler_preserved_during_guard = False
        try:
            with _opportunity_wall_clock_guard(context, stage="orchestration"):
                handler_preserved_during_guard = (
                    signal.getsignal(signal.SIGALRM) is host_handler
                )
                time.sleep(0.12)
                event_order.append("builder_after_sleep")
        except BaseException as exc:
            outcome = {
                "status": "error",
                "type": type(exc).__name__,
                "code": getattr(exc, "code", None),
            }
        remaining = signal.getitimer(signal.ITIMER_REAL)
        handler_restored = signal.getsignal(signal.SIGALRM) is host_handler
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        print(json.dumps({
            "outcome": outcome,
            "event_order": event_order,
            "remaining": remaining,
            "handler_restored": handler_restored,
            "handler_preserved_during_guard": handler_preserved_during_guard,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["outcome"] == {"status": "ok"}
    assert result["event_order"] == ["host_timer", "builder_after_sleep"]
    assert result["remaining"] == [0.0, 0.0]
    assert result["handler_restored"] is True
    assert result["handler_preserved_during_guard"] is True


def test_opportunity_guard_restores_handler_and_falls_back_after_setup_error():
    required_signal_api = ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer")
    if not all(hasattr(signal, name) for name in required_signal_api):
        pytest.skip("setup failure probe requires POSIX interval timers")

    script = textwrap.dedent(
        """
        import json
        import signal
        import time
        from datetime import datetime

        from app.services.holdings_cli import _opportunity_wall_clock_guard
        from app.services.opportunity_market_context import OpportunityMarketContext

        original_handler = signal.getsignal(signal.SIGALRM)
        real_setitimer = signal.setitimer
        setitimer_calls = []

        def failing_setitimer(which, seconds, interval=0.0):
            setitimer_calls.append([which, seconds, interval])
            raise RuntimeError("setitimer setup failed")

        signal.setitimer = failing_setitimer
        started_at = time.monotonic()
        context = OpportunityMarketContext(
            now=datetime.now(),
            started_at=started_at,
            deadline_at=started_at + 1.0,
            index_quotes=[],
            benchmark_trade_date=None,
            monotonic=time.monotonic,
        )
        builder_calls = 0
        outcome = {"status": "ok"}
        try:
            with _opportunity_wall_clock_guard(context, stage="orchestration"):
                builder_calls += 1
        except BaseException as exc:
            outcome = {
                "status": "error",
                "type": type(exc).__name__,
                "message": str(exc),
            }
        handler_restored = signal.getsignal(signal.SIGALRM) == original_handler
        signal.setitimer = real_setitimer
        signal.signal(signal.SIGALRM, original_handler)
        print(json.dumps({
            "outcome": outcome,
            "builder_calls": builder_calls,
            "setitimer_call_count": len(setitimer_calls),
            "handler_restored": handler_restored,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result == {
        "outcome": {"status": "ok"},
        "builder_calls": 1,
        "setitimer_call_count": 1,
        "handler_restored": True,
    }


def test_opportunity_guard_restores_handler_before_setup_keyboard_interrupt():
    required_signal_api = ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer")
    if not all(hasattr(signal, name) for name in required_signal_api):
        pytest.skip("setup interrupt probe requires POSIX interval timers")

    script = textwrap.dedent(
        """
        import json
        import signal
        import time
        from datetime import datetime

        from app.services.holdings_cli import _opportunity_wall_clock_guard
        from app.services.opportunity_market_context import OpportunityMarketContext

        original_handler = signal.getsignal(signal.SIGALRM)
        real_setitimer = signal.setitimer
        setitimer_calls = []

        def interrupting_setitimer(which, seconds, interval=0.0):
            setitimer_calls.append([which, seconds, interval])
            raise KeyboardInterrupt("setup interrupted")

        signal.setitimer = interrupting_setitimer
        started_at = time.monotonic()
        context = OpportunityMarketContext(
            now=datetime.now(),
            started_at=started_at,
            deadline_at=started_at + 1.0,
            index_quotes=[],
            benchmark_trade_date=None,
            monotonic=time.monotonic,
        )
        builder_calls = 0
        outcome = {"status": "ok"}
        try:
            with _opportunity_wall_clock_guard(context, stage="orchestration"):
                builder_calls += 1
        except BaseException as exc:
            outcome = {
                "status": "error",
                "type": type(exc).__name__,
                "message": str(exc),
            }
        handler_restored = signal.getsignal(signal.SIGALRM) == original_handler
        signal.setitimer = real_setitimer
        signal.signal(signal.SIGALRM, original_handler)
        print(json.dumps({
            "outcome": outcome,
            "builder_calls": builder_calls,
            "setitimer_call_count": len(setitimer_calls),
            "handler_restored": handler_restored,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result == {
        "outcome": {
            "status": "error",
            "type": "KeyboardInterrupt",
            "message": "setup interrupted",
        },
        "builder_calls": 0,
        "setitimer_call_count": 1,
        "handler_restored": True,
    }


def test_opportunity_guard_cleans_up_when_deadline_hits_cleanup_handoff():
    required_signal_api = (
        "SIGALRM",
        "ITIMER_REAL",
        "setitimer",
        "getitimer",
        "raise_signal",
    )
    if not all(hasattr(signal, name) for name in required_signal_api):
        pytest.skip("cleanup handoff probe requires POSIX interval timers")

    script = textwrap.dedent(
        """
        import inspect
        import json
        import signal
        import sys
        import time
        from datetime import datetime

        from app.services.holdings_cli import _opportunity_wall_clock_guard
        from app.services.opportunity_market_context import OpportunityMarketContext

        guard_generator = _opportunity_wall_clock_guard.__wrapped__
        source_lines, source_start = inspect.getsourcelines(guard_generator)
        cleanup_handoff_line = next(
            source_start + index
            for index, line in enumerate(source_lines)
            if (
                line.strip() == "cleanup_error = cleanup()"
                and source_lines[index - 1].strip() == "finally:"
            )
        )
        original_handler = signal.getsignal(signal.SIGALRM)
        started_at = time.monotonic()
        context = OpportunityMarketContext(
            now=datetime.now(),
            started_at=started_at,
            deadline_at=started_at + 30.0,
            index_quotes=[],
            benchmark_trade_date=None,
            monotonic=time.monotonic,
        )
        builder_calls = 0
        injected = False

        def inject_at_cleanup_handoff(frame, event, _arg):
            global injected
            if (
                not injected
                and event == "line"
                and frame.f_code is guard_generator.__code__
                and frame.f_lineno == cleanup_handoff_line
            ):
                injected = True
                sys.settrace(None)
                signal.raise_signal(signal.SIGALRM)
            return inject_at_cleanup_handoff

        outcome = {"status": "ok"}
        sys.settrace(inject_at_cleanup_handoff)
        try:
            with _opportunity_wall_clock_guard(context, stage="orchestration"):
                builder_calls += 1
        except BaseException as exc:
            outcome = {
                "status": "error",
                "type": type(exc).__name__,
                "code": getattr(exc, "code", None),
                "stage": getattr(exc, "stage", None),
            }
        finally:
            sys.settrace(None)
        remaining = signal.getitimer(signal.ITIMER_REAL)
        handler_restored = signal.getsignal(signal.SIGALRM) == original_handler
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, original_handler)
        print(json.dumps({
            "outcome": outcome,
            "builder_calls": builder_calls,
            "injected": injected,
            "handler_restored": handler_restored,
            "remaining": remaining,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result == {
        "outcome": {
            "status": "error",
            "type": "CLIError",
            "code": "stage_timeout",
            "stage": "orchestration",
        },
        "builder_calls": 1,
        "injected": True,
        "handler_restored": True,
        "remaining": [0.0, 0.0],
    }


def test_opportunities_command_rejects_success_when_json_serialization_expires_deadline(
    monkeypatch,
):
    clock = {"value": 89.98}
    context = make_opportunity_market_context(
        monotonic=lambda: clock["value"],
    )
    success_payload = {
        "ok": True,
        "data": {"mode": "research_only"},
        "meta": {"schema_version": 6},
    }
    original_dumps = json.dumps

    def late_json_dumps(payload, *args, **kwargs):
        serialized = original_dumps(payload, *args, **kwargs)
        if payload is success_payload:
            clock["value"] = 90.01
        return serialized

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (object(), {"status": "connected"}),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda *_args, **_kwargs: success_payload,
    )
    monkeypatch.setattr(holdings_cli_module.json, "dumps", late_json_dumps)

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "stage_timeout",
            "message": "opportunities command deadline exceeded",
            "stage": "orchestration",
        },
    }


def test_opportunities_command_serializes_success_once_and_preserves_output(monkeypatch):
    context = make_opportunity_market_context()
    success_payload = {
        "ok": True,
        "data": {"mode": "research_only", "candidate_count": 0},
        "meta": {"schema_version": 6},
    }
    original_dumps = json.dumps
    serialized_payloads = []

    def counting_json_dumps(payload, *args, **kwargs):
        serialized_payloads.append(payload)
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (object(), {"status": "connected"}),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda *_args, **_kwargs: success_payload,
    )
    monkeypatch.setattr(holdings_cli_module.json, "dumps", counting_json_dumps)

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert serialized_payloads == [success_payload]
    assert json.loads(result.stdout) == success_payload


def test_opportunities_command_passes_same_context_to_research_builder(monkeypatch):
    context = make_opportunity_market_context()
    seen = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            seen.append(("mongo", timeout_cap_ms)) or None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )

    def fake_research_builder(**kwargs):
        seen.append(("research", kwargs.get("context")))
        return {
            "ok": True,
            "data": {"mode": "research_only", "candidates": []},
            "meta": {"schema_version": 6},
        }

    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        fake_research_builder,
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 0
    assert seen == [("mongo", 5000), ("research", context)]


def test_research_only_builder_reuses_context_for_market_status(monkeypatch):
    context = make_opportunity_market_context()
    seen = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_opportunity_candidates",
        lambda *_args, **_kwargs: [],
    )

    def fake_market_status(db=None, *, database_status=None, context=None):
        seen.append({"db": db, "database_status": database_status, "context": context})
        return {
            "ok": True,
            "data": {"decision": {"action": "wait", "actionable": False}},
            "meta": {"schema_version": 1, "source": "tencent_major_indices"},
        }

    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        fake_market_status,
    )

    payload = holdings_cli_module.build_research_only_opportunities_payload(
        candidate_codes=["601728"],
        database_status={"status": "unavailable", "error_code": "database_error"},
        context=context,
    )

    assert payload["data"]["mode"] == "research_only"
    assert payload["meta"]["schema_version"] == 7
    assert seen == [
        {
            "db": None,
            "database_status": {
                "status": "unavailable",
                "error_code": "database_error",
            },
            "context": context,
        }
    ]


def _make_public_research_discovery(status="ok", *, candidate_count=1):
    has_candidates = status == "ok" and candidate_count > 0
    definitions = []
    for index in range(candidate_count if has_candidates else 0):
        close = 10.25 + index
        amount = 321_000_000.0 + index
        definitions.append(
            {
                "code": f"600{index:03d}",
                "name": f"公开候选{index}",
                "exchange": "sh",
                "objective_id": "technology_new_quality_productive_forces",
                "objective_label": "科技 + 新质生产力",
                "objective_tier": "non_core",
                "objective_tier_label": "非核心方向",
                "objective_segment": "其他行业",
                "objective_match_score": 0.0,
                "objective_reason": "测试候选未匹配核心方向。",
                "price": close,
                "pct_change": 1.25 - index,
                "amount": amount,
                "one_lot_amount": close * 100,
                "bucket": "strength" if index == 0 else "pullback",
                "trade_date": "2026-07-17",
                "amount_percentile": 0.95 - index * 0.10,
                "move_quality": 0.85 - index * 0.10,
                "public_score": 0.90 - index * 0.10,
                "tencent_price": close,
                "tencent_pct_change": 2.5 - index,
                "tencent_amount": amount,
                "tencent_trade_at": "2026-07-17T10:00:00+08:00",
                "tencent_source": "tencent_batch_quotes",
                "tencent_bucket": "strength" if index == 0 else "pullback",
                "turnover_rate": 2.1 + index,
                "volume_ratio": 1.3 - index * 0.1,
                "amplitude": 3.2 + index,
                "circ_mv": 40_000_000_000.0 + index,
                "total_mv": 50_000_000_000.0 + index,
                "limit_up": 11.28 + index,
                "tencent_move_quality": 0.88 - index * 0.10,
                "turnover_quality": 1.0 - index * 0.05,
                "volume_ratio_quality": 1.0 - index * 0.10,
                "amplitude_quality": 1.0 - index * 0.10,
                "tencent_amount_percentile": 0.92 - index * 0.10,
                "tencent_market_cap_percentile": 0.72 - index * 0.10,
                "tencent_score": 0.89 - index * 0.10,
            }
        )
    quote_map = {
        definition["code"]: {
            "code": definition["code"],
            "name": definition["name"],
            "source": "tencent",
            "trade_at": "2026-07-17T10:00:00+08:00",
            "trade_date": "2026-07-17",
            "close": 10.25 + index,
            "open": 10.0 + index,
            "high": 10.5 + index,
            "low": 9.9 + index,
            "pct_chg": 2.5 - index,
            "amount": 321_000_000.0 + index,
            "volume": 12_345_600 + index,
            "quote_volume": 12_300_000 + index,
            "turnover_rate": 2.1 + index,
            "volume_ratio": 1.3 - index * 0.1,
            "amplitude": 3.2 + index,
            "circ_mv": 40_000_000_000.0 + index,
            "total_mv": 50_000_000_000.0 + index,
            "limit_up": 11.28 + index,
        }
        for index, definition in enumerate(definitions)
    }
    source = "akshare.sina.stock_zh_a_spot"
    tencent_status = "not_called_no_preselection"
    if has_candidates:
        source += "+tencent_batch_quotes"
        tencent_status = "ok"
    selected_count = len(definitions)
    candidate_discovery = {
        "mode": "public_full_market",
        "status": status,
        "source": source,
        "benchmark_trade_date": "2026-07-17",
        "provider_expected_count": 5_527,
        "provider_expected_exchange_counts": {
            "sh": 2_307,
            "sz": 2_893,
            "bj": 327,
        },
        "raw_row_count": 5_527,
        "unique_row_count": 5_527,
        "universe_count": 5_527,
        "exchange_counts": {"sh": 2_307, "sz": 2_893, "bj": 327},
        "total_coverage_ratio": 1.0,
        "exchange_coverage_ratio": {"sh": 1.0, "sz": 1.0, "bj": 1.0},
        "eligible_count": 2_134 if has_candidates else 0,
        "public_preselected_count": selected_count,
        "tencent_requested_count": selected_count,
        "tencent_minimum_verified_count": selected_count,
        "tencent_verified_count": selected_count,
        "tencent_rank_population_count": selected_count,
        "selected_count": selected_count,
        "technical_checked_count": 0,
        "technical_screened_count": 0,
        "technical_passed_count": 0,
        "technical_selected_count": 0,
        "technical_screen_status_counts": {},
        "technical_closest_rejection_count": 0,
        "technical_closest_rejections": [],
        "earnings_screened_count": 0,
        "earnings_blocked_count": 0,
        "earnings_selected_count": 0,
        "earnings_report_period": None,
        "earnings_actual_report_period": None,
        "earnings_screen_status_counts": {},
        "earnings_actual_status_counts": {},
        "earnings_screen_results": [],
        "rejection_counts": (
            {"outside_move_window": 1_200}
            if has_candidates
            else {"below_min_amount": 5_527}
        ),
        "quality_counts": {"missing_volume_ratio": 3} if has_candidates else {},
        "stage_sources": {
            "public_snapshot": {
                "provider": "akshare.sina.stock_zh_a_spot",
                "status": "ok",
            },
            "tencent_verification": {
                "provider": "tencent_batch_quotes",
                "status": tencent_status,
            },
        },
    }
    return {
        "status": status,
        "definitions": definitions,
        "quote_map": quote_map,
        "candidate_discovery": candidate_discovery,
    }


def _assert_public_research_safety(value):
    false_keys = {"actionable", "reference_actionable", "new_position_allowed"}
    zero_keys = {
        "suggested_lots",
        "suggested_quantity",
        "new_position_lots",
        "new_position_quantity",
        "max_new_exposure_amount",
        "max_new_exposure_pct",
        "external_new_exposure_amount",
        "market_adjusted_new_exposure_cap",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in false_keys:
                assert nested is False
            elif key in zero_keys:
                assert nested == 0
            else:
                _assert_public_research_safety(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_public_research_safety(nested)


def _fake_public_market_status(db=None, *, database_status=None, context=None):
    return {
        "ok": True,
        "data": {
            "market": "CN",
            "market_session": {
                "market": "CN",
                "timezone": "Asia/Shanghai",
                "local_time": "2026-07-17T10:00:00+08:00",
                "session": "morning",
                "is_trading_hours": True,
                "quote_stale_risk": False,
                "minutes_to_close": 300,
                "is_late_session": False,
                "next_refresh_at": None,
                "next_refresh_session": None,
            },
            "market_gate": {
                "new_position_allowed": True,
                "max_new_exposure_amount": 88_000.0,
                "market_adjusted_new_exposure_cap": 66_000.0,
                "index_price": 3_412.34,
                "quote_volume": 987_654_321,
                "data_complete": True,
            },
            "decision": {
                "action": "evaluate_candidates",
                "actionable": True,
                "suggested_lots": 9,
                "suggested_quantity": 900,
            },
            "database": deepcopy(database_status),
        },
        "meta": {
            "schema_version": 1,
            "source": "tencent_major_indices+akshare_sina_public_breadth",
        },
    }


def _make_public_earnings_screen(
    codes,
    *,
    blocked_codes=(),
    actual_loss_codes=(),
    benchmark_trade_date="2026-07-17",
):
    forecast_blocked = set(blocked_codes)
    actual_loss = set(actual_loss_codes)
    results = []
    for code in codes:
        if code in forecast_blocked:
            results.append(
                {
                    "code": code,
                    "status": "loss_forecast",
                    "blocks_new_position": True,
                    "announcement_date": "2026-07-17",
                    "forecast_types": ["首亏"],
                    "loss_metrics": ["归属于上市公司股东的净利润"],
                    "reason_summary": "预计半年度亏损。",
                    "evidence": [
                        {
                            "metric": "归属于上市公司股东的净利润",
                            "forecast_type": "首亏",
                            "forecast_value": -1_000_000.0,
                            "forecast_change_pct": -120.0,
                            "forecast_text": "预计亏损",
                        }
                    ],
                }
            )
        else:
            results.append(
                {
                    "code": code,
                    "status": "no_forecast",
                    "blocks_new_position": code in actual_loss,
                    "announcement_date": None,
                    "forecast_types": [],
                    "loss_metrics": [],
                    "reason_summary": None,
                    "evidence": [],
                }
            )
        results[-1]["latest_actual"] = {
            "status": "actual_loss" if code in actual_loss else "positive_profit",
            "report_period": "20260331",
            "announcement_date": "2026-04-29",
            "net_profit": -1_000_000.0 if code in actual_loss else 10_000_000.0,
            "net_profit_yoy_pct": -120.0 if code in actual_loss else 10.0,
            "net_profit_qoq_pct": None,
            "revenue": 100_000_000.0,
            "revenue_yoy_pct": 5.0,
            "revenue_qoq_pct": None,
            "eps": None,
            "book_value_per_share": None,
            "roe_pct": None,
            "operating_cash_flow_per_share": None,
            "gross_margin_pct": None,
            "industry": None,
            "risk_flags": (
                ["actual_net_loss", "net_profit_yoy_decline"]
                if code in actual_loss
                else []
            ),
        }
    blocked = forecast_blocked.union(actual_loss)
    actual_blocked_codes = [code for code in codes if code in blocked]
    selected_codes = [code for code in codes if code not in blocked]
    status_counts = Counter(item["status"] for item in results)
    actual_status_counts = Counter(
        item["latest_actual"]["status"] for item in results
    )
    report_period = holdings_cli_module.latest_completed_reporting_period(
        benchmark_trade_date
    )
    actual_report_period = (
        holdings_cli_module.latest_mandatory_actual_reporting_period(
            benchmark_trade_date
        )
    )
    for result in results:
        result["latest_actual"]["report_period"] = actual_report_period
    return {
        "status": "ok",
        "source": holdings_cli_module.EARNINGS_FORECAST_SOURCE,
        "actual_source": holdings_cli_module.EARNINGS_ACTUAL_SOURCE,
        "report_period": report_period,
        "actual_report_period": actual_report_period,
        "screened_count": len(codes),
        "blocked_count": len(actual_blocked_codes),
        "selected_count": len(selected_codes),
        "blocked_codes": actual_blocked_codes,
        "selected_codes": selected_codes,
        "status_counts": dict(status_counts),
        "actual_status_counts": dict(actual_status_counts),
        "results": results,
    }


def _make_public_notice_review(codes, *, notice_codes=()):
    codes_with_notices = set(notice_codes)
    results = []
    for code in codes:
        if code in codes_with_notices:
            notices = [
                {
                    "announcement_date": "2026-07-18",
                    "title": "回购股份比例达到1%暨回购完成的公告",
                    "notice_type": "回购",
                    "url": (
                        "https://data.eastmoney.com/notices/"
                        f"{code}/repurchase"
                    ),
                    "attention_tags": ["share_repurchase"],
                    "manual_review_required": True,
                }
            ]
            results.append(
                {
                    "code": code,
                    "name": "TCL科技",
                    "status": "notices_found",
                    "total_notice_count": 1,
                    "returned_notice_count": 1,
                    "truncated": False,
                    "attention_tags": ["share_repurchase"],
                    "manual_review_required": True,
                    "notices": notices,
                }
            )
        else:
            results.append(
                {
                    "code": code,
                    "name": None,
                    "status": "no_recent_notices",
                    "total_notice_count": 0,
                    "returned_notice_count": 0,
                    "truncated": False,
                    "attention_tags": [],
                    "manual_review_required": False,
                    "notices": [],
                }
            )
    notice_count = len(codes_with_notices.intersection(codes))
    return {
        "status": "ok",
        "source": holdings_cli_module.NOTICE_REVIEW_SOURCE,
        "start_date": "2026-07-14",
        "end_date": "2026-07-20",
        "lookback_calendar_days": 7,
        "reviewed_count": len(codes),
        "codes_with_notices_count": notice_count,
        "manual_review_code_count": notice_count,
        "total_notice_count": notice_count,
        "returned_notice_count": notice_count,
        "attention_tag_code_counts": (
            {"share_repurchase": notice_count} if notice_count else {}
        ),
        "results": results,
    }


def _make_public_notice_history(codes, *, notice_codes=()):
    payload = _make_public_notice_review(codes, notice_codes=notice_codes)
    payload.update(
        {
            "source": holdings_cli_module.NOTICE_HISTORY_SOURCE,
            "start_date": "2026-04-22",
            "lookback_calendar_days": 90,
        }
    )
    return payload


def test_build_public_candidate_earnings_payload_is_validated_and_research_only():
    calls = []

    def fake_screener(codes, *, benchmark_trade_date):
        calls.append((list(codes), benchmark_trade_date))
        return _make_public_earnings_screen(codes)

    payload = holdings_cli_module.build_public_candidate_earnings_payload(
        ["002318", "000100"],
        context=make_hydrated_opportunity_market_context(),
        screener=fake_screener,
    )

    assert calls == [(["002318", "000100"], "2026-07-17")]
    assert payload["meta"] == {
        "schema_version": 1,
        "source": holdings_cli_module.EARNINGS_REVIEW_SOURCE,
        "generated_at": payload["meta"]["generated_at"],
    }
    assert payload["data"]["mode"] == "public_research_only"
    assert payload["data"]["benchmark_trade_date"] == "2026-07-17"
    assert payload["data"]["earnings_review"]["screened_count"] == 2
    assert payload["data"]["earnings_review"]["selected_codes"] == [
        "002318",
        "000100",
    ]
    assert payload["data"]["decision"]["reason_code"] == (
        "earnings_evidence_only"
    )
    _assert_public_research_safety(payload)


def test_earnings_command_is_login_free_deduplicated_and_does_not_open_database(
    monkeypatch,
):
    calls = []
    context = make_hydrated_opportunity_market_context()
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )

    def fake_screener(codes, *, benchmark_trade_date):
        calls.append((list(codes), benchmark_trade_date))
        return _make_public_earnings_screen(codes)

    monkeypatch.setattr(
        holdings_cli_module,
        "screen_public_candidate_earnings_risk",
        fake_screener,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_get_database",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("earnings command must not open MongoDB")
        ),
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "earnings",
            "--code",
            "002318",
            "--code",
            "sz002318",
            "--code",
            "000100",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert calls == [(["002318", "000100"], "2026-07-17")]
    assert [
        item["code"]
        for item in payload["data"]["earnings_review"]["results"]
    ] == ["002318", "000100"]
    _assert_public_research_safety(payload)


@pytest.mark.parametrize("invalid_code", ["not-a-code", "abc000100xyz"])
def test_earnings_command_validates_codes_before_market_context(
    monkeypatch,
    invalid_code,
):
    context_called = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context_called.append(True),
    )

    result = CliRunner().invoke(
        holdings_app,
        ["earnings", "--code", invalid_code],
    )

    assert result.exit_code == 2
    assert context_called == []
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "invalid_earnings_code",
            "message": f"无效的 A 股代码: {invalid_code}",
            "stage": "earnings_forecast_review",
        },
    }


def test_earnings_command_rejects_more_than_eight_codes_before_market_context(
    monkeypatch,
):
    context_called = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context_called.append(True),
    )
    args = ["earnings"]
    for index in range(9):
        args.extend(["--code", f"60000{index}"])

    result = CliRunner().invoke(holdings_app, args)

    assert result.exit_code == 2
    assert context_called == []
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "too_many_earnings_codes"
    assert error["stage"] == "earnings_forecast_review"


def test_earnings_command_reports_actual_provider_failure_with_periods(monkeypatch):
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        make_hydrated_opportunity_market_context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "screen_public_candidate_earnings_risk",
        lambda _codes, *, benchmark_trade_date: {
            "status": "earnings_actual_unavailable",
            "source": holdings_cli_module.EARNINGS_FORECAST_SOURCE,
            "actual_source": holdings_cli_module.EARNINGS_ACTUAL_SOURCE,
            "report_period": "20260630",
            "actual_report_period": "20260331",
            "error_type": "TimeoutError",
            "results": [],
        },
    )

    result = CliRunner().invoke(
        holdings_app,
        ["earnings", "--code", "000100"],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "EarningsActualFetchError"
    assert error["stage"] == "earnings_forecast_review"
    assert error["details"] == {
        "provider_status": "earnings_actual_unavailable",
        "source": holdings_cli_module.EARNINGS_FORECAST_SOURCE,
        "actual_source": holdings_cli_module.EARNINGS_ACTUAL_SOURCE,
        "report_period": "20260630",
        "actual_report_period": "20260331",
        "error_type": "TimeoutError",
    }


def test_public_candidate_earnings_payload_rejects_unvalidated_provider_shape():
    def invalid_screener(codes, *, benchmark_trade_date):
        payload = _make_public_earnings_screen(codes)
        payload["results"][0]["blocks_new_position"] = True
        return payload

    with pytest.raises(CLIError) as exc_info:
        holdings_cli_module.build_public_candidate_earnings_payload(
            ["000100"],
            context=make_hydrated_opportunity_market_context(),
            screener=invalid_screener,
        )

    assert exc_info.value.code == "InvalidEarningsScreenMetadata"
    assert exc_info.value.exit_code == 4
    assert exc_info.value.stage == "earnings_forecast_review"


def test_build_public_candidate_notice_payload_is_validated_and_research_only():
    calls = []
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(
        2026,
        7,
        20,
        1,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    def fake_reviewer(codes, *, as_of_date):
        calls.append((list(codes), str(as_of_date)))
        return _make_public_notice_review(codes, notice_codes={"000100"})

    payload = holdings_cli_module.build_public_candidate_notice_payload(
        ["002318", "000100"],
        context=context,
        reviewer=fake_reviewer,
    )

    assert calls == [(["002318", "000100"], "2026-07-20")]
    assert payload["meta"] == {
        "schema_version": 1,
        "source": holdings_cli_module.NOTICE_REVIEW_SOURCE,
        "generated_at": payload["meta"]["generated_at"],
    }
    assert payload["data"]["mode"] == "public_research_only"
    assert payload["data"]["benchmark_trade_date"] == "2026-07-17"
    assert payload["data"]["market_session"]["session"] == "pre_open"
    assert payload["data"]["notice_review"]["reviewed_count"] == 2
    assert payload["data"]["notice_review"]["manual_review_code_count"] == 1
    assert payload["data"]["decision"]["reason_code"] == (
        "recent_notice_evidence_only"
    )
    _assert_public_research_safety(payload)


def test_notices_command_is_login_free_deduplicated_and_does_not_open_database(
    monkeypatch,
):
    calls = []
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(
        2026,
        7,
        20,
        1,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )

    def fake_reviewer(codes, *, as_of_date):
        calls.append((list(codes), str(as_of_date)))
        return _make_public_notice_review(codes, notice_codes={"000100"})

    monkeypatch.setattr(
        holdings_cli_module,
        "review_public_candidate_notices",
        fake_reviewer,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_get_database",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("notices command must not open MongoDB")
        ),
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "notices",
            "--code",
            "000100",
            "--code",
            "sz000100",
            "--code",
            "002318",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert calls == [(["000100", "002318"], "2026-07-20")]
    assert [
        item["code"]
        for item in payload["data"]["notice_review"]["results"]
    ] == ["000100", "002318"]
    assert "account" not in payload["data"]
    assert "holdings" not in payload["data"]
    _assert_public_research_safety(payload)


def test_notices_command_uses_code_specific_history_for_ninety_days(monkeypatch):
    calls = []
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(
        2026,
        7,
        20,
        1,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )

    def fake_history(codes, *, as_of_date, lookback_calendar_days):
        calls.append(
            (list(codes), str(as_of_date), lookback_calendar_days)
        )
        return _make_public_notice_history(
            codes,
            notice_codes={"600346"},
        )

    monkeypatch.setattr(
        holdings_cli_module,
        "review_public_candidate_notice_history",
        fake_history,
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "notices",
            "--code",
            "600346",
            "--lookback-days",
            "90",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert calls == [(["600346"], "2026-07-20", 90)]
    assert payload["meta"]["source"] == (
        holdings_cli_module.NOTICE_HISTORY_SOURCE
    )
    assert payload["data"]["notice_review"]["start_date"] == "2026-04-22"
    assert payload["data"]["notice_review"]["lookback_calendar_days"] == 90
    _assert_public_research_safety(payload)


@pytest.mark.parametrize("lookback_days", [0, 91])
def test_notices_command_rejects_invalid_lookback_before_market_context(
    monkeypatch,
    lookback_days,
):
    context_called = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context_called.append(True),
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "notices",
            "--code",
            "600346",
            "--lookback-days",
            str(lookback_days),
        ],
    )

    assert result.exit_code == 2
    assert context_called == []
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_notice_lookback_days"
    assert error["stage"] == "recent_notice_review"


@pytest.mark.parametrize("invalid_code", ["not-a-code", "abc000100xyz"])
def test_notices_command_validates_codes_before_market_context(
    monkeypatch,
    invalid_code,
):
    context_called = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context_called.append(True),
    )

    result = CliRunner().invoke(
        holdings_app,
        ["notices", "--code", invalid_code],
    )

    assert result.exit_code == 2
    assert context_called == []
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "invalid_notice_code",
            "message": f"无效的 A 股代码: {invalid_code}",
            "stage": "recent_notice_review",
        },
    }


def test_notices_command_rejects_more_than_eight_codes_before_market_context(
    monkeypatch,
):
    context_called = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context_called.append(True),
    )
    args = ["notices"]
    for index in range(9):
        args.extend(["--code", f"60000{index}"])

    result = CliRunner().invoke(holdings_app, args)

    assert result.exit_code == 2
    assert context_called == []
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "too_many_notice_codes"
    assert error["stage"] == "recent_notice_review"


def test_notices_command_reports_provider_failure_with_failed_date(monkeypatch):
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(2026, 7, 20, 1, 0)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "review_public_candidate_notices",
        lambda _codes, *, as_of_date: {
            "status": "notice_source_unavailable",
            "source": holdings_cli_module.NOTICE_REVIEW_SOURCE,
            "start_date": "2026-07-14",
            "end_date": "2026-07-20",
            "failed_date": "2026-07-18",
            "error_type": "TimeoutError",
            "results": [],
        },
    )

    result = CliRunner().invoke(
        holdings_app,
        ["notices", "--code", "000100"],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "NoticeReviewFetchError"
    assert error["stage"] == "recent_notice_review"
    assert error["details"] == {
        "provider_status": "notice_source_unavailable",
        "source": holdings_cli_module.NOTICE_REVIEW_SOURCE,
        "start_date": "2026-07-14",
        "end_date": "2026-07-20",
        "failed_date": "2026-07-18",
        "error_type": "TimeoutError",
    }


def test_public_candidate_notice_payload_rejects_unvalidated_provider_shape():
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(2026, 7, 20, 1, 0)

    def invalid_reviewer(codes, *, as_of_date):
        payload = _make_public_notice_review(codes, notice_codes={"000100"})
        payload["results"][0]["notices"][0]["actionable"] = True
        return payload

    with pytest.raises(CLIError) as exc_info:
        holdings_cli_module.build_public_candidate_notice_payload(
            ["000100"],
            context=context,
            reviewer=invalid_reviewer,
        )

    assert exc_info.value.code == "InvalidNoticeReviewMetadata"
    assert exc_info.value.exit_code == 4
    assert exc_info.value.stage == "recent_notice_review"


def test_public_quote_snapshot_preserves_tencent_valuation_evidence():
    snapshot = holdings_cli_module._quote_snapshot(
        {
            "source": "tencent",
            "code": "300113",
            "name": "顺网科技",
            "price": 17.01,
            "pe_ratio": 27.0,
            "pb_ratio": 4.7,
            "circ_mv": 8_658_000_000.0,
            "total_mv": 11_468_000_000.0,
        },
        {"code": "300113", "name": "顺网科技"},
    )

    sanitized = holdings_cli_module._sanitize_public_candidate_quote(snapshot)

    assert sanitized["pe_ratio"] == 27.0
    assert sanitized["pb_ratio"] == 4.7
    assert sanitized["circ_mv"] == 8_658_000_000.0
    assert sanitized["total_mv"] == 11_468_000_000.0


def _make_public_deep_check(
    discovery,
    *,
    codes=None,
    technical_codes=None,
    blocked_codes=(),
    actual_loss_codes=(),
):
    definitions_by_code = {
        definition["code"]: definition for definition in discovery["definitions"]
    }
    technical_selected_codes = technical_codes or codes or list(definitions_by_code)
    earnings_screen = _make_public_earnings_screen(
        technical_selected_codes,
        blocked_codes=blocked_codes,
        actual_loss_codes=actual_loss_codes,
    )
    selected_codes = codes or earnings_screen["selected_codes"]
    rejected_definitions = [
        definition
        for code, definition in definitions_by_code.items()
        if code not in technical_selected_codes
    ][:5]
    closest_rejections = [
        {
            "code": definition["code"],
            "name": definition["name"],
            "status": "net_rr_below_1_5",
            "net_reward_risk": round(1.49 - index / 100, 4),
            "min_net_reward_risk": 1.5,
            "gap_to_min_net_reward_risk": round(0.01 + index / 100, 4),
            "tencent_score": float(definition.get("tencent_score") or 0.0),
            "earnings_review_status": "not_reviewed",
            "actionable": False,
            "is_reference_only": True,
        }
        for index, definition in enumerate(rejected_definitions)
    ]
    candidates = []
    for code in selected_codes:
        definition = definitions_by_code.get(code, {})
        source_quote = discovery.get("quote_map", {}).get(code, {})
        candidates.append(
            {
                "code": code,
                "name": definition.get("name", "无关候选"),
                "theme": definition.get("theme"),
                "theme_label": definition.get("theme_label"),
                "quote": {
                    "source": "tencent",
                    "code": code,
                    "name": definition.get("name", "无关候选"),
                    "trade_at": source_quote.get("trade_at"),
                    "trade_date": source_quote.get("trade_date"),
                    "price": definition.get("tencent_price", 10.0),
                    "amount": source_quote.get("amount"),
                    "volume": source_quote.get("volume"),
                    "quote_volume": source_quote.get("quote_volume"),
                    "pe_ratio": source_quote.get("pe_ratio"),
                    "pb_ratio": source_quote.get("pb_ratio"),
                    "circ_mv": source_quote.get("circ_mv"),
                    "total_mv": source_quote.get("total_mv"),
                },
                "buy_lot_size": 100,
                "one_lot_amount": (
                    definition.get("tencent_price", 10.0) * 100
                ),
                "guarded_price_plan": {
                    "status": "history_unavailable",
                    "actionable": False,
                    "failed_gates": [],
                },
                "corporate_action": {
                    "status": "no_upcoming_corporate_action",
                    "blocks_new_position": False,
                    "price_plan_adjustment_required": False,
                    "nearest_action": None,
                    "is_reference_only": True,
                },
                "risk_flags": [],
                "triggers": {
                    "source": "configured_historical_reference",
                    "observation_zone": definition.get("observation_zone"),
                    "breakout_price": definition.get("breakout_price"),
                    "invalidation_price": definition.get("invalidation_price"),
                    "note": definition.get("note"),
                    "is_reference_only": True,
                },
                "is_reference_only": True,
            }
        )
    return {
        "status": "ok",
        "candidates": candidates,
        "technical_screen": {
            "status": "ok",
            "screened_count": len(definitions_by_code),
            "passed_count": len(technical_selected_codes),
            "selected_count": len(technical_selected_codes),
            "selected_codes": list(technical_selected_codes),
            "status_counts": {
                **(
                    {"ok": len(technical_selected_codes)}
                    if technical_selected_codes
                    else {}
                ),
                **(
                    {
                        "net_rr_below_1_5": (
                            len(definitions_by_code) - len(technical_selected_codes)
                        )
                    }
                    if len(definitions_by_code) > len(technical_selected_codes)
                    else {}
                ),
            },
            "closest_rejection_count": len(closest_rejections),
            "closest_rejections": closest_rejections,
        },
        "earnings_screen": earnings_screen,
    }


def _make_post_tencent_filtered_empty_discovery():
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["status"] = "no_eligible_candidates"
    discovery["definitions"] = []
    discovery["candidate_discovery"]["status"] = "no_eligible_candidates"
    discovery["candidate_discovery"]["selected_count"] = 0
    discovery["candidate_discovery"]["tencent_rank_population_count"] = 0
    return discovery


def _make_complete_public_snapshot(*, amount=1.0):
    exchange_specs = (
        ("sh", 600000, 200),
        ("sz", 0, 200),
        ("bj", 830000, 100),
    )
    rows = []
    exchange_counts = {}
    for exchange, first_code, count in exchange_specs:
        exchange_counts[exchange] = count
        rows.extend(
            {
                "code": f"{first_code + offset:06d}",
                "name": f"公开样本{exchange}{offset}",
                "exchange": exchange,
                "close": 10.0,
                "pct_chg": 1.0,
                "amount": amount,
                "trade_date": "2026-07-17",
            }
            for offset in range(count)
        )
    return {
        "status": "ok",
        "source": "akshare.sina.stock_zh_a_spot",
        "benchmark_trade_date": "2026-07-17",
        "provider_trade_date": "2026-07-17",
        "provider_expected_count": len(rows),
        "provider_expected_exchange_counts": dict(exchange_counts),
        "raw_row_count": len(rows),
        "unique_row_count": len(rows),
        "universe_count": len(rows),
        "exchange_counts": dict(exchange_counts),
        "total_coverage_ratio": 1.0,
        "exchange_coverage_ratio": {
            exchange: 1.0 for exchange in exchange_counts
        },
        "rows": rows,
    }


def _make_incomplete_public_snapshot():
    snapshot = _make_complete_public_snapshot()
    snapshot.update(
        {
            "status": "public_snapshot_coverage_incomplete",
            "provider_expected_count": 600,
            "provider_expected_exchange_counts": {
                "sh": 240,
                "sz": 240,
                "bj": 120,
            },
            "total_coverage_ratio": 500 / 600,
            "exchange_coverage_ratio": {
                "sh": 200 / 240,
                "sz": 200 / 240,
                "bj": 100 / 120,
            },
            "rows": [],
        }
    )
    return snapshot


def _make_public_command_payload(database_status):
    return {
        "ok": True,
        "data": {
            "mode": "research_only",
            "database": deepcopy(database_status),
            "account": {
                "status": "unavailable",
                "actionable": False,
                "configured_total_assets": None,
                "cash_or_unallocated": None,
                "estimated_equity": None,
            },
            "candidate_discovery": {
                "mode": "public_full_market",
                "status": "ok",
            },
            "candidates": [],
            "context": {"source": "public_full_market"},
        },
        "meta": {"schema_version": 7, "source": "public_full_market"},
    }


def _make_connected_account_fallback_payload(candidate_status="quote_universe_empty"):
    return {
        "ok": True,
        "data": {
            "account": {
                "configured_total_assets": 10_685.41,
                "total_assets": 10_685.41,
                "cash_or_unallocated": 10_685.41,
                "known_market_value": None,
                "known_profit_loss": None,
                "known_profit_loss_pct": None,
                "estimated_equity": 10_685.41,
                "buy_lot_size": 100,
                "is_reference_only": True,
            },
            "actionable_equity": {
                "value": 10_685.41,
                "status": "configured_total_assets",
                "actionable": True,
            },
            "holdings_risk": [],
            "trade_context": {
                "recent_count": 1,
                "last_trade": {
                    "code": "000977",
                    "name": "浪潮信息",
                    "side": "sell",
                    "sell_price": 70.4,
                    "realized_pnl": 640.0,
                },
                "recent_realized_pnl": 640.0,
                "is_reference_only": True,
            },
            "external_risk_gate": {
                "level": "red",
                "actionable": False,
            },
            "a_share_market_gate": {
                "level": "red",
                "new_position_allowed": False,
            },
            "candidate_discovery": {"status": candidate_status},
        },
        "meta": {"schema_version": 7},
    }


def test_public_research_account_context_preserves_zero_quantity_safety():
    public_payload = _make_public_command_payload({"status": "connected"})
    public_payload["data"].update(
        {
            "decision": {
                "action": "observe",
                "actionable": False,
                "suggested_lots": 0,
                "suggested_quantity": 0,
            },
            "candidates": [
                {
                    "code": "300059",
                    "name": "东方财富",
                    "one_lot_amount": 1_970.0,
                    "guarded_price_plan": {
                        "actionable": False,
                        "status": "invalid_price_ordering",
                        "execution_blocked_by": [
                            "quote_freshness",
                            "account_data_unavailable",
                        ],
                    },
                    "corporate_action": {"blocks_new_position": False},
                    "decision": {
                        "action": "observe",
                        "actionable": False,
                        "suggested_lots": 0,
                        "suggested_quantity": 0,
                    },
                },
                {
                    "code": "600900",
                    "name": "长江电力",
                    "one_lot_amount": 2_799.0,
                    "guarded_price_plan": {
                        "actionable": False,
                        "status": "insufficient_ordered_levels",
                    },
                    "corporate_action": {"blocks_new_position": False},
                    "decision": {
                        "action": "observe",
                        "actionable": False,
                        "suggested_lots": 0,
                        "suggested_quantity": 0,
                    },
                },
            ],
            "context": {
                "source": "public_full_market",
                "available_data": ["public_full_market_snapshot"],
                "unavailable_data": [
                    "account",
                    "holdings",
                    "cash",
                    "recent_trades",
                    "position_sizing",
                ],
            },
        }
    )

    payload = holdings_cli_module.build_account_context_public_research_payload(
        public_payload,
        _make_connected_account_fallback_payload(),
    )

    data = payload["data"]
    assert data["mode"] == "account_context_research_only"
    assert payload["meta"]["schema_version"] == 8
    assert data["account"] == {
        "status": "available",
        "actionable": False,
        "reason_code": "public_candidates_account_fit_only",
        "configured_total_assets": 10_685.41,
        "cash_or_unallocated": 10_685.41,
        "estimated_equity": 10_685.41,
        "buy_lot_size": 100,
        "holding_count": 0,
    }
    assert data["holdings_context"]["items"] == []
    assert data["recent_trade_context"] == {
        "recent_count": 1,
        "last_trade": {
            "code": "000977",
            "name": "浪潮信息",
            "side": "sell",
            "sell_price": 70.4,
            "realized_pnl": 640.0,
        },
        "recent_realized_pnl": 640.0,
        "is_reference_only": True,
    }
    first_fit = data["candidates"][0]["account_fit"]
    assert data["candidates"][0]["guarded_price_plan"][
        "execution_blocked_by"
    ] == ["quote_freshness"]
    assert first_fit["cash_affordable"] is True
    assert first_fit["within_single_symbol_cap"] is True
    assert first_fit["passes_account_size_checks"] is True
    assert first_fit["one_lot_cash_usage_pct"] == 18.44
    assert first_fit["one_lot_equity_pct"] == 18.44
    assert first_fit["blocking_reasons"] == [
        "technical_price_plan",
        "external_risk_gate",
        "a_share_market_gate",
        "public_research_only",
    ]
    second_fit = data["candidates"][1]["account_fit"]
    assert second_fit["cash_affordable"] is True
    assert second_fit["within_single_symbol_cap"] is True
    assert second_fit["passes_account_size_checks"] is True
    assert "post_trade_symbol_cap" not in second_fit["blocking_reasons"]
    assert data["decision"]["actionable"] is False
    assert data["decision"]["suggested_lots"] == 0
    assert all(candidate["decision"]["suggested_lots"] == 0 for candidate in data["candidates"])
    assert "account_context" in data["context"]["available_data"]
    assert "account" not in data["context"]["unavailable_data"]
    assert "position_sizing" in data["context"]["unavailable_data"]


@pytest.mark.parametrize(
    ("valuation_actionable", "expected_existing_value", "expected_reasons"),
    [
        (True, 2500.0, ["post_trade_symbol_cap", "public_research_only"]),
        (False, None, ["account_fit_data_incomplete", "public_research_only"]),
    ],
)
def test_public_account_fit_fails_closed_for_existing_symbol_valuation(
    valuation_actionable,
    expected_existing_value,
    expected_reasons,
):
    public_payload = _make_public_command_payload({"status": "connected"})
    public_payload["data"]["candidates"] = [
        {
            "code": "300059",
            "name": "东方财富",
            "one_lot_amount": 1_970.0,
            "guarded_price_plan": {"actionable": False, "status": "ok"},
            "corporate_action": {"blocks_new_position": False},
            "decision": {
                "action": "observe",
                "actionable": False,
                "suggested_lots": 0,
                "suggested_quantity": 0,
            },
        }
    ]
    mongo_payload = _make_connected_account_fallback_payload()
    mongo_payload["data"]["holdings_risk"] = [
        {
            "code": "300059",
            "name": "东方财富",
            "market": "CN",
            "quantity": 100,
            "market_value": 2500.0,
            "valuation_actionable": valuation_actionable,
            "risk_flags": [],
            "is_reference_only": True,
        }
    ]
    mongo_payload["data"]["external_risk_gate"] = {
        "level": "green",
        "actionable": True,
    }
    mongo_payload["data"]["a_share_market_gate"] = {
        "level": "green",
        "new_position_allowed": True,
    }

    payload = holdings_cli_module.build_account_context_public_research_payload(
        public_payload,
        mongo_payload,
    )

    account_fit = payload["data"]["candidates"][0]["account_fit"]
    assert account_fit["existing_symbol_market_value"] == expected_existing_value
    assert account_fit["passes_account_size_checks"] is False
    assert account_fit["blocking_reasons"] == expected_reasons
    assert payload["data"]["account"]["holding_count"] == 1


def test_public_account_fit_reports_one_lot_stop_loss_budget():
    public_payload = _make_public_command_payload({"status": "connected"})
    public_payload["data"]["candidates"] = [
        {
            "code": "300059",
            "name": "东方财富",
            "one_lot_amount": 1_970.0,
            "guarded_price_plan": {
                "actionable": False,
                "status": "ok",
                "fee_aware_trade": {"risk_amount": 120.0},
            },
            "corporate_action": {"blocks_new_position": False},
            "decision": {
                "action": "observe",
                "actionable": False,
                "suggested_lots": 0,
                "suggested_quantity": 0,
            },
        }
    ]
    mongo_payload = _make_connected_account_fallback_payload()
    mongo_payload["data"]["external_risk_gate"] = {
        "level": "green",
        "actionable": True,
    }
    mongo_payload["data"]["a_share_market_gate"] = {
        "level": "green",
        "new_position_allowed": True,
    }

    payload = holdings_cli_module.build_account_context_public_research_payload(
        public_payload,
        mongo_payload,
    )

    account_fit = payload["data"]["candidates"][0]["account_fit"]
    assert account_fit["within_single_symbol_cap"] is True
    assert account_fit["one_lot_planned_loss"] == 120.0
    assert account_fit["per_position_loss_budget_amount"] == 106.85
    assert account_fit["within_per_position_loss_budget"] is False
    assert account_fit["passes_account_risk_checks"] is False
    assert "one_lot_loss_budget" in account_fit["blocking_reasons"]


def test_public_account_fit_blocks_candidate_waiting_for_trend_recovery():
    public_payload = _make_public_command_payload({"status": "connected"})
    public_payload["data"]["candidates"] = [
        {
            "code": "000519",
            "name": "中兵红箭",
            "one_lot_amount": 1_363.0,
            "guarded_price_plan": {"actionable": False, "status": "ok"},
            "corporate_action": {"blocks_new_position": False},
            "risk_flags": [
                {
                    "key": "trend_recovery_required",
                    "level": "warning",
                    "message": "test",
                }
            ],
            "decision": {
                "action": "observe",
                "actionable": False,
                "suggested_lots": 0,
                "suggested_quantity": 0,
            },
        }
    ]
    mongo_payload = _make_connected_account_fallback_payload()
    mongo_payload["data"]["external_risk_gate"] = {
        "level": "green",
        "actionable": True,
    }
    mongo_payload["data"]["a_share_market_gate"] = {
        "level": "green",
        "new_position_allowed": True,
    }

    payload = holdings_cli_module.build_account_context_public_research_payload(
        public_payload,
        mongo_payload,
    )

    account_fit = payload["data"]["candidates"][0]["account_fit"]
    assert account_fit["passes_account_size_checks"] is True
    assert account_fit["blocking_reasons"] == [
        "trend_recovery_required",
        "public_research_only",
    ]


def test_public_account_context_blocks_recently_sold_matching_candidate():
    public_payload = _make_public_command_payload({"status": "connected"})
    public_payload["data"]["candidates"] = [
        {
            "code": "300059",
            "name": "东方财富",
            "one_lot_amount": 1_970.0,
            "guarded_price_plan": {"actionable": False, "status": "ok"},
            "corporate_action": {"blocks_new_position": False},
            "decision": {
                "action": "observe",
                "actionable": False,
                "suggested_lots": 0,
                "suggested_quantity": 0,
            },
        }
    ]
    mongo_payload = _make_connected_account_fallback_payload()
    sale = {
        "code": "300059",
        "name": "东方财富",
        "market": "CN",
        "side": "sell",
        "quantity": 100,
        "sell_price": 19.9,
        "realized_pnl": 120.0,
        "sold_at": "2026-07-17T10:00:00+08:00",
        "created_at": "2026-07-17T02:00:00Z",
    }
    mongo_payload["data"]["trade_context"] = {
        "recent_trades": [sale],
        "recent_count": 1,
        "last_trade": sale,
        "recent_realized_pnl": 120.0,
        "is_reference_only": True,
    }
    mongo_payload["data"]["external_risk_gate"] = {
        "level": "green",
        "actionable": True,
    }
    mongo_payload["data"]["a_share_market_gate"] = {
        "level": "green",
        "new_position_allowed": True,
    }

    payload = holdings_cli_module.build_account_context_public_research_payload(
        public_payload,
        mongo_payload,
        as_of=datetime(2026, 7, 19, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        benchmark_session_dates=["2026-07-17"],
    )

    policy = payload["data"]["recent_sale_policy"]
    assert policy["status"] == "cooldown"
    assert policy["cooldown_active"] is True
    assert policy["matched_candidate_codes"] == ["300059"]
    account_fit = payload["data"]["candidates"][0]["account_fit"]
    assert account_fit["passes_account_size_checks"] is True
    assert account_fit["blocking_reasons"] == [
        "recent_sale_cooldown",
        "public_research_only",
    ]
    assert account_fit["actionable"] is False
    assert account_fit["suggested_lots"] == 0


@pytest.mark.parametrize("catch_point", ["run_json", "market_status", "opportunities"])
@pytest.mark.parametrize(
    "details",
    [None, {"stage": "sina_snapshot", "diagnostic": {"status": "failed"}}],
    ids=["legacy", "structured"],
)
def test_cli_error_details_are_optional_at_every_catch_point(
    monkeypatch,
    catch_point,
    details,
):
    error_kwargs = {
        "code": "test_error",
        "exit_code": 4,
    }
    if details is not None:
        error_kwargs["details"] = details
    error = CLIError("test message", **error_kwargs)

    def raise_error(*_args, **_kwargs):
        raise error

    if catch_point == "run_json":
        monkeypatch.setattr(holdings_cli_module, "_get_database", lambda: object())
        monkeypatch.setattr(holdings_cli_module, "build_holdings_payload", raise_error)
        arguments = ["list"]
    elif catch_point == "market_status":
        monkeypatch.setattr(
            holdings_cli_module,
            "_optional_market_database",
            lambda **_kwargs: (None, {"status": "unavailable"}),
        )
        monkeypatch.setattr(
            holdings_cli_module,
            "build_market_status_payload",
            raise_error,
        )
        arguments = ["market-status"]
    else:
        monkeypatch.setattr(
            holdings_cli_module,
            "build_opportunity_market_context",
            raise_error,
        )
        arguments = ["opportunities"]

    result = CliRunner().invoke(holdings_app, arguments)

    assert result.exit_code == 4
    assert result.stdout == ""
    expected_error = {"code": "test_error", "message": "test message"}
    if details is not None:
        expected_error["details"] = details
    assert json.loads(result.stderr) == {"ok": False, "error": expected_error}


def test_opportunities_uses_public_research_when_mongo_is_unavailable_without_manual_codes(
    monkeypatch,
):
    context = make_opportunity_market_context()
    public_calls = []
    database_status = {"status": "unavailable", "error_code": "database_error"}
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (None, database_status),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Mongo builder must not run without a database")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual research builder requires manual codes")
        ),
    )

    def fake_public_builder(**kwargs):
        public_calls.append(kwargs)
        return _make_public_command_payload(kwargs["database_status"])

    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        fake_public_builder,
        raising=False,
    )

    result = CliRunner().invoke(holdings_app, ["opportunities", "--username", "hermes"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["data"]["mode"] == "research_only"
    assert payload["data"]["context"]["source"] == "public_full_market"
    assert payload["data"]["account"]["configured_total_assets"] is None
    assert payload["data"]["account"]["cash_or_unallocated"] is None
    assert payload["data"]["account"]["estimated_equity"] is None
    assert {"user", "holdings", "holdings_risk", "trade_context"}.isdisjoint(
        payload["data"]
    )
    assert len(public_calls) == 1
    assert public_calls[0] == {
        "context": context,
        "external_risk_level": None,
        "database_status": database_status,
    }


def test_opportunities_connected_public_fallback_keeps_account_context(
    monkeypatch,
):
    context = make_opportunity_market_context()
    database = object()
    mongo_payload = _make_connected_account_fallback_payload()
    public_calls = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (database, {"status": "connected"}),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda db, **_kwargs: mongo_payload if db is database else None,
    )

    def fake_public_builder(**kwargs):
        public_calls.append(kwargs)
        return _make_public_command_payload(kwargs["database_status"])

    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        fake_public_builder,
        raising=False,
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--username", "admin"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["data"]["mode"] == "account_context_research_only"
    assert payload["data"]["account"]["configured_total_assets"] == 10_685.41
    assert payload["data"]["account"]["cash_or_unallocated"] == 10_685.41
    assert payload["data"]["account"]["holding_count"] == 0
    assert payload["data"]["decision"]["suggested_lots"] == 0
    assert payload["meta"]["schema_version"] == 8
    assert public_calls == [
        {
            "context": context,
            "external_risk_level": None,
            "database_status": {"status": "connected"},
        }
    ]


@pytest.mark.parametrize(
    ("candidate_status", "expected_public_calls"),
    [
        ("candidate_discovery_unavailable", 1),
        ("quote_universe_empty", 1),
        ("stale_quote_universe", 1),
        ("quote_universe_too_small", 1),
        ("no_eligible_candidates", 0),
        ("cash_unavailable", 0),
        ("benchmark_calendar_unavailable", 0),
    ],
)
def test_opportunities_public_fallback_status_matrix(
    monkeypatch,
    candidate_status,
    expected_public_calls,
):
    context = make_opportunity_market_context()
    database = object()
    public_calls = []
    mongo_payload = {
        "ok": True,
        "data": {
            "mode": "mongo_full",
            "candidate_discovery": {"status": candidate_status},
        },
        "meta": {"schema_version": 7},
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (database, {"status": "connected"}),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda db, **_kwargs: mongo_payload if db is database else None,
    )

    def fake_public_builder(**kwargs):
        public_calls.append(kwargs)
        return _make_public_command_payload(kwargs["database_status"])

    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        fake_public_builder,
        raising=False,
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 0
    assert len(public_calls) == expected_public_calls
    payload = json.loads(result.stdout)
    if expected_public_calls:
        assert payload["data"]["mode"] == "research_only"
        assert public_calls[0]["database_status"] == {"status": "connected"}
    else:
        assert payload == mongo_payload


def test_opportunities_lazy_mongo_error_without_manual_codes_uses_public_research(
    monkeypatch,
):
    context = make_opportunity_market_context()
    database = object()
    public_calls = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (database, {"status": "connected"}),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            holdings_cli_module.PyMongoError("lazy read failed")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual fallback must not run without manual codes")
        ),
    )

    def fake_public_builder(**kwargs):
        public_calls.append(kwargs)
        return _make_public_command_payload(kwargs["database_status"])

    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        fake_public_builder,
        raising=False,
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 0
    assert len(public_calls) == 1
    assert public_calls[0]["database_status"] == {
        "status": "unavailable",
        "error_code": "database_error",
    }
    assert json.loads(result.stdout)["data"]["mode"] == "research_only"


@pytest.mark.parametrize("database_available", [True, False])
def test_manual_candidate_codes_never_invoke_public_discovery(
    monkeypatch,
    database_available,
):
    context = make_opportunity_market_context()
    database = object() if database_available else None
    events = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (
            database,
            {"status": "connected"}
            if database_available
            else {"status": "unavailable", "error_code": "database_error"},
        ),
    )

    def fake_full_builder(db, **kwargs):
        events.append(("full", db, kwargs["candidate_codes"]))
        return {
            "ok": True,
            "data": {"mode": "manual_full"},
            "meta": {"schema_version": 7},
        }

    def fake_manual_builder(**kwargs):
        events.append(("manual", kwargs["candidate_codes"]))
        return {
            "ok": True,
            "data": {"mode": "manual_research"},
            "meta": {"schema_version": 7},
        }

    monkeypatch.setattr(holdings_cli_module, "build_opportunities_payload", fake_full_builder)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        fake_manual_builder,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual candidate codes must bypass public discovery")
        ),
        raising=False,
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 0
    assert events == (
        [("full", database, ["601728"])]
        if database_available
        else [("manual", ["601728"])]
    )


def test_public_discovery_unavailable_is_structured_stderr_error(monkeypatch):
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: {
            "status": "public_breadth_fetch_failed",
            "source": "akshare.sina.stock_zh_a_spot",
            "rows": [],
        }
    )
    discovery = _make_public_research_discovery(
        "candidate_discovery_unavailable",
        candidate_count=0,
    )
    discovery["stage"] = "sina_snapshot"
    discovery["candidate_discovery"]["stage_sources"]["public_snapshot"][
        "status"
    ] = "public_breadth_fetch_failed"
    expected_details = {
        "stage": "sina_snapshot",
        "candidate_discovery": deepcopy(discovery["candidate_discovery"]),
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        lambda *_args, **_kwargs: discovery,
        raising=False,
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "candidate_discovery_unavailable",
            "message": "公开全市场候选发现不可用",
            "details": expected_details,
        },
    }
    assert "rows" not in expected_details["candidate_discovery"]


def test_public_builder_error_preserves_valid_failure_snapshot_coverage(monkeypatch):
    snapshot = _make_incomplete_public_snapshot()
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: deepcopy(snapshot)
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coverage failure must not call Tencent candidates")
        ),
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module._build_public_full_market_research_payload(
            context=context,
            database_status={"status": "unavailable", "error_code": "database_error"},
        )

    assert caught.value.code == "candidate_discovery_unavailable"
    assert caught.value.exit_code == 4
    assert caught.value.details["stage"] == "sina_snapshot"
    candidate_discovery = caught.value.details["candidate_discovery"]
    assert candidate_discovery["provider_expected_count"] == 600
    assert candidate_discovery["provider_expected_exchange_counts"] == {
        "sh": 240,
        "sz": 240,
        "bj": 120,
    }
    assert candidate_discovery["raw_row_count"] == 500
    assert candidate_discovery["unique_row_count"] == 500
    assert candidate_discovery["universe_count"] == 500
    assert candidate_discovery["exchange_counts"] == {
        "sh": 200,
        "sz": 200,
        "bj": 100,
    }
    assert candidate_discovery["total_coverage_ratio"] == pytest.approx(500 / 600)
    assert candidate_discovery["exchange_coverage_ratio"] == pytest.approx(
        {"sh": 200 / 240, "sz": 200 / 240, "bj": 100 / 120}
    )


@pytest.mark.parametrize(
    "failure_status",
    [
        "public_snapshot_coverage_incomplete",
        "public_breadth_universe_too_small",
    ],
)
def test_public_builder_error_clears_failure_metrics_that_contradict_status(
    monkeypatch,
    failure_status,
):
    snapshot = _make_complete_public_snapshot()
    snapshot["status"] = failure_status
    snapshot["rows"] = []
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: deepcopy(snapshot)
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid failure metrics must not call Tencent candidates")
        ),
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module._build_public_full_market_research_payload(
            context=context,
            database_status={"status": "unavailable", "error_code": "database_error"},
        )

    candidate_discovery = caught.value.details["candidate_discovery"]
    assert caught.value.details["stage"] == "sina_snapshot"
    assert candidate_discovery["provider_expected_count"] == 0
    assert candidate_discovery["provider_expected_exchange_counts"] == {
        "sh": 0,
        "sz": 0,
        "bj": 0,
    }
    assert candidate_discovery["raw_row_count"] == 0
    assert candidate_discovery["unique_row_count"] == 0
    assert candidate_discovery["universe_count"] == 0
    assert candidate_discovery["exchange_counts"] == {"sh": 0, "sz": 0, "bj": 0}
    assert candidate_discovery["total_coverage_ratio"] == 0.0
    assert candidate_discovery["exchange_coverage_ratio"] == {
        "sh": 0.0,
        "sz": 0.0,
        "bj": 0.0,
    }


def test_opportunities_short_circuits_failed_tencent_context_before_public_io(
    monkeypatch,
):
    calls = []

    def unexpected_public_snapshot(**_kwargs):
        calls.append("public_snapshot")
        raise AssertionError("failed Tencent context must stop before Sina")

    context = make_opportunity_market_context(
        public_snapshot_fetcher=unexpected_public_snapshot
    )
    context.index_status = "index_fetch_failed"
    context.benchmark_trade_date = None
    context.index_quotes = []
    context.index_error = {
        "status": "index_fetch_failed",
        "stage": "tencent_market_context",
        "error_type": "WorkerProcessError",
        "details": {
            "worker_exit_code": 7,
            "provider": "tencent_batch_quotes",
        },
    }
    expected_index_error = deepcopy(context.index_error)

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda *_args, **_kwargs: calls.append("candidate_batch"),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        lambda *_args, **_kwargs: calls.append("candidate_discovery"),
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert calls == []
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "candidate_discovery_unavailable"
    assert "stage" not in payload["error"]
    assert payload["error"]["details"]["stage"] == "tencent_market_context"
    candidate_discovery = payload["error"]["details"]["candidate_discovery"]
    assert candidate_discovery["status"] == "candidate_discovery_unavailable"
    assert candidate_discovery["stage_sources"] == {
        "tencent_market_context": {
            "provider": "tencent_batch_quotes",
            "status": "index_fetch_failed",
            "error": expected_index_error,
        },
        "public_snapshot": {
            "provider": "akshare.sina.stock_zh_a_spot",
            "status": "not_called_tencent_market_context_unavailable",
        },
        "tencent_verification": {
            "provider": "tencent_batch_quotes",
            "status": "not_called_tencent_market_context_unavailable",
        },
    }
    assert context.index_status == "index_fetch_failed"
    assert context.index_error == expected_index_error
    assert context.public_snapshot_loaded is False


def test_public_builder_short_circuits_ok_context_without_benchmark_date():
    calls = []

    def unexpected_public_snapshot(**_kwargs):
        calls.append("public_snapshot")
        raise AssertionError("missing benchmark date must stop before Sina")

    context = make_opportunity_market_context(
        public_snapshot_fetcher=unexpected_public_snapshot
    )
    context.benchmark_trade_date = None

    with pytest.raises(CLIError) as caught:
        holdings_cli_module._build_public_full_market_research_payload(
            context=context,
            database_status={"status": "unavailable"},
        )

    assert caught.value.code == "candidate_discovery_unavailable"
    assert caught.value.exit_code == 4
    assert caught.value.details["stage"] == "tencent_market_context"
    assert calls == []
    candidate_discovery = caught.value.details["candidate_discovery"]
    assert candidate_discovery["status"] == "candidate_discovery_unavailable"
    assert candidate_discovery["stage_sources"]["tencent_market_context"] == {
        "provider": "tencent_batch_quotes",
        "status": "benchmark_trade_date_unavailable",
        "error": {
            "status": "benchmark_trade_date_unavailable",
            "stage": "tencent_market_context",
            "index_status": "ok",
            "benchmark_trade_date": None,
        },
    }
    assert context.public_snapshot_loaded is False


def test_opportunities_command_normalizes_public_deep_check_failure_details(monkeypatch):
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "rows": [],
        }
    )
    discovery = _make_public_research_discovery(candidate_count=1)
    expected_candidate_discovery = deepcopy(discovery["candidate_discovery"])
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        lambda *_args, **_kwargs: deepcopy(discovery),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        lambda *_args, **_kwargs: {
            "status": "technical_deep_check_failed",
            "candidates": [],
        },
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "code": "candidate_discovery_unavailable",
            "message": "公开全市场候选发现不可用",
            "details": {
                "stage": "technical_deep_check",
                "candidate_discovery": expected_candidate_discovery,
            },
        },
    }
    assert discovery["candidate_discovery"] == expected_candidate_discovery


def test_complete_public_scan_with_no_candidates_is_stdout_success(monkeypatch):
    snapshot = _make_complete_public_snapshot(amount=1.0)
    snapshot_calls = []

    def fetch_snapshot(**_kwargs):
        snapshot_calls.append(True)
        return deepcopy(snapshot)

    context = make_opportunity_market_context(
        public_snapshot_fetcher=fetch_snapshot,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda **_kwargs: (
            None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Tencent batch must be skipped when public preselection is empty")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deep check must be skipped when no candidates are eligible")
        ),
        raising=False,
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["mode"] == "research_only"
    assert payload["data"]["candidate_discovery"]["status"] == (
        "no_eligible_candidates"
    )
    assert payload["data"]["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "not_called_no_preselection"
    assert payload["data"]["candidates"] == []
    assert payload["data"]["context"]["source"] == "public_full_market"
    assert payload["meta"]["schema_version"] == 7
    assert snapshot_calls == [True]


def test_public_workflow_reuses_snapshot_and_batches_quotes_and_deep_check_once(
    monkeypatch,
):
    snapshot_calls = []
    batch_calls = []
    deep_calls = []
    market_contexts = []
    snapshot = {"status": "ok", "source": "test.public", "rows": []}

    def fetch_snapshot(**_kwargs):
        snapshot_calls.append(True)
        return deepcopy(snapshot)

    context = make_opportunity_market_context(
        public_snapshot_fetcher=fetch_snapshot,
    )
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["quote_map"]["000001"] = {
        "code": "000001",
        "source": "tencent",
        "close": 9.99,
    }

    def fake_discovery(snapshot_arg, *, fetch_quotes, now):
        assert snapshot_arg == snapshot
        assert now is context.now
        fetch_result = fetch_quotes(["600000"])
        assert fetch_result["requested_codes"] == ["600000"]
        return discovery

    def fake_batch(codes, *, timeout):
        batch_calls.append({"codes": list(codes), "timeout": timeout})
        return {
            "status": "ok",
            "requested_codes": list(codes),
            "rows": [],
            "error_type": None,
        }

    def fake_deep(
        definitions,
        quote_map,
        *,
        benchmark_trade_date,
        command_remaining_seconds,
    ):
        deep_calls.append(
            {
                "definitions": deepcopy(definitions),
                "quote_map": deepcopy(quote_map),
                "benchmark_trade_date": benchmark_trade_date,
                "remaining": command_remaining_seconds,
            }
        )
        return _make_public_deep_check(discovery)

    def fake_market_status(db=None, *, database_status=None, context=None):
        market_contexts.append(context)
        assert context.ensure_public_snapshot() == snapshot
        return _fake_public_market_status(
            db,
            database_status=database_status,
            context=context,
        )

    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        fake_discovery,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        fake_batch,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        fake_deep,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        fake_market_status,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public workflow must not refetch individual quotes")
        ),
    )

    payload = holdings_cli_module._build_public_full_market_research_payload(
        context=context,
        external_risk_level="yellow",
        database_status={"status": "connected"},
    )

    assert payload["ok"] is True
    assert payload["meta"]["schema_version"] == 7
    assert snapshot_calls == [True]
    assert batch_calls == [{"codes": ["600000"], "timeout": 10.0}]
    assert len(deep_calls) == 1
    assert deep_calls[0]["benchmark_trade_date"] == "2026-07-17"
    assert [item["code"] for item in deep_calls[0]["definitions"]] == ["600000"]
    assert set(deep_calls[0]["quote_map"]) == {"600000"}
    assert deep_calls[0]["remaining"] == 50.0
    assert market_contexts == [context]


def test_public_workflow_deep_timeout_is_successful_zero_share_observation(
    monkeypatch,
):
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: {
            "status": "ok",
            "source": "test.public",
            "rows": [],
        }
    )
    discovery = _make_public_research_discovery(candidate_count=1)

    def fake_discovery(_snapshot, *, fetch_quotes, now):
        assert now is context.now
        fetch_quotes(["600000"])
        return discovery

    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        fake_discovery,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda codes, *, timeout: {
            "status": "ok",
            "requested_codes": list(codes),
            "rows": [],
            "error_type": None,
        },
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        lambda *_args, **_kwargs: {
            "status": "technical_deep_check_timeout",
            "candidates": [],
        },
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module._build_public_full_market_research_payload(
        context=context,
        database_status={"status": "unavailable", "error_code": "database_error"},
    )

    assert payload["ok"] is True
    assert payload["data"]["context"]["technical_deep_check_status"] == "timeout"
    assert payload["data"]["candidate_discovery"]["stage_sources"][
        "technical_deep_check"
    ]["status"] == "timeout"
    assert len(payload["data"]["candidates"]) == 1
    candidate = payload["data"]["candidates"][0]
    assert candidate["plan_status"] == "technical_deep_check_timeout"
    assert candidate["decision"]["suggested_lots"] == 0
    assert candidate["decision"]["suggested_quantity"] == 0
    assert candidate["decision"]["actionable"] is False


def test_public_technical_funnel_timeout_fails_closed_without_partial_candidates(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module.build_public_research_opportunities_payload(
            discovery,
            {
                "status": "technical_deep_check_timeout",
                "mode": "technical_funnel",
                "candidates": [],
            },
            context=make_hydrated_opportunity_market_context(),
        )

    assert caught.value.code == "technical_deep_check_timeout"
    assert caught.value.stage == "technical_deep_check"
    assert caught.value.exit_code == 4


def test_public_workflow_preserves_technical_funnel_timeout_error(monkeypatch):
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: {
            "status": "ok",
            "source": "test.public",
            "rows": [],
        }
    )
    discovery = _make_public_research_discovery(candidate_count=1)

    def fake_discovery(_snapshot, *, fetch_quotes, now):
        assert now is context.now
        fetch_quotes(["600000"])
        return discovery

    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        fake_discovery,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        lambda codes, *, timeout: {
            "status": "ok",
            "requested_codes": list(codes),
            "rows": [],
            "error_type": None,
        },
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        lambda *_args, **_kwargs: {
            "status": "technical_deep_check_timeout",
            "mode": "technical_funnel",
            "candidates": [],
        },
        raising=False,
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module._build_public_full_market_research_payload(
            context=context,
            database_status={"status": "unavailable", "error_code": "database_error"},
        )

    assert caught.value.code == "technical_deep_check_timeout"
    assert caught.value.stage == "technical_deep_check"
    assert caught.value.exit_code == 4


@pytest.mark.parametrize(
    ("expired_stage", "expected_stage"),
    [
        ("sina_public_snapshot", "sina_public_snapshot"),
        ("candidate_discovery", "candidate_discovery"),
        ("tencent_candidate_review", "tencent_candidate_review"),
        ("technical_deep_inspection", "technical_deep_inspection"),
        ("orchestration", "orchestration"),
    ],
)
def test_public_workflow_enforces_deadline_after_each_stage(
    monkeypatch,
    expired_stage,
    expected_stage,
):
    clock = {"value": 0.0}

    def expire(stage):
        if expired_stage == stage:
            clock["value"] = 90.01

    def fetch_snapshot(**_kwargs):
        expire("sina_public_snapshot")
        return {"status": "ok", "source": "test.public", "rows": []}

    context = make_opportunity_market_context(
        public_snapshot_fetcher=fetch_snapshot,
        monotonic=lambda: clock["value"],
    )
    discovery = _make_public_research_discovery(candidate_count=1)

    def fake_discovery(_snapshot, *, fetch_quotes, now):
        if expired_stage == "candidate_discovery":
            expire("candidate_discovery")
            return _make_public_research_discovery(
                "no_eligible_candidates",
                candidate_count=0,
            )
        fetch_quotes(["600000"])
        return discovery

    def fake_batch(codes, *, timeout):
        expire("tencent_candidate_review")
        return {
            "status": "ok",
            "requested_codes": list(codes),
            "rows": [],
            "error_type": None,
        }

    def fake_deep(*_args, **_kwargs):
        expire("technical_deep_inspection")
        return _make_public_deep_check(discovery)

    def fake_payload_builder(*_args, **_kwargs):
        expire("orchestration")
        return {"ok": True, "data": {}, "meta": {"schema_version": 7}}

    monkeypatch.setattr(
        holdings_cli_module,
        "discover_public_candidate_universe",
        fake_discovery,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quotes_batched_sync",
        fake_batch,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "run_public_candidate_technical_funnel",
        fake_deep,
        raising=False,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_public_research_opportunities_payload",
        fake_payload_builder,
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module._build_public_full_market_research_payload(
            context=context,
            database_status={"status": "unavailable"},
        )

    assert caught.value.code == "stage_timeout"
    assert caught.value.exit_code == 4
    assert caught.value.stage == expected_stage


def _assert_public_payload_error(
    monkeypatch,
    discovery,
    deep_check,
    *,
    code,
    stage,
):
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module.build_public_research_opportunities_payload(
            discovery,
            deep_check,
            context=make_hydrated_opportunity_market_context(),
        )

    assert caught.value.code == code
    assert caught.value.exit_code == 4
    assert caught.value.stage == stage


@pytest.mark.parametrize(
    ("mismatch_kind", "deep_codes"),
    [
        ("missing_candidate", ["600000"]),
        ("duplicate_candidate", ["600000", "600000"]),
        ("unrelated_candidate", ["600000", "000001"]),
    ],
)
def test_public_research_payload_rejects_unbound_deep_check_candidates(
    monkeypatch,
    mismatch_kind,
    deep_codes,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    deep_check = _make_public_deep_check(discovery, codes=deep_codes)
    deep_check.pop("technical_screen")
    deep_check.pop("earnings_screen")

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        deep_check,
        code="technical_deep_check_failed",
        stage="technical_deep_check",
    )


def test_public_research_payload_reorders_deep_check_candidates_to_discovery_order(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    deep_check = _make_public_deep_check(
        discovery,
        codes=["600001", "600000"],
    )
    deep_check.pop("technical_screen")
    deep_check.pop("earnings_screen")
    original_deep_check = deepcopy(deep_check)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    assert [
        candidate["code"] for candidate in payload["data"]["candidates"]
    ] == ["600000", "600001"]
    assert [
        candidate["name"]
        for candidate in payload["data"]["candidates"]
    ] == ["公开候选0", "公开候选1"]
    assert deep_check == original_deep_check


def test_public_research_payload_adapts_real_flat_discovery_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    assert all("priority" not in item for item in discovery["definitions"])
    assert all("discovery" not in item for item in discovery["definitions"])
    deep_check = _make_public_deep_check(
        discovery,
        codes=["600001", "600000"],
    )
    deep_check.pop("technical_screen")
    deep_check.pop("earnings_screen")
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidates = payload["data"]["candidates"]
    assert [candidate["priority"] for candidate in candidates] == [1, 2]
    first = candidates[0]["discovery"]
    assert first == {
        "source": "public_full_market",
        "trade_date": "2026-07-17",
        "public_rank": 1,
        "objective": {
            "id": "technology_new_quality_productive_forces",
            "label": "科技 + 新质生产力",
            "tier": "non_core",
            "tier_label": "非核心方向",
            "segment": "其他行业",
            "match_score": 0.0,
            "reason": "测试候选未匹配核心方向。",
        },
        "public": {
            "bucket": "strength",
            "score": 0.90,
            "price": 10.25,
            "pct_change": 1.25,
            "amount": 321_000_000.0,
            "one_lot_amount": 1_025.0,
            "amount_percentile": 0.95,
            "move_quality": 0.85,
        },
        "tencent": {
            "source": "tencent_batch_quotes",
            "bucket": "strength",
            "score": 0.89,
            "price": 10.25,
            "pct_change": 2.5,
            "amount": 321_000_000.0,
            "trade_at": "2026-07-17T10:00:00+08:00",
            "amount_percentile": 0.92,
            "market_cap_percentile": 0.72,
            "move_quality": 0.88,
            "turnover_rate": 2.1,
            "turnover_quality": 1.0,
            "volume_ratio": 1.3,
            "volume_ratio_quality": 1.0,
            "amplitude": 3.2,
            "amplitude_quality": 1.0,
            "circ_mv": 40_000_000_000.0,
            "total_mv": 50_000_000_000.0,
            "limit_up": 11.28,
        },
    }
    assert candidates[1]["discovery"]["public_rank"] == 2
    assert candidates[1]["discovery"]["tencent"]["score"] == 0.79
    _assert_public_research_safety(payload)


def test_public_research_payload_preserves_tencent_selection_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0].update(
        {
            "tencent_one_lot_amount": 1_025.0,
            "tencent_quality_rank": 7,
            "selection_lane": "one_lot_diversity",
        }
    )
    deep_check = _make_public_deep_check(discovery)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    tencent_evidence = payload["data"]["candidates"][0]["discovery"][
        "tencent"
    ]
    assert tencent_evidence["one_lot_amount"] == 1_025.0
    assert tencent_evidence["quality_rank"] == 7
    assert tencent_evidence["selection_lane"] == "one_lot_diversity"


@pytest.mark.parametrize(
    "selection_evidence",
    [
        {"tencent_quality_rank": 7},
        {
            "tencent_one_lot_amount": 1_026.0,
            "tencent_quality_rank": 7,
            "selection_lane": "one_lot_diversity",
        },
        {
            "tencent_one_lot_amount": 1_025.0,
            "tencent_quality_rank": 0,
            "selection_lane": "one_lot_diversity",
        },
        {
            "tencent_one_lot_amount": 1_025.0,
            "tencent_quality_rank": 7,
            "selection_lane": "unknown",
        },
    ],
)
def test_public_research_payload_rejects_invalid_tencent_selection_evidence(
    monkeypatch,
    selection_evidence,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0].update(selection_evidence)
    deep_check = _make_public_deep_check(discovery)

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        deep_check,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_preserves_finite_decimal_discovery_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0]["public_score"] = Decimal("0.90")
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        context=make_hydrated_opportunity_market_context(),
    )

    assert payload["data"]["candidates"][0]["discovery"]["public"][
        "score"
    ] == Decimal("0.90")


def test_public_research_timeout_preserves_real_flat_discovery_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        context=make_hydrated_opportunity_market_context(),
    )

    candidates = payload["data"]["candidates"]
    assert [candidate["priority"] for candidate in candidates] == [1, 2]
    assert candidates[0]["discovery"]["public"]["score"] == 0.90
    assert candidates[0]["discovery"]["tencent"]["score"] == 0.89
    assert candidates[1]["discovery"]["public_rank"] == 2
    assert candidates[1]["discovery"]["tencent"]["bucket"] == "pullback"
    _assert_public_research_safety(payload)


def test_public_research_payload_does_not_pass_through_nested_discovery_fields(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0]["discovery"] = {
        "account_snapshot": {"cash": 88_000.0},
        "cash": 88_000.0,
        "diagnostic": "must_not_escape",
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        _make_public_deep_check(discovery),
        context=make_hydrated_opportunity_market_context(),
    )

    evidence = payload["data"]["candidates"][0]["discovery"]
    assert "account_snapshot" not in evidence
    assert "cash" not in evidence
    assert "diagnostic" not in evidence
    _assert_public_research_safety(payload)


def test_public_research_payload_exposes_only_public_schema_fields(monkeypatch):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["candidate_discovery"]["account_snapshot"] = {
        "cash": 88_000.0,
    }
    discovery["candidate_discovery"]["cash"] = 88_000.0
    deep_check = _make_public_deep_check(discovery)
    deep_check["candidates"][0]["private_account"] = {
        "user_id": "must_not_escape",
    }
    deep_check["candidates"][0]["quote"] = {
        "source": "tencent",
        "code": "600000",
        "trade_at": "2026-07-17T10:00:00+08:00",
        "trade_date": "2026-07-17",
        "price": 10.25,
            "amount": 321_000_000.0,
            "volume": 12_345_600,
            "quote_volume": 12_300_000,
            "circ_mv": discovery["quote_map"]["600000"]["circ_mv"],
            "total_mv": discovery["quote_map"]["600000"]["total_mv"],
            "private_payload": {"token": "must_not_escape"},
    }
    deep_check["candidates"][0]["guarded_price_plan"] = {
        "status": "ok",
        "actionable": False,
        "metrics": {"ma5": 10.1, "private_metric": 88_000.0},
        "trend_context": {
            "state": "recovery_required",
            "recovery_required": True,
            "bearish_short_term_alignment": True,
            "drawdown_from_20d_high_pct": -25.0,
            "distance_to_entry_pct": 4.5,
            "below_key_averages": ["ma5", "ma10", "private_average"],
            "private_trend": {"cash": 88_000.0},
        },
        "private_plan": {"cash": 88_000.0},
    }
    deep_check["candidates"][0]["corporate_action"] = {
        "status": "no_upcoming_corporate_action",
        "private_action": {"user_id": "must_not_escape"},
    }
    deep_check["candidates"][0]["risk_flags"] = [
        {
            "key": "quote_not_actionable",
            "level": "warning",
            "message": "test",
            "private_flag": {"cash": 88_000.0},
        }
    ]
    deep_check["candidates"][0]["triggers"] = {
        "source": "public_full_market",
        "status": {
            "position": "unknown",
            "private_status": {"cash": 88_000.0},
        },
        "private_trigger": {"cash": 88_000.0},
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate = payload["data"]["candidates"][0]
    assert "account_snapshot" not in payload["data"]["candidate_discovery"]
    assert "cash" not in payload["data"]["candidate_discovery"]
    assert "private_account" not in candidate
    assert "private_payload" not in candidate["quote"]
    assert "private_plan" not in candidate["guarded_price_plan"]
    assert "private_metric" not in candidate["guarded_price_plan"]["metrics"]
    assert candidate["guarded_price_plan"]["trend_context"] == {
        "state": "recovery_required",
        "recovery_required": True,
        "bearish_short_term_alignment": True,
        "drawdown_from_20d_high_pct": -25.0,
        "distance_to_entry_pct": 4.5,
        "below_key_averages": ["ma5", "ma10"],
    }
    assert "private_action" not in candidate["corporate_action"]
    assert "private_flag" not in candidate["risk_flags"][0]
    assert "private_trigger" not in candidate["triggers"]
    assert "private_status" not in candidate["triggers"]["status"]
    assert candidate["quote"]["volume"] == 12_345_600
    assert candidate["quote"]["quote_volume"] == 12_300_000


def test_public_research_timeout_exposes_only_verified_quote_schema_fields(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["quote_map"]["600000"]["private_payload"] = {
        "token": "must_not_escape",
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        context=make_hydrated_opportunity_market_context(),
    )

    quote = payload["data"]["candidates"][0]["quote"]
    assert "private_payload" not in quote
    assert quote["volume"] == 12_345_600
    assert quote["quote_volume"] == 12_300_000


def test_public_research_timeout_sanitizes_definition_fields(monkeypatch):
    discovery = _make_public_research_discovery(candidate_count=1)
    definition = discovery["definitions"][0]
    definition["theme"] = {"private_nested": {"cash": 88_000.0}}
    definition["theme_label"] = {"private_nested": {"cash": 88_000.0}}
    definition["observation_zone"] = {
        "low": 9.8,
        "high": 10.1,
        "private_nested": {"cash": 88_000.0},
    }
    definition["note"] = {"private_nested": {"cash": 88_000.0}}
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        context=make_hydrated_opportunity_market_context(),
    )

    candidate = payload["data"]["candidates"][0]
    assert candidate["theme"] is None
    assert candidate["theme_label"] is None
    assert candidate["triggers"]["observation_zone"] == {
        "low": 9.8,
        "high": 10.1,
    }
    assert candidate["triggers"]["note"] is None


@pytest.mark.parametrize("deep_status", ["ok", "technical_deep_check_timeout"])
def test_public_research_payload_binds_evidence_to_benchmark_trade_date(
    monkeypatch,
    deep_status,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["candidate_discovery"]["benchmark_trade_date"] = "2026-07-18"
    deep_check = (
        _make_public_deep_check(discovery)
        if deep_status == "ok"
        else {"status": deep_status, "candidates": []}
    )

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        deep_check,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "unparseable",
        "quote_mismatch",
        "trade_date_mismatch",
        "non_string",
        "date_only",
        "missing_seconds",
        "missing_timezone",
    ],
)
def test_public_research_payload_rejects_invalid_or_unbound_tencent_trade_at(
    monkeypatch,
    invalid_kind,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    definition = discovery["definitions"][0]
    quote = discovery["quote_map"]["600000"]
    if invalid_kind == "unparseable":
        definition["tencent_trade_at"] = "not-a-time"
    elif invalid_kind == "quote_mismatch":
        quote["trade_at"] = "2026-07-17T10:01:00+08:00"
    elif invalid_kind == "trade_date_mismatch":
        definition["tencent_trade_at"] = "2026-07-18T10:00:00+08:00"
        quote["trade_at"] = definition["tencent_trade_at"]
    elif invalid_kind == "non_string":
        definition["tencent_trade_at"] = datetime(2026, 7, 17, 10, 0)
        quote["trade_at"] = definition["tencent_trade_at"]
    elif invalid_kind == "date_only":
        definition["tencent_trade_at"] = "2026-07-17"
        quote["trade_at"] = definition["tencent_trade_at"]
    elif invalid_kind == "missing_seconds":
        definition["tencent_trade_at"] = "2026-07-17T10:00+08:00"
        quote["trade_at"] = definition["tencent_trade_at"]
    else:
        definition["tencent_trade_at"] = "2026-07-17T10:00:00"
        quote["trade_at"] = definition["tencent_trade_at"]

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize("field", ["volume_ratio", "limit_up"])
@pytest.mark.parametrize("deep_status", ["ok", "technical_deep_check_timeout"])
def test_public_research_payload_requires_materialized_optional_evidence_keys(
    monkeypatch,
    field,
    deep_status,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0].pop(field)
    deep_check = (
        _make_public_deep_check(discovery)
        if deep_status == "ok"
        else {"status": deep_status, "candidates": []}
    )

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        deep_check,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "missing_provider_expected_count",
        "missing_exchange_count",
        "missing_stage_provider",
        "boolean_count",
        "non_finite_coverage",
        "boolean_exchange_count",
        "inconsistent_total_coverage",
        "inconsistent_exchange_coverage",
        "requested_count_mismatch",
        "verified_below_minimum",
        "consistent_low_coverage",
        "minimum_formula_mismatch",
    ],
)
def test_public_research_payload_rejects_incomplete_discovery_metadata(
    monkeypatch,
    invalid_kind,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    metadata = discovery["candidate_discovery"]
    if invalid_kind == "missing_provider_expected_count":
        metadata.pop("provider_expected_count")
    elif invalid_kind == "missing_exchange_count":
        metadata["exchange_counts"].pop("sh")
    elif invalid_kind == "missing_stage_provider":
        metadata["stage_sources"]["public_snapshot"].pop("provider")
    elif invalid_kind == "boolean_count":
        metadata["provider_expected_count"] = True
    elif invalid_kind == "non_finite_coverage":
        metadata["total_coverage_ratio"] = float("nan")
    elif invalid_kind == "boolean_exchange_count":
        metadata["exchange_counts"]["sh"] = True
    elif invalid_kind == "inconsistent_total_coverage":
        metadata["total_coverage_ratio"] = 0.5
    elif invalid_kind == "inconsistent_exchange_coverage":
        metadata["exchange_coverage_ratio"]["sh"] = 0.5
    elif invalid_kind == "requested_count_mismatch":
        metadata["tencent_requested_count"] += 1
    elif invalid_kind == "verified_below_minimum":
        metadata["tencent_minimum_verified_count"] = 2
    elif invalid_kind == "consistent_low_coverage":
        metadata["provider_expected_exchange_counts"] = {
            exchange: count * 2
            for exchange, count in metadata["exchange_counts"].items()
        }
        metadata["provider_expected_count"] = sum(
            metadata["provider_expected_exchange_counts"].values()
        )
        metadata["total_coverage_ratio"] = 0.5
        metadata["exchange_coverage_ratio"] = {
            "sh": 0.5,
            "sz": 0.5,
            "bj": 0.5,
        }
    else:
        metadata["tencent_minimum_verified_count"] = 0

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        _make_public_deep_check(discovery),
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_rejects_incomplete_ok_deep_check_candidate(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {
            "status": "ok",
            "candidates": [{"code": "600000", "name": "公开候选0"}],
        },
        code="technical_deep_check_failed",
        stage="technical_deep_check",
    )


@pytest.mark.parametrize(
    "mismatch_kind",
    [
        "quote_code",
        "quote_trade_date",
        "quote_trade_at",
        "quote_price",
        "quote_amount",
        "quote_volume",
    ],
)
def test_public_research_payload_binds_deep_quote_to_candidate_definition(
    monkeypatch,
    mismatch_kind,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    deep_check = _make_public_deep_check(discovery)
    quote = deep_check["candidates"][0]["quote"]
    if mismatch_kind == "quote_code":
        quote["code"] = "000001"
    elif mismatch_kind == "quote_trade_date":
        quote["trade_date"] = "2026-07-16"
    elif mismatch_kind == "quote_trade_at":
        quote["trade_at"] = "2026-07-17T10:01:00+08:00"
    elif mismatch_kind == "quote_price":
        quote["price"] += 1.0
    elif mismatch_kind == "quote_amount":
        quote["amount"] += 1_000_000.0
    else:
        quote["volume"] += 1_000_000

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        deep_check,
        code="technical_deep_check_failed",
        stage="technical_deep_check",
    )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("trade_date", "2026/07/17"),
        ("bucket", "unknown"),
        ("public_score", None),
        ("amount_percentile", 1.01),
        ("move_quality", float("nan")),
        ("tencent_source", "unknown_provider"),
        ("tencent_bucket", "unknown"),
        ("tencent_score", None),
        ("tencent_amount_percentile", -0.01),
        ("tencent_market_cap_percentile", 1.01),
        ("tencent_move_quality", float("nan")),
        ("turnover_quality", 1.01),
        ("volume_ratio_quality", -0.01),
        ("amplitude_quality", 1.01),
    ],
)
@pytest.mark.parametrize("deep_status", ["ok", "technical_deep_check_timeout"])
def test_public_research_payload_rejects_invalid_required_discovery_evidence(
    monkeypatch,
    field,
    invalid_value,
    deep_status,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0][field] = invalid_value
    deep_check = (
        _make_public_deep_check(discovery)
        if deep_status == "ok"
        else {"status": deep_status, "candidates": []}
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid discovery must fail before payload assembly")
        ),
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module.build_public_research_opportunities_payload(
            discovery,
            deep_check,
            context=make_hydrated_opportunity_market_context(),
        )

    assert caught.value.code == "candidate_discovery_unavailable"
    assert caught.value.exit_code == 4
    assert caught.value.stage == "candidate_discovery"


@pytest.mark.parametrize(
    "field",
    [
        "one_lot_amount",
        "tencent_price",
        "tencent_pct_change",
        "tencent_amount",
        "turnover_rate",
        "volume_ratio",
        "amplitude",
        "circ_mv",
        "total_mv",
        "limit_up",
    ],
)
def test_public_research_payload_binds_flat_evidence_to_verified_quote(
    monkeypatch,
    field,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["definitions"][0][field] += 1.0

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize("deep_status", ["ok", "technical_deep_check_timeout"])
def test_public_research_payload_allows_explicitly_optional_discovery_evidence(
    monkeypatch,
    deep_status,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    definition = discovery["definitions"][0]
    definition["volume_ratio"] = None
    definition["volume_ratio_quality"] = 0.0
    definition["limit_up"] = None
    discovery["quote_map"]["600000"]["volume_ratio"] = None
    discovery["quote_map"]["600000"]["limit_up"] = None
    deep_check = (
        _make_public_deep_check(discovery)
        if deep_status == "ok"
        else {"status": deep_status, "candidates": []}
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    evidence = payload["data"]["candidates"][0]["discovery"]["tencent"]
    assert evidence["volume_ratio"] is None
    assert evidence["volume_ratio_quality"] == 0.0
    assert evidence["limit_up"] is None
    _assert_public_research_safety(payload)


def test_public_research_payload_rejects_ok_discovery_without_definitions(
    monkeypatch,
):
    discovery = _make_public_research_discovery(status="ok", candidate_count=0)

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        None,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_rejects_no_eligible_discovery_with_definitions(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["status"] = "no_eligible_candidates"
    discovery["candidate_discovery"]["status"] = "no_eligible_candidates"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        None,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_rejects_preselection_empty_discovery_with_quote_rows(
    monkeypatch,
):
    discovery = _make_public_research_discovery(
        status="no_eligible_candidates",
        candidate_count=0,
    )
    discovery["quote_map"]["600000"] = {
        "code": "600000",
        "source": "tencent",
    }

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        None,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_accepts_post_tencent_filtered_empty_discovery(
    monkeypatch,
):
    discovery = _make_post_tencent_filtered_empty_discovery()
    original_discovery = deepcopy(discovery)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        None,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert payload["data"]["candidates"] == []
    assert candidate_discovery["stage_sources"]["technical_deep_check"] == {
        "provider": "tencent_daily_bars",
        "status": "not_called_no_candidates",
    }
    assert payload["meta"]["source"] == (
        "akshare.sina.stock_zh_a_spot+tencent_batch_quotes"
    )
    assert payload["data"]["context"]["available_data"] == [
        "public_full_market_snapshot",
        "tencent_verified_quotes",
    ]
    assert discovery == original_discovery


@pytest.mark.parametrize(
    "invalid_kind",
    ["unsupported_code", "non_mapping_quote", "quote_code_mismatch"],
)
def test_public_research_payload_rejects_invalid_post_tencent_empty_quote_map(
    monkeypatch,
    invalid_kind,
):
    discovery = _make_post_tencent_filtered_empty_discovery()
    if invalid_kind == "unsupported_code":
        quote = discovery["quote_map"].pop("600000")
        quote["code"] = "100000"
        discovery["quote_map"]["100000"] = quote
    elif invalid_kind == "non_mapping_quote":
        discovery["quote_map"]["600000"] = "invalid quote"
    else:
        discovery["quote_map"]["600000"]["code"] = "600001"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        None,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_rejects_no_candidate_discovery_with_other_tencent_status(
    monkeypatch,
):
    discovery = _make_public_research_discovery(
        status="no_eligible_candidates",
        candidate_count=0,
    )
    discovery["candidate_discovery"]["stage_sources"]["tencent_verification"][
        "status"
    ] = "coverage_incomplete"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        None,
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_requires_ok_tencent_stage_for_selected_candidates(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    discovery["candidate_discovery"]["stage_sources"]["tencent_verification"][
        "status"
    ] = "not_called_no_preselection"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        _make_public_deep_check(discovery),
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize("invalid_kind", ["duplicate_code", "invalid_code"])
def test_public_research_payload_rejects_invalid_discovery_definition_codes(
    monkeypatch,
    invalid_kind,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    if invalid_kind == "duplicate_code":
        discovery["definitions"][1]["code"] = "600000"
    else:
        quote = discovery["quote_map"].pop("600001")
        quote["code"] = "100000"
        discovery["quote_map"]["100000"] = quote
        discovery["definitions"][1]["code"] = "100000"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


def test_public_research_payload_screens_more_than_eight_and_keeps_top_eight(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=9)
    for definition in discovery["definitions"]:
        definition.update(
            {
                "pct_change": 1.0,
                "bucket": "strength",
                "amount_percentile": 0.5,
                "move_quality": 0.5,
                "public_score": 0.5,
                "tencent_pct_change": 1.0,
                "tencent_bucket": "strength",
                "turnover_rate": 2.0,
                "volume_ratio": 1.0,
                "amplitude": 3.0,
                "tencent_move_quality": 0.5,
                "turnover_quality": 0.5,
                "volume_ratio_quality": 0.5,
                "amplitude_quality": 0.5,
                "tencent_amount_percentile": 0.5,
                "tencent_market_cap_percentile": 0.5,
                "tencent_score": 0.5,
            }
        )
        quote = discovery["quote_map"][definition["code"]]
        quote.update(
            {
                "pct_chg": 1.0,
                "turnover_rate": 2.0,
                "volume_ratio": 1.0,
                "amplitude": 3.0,
            }
        )
    selected_codes = [item["code"] for item in discovery["definitions"][:8]]
    deep_check = _make_public_deep_check(discovery, codes=selected_codes)
    deep_check["technical_screen"] = {
        "status": "ok",
        "screened_count": 9,
        "passed_count": 9,
        "selected_count": 8,
        "selected_codes": selected_codes,
        "status_counts": {"ok": 9},
        "closest_rejection_count": 0,
        "closest_rejections": [],
    }
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert [item["code"] for item in payload["data"]["candidates"]] == selected_codes
    assert candidate_discovery["technical_screened_count"] == 9
    assert candidate_discovery["technical_passed_count"] == 9
    assert candidate_discovery["technical_selected_count"] == 8
    assert candidate_discovery["technical_checked_count"] == 8
    assert candidate_discovery["technical_screen_status_counts"] == {"ok": 9}
    assert candidate_discovery["stage_sources"]["technical_screen"] == {
        "provider": "tencent_daily_bars",
        "status": "ok",
    }
    assert candidate_discovery["stage_sources"]["technical_deep_check"] == {
        "provider": "cninfo_dividend_calendar",
        "status": "ok",
    }
    _assert_public_research_safety(payload)


def test_public_research_payload_exposes_closest_technical_rejections_safely(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=7)
    for definition in discovery["definitions"]:
        definition.update(
            {
                "pct_change": 1.0,
                "bucket": "strength",
                "move_quality": 0.5,
                "public_score": 0.5,
                "tencent_pct_change": 1.0,
                "tencent_bucket": "strength",
                "turnover_rate": 2.0,
                "volume_ratio": 1.0,
                "amplitude": 3.0,
                "tencent_move_quality": 0.5,
                "turnover_quality": 0.5,
                "volume_ratio_quality": 0.5,
                "amplitude_quality": 0.5,
            }
        )
        discovery["quote_map"][definition["code"]].update(
            {
                "pct_chg": 1.0,
                "turnover_rate": 2.0,
                "volume_ratio": 1.0,
                "amplitude": 3.0,
            }
        )
    technical_codes = [
        item["code"] for item in discovery["definitions"][:2]
    ]
    deep_check = _make_public_deep_check(
        discovery,
        codes=technical_codes,
        technical_codes=technical_codes,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    closest = candidate_discovery["technical_closest_rejections"]
    assert candidate_discovery["technical_closest_rejection_count"] == 5
    assert [item["code"] for item in closest] == [
        item["code"] for item in discovery["definitions"][2:7]
    ]
    assert [item["net_reward_risk"] for item in closest] == [
        1.49,
        1.48,
        1.47,
        1.46,
        1.45,
    ]
    assert all(
        item["earnings_review_status"] == "not_reviewed"
        and item["actionable"] is False
        and item["is_reference_only"] is True
        for item in closest
    )
    assert "technical_closest_rejections" in payload["data"]["context"][
        "available_data"
    ]
    assert not set(item["code"] for item in closest).intersection(
        item["code"] for item in payload["data"]["candidates"]
    )
    _assert_public_research_safety(payload)


def test_public_research_payload_filters_loss_forecasts_with_audit_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=3)
    technical_codes = [item["code"] for item in discovery["definitions"]]
    blocked_codes = technical_codes[:2]
    selected_codes = technical_codes[2:]
    deep_check = _make_public_deep_check(
        discovery,
        codes=selected_codes,
        technical_codes=technical_codes,
        blocked_codes=blocked_codes,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert [item["code"] for item in payload["data"]["candidates"]] == selected_codes
    assert candidate_discovery["technical_selected_count"] == 3
    assert candidate_discovery["earnings_screened_count"] == 3
    assert candidate_discovery["earnings_blocked_count"] == 2
    assert candidate_discovery["earnings_selected_count"] == 1
    assert candidate_discovery["technical_checked_count"] == 1
    assert candidate_discovery["earnings_report_period"] == "20260630"
    assert candidate_discovery["earnings_actual_report_period"] == "20260331"
    assert candidate_discovery["earnings_screen_status_counts"] == {
        "loss_forecast": 2,
        "no_forecast": 1,
    }
    assert candidate_discovery["earnings_actual_status_counts"] == {
        "positive_profit": 3,
    }
    assert [
        item["code"]
        for item in candidate_discovery["earnings_screen_results"]
        if item["blocks_new_position"]
    ] == blocked_codes
    assert candidate_discovery["stage_sources"][
        "earnings_forecast_review"
    ] == {
        "provider": holdings_cli_module.EARNINGS_REVIEW_SOURCE,
        "status": "ok",
    }
    assert "earnings_forecast_review" in payload["data"]["context"][
        "available_data"
    ]
    assert "latest_actual_earnings" in payload["data"]["context"][
        "available_data"
    ]
    assert holdings_cli_module.EARNINGS_FORECAST_SOURCE in payload["meta"]["source"]
    assert holdings_cli_module.EARNINGS_ACTUAL_SOURCE in payload["meta"]["source"]
    _assert_public_research_safety(payload)


def test_public_research_payload_filters_latest_actual_loss_with_audit_evidence(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    technical_codes = [item["code"] for item in discovery["definitions"]]
    blocked_code = technical_codes[0]
    selected_code = technical_codes[1]
    deep_check = _make_public_deep_check(
        discovery,
        codes=[selected_code],
        technical_codes=technical_codes,
        actual_loss_codes=(blocked_code,),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert [item["code"] for item in payload["data"]["candidates"]] == [
        selected_code
    ]
    assert candidate_discovery["earnings_blocked_count"] == 1
    assert candidate_discovery["earnings_actual_status_counts"] == {
        "actual_loss": 1,
        "positive_profit": 1,
    }
    by_code = {
        item["code"]: item
        for item in candidate_discovery["earnings_screen_results"]
    }
    assert by_code[blocked_code]["status"] == "no_forecast"
    assert by_code[blocked_code]["latest_actual"]["status"] == "actual_loss"
    assert by_code[blocked_code]["blocks_new_position"] is True
    _assert_public_research_safety(payload)


def test_public_research_payload_returns_empty_when_all_technical_survivors_forecast_loss(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    technical_codes = [item["code"] for item in discovery["definitions"]]
    deep_check = _make_public_deep_check(
        discovery,
        codes=[],
        technical_codes=technical_codes,
        blocked_codes=technical_codes,
    )
    deep_check["candidates"] = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert payload["data"]["candidates"] == []
    assert candidate_discovery["earnings_blocked_count"] == 2
    assert candidate_discovery["earnings_selected_count"] == 0
    assert candidate_discovery["technical_checked_count"] == 0
    assert candidate_discovery["stage_sources"]["technical_deep_check"] == {
        "provider": "cninfo_dividend_calendar",
        "status": "not_called_no_earnings_survivors",
    }
    assert "cninfo_dividend_calendar" not in payload["meta"]["source"]
    _assert_public_research_safety(payload)


@pytest.mark.parametrize(
    "error_type",
    ["EarningsForecastFetchError", "EarningsActualFetchError"],
)
def test_public_research_payload_reports_earnings_provider_failure_stage(
    monkeypatch,
    error_type,
):
    discovery = _make_public_research_discovery(candidate_count=1)

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {
            "status": "technical_deep_check_failed",
            "candidates": [],
            "error_type": error_type,
        },
        code=error_type,
        stage="earnings_forecast_review",
    )


@pytest.mark.parametrize(
    "invalid_kind",
    ["missing_quote", "non_mapping_quote", "quote_code_mismatch"],
)
def test_public_research_payload_rejects_unusable_discovery_quotes(
    monkeypatch,
    invalid_kind,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    if invalid_kind == "missing_quote":
        discovery["quote_map"].pop("600000")
    elif invalid_kind == "non_mapping_quote":
        discovery["quote_map"]["600000"] = "invalid quote"
    else:
        discovery["quote_map"]["600000"]["code"] = "600001"

    _assert_public_payload_error(
        monkeypatch,
        discovery,
        {"status": "technical_deep_check_timeout", "candidates": []},
        code="candidate_discovery_unavailable",
        stage="candidate_discovery",
    )


@pytest.mark.parametrize(
    "invalid_context_kind",
    ["none", "wrong_type", "not_loaded", "snapshot_not_mapping"],
)
def test_public_research_payload_requires_prepared_market_context(
    monkeypatch,
    invalid_context_kind,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    deep_check = _make_public_deep_check(discovery)
    if invalid_context_kind == "none":
        context = None
    elif invalid_context_kind == "wrong_type":
        context = {"public_snapshot_loaded": True, "public_snapshot": {}}
    else:
        context = make_opportunity_market_context()
        if invalid_context_kind == "snapshot_not_mapping":
            context.public_snapshot_loaded = True
            context.public_snapshot = []

    def unexpected_market_builder(*_args, **_kwargs):
        raise AssertionError("invalid context must fail before market payload assembly")

    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        unexpected_market_builder,
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module.build_public_research_opportunities_payload(
            discovery,
            deep_check,
            context=context,
        )

    assert caught.value.code == "candidate_discovery_unavailable"
    assert caught.value.exit_code == 4
    assert caught.value.stage == "market_context"


def test_public_research_payload_real_market_builder_reuses_hydrated_context_without_io(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    deep_check = _make_public_deep_check(discovery)

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("prepared public market context must prevent hidden I/O")

    context = make_hydrated_opportunity_market_context(
        public_snapshot_fetcher=unexpected_io,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        unexpected_io,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        unexpected_io,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        unexpected_io,
    )
    monkeypatch.setattr(
        opportunity_context_module,
        "fetch_tencent_quotes_sync",
        unexpected_io,
    )
    monkeypatch.setattr(
        opportunity_context_module,
        "fetch_sina_public_market_snapshot",
        unexpected_io,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=context,
    )

    assert payload["data"]["market_status"]["data_completeness"] == (
        "indices_and_public_breadth"
    )
    assert payload["data"]["market_status"]["market_gate"]["breadth_regime"][
        "status"
    ] == "ok"


def test_public_research_payload_normalizes_real_public_candidate_trigger_source(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=1)
    original_discovery = deepcopy(discovery)
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_daily_bars_sync",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "history_unavailable",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_cn_dividend_calendar_sync",
        lambda *_args, **_kwargs: {
            "price_plan_adjustment_required": False,
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    real_candidates = holdings_cli_module._build_opportunity_candidates(
        discovery["definitions"],
        cash=None,
        buy_lot_size=100,
        holding_themes=set(),
        allow_reference_price_plan=True,
        quote_snapshots=discovery["quote_map"],
    )
    deep_check = {"status": "ok", "candidates": real_candidates}
    original_deep_check = deepcopy(deep_check)

    assert real_candidates[0]["triggers"]["source"] == (
        "configured_historical_reference"
    )
    assert real_candidates[0]["quote"]["volume"] == 12_345_600
    assert real_candidates[0]["quote"]["quote_volume"] == 12_300_000
    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate = payload["data"]["candidates"][0]
    assert payload["data"]["context"]["source"] == "public_full_market"
    assert candidate["triggers"]["source"] == "public_full_market"
    assert candidate["quote"]["volume"] == 12_345_600
    assert candidate["quote"]["quote_volume"] == 12_300_000
    assert candidate["same_theme_with_holdings"] is None
    _assert_public_research_safety(payload)
    assert discovery == original_discovery
    assert deep_check == original_deep_check


def test_public_research_payload_removes_fee_aware_order_estimates(monkeypatch):
    discovery = _make_public_research_discovery(candidate_count=1)
    actionable_plan = holdings_cli_module.apply_net_reward_risk_gate(
        {
            "status": "ok",
            "actionable": True,
            "suggested_buy_price": 10.0,
            "stop_loss_price": 9.0,
            "target_price": 12.0,
        },
        quantity=100,
    )
    deep_check = {
        "status": "ok",
        "candidates": [
            {
                "code": "600000",
                "name": "公开候选0",
                "quote": {
                    "source": "tencent",
                    "code": "600000",
                    "trade_at": "2026-07-17T10:00:00+08:00",
                    "trade_date": "2026-07-17",
                    "price": 10.25,
                        "amount": 321_000_000.0,
                        "volume": 12_345_600,
                        "quote_volume": 12_300_000,
                        "circ_mv": discovery["quote_map"]["600000"]["circ_mv"],
                        "total_mv": discovery["quote_map"]["600000"]["total_mv"],
                },
                "guarded_price_plan": actionable_plan,
                "same_theme_with_holdings": False,
            }
        ],
    }
    original_discovery = deepcopy(discovery)
    original_deep_check = deepcopy(deep_check)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    assert all(
        actionable_plan["fee_aware_trade"][order]["quantity"] == 100
        for order in ("entry_order", "stop_order", "target_order")
    )
    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        context=make_hydrated_opportunity_market_context(),
    )

    candidate = payload["data"]["candidates"][0]
    fee_aware_trade = candidate["guarded_price_plan"]["fee_aware_trade"]
    for metric in ("net_reward_risk", "reward_amount", "risk_amount"):
        assert fee_aware_trade[metric] == actionable_plan["fee_aware_trade"][metric]
    assert not {
        "entry_order",
        "stop_order",
        "target_order",
    }.intersection(fee_aware_trade)
    assert candidate["quote"]["amount"] == 321_000_000.0
    assert candidate["quote"]["volume"] == 12_345_600
    assert candidate["quote"]["quote_volume"] == 12_300_000
    assert candidate["same_theme_with_holdings"] is None
    assert discovery == original_discovery
    assert deep_check == original_deep_check


def test_public_research_payload_preserves_complete_discovery_and_sanitizes_all_nesting(
    monkeypatch,
):
    discovery = _make_public_research_discovery()
    deep_check = {
        "status": "ok",
        "candidates": [
            {
                "code": "600000",
                "name": "公开候选0",
                "quote": {
                    "source": "tencent",
                    "code": "600000",
                        "trade_at": "2026-07-17T10:00:00+08:00",
                        "trade_date": "2026-07-17",
                        "price": 10.25,
                        "amount": 321_000_000.0,
                        "volume": 12_345_600,
                        "quote_volume": 12_300_000,
                        "circ_mv": discovery["quote_map"]["600000"]["circ_mv"],
                        "total_mv": discovery["quote_map"]["600000"]["total_mv"],
                        "actionable": True,
                },
                "guarded_price_plan": {
                    "status": "ok",
                    "actionable": True,
                    "reference_actionable": True,
                    "suggested_buy_price": 10.2,
                    "target_price": 11.0,
                    "stop_loss_price": 9.8,
                    "history": {"historical_volume": [1_000_000, 2_000_000]},
                },
                "external_risk": {
                    "new_position_allowed": True,
                    "external_new_exposure_amount": 50_000.0,
                },
                "affordable_with_cash": True,
                "cash_after_one_lot": 50_000.0,
                "cash_usage_pct": 20.0,
                "same_theme_with_holdings": False,
                "is_reference_only": False,
            }
        ],
    }
    database_status = {
        "status": "unavailable",
        "error_code": "database_error",
        "diagnostic": {"actionable": True, "amount": 123_456.0},
    }
    context = make_hydrated_opportunity_market_context()
    original_discovery = deepcopy(discovery)
    original_deep_check = deepcopy(deep_check)
    original_database_status = deepcopy(database_status)
    original_context = deepcopy(context)
    seen_contexts = []

    def fake_market_status(db=None, *, database_status=None, context=None):
        seen_contexts.append(context)
        return _fake_public_market_status(
            db,
            database_status=database_status,
            context=context,
        )

    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        fake_market_status,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_opportunity_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public success payload must copy deep-check candidates")
        ),
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        external_risk_level="green",
        database_status=database_status,
        context=context,
    )

    expected_discovery = deepcopy(discovery["candidate_discovery"])
    expected_discovery["technical_checked_count"] = 1
    expected_discovery["stage_sources"]["technical_deep_check"] = {
        "provider": "tencent_daily_bars",
        "status": "ok",
    }
    expected_discovery["stage_sources"]["earnings_forecast_review"] = {
        "provider": holdings_cli_module.EARNINGS_REVIEW_SOURCE,
        "status": "not_called_legacy_deep_check",
    }
    assert payload["ok"] is True
    assert payload["data"]["mode"] == "research_only"
    assert payload["data"]["candidate_discovery"] == expected_discovery
    assert "definitions" not in payload["data"]
    assert "quote_map" not in payload["data"]
    assert payload["data"]["account"] == {
        "status": "unavailable",
        "actionable": False,
        "reason_code": "public_research_mode",
        "configured_total_assets": None,
        "cash_or_unallocated": None,
        "estimated_equity": None,
    }
    assert payload["data"]["decision"]["action"] == "observe"
    assert payload["data"]["decision"]["suggested_lots"] == 0
    assert payload["data"]["decision"]["suggested_quantity"] == 0
    assert payload["data"]["disclaimer"] == "仅供研究参考，不构成投资建议或交易指令。"
    assert payload["meta"]["schema_version"] == 7
    assert payload["meta"]["source"] == (
        "akshare.sina.stock_zh_a_spot+tencent_batch_quotes+"
        "tencent_daily_bars+cninfo_dividend_calendar"
    )
    candidate = payload["data"]["candidates"][0]
    assert candidate["affordable_with_cash"] is None
    assert candidate["cash_after_one_lot"] is None
    assert candidate["cash_usage_pct"] is None
    assert candidate["same_theme_with_holdings"] is None
    assert candidate["is_reference_only"] is True
    assert candidate["decision"]["action"] == "observe"
    assert candidate["quote"]["price"] == 10.25
    assert candidate["quote"]["amount"] == 321_000_000.0
    assert candidate["quote"]["volume"] == 12_345_600
    assert candidate["quote"]["quote_volume"] == 12_300_000
    assert candidate["guarded_price_plan"]["suggested_buy_price"] == 10.2
    assert candidate["guarded_price_plan"]["history"]["historical_volume"] == [
        1_000_000,
        2_000_000,
    ]
    assert payload["data"]["market_status"]["market_gate"]["index_price"] == 3_412.34
    assert payload["data"]["market_status"]["market_gate"]["quote_volume"] == 987_654_321
    assert payload["data"]["market_status"]["market_gate"]["data_complete"] is True
    assert payload["data"]["market_status"]["market_session"]["session"] == (
        "morning"
    )
    assert payload["data"]["market_status"]["market_session"][
        "quote_stale_risk"
    ] is False
    _assert_public_research_safety(payload)
    assert seen_contexts == [context]
    assert discovery == original_discovery
    assert deep_check == original_deep_check
    assert database_status == original_database_status
    assert context == original_context


def test_public_research_payload_preserves_no_candidate_coverage_without_source_inflation(
    monkeypatch,
):
    discovery = _make_public_research_discovery(
        "no_eligible_candidates",
        candidate_count=0,
    )
    original = deepcopy(discovery)
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        None,
        database_status={"status": "unavailable"},
        context=make_hydrated_opportunity_market_context(),
    )

    expected_discovery = deepcopy(discovery["candidate_discovery"])
    expected_discovery["technical_checked_count"] = 0
    expected_discovery["stage_sources"]["technical_deep_check"] = {
        "provider": "tencent_daily_bars",
        "status": "not_called_no_candidates",
    }
    expected_discovery["stage_sources"]["earnings_forecast_review"] = {
        "provider": holdings_cli_module.EARNINGS_REVIEW_SOURCE,
        "status": "not_called_no_candidates",
    }
    assert payload["data"]["candidate_discovery"] == expected_discovery
    assert set(payload["data"]["candidate_discovery"]) == set(
        _make_public_research_discovery()["candidate_discovery"]
    )
    assert payload["data"]["candidates"] == []
    assert payload["data"]["candidate_discovery"]["source"] == (
        "akshare.sina.stock_zh_a_spot"
    )
    assert payload["data"]["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "not_called_no_preselection"
    assert payload["data"]["context"]["available_data"] == [
        "public_full_market_snapshot"
    ]
    assert payload["meta"]["source"] == "akshare.sina.stock_zh_a_spot"
    assert "tencent" not in payload["meta"]["source"]
    assert discovery == original
    _assert_public_research_safety(payload)


def test_public_research_timeout_uses_verified_quotes_without_hidden_provider_calls(
    monkeypatch,
):
    discovery = _make_public_research_discovery(candidate_count=2)
    discovery["quote_map"]["000001"] = {
        "code": "000001",
        "name": "额外行情",
        "source": "tencent",
        "close": 9.99,
    }
    deep_check = {
        "status": "technical_deep_check_timeout",
        "candidates": [],
    }
    original_discovery = deepcopy(discovery)
    original_deep_check = deepcopy(deep_check)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("timeout fallback must not call a provider or deep builder")

    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        _fake_public_market_status,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_opportunity_candidates",
        unexpected_call,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        unexpected_call,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_daily_bars_sync",
        unexpected_call,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_cn_dividend_calendar_sync",
        unexpected_call,
    )

    payload = holdings_cli_module.build_public_research_opportunities_payload(
        discovery,
        deep_check,
        database_status={"status": "unavailable"},
        context=make_hydrated_opportunity_market_context(),
    )

    candidate_discovery = payload["data"]["candidate_discovery"]
    assert candidate_discovery["technical_checked_count"] == 0
    assert candidate_discovery["stage_sources"]["technical_deep_check"] == {
        "provider": "tencent_daily_bars",
        "status": "timeout",
    }
    assert payload["meta"]["source"] == (
        "akshare.sina.stock_zh_a_spot+tencent_batch_quotes"
    )
    assert payload["data"]["context"]["available_data"] == [
        "public_full_market_snapshot",
        "tencent_verified_quotes",
    ]
    assert [item["code"] for item in payload["data"]["candidates"]] == [
        "600000",
        "600001",
    ]
    for candidate in payload["data"]["candidates"]:
        source_quote = discovery["quote_map"][candidate["code"]]
        assert candidate["quote"]["source"] == "tencent"
        assert candidate["quote"]["amount"] == source_quote["amount"]
        assert candidate["quote"]["volume"] == source_quote["volume"]
        assert candidate["plan_status"] == "technical_deep_check_timeout"
        assert candidate["guarded_price_plan"]["status"] == (
            "technical_deep_check_timeout"
        )
        assert candidate["guarded_price_plan"]["actionable"] is False
        assert candidate["risk_status"]["status"] == "observation_only"
        assert candidate["risk_status"]["new_position_allowed"] is False
        assert candidate["affordable_with_cash"] is None
        assert candidate["cash_after_one_lot"] is None
        assert candidate["cash_usage_pct"] is None
        assert candidate["is_reference_only"] is True
        assert candidate["decision"]["action"] == "observe"
        assert candidate["decision"]["suggested_quantity"] == 0
    _assert_public_research_safety(payload)
    assert discovery == original_discovery
    assert deep_check == original_deep_check


def test_public_research_payload_rejects_incomplete_discovery_status(monkeypatch):
    discovery = _make_public_research_discovery()
    discovery["status"] = "candidate_discovery_unavailable"
    discovery["candidate_discovery"]["status"] = "candidate_discovery_unavailable"
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete discovery must fail before payload assembly")
        ),
    )

    with pytest.raises(CLIError) as caught:
        holdings_cli_module.build_public_research_opportunities_payload(
            discovery,
            None,
            context=make_hydrated_opportunity_market_context(),
        )

    assert caught.value.code == "candidate_discovery_unavailable"
    assert caught.value.exit_code == 4


def test_mongo_timeout_cap_is_narrow_and_keeps_default_connection_values(monkeypatch):
    values = {
        "MONGODB_HOST": "127.0.0.1",
        "MONGODB_DATABASE": "tradingagentscn",
        "MONGO_CONNECT_TIMEOUT_MS": "0",
        "MONGO_SOCKET_TIMEOUT_MS": "4000",
        "MONGO_SERVER_SELECTION_TIMEOUT_MS": "8000",
    }
    captured = []
    events = []

    class FakeClient:
        def __init__(self, client_id):
            self.client_id = client_id
            self.admin = self

        def command(self, name):
            events.append(("ping", self.client_id, name))
            return {"ok": 1}

        def __getitem__(self, name):
            events.append(("database", self.client_id, name))
            return type("FakeDatabase", (), {"name": name})()

        def close(self):
            events.append(("close", self.client_id))

    class FakeTimeout:
        def __init__(self, seconds):
            self.seconds = seconds

        def __enter__(self):
            events.append(("csot_enter", self.seconds))

        def __exit__(self, *_args):
            events.append(("csot_exit", self.seconds))

    def fake_client(**options):
        captured.append(options)
        return FakeClient(len(captured))

    monkeypatch.setattr(
        holdings_cli_module,
        "_mongo_connection_values",
        lambda _configuration: values,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_resolve_cli_mongo_host",
        lambda host, **_kwargs: host,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "MongoClient",
        fake_client,
    )
    monkeypatch.setattr(pymongo, "timeout", lambda seconds: FakeTimeout(seconds))
    configuration = {
        "source": "process_environment",
        "expected_database": "tradingagentscn",
    }

    holdings_cli_module._connect_cli_database(configuration)
    holdings_cli_module._connect_cli_database(configuration, timeout_cap_ms=5000)

    assert captured[0]["connectTimeoutMS"] == 0
    assert captured[0]["socketTimeoutMS"] == 4000
    assert captured[0]["serverSelectionTimeoutMS"] == 8000
    assert captured[1]["connectTimeoutMS"] == 5000
    assert captured[1]["socketTimeoutMS"] == 4000
    assert captured[1]["serverSelectionTimeoutMS"] == 5000
    assert events == [
        ("database", 1, "tradingagentscn"),
        ("csot_enter", 5.0),
        ("ping", 2, "ping"),
        ("csot_exit", 5.0),
        ("database", 2, "tradingagentscn"),
    ]


@pytest.mark.parametrize("timeout_cap_ms", [False, 0, -1, 1.5, "5000"])
def test_mongo_timeout_cap_rejects_non_positive_or_non_integer_values(
    monkeypatch,
    timeout_cap_ms,
):
    monkeypatch.setattr(
        holdings_cli_module,
        "_mongo_connection_values",
        lambda _configuration: {
            "MONGODB_HOST": "127.0.0.1",
            "MONGODB_DATABASE": "tradingagentscn",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_resolve_cli_mongo_host",
        lambda host, **_kwargs: host,
    )
    client_calls = []
    monkeypatch.setattr(
        holdings_cli_module,
        "MongoClient",
        lambda **options: client_calls.append(options),
    )

    with pytest.raises(CLIError) as exc_info:
        holdings_cli_module._connect_cli_database(
            {
                "source": "process_environment",
                "expected_database": "tradingagentscn",
            },
            timeout_cap_ms=timeout_cap_ms,
        )

    assert exc_info.value.code == "mongo_config_invalid"
    assert exc_info.value.exit_code == 4
    assert client_calls == []


def test_opportunity_mongo_ping_failure_closes_client_and_becomes_database_error(
    monkeypatch,
):
    events = []

    class FakeTimeout:
        def __enter__(self):
            events.append("csot_enter")

        def __exit__(self, *_args):
            events.append("csot_exit")

    class FakeClient:
        admin = None

        def __init__(self):
            self.admin = self

        def command(self, name):
            events.append(("ping", name))
            raise holdings_cli_module.PyMongoError("ping failed")

        def __getitem__(self, name):
            events.append(("database", name))
            return type("FakeDatabase", (), {"name": name})()

        def close(self):
            events.append("close")

    monkeypatch.setattr(
        holdings_cli_module,
        "_validate_cli_mongo_configuration",
        lambda: {
            "source": "process_environment",
            "expected_database": "tradingagentscn",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_mongo_connection_values",
        lambda _configuration: {
            "MONGODB_HOST": "127.0.0.1",
            "MONGODB_DATABASE": "tradingagentscn",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_resolve_cli_mongo_host",
        lambda host, **_kwargs: host,
    )
    monkeypatch.setattr(holdings_cli_module, "MongoClient", lambda **_options: FakeClient())
    monkeypatch.setattr(pymongo, "timeout", lambda _seconds: FakeTimeout())

    with pytest.raises(CLIError) as exc_info:
        holdings_cli_module._get_database(timeout_cap_ms=5000)

    assert exc_info.value.code == "database_error"
    assert exc_info.value.exit_code == 4
    assert events == ["csot_enter", ("ping", "ping"), "csot_exit", "close"]


def test_market_status_command_keeps_default_optional_mongo_timeout(monkeypatch):
    seen = []
    retry_flags = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            seen.append(timeout_cap_ms) or None,
            {"status": "unavailable", "error_code": "database_error"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        lambda db=None, *, database_status=None, retry_public_timeout=False: (
            retry_flags.append(retry_public_timeout)
            or {
                "ok": True,
                "data": {"database": database_status},
                "meta": {"schema_version": 1},
            }
        ),
    )

    result = CliRunner().invoke(holdings_app, ["market-status"])

    assert result.exit_code == 0
    assert seen == [None]
    assert retry_flags == [True]


def test_opportunities_command_falls_back_to_research_only_for_manual_candidates(monkeypatch):
    monkeypatch.setattr(
        holdings_cli_module,
        "_get_database",
        lambda **_kwargs: (_ for _ in ()).throw(
            CLIError("MongoDB connection refused", code="database_error", exit_code=4)
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_opportunity_candidates",
        lambda definitions, **_kwargs: [
            {
                "code": definitions[0]["code"],
                "name": "中国电信",
                "quote": {
                    "source": "tencent",
                    "price": 5.86,
                    "freshness": {"status": "live", "actionable": True},
                },
                "guarded_price_plan": {
                    "status": "ok",
                    "actionable": True,
                    "stop_loss_price": 5.6,
                    "suggested_buy_price": 5.86,
                    "target_price": 6.3,
                },
                "corporate_action": {
                    "status": "no_upcoming_corporate_action",
                    "blocks_new_position": False,
                },
                "one_lot_amount": 586.0,
                "affordable_with_cash": False,
                "cash_after_one_lot": None,
                "cash_usage_pct": None,
                "risk_flags": [],
                "is_reference_only": True,
            }
        ],
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "build_market_status_payload",
        lambda db=None, database_status=None, context=None: {
            "ok": True,
            "data": {
                "decision": {"action": "wait", "actionable": False},
                "database": database_status,
            },
            "meta": {
                "schema_version": 1,
                "source": "tencent_major_indices+akshare_sina_public_breadth",
            },
        },
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "opportunities",
            "--candidate-code",
            "601728",
            "--external-risk-level",
            "yellow",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["mode"] == "research_only"
    assert payload["data"]["database"] == {
        "status": "unavailable",
        "error_code": "database_error",
    }
    assert payload["data"]["account"]["status"] == "unavailable"
    assert payload["data"]["account"]["actionable"] is False
    assert payload["data"]["decision"]["actionable"] is False
    assert payload["data"]["decision"]["suggested_lots"] == 0
    assert "akshare_sina_public_breadth" in payload["meta"]["source"]
    candidate = payload["data"]["candidates"][0]
    assert candidate["quote"]["source"] == "tencent"
    assert candidate["guarded_price_plan"]["suggested_buy_price"] == 5.86
    assert candidate["corporate_action"]["status"] == "no_upcoming_corporate_action"
    assert candidate["affordable_with_cash"] is None
    assert candidate["decision"] == {
        "action": "observe",
        "actionable": False,
        "reason_code": "account_data_unavailable",
        "suggested_lots": 0,
        "suggested_quantity": 0,
    }


def test_opportunities_command_falls_back_when_lazy_database_read_fails(monkeypatch):
    context = make_opportunity_market_context()
    events = []
    database = object()
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            events.append(("mongo", timeout_cap_ms)) or database,
            {"status": "connected"},
        ),
    )

    def fail_full_builder(db, **kwargs):
        events.append(("full", db, kwargs.get("context")))
        raise holdings_cli_module.PyMongoError("MongoDB connection refused")

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunities_payload",
        fail_full_builder,
    )

    def fake_research_builder(**kwargs):
        events.append(("research", kwargs.get("context")))
        return {
            "ok": True,
            "data": {"mode": "research_only", "candidates": []},
            "meta": {"schema_version": 6},
        }

    monkeypatch.setattr(
        holdings_cli_module,
        "build_research_only_opportunities_payload",
        fake_research_builder,
    )

    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--candidate-code", "601728"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["data"]["mode"] == "research_only"
    assert events == [
        ("mongo", 5000),
        ("full", database, context),
        ("research", context),
    ]


def test_lazy_trade_context_failure_builds_research_candidates_only_once(monkeypatch):
    context = make_opportunity_market_context()
    database = make_fake_db()
    events = []
    candidate_fetches = []
    candidate_builds = []
    earnings_screen_calls = []
    original_candidate_builder = holdings_cli_module._build_opportunity_candidates
    original_earnings_screen = (
        holdings_cli_module.screen_public_candidate_earnings_risk
    )

    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_optional_market_database",
        lambda *, timeout_cap_ms=None: (
            database,
            {"status": "connected"},
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, *, db=None, context=None: {
            "status": "ok",
            "level": "green",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 1.0,
            "benchmark_trade_date": context.benchmark_trade_date,
            "trade_date": context.benchmark_trade_date,
            "indices": [],
            "breadth_regime": {"status": "ok", "source": "mongo.market_quotes"},
            "breadth_confirmation_required": False,
            "reason": "stable test market",
            "is_reference_only": True,
        },
    )

    def fail_trade_context(*_args, **_kwargs):
        events.append("trade_context")
        raise holdings_cli_module.PyMongoError("lazy trade read failed")

    def count_candidate_quote(code, **_kwargs):
        if str(code) == "601728":
            events.append("candidate_quote")
            candidate_fetches.append(str(code))
        return None

    def count_candidate_builds(*args, **kwargs):
        candidate_builds.append([item.get("code") for item in args[0]])
        return original_candidate_builder(*args, **kwargs)

    def count_earnings_screen(codes, *, benchmark_trade_date):
        earnings_screen_calls.append((list(codes), benchmark_trade_date))
        return original_earnings_screen(
            codes,
            benchmark_trade_date=benchmark_trade_date,
        )

    monkeypatch.setattr(holdings_cli_module, "_build_trade_context", fail_trade_context)
    monkeypatch.setattr(holdings_cli_module, "fetch_tencent_quote_sync", count_candidate_quote)
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_opportunity_candidates",
        count_candidate_builds,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "screen_public_candidate_earnings_risk",
        count_earnings_screen,
    )

    result = CliRunner().invoke(
        holdings_app,
        [
            "opportunities",
            "--username",
            "hermes",
            "--candidate-code",
            "601728",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["mode"] == "research_only"
    assert events == ["trade_context", "candidate_quote"]
    assert candidate_fetches == ["601728"]
    assert candidate_builds == [["601728"]]
    assert earnings_screen_calls == [
        (["601728"], context.benchmark_trade_date),
    ]


def test_opportunities_command_uses_public_research_when_database_config_fails(
    monkeypatch,
):
    context = make_opportunity_market_context()
    public_calls = []
    monkeypatch.setattr(
        holdings_cli_module,
        "build_opportunity_market_context",
        lambda: context,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_get_database",
        lambda: (_ for _ in ()).throw(
            CLIError("MongoDB connection refused", code="database_error", exit_code=4)
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_public_full_market_research_payload",
        lambda **kwargs: (
            public_calls.append(kwargs)
            or _make_public_command_payload(kwargs["database_status"])
        ),
    )

    result = CliRunner().invoke(holdings_app, ["opportunities"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["data"]["mode"] == "research_only"
    assert len(public_calls) == 1
    assert public_calls[0]["context"] is context
    assert public_calls[0]["database_status"] == {
        "status": "unavailable",
        "error_code": "database_error",
    }


def test_opportunities_help_documents_automatic_public_discovery_and_manual_candidates():
    result = CliRunner().invoke(
        holdings_app,
        ["opportunities", "--help"],
        terminal_width=240,
    )

    assert result.exit_code == 0
    normalized_help = " ".join(unstyle(result.stdout).replace("│", " ").split())
    assert "--external-risk-level" in normalized_help
    assert "green/yellow/red" in normalized_help
    assert "不传按 unknown 0% 处理" in normalized_help
    assert "--lot-size" not in normalized_help
    assert "不传候选时优先使用 Mongo" in normalized_help
    assert (
        "Mongo 不可用，或行情候选池不可用、为空、过期、覆盖不足时自动执行公开全市场研究"
        in normalized_help
    )
    assert "--candidate-code" in normalized_help
    assert "手工候选路径" in normalized_help
    assert "指定发现状态" not in normalized_help
    assert "Mongo不可用时显式候选降级" not in normalized_help
    assert "不传则从最新 Mongo 行情动态初筛" not in normalized_help


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


def test_build_market_status_payload_uses_one_command_context_and_schema_one(
    monkeypatch,
):
    batch_calls = []
    snapshot_calls = []
    symbols = holdings_cli_module.A_SHARE_REGIME_INDEX_SYMBOLS

    def fake_batch(*, timeout_seconds):
        batch_calls.append({"codes": tuple(symbols), "timeout": timeout_seconds})
        return {
            "status": "ok",
            "requested_codes": list(symbols),
            "rows": [
                {
                    "code": symbol[2:],
                    "provider_symbol": symbol,
                    "envelope_code": symbol[2:],
                    "payload_code": symbol[2:],
                    "parse_status": "ok",
                    "pct_chg": 0.1,
                    "trade_date": "2026-07-17",
                    "source": "tencent",
                }
                for symbol in symbols
            ],
            "error_type": None,
        }

    def fake_snapshot(**kwargs):
        snapshot_calls.append(kwargs)
        return {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-17",
            "provider_time": "14:30:00",
            "rows": [
                {
                    "code": f"600{index:03d}",
                    "name": f"sample-{index}",
                    "pct_chg": 1.0,
                    "trade_date": "2026-07-17",
                }
                for index in range(500)
            ],
        }

    monkeypatch.setattr(
        "app.services.opportunity_market_context.fetch_tencent_market_context_bounded",
        fake_batch,
    )
    monkeypatch.setattr(
        "app.services.opportunity_market_context.fetch_sina_public_market_snapshot",
        fake_snapshot,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_tencent_quote_sync",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("market-status must use the batch context")
        ),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "fetch_sina_public_market_breadth",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("market-status must use the context snapshot cache")
        ),
    )

    payload = holdings_cli_module.build_market_status_payload(db=None)

    assert len(batch_calls) == 1
    assert batch_calls[0]["codes"] == symbols
    assert batch_calls[0]["timeout"] <= 10.0
    assert len(snapshot_calls) == 1
    assert payload["meta"]["schema_version"] == 1
    assert payload["data"]["market_gate"]["benchmark_trade_date"] == "2026-07-17"


def test_build_market_status_payload_exposes_premarket_refresh_context(monkeypatch):
    context = make_hydrated_opportunity_market_context()
    context.now = datetime(2026, 7, 20, 1, 7)
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, *, db=None, context=None: {
            "status": "ok",
            "level": "red",
            "new_position_allowed": False,
            "max_new_exposure_multiplier": 0.0,
            "benchmark_trade_date": "2026-07-17",
            "trade_date": "2026-07-17",
            "indices": [{"code": "sh000001", "pct_chg": -3.05}],
            "breadth_regime": {
                "status": "ok",
                "level": "red",
                "source": "akshare.sina.stock_zh_a_spot",
            },
            "breadth_confirmation_required": False,
            "reason": "最近交易日市场门禁为红色。",
        },
    )

    payload = holdings_cli_module.build_market_status_payload(
        db=None,
        context=context,
    )

    assert payload["meta"]["schema_version"] == 1
    assert payload["data"]["market_session"] == {
        "market": "CN",
        "timezone": "Asia/Shanghai",
        "local_time": "2026-07-20T01:07:00+08:00",
        "session": "pre_open",
        "is_trading_hours": False,
        "quote_stale_risk": True,
        "minutes_to_close": 833,
        "is_late_session": False,
        "next_refresh_at": "2026-07-20T09:30:00+08:00",
        "next_refresh_session": "open",
    }
    assert payload["data"]["market_gate"]["benchmark_trade_date"] == (
        "2026-07-17"
    )


def test_build_market_status_payload_waits_when_mongo_breadth_is_unavailable(monkeypatch):
    seen_databases = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_benchmark_session_dates",
        lambda: (_ for _ in ()).throw(
            AssertionError("market-status must not call the historical calendar endpoint")
        ),
    )

    def fake_market_gate(benchmark_trade_date, db=None, context=None):
        assert benchmark_trade_date is None
        assert context is not None
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


def test_build_market_status_payload_marks_public_breadth_completeness(monkeypatch):
    monkeypatch.setattr(
        holdings_cli_module,
        "_build_a_share_market_gate",
        lambda benchmark_trade_date, db=None, context=None: {
            "status": "ok",
            "level": "yellow",
            "new_position_allowed": True,
            "max_new_exposure_multiplier": 0.5,
            "benchmark_trade_date": "2026-07-15",
            "trade_date": "2026-07-15",
            "indices": [{"code": "sh000001", "pct_chg": 0.1}],
            "breadth_regime": {
                "status": "ok",
                "level": "yellow",
                "source": "akshare.sina.stock_zh_a_spot",
            },
            "breadth_confirmation_required": False,
            "reason": "市场宽度偏弱，新仓风险预算减半。",
        },
    )

    payload = holdings_cli_module.build_market_status_payload(
        db=None,
        database_status={"status": "unavailable", "error_code": "database_error"},
    )

    assert payload["data"]["data_completeness"] == "indices_and_public_breadth"
    assert payload["data"]["decision"]["action"] == "evaluate_candidates"
    assert payload["meta"]["source"] == "tencent_major_indices+akshare_sina_public_breadth"


def test_build_market_status_payload_recovers_one_public_breadth_timeout():
    calls = []
    rows = [
        {
            "code": f"600{index:03d}",
            "name": f"样本{index}",
            "pct_chg": 1.0,
            "trade_date": "2026-07-17",
        }
        for index in range(500)
    ]

    def fake_snapshot_fetcher(**_kwargs):
        calls.append("fetch")
        if len(calls) == 1:
            return {
                "status": "public_breadth_timeout",
                "source": "akshare.sina.stock_zh_a_spot",
                "timeout_seconds": 25.0,
                "rows": [],
            }
        return {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-17",
            "provider_time": "15:00:00",
            "rows": rows,
        }

    context = make_opportunity_market_context(
        public_snapshot_fetcher=fake_snapshot_fetcher,
    )

    payload = holdings_cli_module.build_market_status_payload(
        db=None,
        context=context,
        retry_public_timeout=True,
    )

    breadth = payload["data"]["market_gate"]["breadth_regime"]
    assert calls == ["fetch", "fetch"]
    assert payload["data"]["data_completeness"] == "indices_and_public_breadth"
    assert breadth["status"] == "ok"
    assert breadth["public_snapshot_attempt_count"] == 2
    assert breadth["retried_after_status"] == "public_breadth_timeout"


def test_build_market_status_payload_keeps_lazy_mongo_read_failure_with_public_breadth():
    rows = [
        {
            "code": f"600{index:03d}",
            "name": f"样本{index}",
            "pct_chg": 1.0,
            "trade_date": "2026-07-15",
        }
        for index in range(500)
    ]
    context = make_opportunity_market_context(
        public_snapshot_fetcher=lambda **_kwargs: {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "provider_trade_date": "2026-07-15",
            "provider_time": "14:40:00",
            "universe_size": len(rows),
            "rows": rows,
        },
    )
    context.benchmark_trade_date = "2026-07-15"
    for quote in context.index_quotes:
        quote["trade_date"] = "2026-07-15"

    class LazyFailingCollection:
        def find(self, *_args, **_kwargs):
            raise RuntimeError("lazy Mongo read failed")

    db = make_fake_db()
    db.collections["market_quotes"] = LazyFailingCollection()

    payload = holdings_cli_module.build_market_status_payload(
        db=db,
        database_status={"status": "connected"},
        context=context,
    )

    assert payload["data"]["data_completeness"] == "indices_and_public_breadth"
    assert payload["data"]["database"] == {
        "status": "unavailable",
        "error_code": "database_error",
        "error_type": "RuntimeError",
    }
    assert payload["data"]["market_gate"]["mongo_breadth"]["status"] == "load_failed"


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
        lambda benchmark_trade_date, db=None, context=None: {
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


def test_cash_deployment_plan_blocks_candidate_waiting_for_trend_recovery(
    monkeypatch,
):
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
            "trend_context": {
                "state": "recovery_required",
                "recovery_required": True,
                "bearish_short_term_alignment": True,
                "drawdown_from_20d_high_pct": -32.0,
                "distance_to_entry_pct": 1.01,
                "below_key_averages": ["ma5", "ma10", "ma20", "ma60"],
            },
        },
    )
    monkeypatch.setattr(
        "app.services.holdings_cli.fetch_tencent_quote_sync",
        lambda code: {
            "code": code,
            "name": "趋势修复标的",
            "price": 19.8,
            "high": 20.0,
            "low": 19.4,
            "turnover_rate": 2.0,
            "pct_chg": -2.0,
            "source": "tencent",
        },
    )
    db = make_fake_db()
    db.collections["user_holdings"].docs.clear()
    db.collections["user_holding_settings"].docs[0]["total_assets"] = 10_640.0

    payload = build_opportunities_payload(
        db,
        username="hermes",
        candidate_codes=["000066"],
        external_risk_level="green",
    )

    candidate = payload["data"]["candidates"][0]
    assert "trend_recovery_required" in {
        flag["key"] for flag in candidate["risk_flags"]
    }
    assert candidate["guarded_price_plan"]["status"] == (
        "trend_recovery_required"
    )
    assert candidate["guarded_price_plan"]["actionable"] is False
    assert candidate["guarded_price_plan"]["fee_aware_trade"][
        "net_reward_risk"
    ] >= 1.5
    plan_item = payload["data"]["brief"]["cash_deployment_plan"][
        "candidate_lot_plan"
    ][0]
    assert plan_item["suggested_lots"] == 0
    assert plan_item["risk_gate"] == "blocked_by_trend_recovery"
    assert "trend_recovery_required" in plan_item["blocking_failed_gates"]
    assert plan_item["activation_condition"] == "wait_for_trend_recovery"
    assert "重新站上短期均线" in plan_item["reason"]


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
