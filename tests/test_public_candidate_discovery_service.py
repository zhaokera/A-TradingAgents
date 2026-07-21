import json
import logging
import math
from datetime import date, datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

import app.services.public_candidate_discovery_service as discovery_module
from app.services.a_share_market_regime import MIN_BREADTH_UNIVERSE_SIZE
from app.services.public_market_breadth import _normalize_sina_snapshot
from app.services.public_candidate_discovery_service import (
    PublicCandidateDiscoveryInputError,
    midrank_percentiles,
    rank_public_candidate_universe,
)
from app.services.tencent_quote_service import parse_tencent_quote_batch_payload


BENCHMARK_TRADE_DATE = "2026-07-15"
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _expected_exchange(code):
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return None


def _row(
    code,
    *,
    name=None,
    exchange=None,
    close=10.0,
    pct_chg=1.5,
    amount=100_000_000.0,
    trade_date=BENCHMARK_TRADE_DATE,
):
    return {
        "code": code,
        "name": name or f"样本{code}",
        "exchange": exchange if exchange is not None else _expected_exchange(code),
        "close": close,
        "pct_chg": pct_chg,
        "amount": amount,
        "trade_date": trade_date,
    }


def _by_code(result):
    return {item["code"]: item for item in result["definitions"]}


def _task_3_definitions(count):
    result = rank_public_candidate_universe(
        [
            _row(
                f"600{index:03d}",
                pct_chg=1.5,
                amount=200_000_000.0 + index,
            )
            for index in range(count)
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )
    return result["definitions"]


def _quote(definition, **overrides):
    code = definition["code"]
    quote = {
        "code": code,
        "provider_symbol": f"{definition['exchange']}{code}",
        "source": "tencent",
        "trade_at": "2026-07-15T09:59:00+08:00",
        "close": 10.0,
        "pct_chg": 1.5,
        "amount": 500_000_000.0,
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "amplitude": 2.0,
        "circ_mv": 2_000_000_000.0,
        "total_mv": 4_000_000_000.0,
    }
    quote.update(overrides)
    return quote


def _tencent_quote_payload(
    definition,
    *,
    price=10.0,
    amount=500_000_000.0,
    limit_up=11.0,
    limit_down=9.0,
    provider_symbol=None,
    payload_code=None,
):
    code = definition["code"]
    fields = ["0"] * 50
    fields[1] = definition.get("name", f"样本{code}")
    fields[2] = payload_code or code
    fields[3] = str(price)
    fields[4] = "9.9"
    fields[5] = "9.95"
    fields[6] = "1000000"
    fields[30] = "20260715095900"
    fields[31] = "0.1"
    fields[32] = "1.5"
    fields[33] = "10.1"
    fields[34] = "9.8"
    fields[35] = f"{price}/1000000/{amount}"
    fields[37] = str(amount / 10_000)
    fields[38] = "1.0"
    fields[39] = "12.3"
    fields[43] = "2.0"
    fields[44] = "20.0"
    fields[45] = "40.0"
    fields[46] = "1.2"
    fields[47] = str(limit_up)
    fields[48] = str(limit_down)
    fields[49] = "1.0"
    symbol = provider_symbol or f"{definition['exchange']}{code}"
    return f'v_{symbol}="{"~".join(fields)}";'


def _snapshot(
    rows,
    *,
    minimum_size=MIN_BREADTH_UNIVERSE_SIZE,
    **overrides,
):
    snapshot_rows = list(rows)
    assert len(snapshot_rows) <= minimum_size
    used_codes = {item.get("code") for item in snapshot_rows}
    filler_bases = {"sh": 601000, "sz": 1000, "bj": 830000}
    filler_offsets = {exchange: 0 for exchange in filler_bases}
    while len(snapshot_rows) < minimum_size:
        for exchange in ("sh", "sz", "bj"):
            if len(snapshot_rows) >= minimum_size:
                break
            while True:
                code = f"{filler_bases[exchange] + filler_offsets[exchange]:06d}"
                filler_offsets[exchange] += 1
                if code not in used_codes:
                    used_codes.add(code)
                    break
            snapshot_rows.append(
                _row(
                    code,
                    name=f"覆盖占位{exchange}",
                    exchange=exchange,
                    amount=1.0,
                )
            )

    exchange_counts = {"sh": 0, "sz": 0, "bj": 0}
    for item in snapshot_rows:
        exchange = item.get("exchange")
        if exchange in exchange_counts:
            exchange_counts[exchange] += 1
    snapshot = {
        "status": "ok",
        "source": "akshare.sina.stock_zh_a_spot",
        "benchmark_trade_date": BENCHMARK_TRADE_DATE,
        "provider_trade_date": BENCHMARK_TRADE_DATE,
        "provider_expected_count": len(snapshot_rows),
        "provider_expected_exchange_counts": dict(exchange_counts),
        "raw_row_count": len(snapshot_rows),
        "unique_row_count": len(snapshot_rows),
        "universe_size": len(snapshot_rows),
        "exchange_counts": dict(exchange_counts),
        "total_coverage_ratio": 1.0,
        "exchange_coverage_ratio": {
            exchange: 1.0 if count else 0.0
            for exchange, count in exchange_counts.items()
        },
        "rows": snapshot_rows,
    }
    snapshot.update(overrides)
    return snapshot


@pytest.mark.parametrize("rows", [None, 42])
def test_public_ranking_wraps_non_iterable_snapshot_rows(rows):
    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^snapshot_rows_unavailable$",
    ) as caught:
        rank_public_candidate_universe(
            rows,
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
        )

    assert isinstance(caught.value.__cause__, TypeError)


@pytest.mark.parametrize("error_type", [TypeError, RuntimeError, OSError])
def test_public_ranking_wraps_errors_raised_during_snapshot_iteration(error_type):
    source_error = error_type("snapshot failed")

    def broken_rows():
        yield _row("600000")
        raise source_error

    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^snapshot_rows_unavailable$",
    ) as caught:
        rank_public_candidate_universe(
            broken_rows(),
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
        )

    assert caught.value.__cause__ is source_error


def test_public_ranking_accepts_an_empty_snapshot_iterable():
    result = rank_public_candidate_universe(
        [],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["eligible_count"] == 0


@pytest.mark.parametrize(
    "benchmark_trade_date",
    [None, "2026-02-31", "2026-07-15junk", " 2026-07-15", ""],
)
def test_public_ranking_rejects_an_invalid_benchmark_trade_date(
    benchmark_trade_date,
):
    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^invalid_benchmark_trade_date$",
    ):
        rank_public_candidate_universe(
            [_row("600000", trade_date=benchmark_trade_date)],
            benchmark_trade_date=benchmark_trade_date,
        )


@pytest.mark.parametrize(
    ("benchmark_trade_date", "expected_trade_date"),
    [
        ("2026-07-15", "2026-07-15"),
        (date(2026, 7, 15), "2026-07-15"),
        (datetime(2026, 7, 15, 9, 30), "2026-07-15"),
    ],
)
def test_public_ranking_accepts_strict_benchmark_date_types(
    benchmark_trade_date,
    expected_trade_date,
):
    result = rank_public_candidate_universe(
        [_row("600000", trade_date=date(2026, 7, 15))],
        benchmark_trade_date=benchmark_trade_date,
    )

    assert result["status"] == "ok"
    assert result["benchmark_trade_date"] == expected_trade_date
    assert result["definitions"][0]["trade_date"] == expected_trade_date


def test_public_ranking_counts_invalid_row_trade_dates_as_stale_quotes():
    rows = [
        _row("600300", trade_date=None),
        _row("600301", trade_date="2026-02-31"),
        _row("600302", trade_date="2026-07-15junk"),
        _row("600303", trade_date=date(2026, 7, 15)),
        _row("600304", trade_date=datetime(2026, 7, 15, 9, 30)),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["eligible_count"] == 2
    assert result["rejection_counts"] == {"stale_quote": 3}
    assert {item["code"] for item in result["definitions"]} == {
        "600303",
        "600304",
    }


@pytest.mark.parametrize(
    "limit",
    [0, -1, True, False, 40.0, None, float("nan"), float("inf"), "40"],
)
def test_public_ranking_rejects_invalid_limits_before_ranking(limit):
    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^invalid_limit$",
    ):
        rank_public_candidate_universe(
            [],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
            limit=limit,
        )


def test_public_ranking_accepts_a_positive_integer_limit():
    result = rank_public_candidate_universe(
        [_row("600310"), _row("600311", amount=200_000_000.0)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        limit=1,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["definitions"]] == ["600311"]
    assert result["public_preselected_count"] == 1


def test_midrank_percentiles_are_deterministic_and_preserve_input_order():
    assert midrank_percentiles([]) == []
    assert midrank_percentiles([100.0]) == [1.0]
    assert midrank_percentiles([100.0, 200.0, 300.0]) == [0.0, 0.5, 1.0]
    assert midrank_percentiles([100.0, 100.0, 300.0]) == [0.25, 0.25, 1.0]
    assert midrank_percentiles([7.0, 7.0, 7.0]) == [0.5, 0.5, 0.5]
    assert midrank_percentiles([300.0, 100.0, 200.0]) == [1.0, 0.0, 0.5]


def test_public_ranking_supports_all_documented_a_share_code_prefixes():
    codes = [
        "600000",
        "000001",
        "300750",
        "430047",
        "830001",
        "870001",
        "880001",
        "920001",
    ]

    result = rank_public_candidate_universe(
        (_row(code) for code in codes),
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["status"] == "ok"
    assert result["eligible_count"] == len(codes)
    assert {item["code"] for item in result["definitions"]} == set(codes)
    assert result["rejection_counts"] == {}


def test_public_ranking_rejects_invalid_codes_and_exchange_mismatches():
    rows = [
        _row("100001", exchange="sh"),
        _row("600001", exchange="sz"),
        _row("000002", exchange="sh"),
        _row("430048", exchange="sz"),
        _row("600002", exchange=""),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["eligible_count"] == 0
    assert result["rejection_counts"] == {
        "exchange_mismatch": 4,
        "unsupported_code": 1,
    }


def test_public_ranking_filters_risk_rows_and_normalizes_task_1_numbers():
    missing_amount = _row("600021")
    missing_amount.pop("amount")
    legacy_aliases_only = {
        "code": "600022",
        "name": "旧字段样本",
        "exchange": "sh",
        "price": 10.0,
        "pct_change": 1.0,
        "amount": 200_000_000.0,
        "trade_date": BENCHMARK_TRADE_DATE,
    }
    rows = [
        _row(
            "600010",
            close="12.34",
            pct_chg="1.5",
            amount="100000000",
        ),
        _row("600011", name="*st风险样本"),
        _row("600012", name="退市风险样本"),
        _row("600013", amount="99999999.99"),
        _row("600014", pct_chg="3.0001"),
        _row("600015", pct_chg="-1.5001"),
        _row("600016", trade_date="2026-07-14"),
        _row("600017", close=0),
        _row("600018", close="not-a-number"),
        _row("600019", amount=float("inf")),
        _row("600020", pct_chg=float("nan")),
        missing_amount,
        legacy_aliases_only,
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["status"] == "ok"
    assert result["eligible_count"] == 1
    assert result["rejection_counts"] == {
        "below_min_amount": 1,
        "invalid_quote": 6,
        "outside_move_window": 2,
        "special_treatment": 2,
        "stale_quote": 1,
    }
    definition = result["definitions"][0]
    assert {
        "code",
        "name",
        "exchange",
        "price",
        "pct_change",
        "amount",
        "one_lot_amount",
        "bucket",
        "amount_percentile",
        "move_quality",
        "public_score",
        "trade_date",
    } <= definition.keys()
    assert definition["code"] == "600010"
    assert definition["price"] == 12.34
    assert definition["pct_change"] == 1.5
    assert definition["amount"] == 100_000_000.0
    assert definition["one_lot_amount"] == 1234.0


def test_public_ranking_rejects_one_lot_overflow_and_returns_strict_json():
    result = rank_public_candidate_universe(
        [
            _row("600023", close=10.0),
            _row("600024", close=1e308),
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["definitions"]] == ["600023"]
    assert result["rejection_counts"] == {"invalid_quote": 1}
    assert json.dumps(result, allow_nan=False)


def test_public_ranking_rejects_every_same_day_duplicate_code_once():
    rows = [
        _row("600030", amount=100_000_000.0),
        _row("600030", amount=300_000_000.0),
        _row("600031", amount=200_000_000.0),
        _row("600031", amount=400_000_000.0, trade_date="2026-07-14"),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert [item["code"] for item in result["definitions"]] == ["600031"]
    assert result["eligible_count"] == 1
    assert result["rejection_counts"] == {
        "duplicate_code": 1,
        "stale_quote": 1,
    }


def test_public_score_uses_full_eligible_population_and_piecewise_move_quality():
    rows = [
        _row("600040", pct_chg=1.5, amount=100_000_000.0),
        _row("600041", pct_chg=0.3, amount=100_000_000.0),
        _row("600042", pct_chg=3.0, amount=100_000_000.0),
        _row("000040", pct_chg=-0.5, amount=100_000_000.0),
        _row("000041", pct_chg=-1.5, amount=100_000_000.0),
        _row("000042", pct_chg=0.299, amount=100_000_000.0),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )
    definitions = _by_code(result)

    assert all(item["amount_percentile"] == 0.5 for item in definitions.values())
    assert definitions["600040"]["bucket"] == "strength"
    assert definitions["600040"]["move_quality"] == 1.0
    assert definitions["600041"]["move_quality"] == 0.0
    assert definitions["600042"]["move_quality"] == 0.0
    assert definitions["000040"]["bucket"] == "pullback"
    assert definitions["000040"]["move_quality"] == 1.0
    assert definitions["000041"]["move_quality"] == 0.0
    assert definitions["000042"]["move_quality"] == pytest.approx(0.00125)
    assert definitions["600040"]["public_score"] == pytest.approx(0.675)
    assert definitions["600041"]["public_score"] == pytest.approx(0.325)


def test_public_ranking_prioritizes_technology_new_productivity_objective():
    result = rank_public_candidate_universe(
        [
            _row(
                "600690",
                name="海尔智家",
                pct_chg=1.5,
                amount=900_000_000.0,
            ),
            _row(
                "601899",
                name="紫金矿业",
                pct_chg=1.0,
                amount=300_000_000.0,
            ),
            _row(
                "600406",
                name="国电南瑞",
                pct_chg=0.3,
                amount=100_000_000.0,
            ),
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        limit=2,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600406",
        "601899",
    ]
    assert result["definitions"][0]["objective_tier"] == "core"
    assert result["definitions"][1]["objective_tier"] == "related"


def test_amount_percentiles_exclude_rejected_rows_and_precede_bucket_quotas():
    rows = [
        _row("600050", amount=100_000_000.0),
        _row("600051", amount=200_000_000.0),
        _row("600052", amount=300_000_000.0),
        _row("600053", amount=900_000_000.0, pct_chg=3.1),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        limit=2,
    )
    definitions = _by_code(result)

    assert result["eligible_count"] == 3
    assert result["rejection_counts"] == {"outside_move_window": 1}
    assert definitions["600052"]["amount_percentile"] == 1.0
    assert definitions["600051"]["amount_percentile"] == 0.5
    assert "600050" not in definitions


def test_public_ranking_uses_120_40_quota_and_caps_explicit_limit_at_160():
    strength_rows = [
        _row(
            f"600{index:03d}",
            pct_chg=1.5,
            amount=200_000_000.0 + index,
        )
        for index in range(130)
    ]
    pullback_rows = [
        _row(
            f"000{index + 1:03d}",
            pct_chg=-0.5,
            amount=150_000_000.0 + index,
        )
        for index in range(70)
    ]

    result = rank_public_candidate_universe(
        strength_rows + pullback_rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        limit=200,
    )

    assert len(result["definitions"]) == 160
    assert result["selected_bucket_counts"] == {"pullback": 40, "strength": 120}
    assert result["eligible_bucket_counts"] == {"pullback": 70, "strength": 130}


@pytest.mark.parametrize(
    (
        "strength_count",
        "pullback_count",
        "expected_selected_bucket_counts",
    ),
    [
        (5, 40, {"pullback": 35, "strength": 5}),
        (40, 2, {"pullback": 2, "strength": 38}),
    ],
)
def test_public_ranking_deterministically_backfills_an_underfilled_bucket(
    strength_count,
    pullback_count,
    expected_selected_bucket_counts,
):
    rows = [
        _row(f"600{index:03d}", pct_chg=1.5, amount=300_000_000.0 + index)
        for index in range(strength_count)
    ] + [
        _row(f"000{index + 1:03d}", pct_chg=-0.5, amount=200_000_000.0 + index)
        for index in range(pullback_count)
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert len(result["definitions"]) == 40
    assert result["selected_bucket_counts"] == expected_selected_bucket_counts
    assert len({item["code"] for item in result["definitions"]}) == 40


def test_public_ranking_sort_key_is_score_amount_one_lot_then_code(monkeypatch):
    monkeypatch.setattr(
        discovery_module,
        "midrank_percentiles",
        lambda values: [0.5] * len(values),
    )
    rows = [
        _row("600103", pct_chg=1.5, amount=100_000_000.0, close=12.0),
        _row("600102", pct_chg=1.5, amount=200_000_000.0, close=12.0),
        _row("600101", pct_chg=1.5, amount=200_000_000.0, close=8.0),
        _row("600100", pct_chg=1.5, amount=200_000_000.0, close=8.0),
        _row("600099", pct_chg=0.3, amount=300_000_000.0, close=1.0),
    ]

    result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600100",
        "600101",
        "600102",
        "600103",
        "600099",
    ]


def test_no_eligible_candidates_keeps_the_complete_ranking_dto():
    success = rank_public_candidate_universe(
        [_row("600200")],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )
    empty = rank_public_candidate_universe(
        [_row("600201", amount=99_999_999.0)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )

    required_keys = {
        "status",
        "definitions",
        "benchmark_trade_date",
        "rejection_counts",
        "eligible_count",
        "eligible_bucket_counts",
        "selected_bucket_counts",
        "public_preselected_count",
    }
    assert required_keys <= empty.keys()
    assert set(empty) == set(success)
    assert "selected_count" not in empty
    assert "selected_count" not in success
    assert empty["status"] == "no_eligible_candidates"
    assert empty["definitions"] == []
    assert empty["benchmark_trade_date"] == BENCHMARK_TRADE_DATE
    assert empty["rejection_counts"] == {"below_min_amount": 1}
    assert empty["eligible_count"] == 0
    assert empty["eligible_bucket_counts"] == {"pullback": 0, "strength": 0}
    assert empty["selected_bucket_counts"] == {"pullback": 0, "strength": 0}
    assert empty["public_preselected_count"] == 0
    assert all(math.isfinite(value) for value in midrank_percentiles([1.0, 1.0]))


@pytest.mark.parametrize(
    ("requested_count", "minimum_verified_count"),
    [
        (0, 0),
        (1, 1),
        (20, 20),
        (21, 20),
        (25, 20),
        (40, 32),
    ],
)
def test_tencent_verification_uses_the_dynamic_minimum_coverage_threshold(
    requested_count,
    minimum_verified_count,
):
    definitions = _task_3_definitions(requested_count)

    sufficient = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        [_quote(item) for item in definitions[:minimum_verified_count]],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert sufficient["minimum_verified_count"] == minimum_verified_count
    assert sufficient["tencent_requested_count"] == requested_count
    assert sufficient["tencent_verified_count"] == minimum_verified_count
    assert sufficient["status"] == (
        "no_eligible_candidates" if requested_count == 0 else "ok"
    )
    assert sufficient["rejection_counts"].get("missing_response", 0) == (
        requested_count - minimum_verified_count
    )

    if requested_count:
        insufficient = discovery_module.verify_and_rank_tencent_candidates(
            definitions,
            [_quote(item) for item in definitions[: minimum_verified_count - 1]],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
            now=NOW,
        )

        assert insufficient["status"] == "candidate_discovery_unavailable"
        assert insufficient["tencent_verified_count"] == minimum_verified_count - 1
        assert insufficient["definitions"] == []
        assert insufficient["quote_map"] == {}


@pytest.mark.parametrize("include_parse_status", [False, True])
def test_tencent_verification_accepts_ok_or_legacy_missing_parse_status(
    include_parse_status,
):
    definition = _task_3_definitions(1)[0]
    quote = _quote(definition)
    if include_parse_status:
        quote["parse_status"] = "ok"

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [quote],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "ok"
    assert result["tencent_verified_count"] == 1
    assert result["rejection_counts"] == {}


@pytest.mark.parametrize(
    ("parse_status", "expected_reason"),
    [
        ("invalid_price", "invalid_price"),
        ("empty_payload", "invalid_response"),
        ("malformed_payload", "invalid_response"),
        ("provider_specific_failure", "invalid_response"),
    ],
)
def test_tencent_verification_rejects_non_ok_parse_status_with_stable_reasons(
    parse_status,
    expected_reason,
):
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, parse_status=parse_status)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["quote_map"] == {}
    assert result["rejection_counts"] == {expected_reason: 1}


@pytest.mark.parametrize(
    ("quote_changes", "expected_reason", "expected_verified_count"),
    [
        ({"close": True}, "invalid_price", 0),
        ({"amount": True}, "invalid_amount", 0),
        ({"pct_chg": True}, "invalid_pct_chg", 1),
    ],
)
def test_tencent_verification_rejects_bool_numeric_fields(
    quote_changes,
    expected_reason,
    expected_verified_count,
):
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, **quote_changes)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["tencent_verified_count"] == expected_verified_count
    assert result["definitions"] == []
    assert result["rejection_counts"] == {expected_reason: 1}


def test_numeric_normalization_rejects_bool_but_keeps_integer_zero():
    with pytest.raises(ValueError, match="midrank values must be finite numbers"):
        midrank_percentiles([True])

    assert midrank_percentiles([0, 1]) == [0.0, 1.0]


def test_tencent_verification_rejects_unexpected_duplicate_mismatched_and_stale_rows():
    definitions = _task_3_definitions(8)
    by_code = {item["code"]: item for item in definitions}
    codes = [item["code"] for item in definitions]
    duplicate = _quote(by_code[codes[0]])
    rows = [
        duplicate,
        dict(duplicate, amount=600_000_000.0),
        _quote(by_code[codes[1]], provider_symbol=f"sz{codes[1]}"),
        _quote(by_code[codes[2]], close=0),
        _quote(by_code[codes[3]], amount=float("inf")),
        _quote(by_code[codes[4]], trade_at="2026-07-14T15:00:00+08:00"),
        _quote(by_code[codes[5]], trade_at="2026-07-15T09:54:59+08:00"),
        _quote(by_code[codes[6]], trade_at="2026-07-15T10:02:01+08:00"),
        _quote(by_code[codes[7]]),
        {
            **_quote({"code": "600099", "exchange": "sh"}),
            "code": "600099",
            "provider_symbol": "sh600099",
        },
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 1
    assert result["quote_map"] == {}
    assert result["rejection_counts"] == {
        "code_mismatch": 1,
        "duplicate_code": 1,
        "future_trade_at": 1,
        "invalid_amount": 1,
        "invalid_price": 1,
        "stale_trade_at": 1,
        "trade_date_mismatch": 1,
        "unexpected_code": 1,
    }


@pytest.mark.parametrize(
    ("provider_symbol", "expected_rejections"),
    [
        ("sz600000", {"code_mismatch": 1}),
        ("sh600001", {"missing_response": 1, "unexpected_code": 1}),
        ("600000", {"code_mismatch": 1}),
        (None, {"code_mismatch": 1}),
    ],
)
def test_tencent_verification_requires_definition_and_response_symbol_consistency(
    provider_symbol,
    expected_rejections,
):
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, provider_symbol=provider_symbol)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["quote_map"] == {}
    assert result["rejection_counts"] == expected_rejections


def test_tencent_verification_attributes_extra_self_mismatch_only_as_unexpected():
    definition = _task_3_definitions(1)[0]
    extra = _quote(
        definition,
        code="600099",
        provider_symbol="sz600099",
    )

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition), extra],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "ok"
    assert result["tencent_verified_count"] == 1
    assert result["rejection_counts"] == {"unexpected_code": 1}


def test_tencent_verification_duplicate_group_only_audits_price_and_amount():
    definition = _task_3_definitions(1)[0]
    code = definition["code"]
    rows = [
        _quote(definition, provider_symbol=f"sz{code}"),
        _quote(
            definition,
            provider_symbol="not-a-symbol",
            close=0,
            amount=0,
        ),
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["definitions"] == []
    assert result["rejection_counts"] == {
        "duplicate_code": 1,
        "invalid_amount": 1,
        "invalid_price": 1,
    }


def test_tencent_parser_chain_keeps_invalid_duplicate_for_coverage_audit():
    definition = _task_3_definitions(1)[0]
    payload = "\n".join(
        [
            _tencent_quote_payload(definition),
            _tencent_quote_payload(definition, price=0),
        ]
    )
    rows = parse_tencent_quote_batch_payload(payload)

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["definitions"] == []
    assert result["rejection_counts"] == {
        "duplicate_code": 1,
        "invalid_price": 1,
    }


def test_tencent_envelope_duplicate_with_empty_payload_cannot_pass_coverage():
    definition = _task_3_definitions(1)[0]
    symbol = f"{definition['exchange']}{definition['code']}"
    rows = parse_tencent_quote_batch_payload(
        "\n".join(
            [
                _tencent_quote_payload(definition),
                f'v_{symbol}="";',
            ]
        )
    )

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["definitions"] == []
    assert result["rejection_counts"] == {
        "duplicate_code": 1,
        "invalid_response": 1,
    }


def test_tencent_payload_code_mismatch_is_still_an_envelope_duplicate():
    definition = _task_3_definitions(1)[0]
    rows = parse_tencent_quote_batch_payload(
        "\n".join(
            [
                _tencent_quote_payload(definition),
                _tencent_quote_payload(definition, payload_code="600099"),
            ]
        )
    )

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["rejection_counts"] == {"duplicate_code": 1}


def test_tencent_unique_envelope_rejects_a_different_payload_code_as_mismatch():
    definition = _task_3_definitions(1)[0]
    rows = parse_tencent_quote_batch_payload(
        _tencent_quote_payload(definition, payload_code="600099")
    )

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["tencent_verified_count"] == 0
    assert result["rejection_counts"] == {"code_mismatch": 1}


def test_tencent_parser_chain_applies_real_limit_up_hard_filter():
    definition = _task_3_definitions(1)[0]
    rows = parse_tencent_quote_batch_payload(
        _tencent_quote_payload(
            definition,
            price=199.0,
            limit_up=200.0,
            limit_down=180.0,
        )
    )

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["tencent_verified_count"] == 1
    assert result["definitions"] == []
    assert result["rejection_counts"] == {"near_limit_up": 1}


@pytest.mark.parametrize(
    ("quote_changes", "reason"),
    [
        ({"source": "akshare"}, "invalid_source"),
        ({"trade_at": None}, "missing_trade_at"),
    ],
)
def test_tencent_verification_uses_research_freshness_source_and_time_gates(
    quote_changes,
    reason,
):
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, **quote_changes)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["rejection_counts"] == {reason: 1}


@pytest.mark.parametrize(
    ("quote_changes", "reason"),
    [
        ({"pct_chg": None}, "missing_pct_chg"),
        ({"pct_chg": float("nan")}, "invalid_pct_chg"),
        ({"pct_chg": -1.5001}, "outside_move_window"),
        ({"pct_chg": 3.0001}, "outside_move_window"),
        ({"turnover_rate": None}, "missing_turnover_rate"),
        ({"turnover_rate": float("inf")}, "invalid_turnover_rate"),
        ({"turnover_rate": -0.0001}, "turnover_rate_out_of_range"),
        ({"turnover_rate": 10.0001}, "turnover_rate_out_of_range"),
        ({"amplitude": None}, "missing_amplitude"),
        ({"amplitude": float("nan")}, "invalid_amplitude"),
        ({"amplitude": -0.0001}, "amplitude_out_of_range"),
        ({"amplitude": 8.0001}, "amplitude_out_of_range"),
        ({"total_mv": None}, "missing_total_mv"),
        ({"total_mv": float("inf")}, "invalid_total_mv"),
        ({"total_mv": 1_999_999_999.99}, "below_min_total_mv"),
        ({"circ_mv": None}, "missing_circ_mv"),
        ({"circ_mv": float("nan")}, "invalid_circ_mv"),
        ({"circ_mv": 999_999_999.99}, "below_min_circ_mv"),
        ({"close": 199.0, "limit_up": 200.0}, "near_limit_up"),
        ({"limit_up": 0}, "invalid_limit_up"),
        ({"limit_up": float("nan")}, "invalid_limit_up"),
    ],
)
def test_tencent_hard_filters_reject_each_explicit_failure_reason(
    quote_changes,
    reason,
):
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, **quote_changes)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["tencent_verified_count"] == 1
    assert result["tencent_rank_population_count"] == 0
    assert result["rejection_counts"] == {reason: 1}
    assert set(result["quote_map"]) == {definition["code"]}
    assert json.dumps(result, allow_nan=False)


def test_tencent_hard_filters_accept_all_inclusive_boundaries():
    definitions = _task_3_definitions(8)
    changes = [
        {"pct_chg": -1.5},
        {"pct_chg": 3.0},
        {"turnover_rate": 0.0},
        {"turnover_rate": 10.0},
        {"amplitude": 0.0},
        {"amplitude": 8.0},
        {"total_mv": 2_000_000_000.0, "circ_mv": 1_000_000_000.0},
        {"close": 198.99, "limit_up": 200.0},
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        [_quote(item, **change) for item, change in zip(definitions, changes)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert result["status"] == "ok"
    assert result["tencent_verified_count"] == 8
    assert result["tencent_rank_population_count"] == 8
    assert result["selected_count"] == 8


def test_tencent_volume_ratio_tiers_are_scored_without_rejecting_missing_values():
    definitions = _task_3_definitions(8)
    rows = []
    values = [
        None,
        0.8,
        2.0,
        0.5,
        3.0,
        float("nan"),
        -0.1,
        0.0,
    ]
    for definition, value in zip(definitions, values):
        row = _quote(definition, volume_ratio=value)
        if value is None:
            row.pop("volume_ratio")
        rows.append(row)

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )
    ranked = _by_code(result)
    qualities = {
        definition["code"]: ranked[definition["code"]]["volume_ratio_quality"]
        for definition in definitions
    }

    assert list(qualities.values()) == [
        0.0,
        1.0,
        1.0,
        0.5,
        0.5,
        0.0,
        0.0,
        0.0,
    ]
    assert ranked[definitions[6]["code"]]["volume_ratio"] is None
    assert ranked[definitions[7]["code"]]["volume_ratio"] == 0.0
    assert result["quality_counts"] == {
        "invalid_volume_ratio": 2,
        "missing_volume_ratio": 1,
        "non_ideal_volume_ratio": 3,
    }
    assert json.dumps(result, allow_nan=False)


def test_tencent_quality_curves_use_documented_turnover_amplitude_and_move_boundaries():
    definitions = _task_3_definitions(6)
    turnover = [0.0, 0.25, 0.5, 5.0, 7.5, 10.0]
    amplitude = [0.0, 4.0, 4.0, 4.0, 6.0, 8.0]
    pct_chg = [0.3, 1.5, 3.0, -1.5, -0.5, 0.299]
    rows = [
        _quote(
            definition,
            turnover_rate=turnover_value,
            amplitude=amplitude_value,
            pct_chg=pct_value,
        )
        for definition, turnover_value, amplitude_value, pct_value in zip(
            definitions,
            turnover,
            amplitude,
            pct_chg,
        )
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )
    ranked = _by_code(result)
    in_input_order = [ranked[item["code"]] for item in definitions]

    assert [item["turnover_quality"] for item in in_input_order] == [
        0.0,
        0.5,
        1.0,
        1.0,
        0.5,
        0.0,
    ]
    assert [item["amplitude_quality"] for item in in_input_order] == [
        1.0,
        1.0,
        1.0,
        1.0,
        0.5,
        0.0,
    ]
    assert [item["tencent_move_quality"] for item in in_input_order] == pytest.approx(
        [0.0, 1.0, 0.0, 0.0, 1.0, 0.00125]
    )


def test_tencent_percentiles_use_only_the_hard_filter_population_and_midrank_ties():
    definitions = _task_3_definitions(4)
    rows = [
        _quote(definitions[0], amount=100.0, total_mv=2_000_000_000.0),
        _quote(definitions[1], amount=100.0, total_mv=4_000_000_000.0),
        _quote(definitions[2], amount=300.0, total_mv=4_000_000_000.0),
        _quote(
            definitions[3],
            amount=999_999.0,
            total_mv=99_000_000_000.0,
            turnover_rate=10.1,
        ),
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )
    ranked = _by_code(result)
    accepted = [ranked[item["code"]] for item in definitions[:3]]

    assert result["tencent_rank_population_count"] == 3
    assert definitions[3]["code"] not in ranked
    assert [item["tencent_amount_percentile"] for item in accepted] == [
        0.25,
        0.25,
        1.0,
    ]
    assert [item["tencent_market_cap_percentile"] for item in accepted] == [
        0.0,
        0.75,
        0.75,
    ]


def test_tencent_single_candidate_percentiles_and_formula_are_exact():
    definition = _task_3_definitions(1)[0]

    result = discovery_module.verify_and_rank_tencent_candidates(
        [definition],
        [_quote(definition, pct_chg=1.5, turnover_rate=0.25, amplitude=6.0)],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )
    selected = result["definitions"][0]

    assert selected["tencent_amount_percentile"] == 1.0
    assert selected["tencent_market_cap_percentile"] == 1.0
    assert selected["tencent_move_quality"] == 1.0
    assert selected["turnover_quality"] == 0.5
    assert selected["volume_ratio_quality"] == 1.0
    assert selected["amplitude_quality"] == 0.5
    assert selected["tencent_score"] == pytest.approx(0.875)


def test_tencent_final_ties_use_score_amount_amplitude_and_code(monkeypatch):
    monkeypatch.setattr(
        discovery_module,
        "midrank_percentiles",
        lambda values: [0.5] * len(values),
    )
    definitions = _task_3_definitions(4)
    desired_codes = ["600103", "600102", "600101", "600100"]
    definitions = [
        {**definition, "code": code, "exchange": "sh"}
        for definition, code in zip(definitions, desired_codes)
    ]
    rows = [
        _quote(definitions[0], amount=100.0, amplitude=2.0),
        _quote(definitions[1], amount=200.0, amplitude=3.0),
        _quote(definitions[2], amount=200.0, amplitude=2.0),
        _quote(definitions[3], amount=200.0, amplitude=2.0),
    ]

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600100",
        "600101",
        "600102",
        "600103",
    ]


@pytest.mark.parametrize(
    "limit",
    [0, -1, True, False, 8.0, None, float("nan"), float("inf"), "8"],
)
def test_tencent_ranking_rejects_invalid_limits_with_a_stable_domain_error(limit):
    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^invalid_tencent_limit$",
    ):
        discovery_module.verify_and_rank_tencent_candidates(
            [],
            [],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
            now=NOW,
            limit=limit,
        )


def test_tencent_ranking_allows_explicit_technical_screen_pool_limits():
    definitions = _task_3_definitions(10)

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        [_quote(item) for item in definitions],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
        limit=100,
    )

    assert result["tencent_rank_population_count"] == 10
    assert result["selected_count"] == 10
    assert len(result["definitions"]) == 10


def test_tencent_ranking_reserves_three_slots_for_lower_one_lot_costs():
    definitions = _task_3_definitions(12)
    rows = []
    for index, definition in enumerate(definitions):
        rows.append(
            _quote(
                definition,
                close=100.0 if index < 8 else 5.0,
                amount=1_000_000_000.0 - index * 10_000_000.0,
                total_mv=50_000_000_000.0 - index * 1_000_000_000.0,
            )
        )

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )
    selected = result["definitions"]

    assert [item["code"] for item in selected] == [
        *(item["code"] for item in definitions[:5]),
        *(item["code"] for item in definitions[8:11]),
    ]
    assert [item["tencent_quality_rank"] for item in selected] == [
        1,
        2,
        3,
        4,
        5,
        9,
        10,
        11,
    ]
    assert [item["selection_lane"] for item in selected] == [
        *(["quality_core"] * 5),
        *(["one_lot_diversity"] * 3),
    ]
    assert all(
        item["tencent_one_lot_amount"] == 500.0 for item in selected[5:]
    )


def test_tencent_ranking_requires_strict_unique_task_3_definition_codes_and_exchanges():
    definition = _task_3_definitions(1)[0]

    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^invalid_definition_exchange$",
    ):
        discovery_module.verify_and_rank_tencent_candidates(
            [{**definition, "exchange": "sz"}],
            [],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
            now=NOW,
        )

    with pytest.raises(
        PublicCandidateDiscoveryInputError,
        match="^duplicate_definition_code$",
    ):
        discovery_module.verify_and_rank_tencent_candidates(
            [definition, dict(definition)],
            [],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
            now=NOW,
        )


def test_tencent_quote_map_is_json_safe_and_keeps_every_verified_quote():
    definitions = _task_3_definitions(2)
    hard_rejected = MappingProxyType(
        _quote(definitions[0], turnover_rate=10.1, volume_ratio=float("nan"))
    )
    accepted = MappingProxyType(_quote(definitions[1]))

    result = discovery_module.verify_and_rank_tencent_candidates(
        definitions,
        [hard_rejected, accepted],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        now=NOW,
    )

    assert set(result["quote_map"]) == {item["code"] for item in definitions}
    assert all(type(item) is dict for item in result["quote_map"].values())
    assert result["quote_map"][definitions[0]["code"]]["volume_ratio"] is None
    assert json.dumps(result, allow_nan=False)


def test_discovery_does_not_call_tencent_when_public_preselection_is_empty():
    snapshot = _snapshot([_row("600500", amount=99_999_999.0)])

    def unexpected_fetch(codes):
        raise AssertionError(f"Tencent must not be called for {list(codes)}")

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=unexpected_fetch,
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["quote_map"] == {}
    assert candidate_discovery == {
        "mode": "public_full_market",
        "status": "no_eligible_candidates",
        "source": "akshare.sina.stock_zh_a_spot",
        "benchmark_trade_date": BENCHMARK_TRADE_DATE,
        "provider_expected_count": MIN_BREADTH_UNIVERSE_SIZE,
        "provider_expected_exchange_counts": {
            "sh": 168,
            "sz": 166,
            "bj": 166,
        },
        "raw_row_count": MIN_BREADTH_UNIVERSE_SIZE,
        "unique_row_count": MIN_BREADTH_UNIVERSE_SIZE,
        "universe_count": MIN_BREADTH_UNIVERSE_SIZE,
        "exchange_counts": {"sh": 168, "sz": 166, "bj": 166},
        "total_coverage_ratio": 1.0,
        "exchange_coverage_ratio": {"sh": 1.0, "sz": 1.0, "bj": 1.0},
        "eligible_count": 0,
        "public_preselected_count": 0,
        "tencent_requested_count": 0,
        "tencent_minimum_verified_count": 0,
        "tencent_verified_count": 0,
        "tencent_rank_population_count": 0,
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
        "selected_count": 0,
        "rejection_counts": {
            "below_min_amount": MIN_BREADTH_UNIVERSE_SIZE
        },
        "quality_counts": {},
        "stage_sources": {
            "public_snapshot": {
                "provider": "akshare.sina.stock_zh_a_spot",
                "status": "ok",
            },
            "tencent_verification": {
                "provider": "tencent_batch_quotes",
                "status": "not_called_no_preselection",
            },
        },
    }
    assert json.dumps(result, allow_nan=False)


def test_discovery_accepts_the_real_task_1_success_dto_without_manual_patching():
    codes = (
        [f"{600000 + index:06d}" for index in range(200)]
        + [f"{index + 1:06d}" for index in range(200)]
        + [f"{430000 + index:06d}" for index in range(100)]
    )
    snapshot = _normalize_sina_snapshot(
        [
            {
                "代码": code,
                "名称": f"样本{code}",
                "最新价": 10.0,
                "涨跌幅": 1.5,
                "成交额": 200_000_000.0,
                "时间戳": "09:59:00",
            }
            for code in codes
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        provider_anchor={
            "trade_date": BENCHMARK_TRADE_DATE,
            "provider_time": "10:00:00",
        },
        provider_expected_counts={
            "total": 500,
            "sh": 200,
            "sz": 200,
            "bj": 100,
        },
        now=NOW,
    )
    calls = []

    def fetch_quotes(requested_codes):
        calls.append(list(requested_codes))
        return {
            "status": "fetch_error",
            "requested_codes": list(requested_codes),
            "rows": [],
            "error_type": "request_failed",
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )

    assert snapshot["status"] == "ok"
    assert snapshot["benchmark_trade_date"] == BENCHMARK_TRADE_DATE
    assert len(calls) == 1
    assert len(calls[0]) == 160
    assert result["stage"] == "tencent_verification"


def test_discovery_preserves_valid_real_task_1_failure_coverage_without_tencent():
    codes = (
        [f"{600000 + index:06d}" for index in range(200)]
        + [f"{index + 1:06d}" for index in range(200)]
        + [f"{430000 + index:06d}" for index in range(100)]
    )
    snapshot = _normalize_sina_snapshot(
        [
            {
                "代码": code,
                "名称": f"覆盖不足样本{code}",
                "最新价": 10.0,
                "涨跌幅": 1.5,
                "成交额": 200_000_000.0,
                "时间戳": "09:59:00",
            }
            for code in codes
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        provider_anchor={
            "trade_date": BENCHMARK_TRADE_DATE,
            "provider_time": "10:00:00",
        },
        provider_expected_counts={
            "total": 600,
            "sh": 240,
            "sz": 240,
            "bj": 120,
        },
        now=NOW,
    )
    calls = []

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert snapshot["status"] == "public_snapshot_coverage_incomplete"
    assert snapshot["rows"] == []
    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert result["definitions"] == []
    assert result["quote_map"] == {}
    assert candidate_discovery["status"] == "candidate_discovery_unavailable"
    assert candidate_discovery["source"] == "akshare.sina.stock_zh_a_spot"
    assert candidate_discovery["benchmark_trade_date"] == BENCHMARK_TRADE_DATE
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
    assert candidate_discovery["stage_sources"] == {
        "public_snapshot": {
            "provider": "akshare.sina.stock_zh_a_spot",
            "status": "public_snapshot_coverage_incomplete",
        },
        "tencent_verification": {
            "provider": "tencent_batch_quotes",
            "status": "not_called_public_snapshot_unavailable",
        },
    }


def test_discovery_preserves_valid_real_task_1_too_small_metrics_without_tencent():
    snapshot = _normalize_sina_snapshot(
        [
            {
                "代码": code,
                "名称": f"小样本{code}",
                "最新价": 10.0,
                "涨跌幅": 1.5,
                "成交额": 200_000_000.0,
                "时间戳": "09:59:00",
            }
            for code in ("600001", "000001", "430001")
        ],
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
        provider_anchor={
            "trade_date": BENCHMARK_TRADE_DATE,
            "provider_time": "10:00:00",
        },
        provider_expected_counts={"total": 6, "sh": 2, "sz": 2, "bj": 2},
        now=NOW,
    )

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda _codes: pytest.fail("Tencent must not be called"),
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert snapshot["status"] == "public_breadth_universe_too_small"
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert candidate_discovery["provider_expected_count"] == 6
    assert candidate_discovery["provider_expected_exchange_counts"] == {
        "sh": 2,
        "sz": 2,
        "bj": 2,
    }
    assert candidate_discovery["raw_row_count"] == 3
    assert candidate_discovery["unique_row_count"] == 3
    assert candidate_discovery["universe_count"] == 3
    assert candidate_discovery["exchange_counts"] == {"sh": 1, "sz": 1, "bj": 1}
    assert candidate_discovery["total_coverage_ratio"] == 0.5
    assert candidate_discovery["exchange_coverage_ratio"] == {
        "sh": 0.5,
        "sz": 0.5,
        "bj": 0.5,
    }


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_metric",
        "bool_metric",
        "nan_ratio",
        "infinite_ratio",
        "expected_sum_mismatch",
        "raw_less_than_unique",
        "universe_mismatch",
        "exchange_sum_mismatch",
        "ratio_mismatch",
        "non_empty_rows",
        "coverage_status_without_coverage_gap",
        "too_small_status_at_minimum",
    ],
)
def test_discovery_does_not_partially_trust_invalid_failure_snapshot_metrics(
    invalid_case,
):
    snapshot = {
        "status": "public_breadth_universe_too_small",
        "source": "akshare.sina.stock_zh_a_spot",
        "benchmark_trade_date": BENCHMARK_TRADE_DATE,
        "provider_expected_count": 6,
        "provider_expected_exchange_counts": {"sh": 2, "sz": 2, "bj": 2},
        "raw_row_count": 3,
        "unique_row_count": 3,
        "universe_count": 3,
        "universe_size": 3,
        "exchange_counts": {"sh": 1, "sz": 1, "bj": 1},
        "total_coverage_ratio": 0.5,
        "exchange_coverage_ratio": {"sh": 0.5, "sz": 0.5, "bj": 0.5},
        "rows": [],
    }
    if invalid_case == "missing_metric":
        snapshot.pop("raw_row_count")
    elif invalid_case == "bool_metric":
        snapshot["provider_expected_count"] = True
    elif invalid_case == "nan_ratio":
        snapshot["total_coverage_ratio"] = float("nan")
    elif invalid_case == "infinite_ratio":
        snapshot["exchange_coverage_ratio"]["sh"] = float("inf")
    elif invalid_case == "expected_sum_mismatch":
        snapshot["provider_expected_count"] = 7
    elif invalid_case == "raw_less_than_unique":
        snapshot["raw_row_count"] = 2
    elif invalid_case == "universe_mismatch":
        snapshot["universe_size"] = 2
    elif invalid_case == "exchange_sum_mismatch":
        snapshot["exchange_counts"]["sh"] = 0
    elif invalid_case == "ratio_mismatch":
        snapshot["exchange_coverage_ratio"]["bj"] = 0.6
    elif invalid_case == "non_empty_rows":
        snapshot["rows"] = [_row("600999")]
    elif invalid_case in {
        "coverage_status_without_coverage_gap",
        "too_small_status_at_minimum",
    }:
        snapshot.update(
            {
                "status": (
                    "public_snapshot_coverage_incomplete"
                    if invalid_case == "coverage_status_without_coverage_gap"
                    else "public_breadth_universe_too_small"
                ),
                "provider_expected_count": 500,
                "provider_expected_exchange_counts": {
                    "sh": 200,
                    "sz": 200,
                    "bj": 100,
                },
                "raw_row_count": 500,
                "unique_row_count": 500,
                "universe_count": 500,
                "universe_size": 500,
                "exchange_counts": {"sh": 200, "sz": 200, "bj": 100},
                "total_coverage_ratio": 1.0,
                "exchange_coverage_ratio": {"sh": 1.0, "sz": 1.0, "bj": 1.0},
            }
        )

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda _codes: pytest.fail("Tencent must not be called"),
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert candidate_discovery["source"] == "akshare.sina.stock_zh_a_spot"
    assert candidate_discovery["benchmark_trade_date"] == BENCHMARK_TRADE_DATE
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


def test_discovery_rejects_tiny_nominally_complete_snapshot_before_tencent():
    snapshot = _snapshot(
        [_row("600501"), _row("000501"), _row("430501")],
        minimum_size=3,
    )
    calls = []

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert snapshot["status"] == "ok"
    assert snapshot["provider_expected_exchange_counts"] == {
        "sh": 1,
        "sz": 1,
        "bj": 1,
    }
    assert snapshot["exchange_coverage_ratio"] == {
        "sh": 1.0,
        "sz": 1.0,
        "bj": 1.0,
    }
    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert result["candidate_discovery"]["stage_sources"]["public_snapshot"][
        "status"
    ] == "invalid_snapshot_dto"


def test_discovery_rejects_snapshot_one_row_below_minimum_before_tencent():
    snapshot = _snapshot(
        [_row("600502"), _row("000502"), _row("430502")],
        minimum_size=MIN_BREADTH_UNIVERSE_SIZE - 1,
    )
    calls = []

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert snapshot["unique_row_count"] == MIN_BREADTH_UNIVERSE_SIZE - 1
    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["candidate_discovery"]["stage_sources"]["public_snapshot"][
        "status"
    ] == "invalid_snapshot_dto"


def test_discovery_accepts_snapshot_at_minimum_and_calls_tencent():
    snapshot = _snapshot(
        [_row("600503"), _row("000503"), _row("430503")]
    )
    calls = []

    def fetch_quotes(codes):
        requested_codes = list(codes)
        calls.append(requested_codes)
        return {
            "status": "fetch_error",
            "requested_codes": requested_codes,
            "rows": [],
            "error_type": "request_failed",
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )

    assert snapshot["unique_row_count"] == MIN_BREADTH_UNIVERSE_SIZE
    assert len(calls) == 1
    assert set(calls[0]) == {"600503", "000503", "430503"}
    assert result["stage"] == "tencent_verification"


def test_discovery_calls_tencent_once_with_public_priority_order_and_builds_full_dto():
    rows = [
        _row("600510", amount=200_000_000.0),
        _row("000510", amount=400_000_000.0),
        _row("430510", amount=300_000_000.0),
    ]
    snapshot = _snapshot(rows)
    public_result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )
    expected_codes = [item["code"] for item in public_result["definitions"]]
    calls = []

    def fetch_quotes(codes):
        requested_codes = list(codes)
        calls.append(requested_codes)
        definitions_by_code = {
            item["code"]: item for item in public_result["definitions"]
        }
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [
                _quote(definitions_by_code[code]) for code in requested_codes
            ],
            "error_type": None,
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert calls == [expected_codes]
    assert result["status"] == "ok"
    assert set(result["quote_map"]) == set(expected_codes)
    assert candidate_discovery["source"] == (
        "akshare.sina.stock_zh_a_spot+tencent_batch_quotes"
    )
    assert candidate_discovery["eligible_count"] == 3
    assert candidate_discovery["public_preselected_count"] == 3
    assert candidate_discovery["tencent_requested_count"] == 3
    assert candidate_discovery["tencent_minimum_verified_count"] == 3
    assert candidate_discovery["tencent_verified_count"] == 3
    assert candidate_discovery["tencent_rank_population_count"] == 3
    assert candidate_discovery["technical_checked_count"] == 0
    assert candidate_discovery["selected_count"] == 3
    assert candidate_discovery["stage_sources"] == {
        "public_snapshot": {
            "provider": "akshare.sina.stock_zh_a_spot",
            "status": "ok",
        },
        "tencent_verification": {
            "provider": "tencent_batch_quotes",
            "status": "ok",
        },
    }
    assert json.dumps(result, allow_nan=False)


def test_discovery_includes_tencent_source_when_completed_hard_filters_select_nothing():
    rows = [_row("600520")]
    snapshot = _snapshot(rows)

    def fetch_quotes(codes):
        requested_codes = list(codes)
        definition = rank_public_candidate_universe(
            rows,
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
        )["definitions"][0]
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [_quote(definition, turnover_rate=10.1)],
            "error_type": None,
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert set(result["quote_map"]) == {"600520"}
    assert result["candidate_discovery"]["source"].endswith(
        "+tencent_batch_quotes"
    )
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "ok"
    assert result["candidate_discovery"]["rejection_counts"] == {
        "below_min_amount": MIN_BREADTH_UNIVERSE_SIZE - 1,
        "turnover_rate_out_of_range": 1
    }


def test_discovery_merges_public_and_tencent_rejection_and_quality_counts():
    rows = [
        _row("600530"),
        _row("600531", amount=99_999_999.0),
    ]
    snapshot = _snapshot(rows)

    def fetch_quotes(codes):
        requested_codes = list(codes)
        definition = rank_public_candidate_universe(
            rows,
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
        )["definitions"][0]
        quote = _quote(definition)
        quote.pop("volume_ratio")
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [quote],
            "error_type": None,
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert candidate_discovery["rejection_counts"] == {
        "below_min_amount": MIN_BREADTH_UNIVERSE_SIZE - 1
    }
    assert candidate_discovery["quality_counts"]["missing_volume_ratio"] == 1


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"status": "public_snapshot_coverage_incomplete"},
        _snapshot([_row("600540")], benchmark_trade_date="2026-07-15junk"),
        {
            **_snapshot([_row("600540")]),
            "rows": tuple([_row("600540")]),
        },
        _snapshot(
            [_row("600540")],
            excluded_future_time_count=True,
        ),
    ],
)
def test_discovery_rejects_non_ok_or_malformed_public_snapshot_dtos(snapshot):
    calls = []

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert result["definitions"] == []
    assert result["quote_map"] == {}
    assert result["candidate_discovery"]["status"] == (
        "candidate_discovery_unavailable"
    )
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "not_called_public_snapshot_unavailable"
    assert json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("snapshot_status", "expected_stage"),
    [
        (
            "public_snapshot_expected_counts_unavailable",
            "sina_expected_counts",
        ),
        ("public_snapshot_coverage_incomplete", "sina_snapshot"),
    ],
)
def test_discovery_uses_the_specific_stage_for_expected_count_failures(
    snapshot_status,
    expected_stage,
):
    calls = []

    result = discovery_module.discover_public_candidate_universe(
        {"status": snapshot_status},
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == expected_stage


@pytest.mark.parametrize(
    "invalid_case",
    [
        "provider_date_mismatch",
        "expected_sum_mismatch",
        "non_positive_exchange_expected",
        "row_count_mismatch",
        "raw_less_than_unique",
        "exchange_count_mismatch",
        "total_ratio_mismatch",
        "exchange_ratio_mismatch",
        "low_coverage",
        "bool_count",
        "nan_ratio",
        "infinite_ratio",
        "negative_count",
        "incomplete_row_exchange",
    ],
)
def test_discovery_rejects_inconsistent_task_1_success_dtos(invalid_case):
    snapshot = _snapshot(
        [_row("600700"), _row("000700"), _row("430700")]
    )
    if invalid_case == "provider_date_mismatch":
        snapshot["provider_trade_date"] = "2026-07-14"
    elif invalid_case == "expected_sum_mismatch":
        snapshot["provider_expected_count"] = 4
    elif invalid_case == "non_positive_exchange_expected":
        snapshot["provider_expected_exchange_counts"]["bj"] = 0
    elif invalid_case == "row_count_mismatch":
        snapshot["unique_row_count"] = 2
    elif invalid_case == "raw_less_than_unique":
        snapshot["raw_row_count"] = 2
    elif invalid_case == "exchange_count_mismatch":
        snapshot["exchange_counts"]["sh"] = 2
    elif invalid_case == "total_ratio_mismatch":
        snapshot["total_coverage_ratio"] = 0.99
    elif invalid_case == "exchange_ratio_mismatch":
        snapshot["exchange_coverage_ratio"]["sh"] = 0.99
    elif invalid_case == "low_coverage":
        snapshot["provider_expected_count"] = 6
        snapshot["provider_expected_exchange_counts"] = {
            "sh": 2,
            "sz": 2,
            "bj": 2,
        }
        snapshot["total_coverage_ratio"] = 0.5
        snapshot["exchange_coverage_ratio"] = {
            "sh": 0.5,
            "sz": 0.5,
            "bj": 0.5,
        }
    elif invalid_case == "bool_count":
        snapshot["provider_expected_count"] = True
    elif invalid_case == "nan_ratio":
        snapshot["total_coverage_ratio"] = float("nan")
    elif invalid_case == "infinite_ratio":
        snapshot["exchange_coverage_ratio"]["sh"] = float("inf")
    elif invalid_case == "negative_count":
        snapshot["raw_row_count"] = -1
    elif invalid_case == "incomplete_row_exchange":
        snapshot["rows"][0] = {**snapshot["rows"][0], "exchange": None}

    calls = []
    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "sina_snapshot"
    assert result["candidate_discovery"]["stage_sources"]["public_snapshot"][
        "status"
    ] == "invalid_snapshot_dto"


def test_discovery_converts_task_3_domain_errors_to_public_preselection_failure(
    monkeypatch,
):
    snapshot = _snapshot([_row("600550")])
    calls = []

    def fail_ranking(rows, *, benchmark_trade_date, limit=40):
        raise PublicCandidateDiscoveryInputError("snapshot_rows_unavailable")

    monkeypatch.setattr(
        discovery_module,
        "rank_public_candidate_universe",
        fail_ranking,
    )

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=lambda codes: calls.append(list(codes)),
        now=NOW,
    )

    assert calls == []
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "public_preselection"
    assert result["candidate_discovery"]["stage_sources"]["public_snapshot"] == {
        "provider": "akshare.sina.stock_zh_a_spot",
        "status": "ok",
    }
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "not_called_public_preselection_unavailable"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("ranking exploded"), KeyError("ranking field missing")],
)
def test_discovery_marks_tencent_not_called_for_unexpected_ranking_failure(
    monkeypatch,
    caplog,
    error,
):
    snapshot = _snapshot([_row("600551")])
    calls = []

    def fail_ranking(rows, *, benchmark_trade_date, limit=40):
        raise error

    monkeypatch.setattr(
        discovery_module,
        "rank_public_candidate_universe",
        fail_ranking,
    )

    with caplog.at_level(logging.ERROR, logger=discovery_module.__name__):
        result = discovery_module.discover_public_candidate_universe(
            snapshot,
            fetch_quotes=lambda codes: calls.append(list(codes)),
            now=NOW,
        )

    assert calls == []
    assert result["stage"] == "public_preselection"
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "not_called_public_preselection_unavailable"
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    ("error", "expected_status", "expects_traceback"),
    [
        (
            PublicCandidateDiscoveryInputError("quote_rows_unavailable"),
            "invalid_fetch_dto",
            False,
        ),
        (RuntimeError("verification exploded"), "internal_error", True),
    ],
)
def test_discovery_classifies_verification_domain_and_internal_errors(
    monkeypatch,
    caplog,
    error,
    expected_status,
    expects_traceback,
):
    snapshot = _snapshot([_row("600552")])

    def fetch_quotes(codes):
        requested_codes = list(codes)
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [],
            "error_type": None,
        }

    monkeypatch.setattr(
        discovery_module,
        "verify_and_rank_tencent_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with caplog.at_level(logging.ERROR, logger=discovery_module.__name__):
        result = discovery_module.discover_public_candidate_universe(
            snapshot,
            fetch_quotes=fetch_quotes,
            now=NOW,
        )

    assert result["stage"] == "tencent_verification"
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == expected_status
    assert any(record.exc_info for record in caplog.records) is expects_traceback


def test_discovery_converts_fetcher_exceptions_to_tencent_verification_failure(
    caplog,
):
    snapshot = _snapshot([_row("600560")])
    calls = []

    def broken_fetch(codes):
        calls.append(list(codes))
        raise RuntimeError("provider down")

    with caplog.at_level(logging.ERROR, logger=discovery_module.__name__):
        result = discovery_module.discover_public_candidate_universe(
            snapshot,
            fetch_quotes=broken_fetch,
            now=NOW,
        )

    assert len(calls) == 1
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "tencent_verification"
    assert result["definitions"] == []
    assert result["quote_map"] == {}
    assert result["candidate_discovery"]["source"] == (
        "akshare.sina.stock_zh_a_spot"
    )
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == "fetch_exception"
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    ("fetch_result", "expected_stage_status"),
    [
        (
            {
                "status": "fetch_error",
                "requested_codes": ["600570"],
                "rows": [],
            },
            "fetch_error",
        ),
        (
            {
                "status": "ok",
                "requested_codes": [],
                "rows": [],
                "error_type": None,
            },
            "requested_codes_mismatch",
        ),
        (
            {
                "status": "ok",
                "requested_codes": ["600570"],
                "rows": None,
                "error_type": None,
            },
            "invalid_fetch_dto",
        ),
        (
            {
                "status": "ok",
                "requested_codes": ["600570"],
                "rows": [],
            },
            "invalid_fetch_dto",
        ),
        (
            {
                "status": "ok",
                "requested_codes": ["600570"],
                "rows": [],
                "error_type": "request_failed",
            },
            "invalid_fetch_dto",
        ),
        (None, "invalid_fetch_dto"),
    ],
)
def test_discovery_rejects_non_ok_or_wrong_tencent_fetch_dtos(
    fetch_result,
    expected_stage_status,
):
    snapshot = _snapshot([_row("600570")])
    calls = []

    def fetch_quotes(codes):
        calls.append(list(codes))
        return fetch_result

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )

    assert calls == [["600570"]]
    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "tencent_verification"
    assert result["candidate_discovery"]["stage_sources"][
        "tencent_verification"
    ]["status"] == expected_stage_status
    assert result["candidate_discovery"]["tencent_requested_count"] == 1
    assert result["candidate_discovery"]["selected_count"] == 0
    assert result["definitions"] == []
    assert result["quote_map"] == {}


def test_discovery_reports_coverage_failure_instead_of_using_a_small_subset():
    rows = [_row(f"600{index + 580:03d}") for index in range(21)]
    snapshot = _snapshot(rows)
    public_result = rank_public_candidate_universe(
        rows,
        benchmark_trade_date=BENCHMARK_TRADE_DATE,
    )
    definitions_by_code = {
        item["code"]: item for item in public_result["definitions"]
    }

    def fetch_quotes(codes):
        requested_codes = list(codes)
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [
                _quote(definitions_by_code[code])
                for code in requested_codes[:19]
            ],
            "error_type": None,
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )
    candidate_discovery = result["candidate_discovery"]

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["stage"] == "tencent_verification"
    assert result["definitions"] == []
    assert result["quote_map"] == {}
    assert candidate_discovery["tencent_requested_count"] == 21
    assert candidate_discovery["tencent_minimum_verified_count"] == 20
    assert candidate_discovery["tencent_verified_count"] == 19
    assert candidate_discovery["selected_count"] == 0
    assert candidate_discovery["stage_sources"]["tencent_verification"][
        "status"
    ] == "coverage_incomplete"
    assert candidate_discovery["rejection_counts"]["missing_response"] == 2


def test_discovery_output_is_strict_json_and_has_no_executable_trade_fields():
    snapshot = _snapshot([_row("600610")])

    def fetch_quotes(codes):
        requested_codes = list(codes)
        definition = rank_public_candidate_universe(
            snapshot["rows"],
            benchmark_trade_date=BENCHMARK_TRADE_DATE,
        )["definitions"][0]
        return {
            "status": "ok",
            "requested_codes": requested_codes,
            "rows": [MappingProxyType(_quote(definition, volume_ratio=float("nan")))],
            "error_type": None,
        }

    result = discovery_module.discover_public_candidate_universe(
        snapshot,
        fetch_quotes=fetch_quotes,
        now=NOW,
    )
    forbidden = {
        "actionable",
        "reference_actionable",
        "suggested_lots",
        "suggested_quantity",
        "new_position_allowed",
        "cash_usage_pct",
        "affordable_with_cash",
    }

    def visit(value):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    assert json.dumps(result, allow_nan=False)
