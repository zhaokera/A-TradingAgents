import importlib.util
import io
import json
import subprocess
import sys
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

import app.services.public_candidate_deep_check as deep_check


def test_public_candidate_deep_check_module_exists():
    assert importlib.util.find_spec(
        "app.services.public_candidate_deep_check"
    ) is not None


def _definition(code="600000"):
    return {
        "code": code,
        "name": f"candidate-{code}",
        "theme": "public_research",
        "priority": 1,
        "trade_date": "2026-07-17",
    }


def _quote(code="600000", close=10.0):
    return {
        "code": code,
        "provider_symbol": f"sh{code}" if code.startswith("6") else f"sz{code}",
        "close": close,
        "pct_chg": 0.5,
        "source": "tencent_batch_quotes",
    }


def _screen_result(code, *, net_reward_risk=2.0, status="ok", passed=True):
    return {
        "code": code,
        "status": status,
        "passed": passed,
        "fatal_error": False,
        "guarded_price_plan": {
            "status": status,
            "actionable": passed,
            "fee_aware_trade": {"net_reward_risk": net_reward_risk},
            "trend_context": {"recovery_required": False},
        },
    }


def _closest_rejection(definition, *, net_reward_risk):
    return {
        "code": definition["code"],
        "name": definition["name"],
        "status": "net_rr_below_1_5",
        "net_reward_risk": net_reward_risk,
        "min_net_reward_risk": 1.5,
        "gap_to_min_net_reward_risk": round(1.5 - net_reward_risk, 4),
        "tencent_score": float(definition.get("tencent_score") or 0.0),
        "earnings_review_status": "not_reviewed",
        "actionable": False,
        "is_reference_only": True,
    }


def _earnings_screen(
    codes,
    *,
    blocked_codes=(),
    actual_loss_codes=(),
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
    selected_codes = [code for code in codes if code not in blocked]
    actual_blocked_codes = [code for code in codes if code in blocked]
    status_counts = {
        "no_forecast": sum(item["status"] == "no_forecast" for item in results),
        "loss_forecast": sum(
            item["status"] == "loss_forecast" for item in results
        ),
    }
    status_counts = {key: value for key, value in status_counts.items() if value}
    actual_status_counts = {
        "positive_profit": len(codes) - len(actual_loss),
        "actual_loss": len(actual_loss),
    }
    actual_status_counts = {
        key: value for key, value in actual_status_counts.items() if value
    }
    return {
        "status": "ok",
        "source": deep_check.EARNINGS_FORECAST_SOURCE,
        "actual_source": deep_check.EARNINGS_ACTUAL_SOURCE,
        "report_period": "20260630",
        "actual_report_period": "20260331",
        "screened_count": len(codes),
        "blocked_count": len(actual_blocked_codes),
        "selected_count": len(selected_codes),
        "blocked_codes": actual_blocked_codes,
        "selected_codes": selected_codes,
        "status_counts": status_counts,
        "actual_status_counts": actual_status_counts,
        "results": results,
    }


def test_deep_check_zero_remaining_returns_timeout_without_spawning():
    calls = []

    result = deep_check.run_public_candidate_deep_check(
        [_definition()],
        {"600000": _quote()},
        command_remaining_seconds=0,
        runner=lambda *_args, **_kwargs: calls.append("spawned"),
    )

    assert result == {
        "status": "technical_deep_check_timeout",
        "candidates": [],
    }
    assert calls == []


@pytest.mark.parametrize(
    ("remaining_seconds", "expected_timeout"),
    [(12.5, 12.5), (60.0, 35.0)],
)
def test_deep_check_uses_bounded_timeout_and_only_selected_quotes(
    remaining_seconds,
    expected_timeout,
):
    definitions = [_definition("600000"), _definition("000001")]
    quote_map = {
        "600000": _quote("600000", 10.0),
        "000001": _quote("000001", 12.0),
        "300001": _quote("300001", 8.0),
    }
    original_definitions = deepcopy(definitions)
    original_quote_map = deepcopy(quote_map)
    calls = []

    def fake_runner(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "candidates": [
                        {"code": "600000", "is_reference_only": True},
                        {"code": "000001", "is_reference_only": True},
                    ],
                }
            ),
            stderr="",
        )

    result = deep_check.run_public_candidate_deep_check(
        definitions,
        quote_map,
        command_remaining_seconds=remaining_seconds,
        runner=fake_runner,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["candidates"]] == ["600000", "000001"]
    assert len(calls) == 1
    call = calls[0]
    assert call["command"] == [
        sys.executable,
        "-m",
        "app.services.public_candidate_deep_check",
        "--worker",
    ]
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["check"] is False
    assert call["timeout"] == expected_timeout
    worker_input = json.loads(call["input"])
    assert worker_input["definitions"] == definitions
    assert set(worker_input["quote_map"]) == {"600000", "000001"}
    assert definitions == original_definitions
    assert quote_map == original_quote_map


def test_deep_check_timeout_terminates_sleeping_worker(monkeypatch):
    monkeypatch.setattr(
        deep_check,
        "_build_worker_command",
        lambda: [sys.executable, "-c", "import time; time.sleep(1)"],
    )
    started_at = time.monotonic()

    result = deep_check.run_public_candidate_deep_check(
        [_definition()],
        {"600000": _quote()},
        command_remaining_seconds=0.05,
    )

    assert result == {
        "status": "technical_deep_check_timeout",
        "candidates": [],
    }
    assert time.monotonic() - started_at < 0.5


@pytest.mark.parametrize(
    ("definitions", "quote_map", "expected_error"),
    [
        ("invalid", {}, "definitions_invalid"),
        ([_definition(f"600{index:03d}") for index in range(9)], {}, "too_many_candidates"),
        ([_definition(), _definition()], {"600000": _quote()}, "duplicate_code"),
        ([_definition("ABC")], {"ABC": {}}, "invalid_code"),
        ([_definition()], [], "quote_map_invalid"),
        ([_definition()], {}, "quote_missing"),
        ([_definition()], {"600000": None}, "quote_invalid"),
    ],
)
def test_deep_check_rejects_invalid_input_without_spawning(
    definitions,
    quote_map,
    expected_error,
):
    calls = []

    result = deep_check.run_public_candidate_deep_check(
        definitions,
        quote_map,
        command_remaining_seconds=35,
        runner=lambda *_args, **_kwargs: calls.append("spawned"),
    )

    assert result == {
        "status": "technical_deep_check_invalid_input",
        "candidates": [],
        "error_type": expected_error,
    }
    assert calls == []


@pytest.mark.parametrize(
    ("completed", "expected_error"),
    [
        (SimpleNamespace(returncode=2, stdout="", stderr="failed"), "WorkerProcessError"),
        (SimpleNamespace(returncode=0, stdout="", stderr=""), "InvalidWorkerOutput"),
        (SimpleNamespace(returncode=0, stdout="[]", stderr=""), "InvalidWorkerPayload"),
        (
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"status": "ok", "candidates": "invalid"}),
                stderr="",
            ),
            "InvalidWorkerCandidates",
        ),
        (
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"status": "ok", "candidates": [{"code": "000001"}]}
                ),
                stderr="",
            ),
            "WorkerCandidateMismatch",
        ),
    ],
)
def test_deep_check_normalizes_invalid_worker_results(completed, expected_error):
    result = deep_check.run_public_candidate_deep_check(
        [_definition()],
        {"600000": _quote()},
        command_remaining_seconds=35,
        runner=lambda *_args, **_kwargs: completed,
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": expected_error,
    }


def test_worker_payload_calls_candidate_builder_once_with_research_only_arguments():
    payload = {
        "definitions": [_definition()],
        "quote_map": {"600000": _quote()},
    }
    original_payload = deepcopy(payload)
    calls = []

    def fake_builder(definitions, **kwargs):
        calls.append({"definitions": definitions, **kwargs})
        return [{"code": "600000", "is_reference_only": True}]

    result = deep_check._run_worker_payload(payload, candidate_builder=fake_builder)

    assert result == {
        "status": "ok",
        "candidates": [{"code": "600000", "is_reference_only": True}],
    }
    assert len(calls) == 1
    assert calls[0]["cash"] is None
    assert calls[0]["buy_lot_size"] == 100
    assert calls[0]["holding_themes"] == set()
    assert calls[0]["allow_reference_price_plan"] is True
    assert calls[0]["quote_snapshots"] == payload["quote_map"]
    assert payload == original_payload


def test_worker_payload_reuses_quote_map_with_real_candidate_builder(monkeypatch):
    import app.services.holdings_cli as holdings_cli

    history_calls = []
    monkeypatch.setattr(
        holdings_cli,
        "fetch_tencent_quote_sync",
        lambda _code: (_ for _ in ()).throw(
            AssertionError("worker must reuse the verified batch quote")
        ),
    )
    monkeypatch.setattr(
        holdings_cli,
        "assess_cn_quote_freshness",
        lambda _snapshot: {"actionable": False, "status": "off_session"},
    )
    monkeypatch.setattr(
        holdings_cli,
        "fetch_tencent_daily_bars_sync",
        lambda code: (
            history_calls.append(code)
            or {
                "ok": False,
                "status": "history_unavailable",
                "reason": "deterministic unit-test boundary",
            }
        ),
    )
    monkeypatch.setattr(
        holdings_cli,
        "fetch_cn_dividend_calendar_sync",
        lambda code: {
            "ok": True,
            "code": code,
            "status": "no_upcoming_corporate_action",
            "price_plan_adjustment_required": False,
        },
    )

    result = deep_check._run_worker_payload(
        {
            "definitions": [_definition()],
            "quote_map": {"600000": _quote()},
        },
        candidate_builder=holdings_cli._build_opportunity_candidates,
    )

    assert result["status"] == "ok"
    assert result["candidates"][0]["quote"]["price"] == 10.0
    assert result["candidates"][0]["quote"]["source"] == "tencent_batch_quotes"
    assert result["candidates"][0]["guarded_price_plan"]["status"] == "history_unavailable"
    assert history_calls == ["600000"]


def test_technical_funnel_accepts_160_candidates_and_uses_one_bounded_worker():
    definitions = [
        {
            **_definition(f"{600000 + index:06d}"),
            "tencent_score": 0.5,
            "tencent_one_lot_amount": 1000.0,
        }
        for index in range(160)
    ]
    quote_map = {
        definition["code"]: _quote(definition["code"])
        for definition in definitions
    }
    selected_codes = [definitions[3]["code"], definitions[97]["code"]]
    rejection_definitions = [
        definitions[index] for index in (0, 1, 2, 4, 5)
    ]
    closest_rejections = [
        _closest_rejection(
            definition,
            net_reward_risk=round(1.49 - index / 100, 4),
        )
        for index, definition in enumerate(rejection_definitions)
    ]
    calls = []

    def fake_runner(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "candidates": [{"code": code} for code in selected_codes],
                    "technical_screen": {
                        "status": "ok",
                        "screened_count": 160,
                        "passed_count": 2,
                        "selected_count": 2,
                        "selected_codes": selected_codes,
                        "status_counts": {"ok": 2, "net_rr_below_1_5": 158},
                        "closest_rejection_count": 5,
                        "closest_rejections": closest_rejections,
                    },
                    "earnings_screen": _earnings_screen(selected_codes),
                }
            ),
            stderr="",
        )

    result = deep_check.run_public_candidate_technical_funnel(
        definitions,
        quote_map,
        benchmark_trade_date="2026-07-17",
        command_remaining_seconds=80,
        runner=fake_runner,
    )

    assert result["status"] == "ok"
    assert result["technical_screen"]["screened_count"] == 160
    assert result["earnings_screen"]["selected_codes"] == selected_codes
    assert [item["code"] for item in result["candidates"]] == selected_codes
    assert len(calls) == 1
    assert calls[0]["timeout"] == deep_check.TECHNICAL_FUNNEL_TIMEOUT_SECONDS
    worker_input = json.loads(calls[0]["input"])
    assert worker_input["mode"] == "technical_funnel"
    assert worker_input["benchmark_trade_date"] == "2026-07-17"
    assert len(worker_input["definitions"]) == 160
    assert set(worker_input["quote_map"]) == set(quote_map)


def test_technical_funnel_worker_screens_all_and_builds_only_top_eight():
    definitions = [
        {
            **_definition(f"{600000 + index:06d}"),
            "tencent_score": round(0.2 + index / 100, 4),
            "tencent_one_lot_amount": 1000.0 + index,
        }
        for index in range(12)
    ]
    quote_map = {
        definition["code"]: _quote(definition["code"])
        for definition in definitions
    }
    screened_codes = []
    builder_calls = []

    def fake_screener(definition, quote):
        code = definition["code"]
        screened_codes.append(code)
        index = int(code) - 600000
        return _screen_result(code, net_reward_risk=1.5 + index / 10)

    def fake_builder(selected_definitions, **kwargs):
        builder_calls.append(
            {"definitions": deepcopy(selected_definitions), **deepcopy(kwargs)}
        )
        return [{"code": item["code"]} for item in selected_definitions]

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "quote_map": quote_map,
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=fake_screener,
        earnings_screener=lambda codes, **_kwargs: _earnings_screen(codes),
        candidate_builder=fake_builder,
    )

    expected_codes = [f"{600000 + index:06d}" for index in range(11, 3, -1)]
    assert result["status"] == "ok"
    assert set(screened_codes) == set(quote_map)
    assert len(screened_codes) == 12
    assert result["technical_screen"] == {
        "status": "ok",
        "screened_count": 12,
        "passed_count": 12,
        "selected_count": 8,
        "selected_codes": expected_codes,
        "status_counts": {"ok": 12},
        "closest_rejection_count": 0,
        "closest_rejections": [],
    }
    assert result["earnings_screen"]["screened_count"] == 8
    assert result["earnings_screen"]["blocked_count"] == 0
    assert result["earnings_screen"]["selected_codes"] == expected_codes
    assert [item["code"] for item in result["candidates"]] == expected_codes
    assert len(builder_calls) == 1
    assert [item["code"] for item in builder_calls[0]["definitions"]] == expected_codes
    assert set(builder_calls[0]["technical_plan_snapshots"]) == set(expected_codes)
    assert builder_calls[0]["cash"] is None
    assert builder_calls[0]["allow_reference_price_plan"] is True


def test_technical_funnel_worker_retains_closest_net_rr_rejections_for_audit():
    definitions = [
        {
            **_definition(f"{600000 + index:06d}"),
            "tencent_score": round(0.8 - index / 100, 4),
            "tencent_one_lot_amount": 1000.0 + index,
        }
        for index in range(8)
    ]
    quote_map = {
        definition["code"]: _quote(definition["code"])
        for definition in definitions
    }
    rejected_ratios = [1.49, 1.45, 1.3, 1.2, 0.9, 0.5]
    earnings_calls = []

    def fake_screener(definition, _quote_value):
        index = int(definition["code"]) - 600000
        if index < len(rejected_ratios):
            return _screen_result(
                definition["code"],
                net_reward_risk=rejected_ratios[index],
                status="net_rr_below_1_5",
                passed=False,
            )
        return _screen_result(
            definition["code"],
            net_reward_risk=1.6 + index / 100,
        )

    def fake_earnings(codes, **_kwargs):
        earnings_calls.append(list(codes))
        return _earnings_screen(codes)

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "quote_map": quote_map,
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=fake_screener,
        earnings_screener=fake_earnings,
        candidate_builder=lambda selected, **_kwargs: [
            {"code": item["code"]} for item in selected
        ],
    )

    technical_screen = result["technical_screen"]
    assert technical_screen["closest_rejection_count"] == 5
    assert [
        item["code"] for item in technical_screen["closest_rejections"]
    ] == ["600000", "600001", "600002", "600003", "600004"]
    assert technical_screen["closest_rejections"][0] == _closest_rejection(
        definitions[0],
        net_reward_risk=1.49,
    )
    assert all(
        item["actionable"] is False
        and item["is_reference_only"] is True
        and item["earnings_review_status"] == "not_reviewed"
        for item in technical_screen["closest_rejections"]
    )
    assert earnings_calls == [["600007", "600006"]]


def test_real_technical_screen_falls_back_to_fee_aware_pullback_plan(monkeypatch):
    import app.services.holding_price_guardrails as guardrails
    import app.services.tencent_quote_service as quote_service

    bars = [
        {
            "date": f"2026-05-{index + 1:02d}",
            "open": 10.0,
            "close": 10.0,
            "high": 10.1,
            "low": 9.9,
        }
        for index in range(60)
    ]
    monkeypatch.setattr(
        quote_service,
        "fetch_tencent_daily_bars_sync",
        lambda _code: {"ok": True, "status": "ok", "bars": bars},
    )
    monkeypatch.setattr(
        quote_service,
        "merge_tencent_quote_into_bars",
        lambda history, _quote: {
            "ok": True,
            "bars": history,
            "merge_action": "replace",
        },
    )
    monkeypatch.setattr(
        guardrails,
        "build_technical_price_plan",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "actionable": True,
            "suggested_buy_price": 10.0,
            "stop_loss_price": 9.5,
            "target_price": 10.4,
            "failed_gates": [],
            "trend_context": {"recovery_required": False},
        },
    )
    monkeypatch.setattr(
        guardrails,
        "build_pullback_price_plan",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "actionable": True,
            "entry_strategy": "pullback",
            "suggested_buy_price": 9.6,
            "stop_loss_price": 9.0,
            "target_price": 11.5,
            "failed_gates": [],
            "trend_context": {"recovery_required": False},
        },
    )

    result = deep_check._screen_candidate_technical_plan(
        _definition(),
        _quote(close=10.0),
    )

    assert result["status"] == "ok"
    assert result["passed"] is True
    assert result["guarded_price_plan"]["entry_strategy"] == "pullback"
    assert result["guarded_price_plan"]["fee_aware_trade"]["net_reward_risk"] >= 1.5
    assert result["guarded_price_plan"]["alternative_breakout_plan"]["status"] == (
        "net_rr_below_1_5"
    )


def test_technical_funnel_worker_fails_closed_when_any_history_fetch_fails():
    definitions = [
        {
            **_definition("600000"),
            "tencent_score": 0.8,
            "tencent_one_lot_amount": 1000.0,
        },
        {
            **_definition("600001"),
            "tencent_score": 0.7,
            "tencent_one_lot_amount": 1100.0,
        },
    ]
    builder_calls = []

    def fake_screener(definition, quote):
        if definition["code"] == "600001":
            return {
                "code": "600001",
                "status": "fetch_error",
                "passed": False,
                "fatal_error": False,
                "guarded_price_plan": {
                    "status": "fetch_error",
                    "actionable": False,
                },
            }
        return _screen_result("600000")

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "benchmark_trade_date": "2026-07-17",
            "quote_map": {
                definition["code"]: _quote(definition["code"])
                for definition in definitions
            },
        },
        technical_screener=fake_screener,
        candidate_builder=lambda *args, **kwargs: builder_calls.append((args, kwargs)),
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "TechnicalHistoryFetchError",
    }
    assert builder_calls == []


def test_technical_funnel_worker_excludes_isolated_history_fetch_error():
    definitions = [_definition(f"{600000 + index:06d}") for index in range(12)]
    quote_map = {
        definition["code"]: _quote(definition["code"])
        for definition in definitions
    }

    def fake_screener(definition, _quote_value):
        if definition["code"] == "600011":
            return _screen_result(
                definition["code"],
                status="fetch_error",
                passed=False,
            )
        return _screen_result(
            definition["code"],
            net_reward_risk=1.0,
            status="net_rr_below_1_5",
            passed=False,
        )

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "benchmark_trade_date": "2026-07-17",
            "quote_map": quote_map,
        },
        technical_screener=fake_screener,
        candidate_builder=lambda *_args, **_kwargs: pytest.fail(
            "no technical survivor should reach the candidate builder"
        ),
    )

    assert result["status"] == "ok"
    assert result["candidates"] == []
    assert result["technical_screen"]["screened_count"] == 12
    assert result["technical_screen"]["status_counts"] == {
        "fetch_error": 1,
        "net_rr_below_1_5": 11,
    }


def test_technical_funnel_worker_caps_parallel_history_fetches_at_six(
    monkeypatch,
):
    definitions = [_definition(f"{600000 + index:06d}") for index in range(9)]
    quote_map = {
        definition["code"]: _quote(definition["code"])
        for definition in definitions
    }
    executor_calls = []

    class RecordingExecutor:
        def __init__(self, *, max_workers):
            executor_calls.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def map(self, function, values):
            return map(function, values)

    monkeypatch.setattr(deep_check, "ThreadPoolExecutor", RecordingExecutor)

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "benchmark_trade_date": "2026-07-17",
            "quote_map": quote_map,
        },
        technical_screener=lambda definition, _quote_value: _screen_result(
            definition["code"],
            net_reward_risk=1.0,
            status="net_rr_below_1_5",
            passed=False,
        ),
        candidate_builder=lambda *_args, **_kwargs: pytest.fail(
            "no technical survivor should reach the candidate builder"
        ),
    )

    assert result["status"] == "ok"
    assert result["technical_screen"]["screened_count"] == 9
    assert executor_calls == [6]


@pytest.mark.parametrize(
    "screen_result",
    [
        _screen_result("600000", status="ok", passed=False),
        {
            **_screen_result(
                "600000",
                status="net_rr_below_1_5",
                passed=False,
            ),
            "guarded_price_plan": {
                "status": "net_rr_below_1_5",
                "actionable": True,
                "fee_aware_trade": {"net_reward_risk": 1.0},
            },
        },
        {
            "code": "600000",
            "status": "fetch_error",
            "passed": False,
            "fatal_error": True,
            "guarded_price_plan": {
                "status": "fetch_error",
                "actionable": False,
            },
        },
    ],
)
def test_technical_funnel_worker_rejects_inconsistent_screen_result(
    screen_result,
):
    builder_calls = []

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": [_definition()],
            "benchmark_trade_date": "2026-07-17",
            "quote_map": {"600000": _quote()},
        },
        technical_screener=lambda _definition, _quote: screen_result,
        candidate_builder=lambda *args, **kwargs: builder_calls.append((args, kwargs)),
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "TechnicalScreenError",
    }
    assert builder_calls == []


def test_technical_funnel_worker_filters_loss_forecast_before_builder():
    definitions = [
        {
            **_definition(code),
            "tencent_score": score,
            "tencent_one_lot_amount": one_lot_amount,
        }
        for code, score, one_lot_amount in (
            ("688599", 0.9, 1233.0),
            ("002165", 0.8, 573.0),
            ("300113", 0.7, 1701.0),
        )
    ]
    builder_calls = []

    def fake_builder(selected_definitions, **kwargs):
        builder_calls.append(
            {"definitions": deepcopy(selected_definitions), **deepcopy(kwargs)}
        )
        return [{"code": item["code"]} for item in selected_definitions]

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "quote_map": {
                definition["code"]: _quote(definition["code"])
                for definition in definitions
            },
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=lambda definition, _quote: _screen_result(
            definition["code"]
        ),
        earnings_screener=lambda codes, **_kwargs: _earnings_screen(
            codes,
            blocked_codes=("688599", "002165"),
        ),
        candidate_builder=fake_builder,
    )

    assert result["status"] == "ok"
    assert result["technical_screen"]["selected_codes"] == [
        "688599",
        "002165",
        "300113",
    ]
    assert result["earnings_screen"]["blocked_codes"] == [
        "688599",
        "002165",
    ]
    assert result["earnings_screen"]["selected_codes"] == ["300113"]
    assert [item["code"] for item in result["candidates"]] == ["300113"]
    assert len(builder_calls) == 1
    assert [
        item["code"] for item in builder_calls[0]["definitions"]
    ] == ["300113"]
    assert set(builder_calls[0]["technical_plan_snapshots"]) == {"300113"}


def test_technical_funnel_worker_filters_latest_actual_loss_before_builder():
    definitions = [
        {
            **_definition(code),
            "tencent_score": score,
            "tencent_one_lot_amount": 1_000.0,
        }
        for code, score in (("300113", 0.9), ("600000", 0.8))
    ]
    builder_calls = []

    def fake_builder(selected_definitions, **kwargs):
        builder_calls.append(deepcopy(selected_definitions))
        return [{"code": item["code"]} for item in selected_definitions]

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": definitions,
            "quote_map": {
                definition["code"]: _quote(definition["code"])
                for definition in definitions
            },
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=lambda definition, _quote: _screen_result(
            definition["code"]
        ),
        earnings_screener=lambda codes, **_kwargs: _earnings_screen(
            codes,
            actual_loss_codes=("300113",),
        ),
        candidate_builder=fake_builder,
    )

    assert result["earnings_screen"]["blocked_codes"] == ["300113"]
    assert result["earnings_screen"]["selected_codes"] == ["600000"]
    assert [item["code"] for item in result["candidates"]] == ["600000"]
    assert [[item["code"] for item in call] for call in builder_calls] == [
        ["600000"]
    ]


def test_technical_funnel_worker_fails_closed_when_earnings_source_fails():
    builder_calls = []

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": [_definition()],
            "quote_map": {"600000": _quote()},
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=lambda definition, _quote: _screen_result(
            definition["code"]
        ),
        earnings_screener=lambda _codes, **_kwargs: {
            "status": "earnings_forecast_unavailable",
            "source": deep_check.EARNINGS_FORECAST_SOURCE,
            "report_period": "20260630",
            "error_type": "TimeoutError",
            "results": [],
        },
        candidate_builder=lambda *args, **kwargs: builder_calls.append((args, kwargs)),
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "EarningsForecastFetchError",
    }
    assert builder_calls == []


def test_technical_funnel_worker_fails_closed_when_actual_earnings_source_fails():
    builder_calls = []

    result = deep_check._run_technical_funnel_worker_payload(
        {
            "definitions": [_definition()],
            "quote_map": {"600000": _quote()},
            "benchmark_trade_date": "2026-07-17",
        },
        technical_screener=lambda definition, _quote: _screen_result(
            definition["code"]
        ),
        earnings_screener=lambda _codes, **_kwargs: {
            "status": "earnings_actual_unavailable",
            "source": deep_check.EARNINGS_FORECAST_SOURCE,
            "actual_source": deep_check.EARNINGS_ACTUAL_SOURCE,
            "report_period": "20260630",
            "actual_report_period": "20260331",
            "error_type": "TimeoutError",
            "results": [],
        },
        candidate_builder=lambda *args, **kwargs: builder_calls.append((args, kwargs)),
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "EarningsActualFetchError",
    }
    assert builder_calls == []


def test_technical_funnel_parent_rejects_inconsistent_earnings_metadata():
    definition = _definition()

    def fake_runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "candidates": [{"code": "600000"}],
                    "technical_screen": {
                        "status": "ok",
                        "screened_count": 1,
                        "passed_count": 1,
                        "selected_count": 1,
                        "selected_codes": ["600000"],
                        "status_counts": {"ok": 1},
                        "closest_rejection_count": 0,
                        "closest_rejections": [],
                    },
                    "earnings_screen": {
                        **_earnings_screen(["600000"]),
                        "blocked_count": 1,
                    },
                }
            ),
            stderr="",
        )

    result = deep_check.run_public_candidate_technical_funnel(
        [definition],
        {"600000": _quote()},
        benchmark_trade_date="2026-07-17",
        command_remaining_seconds=50,
        runner=fake_runner,
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "InvalidEarningsScreenMetadata",
    }


def test_technical_funnel_parent_rejects_actionable_closest_rejection():
    definition = {
        **_definition(),
        "tencent_score": 0.5,
        "tencent_one_lot_amount": 1000.0,
    }
    closest_rejection = _closest_rejection(
        definition,
        net_reward_risk=1.49,
    )
    closest_rejection["actionable"] = True

    def fake_runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "candidates": [],
                    "technical_screen": {
                        "status": "ok",
                        "screened_count": 1,
                        "passed_count": 0,
                        "selected_count": 0,
                        "selected_codes": [],
                        "status_counts": {"net_rr_below_1_5": 1},
                        "closest_rejection_count": 1,
                        "closest_rejections": [closest_rejection],
                    },
                    "earnings_screen": _earnings_screen([]),
                }
            ),
            stderr="",
        )

    result = deep_check.run_public_candidate_technical_funnel(
        [definition],
        {"600000": _quote()},
        benchmark_trade_date="2026-07-17",
        command_remaining_seconds=50,
        runner=fake_runner,
    )

    assert result == {
        "status": "technical_deep_check_failed",
        "candidates": [],
        "error_type": "InvalidTechnicalScreenMetadata",
    }


def test_technical_funnel_rejects_more_than_160_candidates_without_spawning():
    definitions = [_definition(f"{600000 + index:06d}") for index in range(161)]
    calls = []

    result = deep_check.run_public_candidate_technical_funnel(
        definitions,
        {definition["code"]: _quote(definition["code"]) for definition in definitions},
        benchmark_trade_date="2026-07-17",
        command_remaining_seconds=50,
        runner=lambda *_args, **_kwargs: calls.append("spawned"),
    )

    assert result == {
        "status": "technical_deep_check_invalid_input",
        "candidates": [],
        "error_type": "too_many_candidates",
    }
    assert calls == []


def test_worker_main_emits_one_json_document(monkeypatch, capsys):
    monkeypatch.setattr(
        deep_check,
        "_run_worker_payload",
        lambda payload: {
            "status": "ok",
            "candidates": [{"code": payload["definitions"][0]["code"]}],
        },
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"definitions": [_definition()], "quote_map": {}})),
    )

    exit_code = deep_check._worker_main(["--worker"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "ok",
        "candidates": [{"code": "600000"}],
    }
    assert captured.out.count("\n") == 1
