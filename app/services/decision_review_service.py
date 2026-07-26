"""Performance review and bounded calibration for audited decision outcomes."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from app.core.database import get_mongo_db


FEATURE_ALLOWLIST = (
    "objective_match",
    "reward_risk",
    "evidence_completeness",
    "actionability",
)

DEFAULT_BASELINE_WEIGHTS = {
    "objective_match": 4.0,
    "reward_risk": 3.0,
    "evidence_completeness": 2.0,
    "actionability": 1.0,
}


class DecisionReviewError(RuntimeError):
    """Raised when audited performance cannot be read or persisted."""


def deterministic_expanding_folds(
    sample_count: int,
    *,
    fold_count: int = 5,
) -> List[Tuple[Tuple[int, ...], Tuple[int, ...]]]:
    """Return expanding chronological folds with a fixed 50% initial window."""

    if sample_count < 2 or fold_count < 1:
        return []
    initial_count = max(1, sample_count // 2)
    validation_indices = np.array_split(
        np.arange(initial_count, sample_count, dtype=int), fold_count
    )
    folds: List[Tuple[Tuple[int, ...], Tuple[int, ...]]] = []
    for validation in validation_indices:
        if len(validation) == 0:
            continue
        start = int(validation[0])
        folds.append(
            (
                tuple(range(start)),
                tuple(int(index) for index in validation.tolist()),
            )
        )
    return folds


def _ridge_coefficients(
    features: np.ndarray,
    target: np.ndarray,
    *,
    alpha: float,
) -> np.ndarray:
    if features.ndim != 2 or features.shape[1] != len(FEATURE_ALLOWLIST):
        raise ValueError("features must contain the four audited calibration columns")
    if len(features) != len(target):
        raise ValueError("features and target must have the same row count")
    if len(features) == 0:
        return np.zeros(len(FEATURE_ALLOWLIST), dtype=float)

    x = np.asarray(features, dtype=float)
    y = np.asarray(target, dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales = np.where(scales > 0.0, scales, 1.0)
    standardized = (x - means) / scales
    centered_target = y - y.mean()
    identity = np.eye(standardized.shape[1], dtype=float)
    return np.linalg.solve(
        standardized.T @ standardized + float(alpha) * identity,
        standardized.T @ centered_target,
    )


def ridge_weight_deltas(
    features: np.ndarray,
    target: np.ndarray,
    *,
    alpha: float = 1.0,
) -> Dict[str, float]:
    """Convert fixed-ridge coefficients into bounded relative weight deltas."""

    coefficients = _ridge_coefficients(features, target, alpha=alpha)
    maximum = float(np.max(np.abs(coefficients))) if len(coefficients) else 0.0
    if not math.isfinite(maximum) or maximum <= 0.0:
        return {key: 0.0 for key in FEATURE_ALLOWLIST}
    return {
        key: float(np.clip(coefficient / maximum, -1.0, 1.0) * 0.1)
        for key, coefficient in zip(FEATURE_ALLOWLIST, coefficients)
    }


def bounded_simplex_weights(
    baseline_weights: Mapping[str, float],
    relative_deltas: Mapping[str, float],
) -> Dict[str, float]:
    """Project proposed weights to +/-10% bounds while preserving their total."""

    keys = sorted(FEATURE_ALLOWLIST)
    baseline = {key: float(baseline_weights[key]) for key in keys}
    lower = {key: baseline[key] * 0.9 for key in keys}
    upper = {key: baseline[key] * 1.1 for key in keys}
    proposed = {
        key: min(
            upper[key],
            max(
                lower[key],
                baseline[key]
                * (1.0 + min(0.1, max(-0.1, float(relative_deltas.get(key, 0.0))))),
            ),
        )
        for key in keys
    }
    target_total = sum(baseline.values())

    for _ in range(32):
        remainder = target_total - sum(proposed.values())
        if abs(remainder) <= 1e-12:
            break
        if remainder > 0:
            adjustable = [key for key in keys if proposed[key] < upper[key] - 1e-12]
        else:
            adjustable = [key for key in keys if proposed[key] > lower[key] + 1e-12]
        if not adjustable:
            raise ValueError("bounded simplex projection is infeasible")
        share = remainder / len(adjustable)
        for key in adjustable:
            proposed[key] = min(upper[key], max(lower[key], proposed[key] + share))

    if abs(target_total - sum(proposed.values())) > 1e-9:
        raise ValueError("bounded simplex projection did not converge")
    return proposed


class DecisionReviewService:
    """Read immutable shadow outcomes and create inactive calibration proposals."""

    CLOSED_STATUSES = (
        "closed_target",
        "closed_stop",
        "closed_manual",
        "closed_time",
        "closed_expiry",
        "closed_corporate_action",
    )
    METRIC_BASIS = "shadow_trade_v1"
    CALIBRATION_VERSION = "decision-calibration-v1"
    TRAINING_WINDOW_DAYS = 180
    MAXIMUM_SAMPLES = 500
    MINIMUM_OVERALL_SAMPLES = 30
    MINIMUM_SUBGROUP_SAMPLES = 10

    def __init__(
        self,
        *,
        db_provider: Callable[[], Any] = get_mongo_db,
        baseline_weights: Optional[Mapping[str, float]] = None,
    ) -> None:
        self._db_provider = db_provider
        supplied = baseline_weights or DEFAULT_BASELINE_WEIGHTS
        self._baseline_weights = {
            key: float(supplied[key]) for key in FEATURE_ALLOWLIST
        }

    async def _get_db(self) -> Any:
        db = self._db_provider()
        return await db if inspect.isawaitable(db) else db

    async def performance(
        self,
        user_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        generated_at = _as_utc(now or datetime.now(timezone.utc))
        try:
            db = await self._get_db()
        except Exception as exc:
            raise DecisionReviewError("decision outcome storage is unavailable") from exc
        query = {
            "user_id": str(user_id),
            "metric_basis": self.METRIC_BASIS,
            "status": {"$in": self.CLOSED_STATUSES},
        }
        try:
            cursor = db["decision_outcomes"].find(query).sort(
                [("exit_at", -1), ("plan_id", 1)]
            ).limit(5000)
            rows = await cursor.to_list(length=5000)
        except Exception as exc:
            raise DecisionReviewError("decision outcomes could not be read") from exc
        normalized = [self._normalize_row(row) for row in rows]
        normalized = [row for row in normalized if row is not None]
        normalized.sort(key=lambda row: (_datetime_key(row.get("exit_at")), row["plan_id"]))

        groups = self._build_groups(normalized)
        try:
            calibration = await self._review_calibration(
                db,
                str(user_id),
                normalized,
                now=generated_at,
            )
        except DecisionReviewError:
            raise
        except Exception as exc:
            raise DecisionReviewError("calibration review could not be completed") from exc
        return {
            "metric_basis": self.METRIC_BASIS,
            "generated_at": generated_at.isoformat(),
            "overall": self._metrics(normalized),
            "groups": groups,
            "excluded_legacy_count": 0,
            "calibration": calibration,
        }

    def _normalize_row(self, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        status = str(raw.get("status") or "")
        if status not in self.CLOSED_STATUSES:
            return None
        exit_at = _parse_datetime(raw.get("exit_at"))
        net_return = _finite(raw.get("net_return_pct"))
        if exit_at is None or net_return is None:
            return None
        alpha = _finite(raw.get("net_alpha_pct"), raw.get("alpha_pct"))
        features_raw = raw.get("calibration_features")
        features = features_raw if isinstance(features_raw, Mapping) else {}
        return {
            "sample_id": str(raw.get("_id") or raw.get("outcome_id") or ""),
            "plan_id": str(raw.get("plan_id") or ""),
            "decision_id": str(raw.get("decision_id") or raw.get("origin_decision_id") or ""),
            "status": status,
            "exit_at": exit_at,
            "net_return_pct": net_return,
            "net_alpha_pct": alpha,
            "action_bucket": str(raw.get("action_bucket") or "unknown"),
            "horizon": str(raw.get("horizon") or "unknown"),
            "objective_segment": str(raw.get("objective_segment") or "unknown"),
            "industry": str(raw.get("industry") or "unknown"),
            "domestic_regime": str(raw.get("domestic_regime") or "unknown"),
            "macro_regime": str(raw.get("macro_regime") or "unknown"),
            "market_phase": str(raw.get("market_phase") or "unknown"),
            "entry_strategy": str(raw.get("entry_strategy") or "unknown"),
            "reason_codes": sorted(
                {str(value) for value in raw.get("reason_codes", []) if str(value)}
            ),
            "features": {
                key: _finite(features.get(key)) for key in FEATURE_ALLOWLIST
            },
        }

    def _build_groups(self, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        dimensions = (
            "action_bucket",
            "horizon",
            "objective_segment",
            "industry",
            "domestic_regime",
            "macro_regime",
            "market_phase",
            "entry_strategy",
        )
        grouped: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            for dimension in dimensions:
                grouped[(dimension, str(row.get(dimension) or "unknown"))].append(row)
            for reason_code in row.get("reason_codes") or ["unknown"]:
                grouped[("reason_code", str(reason_code))].append(row)
        return [
            {
                "dimension": dimension,
                "value": value,
                **self._metrics(group_rows),
                "calibration_eligible": len(group_rows) >= self.MINIMUM_SUBGROUP_SAMPLES,
            }
            for (dimension, value), group_rows in sorted(grouped.items())
        ]

    @staticmethod
    def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        returns = [float(row["net_return_pct"]) for row in rows]
        alphas = [
            float(row["net_alpha_pct"])
            for row in rows
            if row.get("net_alpha_pct") is not None
        ]
        return {
            "closed_count": len(rows),
            "win_rate_pct": _rounded(
                100.0 * sum(value > 0.0 for value in returns) / len(returns)
            )
            if returns
            else None,
            "average_net_return_pct": _rounded(mean(returns)) if returns else None,
            "median_net_return_pct": _rounded(median(returns)) if returns else None,
            "average_net_alpha_pct": _rounded(mean(alphas)) if alphas else None,
            "aligned_alpha_count": len(alphas),
            "stop_rate_pct": _rounded(
                100.0
                * sum(row.get("status") == "closed_stop" for row in rows)
                / len(rows)
            )
            if rows
            else None,
            "maximum_drawdown_pct": _rounded(_maximum_drawdown(returns))
            if returns
            else None,
        }

    async def _review_calibration(
        self,
        db: Any,
        user_id: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        now: datetime,
    ) -> Dict[str, Any]:
        cutoff = now - timedelta(days=self.TRAINING_WINDOW_DAYS)
        eligible = [
            row
            for row in rows
            if row.get("net_alpha_pct") is not None
            and cutoff <= _as_utc(row["exit_at"]) <= now
            and all(row["features"].get(key) is not None for key in FEATURE_ALLOWLIST)
        ]
        eligible.sort(key=lambda row: (_datetime_key(row["exit_at"]), row["plan_id"]))
        eligible = eligible[-self.MAXIMUM_SAMPLES :]
        common = {
            "eligible_sample_count": len(eligible),
            "required": self.MINIMUM_OVERALL_SAMPLES,
            "minimum_subgroup_samples": self.MINIMUM_SUBGROUP_SAMPLES,
            "training_window_days": self.TRAINING_WINDOW_DAYS,
            "maximum_samples": self.MAXIMUM_SAMPLES,
            "feature_allowlist": list(FEATURE_ALLOWLIST),
            "hard_gates_calibrated": False,
        }
        if len(eligible) < self.MINIMUM_OVERALL_SAMPLES:
            return {"status": "insufficient_overall_samples", **common}

        industry_counts: Dict[str, int] = defaultdict(int)
        for row in eligible:
            industry_counts[str(row.get("industry") or "unknown")] += 1
        subgroups = {
            "industry": {
                value: {
                    "sample_count": count,
                    "eligible": count >= self.MINIMUM_SUBGROUP_SAMPLES,
                    "reason": None
                    if count >= self.MINIMUM_SUBGROUP_SAMPLES
                    else "insufficient_subgroup_samples",
                }
                for value, count in sorted(industry_counts.items())
            }
        }
        common["subgroups"] = subgroups

        features = np.asarray(
            [[row["features"][key] for key in FEATURE_ALLOWLIST] for row in eligible],
            dtype=float,
        )
        target = np.asarray([row["net_alpha_pct"] for row in eligible], dtype=float)
        folds = deterministic_expanding_folds(len(eligible))
        full_deltas = ridge_weight_deltas(features, target, alpha=1.0)
        proposed_weights = bounded_simplex_weights(
            self._baseline_weights, full_deltas
        )
        evaluation = self._evaluate_proposal(
            eligible,
            features,
            target,
            folds,
        )
        result = {
            **common,
            "status": "proposal_rejected_by_guardrails",
            "ridge_alpha": 1.0,
            "fold_assignments": [
                {"train": list(train), "validation": list(validation)}
                for train, validation in folds
            ],
            "baseline_weights": deepcopy(self._baseline_weights),
            "relative_deltas": full_deltas,
            "proposed_weights": proposed_weights,
            "evaluation": evaluation,
        }
        if not evaluation["passes"]:
            return result

        proposal = self._build_proposal(user_id, eligible, result, now=now)
        try:
            await db["decision_calibration_versions"].insert_one(proposal)
        except Exception as exc:
            # A unique proposal id makes a repeated review idempotent.
            existing = await _find_one(
                db["decision_calibration_versions"],
                {"user_id": user_id, "proposal_id": proposal["proposal_id"]},
            )
            if existing is None:
                raise RuntimeError("failed to persist calibration proposal") from exc
        return {
            **result,
            "status": "inactive_proposal",
            "proposal_id": proposal["proposal_id"],
            "proposed_version": proposal["proposed_version"],
            "rollback_target": proposal["rollback_target"],
            "active": False,
        }

    def _evaluate_proposal(
        self,
        rows: Sequence[Mapping[str, Any]],
        features: np.ndarray,
        target: np.ndarray,
        folds: Sequence[Tuple[Tuple[int, ...], Tuple[int, ...]]],
    ) -> Dict[str, Any]:
        baseline_vector = np.asarray(
            [self._baseline_weights[key] for key in FEATURE_ALLOWLIST], dtype=float
        )
        baseline_scores_by_index: Dict[int, float] = {}
        proposed_scores_by_index: Dict[int, float] = {}
        for train, validation in folds:
            train_indices = list(train)
            validation_indices = list(validation)
            fold_deltas = ridge_weight_deltas(
                features[train_indices], target[train_indices], alpha=1.0
            )
            fold_weights = bounded_simplex_weights(
                self._baseline_weights, fold_deltas
            )
            fold_vector = np.asarray(
                [fold_weights[key] for key in FEATURE_ALLOWLIST], dtype=float
            )
            for index in validation_indices:
                baseline_scores_by_index[index] = float(
                    features[index] @ baseline_vector
                )
                proposed_scores_by_index[index] = float(features[index] @ fold_vector)
        validation_indices = sorted(proposed_scores_by_index)
        cohorts: Dict[str, List[int]] = defaultdict(list)
        for index in validation_indices:
            cohorts[str(rows[index].get("decision_id") or "")].append(index)

        baseline_correlations: List[float] = []
        proposed_correlations: List[float] = []
        baseline_selected: List[int] = []
        proposed_selected: List[int] = []
        for indices in cohorts.values():
            if len(indices) < 2:
                continue
            y = target[indices]
            baseline_scores = np.asarray(
                [baseline_scores_by_index[index] for index in indices], dtype=float
            )
            proposed_scores = np.asarray(
                [proposed_scores_by_index[index] for index in indices], dtype=float
            )
            baseline_correlations.append(_spearman(baseline_scores, y))
            proposed_correlations.append(_spearman(proposed_scores, y))
            top_count = max(1, math.ceil(len(indices) / 2))
            baseline_selected.extend(
                indices[position]
                for position in np.argsort(-baseline_scores, kind="stable")[:top_count]
            )
            proposed_selected.extend(
                indices[position]
                for position in np.argsort(-proposed_scores, kind="stable")[:top_count]
            )

        baseline_median = median(baseline_correlations) if baseline_correlations else 0.0
        proposed_median = median(proposed_correlations) if proposed_correlations else 0.0
        baseline_top_returns = [float(target[index]) for index in baseline_selected]
        proposed_top_returns = [float(target[index]) for index in proposed_selected]
        baseline_top = mean(baseline_top_returns) if baseline_top_returns else 0.0
        proposed_top = mean(proposed_top_returns) if proposed_top_returns else 0.0
        correlation_gain = proposed_median - baseline_median
        top_half_gain = proposed_top - baseline_top
        baseline_selected = sorted(set(baseline_selected))
        proposed_selected = sorted(set(proposed_selected))
        baseline_drawdown = _maximum_drawdown(
            float(rows[index]["net_return_pct"]) for index in baseline_selected
        )
        proposed_drawdown = _maximum_drawdown(
            float(rows[index]["net_return_pct"]) for index in proposed_selected
        )
        baseline_stop_rate = _stop_rate(rows, baseline_selected)
        proposed_stop_rate = _stop_rate(rows, proposed_selected)
        drawdown_worsening = proposed_drawdown - baseline_drawdown
        stop_rate_worsening = proposed_stop_rate - baseline_stop_rate
        passes = bool(
            len(baseline_correlations) > 0
            and correlation_gain >= 0.05
            and top_half_gain >= 0.25
            and drawdown_worsening <= 2.0
            and stop_rate_worsening <= 5.0
        )
        return {
            "cohort_count": len(baseline_correlations),
            "baseline_median_spearman": _rounded(baseline_median),
            "proposed_median_spearman": _rounded(proposed_median),
            "spearman_improvement": _rounded(correlation_gain),
            "baseline_top_half_mean_alpha_pct": _rounded(baseline_top),
            "proposed_top_half_mean_alpha_pct": _rounded(proposed_top),
            "top_half_mean_alpha_improvement_pct": _rounded(top_half_gain),
            "baseline_maximum_drawdown_pct": _rounded(baseline_drawdown),
            "proposed_maximum_drawdown_pct": _rounded(proposed_drawdown),
            "maximum_drawdown_worsening_pct": _rounded(drawdown_worsening),
            "baseline_stop_rate_pct": _rounded(baseline_stop_rate),
            "proposed_stop_rate_pct": _rounded(proposed_stop_rate),
            "stop_rate_worsening_pct": _rounded(stop_rate_worsening),
            "passes": passes,
        }

    def _build_proposal(
        self,
        user_id: str,
        rows: Sequence[Mapping[str, Any]],
        result: Mapping[str, Any],
        *,
        now: datetime,
    ) -> Dict[str, Any]:
        identity = {
            "user_id": user_id,
            "sample_ids": [row["sample_id"] for row in rows],
            "baseline_weights": result["baseline_weights"],
            "proposed_weights": result["proposed_weights"],
            "calibration_version": self.CALIBRATION_VERSION,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        return {
            "proposal_id": f"calibration-{digest}",
            "user_id": user_id,
            "status": "inactive",
            "active": False,
            "baseline_version": "decision-ranking-v1",
            "proposed_version": f"decision-ranking-proposal-{digest}",
            "rollback_target": "decision-ranking-v1",
            "calibration_version": self.CALIBRATION_VERSION,
            "training_window": {
                "from": min(row["exit_at"] for row in rows),
                "to": max(row["exit_at"] for row in rows),
                "sample_count": len(rows),
            },
            "sample_ids": [row["sample_id"] for row in rows],
            "fold_assignments": deepcopy(result["fold_assignments"]),
            "baseline_weights": deepcopy(result["baseline_weights"]),
            "proposed_weights": deepcopy(result["proposed_weights"]),
            "relative_deltas": deepcopy(result["relative_deltas"]),
            "evaluation": deepcopy(result["evaluation"]),
            "created_at": now,
        }


def _finite(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_key(value: Any) -> float:
    parsed = _parse_datetime(value)
    return parsed.timestamp() if parsed is not None else float("-inf")


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _maximum_drawdown(returns_pct: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in returns_pct:
        equity *= 1.0 + float(value) / 100.0
        peak = max(peak, equity)
        if peak > 0.0:
            maximum = max(maximum, (peak - equity) / peak * 100.0)
    return maximum


def _stop_rate(rows: Sequence[Mapping[str, Any]], indices: Sequence[int]) -> float:
    if not indices:
        return 0.0
    return (
        sum(rows[index].get("status") == "closed_stop" for index in indices)
        / len(indices)
        * 100.0
    )


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=float)
    index = 0
    while index < len(values):
        end = index + 1
        while end < len(values) and values[order[end]] == values[order[index]]:
            end += 1
        average_rank = (index + end - 1) / 2.0
        ranks[order[index:end]] = average_rank
        index = end
    return ranks


def _spearman(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 2:
        return 0.0
    left_rank = _rank(np.asarray(left, dtype=float))
    right_rank = _rank(np.asarray(right, dtype=float))
    if float(left_rank.std()) == 0.0 or float(right_rank.std()) == 0.0:
        return 0.0
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


async def _find_one(collection: Any, query: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    method = getattr(collection, "find_one", None)
    if method is None:
        return None
    result = method(dict(query))
    return await result if inspect.isawaitable(result) else result


decision_review_service = DecisionReviewService()
