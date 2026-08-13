"""Deterministic public A-share preselection and Tencent verification."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from app.services.a_share_permissions import classify_a_share_board

from app.services.a_share_market_regime import MIN_BREADTH_UNIVERSE_SIZE
from app.services.investment_policy import (
    classify_investment_objective,
    objective_tier_rank,
)
from app.services.public_market_breadth import (
    MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO,
)
from app.services.tencent_quote_service import (
    TENCENT_RESEARCH_FRESHNESS_REJECTION_STATUSES,
    assess_tencent_research_quote_freshness,
)


MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES = 160
_MAX_CANDIDATES = MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES
_MAX_TENCENT_CANDIDATES = MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES
_TENCENT_QUALITY_CORE_NUMERATOR = 5
_TENCENT_QUALITY_CORE_DENOMINATOR = 8
_A_SHARE_ONE_LOT_SIZE = 100
_MIN_AMOUNT = 100_000_000.0
_MIN_TOTAL_MV = 2_000_000_000.0
_MIN_CIRC_MV = 1_000_000_000.0
_BUCKETS = ("pullback", "strength")
_EXCHANGES = ("sh", "sz", "bj")
_SNAPSHOT_METRIC_FAILURE_STATUSES = frozenset(
    {
        "public_snapshot_coverage_incomplete",
        "public_breadth_universe_too_small",
    }
)

# Stable audit vocabulary shared with consumers that validate discovery
# metadata. These are rejection outcomes, not discovery failures.
PUBLIC_CANDIDATE_REJECTION_KEYS = frozenset(
    {
        "amplitude_out_of_range",
        "below_min_amount",
        "below_min_circ_mv",
        "below_min_total_mv",
        "code_mismatch",
        "duplicate_code",
        "exchange_mismatch",
        "invalid_amplitude",
        "invalid_amount",
        "invalid_circ_mv",
        "invalid_limit_up",
        "invalid_pct_chg",
        "invalid_price",
        "invalid_quote",
        "invalid_response",
        "invalid_total_mv",
        "invalid_turnover_rate",
        "missing_amplitude",
        "missing_circ_mv",
        "missing_pct_chg",
        "missing_response",
        "missing_total_mv",
        "missing_turnover_rate",
        "near_limit_up",
        "outside_move_window",
        "special_treatment",
        "stale_quote",
        "turnover_rate_out_of_range",
        "unexpected_code",
        "unsupported_code",
    }
) | TENCENT_RESEARCH_FRESHNESS_REJECTION_STATUSES

logger = logging.getLogger(__name__)


class PublicCandidateDiscoveryInputError(ValueError):
    """Raised when public candidate ranking receives unusable input."""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _normalized_code(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def _exchange_for_code(code: str) -> Optional[str]:
    if re.fullmatch(r"6\d{5}", code):
        return "sh"
    if re.fullmatch(r"[03]\d{5}", code):
        return "sz"
    if re.fullmatch(r"(?:43|83|87|88|92)\d{4}", code):
        return "bj"
    return None


def _response_envelope_code(row: Mapping[str, Any]) -> Optional[str]:
    if "envelope_code" in row:
        envelope_code = row.get("envelope_code")
        return (
            envelope_code
            if isinstance(envelope_code, str)
            and re.fullmatch(r"[0-9]{6}", envelope_code)
            else None
        )

    provider_symbol = row.get("provider_symbol")
    if isinstance(provider_symbol, str):
        match = re.fullmatch(r"(?:sh|sz|bj)([0-9]{6})", provider_symbol)
        if match is not None:
            return match.group(1)

    raw_code = row.get("code")
    return raw_code if isinstance(raw_code, str) else None


def _parse_status_rejection(row: Mapping[str, Any]) -> Optional[str]:
    if "parse_status" not in row or row.get("parse_status") == "ok":
        return None
    return (
        "invalid_price"
        if row.get("parse_status") == "invalid_price"
        else "invalid_response"
    )


def midrank_percentiles(values: Sequence[float]) -> List[float]:
    """Return deterministic midrank percentiles in the original value order."""

    normalized: List[float] = []
    for value in values:
        number = _number(value)
        if number is None:
            raise ValueError("midrank values must be finite numbers")
        normalized.append(number)

    size = len(normalized)
    if size == 0:
        return []
    if size == 1:
        return [1.0]

    ranked = sorted(enumerate(normalized), key=lambda item: (item[1], item[0]))
    percentiles = [0.0] * size
    start = 0
    while start < size:
        end = start + 1
        value = ranked[start][1]
        while end < size and ranked[end][1] == value:
            end += 1
        percentile = (start + 0.5 * (end - start - 1)) / (size - 1)
        for position in range(start, end):
            original_index = ranked[position][0]
            percentiles[original_index] = percentile
        start = end
    return percentiles


def _move_quality(bucket: str, pct_chg: float) -> float:
    if bucket == "strength":
        quality = (
            (pct_chg - 0.3) / (1.5 - 0.3)
            if pct_chg <= 1.5
            else (3.0 - pct_chg) / (3.0 - 1.5)
        )
    else:
        quality = (
            (pct_chg - (-1.5)) / (-0.5 - (-1.5))
            if pct_chg <= -0.5
            else (0.3 - pct_chg) / (0.3 - (-0.5))
        )
    return min(1.0, max(0.0, quality))


def _ranking_key(item: Mapping[str, Any]) -> tuple:
    return (
        objective_tier_rank(item.get("objective_tier")),
        -item["public_score"],
        -item["amount"],
        item["one_lot_amount"],
        item["code"],
    )


def _selection_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise PublicCandidateDiscoveryInputError("invalid_limit")
    return min(_MAX_CANDIDATES, limit)


def _result(
    *,
    benchmark_trade_date: Optional[str],
    definitions: List[Dict[str, Any]],
    rejection_counts: Counter[str],
    eligible: List[Dict[str, Any]],
) -> Dict[str, Any]:
    eligible_counter = Counter(item["bucket"] for item in eligible)
    selected_counter = Counter(item["bucket"] for item in definitions)
    return {
        "status": "ok" if eligible else "no_eligible_candidates",
        "definitions": definitions,
        "benchmark_trade_date": benchmark_trade_date,
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "eligible_count": len(eligible),
        "eligible_bucket_counts": {
            bucket: eligible_counter[bucket] for bucket in _BUCKETS
        },
        "selected_bucket_counts": {
            bucket: selected_counter[bucket] for bucket in _BUCKETS
        },
        "public_preselected_count": len(definitions),
    }


def rank_public_candidate_universe(
    rows: Iterable[Dict[str, Any]],
    *,
    benchmark_trade_date: str,
    limit: int = 40,
) -> Dict[str, Any]:
    """Filter and rank Task 1 public snapshot rows without private data."""

    benchmark_date = _date_text(benchmark_trade_date)
    if benchmark_date is None:
        raise PublicCandidateDiscoveryInputError("invalid_benchmark_trade_date")
    candidate_limit = _selection_limit(limit)
    try:
        snapshot_rows = list(rows)
    except Exception as exc:
        raise PublicCandidateDiscoveryInputError(
            "snapshot_rows_unavailable"
        ) from exc

    same_day_code_counts: Counter[str] = Counter()
    for row in snapshot_rows:
        if not isinstance(row, Mapping):
            continue
        code = _normalized_code(row.get("code"))
        if (
            _exchange_for_code(code) is not None
            and _date_text(row.get("trade_date")) == benchmark_date
        ):
            same_day_code_counts[code] += 1
    duplicate_codes = {
        code for code, count in same_day_code_counts.items() if count > 1
    }

    rejection_counts: Counter[str] = Counter()
    reported_duplicate_codes = set()
    eligible: List[Dict[str, Any]] = []

    for row in snapshot_rows:
        if not isinstance(row, Mapping):
            rejection_counts["invalid_quote"] += 1
            continue

        code = _normalized_code(row.get("code"))
        expected_exchange = _exchange_for_code(code)
        if expected_exchange is None:
            rejection_counts["unsupported_code"] += 1
            continue
        exchange = str(row.get("exchange") or "").strip().lower()
        if exchange != expected_exchange:
            rejection_counts["exchange_mismatch"] += 1
            continue
        if _date_text(row.get("trade_date")) != benchmark_date:
            rejection_counts["stale_quote"] += 1
            continue
        if code in duplicate_codes:
            if code not in reported_duplicate_codes:
                rejection_counts["duplicate_code"] += 1
                reported_duplicate_codes.add(code)
            continue

        name = str(row.get("name") or "").strip()
        if "ST" in name.upper() or "退" in name:
            rejection_counts["special_treatment"] += 1
            continue

        close = _number(row.get("close"))
        pct_chg = _number(row.get("pct_chg"))
        amount = _number(row.get("amount"))
        if (
            close is None
            or close <= 0
            or pct_chg is None
            or amount is None
            or amount <= 0
        ):
            rejection_counts["invalid_quote"] += 1
            continue

        one_lot_amount = close * 100
        if not math.isfinite(one_lot_amount):
            rejection_counts["invalid_quote"] += 1
            continue
        if amount < _MIN_AMOUNT:
            rejection_counts["below_min_amount"] += 1
            continue
        if pct_chg < -1.5 or pct_chg > 3.0:
            rejection_counts["outside_move_window"] += 1
            continue

        bucket = "strength" if pct_chg >= 0.3 else "pullback"
        eligible.append(
            {
                "code": code,
                "name": name,
                "exchange": expected_exchange,
                "price": close,
                "pct_change": pct_chg,
                "amount": amount,
                "one_lot_amount": round(one_lot_amount, 2),
                "bucket": bucket,
                "trade_date": benchmark_date,
                **classify_investment_objective(code, name),
            }
        )

    if not eligible:
        return _result(
            benchmark_trade_date=benchmark_date,
            definitions=[],
            rejection_counts=rejection_counts,
            eligible=[],
        )

    amount_percentiles = midrank_percentiles([item["amount"] for item in eligible])
    for item, amount_percentile in zip(eligible, amount_percentiles):
        move_quality = _move_quality(item["bucket"], item["pct_change"])
        public_score = 0.65 * amount_percentile + 0.35 * move_quality
        item["amount_percentile"] = amount_percentile
        item["move_quality"] = move_quality
        item["public_score"] = min(1.0, max(0.0, public_score))

    eligible.sort(key=_ranking_key)
    strength_quota = math.ceil(candidate_limit * 0.75)
    pullback_quota = candidate_limit - strength_quota
    by_bucket = {
        bucket: [item for item in eligible if item["bucket"] == bucket]
        for bucket in _BUCKETS
    }

    selected = (
        by_bucket["strength"][:strength_quota]
        + by_bucket["pullback"][:pullback_quota]
    )
    selected_codes = {item["code"] for item in selected}
    if len(selected) < candidate_limit:
        remaining = [item for item in eligible if item["code"] not in selected_codes]
        selected.extend(remaining[: candidate_limit - len(selected)])
    selected.sort(key=_ranking_key)

    return _result(
        benchmark_trade_date=benchmark_date,
        definitions=[dict(item) for item in selected],
        rejection_counts=rejection_counts,
        eligible=eligible,
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_safe(item) for item in value]
    return str(value)


def _validated_definitions(
    definitions: Sequence[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    if definitions is None or isinstance(
        definitions,
        (str, bytes, bytearray, Mapping),
    ):
        raise PublicCandidateDiscoveryInputError("definitions_unavailable")
    try:
        items = list(definitions)
    except Exception as exc:
        raise PublicCandidateDiscoveryInputError("definitions_unavailable") from exc

    normalized: List[Dict[str, Any]] = []
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise PublicCandidateDiscoveryInputError("invalid_definition")
        raw_code = item.get("code")
        if not isinstance(raw_code, str) or raw_code != raw_code.strip():
            raise PublicCandidateDiscoveryInputError("invalid_definition_code")
        code = raw_code
        expected_exchange = _exchange_for_code(code)
        if expected_exchange is None:
            raise PublicCandidateDiscoveryInputError("invalid_definition_code")
        if item.get("exchange") != expected_exchange:
            raise PublicCandidateDiscoveryInputError("invalid_definition_exchange")
        if code in by_code:
            raise PublicCandidateDiscoveryInputError("duplicate_definition_code")
        safe_item = _json_safe(item)
        normalized.append(safe_item)
        by_code[code] = safe_item
    return normalized, by_code


def _verification_result(
    *,
    status: str,
    benchmark_trade_date: str,
    definitions: List[Dict[str, Any]],
    quote_map: Dict[str, Dict[str, Any]],
    rejection_counts: Counter[str],
    quality_counts: Counter[str],
    requested_count: int,
    minimum_verified_count: int,
    verified_count: int,
    rank_population_count: int,
) -> Dict[str, Any]:
    return {
        "status": status,
        "definitions": definitions,
        "quote_map": quote_map,
        "benchmark_trade_date": benchmark_trade_date,
        "rejection_counts": dict(
            sorted(
                (key, int(value))
                for key, value in rejection_counts.items()
                if value
            )
        ),
        "quality_counts": dict(
            sorted(
                (key, int(value))
                for key, value in quality_counts.items()
                if value
            )
        ),
        "tencent_requested_count": requested_count,
        "minimum_verified_count": minimum_verified_count,
        "tencent_verified_count": verified_count,
        "tencent_rank_population_count": rank_population_count,
        "selected_count": len(definitions),
    }


def _required_numeric_field(
    row: Mapping[str, Any],
    key: str,
    rejection_counts: Counter[str],
) -> Optional[float]:
    if key not in row or row.get(key) is None:
        rejection_counts[f"missing_{key}"] += 1
        return None
    number = _number(row.get(key))
    if number is None:
        rejection_counts[f"invalid_{key}"] += 1
        return None
    return number


def _volume_ratio_quality(
    row: Mapping[str, Any],
    quality_counts: Counter[str],
) -> tuple[Optional[float], float]:
    if "volume_ratio" not in row or row.get("volume_ratio") is None:
        quality_counts["missing_volume_ratio"] += 1
        return None, 0.0
    volume_ratio = _number(row.get("volume_ratio"))
    if volume_ratio is None or volume_ratio < 0:
        quality_counts["invalid_volume_ratio"] += 1
        return None, 0.0
    if 0.8 <= volume_ratio <= 2.0:
        return volume_ratio, 1.0
    quality_counts["non_ideal_volume_ratio"] += 1
    if 0.5 <= volume_ratio < 0.8 or 2.0 < volume_ratio <= 3.0:
        return volume_ratio, 0.5
    return volume_ratio, 0.0


def _select_tencent_candidates(
    rank_population: Sequence[Dict[str, Any]],
    *,
    candidate_limit: int,
) -> List[Dict[str, Any]]:
    """Keep a quality core while reserving bounded one-lot cost diversity."""

    ranked: List[Dict[str, Any]] = []
    for quality_rank, raw_candidate in enumerate(rank_population, start=1):
        candidate = dict(raw_candidate)
        candidate["tencent_quality_rank"] = quality_rank
        candidate["tencent_one_lot_amount"] = round(
            float(candidate["tencent_price"]) * _A_SHARE_ONE_LOT_SIZE,
            2,
        )
        ranked.append(candidate)

    if len(ranked) <= candidate_limit:
        return [
            {**candidate, "selection_lane": "quality_core"}
            for candidate in ranked
        ]

    quality_core_count = min(
        candidate_limit,
        max(
            1,
            math.ceil(
                candidate_limit
                * _TENCENT_QUALITY_CORE_NUMERATOR
                / _TENCENT_QUALITY_CORE_DENOMINATOR
            ),
        ),
    )
    low_cost_pool_size = math.ceil(len(ranked) / 2)
    low_cost_codes = {
        candidate["code"]
        for candidate in sorted(
            ranked,
            key=lambda item: (
                item["tencent_one_lot_amount"],
                -item["tencent_score"],
                item["code"],
            ),
        )[:low_cost_pool_size]
    }

    selected_lanes = {
        candidate["code"]: "quality_core"
        for candidate in ranked[:quality_core_count]
    }
    for candidate in ranked:
        if len(selected_lanes) >= candidate_limit:
            break
        code = candidate["code"]
        if code in low_cost_codes and code not in selected_lanes:
            selected_lanes[code] = "one_lot_diversity"
    for candidate in ranked:
        if len(selected_lanes) >= candidate_limit:
            break
        code = candidate["code"]
        if code not in selected_lanes:
            selected_lanes[code] = "quality_fill"

    return [
        {**candidate, "selection_lane": selected_lanes[candidate["code"]]}
        for candidate in ranked
        if candidate["code"] in selected_lanes
    ]


def _minimum_tencent_verified_count(requested_count: int) -> int:
    return max(
        math.ceil(0.8 * requested_count),
        min(20, requested_count),
    )


def verify_and_rank_tencent_candidates(
    definitions: Sequence[Dict[str, Any]],
    quote_rows: Sequence[Dict[str, Any]],
    *,
    benchmark_trade_date: str,
    now: datetime,
    limit: int = 8,
    _minimum_verified_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate Tencent coverage, apply hard gates, and rank candidates."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise PublicCandidateDiscoveryInputError("invalid_tencent_limit")
    candidate_limit = min(_MAX_TENCENT_CANDIDATES, limit)
    benchmark_date = _date_text(benchmark_trade_date)
    if benchmark_date is None:
        raise PublicCandidateDiscoveryInputError("invalid_benchmark_trade_date")
    if not isinstance(now, datetime):
        raise PublicCandidateDiscoveryInputError("invalid_now")
    normalized_definitions, definitions_by_code = _validated_definitions(definitions)
    requested_codes = [item["code"] for item in normalized_definitions]
    requested_count = len(requested_codes)
    minimum_verified_count = (
        _minimum_tencent_verified_count(requested_count)
        if _minimum_verified_count is None
        else _minimum_verified_count
    )
    if requested_count == 0:
        return _verification_result(
            status="no_eligible_candidates",
            benchmark_trade_date=benchmark_date,
            definitions=[],
            quote_map={},
            rejection_counts=Counter(),
            quality_counts=Counter(),
            requested_count=0,
            minimum_verified_count=0,
            verified_count=0,
            rank_population_count=0,
        )

    if quote_rows is None or isinstance(
        quote_rows,
        (str, bytes, bytearray, Mapping),
    ):
        raise PublicCandidateDiscoveryInputError("quote_rows_unavailable")
    try:
        rows = list(quote_rows)
    except Exception as exc:
        raise PublicCandidateDiscoveryInputError("quote_rows_unavailable") from exc

    requested_set = set(requested_codes)
    response_counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        envelope_code = _response_envelope_code(row)
        if envelope_code in requested_set:
            response_counts[envelope_code] += 1
    duplicate_codes = {
        code for code, count in response_counts.items() if count > 1
    }

    rejection_counts: Counter[str] = Counter()
    rejection_counts["duplicate_code"] = len(duplicate_codes)
    quote_map: Dict[str, Dict[str, Any]] = {}
    verified_rows: List[tuple[Dict[str, Any], Mapping[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            rejection_counts["invalid_response"] += 1
            continue
        envelope_code = _response_envelope_code(row)
        if envelope_code not in requested_set:
            rejection_counts["unexpected_code"] += 1
            continue
        code = envelope_code
        parse_status_rejection = _parse_status_rejection(row)
        if code in duplicate_codes:
            if parse_status_rejection is not None:
                rejection_counts[parse_status_rejection] += 1
                continue
            close = _number(row.get("close"))
            if close is None or close <= 0:
                rejection_counts["invalid_price"] += 1
            amount = _number(row.get("amount"))
            if amount is None or amount <= 0:
                rejection_counts["invalid_amount"] += 1
            continue

        definition = definitions_by_code[code]
        expected_symbol = f"{definition['exchange']}{code}"
        provider_symbol = row.get("provider_symbol")
        payload_code = row.get("payload_code")
        raw_code = row.get("code")
        if (
            not isinstance(provider_symbol, str)
            or provider_symbol != expected_symbol
            or provider_symbol[2:] != code
            or (payload_code is not None and payload_code != code)
            or raw_code != code
        ):
            rejection_counts["code_mismatch"] += 1
            continue

        if parse_status_rejection is not None:
            rejection_counts[parse_status_rejection] += 1
            continue

        close = _number(row.get("close"))
        if close is None or close <= 0:
            rejection_counts["invalid_price"] += 1
            continue
        amount = _number(row.get("amount"))
        if amount is None or amount <= 0:
            rejection_counts["invalid_amount"] += 1
            continue

        freshness = assess_tencent_research_quote_freshness(
            dict(row),
            benchmark_trade_date=benchmark_date,
            now=now,
        )
        if freshness.get("data_complete") is not True:
            freshness_status = str(freshness.get("status") or "invalid_freshness")
            rejection_counts[freshness_status] += 1
            continue

        safe_quote = _json_safe(row)
        safe_quote.update(
            {
                "code": code,
                "provider_symbol": expected_symbol,
                "source": "tencent",
                "close": close,
                "amount": amount,
                "trade_at": freshness.get("trade_at"),
                "trade_date": freshness.get("trade_date"),
            }
        )
        quote_map[code] = safe_quote
        verified_rows.append((definition, row))

    for code in requested_codes:
        if response_counts[code] == 0:
            rejection_counts["missing_response"] += 1

    verified_count = len(verified_rows)
    if verified_count < minimum_verified_count:
        return _verification_result(
            status="candidate_discovery_unavailable",
            benchmark_trade_date=benchmark_date,
            definitions=[],
            quote_map={},
            rejection_counts=rejection_counts,
            quality_counts=Counter(),
            requested_count=requested_count,
            minimum_verified_count=minimum_verified_count,
            verified_count=verified_count,
            rank_population_count=0,
        )

    quality_counts: Counter[str] = Counter()
    rank_population: List[Dict[str, Any]] = []
    for definition, row in verified_rows:
        pct_chg = _required_numeric_field(row, "pct_chg", rejection_counts)
        if pct_chg is None:
            continue
        if pct_chg < -1.5 or pct_chg > 3.0:
            rejection_counts["outside_move_window"] += 1
            continue

        turnover_rate = _required_numeric_field(
            row,
            "turnover_rate",
            rejection_counts,
        )
        if turnover_rate is None:
            continue
        if turnover_rate < 0 or turnover_rate > 10:
            rejection_counts["turnover_rate_out_of_range"] += 1
            continue

        amplitude = _required_numeric_field(row, "amplitude", rejection_counts)
        if amplitude is None:
            continue
        if amplitude < 0 or amplitude > 8:
            rejection_counts["amplitude_out_of_range"] += 1
            continue

        total_mv = _required_numeric_field(row, "total_mv", rejection_counts)
        if total_mv is None:
            continue
        if total_mv < _MIN_TOTAL_MV:
            rejection_counts["below_min_total_mv"] += 1
            continue

        circ_mv = _required_numeric_field(row, "circ_mv", rejection_counts)
        if circ_mv is None:
            continue
        if circ_mv < _MIN_CIRC_MV:
            rejection_counts["below_min_circ_mv"] += 1
            continue

        close = _number(row.get("close"))
        amount = _number(row.get("amount"))
        limit_up: Optional[float] = None
        if row.get("limit_up") is not None:
            limit_up = _number(row.get("limit_up"))
            if limit_up is None or limit_up <= 0:
                rejection_counts["invalid_limit_up"] += 1
                continue
            if close is not None and (limit_up - close) / limit_up <= 0.005:
                rejection_counts["near_limit_up"] += 1
                continue

        volume_ratio, volume_ratio_quality = _volume_ratio_quality(
            row,
            quality_counts,
        )
        bucket = "strength" if pct_chg >= 0.3 else "pullback"
        move_quality = _move_quality(bucket, pct_chg)
        if turnover_rate <= 0.5:
            turnover_quality = turnover_rate / 0.5
        elif turnover_rate <= 5.0:
            turnover_quality = 1.0
        else:
            turnover_quality = (10.0 - turnover_rate) / 5.0
        amplitude_quality = (
            1.0 if amplitude <= 4.0 else (8.0 - amplitude) / 4.0
        )
        if move_quality < 1.0:
            quality_counts["reduced_move_quality"] += 1
        if turnover_quality < 1.0:
            quality_counts["reduced_turnover_quality"] += 1
        if amplitude_quality < 1.0:
            quality_counts["reduced_amplitude_quality"] += 1

        candidate = dict(definition)
        candidate.update(
            {
                "tencent_price": close,
                "tencent_pct_change": pct_chg,
                "tencent_amount": amount,
                "tencent_trade_at": quote_map[definition["code"]].get("trade_at"),
                "tencent_source": "tencent_batch_quotes",
                "tencent_bucket": bucket,
                "turnover_rate": turnover_rate,
                "volume_ratio": volume_ratio,
                "amplitude": amplitude,
                "circ_mv": circ_mv,
                "total_mv": total_mv,
                "limit_up": limit_up,
                "tencent_move_quality": move_quality,
                "turnover_quality": turnover_quality,
                "volume_ratio_quality": volume_ratio_quality,
                "amplitude_quality": amplitude_quality,
            }
        )
        rank_population.append(candidate)

    amount_percentiles = midrank_percentiles(
        [item["tencent_amount"] for item in rank_population]
    )
    market_cap_percentiles = midrank_percentiles(
        [item["total_mv"] for item in rank_population]
    )
    for candidate, amount_percentile, market_cap_percentile in zip(
        rank_population,
        amount_percentiles,
        market_cap_percentiles,
    ):
        score = (
            0.30 * amount_percentile
            + 0.25 * candidate["tencent_move_quality"]
            + 0.15 * candidate["turnover_quality"]
            + 0.10 * candidate["volume_ratio_quality"]
            + 0.10 * candidate["amplitude_quality"]
            + 0.10 * market_cap_percentile
        )
        candidate["tencent_amount_percentile"] = amount_percentile
        candidate["tencent_market_cap_percentile"] = market_cap_percentile
        candidate["tencent_score"] = min(1.0, max(0.0, score))

    rank_population.sort(
        key=lambda item: (
            objective_tier_rank(item.get("objective_tier")),
            -item["tencent_score"],
            -item["tencent_amount"],
            item["amplitude"],
            item["code"],
        )
    )
    selected = _select_tencent_candidates(
        rank_population,
        candidate_limit=candidate_limit,
    )
    return _verification_result(
        status="ok" if selected else "no_eligible_candidates",
        benchmark_trade_date=benchmark_date,
        definitions=selected,
        quote_map=quote_map,
        rejection_counts=rejection_counts,
        quality_counts=quality_counts,
        requested_count=requested_count,
        minimum_verified_count=minimum_verified_count,
        verified_count=verified_count,
        rank_population_count=len(rank_population),
    )


def _empty_snapshot_context() -> Dict[str, Any]:
    empty_counts = {exchange: 0 for exchange in _EXCHANGES}
    return {
        "source": "unknown",
        "benchmark_trade_date": None,
        "provider_expected_count": 0,
        "provider_expected_exchange_counts": dict(empty_counts),
        "raw_row_count": 0,
        "unique_row_count": 0,
        "universe_count": 0,
        "exchange_counts": dict(empty_counts),
        "total_coverage_ratio": 0.0,
        "exchange_coverage_ratio": {
            exchange: 0.0 for exchange in _EXCHANGES
        },
        "checked_at": None,
        "freshness": "unavailable",
        "degraded": False,
        "provider_errors": [],
        "rows": [],
    }


def _snapshot_audit_metadata(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    provider_errors = snapshot.get("provider_errors")
    return {
        "checked_at": (
            str(snapshot.get("checked_at"))
            if snapshot.get("checked_at") not in (None, "")
            else None
        ),
        "freshness": str(snapshot.get("freshness") or "unknown"),
        "degraded": snapshot.get("degraded") is True,
        "provider_errors": (
            [
                _json_safe(dict(item))
                for item in provider_errors
                if isinstance(item, Mapping)
            ]
            if isinstance(provider_errors, list)
            else []
        ),
    }


def _snapshot_integer(value: Any, *, positive: bool = False) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < (1 if positive else 0):
        return None
    return value


def _snapshot_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _validated_success_snapshot_context(
    snapshot: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    source = snapshot.get("source")
    benchmark = snapshot.get("benchmark_trade_date")
    provider_trade_date = snapshot.get("provider_trade_date")
    if (
        not isinstance(source, str)
        or not source.strip()
        or not isinstance(benchmark, str)
        or _date_text(benchmark) != benchmark
        or not isinstance(provider_trade_date, str)
        or _date_text(provider_trade_date) != provider_trade_date
        or provider_trade_date != benchmark
    ):
        return None

    expected_total = _snapshot_integer(
        snapshot.get("provider_expected_count"),
        positive=True,
    )
    expected_by_exchange = snapshot.get("provider_expected_exchange_counts")
    if expected_total is None or not isinstance(expected_by_exchange, Mapping):
        return None
    normalized_expected = {
        exchange: _snapshot_integer(
            expected_by_exchange.get(exchange),
            positive=True,
        )
        for exchange in _EXCHANGES
    }
    if (
        any(value is None for value in normalized_expected.values())
        or sum(normalized_expected.values()) != expected_total
    ):
        return None

    rows = snapshot.get("rows")
    raw_count = _snapshot_integer(snapshot.get("raw_row_count"))
    unique_count = _snapshot_integer(snapshot.get("unique_row_count"))
    universe_value = snapshot.get(
        "universe_count",
        snapshot.get("universe_size"),
    )
    universe_count = _snapshot_integer(universe_value)
    if (
        not isinstance(rows, list)
        or raw_count is None
        or unique_count is None
        or universe_count is None
        or raw_count < unique_count
        or unique_count < MIN_BREADTH_UNIVERSE_SIZE
        or unique_count != universe_count
        or unique_count != len(rows)
    ):
        return None
    if (
        "universe_count" in snapshot
        and "universe_size" in snapshot
        and snapshot.get("universe_count") != snapshot.get("universe_size")
    ):
        return None
    for key in (
        "duplicate_count",
        "excluded_stale_count",
        "excluded_future_time_count",
    ):
        if key in snapshot and _snapshot_integer(snapshot.get(key)) is None:
            return None

    actual_counts: Counter[str] = Counter()
    seen_codes = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        code = row.get("code")
        exchange = row.get("exchange")
        if (
            not isinstance(code, str)
            or code in seen_codes
            or exchange not in _EXCHANGES
            or _exchange_for_code(code) != exchange
            or row.get("trade_date") != benchmark
        ):
            return None
        close = None if isinstance(row.get("close"), bool) else _number(row.get("close"))
        pct_chg = None if isinstance(row.get("pct_chg"), bool) else _number(row.get("pct_chg"))
        amount = None if isinstance(row.get("amount"), bool) else _number(row.get("amount"))
        if (
            close is None
            or close <= 0
            or pct_chg is None
            or amount is None
            or amount <= 0
        ):
            return None
        seen_codes.add(code)
        actual_counts[exchange] += 1

    declared_counts = snapshot.get("exchange_counts")
    if not isinstance(declared_counts, Mapping):
        return None
    normalized_counts = {
        exchange: _snapshot_integer(declared_counts.get(exchange))
        for exchange in _EXCHANGES
    }
    if (
        any(value is None for value in normalized_counts.values())
        or normalized_counts != {
            exchange: actual_counts[exchange] for exchange in _EXCHANGES
        }
        or sum(normalized_counts.values()) != unique_count
    ):
        return None

    total_ratio = _snapshot_number(snapshot.get("total_coverage_ratio"))
    exchange_ratios = snapshot.get("exchange_coverage_ratio")
    if total_ratio is None or not isinstance(exchange_ratios, Mapping):
        return None
    normalized_ratios = {
        exchange: _snapshot_number(exchange_ratios.get(exchange))
        for exchange in _EXCHANGES
    }
    if any(value is None for value in normalized_ratios.values()):
        return None
    recalculated_total_ratio = unique_count / expected_total
    recalculated_exchange_ratios = {
        exchange: normalized_counts[exchange] / normalized_expected[exchange]
        for exchange in _EXCHANGES
    }
    if not math.isclose(
        total_ratio,
        recalculated_total_ratio,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ) or any(
        not math.isclose(
            normalized_ratios[exchange],
            recalculated_exchange_ratios[exchange],
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        for exchange in _EXCHANGES
    ):
        return None
    if total_ratio < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO or any(
        normalized_ratios[exchange] < MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
        for exchange in _EXCHANGES
    ):
        return None

    return {
        "source": source.strip(),
        "benchmark_trade_date": benchmark,
        "provider_expected_count": expected_total,
        "provider_expected_exchange_counts": normalized_expected,
        "raw_row_count": raw_count,
        "unique_row_count": unique_count,
        "universe_count": universe_count,
        "exchange_counts": normalized_counts,
        "total_coverage_ratio": total_ratio,
        "exchange_coverage_ratio": normalized_ratios,
        "rows": rows,
    }


def _validated_failure_snapshot_context(
    snapshot: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    source = snapshot.get("source")
    benchmark = snapshot.get("benchmark_trade_date")
    if (
        not isinstance(source, str)
        or not source.strip()
        or not isinstance(benchmark, str)
        or _date_text(benchmark) != benchmark
        or snapshot.get("status") not in _SNAPSHOT_METRIC_FAILURE_STATUSES
    ):
        return None

    expected_total = _snapshot_integer(
        snapshot.get("provider_expected_count"),
        positive=True,
    )
    expected_by_exchange = snapshot.get("provider_expected_exchange_counts")
    if (
        expected_total is None
        or not isinstance(expected_by_exchange, Mapping)
        or set(expected_by_exchange) != set(_EXCHANGES)
    ):
        return None
    normalized_expected = {
        exchange: _snapshot_integer(
            expected_by_exchange.get(exchange),
            positive=True,
        )
        for exchange in _EXCHANGES
    }
    if (
        any(value is None for value in normalized_expected.values())
        or sum(normalized_expected.values()) != expected_total
    ):
        return None

    raw_count = _snapshot_integer(snapshot.get("raw_row_count"))
    unique_count = _snapshot_integer(snapshot.get("unique_row_count"))
    universe_values = [
        _snapshot_integer(snapshot.get(key))
        for key in ("universe_count", "universe_size")
        if key in snapshot
    ]
    if (
        raw_count is None
        or unique_count is None
        or not universe_values
        or any(value is None for value in universe_values)
        or len(set(universe_values)) != 1
        or raw_count < unique_count
        or unique_count != universe_values[0]
    ):
        return None

    declared_counts = snapshot.get("exchange_counts")
    if (
        not isinstance(declared_counts, Mapping)
        or set(declared_counts) != set(_EXCHANGES)
    ):
        return None
    normalized_counts = {
        exchange: _snapshot_integer(declared_counts.get(exchange))
        for exchange in _EXCHANGES
    }
    if (
        any(value is None for value in normalized_counts.values())
        or sum(normalized_counts.values()) != unique_count
    ):
        return None

    total_ratio = _snapshot_number(snapshot.get("total_coverage_ratio"))
    exchange_ratios = snapshot.get("exchange_coverage_ratio")
    if (
        total_ratio is None
        or not isinstance(exchange_ratios, Mapping)
        or set(exchange_ratios) != set(_EXCHANGES)
    ):
        return None
    normalized_ratios = {
        exchange: _snapshot_number(exchange_ratios.get(exchange))
        for exchange in _EXCHANGES
    }
    if any(value is None for value in normalized_ratios.values()):
        return None
    if not math.isclose(
        total_ratio,
        unique_count / expected_total,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ) or any(
        not math.isclose(
            normalized_ratios[exchange],
            normalized_counts[exchange] / normalized_expected[exchange],
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        for exchange in _EXCHANGES
    ):
        return None

    failure_status = snapshot["status"]
    if failure_status == "public_breadth_universe_too_small":
        if unique_count >= MIN_BREADTH_UNIVERSE_SIZE:
            return None
    elif failure_status == "public_snapshot_coverage_incomplete":
        coverage_is_complete = (
            total_ratio >= MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
            and all(
                normalized_ratios[exchange]
                >= MIN_PUBLIC_SNAPSHOT_COVERAGE_RATIO
                for exchange in _EXCHANGES
            )
        )
        if unique_count < MIN_BREADTH_UNIVERSE_SIZE or coverage_is_complete:
            return None

    rows = snapshot.get("rows")
    if not isinstance(rows, list) or rows:
        return None

    return {
        "source": source.strip(),
        "benchmark_trade_date": benchmark,
        "provider_expected_count": expected_total,
        "provider_expected_exchange_counts": normalized_expected,
        "raw_row_count": raw_count,
        "unique_row_count": unique_count,
        "universe_count": universe_values[0],
        "exchange_counts": normalized_counts,
        "total_coverage_ratio": total_ratio,
        "exchange_coverage_ratio": normalized_ratios,
        "rows": [],
    }


def _snapshot_context(snapshot: Any) -> tuple[Dict[str, Any], Optional[str]]:
    context = _empty_snapshot_context()
    if not isinstance(snapshot, Mapping):
        return context, "invalid_snapshot_dto"

    source = snapshot.get("source")
    if isinstance(source, str) and source.strip():
        context["source"] = source.strip()

    benchmark = snapshot.get("benchmark_trade_date")
    if isinstance(benchmark, str) and _date_text(benchmark) == benchmark:
        context["benchmark_trade_date"] = benchmark

    raw_status = snapshot.get("status")
    if raw_status != "ok":
        status = raw_status if isinstance(raw_status, str) and raw_status else "invalid_snapshot_dto"
        validated_failure = _validated_failure_snapshot_context(snapshot)
        failure_context = validated_failure or context
        failure_context.update(_snapshot_audit_metadata(snapshot))
        return failure_context, status
    validated = _validated_success_snapshot_context(snapshot)
    if validated is not None:
        validated.update(_snapshot_audit_metadata(snapshot))
    return (
        (validated, None)
        if validated is not None
        else (context, "invalid_snapshot_dto")
    )


def _tencent_fetch_stage_status(
    fetch_result: Any,
    requested_codes: List[str],
) -> Optional[str]:
    if not isinstance(fetch_result, Mapping):
        return "invalid_fetch_dto"
    if fetch_result.get("status") != "ok":
        raw_status = fetch_result.get("status")
        return (
            str(raw_status)
            if isinstance(raw_status, str) and raw_status
            else "invalid_fetch_dto"
        )
    if (
        "error_type" not in fetch_result
        or fetch_result.get("error_type") is not None
    ):
        return "invalid_fetch_dto"
    response_codes = fetch_result.get("requested_codes")
    if not isinstance(response_codes, list):
        return "invalid_fetch_dto"
    if response_codes != requested_codes:
        return "requested_codes_mismatch"
    if not isinstance(fetch_result.get("rows"), list):
        return "invalid_fetch_dto"
    return None


def _discovery_output(
    context: Mapping[str, Any],
    *,
    status: str,
    source: str,
    public_stage_status: str,
    tencent_stage_status: str,
    counts: Optional[Mapping[str, int]] = None,
    rejection_counts: Optional[Mapping[str, int]] = None,
    quality_counts: Optional[Mapping[str, int]] = None,
    definitions: Optional[List[Dict[str, Any]]] = None,
    quote_map: Optional[Dict[str, Dict[str, Any]]] = None,
    stage: Optional[str] = None,
) -> Dict[str, Any]:
    stage_counts = {
        "eligible_count": 0,
        "public_preselected_count": 0,
        "tencent_requested_count": 0,
        "tencent_minimum_verified_count": 0,
        "tencent_verified_count": 0,
        "tencent_rank_population_count": 0,
        "selected_count": 0,
        **(counts or {}),
    }
    candidate_discovery = {
        "mode": "public_full_market",
        "status": status,
        "source": source,
        "benchmark_trade_date": context["benchmark_trade_date"],
        "provider_expected_count": context["provider_expected_count"],
        "provider_expected_exchange_counts": dict(
            context["provider_expected_exchange_counts"]
        ),
        "raw_row_count": context["raw_row_count"],
        "unique_row_count": context["unique_row_count"],
        "universe_count": context["universe_count"],
        "exchange_counts": dict(context["exchange_counts"]),
        "total_coverage_ratio": context["total_coverage_ratio"],
        "exchange_coverage_ratio": dict(context["exchange_coverage_ratio"]),
        "checked_at": context.get("checked_at"),
        "freshness": context.get("freshness") or "unknown",
        "degraded": context.get("degraded") is True,
        "provider_errors": list(context.get("provider_errors") or []),
        **stage_counts,
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
        "rejection_counts": dict(sorted((rejection_counts or {}).items())),
        "quality_counts": dict(sorted((quality_counts or {}).items())),
        "permission_prefilter_excluded_count": len(
            context.get("permission_prefilter_excluded") or []
        ),
        "permission_prefilter_excluded": list(
            context.get("permission_prefilter_excluded") or []
        ),
        "stage_sources": {
            "public_snapshot": {
                "provider": context["source"],
                "status": public_stage_status,
                "checked_at": context.get("checked_at"),
                "freshness": context.get("freshness") or "unknown",
                "degraded": context.get("degraded") is True,
                "provider_errors": list(context.get("provider_errors") or []),
            },
            "tencent_verification": {
                "provider": "tencent_batch_quotes",
                "status": tencent_stage_status,
            },
        },
    }
    result = {
        "status": status,
        "definitions": definitions or [],
        "quote_map": quote_map or {},
        "candidate_discovery": candidate_discovery,
    }
    if stage is not None:
        result["stage"] = stage
    return result


def discover_public_candidate_universe(
    snapshot: Dict[str, Any],
    *,
    fetch_quotes: Callable[[Iterable[str]], Dict[str, Any]],
    now: datetime,
    excluded_code_reasons: Optional[Mapping[str, str]] = None,
    board_exclusion_reasons: Optional[Mapping[str, str]] = None,
    star_market_exclusion_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Run public preselection and one deadline-bounded Tencent review callback."""

    context, snapshot_error = _snapshot_context(snapshot)
    if snapshot_error is not None:
        snapshot_stage = (
            "sina_expected_counts"
            if snapshot_error == "public_snapshot_expected_counts_unavailable"
            else "sina_snapshot"
        )
        return _discovery_output(
            context,
            status="candidate_discovery_unavailable",
            source=context["source"],
            public_stage_status=snapshot_error,
            tencent_stage_status="not_called_public_snapshot_unavailable",
            stage=snapshot_stage,
        )
    public_source = context["source"]
    explicit_exclusions = {
        str(code or "").strip(): str(reason or "user_excluded").strip()
        for code, reason in (excluded_code_reasons or {}).items()
        if re.fullmatch(r"[0-9]{6}", str(code or "").strip())
    }
    board_exclusions = {
        str(board or "").strip().upper(): str(reason or "").strip()
        for board, reason in (board_exclusion_reasons or {}).items()
        if str(board or "").strip() and str(reason or "").strip()
    }
    if star_market_exclusion_reason and "STAR" not in board_exclusions:
        board_exclusions["STAR"] = star_market_exclusion_reason
    permission_prefilter_excluded: List[Dict[str, str]] = []
    filtered_rows: List[Dict[str, Any]] = []
    for row in context["rows"]:
        code = _normalized_code(row.get("code")) if isinstance(row, Mapping) else ""
        reason = explicit_exclusions.get(code)
        board = classify_a_share_board(code)["board"]
        if reason is None:
            reason = board_exclusions.get(str(board))
        if reason:
            permission_prefilter_excluded.append(
                {
                    "code": code,
                    "name": str(row.get("name") or code),
                    "board": str(board),
                    "reason_code": reason,
                }
            )
            continue
        filtered_rows.append(dict(row))
    context = {
        **context,
        "rows": filtered_rows,
        "permission_prefilter_excluded": permission_prefilter_excluded,
    }
    try:
        public_result = rank_public_candidate_universe(
            context["rows"],
            benchmark_trade_date=context["benchmark_trade_date"],
            limit=MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
        )
        if (
            not isinstance(public_result, Mapping)
            or public_result.get("status") not in {"ok", "no_eligible_candidates"}
            or not isinstance(public_result.get("definitions"), list)
            or not isinstance(public_result.get("rejection_counts"), Mapping)
        ):
            raise PublicCandidateDiscoveryInputError("invalid_public_ranking_dto")
        normalized_definitions, _ = _validated_definitions(
            public_result["definitions"]
        )
    except PublicCandidateDiscoveryInputError:
        return _discovery_output(
            context,
            status="candidate_discovery_unavailable",
            source=public_source,
            public_stage_status="ok",
            tencent_stage_status="not_called_public_preselection_unavailable",
            stage="public_preselection",
        )
    except Exception:
        logger.exception("Public candidate preselection failed internally")
        return _discovery_output(
            context,
            status="candidate_discovery_unavailable",
            source=public_source,
            public_stage_status="ok",
            tencent_stage_status="not_called_public_preselection_unavailable",
            stage="public_preselection",
        )

    public_rejection_counts = Counter(public_result["rejection_counts"])
    eligible_count = int(public_result.get("eligible_count") or 0)
    public_preselected_count = len(normalized_definitions)
    counts = {
        "eligible_count": eligible_count,
        "public_preselected_count": public_preselected_count,
        "tencent_requested_count": public_preselected_count,
    }
    if public_preselected_count == 0:
        return _discovery_output(
            context,
            status="no_eligible_candidates",
            source=public_source,
            public_stage_status="ok",
            tencent_stage_status="not_called_no_preselection",
            counts=counts,
            rejection_counts=public_rejection_counts,
        )

    requested_codes = [item["code"] for item in normalized_definitions]
    minimum_verified_count = _minimum_tencent_verified_count(
        public_preselected_count
    )
    counts["tencent_minimum_verified_count"] = minimum_verified_count

    def unavailable(tencent_status: str) -> Dict[str, Any]:
        return _discovery_output(
            context,
            status="candidate_discovery_unavailable",
            source=public_source,
            public_stage_status="ok",
            tencent_stage_status=tencent_status,
            counts=counts,
            rejection_counts=public_rejection_counts,
            stage="tencent_verification",
        )

    if not isinstance(now, datetime):
        return unavailable("invalid_now")

    try:
        fetch_result = fetch_quotes(list(requested_codes))
    except Exception:
        logger.exception("Tencent candidate quote fetcher raised unexpectedly")
        return unavailable("fetch_exception")

    fetch_stage_status = _tencent_fetch_stage_status(
        fetch_result,
        requested_codes,
    )

    if fetch_stage_status is not None:
        return unavailable(fetch_stage_status)

    try:
        verification = verify_and_rank_tencent_candidates(
            normalized_definitions,
            fetch_result["rows"],
            benchmark_trade_date=context["benchmark_trade_date"],
            now=now,
            limit=MAX_PUBLIC_TECHNICAL_SCREEN_CANDIDATES,
            _minimum_verified_count=minimum_verified_count,
        )
    except PublicCandidateDiscoveryInputError:
        return unavailable("invalid_fetch_dto")
    except Exception:
        logger.exception("Tencent candidate verification failed internally")
        return unavailable("internal_error")

    merged_rejection_counts = public_rejection_counts + Counter(
        verification["rejection_counts"]
    )
    verification_status = verification["status"]
    counts.update(
        {
            "tencent_requested_count": verification["tencent_requested_count"],
            "tencent_minimum_verified_count": verification[
                "minimum_verified_count"
            ],
            "tencent_verified_count": verification["tencent_verified_count"],
            "tencent_rank_population_count": verification[
                "tencent_rank_population_count"
            ],
            "selected_count": verification["selected_count"],
        }
    )
    return _discovery_output(
        context,
        status=verification_status,
        source=f"{public_source}+tencent_batch_quotes",
        public_stage_status="ok",
        tencent_stage_status=(
            "coverage_incomplete"
            if verification_status == "candidate_discovery_unavailable"
            else "ok"
        ),
        counts=counts,
        rejection_counts=merged_rejection_counts,
        quality_counts=verification["quality_counts"],
        definitions=verification["definitions"],
        quote_map=verification["quote_map"],
        stage=(
            "tencent_verification"
            if verification_status == "candidate_discovery_unavailable"
            else None
        ),
    )
