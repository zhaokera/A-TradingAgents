import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.tencent_quote_service import (
    assess_cn_quote_freshness,
    fetch_tencent_daily_bars_sync,
    fetch_tencent_quote_sync,
    merge_tencent_quote_into_bars,
    normalize_tencent_daily_bars,
    parse_tencent_quote_payload,
    to_tencent_symbol,
)


def _make_tencent_payload(
    *,
    code: str = "601006",
    name: str = "大秦铁路",
    price: str = "5.19",
    volume: str = "1234",
    amount_triplet: str = "",
    amount_wan: str = "640.45",
    turnover_rate: str = "0.69",
    circ_mv_yi: str = "0.93",
    total_mv_yi: str = "1.20",
    provider_timestamp: str = "20260710145930",
) -> str:
    fields = ["0"] * 50
    fields[1] = name
    fields[2] = code
    fields[3] = price
    fields[4] = "5.00"
    fields[5] = "5.10"
    fields[6] = volume
    fields[30] = provider_timestamp
    fields[31] = "0.19"
    fields[32] = "3.80"
    fields[33] = "5.20"
    fields[34] = "5.05"
    if amount_triplet:
        fields[35] = amount_triplet
    fields[37] = amount_wan
    fields[38] = turnover_rate
    fields[39] = "12.3"
    fields[43] = "2.00"
    fields[44] = circ_mv_yi
    fields[45] = total_mv_yi
    fields[46] = "1.20"
    fields[49] = "0.63"
    return f'v_sh{code}="{"~".join(fields)}";'


def test_to_tencent_symbol_adds_market_prefix():
    assert to_tencent_symbol("601006") == "sh601006"
    assert to_tencent_symbol("000977") == "sz000977"
    assert to_tencent_symbol("300750") == "sz300750"
    assert to_tencent_symbol("bj430047") == "bj430047"


def test_parse_tencent_quote_payload_returns_market_quote_shape():
    quote = parse_tencent_quote_payload("601006", _make_tencent_payload())

    assert quote is not None
    assert quote["code"] == "601006"
    assert quote["name"] == "大秦铁路"
    assert quote["source"] == "tencent"
    assert quote["data_source"] == "tencent"
    assert quote["close"] == 5.19
    assert quote["price"] == 5.19
    assert quote["current_price"] == 5.19
    assert quote["pct_chg"] == 3.8
    assert quote["change"] == 0.19
    assert quote["volume"] == 123400
    assert quote["amount"] == 6404500
    assert quote["open"] == 5.10
    assert quote["high"] == 5.20
    assert quote["low"] == 5.05
    assert quote["pre_close"] == 5.00
    assert quote["turnover_rate"] == 0.69
    assert quote["amplitude"] == 2.00
    assert quote["volume_ratio"] == 0.63
    assert quote["pe_ratio"] == 12.3
    assert quote["pb_ratio"] == 1.20
    assert quote["circ_mv"] == 93000000
    assert quote["total_mv"] == 120000000
    assert quote["trade_at"] == "2026-07-10T14:59:30+08:00"
    assert quote["trade_date"] == "2026-07-10"
    assert quote["received_at"].endswith("Z")


def test_parse_tencent_quote_payload_keeps_malformed_provider_time_non_actionable():
    quote = parse_tencent_quote_payload(
        "601006",
        _make_tencent_payload(provider_timestamp="not-a-market-time"),
    )

    assert quote is not None
    assert "trade_at" not in quote
    freshness = assess_cn_quote_freshness(
        quote,
        now=datetime(2026, 7, 10, 14, 59, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert freshness["actionable"] is False
    assert freshness["status"] == "missing_trade_at"


def test_parse_tencent_quote_payload_uses_precise_amount_and_share_volume():
    quote = parse_tencent_quote_payload(
        "688691",
        _make_tencent_payload(
            code="688691",
            name="灿芯股份",
            price="122.70",
            volume="10931723",
            amount_triplet="122.70/10931723/1327404280",
            amount_wan="168369.8131",
            turnover_rate="14.98",
            circ_mv_yi="89.53",
            total_mv_yi="147.24",
        ),
    )

    assert quote is not None
    assert quote["volume"] == 10931723
    assert quote["amount"] == 1327404280


def test_parse_tencent_quote_payload_rejects_empty_and_malformed_payloads():
    assert parse_tencent_quote_payload("601006", 'v_sh601006="";') is None
    assert parse_tencent_quote_payload("601006", "bad payload") is None


def test_fetch_tencent_quote_uses_https(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = _make_tencent_payload()
        encoding = None

    def fake_get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    quote = fetch_tencent_quote_sync("601006")

    assert quote is not None
    assert captured["url"].startswith("https://qt.gtimg.cn/")
    assert captured["headers"]["Referer"].startswith("https://")


def test_quote_freshness_uses_provider_time_at_exact_boundaries():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 10, 10, 0, 0, tzinfo=tz)

    exactly_five_minutes = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T09:55:00+08:00"},
        now=now,
    )
    five_minutes_and_one_second = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T09:54:59+08:00"},
        now=now,
    )
    exactly_sixty_seconds_future = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T10:01:00+08:00"},
        now=now,
    )
    sixty_one_seconds_future = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T10:01:01+08:00"},
        now=now,
    )

    assert exactly_five_minutes["actionable"] is True
    assert exactly_five_minutes["age_seconds"] == 300
    assert five_minutes_and_one_second["status"] == "stale_trade_at"
    assert exactly_sixty_seconds_future["actionable"] is True
    assert sixty_one_seconds_future["status"] == "future_trade_at"


def test_quote_freshness_compares_fractional_age_without_truncation():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 10, 10, 0, 0, 900000, tzinfo=tz)

    stale = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T09:55:00+08:00"},
        now=now,
    )
    future = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T10:01:01.800000+08:00"},
        now=now,
    )

    assert stale["status"] == "stale_trade_at"
    assert future["status"] == "future_trade_at"


def test_quote_freshness_rejects_friday_quote_during_monday_session():
    freshness = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T15:00:00+08:00"},
        now=datetime(2026, 7, 13, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert freshness["actionable"] is False
    assert freshness["status"] == "previous_trade_date"
    assert freshness["trade_date"] == "2026-07-10"


def test_quote_freshness_marks_off_session_and_fallback_as_display_only():
    lunch = assess_cn_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T11:30:00+08:00"},
        now=datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    fallback = assess_cn_quote_freshness(
        {"source": "akshare", "trade_at": "2026-07-10T10:00:00+08:00"},
        now=datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert lunch == {
        "actionable": False,
        "status": "off_session",
        "reason": "当前不在A股连续交易时段，行情仅用于研究展示。",
        "source": "tencent",
        "trade_at": "2026-07-10T11:30:00+08:00",
        "trade_date": "2026-07-10",
        "age_seconds": 1800,
        "session": "lunch_break",
    }
    assert fallback["actionable"] is False
    assert fallback["status"] == "display_only_source"


def test_normalize_tencent_daily_bars_drops_invalid_rows_deduplicates_and_sorts():
    rows = [
        {"date": "2026-07-09", "open": "10", "close": "11", "high": "11.2", "low": "9.8", "volume": "100"},
        {"date": "bad", "open": "1", "close": "1", "high": "1", "low": "1"},
        {"date": "2026-07-08", "open": "9", "close": "10", "high": "10.2", "low": "8.9", "volume": "90"},
        {"date": "2026-07-09", "open": "10.5", "close": "11.5", "high": "11.8", "low": "10.3", "volume": "120"},
        {"date": "2026-07-10", "open": "0", "close": "0", "high": "0", "low": "0"},
    ]

    bars = normalize_tencent_daily_bars(rows)

    assert [item["date"] for item in bars] == ["2026-07-08", "2026-07-09"]
    assert bars[-1]["close"] == 11.5
    assert bars[-1]["volume"] == 120.0


def test_normalize_tencent_daily_bars_rejects_non_finite_numbers():
    rows = [
        {"date": "2026-07-07", "open": "NaN", "close": 10, "high": 11, "low": 9},
        {"date": "2026-07-08", "open": 10, "close": "Infinity", "high": 11, "low": 9},
        {"date": "2026-07-09", "open": 10, "close": 10, "high": float("inf"), "low": 9},
        {
            "date": "2026-07-10",
            "open": 10,
            "close": 10.5,
            "high": 11,
            "low": 9.5,
            "volume": float("nan"),
            "amount": "-Infinity",
        },
    ]

    bars = normalize_tencent_daily_bars(rows)

    assert bars == [
        {
            "date": "2026-07-10",
            "open": 10.0,
            "close": 10.5,
            "high": 11.0,
            "low": 9.5,
        }
    ]


def test_normalize_tencent_daily_bars_rejects_prices_outside_high_low_range():
    rows = [
        {"date": "2026-07-06", "open": 12, "close": 10, "high": 11, "low": 9},
        {"date": "2026-07-07", "open": 10, "close": 12, "high": 11, "low": 9},
        {"date": "2026-07-08", "open": 8, "close": 10, "high": 11, "low": 9},
        {"date": "2026-07-09", "open": 10, "close": 8, "high": 11, "low": 9},
        {"date": "2026-07-10", "open": 10, "close": 10.5, "high": 11, "low": 9.5},
    ]

    bars = normalize_tencent_daily_bars(rows)

    assert [bar["date"] for bar in bars] == ["2026-07-10"]


def test_fetch_tencent_daily_bars_uses_tx_qfq_contract(monkeypatch):
    captured = {}

    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"date": "2026-07-08", "open": 9, "close": 10, "high": 10.2, "low": 8.9},
                {"date": "2026-07-09", "open": 10, "close": 11, "high": 11.2, "low": 9.8},
            ]

    def fake_history(**kwargs):
        captured.update(kwargs)
        return FakeFrame()

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_a_hist_tx=fake_history))

    result = fetch_tencent_daily_bars_sync(
        "000977",
        start_date="20260701",
        end_date="20260710",
        min_rows=2,
    )

    assert captured == {
        "symbol": "sz000977",
        "start_date": "20260701",
        "end_date": "20260710",
        "adjust": "qfq",
    }
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert len(result["bars"]) == 2


def test_fetch_tencent_daily_bars_returns_structured_insufficient_history(monkeypatch):
    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            return [{"date": "2026-07-09", "open": 10, "close": 11, "high": 11.2, "low": 9.8}]

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist_tx=lambda **kwargs: FakeFrame()),
    )

    result = fetch_tencent_daily_bars_sync(
        "000977",
        start_date="20260701",
        end_date="20260710",
        min_rows=60,
    )

    assert result["ok"] is False
    assert result["status"] == "insufficient_history"
    assert result["required_rows"] == 60
    assert result["available_rows"] == 1


def test_merge_tencent_quote_replaces_same_day_or_appends_next_day():
    bars = [
        {"date": "2026-07-09", "open": 10.0, "close": 10.5, "high": 10.8, "low": 9.9, "volume": 1000.0},
        {"date": "2026-07-10", "open": 10.5, "close": 11.0, "high": 11.2, "low": 10.4, "volume": 1200.0},
    ]
    replacement = merge_tencent_quote_into_bars(
        bars,
        {
            "trade_date": "2026-07-10",
            "open": 10.6,
            "price": 11.4,
            "high": 11.5,
            "low": 10.5,
            "volume": 1400,
        },
    )
    appended = merge_tencent_quote_into_bars(
        replacement["bars"],
        {
            "trade_date": "2026-07-13",
            "open": 11.4,
            "price": 11.8,
            "high": 12.0,
            "low": 11.3,
            "volume": 800,
        },
    )

    assert replacement["ok"] is True
    assert replacement["merge_action"] == "replace"
    assert replacement["bars"][-1]["close"] == 11.4
    assert appended["merge_action"] == "append"
    assert [item["date"] for item in appended["bars"]] == ["2026-07-09", "2026-07-10", "2026-07-13"]


def test_merge_tencent_quote_rejects_adjustment_scale_mismatch():
    result = merge_tencent_quote_into_bars(
        [{"date": "2026-07-10", "open": 100.0, "close": 100.0, "high": 101.0, "low": 99.0}],
        {
            "trade_date": "2026-07-10",
            "open": 50.0,
            "price": 50.0,
            "high": 51.0,
            "low": 49.0,
        },
    )

    assert result["ok"] is False
    assert result["status"] == "price_scale_mismatch"
    assert result["price_ratio"] == 0.5
