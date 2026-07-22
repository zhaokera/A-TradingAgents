from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.services.decision_tracking_service as tracking_module
from app.services.decision_tracking_service import (
    DEFAULT_CN_A_FEE_POLICY,
    DecisionTrackingService,
    MinuteBarAggregator,
    ObservationConflictError,
    TrackingPoller,
    calculate_trade_metrics,
)


CN = ZoneInfo("Asia/Shanghai")
T0930 = datetime(2026, 7, 22, 9, 30, tzinfo=CN)
T1000 = datetime(2026, 7, 22, 10, 0, tzinfo=CN)


def _get(row, path):
    current = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches(row, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(row, branch) for branch in expected):
                return False
            continue
        actual = _get(row, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            if "$exists" in expected and (actual is not None) != expected["$exists"]:
                return False
        elif actual != expected:
            return False
    return True


def _set(row, path, value):
    parts = path.split(".")
    target = row
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = deepcopy(value)


class Cursor:
    def __init__(self, rows):
        self.rows = [deepcopy(row) for row in rows]

    def sort(self, spec, direction=None):
        pairs = [(spec, direction)] if isinstance(spec, str) else list(spec)
        for key, order in reversed(pairs):
            self.rows.sort(key=lambda row: _get(row, key), reverse=order < 0)
        return self

    def limit(self, value):
        self.rows = self.rows[:value]
        return self

    async def to_list(self, length=None):
        return deepcopy(self.rows if length is None else self.rows[:length])

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self.rows):
            raise StopAsyncIteration
        value = deepcopy(self.rows[self._index])
        self._index += 1
        return value


class Collection:
    def __init__(self, name):
        self.name = name
        self.rows = []
        self.guard = asyncio.Lock()

    async def find_one(self, query, projection=None, sort=None):
        rows = [row for row in self.rows if _matches(row, query)]
        if sort:
            rows = Cursor(rows).sort(sort).rows
        return deepcopy(rows[0]) if rows else None

    def find(self, query, projection=None):
        return Cursor([row for row in self.rows if _matches(row, query)])

    async def insert_one(self, document):
        async with self.guard:
            if self.name == "decision_plans" and any(
                row["plan_id"] == document["plan_id"] for row in self.rows
            ):
                raise RuntimeError("duplicate plan")
            if self.name == "decision_outcomes" and any(
                (row["plan_id"], row["observation_sequence"])
                == (document["plan_id"], document["observation_sequence"])
                for row in self.rows
            ):
                raise RuntimeError("duplicate outcome")
            self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id", len(self.rows)))

    async def update_one(self, query, update, upsert=False):
        async with self.guard:
            row = next((row for row in self.rows if _matches(row, query)), None)
            if row is None and upsert:
                row = {key: deepcopy(value) for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
                self.rows.append(row)
            if row is None:
                return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
            for key, value in update.get("$set", {}).items():
                _set(row, key, value)
            for key, value in update.get("$setOnInsert", {}).items():
                if _get(row, key) is None:
                    _set(row, key, value)
            for key, value in update.get("$inc", {}).items():
                _set(row, key, (_get(row, key) or 0) + value)
            for key, value in update.get("$addToSet", {}).items():
                target = _get(row, key)
                if target is None:
                    target = []
                    _set(row, key, target)
                if value not in target:
                    target.append(deepcopy(value))
            for key in update.get("$unset", {}):
                row.pop(key, None)
            return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    async def find_one_and_update(self, query, update, upsert=False, return_document=None):
        async with self.guard:
            row = next((row for row in self.rows if _matches(row, query)), None)
            inserted = False
            if row is None and upsert:
                row = {key: deepcopy(value) for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
                self.rows.append(row)
                inserted = True
            if row is None:
                return None
            if inserted:
                for key, value in update.get("$setOnInsert", {}).items():
                    _set(row, key, value)
            for key, value in update.get("$set", {}).items():
                _set(row, key, value)
            for key, value in update.get("$inc", {}).items():
                _set(row, key, (_get(row, key) or 0) + value)
            for key, value in update.get("$addToSet", {}).items():
                target = _get(row, key)
                if target is None:
                    target = []
                    _set(row, key, target)
                if value not in target:
                    target.append(deepcopy(value))
            return deepcopy(row)


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection(name))


def _item(plan_id="plan-a", *, code="000977", strategy="pullback", entry=63, stop=61.5, target=67, quantity=100):
    return {
        "plan_id": plan_id,
        "identity": {"code": code, "objective_segment": "数字科技"},
        "plans": {"short": {"entry_strategy": strategy, "entry_price": entry, "stop_price": stop, "target_price": target}},
        "allocation": {"status": "allocated", "quantity": quantity, "amount": entry * quantity},
        "invalidation": {"stop_price": stop, "plan_expires_at": "2026-07-24T15:00:00+08:00"},
        "versions": {"rule_version": "decision-v1", "fee_policy_version": "cn_a_v1"},
    }


def _decision(decision_id="decision-1", revision=1, *, bucket="condition_order", phase="pre_open", at=T0930, item=None):
    packet = {
        "decision_id": decision_id,
        "user_id": "user-1",
        "candidate_run_id": "run-1",
        "revision": revision,
        "as_of": at.isoformat(),
        "market_phase": phase,
        "buy_now": [],
        "condition_order": [],
        "wait": [],
        "avoid": [],
    }
    packet[bucket] = [deepcopy(item or _item())]
    return packet


def _bar(start, *, open, high, low, close, closed=True):
    return {
        "kind": "minute_bar",
        "source": "tencent_own_ticks",
        "interval_start": start.isoformat(),
        "interval_end": (start + timedelta(minutes=1)).isoformat(),
        "is_closed": closed,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
    }


def _tick(at, price):
    return {"kind": "last_trade", "source": "tencent", "trade_at": at.isoformat(), "price": price}


@pytest.mark.asyncio
async def test_registration_reuses_plan_and_uses_latest_preceding_trigger_context():
    db = Database()
    service = DecisionTrackingService(db=db)

    first = await service.register_decision(_decision())
    await service.register_decision(_decision("decision-2", 2, bucket="buy_now", phase="live_am", at=T1000))
    assert await service.observe("plan-a", _tick(T1000 + timedelta(seconds=1), 64)) is None
    outcome = await service.observe("plan-a", _tick(T1000 + timedelta(seconds=2), 62.8))

    assert first[0]["origin_decision_id"] == "decision-1"
    assert first[0]["eligibility_at"] == T0930
    assert len(db["decision_plans"].rows) == 1
    plan = db["decision_plans"].rows[0]
    assert [ref["decision_id"] for ref in plan["decision_refs"]] == ["decision-1", "decision-2"]
    assert outcome["trigger_context_decision_id"] == "decision-2"
    assert outcome["trigger_context_action_bucket"] == "buy_now"
    assert outcome["trigger_context_market_phase"] == "live_am"


@pytest.mark.asyncio
async def test_service_accepts_sync_motor_database_factory(monkeypatch):
    db = Database()
    monkeypatch.setattr(tracking_module, "get_mongo_db", lambda: db)
    registered = await DecisionTrackingService().register_decision(_decision())
    assert registered[0]["plan_id"] == "plan-a"


@pytest.mark.asyncio
async def test_untrackable_wait_item_does_not_abort_an_actionable_plan():
    packet = _decision()
    packet["wait"] = [_item("plan-wait", code="300750", quantity=0)]
    registered = await DecisionTrackingService(db=Database()).register_decision(packet)
    assert [plan["plan_id"] for plan in registered] == ["plan-a"]


@pytest.mark.asyncio
async def test_concurrent_registration_is_idempotent():
    db = Database()
    service = DecisionTrackingService(db=db)
    await asyncio.gather(
        service.register_decision(_decision()),
        service.register_decision(_decision()),
    )
    assert len(db["decision_plans"].rows) == 1
    assert len(db["decision_plans"].rows[0]["decision_refs"]) == 1


@pytest.mark.asyncio
async def test_changed_plan_supersedes_waiting_but_preserves_active_plan():
    db = Database()
    service = DecisionTrackingService(db=db)
    await service.register_decision(_decision(item=_item("plan-old")))
    await service.register_decision(_decision("decision-2", 2, at=T1000, item=_item("plan-new", entry=62)))
    assert (await service.latest_outcome("plan-old"))["state"] == "superseded_untriggered"

    db2 = Database()
    service2 = DecisionTrackingService(db=db2)
    await service2.register_decision(_decision(item=_item("plan-active")))
    await service2.observe("plan-active", _bar(T0930, open=64, high=64, low=62.8, close=63))
    await service2.register_decision(_decision("decision-2", 2, at=T1000, item=_item("plan-new", entry=62)))
    assert (await service2.latest_outcome("plan-active"))["state"] == "active"


@pytest.mark.asyncio
async def test_cas_sequences_are_unique_append_only_and_transitions_are_exact():
    db = Database()
    service = DecisionTrackingService(db=db)
    await service.register_decision(_decision())

    entered = await service.transition("plan-a", expected_sequence=0, new_state="active", observed_at=T1000, details={"entry_execution_price": 63})
    with pytest.raises(ObservationConflictError):
        await service.transition("plan-a", expected_sequence=0, new_state="active", observed_at=T1000, details={})
    closed = await service.transition("plan-a", expected_sequence=1, new_state="closed_stop", observed_at=T1000 + timedelta(minutes=1), details={"exit_execution_price": 61.5})
    with pytest.raises(ValueError, match="invalid_state_transition"):
        await service.transition("plan-a", expected_sequence=2, new_state="active", observed_at=T1000, details={})

    assert [entered["observation_sequence"], closed["observation_sequence"]] == [1, 2]
    assert db["decision_outcomes"].rows == [entered, closed]


@pytest.mark.asyncio
async def test_transition_identity_and_metric_basis_cannot_be_overridden_by_details():
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    outcome = await service.transition(
        "plan-a",
        expected_sequence=0,
        new_state="active",
        observed_at=T1000,
        details={
            "plan_id": "wrong",
            "observation_sequence": 99,
            "state": "closed_target",
            "metric_basis": "legacy_generated_baseline",
        },
    )
    assert (outcome["plan_id"], outcome["observation_sequence"], outcome["state"]) == ("plan-a", 1, "active")
    assert outcome["metric_basis"] == "shadow_trade_v1"


@pytest.mark.asyncio
async def test_last_trade_must_be_tencent_and_strictly_after_eligibility():
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision(at=T1000))
    assert await service.observe("plan-a", _tick(T1000, 62.5)) is None
    assert await service.observe("plan-a", {**_tick(T1000 + timedelta(seconds=1), 62.5), "source": "sina"}) is None
    assert await service.observe("plan-a", _tick(T1000 + timedelta(seconds=1), 64)) is None
    outcome = await service.observe("plan-a", _tick(T1000 + timedelta(seconds=2), 62.5))
    assert outcome["state"] == "active"
    assert outcome["entry_raw_price"] == 63


@pytest.mark.asyncio
async def test_preopen_and_live_bar_eligibility_excludes_partial_decision_minute():
    pre = DecisionTrackingService(db=Database())
    await pre.register_decision(_decision(at=datetime(2026, 7, 22, 9, 20, tzinfo=CN)))
    assert await pre.observe("plan-a", _bar(T0930 - timedelta(minutes=1), open=64, high=64, low=62, close=63)) is None
    assert (await pre.observe("plan-a", _bar(T0930, open=64, high=64, low=62, close=63)))["state"] == "active"

    live = DecisionTrackingService(db=Database())
    created = T1000 + timedelta(seconds=15)
    await live.register_decision(_decision(phase="live_am", at=created))
    assert await live.observe("plan-a", _bar(T1000, open=64, high=64, low=62, close=63)) is None
    assert (await live.observe("plan-a", _bar(T1000 + timedelta(minutes=1), open=64, high=64, low=62, close=63)))["state"] == "active"

    next_day = DecisionTrackingService(db=Database())
    await next_day.register_decision(_decision(at=datetime(2026, 7, 22, 9, 20, tzinfo=CN)))
    following_open = datetime(2026, 7, 23, 9, 30, tzinfo=CN)
    assert (await next_day.observe("plan-a", _bar(following_open, open=64, high=64, low=62, close=63)))["state"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item", "bar", "entry_raw"),
    [
        (_item(strategy="pullback"), _bar(T0930, open=62.5, high=64, low=62, close=63), 62.5),
        (_item(strategy="pullback"), _bar(T0930, open=64, high=64, low=62, close=63), 63.0),
        (_item(strategy="breakout"), _bar(T0930, open=64, high=65, low=63.5, close=64.5), 64.0),
        (_item(strategy="breakout"), _bar(T0930, open=62, high=63.5, low=62, close=63.2), 63.0),
    ],
)
async def test_pullback_and_breakout_fill_rules(item, bar, entry_raw):
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision(item=item))
    outcome = await service.observe("plan-a", bar)
    assert outcome["state"] == "active"
    assert outcome["entry_raw_price"] == entry_raw
    assert outcome["entry_execution_price"] == pytest.approx(entry_raw * 1.0005)


@pytest.mark.asyncio
async def test_unfilled_stop_gap_invalidates_and_same_bar_stop_wins():
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    invalid = await service.observe("plan-a", _bar(T0930, open=61.5, high=63, low=61, close=62))
    assert invalid["state"] == "invalidated_stop_gap"

    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    closed = await service.observe("plan-a", _bar(T0930, open=64, high=68, low=61, close=65))
    assert closed["state"] == "closed_stop"
    assert closed["observation_sequence"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bar", "state", "raw_exit"),
    [
        (_bar(T0930 + timedelta(minutes=1), open=60, high=62, low=59, close=61), "closed_stop", 60),
        (_bar(T0930 + timedelta(minutes=1), open=68, high=69, low=67.5, close=68), "closed_target", 67),
        (_bar(T0930 + timedelta(minutes=1), open=64, high=68, low=61, close=65), "closed_stop", 61.5),
        (_bar(T0930 + timedelta(minutes=1), open=64, high=68, low=63, close=67), "closed_target", 67),
    ],
)
async def test_active_gap_cross_and_double_trigger_exit_rules(bar, state, raw_exit):
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    await service.observe("plan-a", _bar(T0930, open=63, high=63, low=62.5, close=63))
    outcome = await service.observe("plan-a", bar)
    assert outcome["state"] == state
    assert outcome["exit_raw_price"] == raw_exit


def test_exact_cn_a_v1_fees_slippage_and_benchmark_alpha():
    metrics = calculate_trade_metrics(
        entry_raw_price=10,
        exit_raw_price=11,
        quantity=100,
        entry_at=T1000,
        exit_at=T1000 + timedelta(minutes=30),
        fee_policy={**DEFAULT_CN_A_FEE_POLICY},
        mae_price=9.5,
        mfe_price=11.2,
        benchmark_observations=[
            {"at": (T1000 + timedelta(minutes=2)).isoformat(), "price": 4000, "interval": "intraday"},
            {"at": (T1000 + timedelta(minutes=33)).isoformat(), "price": 4040, "interval": "intraday"},
        ],
    )
    assert metrics["entry_execution_price"] == pytest.approx(10.005)
    assert metrics["exit_execution_price"] == pytest.approx(10.9945)
    assert metrics["entry_commission"] == 5
    assert metrics["exit_commission"] == 5
    assert metrics["seller_stamp_duty"] == pytest.approx(0.549725)
    assert metrics["net_pnl"] == pytest.approx(88.400275)
    assert metrics["mae_pct"] == pytest.approx((9.5 / 10.005 - 1) * 100)
    assert metrics["mfe_pct"] == pytest.approx((11.2 / 10.005 - 1) * 100)
    assert metrics["alpha_pct"] == pytest.approx(metrics["net_return_pct"] - 1.0)
    assert metrics["metric_basis"] == "shadow_trade_v1"
    assert metrics["fee_policy"]["version"] == "cn_a_v1"


def test_commission_above_minimum_and_missing_or_misaligned_benchmark_alpha():
    metrics = calculate_trade_metrics(
        entry_raw_price=100,
        exit_raw_price=101,
        quantity=10000,
        entry_at=T1000,
        exit_at=T1000 + timedelta(minutes=30),
        benchmark_observations=[
            {"at": (T1000 + timedelta(minutes=6)).isoformat(), "price": 4000, "interval": "intraday"},
        ],
    )
    assert metrics["entry_commission"] == pytest.approx(300.15)
    assert metrics["alpha_pct"] is None


@pytest.mark.asyncio
async def test_configured_fee_policy_is_snapshotted_on_every_transition():
    db = Database()
    service = DecisionTrackingService(
        db=db,
        fee_policy={"version": "cn_a_v1", "commission_rate": 0.001},
    )
    await service.register_decision(_decision())
    outcome = await service.observe(
        "plan-a", _bar(T0930, open=63, high=63, low=62.5, close=63)
    )
    assert outcome["fee_policy"]["commission_rate"] == 0.001
    assert db["decision_plans"].rows[0]["fee_policy"]["commission_rate"] == 0.001


@pytest.mark.asyncio
async def test_mae_mfe_accumulate_until_exit_and_quantity_comes_from_allocation():
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision(item=_item(quantity=200)))
    await service.observe("plan-a", _bar(T0930, open=63, high=63, low=62.5, close=63))
    await service.observe("plan-a", _bar(T0930 + timedelta(minutes=1), open=63, high=66, low=62, close=65))
    closed = await service.observe("plan-a", _bar(T0930 + timedelta(minutes=2), open=65, high=68, low=64, close=67))
    assert closed["quantity"] == 200
    assert closed["mae_price"] == 62
    assert closed["mfe_price"] == 68


@pytest.mark.asyncio
async def test_expiry_and_pre_post_entry_corporate_actions():
    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    expired = await service.expire_due(datetime(2026, 7, 24, 15, 0, tzinfo=CN))
    assert expired[0]["state"] == "expired_untriggered"

    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    before = await service.apply_corporate_action("plan-a", {"effective_at": T1000.isoformat(), "type": "split"})
    assert before["state"] == "invalidated_corporate_action"

    service = DecisionTrackingService(db=Database())
    await service.register_decision(_decision())
    await service.observe("plan-a", _bar(T0930, open=63, high=63, low=62.5, close=63))
    after = await service.apply_corporate_action("plan-a", {"effective_at": T1000.isoformat(), "type": "dividend"})
    assert after["state"] == "closed_invalidated_corporate_action"


@pytest.mark.asyncio
async def test_recovery_repairs_reserved_transition_without_duplicate_outcome():
    db = Database()
    service = DecisionTrackingService(db=db)
    await service.register_decision(_decision())
    plan = db["decision_plans"].rows[0]
    plan["latest_state"] = "active"
    plan["observation_sequence"] = 1
    plan["pending_outcome"] = {
        "plan_id": "plan-a",
        "observation_sequence": 1,
        "prior_state": "waiting_entry",
        "state": "active",
        "observed_at": T1000,
        "metric_basis": "shadow_trade_v1",
    }

    recovered = await service.recover_pending("plan-a")
    again = await service.recover_pending("plan-a")
    assert recovered["observation_sequence"] == 1
    assert again is None
    assert len(db["decision_outcomes"].rows) == 1


@pytest.mark.asyncio
async def test_bar_recovery_skips_unsafe_partial_minute_and_replays_in_order():
    db = Database()
    service = DecisionTrackingService(db=db)
    created = T1000 + timedelta(seconds=15)
    await service.register_decision(_decision(phase="live_am", at=created))
    db["decision_minute_bars"].rows.extend(
        [
            _bar(T1000 + timedelta(minutes=1), open=64, high=64, low=62, close=63),
            _bar(T1000, open=64, high=64, low=62, close=63),
        ]
    )
    for row in db["decision_minute_bars"].rows:
        row["code"] = "000977"

    outcomes = await service.recover_from_bars("plan-a")
    assert [outcome["state"] for outcome in outcomes] == ["active"]
    assert outcomes[0]["observed_at"] == T1000 + timedelta(minutes=2)


def test_minute_aggregator_emits_only_closed_own_tick_bars():
    aggregator = MinuteBarAggregator()
    assert aggregator.add("000977", _tick(T1000 + timedelta(seconds=5), 10)) == []
    assert aggregator.add("000977", _tick(T1000 + timedelta(seconds=20), 11)) == []
    bars = aggregator.add("000977", _tick(T1000 + timedelta(minutes=1, seconds=1), 10.5))
    assert bars == [{
        "kind": "minute_bar",
        "source": "tencent_own_ticks",
        "code": "000977",
        "interval_start": T1000,
        "interval_end": T1000 + timedelta(minutes=1),
        "is_closed": True,
        "open": 10.0,
        "high": 11.0,
        "low": 10.0,
        "close": 11.0,
        "tick_count": 2,
    }]


def test_minute_aggregator_can_close_due_bar_without_a_later_tick():
    aggregator = MinuteBarAggregator()
    aggregator.add("000977", _tick(T1000 + timedelta(seconds=5), 10))
    assert aggregator.close_due(T1000 + timedelta(seconds=59)) == []
    bars = aggregator.close_due(T1000 + timedelta(minutes=1))
    assert len(bars) == 1
    assert bars[0]["interval_start"] == T1000
    assert aggregator.close_due(T1000 + timedelta(minutes=2)) == []


class Session:
    def __init__(self, phase="live_am"):
        self.phase = phase

    async def classify(self, *, now=None):
        return {"phase": self.phase, "classified_at": (now or T1000).isoformat()}


class BrokenSession:
    async def classify(self, *, now=None):
        raise RuntimeError("calendar unavailable")


@pytest.mark.asyncio
async def test_bounded_poller_only_fetches_holdings_and_waiting_active_plans():
    db = Database()
    db["user_holdings"].rows.extend([
        {"user_id": "u", "code": "600519", "quantity": 100},
        {"user_id": "u", "code": "000001", "quantity": 0},
    ])
    db["decision_plans"].rows.extend([
        {"plan_id": "a", "user_id": "u", "code": "000977", "latest_state": "waiting_entry", "observation_sequence": 0},
        {"plan_id": "b", "user_id": "u", "code": "300750", "latest_state": "closed_stop", "observation_sequence": 2},
    ])
    calls = []

    async def fetch(codes):
        calls.append(list(codes))
        return {code: _tick(T1000 + timedelta(seconds=1), 10) for code in codes}

    poller = TrackingPoller(db=db, quote_fetcher=fetch, market_session=Session(), max_symbols=2)
    result = await poller.poll_once(now=T1000)
    assert result["status"] == "polled"
    assert calls == [["000977", "600519"]]

    limited = TrackingPoller(db=db, quote_fetcher=fetch, market_session=Session(), max_symbols=1)
    assert (await limited.poll_once(now=T1000))["status"] == "disabled_symbol_limit"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_poller_processes_closed_previous_minute_before_current_tick():
    db = Database()
    db["decision_plans"].rows.append(
        {"plan_id": "a", "user_id": "u", "code": "000977", "latest_state": "waiting_entry", "observation_sequence": 0}
    )
    seen = []

    class Tracker:
        async def observe(self, plan_id, observation):
            seen.append(observation["kind"])

    async def fetch(codes):
        return {"000977": _tick(T1000 + timedelta(minutes=1, seconds=1), 10.5)}

    aggregator = MinuteBarAggregator()
    aggregator.add("000977", _tick(T1000 + timedelta(seconds=1), 10))
    poller = TrackingPoller(
        db=db,
        quote_fetcher=fetch,
        market_session=Session(),
        max_symbols=2,
        tracking_service=Tracker(),
        aggregator=aggregator,
    )
    await poller.poll_once(now=T1000 + timedelta(minutes=1, seconds=1))
    assert seen == ["minute_bar", "last_trade"]


@pytest.mark.asyncio
async def test_poller_self_disables_outside_live_and_lock_contention_fails_closed():
    db = Database()
    db["user_holdings"].rows.append({"user_id": "u", "code": "600519", "quantity": 100})
    calls = []

    async def fetch(codes):
        calls.append(codes)
        return {}

    closed = TrackingPoller(db=db, quote_fetcher=fetch, market_session=Session("pre_open"), max_symbols=2)
    assert (await closed.poll_once(now=T1000))["status"] == "disabled_market_phase"

    db["job_locks"].rows.append({"_id": "decision-tracking-poller", "owner": "other", "lease_until": T1000 + timedelta(minutes=1), "fence": 2})
    locked = TrackingPoller(db=db, quote_fetcher=fetch, market_session=Session(), max_symbols=2)
    assert (await locked.poll_once(now=T1000))["status"] == "lock_unavailable"
    assert calls == []

    unavailable = TrackingPoller(db=db, quote_fetcher=fetch, market_session=BrokenSession(), max_symbols=2)
    assert (await unavailable.poll_once(now=T1000))["status"] == "disabled_session_unavailable"


@pytest.mark.asyncio
async def test_poller_fails_closed_on_quote_error_or_lost_fence():
    db = Database()
    db["user_holdings"].rows.append(
        {"user_id": "u", "code": "600519", "quantity": 100}
    )

    async def broken_fetch(codes):
        raise RuntimeError("provider down")

    broken = TrackingPoller(
        db=db,
        quote_fetcher=broken_fetch,
        market_session=Session(),
        max_symbols=2,
    )
    assert (await broken.poll_once(now=T1000))["status"] == "quote_fetch_failed"

    seen = []

    class Tracker:
        async def observe(self, plan_id, observation):
            seen.append(observation)

    async def stealing_fetch(codes):
        lock = db["job_locks"].rows[0]
        lock["owner"] = "new-owner"
        lock["fence"] += 1
        return {"600519": _tick(T1000, 10)}

    stolen = TrackingPoller(
        db=db,
        quote_fetcher=stealing_fetch,
        market_session=Session(),
        max_symbols=2,
        tracking_service=Tracker(),
    )
    assert (await stolen.poll_once(now=T1000))["status"] == "lock_lost"
    assert seen == []
