from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.services.decision_review_service import (
    FEATURE_ALLOWLIST,
    DecisionReviewService,
    bounded_simplex_weights,
    deterministic_expanding_folds,
    ridge_weight_deltas,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, rows):
        self.rows = [deepcopy(row) for row in rows]

    def sort(self, spec):
        for field, direction in reversed(spec):
            self.rows.sort(key=lambda row: row.get(field), reverse=direction < 0)
        return self

    def limit(self, value):
        self.rows = self.rows[:value]
        return self

    async def to_list(self, length):
        return self.rows[:length]


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(deepcopy(query))
        rows = [row for row in self.rows if _matches(row, query)]
        return FakeCursor(rows)

    async def insert_one(self, document):
        self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("proposal_id"))


class FakeDB(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = FakeCollection()
        return super().__getitem__(key)


def _matches(row, query):
    for key, expected in query.items():
        value = row.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and value not in expected["$in"]:
                return False
            if "$gte" in expected and value < expected["$gte"]:
                return False
            continue
        if value != expected:
            return False
    return True


def _row(index: int, **updates):
    value = ((index % 7) - 3) / 10
    row = {
        "_id": f"outcome-{index:03d}",
        "user_id": "owner-1",
        "plan_id": f"plan-{index:03d}",
        "decision_id": f"decision-{index // 3:03d}",
        "metric_basis": "shadow_trade_v1",
        "status": "closed_target" if index % 3 else "closed_stop",
        "exit_at": NOW - timedelta(days=index),
        "net_return_pct": value + 0.8,
        "net_alpha_pct": value + 0.5,
        "action_bucket": "condition_order",
        "horizon": "swing",
        "objective_segment": "数字科技",
        "industry": "计算机设备",
        "domestic_regime": "green",
        "macro_regime": "neutral",
        "market_phase": "live_am",
        "entry_strategy": "pullback",
        "reason_codes": ["price_condition_met"],
        "calibration_features": {
            "objective_match": (index % 5) / 4,
            "reward_risk": ((index * 2) % 7) / 6,
            "evidence_completeness": ((index * 3) % 9) / 8,
            "actionability": ((index * 5) % 11) / 10,
            "capital_limit": 999,
            "freshness": 999,
        },
    }
    row.update(updates)
    return row


@pytest.mark.asyncio
async def test_performance_queries_only_closed_shadow_trade_v1_rows():
    rows = [_row(index) for index in range(6)]
    rows.extend(
        [
            _row(90, metric_basis="legacy_generated_baseline"),
            _row(91, status="waiting_entry"),
            _row(92, user_id="other-owner"),
        ]
    )
    db = FakeDB(decision_outcomes=FakeCollection(rows))
    service = DecisionReviewService(db_provider=lambda: db)

    result = await service.performance("owner-1", now=NOW)

    assert result["metric_basis"] == "shadow_trade_v1"
    assert result["overall"]["closed_count"] == 6
    assert result["excluded_legacy_count"] == 0
    assert db["decision_outcomes"].queries == [
        {
            "user_id": "owner-1",
            "metric_basis": "shadow_trade_v1",
            "status": {"$in": service.CLOSED_STATUSES},
        }
    ]


@pytest.mark.asyncio
async def test_calibration_requires_30_overall_and_10_in_each_subgroup():
    db = FakeDB(decision_outcomes=FakeCollection([_row(index) for index in range(29)]))
    result = await DecisionReviewService(db_provider=lambda: db).performance(
        "owner-1", now=NOW
    )
    assert result["calibration"]["status"] == "insufficient_overall_samples"
    assert result["calibration"]["required"] == 30

    rows = [
        _row(index, industry="计算机设备" if index < 9 else "电力设备")
        for index in range(30)
    ]
    db = FakeDB(decision_outcomes=FakeCollection(rows))
    result = await DecisionReviewService(db_provider=lambda: db).performance(
        "owner-1", now=NOW
    )
    assert result["calibration"]["minimum_subgroup_samples"] == 10
    assert result["calibration"]["subgroups"]["industry"]["计算机设备"] == {
        "sample_count": 9,
        "eligible": False,
        "reason": "insufficient_subgroup_samples",
    }
    assert result["calibration"]["subgroups"]["industry"]["电力设备"] == {
        "sample_count": 21,
        "eligible": True,
        "reason": None,
    }


def test_feature_allowlist_and_ridge_deltas_ignore_hard_gate_fields():
    assert FEATURE_ALLOWLIST == (
        "objective_match",
        "reward_risk",
        "evidence_completeness",
        "actionability",
    )
    features = np.asarray(
        [
            [0.0, 1.0, 0.0, 1.0],
            [1.0, 0.0, 1.0, 0.0],
            [2.0, 1.0, 0.0, 1.0],
            [3.0, 0.0, 1.0, 0.0],
        ],
        dtype=float,
    )
    target = np.asarray([0.0, 1.0, 2.0, 3.0], dtype=float)

    deltas = ridge_weight_deltas(features, target, alpha=1.0)

    assert set(deltas) == set(FEATURE_ALLOWLIST)
    assert max(abs(value) for value in deltas.values()) == pytest.approx(0.1)


def test_deterministic_folds_and_bounded_simplex_are_exact_and_stable():
    folds = deterministic_expanding_folds(20)
    assert folds == [
        (tuple(range(10)), (10, 11)),
        (tuple(range(12)), (12, 13)),
        (tuple(range(14)), (14, 15)),
        (tuple(range(16)), (16, 17)),
        (tuple(range(18)), (18, 19)),
    ]

    baseline = {
        "objective_match": 4.0,
        "reward_risk": 3.0,
        "evidence_completeness": 2.0,
        "actionability": 1.0,
    }
    proposed = bounded_simplex_weights(
        baseline,
        {
            "objective_match": 0.1,
            "reward_risk": -0.1,
            "evidence_completeness": 0.1,
            "actionability": -0.1,
        },
    )

    assert proposed == {
        "actionability": pytest.approx(0.9),
        "evidence_completeness": pytest.approx(2.1),
        "objective_match": pytest.approx(4.3),
        "reward_risk": pytest.approx(2.7),
    }
    assert sum(proposed.values()) == pytest.approx(sum(baseline.values()))
    for key, value in proposed.items():
        assert baseline[key] * 0.9 <= value <= baseline[key] * 1.1


@pytest.mark.asyncio
async def test_training_window_uses_newest_500_aligned_alpha_rows_from_180_days():
    rows = [_row(index) for index in range(520)]
    rows.append(_row(700, exit_at=NOW - timedelta(days=181)))
    rows.append(_row(701, net_alpha_pct=None))
    db = FakeDB(decision_outcomes=FakeCollection(rows))
    service = DecisionReviewService(db_provider=lambda: db)

    result = await service.performance("owner-1", now=NOW)

    assert result["calibration"]["eligible_sample_count"] == 181
    assert result["calibration"]["training_window_days"] == 180
    assert result["calibration"]["maximum_samples"] == 500
    assert result["calibration"]["feature_allowlist"] == list(FEATURE_ALLOWLIST)
