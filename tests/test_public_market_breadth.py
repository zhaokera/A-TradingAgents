import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.services.public_market_breadth as public_breadth_module
from app.services.public_market_breadth import (
    _normalize_sina_snapshot,
    _parse_sina_anchor_response,
    _worker_main,
    fetch_sina_public_market_breadth,
)


EXPECTED_COUNTS = {"total": 500, "sh": 200, "sz": 200, "bj": 100}


def _sina_rows_by_exchange(
    sh_count: int,
    sz_count: int,
    bj_count: int,
    *,
    timestamp: str = "13:30:00",
):
    codes = (
        [f"{600000 + index:06d}" for index in range(sh_count)]
        + [f"{index + 1:06d}" for index in range(sz_count)]
        + [f"{430000 + index:06d}" for index in range(bj_count)]
    )
    midpoint = len(codes) // 2
    return [
        {
            "代码": code,
            "名称": f"样本{index}",
            "最新价": 10.0 + index / 100,
            "涨跌幅": 1.0 if index < midpoint else -1.0,
            "成交额": 1_000_000.0 + index,
            "时间戳": timestamp,
        }
        for index, code in enumerate(codes)
    ]


def _sina_rows(count=500, *, timestamp="13:30:00"):
    sh_count = count * 2 // 5
    sz_count = count * 2 // 5
    return _sina_rows_by_exchange(
        sh_count,
        sz_count,
        count - sh_count - sz_count,
        timestamp=timestamp,
    )


def _anchor(*, trade_date="2026-07-15", provider_time="13:35:00"):
    return {"trade_date": trade_date, "provider_time": provider_time}


def _now(*, hour=13, minute=35):
    return datetime(2026, 7, 15, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_normalize_sina_public_snapshot_proves_complete_exchange_coverage():
    result = _normalize_sina_snapshot(
        _sina_rows(),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "ok"
    assert result["source"] == "akshare.sina.stock_zh_a_spot"
    assert result["provider_expected_count"] == 500
    assert result["provider_expected_exchange_counts"] == {
        "sh": 200,
        "sz": 200,
        "bj": 100,
    }
    assert result["raw_row_count"] == 500
    assert result["unique_row_count"] == 500
    assert result["universe_size"] == 500
    assert result["exchange_counts"] == {"sh": 200, "sz": 200, "bj": 100}
    assert result["total_coverage_ratio"] == 1.0
    assert result["exchange_coverage_ratio"] == {"sh": 1.0, "sz": 1.0, "bj": 1.0}
    assert result["benchmark_trade_date"] == "2026-07-15"
    assert result["provider_trade_date"] == "2026-07-15"
    assert result["provider_time"] == "13:30:00"
    assert result["duplicate_count"] == 0
    assert result["excluded_stale_count"] == 0
    assert result["rows"][0] == {
        "code": "600000",
        "name": "样本0",
        "exchange": "sh",
        "close": 10.0,
        "pct_chg": 1.0,
        "amount": 1_000_000.0,
        "trade_date": "2026-07-15",
        "provider_time": "13:30:00",
    }


def test_normalize_sina_public_snapshot_accepts_coverage_exactly_at_95_percent():
    result = _normalize_sina_snapshot(
        _sina_rows_by_exchange(190, 190, 190),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts={"total": 600, "sh": 200, "sz": 200, "bj": 200},
        now=_now(),
    )

    assert result["status"] == "ok"
    assert result["total_coverage_ratio"] == 0.95
    assert result["exchange_coverage_ratio"] == {"sh": 0.95, "sz": 0.95, "bj": 0.95}
    assert result["unique_row_count"] == 570


def test_normalize_sina_public_snapshot_rejects_total_coverage_below_95_percent():
    result = _normalize_sina_snapshot(
        _sina_rows_by_exchange(190, 190, 189),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts={"total": 600, "sh": 200, "sz": 200, "bj": 200},
        now=_now(),
    )

    assert result["status"] == "public_snapshot_coverage_incomplete"
    assert result["total_coverage_ratio"] < 0.95
    assert result["rows"] == []


def test_normalize_sina_public_snapshot_rejects_one_exchange_below_95_percent():
    result = _normalize_sina_snapshot(
        _sina_rows_by_exchange(211, 200, 189),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts={"total": 600, "sh": 200, "sz": 200, "bj": 200},
        now=_now(),
    )

    assert result["status"] == "public_snapshot_coverage_incomplete"
    assert result["total_coverage_ratio"] == 1.0
    assert result["exchange_coverage_ratio"]["bj"] < 0.95
    assert result["rows"] == []


def test_normalize_sina_public_snapshot_rejects_missing_exchange():
    result = _normalize_sina_snapshot(
        _sina_rows_by_exchange(300, 300, 0),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts={"total": 600, "sh": 200, "sz": 200, "bj": 200},
        now=_now(),
    )

    assert result["status"] == "public_snapshot_coverage_incomplete"
    assert result["exchange_counts"]["bj"] == 0
    assert result["exchange_coverage_ratio"]["bj"] == 0.0


def test_normalize_sina_public_snapshot_rejects_500_row_truncation():
    result = _normalize_sina_snapshot(
        _sina_rows(),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts={
            "total": 5527,
            "sh": 2307,
            "sz": 2893,
            "bj": 327,
        },
        now=_now(),
    )

    assert result["unique_row_count"] == 500
    assert result["status"] == "public_snapshot_coverage_incomplete"
    assert result["total_coverage_ratio"] < 0.95
    assert result["rows"] == []


def test_normalize_sina_public_snapshot_requires_at_least_500_unique_rows():
    result = _normalize_sina_snapshot(
        _sina_rows(499),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "public_breadth_universe_too_small"
    assert result["universe_size"] == 499
    assert result["minimum_universe_size"] == 500


def test_normalize_sina_public_snapshot_requires_positive_finite_close_and_amount():
    rows = _sina_rows_by_exchange(202, 200, 100)
    rows[0]["最新价"] = 0
    rows[1]["成交额"] = float("inf")
    rows[2]["涨跌幅"] = -2.0
    rows[3]["涨跌幅"] = 0.0
    rows[4]["涨跌幅"] = 2.0

    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "ok"
    assert result["raw_row_count"] == 502
    assert result["unique_row_count"] == 500
    rows_by_code = {row["code"]: row for row in result["rows"]}
    assert "600000" not in rows_by_code
    assert "600001" not in rows_by_code
    assert rows_by_code["600002"]["pct_chg"] == -2.0
    assert rows_by_code["600003"]["pct_chg"] == 0.0
    assert rows_by_code["600004"]["pct_chg"] == 2.0


def test_normalize_sina_public_snapshot_excludes_row_with_out_of_range_numeric_timestamp():
    rows = _sina_rows_by_exchange(201, 200, 100)
    rows[0]["时间戳"] = 1e308

    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "ok"
    assert result["raw_row_count"] == 501
    assert result["unique_row_count"] == 500
    assert "600000" not in {row["code"] for row in result["rows"]}


def test_normalize_sina_public_breadth_rejects_time_only_rows_without_anchor():
    result = _normalize_sina_snapshot(
        _sina_rows(),
        benchmark_trade_date="2026-07-15",
        provider_anchor=None,
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "public_breadth_trade_date_unverifiable"
    assert result["rows"] == []


def test_normalize_sina_public_breadth_rejects_mismatched_anchor_date():
    result = _normalize_sina_snapshot(
        _sina_rows(),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(trade_date="2026-07-14"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "public_breadth_trade_date_mismatch"
    assert result["provider_trade_date"] == "2026-07-14"
    assert result["rows"] == []


def test_normalize_sina_public_breadth_requires_unique_fresh_codes():
    duplicate_rows = [
        {
            "代码": "600000",
            "名称": "重复样本",
            "最新价": 10.0,
            "涨跌幅": 1.0,
            "成交额": 1_000_000.0,
            "时间戳": "13:30:00",
        }
        for _ in range(500)
    ]
    stale_rows = _sina_rows(499, timestamp="2026-07-14 15:00:00") + [
        {
            "代码": "601999",
            "名称": "当前样本",
            "最新价": 10.0,
            "涨跌幅": 1.0,
            "成交额": 1_000_000.0,
            "时间戳": "2026-07-15 13:30:00",
        }
    ]

    duplicate_result = _normalize_sina_snapshot(
        duplicate_rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )
    stale_result = _normalize_sina_snapshot(
        stale_rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert duplicate_result["status"] == "public_breadth_universe_too_small"
    assert duplicate_result["universe_size"] == 1
    assert duplicate_result["duplicate_count"] == 499
    assert stale_result["status"] == "public_breadth_universe_too_small"
    assert stale_result["universe_size"] == 1
    assert stale_result["excluded_stale_count"] == 499


def test_normalize_sina_public_breadth_keeps_same_day_illiquid_quotes():
    rows = _sina_rows(500, timestamp="10:00:00")
    rows[-1]["时间戳"] = "13:30:00"

    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "ok"
    assert result["universe_size"] == 500
    assert result["rows"][0]["provider_time"] == "10:00:00"


def test_normalize_sina_public_breadth_excludes_isolated_future_time_only_row():
    rows = _sina_rows_by_exchange(201, 200, 100, timestamp="09:35:00")
    rows[0]["时间戳"] = "15:00:00"

    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="09:35:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=9, minute=35),
    )

    assert result["status"] == "ok"
    assert result["raw_row_count"] == 501
    assert result["unique_row_count"] == 500
    assert result["excluded_future_time_count"] == 1
    assert "600000" not in {row["code"] for row in result["rows"]}


def test_normalize_sina_public_breadth_accepts_same_day_post_close_snapshot():
    result = _normalize_sina_snapshot(
        _sina_rows(500, timestamp="15:36:00"),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="15:57:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=15, minute=57),
    )

    assert result["status"] == "ok"
    assert result["provider_time"] == "15:36:00"


@pytest.mark.parametrize(
    ("provider_time", "expected_status"),
    [
        ("14:55:00", "ok"),
        ("14:54:59", "public_breadth_provider_time_stale"),
    ],
)
def test_normalize_sina_public_breadth_uses_post_close_threshold_at_15_00(
    provider_time,
    expected_status,
):
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp=provider_time),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="15:00:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=15, minute=0),
    )

    assert result["status"] == expected_status


@pytest.mark.parametrize(
    ("provider_time", "expected_status"),
    [
        ("09:40:00", "ok"),
        ("09:39:59", "public_breadth_provider_time_stale"),
        ("10:02:00", "ok"),
        ("10:02:01", "public_breadth_provider_time_stale"),
    ],
)
def test_normalize_sina_public_breadth_intraday_time_tolerance_boundaries(
    provider_time,
    expected_status,
):
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp=provider_time),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="10:00:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=10, minute=0),
    )

    assert result["status"] == expected_status


def test_normalize_sina_public_breadth_accepts_delayed_midday_provider_update():
    rows = _sina_rows(timestamp="11:30:00")
    rows[-1]["时间戳"] = "11:36:02"

    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="11:36:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=12, minute=19),
    )

    assert result["status"] == "ok"
    assert result["provider_time"] == "11:36:02"
    assert result["provider_time_semantics"] == "provider_snapshot_update_time"
    assert result["exchange_trade_time_verified"] is False


def test_normalize_sina_public_breadth_rejects_future_midday_provider_update():
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp="12:22:01"),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="12:22:01"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(hour=12, minute=19),
    )

    assert result["status"] == "public_breadth_provider_time_stale"
    assert result["rows"] == []


@pytest.mark.parametrize(
    ("benchmark_trade_date", "local_now"),
    [
        (
            "2026-07-15",
            datetime(2026, 7, 16, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            "2026-07-17",
            datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    ],
)
def test_normalize_sina_public_breadth_accepts_completed_prior_trade_date_snapshot(
    benchmark_trade_date,
    local_now,
):
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp="15:00:00"),
        benchmark_trade_date=benchmark_trade_date,
        provider_anchor=_anchor(
            trade_date=benchmark_trade_date,
            provider_time="15:00:00",
        ),
        provider_expected_counts=EXPECTED_COUNTS,
        now=local_now,
    )

    assert result["status"] == "ok"
    assert result["provider_trade_date"] == benchmark_trade_date
    assert result["provider_time"] == "15:00:00"


def test_normalize_sina_public_breadth_rejects_incomplete_prior_trade_date_snapshot():
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp="14:54:59"),
        benchmark_trade_date="2026-07-15",
        provider_anchor=_anchor(provider_time="14:54:59"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=datetime(2026, 7, 16, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "public_breadth_provider_time_stale"
    assert result["rows"] == []


def test_normalize_sina_public_breadth_rejects_future_benchmark_trade_date():
    result = _normalize_sina_snapshot(
        _sina_rows(timestamp="13:30:00"),
        benchmark_trade_date="2026-07-16",
        provider_anchor=_anchor(trade_date="2026-07-16", provider_time="13:30:00"),
        provider_expected_counts=EXPECTED_COUNTS,
        now=_now(),
    )

    assert result["status"] == "public_breadth_trade_date_in_future"
    assert result["benchmark_trade_date"] == "2026-07-16"
    assert result["local_trade_date"] == "2026-07-15"
    assert result["rows"] == []


def test_parse_sina_anchor_response_extracts_provider_date_and_time():
    fields = ["上证指数"] + ["0"] * 29 + ["2026-07-15", "14:40:00", "00", ""]
    response = f'var hq_str_sh000001="{",".join(fields)}";'.encode("gbk")

    assert _parse_sina_anchor_response(response) == {
        "trade_date": "2026-07-15",
        "provider_time": "14:40:00",
        "symbol": "sh000001",
    }


@pytest.mark.parametrize("value", [1e308, -1e308])
def test_parse_provider_timestamp_rejects_out_of_range_finite_numbers(value):
    assert public_breadth_module._parse_provider_timestamp(value) == (None, None)


@pytest.mark.parametrize(
    ("code", "expected_exchange"),
    [
        ("600000", "sh"),
        ("000001", "sz"),
        ("300001", "sz"),
        ("430001", "bj"),
        ("830001", "bj"),
        ("870001", "bj"),
        ("880001", "bj"),
        ("920001", "bj"),
        ("500001", None),
        ("800001", None),
        ("930001", None),
    ],
)
def test_exchange_for_code_supports_only_approved_a_share_prefixes(
    code,
    expected_exchange,
):
    assert public_breadth_module._exchange_for_code(code) == expected_exchange


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'  "5527"\n', 5527),
        (" 2307 ", 2307),
    ],
)
def test_parse_sina_expected_count_accepts_quotes_and_whitespace(payload, expected):
    assert public_breadth_module._parse_sina_expected_count(payload) == expected


@pytest.mark.parametrize("payload", [b"", '""', "abc", "1.5", "0", "-1"])
def test_parse_sina_expected_count_rejects_invalid_or_non_positive_values(payload):
    with pytest.raises(ValueError, match="invalid Sina expected count"):
        public_breadth_module._parse_sina_expected_count(payload)


def test_fetch_sina_spot_page_maps_only_required_provider_fields(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "trade": "12.34",
                    "changepercent": 1.25,
                    "amount": 123_456_789,
                    "ticktime": "15:00:00",
                    "unknown": "must not leak",
                }
            ]

    def fake_get(url, *, params, headers, timeout):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    rows = public_breadth_module._fetch_sina_spot_page(3)

    assert rows == [
        {
            "代码": "000001",
            "名称": "平安银行",
            "最新价": "12.34",
            "涨跌幅": 1.25,
            "成交额": 123_456_789,
            "时间戳": "15:00:00",
        }
    ]
    assert calls == [
        {
            "url": public_breadth_module.SINA_SPOT_URL,
            "params": {
                "page": 3,
                "num": public_breadth_module.SINA_SPOT_PAGE_SIZE,
                "sort": "symbol",
                "asc": "1",
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
            "headers": {"Referer": "https://finance.sina.com.cn/"},
            "timeout": public_breadth_module.SINA_REQUEST_TIMEOUT_SECONDS,
        }
    ]


def test_load_sina_spot_fetches_all_pages_and_restores_provider_order(
    monkeypatch,
):
    calls = []
    page_size = public_breadth_module.SINA_SPOT_PAGE_SIZE
    expected_count = page_size * 2 + 5

    monkeypatch.setattr(
        public_breadth_module,
        "_fetch_sina_expected_count",
        lambda node: expected_count if node == "hs_a" else None,
    )

    def fake_page(page):
        calls.append(page)
        count = page_size if page < 3 else 5
        return [
            {
                "代码": f"{page}{index:05d}",
                "名称": f"第{page}页",
                "最新价": 10,
                "涨跌幅": 0,
                "成交额": 1,
                "时间戳": "15:00:00",
            }
            for index in range(count)
        ]

    monkeypatch.setattr(
        public_breadth_module,
        "_fetch_sina_spot_page",
        fake_page,
    )

    rows = public_breadth_module._load_sina_spot()

    assert sorted(calls) == [1, 2, 3]
    assert len(rows) == expected_count
    assert rows[0]["代码"] == "100000"
    assert rows[page_size]["代码"] == "200000"
    assert rows[-1]["代码"] == "300004"


def test_load_sina_spot_fails_closed_when_any_page_is_incomplete(monkeypatch):
    page_size = public_breadth_module.SINA_SPOT_PAGE_SIZE
    monkeypatch.setattr(
        public_breadth_module,
        "_fetch_sina_expected_count",
        lambda _node: page_size + 1,
    )
    monkeypatch.setattr(
        public_breadth_module,
        "_fetch_sina_spot_page",
        lambda page: [{}] if page == 2 else [{}] * (page_size - 1),
    )

    with pytest.raises(ValueError, match="incomplete Sina spot page"):
        public_breadth_module._load_sina_spot()


def test_load_sina_expected_counts_uses_total_shanghai_shenzhen_and_derives_beijing(
    monkeypatch,
):
    calls = []
    provider_counts = {"hs_a": 5527, "sh_a": 2307, "sz_a": 2893}

    def fake_fetch_count(node):
        calls.append(node)
        return provider_counts[node]

    monkeypatch.setattr(public_breadth_module, "_fetch_sina_expected_count", fake_fetch_count)

    assert public_breadth_module._load_sina_expected_counts() == {
        "total": 5527,
        "sh": 2307,
        "sz": 2893,
        "bj": 327,
    }
    assert calls == ["hs_a", "sh_a", "sz_a"]


def test_worker_fails_closed_when_expected_count_provider_is_unavailable(monkeypatch, capsys):
    snapshot_calls = []
    monkeypatch.setattr(public_breadth_module, "_load_sina_anchor", _anchor)
    monkeypatch.setattr(
        public_breadth_module,
        "_load_sina_expected_counts",
        lambda: (_ for _ in ()).throw(TimeoutError("count timeout")),
    )
    monkeypatch.setattr(
        public_breadth_module,
        "_load_sina_spot",
        lambda: snapshot_calls.append(True),
    )

    exit_code = _worker_main(
        [
            "--worker",
            "--benchmark-trade-date",
            "2026-07-15",
            "--now",
            _now().isoformat(),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert exit_code == 0
    assert payload["status"] == "public_snapshot_expected_counts_unavailable"
    assert payload["error_type"] == "TimeoutError"
    assert payload["rows"] == []
    assert snapshot_calls == []


def test_worker_fails_closed_when_derived_beijing_count_is_non_positive(monkeypatch, capsys):
    provider_counts = {"hs_a": 500, "sh_a": 300, "sz_a": 250}
    snapshot_calls = []
    monkeypatch.setattr(public_breadth_module, "_load_sina_anchor", _anchor)
    monkeypatch.setattr(
        public_breadth_module,
        "_fetch_sina_expected_count",
        lambda node: provider_counts[node],
    )
    monkeypatch.setattr(
        public_breadth_module,
        "_load_sina_spot",
        lambda: snapshot_calls.append(True),
    )

    _worker_main(
        [
            "--worker",
            "--benchmark-trade-date",
            "2026-07-15",
            "--now",
            _now().isoformat(),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["status"] == "public_snapshot_expected_counts_unavailable"
    assert payload["error_type"] == "ValueError"
    assert payload["rows"] == []
    assert snapshot_calls == []


def test_worker_reads_anchor_expected_counts_and_snapshot_once(monkeypatch, capsys):
    calls = {"anchor": 0, "counts": 0, "snapshot": 0}

    def fake_anchor():
        calls["anchor"] += 1
        return _anchor()

    def fake_counts():
        calls["counts"] += 1
        return EXPECTED_COUNTS

    def fake_snapshot():
        calls["snapshot"] += 1
        return _sina_rows()

    monkeypatch.setattr(public_breadth_module, "_load_sina_anchor", fake_anchor)
    monkeypatch.setattr(public_breadth_module, "_load_sina_expected_counts", fake_counts)
    monkeypatch.setattr(public_breadth_module, "_load_sina_spot", fake_snapshot)

    _worker_main(
        [
            "--worker",
            "--benchmark-trade-date",
            "2026-07-15",
            "--now",
            _now().isoformat(),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["status"] == "ok"
    assert calls == {"anchor": 1, "counts": 1, "snapshot": 1}


def test_fetch_sina_public_breadth_compatibility_entry_delegates_once(monkeypatch):
    assert hasattr(public_breadth_module, "fetch_sina_public_market_snapshot")
    calls = []

    def fake_fetch_snapshot(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "rows": []}

    monkeypatch.setattr(
        public_breadth_module,
        "fetch_sina_public_market_snapshot",
        fake_fetch_snapshot,
    )

    result = fetch_sina_public_market_breadth(
        benchmark_trade_date="2026-07-15",
        timeout_seconds=3.0,
        now=_now(),
    )

    assert result == {"status": "ok", "rows": []}
    assert calls == [
        {
            "benchmark_trade_date": "2026-07-15",
            "timeout_seconds": 3.0,
            "now": _now(),
        }
    ]


def test_fetch_sina_public_market_snapshot_returns_stable_failure_when_spawn_fails(
    monkeypatch,
):
    def fail_to_spawn(*_args, **_kwargs):
        raise OSError("worker spawn failed")

    monkeypatch.setattr(public_breadth_module.subprocess, "run", fail_to_spawn)

    result = public_breadth_module.fetch_sina_public_market_snapshot(
        benchmark_trade_date="2026-07-15",
        now=_now(),
    )

    assert result == {
        "status": "public_breadth_fetch_failed",
        "source": "akshare.sina.stock_zh_a_spot",
        "error_type": "OSError",
        "rows": [],
    }


def test_fetch_sina_public_breadth_terminates_timed_out_worker_process(monkeypatch):
    monkeypatch.setattr(
        public_breadth_module,
        "_build_worker_command",
        lambda **_kwargs: [sys.executable, "-c", "import time; time.sleep(2)"],
    )

    started = time.monotonic()
    result = fetch_sina_public_market_breadth(
        benchmark_trade_date="2026-07-15",
        timeout_seconds=0.05,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result["status"] == "public_breadth_timeout"
    assert result["timeout_seconds"] == 0.05
    assert result["rows"] == []
