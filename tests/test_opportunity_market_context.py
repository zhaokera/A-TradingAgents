import json
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.services.opportunity_market_context as context_module
from app.services.opportunity_market_context import (
    OpportunityMarketContext,
    build_opportunity_market_context,
)


INDEX_SYMBOLS = ("sh000001", "sz399001", "sz399006", "sh000688")
NOW = datetime(2026, 7, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class FakeMonotonic:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def _index_row(symbol, *, trade_date="2026-07-17", pct_chg=0.1):
    return {
        "code": symbol[2:],
        "provider_symbol": symbol,
        "envelope_code": symbol[2:],
        "payload_code": symbol[2:],
        "parse_status": "ok",
        "pct_chg": pct_chg,
        "trade_date": trade_date,
        "source": "tencent",
    }


def _successful_index_result(rows=None):
    return {
        "status": "ok",
        "requested_codes": list(INDEX_SYMBOLS),
        "rows": rows or [_index_row(symbol) for symbol in INDEX_SYMBOLS],
        "error_type": None,
    }


def test_build_context_fetches_four_indices_in_one_batch():
    calls = []

    def fake_quote_fetcher(codes, *, timeout):
        calls.append({"codes": tuple(codes), "timeout": timeout})
        return _successful_index_result()

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=FakeMonotonic(100.0),
        quote_fetcher=fake_quote_fetcher,
    )

    assert calls == [{"codes": INDEX_SYMBOLS, "timeout": 10.0}]
    assert context.index_status == "ok"
    assert context.benchmark_trade_date == "2026-07-17"
    assert [row["requested_symbol"] for row in context.index_quotes] == list(
        INDEX_SYMBOLS
    )
    assert context.public_snapshot_loaded is False


def test_default_tencent_context_uses_bounded_parent_fetcher(monkeypatch):
    bounded_fetcher = getattr(
        context_module,
        "fetch_tencent_market_context_bounded",
        None,
    )
    assert callable(bounded_fetcher)
    calls = []

    def fake_bounded_fetcher(*, timeout_seconds):
        calls.append(timeout_seconds)
        return _successful_index_result()

    monkeypatch.setattr(
        context_module,
        "fetch_tencent_market_context_bounded",
        fake_bounded_fetcher,
    )

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=FakeMonotonic(),
    )

    assert context.index_status == "ok"
    assert calls == [10.0]


def test_bounded_tencent_context_returns_stable_timeout(monkeypatch):
    bounded_fetcher = getattr(
        context_module,
        "fetch_tencent_market_context_bounded",
        None,
    )
    assert callable(bounded_fetcher)
    calls = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(context_module.subprocess, "run", fake_run)

    result = bounded_fetcher(timeout_seconds=2.5)

    assert calls[0]["timeout"] == 2.5
    assert calls[0]["capture_output"] is True
    assert calls[0]["text"] is True
    assert result["status"] == "stage_timeout"
    assert result["stage"] == "tencent_market_context"
    assert result["timeout_seconds"] == 2.5
    assert result["rows"] == []


def test_bounded_tencent_context_logs_nonzero_worker_exit_with_truncated_stderr(
    monkeypatch,
    caplog,
):
    stderr = "worker fatal: " + ("x" * 2000)

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            returncode=7,
            stdout="",
            stderr=stderr,
        )

    monkeypatch.setattr(context_module.subprocess, "run", fake_run)

    with caplog.at_level("ERROR", logger=context_module.__name__):
        result = context_module.fetch_tencent_market_context_bounded(
            timeout_seconds=2.5
        )

    assert result == {
        "status": "index_fetch_failed",
        "stage": "tencent_market_context",
        "requested_codes": list(INDEX_SYMBOLS),
        "rows": [],
        "error_type": "WorkerProcessError",
        "worker_exit_code": 7,
    }
    message = next(
        record.getMessage()
        for record in caplog.records
        if "worker exited with nonzero status" in record.getMessage()
    )
    stderr_limit = context_module.TENCENT_WORKER_STDERR_LOG_LIMIT
    assert "returncode=7" in message
    assert stderr[:stderr_limit] in message
    assert stderr not in message


def test_bounded_tencent_context_logs_structured_provider_failure(monkeypatch, caplog):
    stderr = "provider warning: " + ("y" * 2000)
    provider_payload = {
        "status": "index_fetch_failed",
        "requested_codes": list(INDEX_SYMBOLS),
        "rows": [],
        "error_type": "RuntimeError",
    }

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(provider_payload),
            stderr=stderr,
        )

    monkeypatch.setattr(context_module.subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger=context_module.__name__):
        result = context_module.fetch_tencent_market_context_bounded(
            timeout_seconds=2.5
        )

    assert result == provider_payload
    message = next(
        record.getMessage()
        for record in caplog.records
        if "worker returned provider failure" in record.getMessage()
    )
    stderr_limit = context_module.TENCENT_WORKER_STDERR_LOG_LIMIT
    assert "status=index_fetch_failed" in message
    assert "error_type=RuntimeError" in message
    assert stderr[:stderr_limit] in message
    assert stderr not in message


def test_tencent_context_worker_fetches_one_batch(monkeypatch, capsys):
    worker_main = getattr(context_module, "_worker_main", None)
    assert callable(worker_main)
    calls = []

    def fake_fetch(codes, *, timeout):
        calls.append({"codes": tuple(codes), "timeout": timeout})
        return _successful_index_result()

    monkeypatch.setattr(context_module, "fetch_tencent_quotes_sync", fake_fetch)

    exit_code = worker_main(
        ["--tencent-worker", "--timeout-seconds", "3.5"]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert exit_code == 0
    assert calls == [{"codes": INDEX_SYMBOLS, "timeout": 3.5}]
    assert payload == _successful_index_result()


def test_build_context_discards_success_returned_after_deadline():
    clock = FakeMonotonic()

    def late_success(_codes, *, timeout):
        assert timeout == 10.0
        clock.value = 91.0
        return _successful_index_result()

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=clock,
        quote_fetcher=late_success,
    )

    assert context.index_status == "stage_timeout"
    assert context.index_quotes == []
    assert context.benchmark_trade_date is None
    assert context.index_error["stage"] == "tencent_market_context"


def test_build_context_logs_unexpected_fetcher_exception_with_traceback(caplog):
    def failing_fetcher(_codes, *, timeout):
        raise RuntimeError("unexpected Tencent fetch failure")

    with caplog.at_level("ERROR", logger=context_module.__name__):
        context = build_opportunity_market_context(
            now=NOW,
            monotonic=FakeMonotonic(),
            quote_fetcher=failing_fetcher,
        )

    assert context.index_status == "index_fetch_failed"
    assert context.index_error["error_type"] == "RuntimeError"
    matching_records = [
        record
        for record in caplog.records
        if "Tencent market context fetch failed" in record.getMessage()
    ]
    assert len(matching_records) == 1
    assert matching_records[0].exc_info is not None
    assert matching_records[0].exc_info[0] is RuntimeError


@pytest.mark.parametrize(
    ("rows", "expected_status"),
    [
        (
            [
                _index_row(symbol, trade_date=None if index == 0 else "2026-07-17")
                for index, symbol in enumerate(INDEX_SYMBOLS)
            ],
            "index_trade_date_missing",
        ),
        (
            [
                _index_row(
                    symbol,
                    trade_date="2026-07-16" if symbol == "sz399001" else "2026-07-17",
                )
                for symbol in INDEX_SYMBOLS
            ],
            "index_trade_date_mismatch",
        ),
    ],
)
def test_build_context_fails_closed_for_missing_or_mismatched_index_dates(
    rows,
    expected_status,
):
    context = build_opportunity_market_context(
        now=NOW,
        monotonic=FakeMonotonic(),
        quote_fetcher=lambda _codes, *, timeout: _successful_index_result(rows),
    )

    assert context.index_status == expected_status
    assert context.benchmark_trade_date is None
    assert context.index_error["status"] == expected_status
    assert context.index_error["stage"] == "tencent_market_context"


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("wrong_requested_codes", "index_requested_codes_mismatch"),
        ("extra_row", "index_quote_count_mismatch"),
        ("partial_rows", "index_quote_count_mismatch"),
        ("duplicate_provider", "index_quote_identity_mismatch"),
        ("missing_error_type", "index_response_invalid"),
        ("non_null_error_type", "index_response_invalid"),
        ("missing_parse_status", "index_quote_parse_failed"),
        ("identity_mismatch", "index_quote_identity_mismatch"),
        ("boolean_identity", "index_quote_identity_mismatch"),
        ("non_strict_trade_date", "index_trade_date_invalid"),
        ("future_unified_date", "index_trade_date_in_future"),
    ],
)
def test_build_context_strictly_validates_index_dto_and_clears_partial_rows(
    case,
    expected_status,
):
    result = _successful_index_result()
    if case == "wrong_requested_codes":
        result["requested_codes"] = list(reversed(INDEX_SYMBOLS))
    elif case == "extra_row":
        result["rows"].append(_index_row("sh000001"))
    elif case == "partial_rows":
        result["rows"] = result["rows"][:3]
    elif case == "duplicate_provider":
        result["rows"][-1] = _index_row("sz399006")
    elif case == "missing_error_type":
        result.pop("error_type")
    elif case == "non_null_error_type":
        result["error_type"] = "unexpected"
    elif case == "missing_parse_status":
        result["rows"][0].pop("parse_status")
    elif case == "identity_mismatch":
        result["rows"][1]["payload_code"] = "399006"
    elif case == "boolean_identity":
        result["rows"][1]["code"] = True
    elif case == "non_strict_trade_date":
        for row in result["rows"]:
            row["trade_date"] = "20260717"
    elif case == "future_unified_date":
        for row in result["rows"]:
            row["trade_date"] = "2026-07-18"

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=FakeMonotonic(),
        quote_fetcher=lambda _codes, *, timeout: result,
    )

    assert context.index_status == expected_status
    assert context.index_quotes == []
    assert context.benchmark_trade_date is None
    assert context.index_error["status"] == expected_status
    assert context.index_error["stage"] == "tencent_market_context"


@pytest.mark.parametrize(
    "invalid_pct_chg",
    [
        None,
        True,
        "0.1",
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
    ],
    ids=[
        "missing",
        "boolean",
        "string",
        "nan",
        "positive_inf",
        "negative_inf",
        "huge_integer",
    ],
)
def test_build_context_rejects_invalid_index_pct_chg(invalid_pct_chg):
    result = _successful_index_result()
    if invalid_pct_chg is None:
        result["rows"][0].pop("pct_chg")
    else:
        result["rows"][0]["pct_chg"] = invalid_pct_chg

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=FakeMonotonic(),
        quote_fetcher=lambda _codes, *, timeout: result,
    )

    assert context.index_status == "index_quote_change_invalid"
    assert context.index_quotes == []
    assert context.benchmark_trade_date is None
    assert context.index_error == {
        "status": "index_quote_change_invalid",
        "stage": "tencent_market_context",
        "symbol": "sh000001",
    }


def test_ensure_public_snapshot_caches_success_and_uses_remaining_budget():
    clock = FakeMonotonic(10.0)
    calls = []
    expected = {
        "status": "ok",
        "source": "akshare.sina.stock_zh_a_spot",
        "rows": [{"code": "600000", "trade_date": "2026-07-17"}],
    }

    def fake_snapshot_fetcher(*, benchmark_trade_date, timeout_seconds, now):
        calls.append(
            {
                "benchmark_trade_date": benchmark_trade_date,
                "timeout_seconds": timeout_seconds,
                "now": now,
            }
        )
        return json.loads(json.dumps(expected))

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=clock,
        public_snapshot_fetcher=fake_snapshot_fetcher,
    )

    first = context.ensure_public_snapshot()
    first["status"] = "mutated"
    first["rows"][0]["code"] = "mutated"
    assert context.public_snapshot == expected
    clock.value = 89.0
    second = context.ensure_public_snapshot()

    assert second == expected
    assert second is not first
    assert second["rows"] is not first["rows"]
    assert context.public_snapshot is not first
    assert context.public_snapshot is not second
    assert context.public_snapshot["rows"] is not second["rows"]
    assert context.public_snapshot_loaded is True
    assert calls == [
        {
            "benchmark_trade_date": "2026-07-17",
            "timeout_seconds": 25.0,
            "now": NOW,
        }
    ]


def test_ensure_public_snapshot_discards_success_returned_after_deadline():
    clock = FakeMonotonic()

    def late_success(**_kwargs):
        clock.value = 91.0
        return {
            "status": "ok",
            "source": "akshare.sina.stock_zh_a_spot",
            "rows": [{"code": "600000"}],
        }

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=clock,
        public_snapshot_fetcher=late_success,
    )

    result = context.ensure_public_snapshot()

    assert result["status"] == "stage_timeout"
    assert result["stage"] == "sina_public_snapshot"
    assert result["rows"] == []
    assert context.public_snapshot == result
    assert context.public_snapshot is not result


def test_ensure_public_snapshot_caches_failure():
    calls = []
    expected = {
        "status": "public_breadth_fetch_failed",
        "source": "akshare.sina.stock_zh_a_spot",
        "error_type": "WorkerProcessError",
        "rows": [],
    }

    def fake_snapshot_fetcher(**_kwargs):
        calls.append("fetch")
        return json.loads(json.dumps(expected))

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=FakeMonotonic(),
        public_snapshot_fetcher=fake_snapshot_fetcher,
    )

    first = context.ensure_public_snapshot()
    first["error_type"] = "mutated"
    first["rows"].append({"code": "mutated"})
    assert context.public_snapshot == expected
    second = context.ensure_public_snapshot()

    assert second == expected
    assert second is not first
    assert second["rows"] is not first["rows"]
    assert calls == ["fetch"]


def test_retry_public_snapshot_once_recovers_exact_provider_timeout():
    calls = []

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
            "rows": [{"code": "600000", "trade_date": "2026-07-17"}],
        }

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=FakeMonotonic(),
        public_snapshot_fetcher=fake_snapshot_fetcher,
    )

    assert context.ensure_public_snapshot()["status"] == "public_breadth_timeout"
    recovered = context.retry_public_snapshot_once_if_timeout()
    cached = context.retry_public_snapshot_once_if_timeout()

    assert recovered["status"] == "ok"
    assert recovered["attempt_count"] == 2
    assert recovered["retried_after_status"] == "public_breadth_timeout"
    assert cached == recovered
    assert calls == ["fetch", "fetch"]


def test_ensure_public_snapshot_normalizes_and_caches_empty_failure_payload():
    calls = []

    def fake_snapshot_fetcher(**_kwargs):
        calls.append("fetch")
        return {}

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=FakeMonotonic(),
        public_snapshot_fetcher=fake_snapshot_fetcher,
    )

    first = context.ensure_public_snapshot()
    first["status"] = "mutated"
    first["rows"].append({"code": "mutated"})
    second = context.ensure_public_snapshot()

    assert second == {
        "status": "public_breadth_fetch_failed",
        "source": "akshare.sina.stock_zh_a_spot",
        "error_type": "InvalidFetcherPayload",
        "rows": [],
    }
    assert second is not first
    assert second["rows"] is not first["rows"]
    assert calls == ["fetch"]


def test_ensure_public_snapshot_caches_exception_with_isolated_copies():
    calls = []

    def failing_fetcher(**_kwargs):
        calls.append("fetch")
        raise RuntimeError("unexpected snapshot failure")

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=FakeMonotonic(),
        public_snapshot_fetcher=failing_fetcher,
    )

    first = context.ensure_public_snapshot()
    first["error_type"] = "mutated"
    first["rows"].append({"code": "mutated"})
    second = context.ensure_public_snapshot()

    assert second == {
        "status": "public_breadth_fetch_failed",
        "source": "akshare.sina.stock_zh_a_spot",
        "error_type": "RuntimeError",
        "rows": [],
    }
    assert second is not first
    assert second["rows"] is not first["rows"]
    assert calls == ["fetch"]


def test_remaining_seconds_and_stage_timeout_use_the_smaller_budget():
    clock = FakeMonotonic(10.0)
    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=clock,
    )

    assert context.remaining_seconds() == 80.0
    assert context.stage_timeout("mongo") == 5.0
    assert context.stage_timeout("tencent_market_context") == 10.0
    assert context.stage_timeout("sina_public_snapshot") == 25.0
    assert context.stage_timeout("tencent_candidate_review") == 10.0
    assert context.stage_timeout("technical_deep_inspection") == 50.0
    assert context.stage_timeout("orchestration") == 5.0

    clock.value = 72.5
    assert context.remaining_seconds() == 17.5
    assert context.stage_timeout("sina_public_snapshot") == 17.5
    assert context.stage_timeout("technical_deep_inspection") == 17.5


def test_ensure_public_snapshot_does_not_fetch_after_command_deadline():
    clock = FakeMonotonic(90.0)
    calls = []

    def unexpected_fetcher(**_kwargs):
        calls.append("fetch")
        raise AssertionError("expired context must not call Sina")

    context = OpportunityMarketContext(
        now=NOW,
        started_at=0.0,
        deadline_at=90.0,
        index_quotes=[],
        benchmark_trade_date="2026-07-17",
        monotonic=clock,
        public_snapshot_fetcher=unexpected_fetcher,
    )

    first = context.ensure_public_snapshot()
    first["status"] = "mutated"
    first["rows"].append({"code": "mutated"})
    second = context.ensure_public_snapshot()

    assert second == {
        "status": "stage_timeout",
        "stage": "sina_public_snapshot",
        "reason": "command_deadline_exceeded",
        "timeout_seconds": 0.0,
        "rows": [],
    }
    assert second is not first
    assert second["rows"] is not first["rows"]
    assert calls == []


def test_build_context_returns_explicit_timeout_at_ninety_seconds():
    class DeadlineClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return 0.0 if self.calls == 1 else 90.0

    calls = []

    def unexpected_quote_fetcher(_codes, *, timeout):
        calls.append(timeout)
        raise AssertionError("expired context must not call Tencent")

    context = build_opportunity_market_context(
        now=NOW,
        monotonic=DeadlineClock(),
        quote_fetcher=unexpected_quote_fetcher,
    )

    assert context.started_at == 0.0
    assert context.deadline_at == 90.0
    assert context.index_status == "stage_timeout"
    assert context.benchmark_trade_date is None
    assert context.index_error == {
        "status": "stage_timeout",
        "stage": "tencent_market_context",
        "reason": "command_deadline_exceeded",
        "timeout_seconds": 0.0,
    }
    assert calls == []
