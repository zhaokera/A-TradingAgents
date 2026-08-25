from copy import deepcopy
import threading
import time

import pytest

from app.services.public_candidate_notice_review import (
    MAX_NOTICES_PER_CODE,
    NOTICE_REVIEW_WORKERS,
    NOTICE_HISTORY_SOURCE,
    NOTICE_REVIEW_SOURCE,
    review_public_candidate_notice_history,
    review_public_candidate_notices,
    validate_public_candidate_notice_review,
)


def _row(
    code: str,
    name: str,
    title: str,
    notice_type: str,
    announcement_date: str,
    suffix: str,
):
    return {
        "代码": code,
        "名称": name,
        "公告标题": title,
        "公告类型": notice_type,
        "公告日期": announcement_date,
        "网址": f"https://data.eastmoney.com/notices/{suffix}",
    }


def _loader_for(rows_by_date):
    calls = []

    def loader(date_text):
        calls.append(date_text)
        return deepcopy(rows_by_date.get(date_text, []))

    return loader, calls


def test_notice_review_uses_seven_days_filters_and_classifies_requested_codes():
    loader, calls = _loader_for(
        {
            "20260717": [
                _row(
                    "000100",
                    "TCL科技",
                    "发行股份及支付现金购买资产暨重大资产重组报告书",
                    "重大事项",
                    "2026-07-17",
                    "000100/restructuring",
                ),
                _row(
                    "600000",
                    "浦发银行",
                    "年度股东大会决议公告",
                    "股东大会",
                    "2026-07-17",
                    "600000/unrelated",
                ),
            ],
            "20260718": [
                _row(
                    "000100",
                    "TCL科技",
                    "回购股份比例达到1%暨回购完成的公告",
                    "回购",
                    "2026-07-18",
                    "000100/repurchase",
                ),
                _row(
                    "002318",
                    "久立特材",
                    "实际控制人的一致行动人增持股份计划实施完成",
                    "持股变动",
                    "2026-07-18",
                    "002318/increase",
                ),
            ],
            "20260720": [
                _row(
                    "300803",
                    "指南针",
                    "子公司2026年半年度财务报表",
                    "财务报告",
                    "2026-07-20",
                    "300803/financials",
                ),
            ],
        }
    )

    result = review_public_candidate_notices(
        ["300803", "000100", "002318", "000777"],
        as_of_date="2026-07-20",
        loader=loader,
    )

    assert sorted(calls) == [
        "20260714",
        "20260715",
        "20260716",
        "20260717",
        "20260718",
        "20260719",
        "20260720",
    ]
    assert result["status"] == "ok"
    assert result["source"] == NOTICE_REVIEW_SOURCE
    assert result["start_date"] == "2026-07-14"
    assert result["end_date"] == "2026-07-20"
    assert [item["code"] for item in result["results"]] == [
        "300803",
        "000100",
        "002318",
        "000777",
    ]
    by_code = {item["code"]: item for item in result["results"]}
    assert by_code["300803"]["attention_tags"] == [
        "financial_disclosure"
    ]
    assert by_code["000100"]["attention_tags"] == [
        "material_asset_restructuring",
        "financing_or_dilution",
        "share_repurchase",
    ]
    assert [
        item["announcement_date"]
        for item in by_code["000100"]["notices"]
    ] == ["2026-07-18", "2026-07-17"]
    assert by_code["002318"]["attention_tags"] == [
        "shareholding_change"
    ]
    assert by_code["000777"] == {
        "code": "000777",
        "name": None,
        "status": "no_recent_notices",
        "total_notice_count": 0,
        "returned_notice_count": 0,
        "truncated": False,
        "attention_tags": [],
        "manual_review_required": False,
        "notices": [],
    }
    assert result["codes_with_notices_count"] == 3
    assert result["manual_review_code_count"] == 3
    assert result["total_notice_count"] == 4
    assert result["returned_notice_count"] == 4
    assert result["attention_tag_code_counts"] == {
        "financial_disclosure": 1,
        "financing_or_dilution": 1,
        "material_asset_restructuring": 1,
        "share_repurchase": 1,
        "shareholding_change": 1,
    }

    validated, error = validate_public_candidate_notice_review(
        result,
        expected_codes=["300803", "000100", "002318", "000777"],
        expected_start_date="2026-07-14",
        expected_end_date="2026-07-20",
    )
    assert error is None
    assert validated == result


def test_notice_history_uses_one_range_request_per_code_and_tags_sanctions():
    calls = []

    def loader(code, start_date, end_date):
        calls.append((code, start_date, end_date))
        if code != "600346":
            return []
        return [
            _row(
                "600346",
                "恒力石化",
                "关于重要子公司被美国财政部列入SDN清单的公告",
                "其他",
                "2026-04-27",
                "600346/sdn",
            )
        ]

    result = review_public_candidate_notice_history(
        ["600346", "000100"],
        as_of_date="2026-07-20",
        lookback_calendar_days=90,
        loader=loader,
    )

    assert calls == [
        ("600346", "20260422", "20260720"),
        ("000100", "20260422", "20260720"),
    ]
    assert result["source"] == NOTICE_HISTORY_SOURCE
    assert result["lookback_calendar_days"] == 90
    assert result["start_date"] == "2026-04-22"
    assert result["end_date"] == "2026-07-20"
    by_code = {item["code"]: item for item in result["results"]}
    assert by_code["600346"]["attention_tags"] == [
        "sanctions_or_trade_restrictions"
    ]
    assert by_code["600346"]["manual_review_required"] is True
    assert by_code["000100"]["status"] == "no_recent_notices"
    assert result["attention_tag_code_counts"] == {
        "sanctions_or_trade_restrictions": 1
    }

    validated, error = validate_public_candidate_notice_review(
        result,
        expected_codes=["600346", "000100"],
        expected_start_date="2026-04-22",
        expected_end_date="2026-07-20",
        expected_lookback_calendar_days=90,
        expected_source=NOTICE_HISTORY_SOURCE,
    )
    assert error is None
    assert validated == result


def test_notice_review_deduplicates_urls_and_bounds_each_code():
    rows = [
        _row(
            "000100",
            "TCL科技",
            f"公告{i:02d}",
            "其他公告",
            "2026-07-20",
            f"000100/{i:02d}",
        )
        for i in range(MAX_NOTICES_PER_CODE + 1)
    ]
    rows.append(deepcopy(rows[0]))
    loader, _ = _loader_for({"20260720": rows})

    result = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=loader,
    )

    item = result["results"][0]
    assert item["total_notice_count"] == MAX_NOTICES_PER_CODE + 1
    assert item["returned_notice_count"] == MAX_NOTICES_PER_CODE
    assert item["truncated"] is True
    assert len({notice["url"] for notice in item["notices"]}) == (
        MAX_NOTICES_PER_CODE
    )


def test_notice_review_fails_closed_without_returning_partial_results():
    calls = []

    def loader(date_text):
        calls.append(date_text)
        if date_text == "20260718":
            raise TimeoutError("provider timeout")
        return [
            _row(
                "000100",
                "TCL科技",
                "回购进展公告",
                "回购",
                date_text[:4] + "-" + date_text[4:6] + "-" + date_text[6:],
                date_text,
            )
        ]

    result = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=loader,
    )

    assert sorted(calls) == [
        "20260714",
        "20260715",
        "20260716",
        "20260717",
        "20260718",
        "20260719",
        "20260720",
    ]
    assert result == {
        "status": "notice_source_unavailable",
        "source": NOTICE_REVIEW_SOURCE,
        "start_date": "2026-07-14",
        "end_date": "2026-07-20",
        "failed_date": "2026-07-18",
        "error_type": "TimeoutError",
        "results": [],
    }


def test_notice_review_loads_days_with_bounded_parallelism():
    calls = []
    active = 0
    peak_active = 0
    lock = threading.Lock()

    def loader(date_text):
        nonlocal active, peak_active
        with lock:
            calls.append(date_text)
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return []

    result = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=loader,
    )

    assert result["status"] == "ok"
    assert len(calls) == 7
    assert 1 < peak_active <= NOTICE_REVIEW_WORKERS


def test_notice_review_rejects_invalid_provider_rows_for_requested_code():
    row = _row(
        "000100",
        "TCL科技",
        "回购进展公告",
        "回购",
        "2026-07-20",
        "000100/repurchase",
    )
    row["网址"] = "javascript:alert(1)"
    loader, _ = _loader_for({"20260720": [row]})

    result = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=loader,
    )

    assert result["status"] == "notice_source_unavailable"
    assert result["failed_date"] == "2026-07-20"
    assert result["error_type"] == "InvalidProviderPayload"
    assert result["results"] == []


def test_notice_review_rejects_empty_dataframe_with_missing_columns():
    class InvalidEmptyFrame:
        columns = []

        def to_dict(self, *, orient):
            assert orient == "records"
            return []

    result = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=lambda _date_text: InvalidEmptyFrame(),
    )

    assert result["status"] == "notice_source_unavailable"
    assert result["failed_date"] == "2026-07-14"
    assert result["error_type"] == "InvalidProviderPayload"


@pytest.mark.parametrize(
    ("codes", "as_of_date", "expected_error"),
    [
        (None, "2026-07-20", "codes_invalid"),
        ([], "2026-07-20", "codes_invalid"),
        (["000100", "000100"], "2026-07-20", "duplicate_code"),
        (["999999"], "2026-07-20", "invalid_code"),
        ([f"00010{i}" for i in range(9)], "2026-07-20", "too_many_candidates"),
        (["000100"], "invalid", "as_of_date_invalid"),
    ],
)
def test_notice_review_rejects_invalid_input(codes, as_of_date, expected_error):
    result = review_public_candidate_notices(
        codes,
        as_of_date=as_of_date,
        loader=lambda _: [],
    )

    assert result["status"] == "notice_review_invalid_input"
    assert result["error_type"] == expected_error


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"actionable": True}),
        lambda payload: payload["results"][0].update({"unknown": "field"}),
        lambda payload: payload["results"][0]["notices"][0].update(
            {"actionable": True}
        ),
        lambda payload: payload["results"][0]["notices"][0].update(
            {"url": "javascript:alert(1)"}
        ),
        lambda payload: payload["results"][0]["notices"][0].update(
            {"announcement_date": "2026-07-13"}
        ),
        lambda payload: payload.update({"total_notice_count": 99}),
        lambda payload: payload["results"][0].update(
            {"manual_review_required": False}
        ),
    ],
)
def test_notice_validator_rejects_unknown_unsafe_or_inconsistent_metadata(mutate):
    loader, _ = _loader_for(
        {
            "20260720": [
                _row(
                    "000100",
                    "TCL科技",
                    "回购完成公告",
                    "回购",
                    "2026-07-20",
                    "000100/repurchase",
                )
            ]
        }
    )
    payload = review_public_candidate_notices(
        ["000100"],
        as_of_date="2026-07-20",
        loader=loader,
    )
    mutate(payload)

    validated, error = validate_public_candidate_notice_review(
        payload,
        expected_codes=["000100"],
        expected_start_date="2026-07-14",
        expected_end_date="2026-07-20",
    )

    assert validated is None
    assert error == "InvalidNoticeReviewMetadata"
