from app.services.a_share_market_regime import (
    assess_a_share_market_breadth,
    assess_a_share_market_regime,
    combine_a_share_market_regimes,
)


def _index(code, pct_chg, trade_date="2026-07-13"):
    return {
        "code": code,
        "name": code,
        "pct_chg": pct_chg,
        "trade_date": trade_date,
        "source": "tencent",
    }


def _breadth_rows(*, advancers, decliners, unchanged=0, trade_date="2026-07-13"):
    rows = []
    for index in range(advancers):
        rows.append(
            {
                "code": f"600{index:03d}",
                "name": f"上涨{index}",
                "pct_chg": 1.0,
                "trade_date": trade_date,
            }
        )
    for index in range(decliners):
        rows.append(
            {
                "code": f"601{index:03d}",
                "name": f"下跌{index}",
                "pct_chg": -1.0,
                "trade_date": trade_date,
            }
        )
    for index in range(unchanged):
        rows.append(
            {
                "code": f"603{index:03d}",
                "name": f"平盘{index}",
                "pct_chg": 0.0,
                "trade_date": trade_date,
            }
        )
    return rows


def test_market_regime_blocks_new_positions_during_systemic_decline():
    result = assess_a_share_market_regime(
        [
            _index("sh000001", -2.06),
            _index("sz399001", -3.48),
            _index("sz399006", -3.10),
            _index("sh000688", -3.42),
        ],
        benchmark_trade_date="2026-07-13",
    )

    assert result["level"] == "red"
    assert result["new_position_allowed"] is False
    assert result["max_new_exposure_multiplier"] == 0.0
    assert result["severe_decline_count"] == 4
    assert result["average_pct_chg"] == -3.02


def test_market_regime_halves_exposure_during_moderate_weakness():
    result = assess_a_share_market_regime(
        [
            _index("sh000001", -0.8),
            _index("sz399001", -1.6),
            _index("sz399006", -1.2),
        ],
        benchmark_trade_date="2026-07-13",
    )

    assert result["level"] == "yellow"
    assert result["new_position_allowed"] is True
    assert result["max_new_exposure_multiplier"] == 0.5


def test_market_regime_keeps_full_external_cap_when_indices_are_stable():
    result = assess_a_share_market_regime(
        [
            _index("sh000001", 0.3),
            _index("sz399001", -0.2),
            _index("sz399006", 0.1),
        ],
        benchmark_trade_date="2026-07-13",
    )

    assert result["level"] == "green"
    assert result["new_position_allowed"] is True
    assert result["max_new_exposure_multiplier"] == 1.0


def test_market_regime_fails_closed_for_stale_or_incomplete_index_data():
    stale = assess_a_share_market_regime(
        [
            _index("sh000001", 0.3, "2026-07-10"),
            _index("sz399001", -0.2, "2026-07-10"),
            _index("sz399006", 0.1, "2026-07-10"),
        ],
        benchmark_trade_date="2026-07-13",
    )
    incomplete = assess_a_share_market_regime(
        [_index("sh000001", 0.3), _index("sz399001", -0.2)],
        benchmark_trade_date="2026-07-13",
    )

    assert stale["status"] == "stale_market_data"
    assert stale["new_position_allowed"] is False
    assert incomplete["status"] == "market_data_unavailable"
    assert incomplete["max_new_exposure_multiplier"] == 0.0


def test_market_breadth_turns_red_when_three_quarters_of_stocks_decline():
    result = assess_a_share_market_breadth(
        _breadth_rows(advancers=150, decliners=800, unchanged=50),
        benchmark_trade_date="2026-07-13",
    )

    assert result["status"] == "ok"
    assert result["level"] == "red"
    assert result["decliner_count"] == 800
    assert result["decliner_ratio_pct"] == 80.0
    assert result["max_new_exposure_multiplier"] == 0.0


def test_market_breadth_is_yellow_during_moderate_negative_breadth():
    result = assess_a_share_market_breadth(
        _breadth_rows(advancers=300, decliners=650, unchanged=50),
        benchmark_trade_date="2026-07-13",
    )

    assert result["level"] == "yellow"
    assert result["max_new_exposure_multiplier"] == 0.5


def test_market_breadth_detects_deep_declines_and_board_specific_limit_downs():
    rows = _breadth_rows(advancers=600, decliners=350, unchanged=50)
    for index in range(60):
        rows[index]["pct_chg"] = -8.0
    rows.extend(
        [
            {"code": "000001", "name": "主板", "pct_chg": -9.8, "trade_date": "2026-07-13"},
            {"code": "300001", "name": "创业板", "pct_chg": -19.8, "trade_date": "2026-07-13"},
            {"code": "688001", "name": "科创板", "pct_chg": -19.8, "trade_date": "2026-07-13"},
            {"code": "600001", "name": "ST样本", "pct_chg": -4.9, "trade_date": "2026-07-13"},
        ]
    )

    result = assess_a_share_market_breadth(rows, benchmark_trade_date="2026-07-13")

    assert result["level"] == "red"
    assert result["deep_decline_count"] == 63
    assert result["limit_down_like_count"] == 4
    assert result["risk_triggers"] == ["deep_decline_ratio"]


def test_market_breadth_explains_tail_risk_when_advancers_still_dominate():
    rows = _breadth_rows(advancers=800, decliners=170)
    rows.extend(
        {
            "code": f"002{index:03d}",
            "name": f"近跌停{index}",
            "pct_chg": -9.8,
            "trade_date": "2026-07-13",
        }
        for index in range(30)
    )

    result = assess_a_share_market_breadth(
        rows,
        benchmark_trade_date="2026-07-13",
    )

    assert result["level"] == "yellow"
    assert result["advancer_count"] == 800
    assert result["decliner_ratio_pct"] == 20.0
    assert result["limit_down_like_count"] == 30
    assert result["limit_down_like_ratio_pct"] == 3.0
    assert result["risk_triggers"] == [
        "deep_decline_ratio",
        "limit_down_like_count",
    ]
    assert result["reason"] == (
        "整体下跌比例未触发门槛，但个股深跌尾部风险偏高，新仓风险预算减半。"
    )


def test_market_breadth_marks_stale_and_small_universes_unavailable():
    stale = assess_a_share_market_breadth(
        _breadth_rows(advancers=300, decliners=700, trade_date="2026-07-10"),
        benchmark_trade_date="2026-07-13",
    )
    small = assess_a_share_market_breadth(
        _breadth_rows(advancers=100, decliners=100),
        benchmark_trade_date="2026-07-13",
    )

    assert stale["status"] == "stale_market_breadth"
    assert small["status"] == "market_breadth_unavailable"


def test_market_breadth_excludes_stale_suspended_quotes_when_current_universe_is_sufficient():
    rows = _breadth_rows(advancers=400, decliners=600)
    rows.extend(
        _breadth_rows(
            advancers=5,
            decliners=5,
            trade_date="2026-07-10",
        )
    )

    result = assess_a_share_market_breadth(rows, benchmark_trade_date="2026-07-13")

    assert result["status"] == "ok"
    assert result["universe_size"] == 1000
    assert result["excluded_stale_count"] == 10
    assert result["decliner_ratio_pct"] == 60.0


def test_combined_market_gate_uses_stricter_valid_component():
    index_green = assess_a_share_market_regime(
        [_index("sh000001", 0.3), _index("sz399001", -0.2), _index("sz399006", 0.1)],
        benchmark_trade_date="2026-07-13",
    )
    breadth_red = assess_a_share_market_breadth(
        _breadth_rows(advancers=150, decliners=800, unchanged=50),
        benchmark_trade_date="2026-07-13",
    )

    result = combine_a_share_market_regimes(index_green, breadth_red)

    assert result["level"] == "red"
    assert result["new_position_allowed"] is False
    assert result["max_new_exposure_multiplier"] == 0.0
    assert result["index_regime"]["level"] == "green"
    assert result["breadth_regime"]["level"] == "red"


def test_combined_market_gate_keeps_index_result_when_breadth_is_unavailable():
    index_green = assess_a_share_market_regime(
        [_index("sh000001", 0.3), _index("sz399001", -0.2), _index("sz399006", 0.1)],
        benchmark_trade_date="2026-07-13",
    )
    breadth_unavailable = assess_a_share_market_breadth(
        [],
        benchmark_trade_date="2026-07-13",
    )

    result = combine_a_share_market_regimes(index_green, breadth_unavailable)

    assert result["level"] == "green"
    assert result["new_position_allowed"] is True
    assert result["breadth_confirmation_required"] is True
