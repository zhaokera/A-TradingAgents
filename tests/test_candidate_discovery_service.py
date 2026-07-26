import pytest

from app.services.candidate_discovery_service import (
    discover_dynamic_candidate_universe,
    rank_dynamic_candidate_universe,
)


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection):
        return list(self.rows)


class _DB:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def _quote(
    code,
    *,
    close,
    pct_chg,
    amount,
    trade_date="2026-07-13",
    turnover_rate=1.0,
):
    return {
        "code": code,
        "close": close,
        "pct_chg": pct_chg,
        "amount": amount,
        "trade_date": trade_date,
        "turnover_rate": turnover_rate,
    }


def test_dynamic_ranking_filters_risk_and_limits_industry_concentration():
    basics = [
        {"code": "002966", "name": "苏州银行", "industry": "银行"},
        {"code": "601077", "name": "渝农商行", "industry": "银行"},
        {"code": "601328", "name": "交通银行", "industry": "银行"},
        {"code": "600123", "name": "兰花科创", "industry": "煤炭"},
        {"code": "002261", "name": "拓维信息", "industry": "软件"},
        {"code": "000938", "name": "紫光股份", "industry": "硬件"},
        {"code": "600900", "name": "长江电力", "industry": "电力"},
        {"code": "300001", "name": "高价样本", "industry": "软件"},
        {"code": "000001", "name": "*ST样本", "industry": "银行"},
        {"code": "830001", "name": "北交样本", "industry": "机械"},
    ]
    quotes = [
        _quote("002966", close=7.77, pct_chg=4.5, amount=900),
        _quote("601077", close=6.59, pct_chg=3.0, amount=800),
        _quote("601328", close=6.77, pct_chg=2.4, amount=1000),
        _quote("600123", close=5.78, pct_chg=1.9, amount=700),
        _quote("002261", close=33.0, pct_chg=6.0, amount=1200),
        _quote("000938", close=38.4, pct_chg=0.0, amount=1100, turnover_rate=12.0),
        _quote("600900", close=28.42, pct_chg=1.4, amount=600, trade_date="2026-07-10"),
        _quote("300001", close=120.0, pct_chg=1.0, amount=500),
        _quote("000001", close=8.0, pct_chg=1.0, amount=400),
        _quote("830001", close=6.0, pct_chg=1.0, amount=300),
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=8,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["definitions"]] == [
        "002966",
        "601077",
        "600123",
    ]
    assert result["definitions"][0]["theme"] == "industry:银行"
    assert result["definitions"][0]["discovery"]["one_lot_amount"] == 777.0
    assert result["rejection_counts"] == {
        "high_turnover": 1,
        "hot_move": 1,
        "industry_cap": 1,
        "special_treatment": 1,
        "stale_quote": 1,
        "unaffordable": 1,
        "unsupported_code": 1,
    }


def test_dynamic_ranking_balances_strength_and_pullback_buckets():
    rows = [
        ("600001", "强势一", "行业一", 4.8, 1200),
        ("600002", "强势二", "行业二", 4.2, 1100),
        ("600003", "强势三", "行业三", 3.6, 1000),
        ("600004", "强势四", "行业四", 2.8, 900),
        ("600005", "强势五", "行业五", 1.8, 800),
        ("600006", "强势六", "行业六", 0.8, 700),
        ("600007", "强势七", "行业七", 0.4, 600),
        ("600008", "强势八", "行业八", 0.1, 500),
        ("000001", "回撤一", "行业九", -0.2, 950),
        ("000002", "回撤二", "行业十", -0.6, 850),
        ("000003", "回撤三", "行业十一", -1.2, 750),
        ("000004", "回撤四", "行业十二", -2.0, 650),
    ]
    basics = [
        {"code": code, "name": name, "industry": industry}
        for code, name, industry, _, _ in rows
    ]
    quotes = [
        _quote(code, close=8.0, pct_chg=pct_chg, amount=amount)
        for code, _, _, pct_chg, amount in rows
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=8,
    )

    assert [
        item["discovery"]["selection_bucket"] for item in result["definitions"]
    ] == [
        "strength",
        "strength",
        "pullback",
        "strength",
        "strength",
        "pullback",
        "strength",
        "strength",
    ]
    assert [item["priority"] for item in result["definitions"]] == list(range(1, 9))
    assert result["eligible_bucket_counts"] == {"pullback": 4, "strength": 8}
    assert result["selected_bucket_counts"] == {"pullback": 2, "strength": 6}


@pytest.mark.parametrize(
    ("limit", "expected_buckets"),
    [
        (2, ["strength", "strength"]),
        (6, ["strength", "strength", "pullback", "strength", "strength", "strength"]),
        (
            12,
            [
                "strength",
                "strength",
                "pullback",
                "strength",
                "strength",
                "strength",
                "pullback",
                "strength",
                "strength",
                "pullback",
                "strength",
                "strength",
            ],
        ),
    ],
)
def test_dynamic_ranking_preserves_bucket_quota_for_non_default_limits(
    limit,
    expected_buckets,
):
    strength_rows = [
        (f"600{index:03d}", f"强势{index}", f"强势行业{index}", 4.9 - index * 0.2)
        for index in range(1, 13)
    ]
    pullback_rows = [
        (f"000{index:03d}", f"回撤{index}", f"回撤行业{index}", -0.1 * index)
        for index in range(1, 6)
    ]
    rows = strength_rows + pullback_rows
    basics = [
        {"code": code, "name": name, "industry": industry}
        for code, name, industry, _ in rows
    ]
    quotes = [
        _quote(code, close=8.0, pct_chg=pct_chg, amount=2000 - index)
        for index, (code, _, _, pct_chg) in enumerate(rows)
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=limit,
    )

    assert [
        item["discovery"]["selection_bucket"] for item in result["definitions"]
    ] == expected_buckets


def test_dynamic_ranking_backfills_missing_pullback_slots_without_duplicates():
    rows = [
        (f"600{index:03d}", f"强势{index}", f"强势行业{index}", 4.5 - index * 0.2)
        for index in range(1, 11)
    ] + [("000001", "唯一回撤", "回撤行业", -0.5)]
    basics = [
        {"code": code, "name": name, "industry": industry}
        for code, name, industry, _ in rows
    ]
    quotes = [
        _quote(code, close=8.0, pct_chg=pct_chg, amount=2000 - index)
        for index, (code, _, _, pct_chg) in enumerate(rows)
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=8,
    )

    codes = [item["code"] for item in result["definitions"]]
    assert len(codes) == len(set(codes)) == 8
    assert result["selected_bucket_counts"] == {"pullback": 1, "strength": 7}


def test_dynamic_ranking_backfills_missing_strength_slots_without_duplicates():
    rows = [
        (f"600{index:03d}", f"强势{index}", f"强势行业{index}", 4.5 - index * 0.4)
        for index in range(1, 5)
    ] + [
        (f"000{index:03d}", f"回撤{index}", f"回撤行业{index}", -0.2 * index)
        for index in range(1, 9)
    ]
    basics = [
        {"code": code, "name": name, "industry": industry}
        for code, name, industry, _ in rows
    ]
    quotes = [
        _quote(code, close=8.0, pct_chg=pct_chg, amount=2000 - index)
        for index, (code, _, _, pct_chg) in enumerate(rows)
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=8,
    )

    codes = [item["code"] for item in result["definitions"]]
    assert len(codes) == len(set(codes)) == 8
    assert result["selected_bucket_counts"] == {"pullback": 4, "strength": 4}


def test_dynamic_ranking_applies_industry_cap_across_bucket_slots_once():
    rows = [
        ("600101", "强势甲一", "甲行业", 4.9, 1200),
        ("600102", "强势甲二", "甲行业", 4.5, 1100),
        ("600103", "强势甲三", "甲行业", 4.0, 1000),
        ("600104", "强势丙", "丙行业", 3.5, 900),
        ("600105", "强势丁", "丁行业", 3.0, 800),
        ("600106", "强势己", "己行业", 2.5, 700),
        ("600107", "强势庚", "庚行业", 2.0, 600),
        ("600108", "强势辛", "辛行业", 1.5, 500),
        ("000101", "回撤甲一", "甲行业", -0.1, 1150),
        ("000102", "回撤乙", "乙行业", -0.2, 1050),
        ("000103", "回撤甲二", "甲行业", -0.3, 950),
        ("000104", "回撤戊", "戊行业", -0.4, 850),
    ]
    basics = [
        {"code": code, "name": name, "industry": industry}
        for code, name, industry, _, _ in rows
    ]
    quotes = [
        _quote(code, close=8.0, pct_chg=pct_chg, amount=amount)
        for code, _, _, pct_chg, amount in rows
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
        limit=8,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600101",
        "600102",
        "000102",
        "600104",
        "600105",
        "000104",
        "600106",
        "600107",
    ]
    assert result["rejection_counts"]["industry_cap"] == 3
    assert len({item["code"] for item in result["definitions"]}) == 8


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close", float("nan")),
        ("pct_chg", float("nan")),
        ("amount", float("inf")),
        ("turnover_rate", float("-inf")),
    ],
)
def test_dynamic_ranking_rejects_non_finite_quote_numbers(field, value):
    quote = _quote("600123", close=5.78, pct_chg=1.9, amount=700)
    quote[field] = value

    result = rank_dynamic_candidate_universe(
        [quote],
        [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["rejection_counts"] == {"invalid_quote": 1}


def test_dynamic_ranking_rejects_ambiguous_duplicate_quote_codes():
    quotes = [
        _quote("600001", close=8.0, pct_chg=2.0, amount=1200),
        _quote("600001", close=8.1, pct_chg=2.1, amount=1300),
        _quote("000001", close=7.0, pct_chg=-0.5, amount=900),
    ]
    basics = [
        {"code": "600001", "name": "重复行情", "industry": "软件"},
        {"code": "000001", "name": "唯一行情", "industry": "银行"},
    ]

    result = rank_dynamic_candidate_universe(
        quotes,
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert [item["code"] for item in result["definitions"]] == ["000001"]
    assert result["rejection_counts"] == {"duplicate_quote": 1}


@pytest.mark.parametrize("reverse_basics", [False, True])
def test_dynamic_ranking_rejects_special_treatment_from_any_basic_source(
    reverse_basics,
):
    basics = [
        {
            "code": "600001",
            "name": "*ST多来源",
            "industry": "软件",
            "source": "akshare",
        },
        {
            "code": "600001",
            "name": "多来源",
            "industry": "软件",
            "source": "tushare",
        },
    ]
    if reverse_basics:
        basics.reverse()

    result = rank_dynamic_candidate_universe(
        [_quote("600001", close=8.0, pct_chg=2.0, amount=1200)],
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["rejection_counts"] == {"special_treatment": 1}


def test_dynamic_ranking_rejects_special_treatment_from_quote_name():
    quote = _quote("600001", close=8.0, pct_chg=2.0, amount=1200)
    quote["name"] = "*ST最新行情"

    result = rank_dynamic_candidate_universe(
        [quote],
        [
            {
                "code": "600001",
                "name": "旧普通名称",
                "industry": "软件",
                "source": "tushare",
            }
        ],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["rejection_counts"] == {"special_treatment": 1}


@pytest.mark.parametrize("reverse_basics", [False, True])
def test_dynamic_ranking_rejects_high_turnover_from_any_basic_source(
    reverse_basics,
):
    basics = [
        {
            "code": "600001",
            "name": "多来源",
            "industry": "软件",
            "source": "akshare",
            "turnover_rate": 12.0,
        },
        {
            "code": "600001",
            "name": "多来源",
            "industry": "软件",
            "source": "tushare",
            "turnover_rate": 2.0,
        },
    ]
    if reverse_basics:
        basics.reverse()

    result = rank_dynamic_candidate_universe(
        [
            _quote(
                "600001",
                close=8.0,
                pct_chg=2.0,
                amount=1200,
                turnover_rate=None,
            )
        ],
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["definitions"] == []
    assert result["rejection_counts"] == {"high_turnover": 1}


def test_dynamic_ranking_rejects_non_finite_basic_turnover_fallback():
    result = rank_dynamic_candidate_universe(
        [
            _quote(
                "600001",
                close=8.0,
                pct_chg=2.0,
                amount=1200,
                turnover_rate=None,
            )
        ],
        [
            {
                "code": "600001",
                "name": "基础数据异常",
                "industry": "软件",
                "source": "tushare",
                "turnover_rate": float("nan"),
            }
        ],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "no_eligible_candidates"
    assert result["rejection_counts"] == {"invalid_basic": 1}


@pytest.mark.parametrize("reverse_basics", [False, True])
def test_dynamic_ranking_uses_deterministic_basic_source_priority(reverse_basics):
    basics = [
        {
            "code": "600001",
            "name": "低优先级名称",
            "industry": "低优先级行业",
            "source": "akshare",
        },
        {
            "code": "600001",
            "name": "高优先级名称",
            "industry": "高优先级行业",
            "source": "tushare",
        },
    ]
    if reverse_basics:
        basics.reverse()

    result = rank_dynamic_candidate_universe(
        [_quote("600001", close=8.0, pct_chg=2.0, amount=1200)],
        basics,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["definitions"][0]["name"] == "高优先级名称"
    assert result["definitions"][0]["theme_label"] == "高优先级行业"


def test_dynamic_ranking_keeps_inclusive_bucket_boundaries():
    rows = [
        ("600001", "强势上界", "行业一", 5.0),
        ("600002", "强势下界", "行业二", 0.0),
        ("000001", "回撤下界", "行业三", -3.0),
        ("600003", "超出上界", "行业四", 5.01),
        ("000002", "超出下界", "行业五", -3.01),
    ]
    result = rank_dynamic_candidate_universe(
        [
            _quote(code, close=8.0, pct_chg=pct_chg, amount=1000 - index)
            for index, (code, _, _, pct_chg) in enumerate(rows)
        ],
        [
            {"code": code, "name": name, "industry": industry}
            for code, name, industry, _ in rows
        ],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600001",
        "600002",
        "000001",
    ]
    assert result["eligible_bucket_counts"] == {"pullback": 1, "strength": 2}
    assert result["rejection_counts"] == {"hot_move": 2}


def test_dynamic_ranking_uses_all_documented_tie_breakers():
    rows = [
        ("600001", 9.0, 100),
        ("600002", 8.0, 200),
        ("600003", 7.0, 100),
        ("600004", 7.0, 100),
    ]
    result = rank_dynamic_candidate_universe(
        [
            _quote(code, close=close, pct_chg=1.0, amount=amount)
            for code, close, amount in rows
        ],
        [
            {"code": code, "name": code, "industry": f"行业{index}"}
            for index, (code, _, _) in enumerate(rows)
        ],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert [item["code"] for item in result["definitions"]] == [
        "600002",
        "600003",
        "600004",
        "600001",
    ]


def test_dynamic_ranking_fails_closed_when_quote_universe_is_stale():
    result = rank_dynamic_candidate_universe(
        [_quote("600123", close=5.78, pct_chg=1.9, amount=700, trade_date="2026-07-10")],
        [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}],
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "stale_quote_universe"
    assert result["definitions"] == []
    assert result["latest_quote_trade_date"] == "2026-07-10"
    assert result["benchmark_trade_date"] == "2026-07-13"
    assert result["eligible_bucket_counts"] == {"pullback": 0, "strength": 0}
    assert result["selected_bucket_counts"] == {"pullback": 0, "strength": 0}


def test_dynamic_ranking_requires_benchmark_calendar():
    result = rank_dynamic_candidate_universe(
        [_quote("600123", close=5.78, pct_chg=1.9, amount=700)],
        [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}],
        benchmark_trade_date=None,
        cash_available=10000.0,
    )

    assert result["status"] == "benchmark_calendar_unavailable"
    assert result["definitions"] == []


def test_dynamic_discovery_reads_mongo_quote_and_basic_collections():
    db = _DB(
        {
            "market_quotes": _Collection(
                [_quote("600123", close=5.78, pct_chg=1.9, amount=700)]
            ),
            "stock_basic_info": _Collection(
                [{"code": "600123", "name": "兰花科创", "industry": "煤炭"}]
            ),
        }
    )

    result = discover_dynamic_candidate_universe(
        db,
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["definitions"]] == ["600123"]
    assert result["source"] == "mongo.market_quotes+stock_basic_info"


def test_dynamic_discovery_fails_closed_when_mongo_collections_are_unavailable():
    result = discover_dynamic_candidate_universe(
        _DB({}),
        benchmark_trade_date="2026-07-13",
        cash_available=10000.0,
    )

    assert result["status"] == "candidate_discovery_unavailable"
    assert result["definitions"] == []
    assert result["source"] == "mongo.market_quotes+stock_basic_info"
    assert result["eligible_bucket_counts"] == {"pullback": 0, "strength": 0}
    assert result["selected_bucket_counts"] == {"pullback": 0, "strength": 0}
