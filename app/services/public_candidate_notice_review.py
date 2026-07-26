"""Recent public-announcement evidence for bounded A-share code batches."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


NOTICE_REVIEW_SOURCE = "akshare.eastmoney.stock_notice_report"
NOTICE_HISTORY_SOURCE = "akshare.eastmoney.stock_individual_notice_report"
MAX_NOTICE_REVIEW_CANDIDATES = 8
NOTICE_LOOKBACK_CALENDAR_DAYS = 7
MAX_NOTICE_LOOKBACK_CALENDAR_DAYS = 90
MAX_NOTICES_PER_CODE = 20
PUBLIC_NOTICE_REVIEW_STATUSES = frozenset(
    {"notices_found", "no_recent_notices"}
)
PUBLIC_NOTICE_ATTENTION_TAGS = (
    "risk_warning",
    "sanctions_or_trade_restrictions",
    "material_asset_restructuring",
    "financing_or_dilution",
    "shareholding_change",
    "share_repurchase",
    "financial_disclosure",
    "major_contract",
)


NoticeLoader = Callable[[str], Any]
NoticeHistoryLoader = Callable[[str, str, str], Any]
_A_SHARE_CODE_PATTERN = re.compile(
    r"(?:[036][0-9]{5}|(?:43|83|87|88|92)[0-9]{4})"
)
_HTTP_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_REQUIRED_PROVIDER_FIELDS = frozenset(
    {"代码", "名称", "公告标题", "公告类型", "公告日期", "网址"}
)
_ATTENTION_TERMS = {
    "risk_warning": (
        "风险提示",
        "立案",
        "调查",
        "处罚",
        "退市",
        "终止上市",
        "债务逾期",
        "重大诉讼",
        "仲裁",
        "违规",
        "停牌",
    ),
    "sanctions_or_trade_restrictions": (
        "制裁",
        "SDN清单",
        "实体清单",
        "出口管制",
        "贸易限制",
        "禁运",
    ),
    "material_asset_restructuring": (
        "重大资产重组",
        "并购重组",
        "发行股份及支付现金购买资产",
        "购买资产",
    ),
    "financing_or_dilution": (
        "增发",
        "定向发行",
        "发行股份",
        "融资",
        "配股",
        "可转换公司债券",
    ),
    "shareholding_change": (
        "增持",
        "减持",
        "持股变动",
        "权益变动",
    ),
    "share_repurchase": ("回购",),
    "financial_disclosure": (
        "财务报表",
        "业绩预告",
        "业绩快报",
        "半年度报告",
        "季度报告",
        "年度报告",
        "审计报告",
    ),
    "major_contract": (
        "重大合同",
        "中标",
        "签订合同",
        "框架协议",
    ),
}


def _normalized_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalized_code(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value).zfill(6)
    elif isinstance(value, str):
        text = value.strip().zfill(6)
    else:
        return None
    return text if _A_SHARE_CODE_PATTERN.fullmatch(text) else None


def _normalized_text(value: Any, *, limit: int) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _normalized_url(value: Any) -> Optional[str]:
    text = _normalized_text(value, limit=2000)
    if text is None or _HTTP_URL_PATTERN.fullmatch(text) is None:
        return None
    return text


def _rows_from_loader_payload(value: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(value, list):
        rows = value
    elif hasattr(value, "to_dict"):
        if hasattr(value, "columns"):
            try:
                columns = {str(column) for column in value.columns}
            except (TypeError, ValueError):
                return None
            if not _REQUIRED_PROVIDER_FIELDS.issubset(columns):
                return None
        try:
            rows = value.to_dict(orient="records")
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not all(
        isinstance(row, Mapping)
        and _REQUIRED_PROVIDER_FIELDS.issubset(row)
        for row in rows
    ):
        return None
    return [dict(row) for row in rows]


def _load_notice_day(date_text: str) -> Any:
    import akshare as ak

    return ak.stock_notice_report(symbol="全部", date=date_text)


def _load_individual_notice_history(
    code: str,
    start_date: str,
    end_date: str,
) -> Any:
    import akshare as ak

    return ak.stock_individual_notice_report(
        security=code,
        symbol="全部",
        begin_date=start_date,
        end_date=end_date,
    )


def _attention_tags(title: str, notice_type: str) -> List[str]:
    haystack = f"{title} {notice_type}"
    return [
        tag
        for tag in PUBLIC_NOTICE_ATTENTION_TAGS
        if any(term in haystack for term in _ATTENTION_TERMS[tag])
    ]


def _invalid_result(
    error_type: str,
    *,
    source: str = NOTICE_REVIEW_SOURCE,
) -> Dict[str, Any]:
    return {
        "status": "notice_review_invalid_input",
        "source": source,
        "error_type": error_type,
        "results": [],
    }


def _source_failure(
    *,
    start_date: date,
    end_date: date,
    failed_date: date,
    error_type: str,
) -> Dict[str, Any]:
    return {
        "status": "notice_source_unavailable",
        "source": NOTICE_REVIEW_SOURCE,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "failed_date": failed_date.isoformat(),
        "error_type": error_type,
        "results": [],
    }


def _history_source_failure(
    *,
    start_date: date,
    end_date: date,
    failed_code: str,
    error_type: str,
) -> Dict[str, Any]:
    return {
        "status": "notice_source_unavailable",
        "source": NOTICE_HISTORY_SOURCE,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "failed_code": failed_code,
        "error_type": error_type,
        "results": [],
    }


def _build_notice_review_result(
    normalized_codes: Sequence[str],
    notices_by_code: Mapping[str, List[Dict[str, Any]]],
    *,
    source: str,
    start_date: date,
    end_date: date,
    lookback_calendar_days: int,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for code in normalized_codes:
        sorted_notices = sorted(
            notices_by_code[code],
            key=lambda item: (
                item["announcement_date"],
                item["title"],
                item["url"],
            ),
            reverse=True,
        )
        deduplicated: List[Dict[str, Any]] = []
        seen_urls = set()
        for item in sorted_notices:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            deduplicated.append(item)

        returned = deduplicated[:MAX_NOTICES_PER_CODE]
        name = returned[0]["_name"] if returned else None
        public_notices = [
            {key: value for key, value in item.items() if key != "_name"}
            for item in returned
        ]
        code_tags = [
            tag
            for tag in PUBLIC_NOTICE_ATTENTION_TAGS
            if any(tag in item["attention_tags"] for item in public_notices)
        ]
        total_count = len(deduplicated)
        results.append(
            {
                "code": code,
                "name": name,
                "status": (
                    "notices_found" if total_count else "no_recent_notices"
                ),
                "total_notice_count": total_count,
                "returned_notice_count": len(public_notices),
                "truncated": total_count > len(public_notices),
                "attention_tags": code_tags,
                "manual_review_required": any(
                    item["manual_review_required"] for item in public_notices
                ),
                "notices": public_notices,
            }
        )

    total_notice_count = sum(
        item["total_notice_count"] for item in results
    )
    returned_notice_count = sum(
        item["returned_notice_count"] for item in results
    )
    return {
        "status": "ok",
        "source": source,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "lookback_calendar_days": lookback_calendar_days,
        "reviewed_count": len(results),
        "codes_with_notices_count": sum(
            item["status"] == "notices_found" for item in results
        ),
        "manual_review_code_count": sum(
            item["manual_review_required"] for item in results
        ),
        "total_notice_count": total_notice_count,
        "returned_notice_count": returned_notice_count,
        "attention_tag_code_counts": dict(
            sorted(
                Counter(
                    tag
                    for item in results
                    for tag in item["attention_tags"]
                ).items()
            )
        ),
        "results": results,
    }


def review_public_candidate_notices(
    codes: Any,
    *,
    as_of_date: Any,
    loader: Optional[NoticeLoader] = None,
) -> Dict[str, Any]:
    """Read seven calendar days of notices for at most eight requested codes."""
    if not isinstance(codes, list) or not codes:
        return _invalid_result("codes_invalid")
    if len(codes) > MAX_NOTICE_REVIEW_CANDIDATES:
        return _invalid_result("too_many_candidates")
    normalized_codes: List[str] = []
    for raw_code in codes:
        code = _normalized_code(raw_code)
        if code is None:
            return _invalid_result("invalid_code")
        if code in normalized_codes:
            return _invalid_result("duplicate_code")
        normalized_codes.append(code)

    end_date = _normalized_date(as_of_date)
    if end_date is None:
        return _invalid_result("as_of_date_invalid")
    start_date = end_date - timedelta(
        days=NOTICE_LOOKBACK_CALENDAR_DAYS - 1
    )
    effective_loader = loader or _load_notice_day
    notices_by_code: Dict[str, List[Dict[str, Any]]] = {
        code: [] for code in normalized_codes
    }

    for offset in range(NOTICE_LOOKBACK_CALENDAR_DAYS):
        query_date = start_date + timedelta(days=offset)
        try:
            raw_rows = effective_loader(query_date.strftime("%Y%m%d"))
        except Exception as exc:
            return _source_failure(
                start_date=start_date,
                end_date=end_date,
                failed_date=query_date,
                error_type=type(exc).__name__,
            )
        rows = _rows_from_loader_payload(raw_rows)
        if rows is None:
            return _source_failure(
                start_date=start_date,
                end_date=end_date,
                failed_date=query_date,
                error_type="InvalidProviderPayload",
            )
        for row in rows:
            code = _normalized_code(row.get("代码"))
            if code not in notices_by_code:
                continue
            announcement_date = _normalized_date(row.get("公告日期"))
            title = _normalized_text(row.get("公告标题"), limit=500)
            notice_type = _normalized_text(row.get("公告类型"), limit=100)
            url = _normalized_url(row.get("网址"))
            name = _normalized_text(row.get("名称"), limit=100)
            if (
                announcement_date != query_date
                or title is None
                or notice_type is None
                or url is None
                or name is None
            ):
                return _source_failure(
                    start_date=start_date,
                    end_date=end_date,
                    failed_date=query_date,
                    error_type="InvalidProviderPayload",
                )
            tags = _attention_tags(title, notice_type)
            notices_by_code[code].append(
                {
                    "announcement_date": announcement_date.isoformat(),
                    "title": title,
                    "notice_type": notice_type,
                    "url": url,
                    "attention_tags": tags,
                    "manual_review_required": bool(tags),
                    "_name": name,
                }
            )

    return _build_notice_review_result(
        normalized_codes,
        notices_by_code,
        source=NOTICE_REVIEW_SOURCE,
        start_date=start_date,
        end_date=end_date,
        lookback_calendar_days=NOTICE_LOOKBACK_CALENDAR_DAYS,
    )


def review_public_candidate_notice_history(
    codes: Any,
    *,
    as_of_date: Any,
    lookback_calendar_days: Any,
    loader: Optional[NoticeHistoryLoader] = None,
) -> Dict[str, Any]:
    """Read up to 90 calendar days of code-specific notices."""
    if not isinstance(codes, list) or not codes:
        return _invalid_result("codes_invalid", source=NOTICE_HISTORY_SOURCE)
    if len(codes) > MAX_NOTICE_REVIEW_CANDIDATES:
        return _invalid_result(
            "too_many_candidates",
            source=NOTICE_HISTORY_SOURCE,
        )
    if (
        isinstance(lookback_calendar_days, bool)
        or not isinstance(lookback_calendar_days, int)
        or not 1
        <= lookback_calendar_days
        <= MAX_NOTICE_LOOKBACK_CALENDAR_DAYS
    ):
        return _invalid_result(
            "lookback_calendar_days_invalid",
            source=NOTICE_HISTORY_SOURCE,
        )

    normalized_codes: List[str] = []
    for raw_code in codes:
        code = _normalized_code(raw_code)
        if code is None:
            return _invalid_result("invalid_code", source=NOTICE_HISTORY_SOURCE)
        if code in normalized_codes:
            return _invalid_result(
                "duplicate_code",
                source=NOTICE_HISTORY_SOURCE,
            )
        normalized_codes.append(code)

    end_date = _normalized_date(as_of_date)
    if end_date is None:
        return _invalid_result(
            "as_of_date_invalid",
            source=NOTICE_HISTORY_SOURCE,
        )
    start_date = end_date - timedelta(days=lookback_calendar_days - 1)
    effective_loader = loader or _load_individual_notice_history
    notices_by_code: Dict[str, List[Dict[str, Any]]] = {
        code: [] for code in normalized_codes
    }

    for code in normalized_codes:
        try:
            raw_rows = effective_loader(
                code,
                start_date.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
            )
        except Exception as exc:
            return _history_source_failure(
                start_date=start_date,
                end_date=end_date,
                failed_code=code,
                error_type=type(exc).__name__,
            )
        rows = _rows_from_loader_payload(raw_rows)
        if rows is None:
            return _history_source_failure(
                start_date=start_date,
                end_date=end_date,
                failed_code=code,
                error_type="InvalidProviderPayload",
            )
        for row in rows:
            row_code = _normalized_code(row.get("代码"))
            announcement_date = _normalized_date(row.get("公告日期"))
            title = _normalized_text(row.get("公告标题"), limit=500)
            notice_type = _normalized_text(row.get("公告类型"), limit=100)
            url = _normalized_url(row.get("网址"))
            name = _normalized_text(row.get("名称"), limit=100)
            if (
                row_code != code
                or announcement_date is None
                or not start_date <= announcement_date <= end_date
                or title is None
                or notice_type is None
                or url is None
                or name is None
            ):
                return _history_source_failure(
                    start_date=start_date,
                    end_date=end_date,
                    failed_code=code,
                    error_type="InvalidProviderPayload",
                )
            tags = _attention_tags(title, notice_type)
            notices_by_code[code].append(
                {
                    "announcement_date": announcement_date.isoformat(),
                    "title": title,
                    "notice_type": notice_type,
                    "url": url,
                    "attention_tags": tags,
                    "manual_review_required": bool(tags),
                    "_name": name,
                }
            )

    return _build_notice_review_result(
        normalized_codes,
        notices_by_code,
        source=NOTICE_HISTORY_SOURCE,
        start_date=start_date,
        end_date=end_date,
        lookback_calendar_days=lookback_calendar_days,
    )


def _valid_non_negative_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _valid_unique_tags(value: Any) -> bool:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return False
    expected = [tag for tag in PUBLIC_NOTICE_ATTENTION_TAGS if tag in value]
    return value == expected and len(value) == len(set(value))


def validate_public_candidate_notice_review(
    value: Any,
    *,
    expected_codes: Sequence[str],
    expected_start_date: Any,
    expected_end_date: Any,
    expected_lookback_calendar_days: Any = NOTICE_LOOKBACK_CALENDAR_DAYS,
    expected_source: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and copy a notice result before exposing it through the CLI."""
    top_keys = {
        "status",
        "source",
        "start_date",
        "end_date",
        "lookback_calendar_days",
        "reviewed_count",
        "codes_with_notices_count",
        "manual_review_code_count",
        "total_notice_count",
        "returned_notice_count",
        "attention_tag_code_counts",
        "results",
    }
    start_date = _normalized_date(expected_start_date)
    end_date = _normalized_date(expected_end_date)
    lookback_calendar_days = expected_lookback_calendar_days
    source = expected_source or (
        NOTICE_REVIEW_SOURCE
        if lookback_calendar_days == NOTICE_LOOKBACK_CALENDAR_DAYS
        else NOTICE_HISTORY_SOURCE
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != top_keys
        or value.get("status") != "ok"
        or source not in {NOTICE_REVIEW_SOURCE, NOTICE_HISTORY_SOURCE}
        or value.get("source") != source
        or start_date is None
        or end_date is None
        or isinstance(lookback_calendar_days, bool)
        or not isinstance(lookback_calendar_days, int)
        or not 1
        <= lookback_calendar_days
        <= MAX_NOTICE_LOOKBACK_CALENDAR_DAYS
        or (end_date - start_date).days
        != lookback_calendar_days - 1
        or value.get("start_date") != start_date.isoformat()
        or value.get("end_date") != end_date.isoformat()
        or value.get("lookback_calendar_days")
        != lookback_calendar_days
        or value.get("reviewed_count") != len(expected_codes)
    ):
        return None, "InvalidNoticeReviewMetadata"

    count_fields = (
        "reviewed_count",
        "codes_with_notices_count",
        "manual_review_code_count",
        "total_notice_count",
        "returned_notice_count",
    )
    if any(not _valid_non_negative_int(value.get(field)) for field in count_fields):
        return None, "InvalidNoticeReviewMetadata"

    results = value.get("results")
    if not isinstance(results, list) or len(results) != len(expected_codes):
        return None, "InvalidNoticeReviewMetadata"
    result_keys = {
        "code",
        "name",
        "status",
        "total_notice_count",
        "returned_notice_count",
        "truncated",
        "attention_tags",
        "manual_review_required",
        "notices",
    }
    notice_keys = {
        "announcement_date",
        "title",
        "notice_type",
        "url",
        "attention_tags",
        "manual_review_required",
    }
    normalized_results: List[Dict[str, Any]] = []
    for expected_code, item in zip(expected_codes, results):
        if not isinstance(item, Mapping) or set(item) != result_keys:
            return None, "InvalidNoticeReviewMetadata"
        name = item.get("name")
        notices = item.get("notices")
        if (
            item.get("code") != expected_code
            or item.get("status") not in PUBLIC_NOTICE_REVIEW_STATUSES
            or (
                name is not None
                and (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 100
                )
            )
            or not _valid_non_negative_int(item.get("total_notice_count"))
            or not _valid_non_negative_int(item.get("returned_notice_count"))
            or not isinstance(item.get("truncated"), bool)
            or not _valid_unique_tags(item.get("attention_tags"))
            or not isinstance(item.get("manual_review_required"), bool)
            or not isinstance(notices, list)
            or len(notices) != item.get("returned_notice_count")
            or item["returned_notice_count"] > MAX_NOTICES_PER_CODE
            or item["returned_notice_count"] > item["total_notice_count"]
            or item["truncated"]
            != (item["total_notice_count"] > item["returned_notice_count"])
        ):
            return None, "InvalidNoticeReviewMetadata"

        normalized_notices: List[Dict[str, Any]] = []
        seen_urls = set()
        for notice in notices:
            if not isinstance(notice, Mapping) or set(notice) != notice_keys:
                return None, "InvalidNoticeReviewMetadata"
            announcement_date = _normalized_date(
                notice.get("announcement_date")
            )
            title = notice.get("title")
            notice_type = notice.get("notice_type")
            url = notice.get("url")
            tags = notice.get("attention_tags")
            if (
                announcement_date is None
                or not start_date <= announcement_date <= end_date
                or not isinstance(title, str)
                or not title
                or len(title) > 500
                or not isinstance(notice_type, str)
                or not notice_type
                or len(notice_type) > 100
                or not isinstance(url, str)
                or _HTTP_URL_PATTERN.fullmatch(url) is None
                or url in seen_urls
                or not _valid_unique_tags(tags)
                or not isinstance(notice.get("manual_review_required"), bool)
                or notice["manual_review_required"] != bool(tags)
            ):
                return None, "InvalidNoticeReviewMetadata"
            seen_urls.add(url)
            normalized_notices.append(dict(notice))

        sorted_notices = sorted(
            normalized_notices,
            key=lambda notice: (
                notice["announcement_date"],
                notice["title"],
                notice["url"],
            ),
            reverse=True,
        )
        derived_tags = [
            tag
            for tag in PUBLIC_NOTICE_ATTENTION_TAGS
            if any(tag in notice["attention_tags"] for notice in normalized_notices)
        ]
        expected_status = (
            "notices_found"
            if item["total_notice_count"] > 0
            else "no_recent_notices"
        )
        if (
            normalized_notices != sorted_notices
            or item["status"] != expected_status
            or item["attention_tags"] != derived_tags
            or item["manual_review_required"]
            != any(
                notice["manual_review_required"]
                for notice in normalized_notices
            )
            or (
                item["status"] == "no_recent_notices"
                and any(
                    (
                        name is not None,
                        item["total_notice_count"] != 0,
                        item["returned_notice_count"] != 0,
                        bool(item["attention_tags"]),
                        item["manual_review_required"],
                        bool(normalized_notices),
                    )
                )
            )
            or (item["status"] == "notices_found" and name is None)
        ):
            return None, "InvalidNoticeReviewMetadata"
        normalized_results.append(
            {
                **dict(item),
                "notices": normalized_notices,
            }
        )

    derived_codes_with_notices = sum(
        item["status"] == "notices_found" for item in normalized_results
    )
    derived_manual_review_codes = sum(
        item["manual_review_required"] for item in normalized_results
    )
    derived_total_notices = sum(
        item["total_notice_count"] for item in normalized_results
    )
    derived_returned_notices = sum(
        item["returned_notice_count"] for item in normalized_results
    )
    attention_tag_code_counts = value.get("attention_tag_code_counts")
    expected_tag_counts = Counter(
        tag
        for item in normalized_results
        for tag in item["attention_tags"]
    )
    if (
        value["codes_with_notices_count"] != derived_codes_with_notices
        or value["manual_review_code_count"] != derived_manual_review_codes
        or value["total_notice_count"] != derived_total_notices
        or value["returned_notice_count"] != derived_returned_notices
        or not isinstance(attention_tag_code_counts, Mapping)
        or not set(attention_tag_code_counts).issubset(
            PUBLIC_NOTICE_ATTENTION_TAGS
        )
        or any(
            not _valid_non_negative_int(count) or count == 0
            for count in attention_tag_code_counts.values()
        )
        or dict(attention_tag_code_counts)
        != dict(sorted(expected_tag_counts.items()))
    ):
        return None, "InvalidNoticeReviewMetadata"

    return {
        "status": "ok",
        "source": source,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "lookback_calendar_days": lookback_calendar_days,
        "reviewed_count": value["reviewed_count"],
        "codes_with_notices_count": value["codes_with_notices_count"],
        "manual_review_code_count": value["manual_review_code_count"],
        "total_notice_count": value["total_notice_count"],
        "returned_notice_count": value["returned_notice_count"],
        "attention_tag_code_counts": dict(
            sorted(attention_tag_code_counts.items())
        ),
        "results": normalized_results,
    }, None
