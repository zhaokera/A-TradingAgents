"""Bounded public A-share market breadth fallback for the holdings CLI."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from app.services.a_share_market_regime import MIN_BREADTH_UNIVERSE_SIZE


SINA_BREADTH_SOURCE = "akshare.sina.stock_zh_a_spot"
SINA_ANCHOR_SYMBOL = "sh000001"
SINA_ANCHOR_URL = f"https://hq.sinajs.cn/list={SINA_ANCHOR_SYMBOL}"
SINA_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount?node={node}"
)
SINA_SPOT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_SPOT_PAGE_SIZE = 100
SINA_SPOT_PAGE_WORKERS = 8
SINA_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_PUBLIC_BREADTH_TIMEOUT_SECONDS = 25.0
MAX_PROVIDER_LAG_SECONDS = 20 * 60
MAX_PROVIDER_FUTURE_SECONDS = 2 * 60
MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO = 0.95
CN_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _fetch_sina_spot_page(page: int) -> List[Dict[str, Any]]:
    import requests

    response = requests.get(
        SINA_SPOT_URL,
        params={
            "page": page,
            "num": SINA_SPOT_PAGE_SIZE,
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        },
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=SINA_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        raise ValueError("invalid Sina spot page")
    return [
        {
            "代码": row.get("code"),
            "名称": row.get("name"),
            "最新价": row.get("trade"),
            "涨跌幅": row.get("changepercent"),
            "成交额": row.get("amount"),
            "时间戳": row.get("ticktime"),
        }
        for row in payload
    ]


def _load_sina_spot() -> List[Dict[str, Any]]:
    """Fetch all Sina A-share pages concurrently with exact page coverage."""
    expected_count = _fetch_sina_expected_count("hs_a")
    page_count = math.ceil(expected_count / SINA_SPOT_PAGE_SIZE)
    rows_by_page: Dict[int, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(
        max_workers=min(SINA_SPOT_PAGE_WORKERS, page_count)
    ) as pool:
        futures = {
            pool.submit(_fetch_sina_spot_page, page): page
            for page in range(1, page_count + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            rows = future.result()
            expected_page_count = min(
                SINA_SPOT_PAGE_SIZE,
                expected_count - (page - 1) * SINA_SPOT_PAGE_SIZE,
            )
            if len(rows) != expected_page_count:
                raise ValueError("incomplete Sina spot page")
            rows_by_page[page] = rows
    if len(rows_by_page) != page_count:
        raise ValueError("incomplete Sina spot snapshot")
    return [
        row
        for page in range(1, page_count + 1)
        for row in rows_by_page[page]
    ]


def _parse_sina_anchor_response(content: bytes) -> Dict[str, str]:
    text = content.decode("gb18030", errors="replace")
    match = re.search(r'var\s+hq_str_sh000001="([^"]*)"', text)
    if not match:
        raise ValueError("missing Sina anchor payload")
    fields = match.group(1).split(",")
    if len(fields) <= 31:
        raise ValueError("incomplete Sina anchor payload")
    trade_date = fields[30].strip()
    provider_time = fields[31].strip()
    date.fromisoformat(trade_date)
    time.fromisoformat(provider_time)
    return {
        "trade_date": trade_date,
        "provider_time": provider_time,
        "symbol": SINA_ANCHOR_SYMBOL,
    }


def _load_sina_anchor() -> Dict[str, str]:
    import requests

    response = requests.get(
        SINA_ANCHOR_URL,
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=5,
    )
    response.raise_for_status()
    return _parse_sina_anchor_response(response.content)


def _parse_sina_expected_count(payload: Any) -> int:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid Sina expected count") from exc
    else:
        text = str(payload or "")
    text = text.strip()
    if not text:
        raise ValueError("invalid Sina expected count")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid Sina expected count") from exc
    if isinstance(parsed, bool):
        raise ValueError("invalid Sina expected count")
    if isinstance(parsed, int):
        count = parsed
    elif isinstance(parsed, str) and re.fullmatch(r"[+-]?\d+", parsed.strip()):
        count = int(parsed)
    else:
        raise ValueError("invalid Sina expected count")
    if count <= 0:
        raise ValueError("invalid Sina expected count")
    return count


def _fetch_sina_expected_count(node: str) -> int:
    import requests

    response = requests.get(
        SINA_COUNT_URL.format(node=node),
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=5,
    )
    response.raise_for_status()
    return _parse_sina_expected_count(response.content)


def _load_sina_expected_counts() -> Dict[str, int]:
    total = _fetch_sina_expected_count("hs_a")
    sh = _fetch_sina_expected_count("sh_a")
    sz = _fetch_sina_expected_count("sz_a")
    bj = total - sh - sz
    if bj <= 0:
        raise ValueError("invalid Sina expected counts")
    return {"total": total, "sh": sh, "sz": sz, "bj": bj}


def _records(snapshot: Any) -> List[Dict[str, Any]]:
    if snapshot is None:
        return []
    if isinstance(snapshot, list):
        return [dict(row) for row in snapshot if isinstance(row, dict)]
    if hasattr(snapshot, "to_dict"):
        try:
            rows = snapshot.to_dict("records")
        except TypeError:
            rows = snapshot.to_dict(orient="records")
        return [dict(row) for row in rows if isinstance(row, dict)]
    if isinstance(snapshot, Iterable) and not isinstance(snapshot, (str, bytes, dict)):
        return [dict(row) for row in snapshot if isinstance(row, dict)]
    return []


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _exchange_for_code(code: str) -> Optional[str]:
    if re.fullmatch(r"6\d{5}", code):
        return "sh"
    if re.fullmatch(r"[03]\d{5}", code):
        return "sz"
    if re.fullmatch(r"(?:43|83|87|88|92)\d{4}", code):
        return "bj"
    return None


def _validated_provider_expected_counts(value: Any) -> Optional[Dict[str, int]]:
    if not isinstance(value, dict):
        return None
    counts: Dict[str, int] = {}
    for key in ("total", "sh", "sz", "bj"):
        count = value.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return None
        counts[key] = count
    if counts["total"] != counts["sh"] + counts["sz"] + counts["bj"]:
        return None
    return counts


def _parse_benchmark_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _parse_provider_timestamp(value: Any) -> tuple[Optional[date], Optional[time]]:
    if isinstance(value, datetime):
        localized = value.astimezone(CN_TIMEZONE) if value.tzinfo else value.replace(tzinfo=CN_TIMEZONE)
        return localized.date(), localized.time().replace(tzinfo=None)

    if isinstance(value, (int, float)):
        try:
            raw = float(value)
        except (OverflowError, ValueError):
            return None, None
        if not math.isfinite(raw):
            return None, None
        if raw > 10_000_000_000:
            raw /= 1000
        try:
            localized = datetime.fromtimestamp(raw, tz=CN_TIMEZONE)
        except (OverflowError, OSError, ValueError):
            return None, None
        return localized.date(), localized.time().replace(tzinfo=None)

    text = str(value or "").strip()
    if not text:
        return None, None
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
        localized = parsed.astimezone(CN_TIMEZONE) if parsed.tzinfo else parsed.replace(tzinfo=CN_TIMEZONE)
        return localized.date(), localized.time().replace(tzinfo=None)
    except ValueError:
        pass

    for pattern in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=CN_TIMEZONE)
            return parsed.date(), parsed.time().replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return None, time.fromisoformat(text)
    except ValueError:
        return None, None


def _expected_provider_time(now: datetime) -> Optional[time]:
    current = now.timetz().replace(tzinfo=None)
    if current < time(9, 30):
        return None
    if current <= time(11, 30):
        return current
    if current < time(13, 0):
        return time(11, 30)
    if current <= time(15, 0):
        return current
    return time(15, 0)


def _seconds(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _normalize_sina_snapshot(
    snapshot: Any,
    *,
    benchmark_trade_date: Optional[str],
    provider_anchor: Optional[Dict[str, Any]],
    provider_expected_counts: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    benchmark_date = _parse_benchmark_date(benchmark_trade_date)
    if benchmark_date is None:
        return {
            "status": "public_breadth_benchmark_unavailable",
            "source": SINA_BREADTH_SOURCE,
            "rows": [],
        }

    anchor_date = _parse_benchmark_date((provider_anchor or {}).get("trade_date"))
    _, anchor_time = _parse_provider_timestamp((provider_anchor or {}).get("provider_time"))
    if provider_anchor and (anchor_date is None or anchor_time is None):
        return {
            "status": "public_breadth_trade_date_unverifiable",
            "source": SINA_BREADTH_SOURCE,
            "rows": [],
        }
    if anchor_date is not None and anchor_date != benchmark_date:
        return {
            "status": "public_breadth_trade_date_mismatch",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "provider_trade_date": anchor_date.isoformat(),
            "rows": [],
        }

    expected_counts = _validated_provider_expected_counts(provider_expected_counts)
    if expected_counts is None:
        return {
            "status": "public_snapshot_expected_counts_unavailable",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "rows": [],
        }

    local_now = now.astimezone(CN_TIMEZONE) if now.tzinfo else now.replace(tzinfo=CN_TIMEZONE)
    local_trade_date = local_now.date()
    if benchmark_date > local_trade_date:
        return {
            "status": "public_breadth_trade_date_in_future",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "local_trade_date": local_trade_date.isoformat(),
            "rows": [],
        }
    completed_prior_trade_date = benchmark_date < local_trade_date
    expected_time = (
        time(14, 55)
        if completed_prior_trade_date
        else _expected_provider_time(local_now)
    )
    if expected_time is None:
        return {
            "status": "public_breadth_provider_time_stale",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "expected_provider_time": None,
            "rows": [],
        }
    expected_seconds = _seconds(expected_time)
    current_time = local_now.timetz().replace(tzinfo=None)

    raw_rows = _records(snapshot)
    parsed_rows = []
    time_only_count = 0
    excluded_stale_count = 0
    excluded_future_time_count = 0
    for row in raw_rows:
        code_digits = re.sub(
            r"\D",
            "",
            str(row.get("代码") or row.get("code") or row.get("symbol") or ""),
        )
        code = code_digits[-6:] if len(code_digits) >= 6 else ""
        pct_chg = _number(
            row.get("涨跌幅")
            if row.get("涨跌幅") is not None
            else row.get("pct_chg", row.get("changepercent"))
        )
        close = _number(
            row.get("最新价")
            if row.get("最新价") is not None
            else row.get("close", row.get("trade"))
        )
        amount = _number(
            row.get("成交额")
            if row.get("成交额") is not None
            else row.get("amount", row.get("turnover"))
        )
        exchange = _exchange_for_code(code)
        provider_date, provider_time = _parse_provider_timestamp(
            row.get("时间戳")
            if row.get("时间戳") is not None
            else row.get("timestamp", row.get("time"))
        )
        if provider_date is None:
            time_only_count += 1
            provider_date = anchor_date
        if (
            not code
            or exchange is None
            or close is None
            or close <= 0
            or pct_chg is None
            or amount is None
            or amount <= 0
            or provider_time is None
            or provider_date is None
        ):
            continue
        if provider_date != benchmark_date:
            excluded_stale_count += 1
            continue
        if (
            benchmark_date == local_trade_date
            and _seconds(provider_time)
            > _seconds(current_time) + MAX_PROVIDER_FUTURE_SECONDS
        ):
            excluded_future_time_count += 1
            continue
        parsed_rows.append(
            {
                "code": code,
                "name": str(row.get("名称") or row.get("name") or ""),
                "exchange": exchange,
                "close": close,
                "pct_chg": pct_chg,
                "amount": amount,
                "provider_time": provider_time,
            }
        )

    if time_only_count and anchor_date is None:
        return {
            "status": "public_breadth_trade_date_unverifiable",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "rows": [],
        }

    rows_by_code: Dict[str, Dict[str, Any]] = {}
    for item in parsed_rows:
        previous = rows_by_code.get(item["code"])
        if previous is None or item["provider_time"] >= previous["provider_time"]:
            rows_by_code[item["code"]] = item
    aligned = list(rows_by_code.values())
    duplicate_count = len(parsed_rows) - len(aligned)
    provider_time = max(
        (item["provider_time"] for item in aligned),
        default=None,
    )
    provider_seconds = _seconds(provider_time) if provider_time is not None else None
    provider_datetime = (
        datetime.combine(benchmark_date, provider_time, tzinfo=CN_TIMEZONE)
        if provider_time is not None
        else None
    )
    provider_not_in_future = bool(
        provider_datetime is not None
        and provider_datetime
        <= local_now + timedelta(seconds=MAX_PROVIDER_FUTURE_SECONDS)
    )
    if completed_prior_trade_date or current_time >= time(15, 0):
        provider_time_is_fresh = bool(
            provider_seconds is not None
            and provider_seconds >= _seconds(time(14, 55))
            and provider_not_in_future
        )
    else:
        provider_time_is_fresh = bool(
            provider_seconds is not None
            and provider_seconds >= expected_seconds - MAX_PROVIDER_LAG_SECONDS
            and provider_seconds <= expected_seconds + MAX_PROVIDER_FUTURE_SECONDS
        )
    if not provider_time_is_fresh:
        return {
            "status": "public_breadth_provider_time_stale",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "provider_trade_date": anchor_date.isoformat() if anchor_date else None,
            "provider_time": provider_time.isoformat() if provider_time else None,
            "expected_provider_time": expected_time.isoformat(),
            "rows": [],
        }

    exchange_counts = {"sh": 0, "sz": 0, "bj": 0}
    for item in aligned:
        exchange_counts[item["exchange"]] += 1
    expected_exchange_counts = {
        "sh": expected_counts["sh"],
        "sz": expected_counts["sz"],
        "bj": expected_counts["bj"],
    }
    total_coverage_ratio = len(aligned) / expected_counts["total"]
    exchange_coverage_ratio = {
        exchange: exchange_counts[exchange] / expected_exchange_counts[exchange]
        for exchange in ("sh", "sz", "bj")
    }
    snapshot_metrics = {
        "provider_expected_count": expected_counts["total"],
        "provider_expected_exchange_counts": expected_exchange_counts,
        "raw_row_count": len(raw_rows),
        "unique_row_count": len(aligned),
        "exchange_counts": exchange_counts,
        "total_coverage_ratio": total_coverage_ratio,
        "exchange_coverage_ratio": exchange_coverage_ratio,
        "excluded_future_time_count": excluded_future_time_count,
    }
    if len(aligned) < MIN_BREADTH_UNIVERSE_SIZE:
        return {
            "status": "public_breadth_universe_too_small",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "provider_trade_date": anchor_date.isoformat() if anchor_date else None,
            "universe_size": len(aligned),
            "minimum_universe_size": MIN_BREADTH_UNIVERSE_SIZE,
            "excluded_stale_count": excluded_stale_count,
            "duplicate_count": duplicate_count,
            **snapshot_metrics,
            "rows": [],
        }

    if total_coverage_ratio < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO or any(
        ratio < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
        for ratio in exchange_coverage_ratio.values()
    ):
        return {
            "status": "public_snapshot_coverage_incomplete",
            "source": SINA_BREADTH_SOURCE,
            "benchmark_trade_date": benchmark_date.isoformat(),
            "provider_trade_date": anchor_date.isoformat() if anchor_date else benchmark_date.isoformat(),
            "provider_time": provider_time.isoformat(),
            "universe_size": len(aligned),
            "minimum_coverage_ratio": MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO,
            "excluded_stale_count": excluded_stale_count,
            "duplicate_count": duplicate_count,
            **snapshot_metrics,
            "rows": [],
        }

    normalized_rows = [
        {
            "code": item["code"],
            "name": item["name"],
            "exchange": item["exchange"],
            "close": item["close"],
            "pct_chg": item["pct_chg"],
            "amount": item["amount"],
            "trade_date": benchmark_date.isoformat(),
            "provider_time": item["provider_time"].isoformat(),
        }
        for item in aligned
    ]
    return {
        "status": "ok",
        "source": SINA_BREADTH_SOURCE,
        "benchmark_trade_date": benchmark_date.isoformat(),
        "provider_trade_date": benchmark_date.isoformat(),
        "provider_time": provider_time.isoformat(),
        "universe_size": len(normalized_rows),
        "excluded_stale_count": excluded_stale_count,
        "duplicate_count": duplicate_count,
        **snapshot_metrics,
        "rows": normalized_rows,
    }


def _build_worker_command(
    *,
    benchmark_trade_date: Optional[str],
    now: datetime,
) -> List[str]:
    return [
        sys.executable,
        "-m",
        "app.services.public_market_breadth",
        "--worker",
        "--benchmark-trade-date",
        str(benchmark_trade_date or ""),
        "--now",
        now.isoformat(),
    ]


def fetch_sina_public_market_snapshot(
    *,
    benchmark_trade_date: Optional[str],
    timeout_seconds: float = DEFAULT_PUBLIC_BREADTH_TIMEOUT_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fetch a complete public A-share snapshot in a bounded child process."""
    effective_timeout = max(0.0, float(timeout_seconds))
    effective_now = now or datetime.now(CN_TIMEZONE)
    command = _build_worker_command(
        benchmark_trade_date=benchmark_trade_date,
        now=effective_now,
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "public_breadth_timeout",
            "source": SINA_BREADTH_SOURCE,
            "timeout_seconds": effective_timeout,
            "rows": [],
        }
    except OSError as exc:
        return {
            "status": "public_breadth_fetch_failed",
            "source": SINA_BREADTH_SOURCE,
            "error_type": type(exc).__name__,
            "rows": [],
        }
    if completed.returncode != 0:
        return {
            "status": "public_breadth_fetch_failed",
            "source": SINA_BREADTH_SOURCE,
            "error_type": "WorkerProcessError",
            "worker_exit_code": completed.returncode,
            "rows": [],
        }
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {
            "status": "public_breadth_fetch_failed",
            "source": SINA_BREADTH_SOURCE,
            "error_type": "InvalidWorkerOutput",
            "rows": [],
        }
    return payload if isinstance(payload, dict) else {
        "status": "public_breadth_fetch_failed",
        "source": SINA_BREADTH_SOURCE,
        "error_type": "InvalidWorkerPayload",
        "rows": [],
    }


def fetch_sina_public_market_breadth(
    *,
    benchmark_trade_date: Optional[str],
    timeout_seconds: float = DEFAULT_PUBLIC_BREADTH_TIMEOUT_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compatibility entry for consumers that only need breadth fields."""
    return fetch_sina_public_market_snapshot(
        benchmark_trade_date=benchmark_trade_date,
        timeout_seconds=timeout_seconds,
        now=now,
    )


def _worker_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--benchmark-trade-date", required=True)
    parser.add_argument("--now", required=True)
    args = parser.parse_args(argv)
    if not args.worker:
        return 2
    try:
        provider_anchor = _load_sina_anchor()
        try:
            provider_expected_counts = _load_sina_expected_counts()
        except Exception as exc:
            result = {
                "status": "public_snapshot_expected_counts_unavailable",
                "source": SINA_BREADTH_SOURCE,
                "error_type": type(exc).__name__,
                "rows": [],
            }
        else:
            snapshot = _load_sina_spot()
            result = _normalize_sina_snapshot(
                snapshot,
                benchmark_trade_date=args.benchmark_trade_date,
                provider_anchor=provider_anchor,
                provider_expected_counts=provider_expected_counts,
                now=datetime.fromisoformat(args.now),
            )
    except Exception as exc:
        result = {
            "status": "public_breadth_fetch_failed",
            "source": SINA_BREADTH_SOURCE,
            "error_type": type(exc).__name__,
            "rows": [],
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
