"""Strict, evidence-only adapters for company profile providers.

The adapters deliberately return source documents, not normalized profiles.  A
provider failure is allowed to reach the enrichment service so it can apply its
safe error contract; an empty or unavailable provider returns no documents.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


ProviderFactory = Callable[[], Any]
PROVIDER_ENDPOINT_TIMEOUT_SECONDS = 15.0


class _ProviderResponseError(Exception):
    def __init__(self, safe_error_code: str = "provider_error") -> None:
        self.safe_error_code = safe_error_code


class ProfileFetchResult(list):
    """List-compatible source documents with non-document adapter outcomes."""

    def __init__(
        self,
        documents: Iterable[Mapping[str, Any]] = (),
        *,
        display_only: Iterable[Mapping[str, Any]] = (),
        provider_errors: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(documents)
        self.display_only = [dict(item) for item in display_only]
        self.provider_errors = [dict(item) for item in provider_errors]


def _clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if value != value:  # NaN and pandas.NA-like scalar values
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _code(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text else ""


def _provider_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith(("SH.", "SZ.")):
        text = text[3:]
    if "." in text:
        text = text.split(".", 1)[0]
    if not re.fullmatch(r"\d{1,6}", text):
        return ""
    return text.zfill(6)


def _code_mismatch(source: str, endpoint: str, returned_code: Any) -> dict[str, Any]:
    return {
        "reason": "document_code_mismatch",
        "source": source,
        "source_endpoint": endpoint,
        "returned_code": str(returned_code or ""),
    }


def _endpoint_error(source: str, endpoint: str, error: BaseException) -> dict[str, Any]:
    if getattr(error, "safe_error_code", None) in {
        "provider_error",
        "provider_permission_denied",
        "provider_unavailable",
    }:
        error_code = error.safe_error_code
    elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        error_code = "provider_timeout"
    elif isinstance(error, PermissionError):
        error_code = "provider_permission_denied"
    else:
        error_code = "provider_error"
    return {
        "source": source,
        "source_endpoint": endpoint,
        "error_code": error_code,
    }


def _ts_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        return text
    exchange = "SH" if text.startswith(("5", "6", "688", "689")) else "SZ"
    return f"{text.zfill(6)}.{exchange}"


def _baostock_code(code: str) -> str:
    normalized = _code(code)
    return f"sh.{normalized}" if normalized.startswith(("5", "6", "688", "689")) else f"sz.{normalized}"


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
            return [dict(item) for item in records or [] if isinstance(item, Mapping)]
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _source_updated(row: Mapping[str, Any]) -> Any:
    for key in (
        "source_updated_at",
        "updated_at",
        "update_date",
        "ann_date",
        "f_ann_date",
        "data_update_time",
        "更新时间",
    ):
        value = _clean(row.get(key))
        if value is None:
            continue
        if _parse_date(value) is not None:
            return value
    return None


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _document(code: str, source: str, endpoint: str, record_key: str, **fields: Any) -> dict[str, Any]:
    document = {
        "code": _code(code),
        "source": source,
        "source_endpoint": endpoint,
        "source_record_key": record_key,
        "retrieved_at": datetime.now(timezone.utc),
    }
    document.update({key: value for key, value in fields.items() if _clean(value) is not None})
    return document


async def _call_with_timeout(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(function, *args, **kwargs),
        timeout=PROVIDER_ENDPOINT_TIMEOUT_SECONDS,
    )


def _provider_ready(provider: Any, source: str) -> bool:
    if source == "tushare":
        return bool(getattr(provider, "connected", False) and getattr(provider, "api", None))
    if source == "baostock":
        # BaoStock's raw module is the usable resource; the query owns login/logout.
        return getattr(provider, "bs", None) is not None
    return bool(getattr(provider, "connected", False) and getattr(provider, "ak", None))


async def _connected_provider(
    provider_factory: ProviderFactory,
    source: str,
    provider: Any = None,
) -> Any:
    try:
        provider = provider if provider is not None else provider_factory()
        if _provider_ready(provider, source):
            return provider
        if source == "baostock" and getattr(provider, "bs", None) is not None:
            return provider
        if source == "tushare" and callable(getattr(provider, "connect_sync", None)):
            connected = await _call_with_timeout(provider.connect_sync)
        else:
            connect = getattr(provider, "connect", None)
            if not callable(connect):
                raise _ProviderResponseError("provider_unavailable")
            connected = connect()
            if inspect.isawaitable(connected):
                connected = await asyncio.wait_for(
                    connected, timeout=PROVIDER_ENDPOINT_TIMEOUT_SECONDS
                )
        if connected is False or not _provider_ready(provider, source):
            raise _ProviderResponseError("provider_unavailable")
        return provider
    except _ProviderResponseError:
        raise
    except Exception as exc:
        raise _ProviderResponseError("provider_unavailable") from exc


def _shared_fetcher(
    source: str,
    provider_factory: ProviderFactory | None,
    fetcher: Callable[..., Any],
) -> Callable[[str], Any]:
    factory = provider_factory or _default_factory(source)
    provider_holder: dict[str, Any] = {}
    lock = asyncio.Lock()

    async def fetch(code: str) -> Any:
        async with lock:
            if "provider" not in provider_holder:
                try:
                    provider_holder["provider"] = factory()
                except Exception:
                    return ProfileFetchResult(
                        provider_errors=[
                            {
                                "source": source,
                                "source_endpoint": "connect",
                                "error_code": "provider_unavailable",
                            }
                        ]
                    )
            return await fetcher(code, provider=provider_holder["provider"])

    return fetch


def _default_factory(source: str) -> ProviderFactory:
    def factory() -> Any:
        if source == "tushare":
            from tradingagents.dataflows.providers.china.tushare import TushareProvider

            return TushareProvider()
        if source == "baostock":
            from tradingagents.dataflows.providers.china.baostock import BaoStockProvider

            return BaoStockProvider()
        from tradingagents.dataflows.providers.china.akshare import AKShareProvider

        return AKShareProvider()

    return factory


def _map_tushare_basic(code: str, row: Mapping[str, Any]) -> dict[str, Any]:
    ts_code = _clean(row.get("ts_code"))
    return _document(
        code,
        "tushare",
        "stock_basic",
        f"{ts_code}:stock_basic",
        ts_code=ts_code,
        name=_clean(row.get("name")),
        industry=_clean(row.get("industry")),
        market=_clean(row.get("market")),
        list_date=_clean(row.get("list_date")),
        source_updated_at=_source_updated(row),
    )


def _map_tushare_company(code: str, row: Mapping[str, Any]) -> dict[str, Any]:
    ts_code = _clean(row.get("ts_code"))
    return _document(
        code,
        "tushare",
        "stock_company",
        f"{ts_code}:stock_company",
        ts_code=ts_code,
        industry=_clean(row.get("industry")),
        introduction=_clean(row.get("introduction")),
        main_business=_clean(row.get("main_business")),
        business_scope=_clean(row.get("business_scope")),
        source_updated_at=_source_updated(row),
    )


def _map_tushare_revenue(code: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows = [dict(row) for row in rows]
    periods = [row.get("end_date") or row.get("report_period") for row in rows]
    dated = [(period, row) for period, row in zip(periods, rows) if _parse_date(period) is not None]
    if not dated:
        return None
    period, _ = max(dated, key=lambda item: _parse_date(item[0]))
    latest_rows = [row for row in rows if (row.get("end_date") or row.get("report_period")) == period]
    items = []
    for row in latest_rows:
        item = {
            "item": _clean(row.get("bz_item") or row.get("item") or row.get("name")),
            "revenue": _clean(row.get("bz_sales") or row.get("revenue")),
            "profit": _clean(row.get("bz_profit") or row.get("profit")),
            "cost": _clean(row.get("bz_cost") or row.get("cost")),
            "currency": _clean(row.get("curr_type") or row.get("currency")),
        }
        items.append({key: value for key, value in item.items() if value is not None})
    ts_code = _clean(latest_rows[0].get("ts_code"))
    return _document(
        code,
        "tushare",
        "fina_mainbz",
        f"{ts_code}:fina_mainbz:{period}",
        ts_code=ts_code,
        report_period=period,
        revenue_composition={"items": items, "report_period": period},
        source_updated_at=_source_updated(latest_rows[0]),
    )


async def fetch_tushare_profile(
    code: str,
    *,
    provider_factory: ProviderFactory | None = None,
    provider: Any = None,
) -> list[dict[str, Any]]:
    """Fetch exact Tushare profile endpoints for one code."""

    try:
        provider = await _connected_provider(
            provider_factory or _default_factory("tushare"), "tushare", provider
        )
    except _ProviderResponseError as exc:
        return ProfileFetchResult(
            provider_errors=[_endpoint_error("tushare", "connect", exc)]
        )
    ts_code = _ts_code(code)
    api = provider.api
    endpoint_errors = []
    endpoint_values = {}
    for endpoint in ("stock_basic", "stock_company", "fina_mainbz"):
        try:
            endpoint_values[endpoint] = await _call_with_timeout(
                getattr(api, endpoint), ts_code=ts_code
            )
        except Exception as exc:
            endpoint_errors.append(_endpoint_error("tushare", endpoint, exc))
    documents = []
    display_only = []
    for endpoint, mapper in (
        ("stock_basic", _map_tushare_basic),
        ("stock_company", _map_tushare_company),
    ):
        for row in _rows(endpoint_values.get(endpoint)):
            if _provider_code(row.get("ts_code")) != _code(code):
                display_only.append(_code_mismatch("tushare", endpoint, row.get("ts_code")))
                continue
            documents.append(mapper(code, row))
    revenue_rows = []
    for row in _rows(endpoint_values.get("fina_mainbz")):
        if _provider_code(row.get("ts_code")) != _code(code):
            display_only.append(_code_mismatch("tushare", "fina_mainbz", row.get("ts_code")))
            continue
        revenue_rows.append(row)
    revenue_document = _map_tushare_revenue(code, revenue_rows)
    if revenue_document:
        documents.append(revenue_document)
    return ProfileFetchResult(
        documents, display_only=display_only, provider_errors=endpoint_errors
    )


def _baostock_rows(result: Any) -> list[dict[str, Any]]:
    if hasattr(result, "to_dict"):
        return _rows(result)
    if str(getattr(result, "error_code", "0")) != "0":
        raise _ProviderResponseError()
    fields = list(getattr(result, "fields", []) or [])
    rows = []
    while getattr(result, "error_code", "0") == "0" and result.next():
        values = result.get_row_data()
        rows.append(dict(zip(fields, values)))
    return rows


async def fetch_baostock_profile(
    code: str,
    *,
    provider_factory: ProviderFactory | None = None,
    provider: Any = None,
) -> list[dict[str, Any]]:
    """Fetch one BaoStock query_stock_basic source document."""

    try:
        provider = await _connected_provider(
            provider_factory or _default_factory("baostock"), "baostock", provider
        )
    except _ProviderResponseError as exc:
        return ProfileFetchResult(
            provider_errors=[_endpoint_error("baostock", "connect", exc)]
        )
    raw = getattr(provider, "bs", None) if provider is not None else None
    if raw is None:
        return ProfileFetchResult()

    def query() -> list[dict[str, Any]]:
        login = getattr(raw, "login", None)
        logout = getattr(raw, "logout", None)
        if callable(login):
            result = login()
            if getattr(result, "error_code", "0") != "0":
                raise PermissionError(getattr(result, "error_msg", "permission denied"))
        try:
            return _baostock_rows(raw.query_stock_basic(code=_baostock_code(code)))
        finally:
            if callable(logout):
                logout()

    try:
        rows = await _call_with_timeout(query)
    except Exception as exc:
        return ProfileFetchResult(
            provider_errors=[_endpoint_error("baostock", "query_stock_basic", exc)]
        )
    if not rows:
        return ProfileFetchResult()
    row = rows[0]
    returned_code = row.get("code")
    if _provider_code(returned_code) != _code(code):
        return ProfileFetchResult(
            display_only=[_code_mismatch("baostock", "query_stock_basic", returned_code)]
        )
    return [
        _document(
            code,
            "baostock",
            "query_stock_basic",
            f"{returned_code}:query_stock_basic",
            name=_clean(row.get("code_name") or row.get("name")),
            list_date=_clean(row.get("ipoDate") or row.get("list_date")),
            status=_clean(row.get("status")),
            stock_type=_clean(row.get("type")),
            provider_code=_clean(returned_code),
        )
    ]


async def fetch_akshare_profile(
    code: str,
    *,
    provider_factory: ProviderFactory | None = None,
    provider: Any = None,
) -> list[dict[str, Any]]:
    """Fetch and parse AKShare's item/value company information frame."""

    try:
        provider = await _connected_provider(
            provider_factory or _default_factory("akshare"), "akshare", provider
        )
    except _ProviderResponseError as exc:
        return ProfileFetchResult(
            provider_errors=[_endpoint_error("akshare", "connect", exc)]
        )
    raw = getattr(provider, "ak", None) if provider is not None else None
    method = getattr(raw, "stock_individual_info_em", None) if raw is not None else None
    if not callable(method):
        return ProfileFetchResult()
    try:
        frame = await _call_with_timeout(method, symbol=_code(code))
    except Exception as exc:
        return ProfileFetchResult(
            provider_errors=[
                _endpoint_error("akshare", "stock_individual_info_em", exc)
            ]
        )
    values = {}
    for row in _rows(frame):
        item = _clean(row.get("item"))
        if item is not None:
            values[str(item)] = _clean(row.get("value"))
    return [
        _document(
            code,
            "akshare",
            "stock_individual_info_em",
            f"{_code(code)}:stock_individual_info_em",
            name=values.get("股票简称") or values.get("名称"),
            industry=values.get("所属行业") or values.get("行业"),
            main_business=values.get("主营业务"),
            business_scope=values.get("经营范围"),
            listing_date=values.get("上市时间") or values.get("上市日期"),
            source_updated_at=values.get("更新时间") or values.get("数据更新时间"),
        )
    ]


def build_default_profile_fetchers(
    *,
    tushare_provider_factory: ProviderFactory | None = None,
    baostock_provider_factory: ProviderFactory | None = None,
    akshare_provider_factory: ProviderFactory | None = None,
) -> dict[str, Callable[[str], Any]]:
    """Build lazy source fetchers; provider construction remains test-injectable."""

    return {
        "tushare": _shared_fetcher(
            "tushare", tushare_provider_factory, fetch_tushare_profile
        ),
        "baostock": _shared_fetcher(
            "baostock", baostock_provider_factory, fetch_baostock_profile
        ),
        "akshare": _shared_fetcher(
            "akshare", akshare_provider_factory, fetch_akshare_profile
        ),
    }
