from datetime import datetime, timedelta, timezone

import pytest

from app.services.investment_policy import allocate_candidate_portfolio
from app.services.portfolio_diversification_service import (
    PortfolioDiversificationService,
    calculate_return_correlation,
    taxonomy_fallback_correlation,
)


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)


def candidate(
    code,
    *,
    rank=1,
    rank_score=100,
    entry=10.0,
    stop=9.0,
    quantity=1_000,
    theme="数字科技",
    sector="信息技术",
    industry="计算机设备",
):
    return {
        "code": code,
        "rank": rank,
        "rank_score": rank_score,
        "objective_segment": theme,
        "provider_sector": sector,
        "industry": industry,
        "price_plan": {"entry_price": entry, "stop_price": stop},
        "position_sizing": {
            "status": "sized",
            "suggested_quantity": quantity,
        },
    }


def holding(
    code="600000",
    *,
    quantity=100,
    market_value=1_000.0,
    theme="数字科技",
    sector="信息技术",
    industry="计算机设备",
    quote_trade_at=None,
    valuation_phase="live_am",
    denominator=10_000.0,
):
    return {
        "code": code,
        "quantity": quantity,
        "market_value": market_value,
        "objective_segment": theme,
        "provider_sector": sector,
        "industry": industry,
        "quote_trade_at": quote_trade_at or (NOW - timedelta(minutes=1)).isoformat(),
        "valuation_phase": valuation_phase,
        "total_assets_denominator": denominator,
    }


def policy(**overrides):
    base = {
        "available_new_exposure_pct": 100,
        "total_new_position_loss_budget_pct": 100,
        "hard_single_symbol_cap_pct": 100,
        "theme_exposure_cap_pct": 35,
        "provider_sector_exposure_cap_pct": 40,
        "industry_exposure_cap_pct": 30,
        "pairwise_correlation_cap": 0.80,
    }
    base.update(overrides)
    return base


def bars_from_returns(returns, *, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    price = 100.0
    bars = [{"date": start.date().isoformat(), "close": price}]
    for offset, change in enumerate(returns, 1):
        price *= 1 + change
        bars.append(
            {
                "date": (start + timedelta(days=offset)).date().isoformat(),
                "close": price,
            }
        )
    return bars


class HistoryLoader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def __call__(self, code):
        self.calls.append(code)
        return {
            "ok": True,
            "source": "test",
            "adjust": "qfq",
            "bars": self.rows[code],
        }


@pytest.mark.asyncio
async def test_allocates_in_stable_rank_score_code_order_and_seeds_exposure_ledgers():
    base = [0.01 if i % 2 else -0.01 for i in range(60)]
    histories = {
        "600000": bars_from_returns(base),
        "600001": bars_from_returns([-value for value in base]),
        "600002": bars_from_returns([0.005 if i % 4 < 2 else -0.005 for i in range(60)]),
    }
    service = PortfolioDiversificationService(history_loader=HistoryLoader(histories))

    result = await service.allocate(
        [
            candidate("600002", rank=2, rank_score=99, industry="软件开发"),
            candidate("600001", rank=1, rank_score=80, industry="通信设备"),
        ],
        holdings=[holding()],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="live_am",
        as_of=NOW,
    )

    assert [item["code"] for item in result["allocations"]] == ["600001", "600002"]
    first = result["allocations"][0]
    assert first["status"] == "allocated"
    assert first["quantity"] == 200
    assert first["exposure_audit"]["theme"]["before_amount"] == 1_000
    assert first["exposure_audit"]["theme"]["after_amount"] == 3_000
    assert first["reason_codes"] == ["allocated"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "audit_key", "holding_value", "candidate_value", "expected_quantity", "expected_pct"),
    [
        ("objective_segment", "theme", "数字科技", "数字科技", 100, 30.0),
        ("provider_sector", "provider_sector", "信息技术", "信息技术", 200, 40.0),
        ("industry", "industry", "计算机设备", "计算机设备", 100, 30.0),
    ],
)
async def test_reduces_to_largest_legal_board_lot_for_each_taxonomy_cap(
    field, audit_key, holding_value, candidate_value, expected_quantity, expected_pct
):
    existing = holding(
        market_value=2_000,
        theme="持仓主题",
        sector="持仓板块",
        industry="持仓行业",
        valuation_phase="pre_open",
    )
    existing[field] = holding_value
    item = candidate(
        "600001",
        quantity=1_000,
        theme="候选主题",
        sector="候选板块",
        industry="候选行业",
    )
    item[field] = candidate_value
    base = [0.01, -0.01] * 30
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader(
            {
                "600000": bars_from_returns(base),
                "600001": bars_from_returns([-value for value in base]),
            }
        )
    )

    result = await service.allocate(
        [item],
        holdings=[existing],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "allocated"
    assert allocation["quantity"] == expected_quantity
    assert allocation["exposure_audit"][audit_key]["after_pct"] == expected_pct


@pytest.mark.asyncio
async def test_exact_correlation_cap_allows_point_eight_and_blocks_above_it():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    exact = [0.8 * left + 0.6 * right for left, right in zip(base, orthogonal)]
    high = list(base)
    loader = HistoryLoader(
        {
            "600000": bars_from_returns(base),
            "600001": bars_from_returns(exact),
            "600002": bars_from_returns(high),
        }
    )
    service = PortfolioDiversificationService(history_loader=loader)

    result = await service.allocate(
        [
            candidate("600001", rank=1, industry="软件开发", theme="新能源"),
            candidate("600002", rank=2, industry="通信设备", theme="高端装备"),
        ],
        holdings=[
            holding(
                theme="数字科技",
                industry="计算机设备",
                denominator=100_000,
                valuation_phase="pre_open",
            )
        ],
        total_assets=100_000,
        available_cash=100_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["allocations"][0]["status"] == "allocated"
    assert result["allocations"][1]["status"] == "wait"
    assert result["allocations"][1]["reason_codes"] == ["correlation_limit"]
    assert result["allocations"][1]["correlation_audit"]["compared_symbols"] == [
        "600000",
        "600001",
    ]
    assert result["allocations"][1]["correlation_audit"]["blocking_pair"]["value"] > 0.80


@pytest.mark.asyncio
async def test_live_allocation_excludes_current_uncompleted_daily_bar():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    exact = [0.8 * left + 0.6 * right for left, right in zip(base, orthogonal)]
    start = NOW - timedelta(days=61)
    loader = HistoryLoader(
        {
            "600000": bars_from_returns(base + [0.1], start=start),
            "600001": bars_from_returns(exact + [0.1], start=start),
        }
    )
    service = PortfolioDiversificationService(history_loader=loader)

    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[holding(valuation_phase="live_am")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="live_am",
        as_of=NOW,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "allocated"
    assert allocation["correlation_value"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_non_qfq_history_uses_taxonomy_fallback_instead_of_empirical_result():
    rows = bars_from_returns([0.01, -0.01] * 30)

    async def non_qfq_loader(code):
        return {"ok": True, "adjust": "hfq", "bars": rows}

    service = PortfolioDiversificationService(history_loader=non_qfq_loader)
    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[holding(valuation_phase="pre_open")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "allocated"
    assert allocation["correlation_basis"] == "taxonomy_fallback"
    assert allocation["correlation_audit"]["comparisons"][0][
        "empirical_unavailable_reason"
    ] == "non_qfq_history"


@pytest.mark.asyncio
async def test_post_close_can_use_same_day_final_bar():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    exact = [0.8 * left + 0.6 * right for left, right in zip(base, orthogonal)]
    start = NOW - timedelta(days=61)
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader(
            {
                "600000": bars_from_returns(base + [0.1], start=start),
                "600001": bars_from_returns(exact + [0.1], start=start),
            }
        )
    )
    post_close = NOW.replace(hour=15, minute=1)

    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[
            holding(
                quote_trade_at=NOW.replace(hour=15).isoformat(),
                valuation_phase="post_close",
            )
        ],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="post_close",
        as_of=post_close,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "wait"
    assert allocation["reason_codes"] == ["correlation_limit"]
    assert allocation["correlation_value"] > 0.8


@pytest.mark.asyncio
async def test_closed_day_uses_last_available_completed_bar():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    exact = [0.8 * left + 0.6 * right for left, right in zip(base, orthogonal)]
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader(
            {
                "600000": bars_from_returns(base),
                "600001": bars_from_returns(exact),
            }
        )
    )

    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[holding(valuation_phase="post_close")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="closed_day",
        as_of=datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI),
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "allocated"
    assert allocation["correlation_value"] == pytest.approx(0.8)


def test_empirical_correlation_uses_last_60_overlaps_and_retains_zero_returns():
    left_returns = ([0.01, -0.01, 0.01, -0.01, 0.0] * 12)
    right_returns = ([0.02, -0.02, 0.02, -0.02, 0.0] * 12)

    result = calculate_return_correlation(
        bars_from_returns(left_returns),
        bars_from_returns(right_returns),
    )

    assert result["basis"] == "empirical_qfq_60_sessions"
    assert result["overlap"] == 60
    assert result["zero_return_ratio_left"] == pytest.approx(12 / 60)
    assert result["value"] == pytest.approx(1.0)


def test_empirical_correlation_is_unavailable_below_40_overlaps():
    result = calculate_return_correlation(
        bars_from_returns([0.01, -0.01] * 19),
        bars_from_returns([0.02, -0.02] * 19),
    )

    assert result["basis"] == "unavailable"
    assert result["reason"] == "insufficient_overlap"
    assert result["overlap"] == 38


def test_empirical_correlation_is_unavailable_above_twenty_percent_zero_returns():
    returns = [0.01] + [0.0] * 13 + [0.01, -0.01] * 23 + [0.01]
    result = calculate_return_correlation(
        bars_from_returns(returns),
        bars_from_returns([0.02, -0.02] * 30 + [0.01]),
    )

    assert result["basis"] == "unavailable"
    assert result["reason"] == "excessive_zero_returns"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ({"industry": "软件", "objective_segment": "A"}, {"industry": "软件", "objective_segment": "B"}, 1.0),
        ({"industry": "软件", "objective_segment": "A"}, {"industry": "通信", "objective_segment": "A"}, 0.85),
        ({"industry": "软件", "objective_segment": "A"}, {"industry": "通信", "objective_segment": "B"}, 0.50),
    ],
)
def test_taxonomy_fallback_values_are_exact(left, right, expected):
    result = taxonomy_fallback_correlation(left, right)

    assert result == {"basis": "taxonomy_fallback", "overlap": 0, "value": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"market_value": None}, "holding_valuation_missing"),
        ({"provider_sector": None}, "holding_taxonomy_missing"),
        ({"industry": None}, "holding_taxonomy_missing"),
        ({"quote_trade_at": None}, "holding_quote_trade_at_missing"),
        ({"valuation_phase": None}, "holding_valuation_phase_missing"),
        ({"total_assets_denominator": None}, "holding_denominator_missing"),
    ],
)
async def test_positive_holding_missing_required_audit_blocks_all_allocations(patch, reason):
    current = holding(valuation_phase="pre_open")
    current.update(patch)
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001")],
        holdings=[current],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["allocations"][0]["status"] == "wait"
    assert result["allocations"][0]["reason_codes"] == [reason]
    assert result["holding_valuation_audit"][0]["valid"] is False


@pytest.mark.asyncio
async def test_stale_live_holding_quote_blocks_every_candidate():
    stale = holding(quote_trade_at=(NOW - timedelta(minutes=5, seconds=1)).isoformat())
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001"), candidate("600002", rank=2)],
        holdings=[stale],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="live_am",
        as_of=NOW,
    )

    assert [item["reason_codes"] for item in result["allocations"]] == [
        ["holding_quote_stale"],
        ["holding_quote_stale"],
    ]
    assert result["holding_valuation_audit"][0]["quote_age_seconds"] == 301
    assert len(result["allocations"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_phase", "valuation_phase"),
    [
        ("pre_open", "live_am"),
        ("live_am", "pre_open"),
        ("midday_break", "live_am"),
        ("live_pm", "midday_break"),
        ("post_close", "live_pm"),
    ],
)
async def test_holding_valuation_phase_mismatch_fails_closed(
    market_phase, valuation_phase
):
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001")],
        holdings=[holding(valuation_phase=valuation_phase)],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase=market_phase,
        as_of=NOW,
    )

    audit = result["holding_valuation_audit"][0]
    assert result["allocations"][0]["reason_codes"] == [
        "holding_valuation_phase_mismatch"
    ]
    assert audit["expected_valuation_phases"]
    assert audit["valid"] is False


@pytest.mark.asyncio
async def test_naive_a_share_quote_time_is_interpreted_as_shanghai_time():
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001")],
        holdings=[holding(quote_trade_at="2026-07-22T09:59:00")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="live_am",
        as_of=NOW,
    )

    audit = result["holding_valuation_audit"][0]
    assert audit["quote_trade_at"] == "2026-07-22T09:59:00+08:00"
    assert audit["quote_age_seconds"] == 60
    assert "holding_quote_stale" not in audit["reason_codes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"market_value": 0}, "holding_valuation_invalid"),
        ({"market_value": -1}, "holding_valuation_invalid"),
        ({"total_assets_denominator": 9_999}, "holding_denominator_mismatch"),
    ],
)
async def test_non_positive_valuation_or_mismatched_denominator_blocks_allocation(
    patch, reason
):
    current = holding(valuation_phase="pre_open")
    current.update(patch)
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001")],
        holdings=[current],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["allocations"][0]["reason_codes"] == [reason]


@pytest.mark.asyncio
async def test_existing_same_symbol_is_included_in_pairwise_correlation_checks():
    base = [0.01, -0.01] * 30
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader({"600001": bars_from_returns(base)})
    )

    result = await service.allocate(
        [candidate("600001", quantity=1_000)],
        holdings=[
            holding(
                code="600001",
                market_value=2_000,
                valuation_phase="pre_open",
            )
        ],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(hard_single_symbol_cap_pct=30),
        market_phase="pre_open",
        as_of=NOW,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "wait"
    assert allocation["reason_codes"] == ["correlation_limit"]
    assert allocation["correlation_audit"]["compared_symbols"] == ["600001"]


@pytest.mark.asyncio
async def test_new_symbol_allocation_rechecks_hard_single_symbol_cap():
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001", quantity=1_000)],
        holdings=[],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(hard_single_symbol_cap_pct=30),
        market_phase="pre_open",
        as_of=NOW,
    )

    allocation = result["allocations"][0]
    assert allocation["status"] == "allocated"
    assert allocation["quantity"] == 300
    assert allocation["symbol_exposure_audit"]["after_pct"] == 30.0


@pytest.mark.asyncio
async def test_correlation_value_immediately_above_cap_is_blocked(monkeypatch):
    monkeypatch.setattr(
        "app.services.portfolio_diversification_service.calculate_return_correlation",
        lambda *_args: {
            "basis": "empirical_qfq_60_sessions",
            "overlap": 60,
            "value": 0.800000000001,
        },
    )
    rows = bars_from_returns([0.01, -0.01] * 30)
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader({"600000": rows, "600001": rows})
    )

    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[holding(valuation_phase="pre_open")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["allocations"][0]["reason_codes"] == ["correlation_limit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", "ABC", "6000017"])
async def test_invalid_candidate_code_cannot_be_allocated(code):
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate(code)],
        holdings=[],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["allocations"][0]["reason_codes"] == ["candidate_code_invalid"]


@pytest.mark.asyncio
async def test_missing_rank_orders_by_score_then_code_independent_of_input_order():
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))
    first = candidate("600002", rank=None, rank_score=90, theme="新能源", sector="工业", industry="电池")
    second = candidate("600001", rank=None, rank_score=90, theme="高端装备", sector="原材料", industry="机械设备")
    kwargs = {
        "holdings": [],
        "total_assets": 100_000,
        "available_cash": 100_000,
        "policy": policy(),
        "market_phase": "pre_open",
        "as_of": NOW,
    }

    forward = await service.allocate([first, second], **kwargs)
    reverse = await service.allocate([second, first], **kwargs)

    assert [item["code"] for item in forward["allocations"]] == ["600001", "600002"]
    assert [item["code"] for item in reverse["allocations"]] == ["600001", "600002"]


@pytest.mark.asyncio
async def test_utc_as_of_uses_shanghai_date_for_completed_session_cutoff():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    exact = [0.8 * left + 0.6 * right for left, right in zip(base, orthogonal)]
    start = datetime(2026, 5, 22, tzinfo=timezone.utc)
    service = PortfolioDiversificationService(
        history_loader=HistoryLoader(
            {
                "600000": bars_from_returns(base, start=start),
                "600001": bars_from_returns(exact, start=start),
            }
        )
    )

    result = await service.allocate(
        [candidate("600001", theme="新能源", industry="软件开发")],
        holdings=[holding(valuation_phase="pre_open")],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(),
        market_phase="pre_open",
        as_of=datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc),
    )

    allocation = result["allocations"][0]
    assert allocation["correlation_basis"] == "empirical_qfq_60_sessions"
    assert allocation["correlation_overlap"] == 60


def test_correlation_calculation_does_not_round_before_threshold_check():
    base = [0.01, -0.01, 0.01, -0.01] * 15
    orthogonal = [0.01, 0.01, -0.01, -0.01] * 15
    slightly_high = [
        0.8000000000004 * left + 0.5999999999994667 * right
        for left, right in zip(base, orthogonal)
    ]

    result = calculate_return_correlation(
        bars_from_returns(base),
        bars_from_returns(slightly_high),
    )

    assert result["value"] > 0.80


@pytest.mark.asyncio
async def test_effective_diversification_caps_cannot_be_overridden_by_policy():
    malicious = policy(
        theme_exposure_cap_pct=100,
        provider_sector_exposure_cap_pct=100,
        industry_exposure_cap_pct=100,
        pairwise_correlation_cap=1,
    )
    service = PortfolioDiversificationService(history_loader=HistoryLoader({}))

    result = await service.allocate(
        [candidate("600001")],
        holdings=[],
        total_assets=10_000,
        available_cash=10_000,
        policy=malicious,
        market_phase="pre_open",
        as_of=NOW,
    )

    assert result["effective_limits"] == {
        "source": "decision_loop_v1_constants",
        "theme_exposure_cap_pct": 35.0,
        "provider_sector_exposure_cap_pct": 40.0,
        "industry_exposure_cap_pct": 30.0,
        "pairwise_correlation_cap": 0.8,
    }
    assert result["policy"]["theme_exposure_cap_pct"] == 35.0


def test_legacy_allocator_rounds_suggested_quantity_down_and_rechecks_hard_cap():
    result = allocate_candidate_portfolio(
        [candidate("600001", quantity=150)],
        total_assets=10_000,
        available_cash=10_000,
        policy=policy(
            available_new_exposure_pct=100,
            total_new_position_loss_budget_pct=100,
            hard_single_symbol_cap_pct=100,
        ),
    )

    assert result["allocations"][0]["quantity"] == 100


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("available_new_exposure_pct", "bad"),
        ("total_new_position_loss_budget_pct", object()),
        ("hard_single_symbol_cap_pct", float("nan")),
    ],
)
def test_legacy_allocator_malformed_policy_fails_closed_without_raising(key, value):
    malformed = policy()
    malformed[key] = value

    result = allocate_candidate_portfolio(
        [candidate("600001", quantity=150)],
        total_assets=10_000,
        available_cash=10_000,
        policy=malformed,
    )

    assert result["allocated_position_count"] == 0
    assert result["allocations"][0]["reason"] == "invalid_portfolio_policy"
