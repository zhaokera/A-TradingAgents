import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import tencent_quote_service as quote_service
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
    limit_up: str = "5.71",
    limit_down: str = "4.67",
    volume_ratio: str = "0.63",
    provider_timestamp: str = "20260710145930",
    provider_symbol: str | None = None,
    field_count: int = 50,
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
    fields[47] = limit_up
    fields[48] = limit_down
    fields[49] = volume_ratio
    symbol = provider_symbol or f"sh{code}"
    return f'v_{symbol}="{"~".join(fields[:field_count])}";'


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
    assert quote["limit_up"] == 5.71
    assert quote["limit_down"] == 4.67
    assert quote["volume_ratio"] == 0.63
    assert quote["pe_ratio"] == 12.3
    assert quote["pb_ratio"] == 1.20
    assert quote["circ_mv"] == 93000000
    assert quote["total_mv"] == 120000000
    assert quote["trade_at"] == "2026-07-10T14:59:30+08:00"
    assert quote["provider_updated_at"] == "2026-07-10T14:59:30+08:00"
    assert quote["quote_time_semantics"] == "provider_snapshot_updated_at"
    assert quote["exchange_trade_time_verified"] is False
    assert quote["trade_at_compatibility_alias"] is True
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


def test_post_close_provider_update_time_is_a_non_actionable_compatibility_alias():
    quote = parse_tencent_quote_payload(
        "000969",
        _make_tencent_payload(
            code="000969",
            provider_symbol="sz000969",
            provider_timestamp="20260729161418",
        ),
    )

    freshness = assess_cn_quote_freshness(
        quote,
        now=datetime(2026, 7, 29, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert freshness["actionable"] is False
    assert freshness["status"] == "off_session"
    assert freshness["provider_updated_at"] == "2026-07-29T16:14:18+08:00"
    assert freshness["quote_time_semantics"] == "provider_snapshot_updated_at"
    assert freshness["exchange_trade_time_verified"] is False
    assert "成交时间" not in freshness["reason"]


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


def test_parse_tencent_quote_payload_rejects_a_complete_non_positive_price():
    assert (
        parse_tencent_quote_payload(
            "601006",
            _make_tencent_payload(price="0"),
        )
        is None
    )


def test_parse_tencent_quote_batch_preserves_order_symbols_and_duplicates():
    payload = "\n".join(
        [
            _make_tencent_payload(
                code="600000",
                name="浦发银行",
                provider_symbol="sh600000",
            ),
            _make_tencent_payload(
                code="000001",
                name="平安银行",
                provider_symbol="sz000001",
            ),
            _make_tencent_payload(
                code="600000",
                name="浦发银行重复行",
                provider_symbol="sh600000",
            ),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert [row["code"] for row in rows] == ["600000", "000001", "600000"]
    assert [row["provider_symbol"] for row in rows] == [
        "sh600000",
        "sz000001",
        "sh600000",
    ]
    assert [row["envelope_code"] for row in rows] == [
        "600000",
        "000001",
        "600000",
    ]
    assert [row["payload_code"] for row in rows] == [
        "600000",
        "000001",
        "600000",
    ]
    assert [row["parse_status"] for row in rows] == ["ok", "ok", "ok"]
    assert rows[2]["name"] == "浦发银行重复行"


def test_parse_tencent_quote_batch_preserves_complete_invalid_price_identity():
    payload = "\n".join(
        [
            _make_tencent_payload(
                code="600000",
                name="浦发银行",
                provider_symbol="sh600000",
            ),
            _make_tencent_payload(
                code="600000",
                name="浦发银行无效价",
                price="0",
                provider_symbol="sh600000",
            ),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert [row["code"] for row in rows] == ["600000", "600000"]
    assert [row["provider_symbol"] for row in rows] == [
        "sh600000",
        "sh600000",
    ]
    assert rows[0]["close"] == 5.19
    assert rows[1].get("close") is None
    assert rows[1] == {
        "code": "600000",
        "provider_symbol": "sh600000",
        "envelope_code": "600000",
        "payload_code": "600000",
        "parse_status": "invalid_price",
        "source": "tencent",
        "close": None,
        "amount": 6_404_500.0,
    }
    assert json.dumps(rows, allow_nan=False)


def test_parse_tencent_quote_batch_preserves_malformed_assignment_envelopes():
    payload = "\n".join(
        [
            _make_tencent_payload(code="600000", provider_symbol="sh600000"),
            'v_sz300750="damaged";',
            "",
            'v_bj430047="";',
            _make_tencent_payload(code="000001", provider_symbol="sz000001"),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert [row["code"] for row in rows] == [
        "600000",
        "300750",
        "430047",
        "000001",
    ]
    assert [row["envelope_code"] for row in rows] == [
        "600000",
        "300750",
        "430047",
        "000001",
    ]
    assert [row["payload_code"] for row in rows] == [
        "600000",
        None,
        None,
        "000001",
    ]
    assert [row["parse_status"] for row in rows] == [
        "ok",
        "malformed_payload",
        "empty_payload",
        "ok",
    ]


def test_parse_tencent_quote_batch_keeps_empty_and_valid_same_envelope_rows():
    payload = "\n".join(
        [
            'v_sh600000="";',
            _make_tencent_payload(code="600000", provider_symbol="sh600000"),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert len(rows) == 2
    assert [row["envelope_code"] for row in rows] == ["600000", "600000"]
    assert [row["parse_status"] for row in rows] == ["empty_payload", "ok"]
    assert rows[0]["payload_code"] is None
    assert rows[0]["code"] == "600000"


def test_parse_tencent_quote_batch_keeps_49_field_invalid_price_assignment():
    rows = quote_service.parse_tencent_quote_batch_payload(
        _make_tencent_payload(
            code="600000",
            provider_symbol="sh600000",
            price="0",
            field_count=49,
        )
    )

    assert len(rows) == 1
    assert rows[0]["envelope_code"] == "600000"
    assert rows[0]["payload_code"] == "600000"
    assert rows[0]["parse_status"] == "invalid_price"
    assert rows[0].get("close") is None


def test_parse_tencent_quote_batch_separates_envelope_and_payload_code():
    rows = quote_service.parse_tencent_quote_batch_payload(
        _make_tencent_payload(
            code="000001",
            provider_symbol="sh600000",
        )
    )

    assert len(rows) == 1
    assert rows[0]["provider_symbol"] == "sh600000"
    assert rows[0]["envelope_code"] == "600000"
    assert rows[0]["payload_code"] == "000001"
    assert rows[0]["code"] == "000001"
    assert rows[0]["parse_status"] == "ok"


def test_parse_tencent_quote_batch_ignores_assignment_like_text_inside_fields():
    payload = "\n".join(
        [
            _make_tencent_payload(
                code="600000",
                name="浦发银行 v_sz300750=not-an-assignment",
                provider_symbol="sh600000",
            ),
            _make_tencent_payload(code="000001", provider_symbol="sz000001"),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert [row["code"] for row in rows] == ["600000", "000001"]
    assert rows[0]["name"] == "浦发银行 v_sz300750=not-an-assignment"


def test_parse_tencent_quote_batch_parses_semicolon_adjacent_assignments():
    payload = "".join(
        [
            _make_tencent_payload(code="600000", provider_symbol="sh600000"),
            _make_tencent_payload(code="000001", provider_symbol="sz000001"),
        ]
    )

    rows = quote_service.parse_tencent_quote_batch_payload(payload)

    assert [row["code"] for row in rows] == ["600000", "000001"]
    assert [row["provider_symbol"] for row in rows] == ["sh600000", "sz000001"]


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


def test_fetch_tencent_quotes_uses_one_request_and_ordered_unique_codes(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = "\n".join(
            [
                _make_tencent_payload(code="600000", provider_symbol="sh600000"),
                _make_tencent_payload(code="000001", provider_symbol="sz000001"),
                _make_tencent_payload(code="600000", provider_symbol="sh600000"),
            ]
        )
        encoding = None

    response = FakeResponse()

    def fake_get(url, *, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return response

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    result = quote_service.fetch_tencent_quotes_sync(
        ["600000", "sh600000", "000001.SZ"],
        timeout=7.5,
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://qt.gtimg.cn/q=sh600000,sz000001"
    assert calls[0]["timeout"] == 7.5
    assert calls[0]["headers"]["Referer"] == "https://finance.qq.com"
    assert response.encoding == "gbk"
    assert result["status"] == "ok"
    assert result["requested_codes"] == ["600000", "000001"]
    assert [row["code"] for row in result["rows"]] == ["600000", "000001", "600000"]
    assert result["error_type"] is None


def test_fetch_tencent_quotes_preserves_four_major_index_provider_symbols(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""
        encoding = None

    def fake_get(url, *, headers, timeout):
        calls.append({"url": url, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    result = quote_service.fetch_tencent_quotes_sync(
        ["sh000001", "sz399001", "sz399006", "sh000688"],
        timeout=9.5,
    )

    assert calls == [
        {
            "url": (
                "https://qt.gtimg.cn/q="
                "sh000001,sz399001,sz399006,sh000688"
            ),
            "timeout": 9.5,
        }
    ]
    assert result["status"] == "ok"
    assert result["requested_codes"] == [
        "sh000001",
        "sz399001",
        "sz399006",
        "sh000688",
    ]


def test_fetch_tencent_quotes_deduplicates_index_aliases_by_provider_symbol(
    monkeypatch,
):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""
        encoding = None

    def fake_get(url, *, headers, timeout):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    result = quote_service.fetch_tencent_quotes_sync(
        [
            "sz399001",
            "399001",
            "399001.SZ",
            "sz399006",
            "399006",
            "399006.SZ",
            "000001",
            "sz000001",
        ]
    )

    assert result["requested_codes"] == ["sz399001", "sz399006", "000001"]
    assert captured["url"] == (
        "https://qt.gtimg.cn/q=sz399001,sz399006,sz000001"
    )


def test_fetch_tencent_quotes_limits_request_to_first_40_unique_codes(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""
        encoding = None

    def fake_get(url, *, headers, timeout):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)
    codes = [str(code) for code in range(600000, 600041)]

    result = quote_service.fetch_tencent_quotes_sync(codes)

    assert result["requested_codes"] == codes[:40]
    assert captured["url"].count(",") == 39
    assert captured["url"].endswith("sh600039")


def test_fetch_tencent_quotes_batched_splits_160_codes_into_ordered_40_code_requests(
    monkeypatch,
):
    codes = [f"{600000 + index:06d}" for index in range(160)]
    calls = []

    def fake_fetch(batch, *, timeout):
        requested_codes = list(batch)
        calls.append({"codes": requested_codes, "timeout": timeout})
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [{"code": code} for code in requested_codes],
            "error_type": None,
        }

    monotonic_values = iter([100.0, 100.1, 100.2, 100.3, 100.4])
    monkeypatch.setattr(quote_service, "fetch_tencent_quotes_sync", fake_fetch)
    monkeypatch.setattr(
        quote_service.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = quote_service.fetch_tencent_quotes_batched_sync(codes, timeout=8.0)

    assert [len(call["codes"]) for call in calls] == [40, 40, 40, 40]
    assert [code for call in calls for code in call["codes"]] == codes
    assert all(0 < call["timeout"] <= 8.0 for call in calls)
    assert result == {
        "status": "ok",
        "requested_codes": codes,
        "rows": [{"code": code} for code in codes],
        "error_type": None,
        "batch_count": 4,
        "completed_batch_count": 4,
    }


def test_fetch_tencent_quotes_batched_fails_closed_without_partial_rows(monkeypatch):
    codes = [f"{600000 + index:06d}" for index in range(81)]
    calls = []

    def fake_fetch(batch, *, timeout):
        requested_codes = list(batch)
        calls.append(requested_codes)
        if len(calls) == 2:
            return {
                "status": "fetch_error",
                "requested_codes": requested_codes,
                "rows": [],
                "error_type": "request_timeout",
            }
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [{"code": code} for code in requested_codes],
            "error_type": None,
        }

    monkeypatch.setattr(quote_service, "fetch_tencent_quotes_sync", fake_fetch)

    result = quote_service.fetch_tencent_quotes_batched_sync(codes, timeout=8.0)

    assert len(calls) == 2
    assert result == {
        "status": "fetch_error",
        "requested_codes": codes,
        "rows": [],
        "error_type": "request_timeout",
        "batch_count": 3,
        "completed_batch_count": 1,
        "failed_batch_index": 1,
    }


def test_fetch_tencent_quotes_batched_caps_the_public_screen_pool_at_160(monkeypatch):
    codes = [f"{600000 + index:06d}" for index in range(170)]
    calls = []

    def fake_fetch(batch, *, timeout):
        requested_codes = list(batch)
        calls.append(requested_codes)
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [],
            "error_type": None,
        }

    monkeypatch.setattr(quote_service, "fetch_tencent_quotes_sync", fake_fetch)

    result = quote_service.fetch_tencent_quotes_batched_sync(codes)

    assert result["requested_codes"] == codes[:160]
    assert len(calls) == 4
    assert all(len(batch) == 40 for batch in calls)


def test_fetch_tencent_quotes_returns_empty_without_request(monkeypatch):
    def unexpected_get(url, *, headers, timeout):
        raise AssertionError("empty request must not call Tencent")

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", unexpected_get)

    result = quote_service.fetch_tencent_quotes_sync([])

    assert result == {
        "status": "empty",
        "requested_codes": [],
        "rows": [],
        "error_type": None,
    }


def test_fetch_tencent_quotes_returns_stable_http_failure(monkeypatch):
    class FakeResponse:
        status_code = 503
        text = "unavailable"
        encoding = None

    monkeypatch.setattr(
        "app.services.tencent_quote_service.requests.get",
        lambda url, *, headers, timeout: FakeResponse(),
    )

    result = quote_service.fetch_tencent_quotes_sync(["600000"])

    assert result == {
        "status": "fetch_error",
        "requested_codes": ["600000"],
        "rows": [],
        "error_type": "HTTPError",
        "http_status": 503,
    }


def test_fetch_tencent_quotes_returns_stable_request_failure(monkeypatch):
    def fake_get(url, *, headers, timeout):
        raise quote_service.requests.Timeout("slow response")

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    result = quote_service.fetch_tencent_quotes_sync(["600000"])

    assert result == {
        "status": "fetch_error",
        "requested_codes": ["600000"],
        "rows": [],
        "error_type": "request_timeout",
    }


def test_fetch_tencent_quotes_rejects_scalar_code_inputs_without_request(monkeypatch):
    def unexpected_get(url, *, headers, timeout):
        raise AssertionError("invalid top-level input must not call Tencent")

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", unexpected_get)

    for invalid_codes in (None, "600000", b"600000"):
        result = quote_service.fetch_tencent_quotes_sync(invalid_codes)

        assert result == {
            "status": "invalid_request",
            "requested_codes": [],
            "rows": [],
            "error_type": "invalid_codes",
        }


def test_fetch_tencent_quotes_only_accepts_valid_exchange_matched_ascii_codes(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""
        encoding = None

    def fake_get(url, *, headers, timeout):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    result = quote_service.fetch_tencent_quotes_sync(
        [
            "foo1",
            "６０００００",
            "500000",
            "sz600000",
            "600000.sz",
            "sh600000",
            "600000.SH",
            "000001",
            "SZ300750",
            "430047.BJ",
            "bj830001",
            "870001",
            "880001",
            "920001",
        ]
    )

    assert result["requested_codes"] == [
        "600000",
        "000001",
        "300750",
        "430047",
        "830001",
        "870001",
        "880001",
        "920001",
    ]
    assert captured["url"] == (
        "https://qt.gtimg.cn/q="
        "sh600000,sz000001,sz300750,bj430047,"
        "bj830001,bj870001,bj880001,bj920001"
    )


def test_fetch_tencent_quotes_returns_stable_failure_for_broken_iterator(monkeypatch):
    class BrokenCodes:
        def __iter__(self):
            yield "600000"
            raise RuntimeError("broken iterator")

    def unexpected_get(url, *, headers, timeout):
        raise AssertionError("broken iterator must not call Tencent")

    normalize_calls = []
    original_normalize = quote_service._normalize_tencent_request_code

    def track_normalize(value):
        normalize_calls.append(value)
        return original_normalize(value)

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", unexpected_get)
    monkeypatch.setattr(
        quote_service,
        "_normalize_tencent_request_code",
        track_normalize,
    )

    result = quote_service.fetch_tencent_quotes_sync(BrokenCodes())

    assert result == {
        "status": "invalid_request",
        "requested_codes": [],
        "rows": [],
        "error_type": "invalid_codes",
    }
    assert normalize_calls == []


def test_fetch_tencent_quotes_does_not_hide_internal_normalization_errors(
    monkeypatch,
):
    def broken_normalize(value):
        raise RuntimeError(f"normalizer failed for {value}")

    monkeypatch.setattr(
        quote_service,
        "_normalize_tencent_request_code",
        broken_normalize,
    )

    with pytest.raises(RuntimeError, match="normalizer failed"):
        quote_service.fetch_tencent_quotes_sync(["600000"])


def test_fetch_tencent_quotes_reports_parser_internal_error(
    monkeypatch,
    caplog,
):
    class FakeResponse:
        status_code = 200
        text = _make_tencent_payload()
        encoding = None

    monkeypatch.setattr(
        quote_service.requests,
        "get",
        lambda url, *, headers, timeout: FakeResponse(),
    )
    monkeypatch.setattr(
        quote_service,
        "parse_tencent_quote_batch_payload",
        lambda payload: (_ for _ in ()).throw(RuntimeError("parser exploded")),
    )

    with caplog.at_level("ERROR", logger=quote_service.__name__):
        result = quote_service.fetch_tencent_quotes_sync(["600000"])

    assert result == {
        "status": "internal_error",
        "requested_codes": ["600000"],
        "rows": [],
        "error_type": "parser_error",
    }
    assert any(record.exc_info for record in caplog.records)


def test_fetch_tencent_quotes_uses_stable_request_exception_types(monkeypatch):
    current_error = quote_service.requests.ConnectTimeout("connect timeout")

    def fake_get(url, *, headers, timeout):
        raise current_error

    monkeypatch.setattr("app.services.tencent_quote_service.requests.get", fake_get)

    for timeout_error in (
        quote_service.requests.Timeout("timeout"),
        quote_service.requests.ConnectTimeout("connect timeout"),
        quote_service.requests.ReadTimeout("read timeout"),
    ):
        current_error = timeout_error
        result = quote_service.fetch_tencent_quotes_sync(["600000"])
        assert result["error_type"] == "request_timeout"

    current_error = quote_service.requests.ConnectionError("connection failed")
    result = quote_service.fetch_tencent_quotes_sync(["600000"])
    assert result["error_type"] == "request_failed"


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
        "provider_updated_at": "2026-07-10T11:30:00+08:00",
        "quote_time_semantics": "legacy_provider_time_unverified",
        "exchange_trade_time_verified": False,
        "trade_date": "2026-07-10",
        "age_seconds": 1800,
        "session": "lunch_break",
    }
    assert fallback["actionable"] is False
    assert fallback["status"] == "display_only_source"


def test_research_quote_freshness_uses_intraday_age_boundaries():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    exactly_five_minutes = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T09:55:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    five_minutes_and_one_second = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T09:54:59+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    exactly_two_minutes_future = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T10:02:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    two_minutes_and_one_second_future = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T10:02:01+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )

    assert exactly_five_minutes["data_complete"] is True
    assert exactly_five_minutes["age_seconds"] == 300
    assert five_minutes_and_one_second["data_complete"] is False
    assert five_minutes_and_one_second["status"] == "stale_trade_at"
    assert exactly_two_minutes_future["data_complete"] is True
    assert two_minutes_and_one_second_future["data_complete"] is False
    assert two_minutes_and_one_second_future["status"] == "future_trade_at"
    assert all(
        "actionable" not in result
        for result in (
            exactly_five_minutes,
            five_minutes_and_one_second,
            exactly_two_minutes_future,
            two_minutes_and_one_second_future,
        )
    )


def test_research_quote_freshness_uses_completed_day_close_threshold():
    now = datetime(2026, 7, 10, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    complete = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T14:55:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    incomplete = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T14:54:59+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )

    assert complete["data_complete"] is True
    assert complete["status"] == "fresh"
    assert incomplete["data_complete"] is False
    assert incomplete["status"] == "stale_trade_at"


def test_research_quote_freshness_limits_completed_day_future_skew():
    now = datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    exactly_two_minutes_future = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T15:02:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    two_minutes_and_one_second_future = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T15:02:01+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )

    assert exactly_two_minutes_future["data_complete"] is True
    assert two_minutes_and_one_second_future["data_complete"] is False
    assert two_minutes_and_one_second_future["status"] == "future_trade_at"


def test_research_quote_freshness_handles_datetime_max_without_overflow():
    result = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "9999-12-31T23:59:59+08:00"},
        benchmark_trade_date="9999-12-31",
        now=datetime.max.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["data_complete"] is True
    assert result["status"] == "fresh"


def test_research_quote_freshness_handles_extreme_future_skew_without_overflow():
    result = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T15:02:01+08:00"},
        benchmark_trade_date="2026-07-10",
        now=datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        max_future_skew_seconds=10**20,
    )

    assert result["data_complete"] is True
    assert result["status"] == "fresh"


def test_research_quote_freshness_treats_prior_benchmark_as_completed_day():
    quote = {"source": "tencent", "trade_at": "2026-07-10T14:55:00+08:00"}
    before_open = datetime(2026, 7, 13, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    weekend = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    before_open_result = quote_service.assess_tencent_research_quote_freshness(
        quote,
        benchmark_trade_date="2026-07-10",
        now=before_open,
    )
    weekend_result = quote_service.assess_tencent_research_quote_freshness(
        quote,
        benchmark_trade_date="2026-07-10",
        now=weekend,
    )
    incomplete = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-10T14:54:59+08:00"},
        benchmark_trade_date="2026-07-10",
        now=before_open,
    )

    assert before_open_result["data_complete"] is True
    assert weekend_result["data_complete"] is True
    assert incomplete["data_complete"] is False
    assert incomplete["status"] == "stale_trade_at"


def test_research_quote_freshness_rejects_wrong_or_future_benchmark_date():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    wrong_trade_date = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-09T15:00:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    future_benchmark = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "2026-07-13T15:00:00+08:00"},
        benchmark_trade_date="2026-07-13",
        now=now,
    )

    assert wrong_trade_date["data_complete"] is False
    assert wrong_trade_date["status"] == "trade_date_mismatch"
    assert future_benchmark["data_complete"] is False
    assert future_benchmark["status"] == "future_benchmark_trade_date"


def test_research_quote_freshness_requires_tencent_and_parseable_trade_at():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    wrong_source = quote_service.assess_tencent_research_quote_freshness(
        {"source": "akshare", "trade_at": "2026-07-10T10:00:00+08:00"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )
    malformed_time = quote_service.assess_tencent_research_quote_freshness(
        {"source": "tencent", "trade_at": "not-a-time"},
        benchmark_trade_date="2026-07-10",
        now=now,
    )

    assert wrong_source["data_complete"] is False
    assert wrong_source["status"] == "invalid_source"
    assert malformed_time["data_complete"] is False
    assert malformed_time["status"] == "missing_trade_at"


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
        db_factory=lambda: {},
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
        db_factory=lambda: {},
    )

    assert result["ok"] is False
    assert result["status"] == "insufficient_history"
    assert result["required_rows"] == 60
    assert result["available_rows"] == 1


def test_fetch_tencent_daily_bars_retries_then_uses_fresh_audited_cache(monkeypatch):
    now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
    calls = []
    sleeps = []
    bars = [
        {
            "date": (datetime(2026, 5, 29) + timedelta(days=index)).date().isoformat(),
            "open": 10.0,
            "close": 10.1,
            "high": 10.2,
            "low": 9.9,
        }
        for index in range(60)
    ]

    def failing_history(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("upstream disconnected")

    class Collection:
        def find_one(self, query):
            assert query == {"_id": "000977:qfq"}
            return {
                "_id": "000977:qfq",
                "source": "tencent",
                "checked_at": now - timedelta(hours=1),
                "bars": bars,
            }

    class Database:
        def __getitem__(self, name):
            assert name == quote_service.TENCENT_HISTORY_CACHE_COLLECTION
            return Collection()

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist_tx=failing_history),
    )

    result = fetch_tencent_daily_bars_sync(
        "000977",
        start_date="20260501",
        end_date="20260728",
        min_rows=60,
        now=now,
        db_factory=Database,
        sleeper=sleeps.append,
    )

    assert len(calls) == quote_service.TENCENT_HISTORY_FETCH_ATTEMPTS
    assert sleeps == [quote_service.TENCENT_HISTORY_RETRY_SECONDS]
    assert result["ok"] is True
    assert result["source"] == "mongo.candidate_technical_history_cache"
    assert result["freshness"] == "cached_fresh"
    assert result["degraded"] is True
    assert result["cache_age_seconds"] == 3600.0
    assert result["provider_errors"] == [
        {
            "provider": "tencent",
            "status": "fetch_error",
            "error_type": "ConnectionError",
            "checked_at": now.isoformat(),
        }
    ]


def test_fetch_tencent_daily_bars_can_prefer_fresh_audited_cache(monkeypatch):
    now = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)
    bars = [
        {
            "date": (datetime(2026, 5, 30) + timedelta(days=index)).date().isoformat(),
            "open": 10.0,
            "close": 10.1,
            "high": 10.2,
            "low": 9.9,
        }
        for index in range(60)
    ]

    class Collection:
        def find_one(self, query):
            assert query == {"_id": "000977:qfq"}
            return {
                "_id": "000977:qfq",
                "source": "tencent",
                "checked_at": now - timedelta(minutes=15),
                "bars": bars,
            }

    class Database:
        def __getitem__(self, name):
            assert name == quote_service.TENCENT_HISTORY_CACHE_COLLECTION
            return Collection()

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            stock_zh_a_hist_tx=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("fresh preferred cache must avoid the network")
            )
        ),
    )

    result = fetch_tencent_daily_bars_sync(
        "000977",
        start_date="20260501",
        end_date="20260729",
        min_rows=60,
        now=now,
        db_factory=Database,
        prefer_cache=True,
    )

    assert result["ok"] is True
    assert result["source"] == "mongo.candidate_technical_history_cache"
    assert result["freshness"] == "cached_fresh"
    assert result["degraded"] is True
    assert result["cache_usage"] == "preferred"
    assert result["cache_age_seconds"] == 900.0
    assert result["provider_errors"] == []


def test_fetch_tencent_daily_bars_rejects_stale_cache(monkeypatch):
    now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
    bars = [
        {
            "date": (datetime(2026, 5, 29) + timedelta(days=index)).date().isoformat(),
            "open": 10.0,
            "close": 10.1,
            "high": 10.2,
            "low": 9.9,
        }
        for index in range(60)
    ]

    class Collection:
        def find_one(self, query):
            return {
                "_id": "000977:qfq",
                "source": "tencent",
                "checked_at": now - timedelta(hours=73),
                "bars": bars,
            }

    class Database:
        def __getitem__(self, name):
            return Collection()

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            stock_zh_a_hist_tx=lambda **kwargs: (_ for _ in ()).throw(
                ConnectionError("upstream disconnected")
            )
        ),
    )

    result = fetch_tencent_daily_bars_sync(
        "000977",
        start_date="20260501",
        end_date="20260728",
        min_rows=60,
        now=now,
        db_factory=Database,
        sleeper=lambda _seconds: None,
    )

    assert result["ok"] is False
    assert result["status"] == "fetch_error"
    assert result["freshness"] == "unavailable"
    assert result["degraded"] is False
    assert result["bars"] == []


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
