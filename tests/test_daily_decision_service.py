from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.daily_decision_service import (
    DailyDecisionService,
    DecisionPersistenceError,
    material_hash,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def _profile(code: str, *, complete: bool = True) -> dict:
    return {
        "code": code,
        "name": f"Stock {code}",
        "provider_sector": "信息技术" if complete else None,
        "industry": "计算机设备" if complete else None,
        "main_business": "服务器与算力基础设施" if complete else None,
        "provider_sector_evidence": {
            "value": "信息技术",
            "source": "tushare",
            "source_endpoint": "stock_basic",
            "source_record_key": f"{code}.SZ",
            "source_updated_at": "2026-07-21T00:00:00+00:00",
            "retrieved_at": "2026-07-22T00:00:00+00:00",
            "normalization_version": "cn-sector-v1",
        },
        "industry_evidence": {
            "value": "计算机设备",
            "source": "tushare",
            "source_endpoint": "stock_basic",
            "source_record_key": f"{code}.SZ",
            "source_updated_at": "2026-07-21T00:00:00+00:00",
            "retrieved_at": "2026-07-22T00:00:00+00:00",
        },
        "main_business_evidence": {
            "value": "服务器与算力基础设施",
            "source": "tushare",
            "source_endpoint": "stock_company",
            "source_record_key": f"{code}.SZ",
            "source_updated_at": "2026-07-21T00:00:00+00:00",
            "retrieved_at": "2026-07-22T00:00:00+00:00",
        },
        "revenue_composition": {
            "items": [{"name": "服务器", "value": 100.0}],
            "source": "tushare",
            "source_endpoint": "fina_mainbz",
            "source_record_key": f"{code}.SZ:20251231:P",
            "report_period": "2025-12-31",
            "retrieved_at": "2026-07-22T00:00:00+00:00",
        },
        "data_quality": {
            "complete": complete,
            "missing_fields": [] if complete else ["provider_sector"],
            "provider_errors": [],
            "profile_conflicts": [],
        },
    }


def _candidate(code: str = "000977", **updates) -> dict:
    candidate = {
        "code": code,
        "name": "浪潮信息",
        "rank": 1,
        "rank_score": 88.125,
        "objective_tier": "core",
        "objective_segment": "数字科技",
        "reference_price": 63.005,
        "quote_source": "tencent",
        "trade_at": "2026-07-22T10:00:00+08:00",
        "quote_checked_at": "2026-07-22T10:00:01+08:00",
        "quote": {
            "price": 63.005,
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "quote_checked_at": "2026-07-22T10:00:01+08:00",
        },
        "price_plan": {
            "entry_strategy": "pullback",
            "entry_price": 63.0,
            "stop_price": 61.5,
            "target_price": 67.0,
            "price_condition_met": True,
            "entry_status": "price_ready",
            "status": "ok",
        },
        "plans": {
            "short": {"horizon": "short", "entry_price": 63.0},
            "swing": {"horizon": "swing", "entry_price": 63.0},
            "position": {"horizon": "position", "entry_price": 63.0},
        },
        "position_sizing": {"status": "sized", "suggested_quantity": 100},
        "risk_flags": [],
    }
    candidate.update(updates)
    return candidate


def _run(candidates=None) -> dict:
    candidates = candidates or [_candidate()]
    return {
        "run_id": "run-001",
        "generated_at": "2026-07-22T09:55:00+08:00",
        "plan_expires_at": "2026-07-25T15:00:00+08:00",
        "candidates": candidates,
        "market": {"regime": "green", "domestic_regime": "green"},
        "portfolio_plan": {
            "policy": {
                "available_new_exposure_pct": 60.0,
                "total_new_position_loss_budget_pct": 2.0,
                "hard_single_symbol_cap_pct": 40.0,
                "policy_version": "investment-policy-v2",
                "fee_policy_version": "cn_a_v1",
            }
        },
    }


def _briefing() -> dict:
    return {
        "as_of": "2026-07-22T10:00:00+08:00",
        "account": {"total_assets": 100000.0, "available_cash": 60000.0},
        "holdings": {"items": []},
        "market": {"combined_regime": "green", "domestic_regime": "green"},
        "data_quality": {},
    }


def _condition_order_briefing() -> dict:
    briefing = _briefing()
    briefing["account"]["execution_capabilities"] = {
        "condition_order": {
            "verified": True,
            "independent_trigger_price_supported": True,
            "separate_order_limit_price_supported": True,
        }
    }
    return briefing


class FakeBriefingService:
    def __init__(self, payload=None):
        self.payload = payload or _briefing()
        self.calls = []

    async def build(self, user_id, *, refresh=True):
        self.calls.append((user_id, refresh))
        return deepcopy(self.payload)


class FakeCandidateService:
    def __init__(self, payload=None):
        self.payload = payload or _run()
        self.calls = []

    async def latest(self, user_id, *, refresh_quotes=True):
        self.calls.append((user_id, refresh_quotes))
        return deepcopy(self.payload)


class FakeSessionPolicy:
    def __init__(self, phase):
        self.phase = phase

    async def classify(self, *, now=None):
        return {
            "phase": self.phase,
            "is_trading_day": self.phase not in {"closed_day", "calendar_unknown"},
            "buy_now_allowed": self.phase in {"live_am", "live_pm"},
            "quote_freshness_required_seconds": 90 if self.phase in {"live_am", "live_pm"} else 0,
            "classified_at": (now or NOW).isoformat(),
            "calendar_authoritative": self.phase != "calendar_unknown",
            "timezone": "Asia/Shanghai",
        }

    async def quote_status(self, quote, *, now=None, session=None):
        trade_at = str(quote.get("trade_at") or "")
        actionable = bool(
            self.phase in {"live_am", "live_pm"}
            and quote.get("source") == "tencent"
            and trade_at.startswith("2026-07-22")
            and quote.get("fresh", True)
        )
        return {
            "actionable": actionable,
            "status": "fresh" if actionable else "stale_trade_at",
            "trade_at": quote.get("trade_at"),
            "source": quote.get("source"),
        }


class FakeProfileResolver:
    def __init__(self, profiles=None):
        self.profiles = profiles or {}
        self.calls = []

    async def resolve_many(self, codes, refresh=False):
        self.calls.append((tuple(codes), refresh))
        return {code: deepcopy(self.profiles.get(code) or _profile(code)) for code in codes}


class FakeDiversificationService:
    async def allocate(self, candidates, **kwargs):
        allocations = []
        for candidate in candidates:
            forced = candidate.get("test_allocation") or {}
            status = forced.get("status", "allocated")
            reason = forced.get("reason", "allocated")
            quantity = forced.get("quantity", 100 if status == "allocated" else 0)
            amount = forced.get("amount", 6300.0 if status == "allocated" else 0.0)
            allocations.append(
                {
                    **deepcopy(candidate),
                    "status": status,
                    "reason": reason,
                    "reason_codes": forced.get("reason_codes", [reason]),
                    "quantity": quantity,
                    "amount": amount,
                    "position_pct": 6.3 if status == "allocated" else 0.0,
                    "planned_loss_amount": 150.0 if status == "allocated" else 0.0,
                    "planned_loss_pct_of_assets": 0.15 if status == "allocated" else 0.0,
                    "exposure_audit": {"industry": {"after_pct": 6.3, "cap_pct": 30.0}},
                    "correlation_audit": {"cap": 0.8, "comparisons": [], "blocking_pair": None},
                }
            )
        return {
            "allocations": allocations,
            "policy": {**kwargs["policy"], "theme_exposure_cap_pct": 35.0},
            "effective_limits": {
                "theme_exposure_cap_pct": 35.0,
                "provider_sector_exposure_cap_pct": 40.0,
                "industry_exposure_cap_pct": 30.0,
                "pairwise_correlation_cap": 0.8,
            },
            "holding_valuation_audit": [],
        }


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.limit_value = None

    def sort(self, spec):
        for field, direction in reversed(spec):
            self.rows.sort(key=lambda row: row.get(field), reverse=direction < 0)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def to_list(self, length):
        limit = min(length, self.limit_value or length)
        return [deepcopy(row) for row in self.rows[:limit]]


class FakeDailyDecisions:
    def __init__(self, *, fail_insert=False, duplicate_once=False, insert_delay=0.0):
        self.rows = []
        self._guard = asyncio.Lock()
        self.fail_insert = fail_insert
        self.duplicate_once = duplicate_once
        self.insert_delay = insert_delay

    async def find_one(self, query, projection=None, sort=None):
        async with self._guard:
            rows = [row for row in self.rows if _matches(row, query)]
            if sort:
                for field, direction in reversed(sort):
                    rows.sort(key=lambda row: row.get(field), reverse=direction < 0)
            return deepcopy(rows[0]) if rows else None

    async def insert_one(self, document):
        if self.fail_insert:
            raise RuntimeError("mongo unavailable")
        if self.insert_delay:
            await asyncio.sleep(self.insert_delay)
        async with self._guard:
            if self.duplicate_once:
                self.duplicate_once = False
                winner = deepcopy(document)
                winner["decision_id"] = "decision_competing_winner"
                self.rows.append(winner)
                raise FakeDuplicateKeyError()
            for row in self.rows:
                same_revision = all(
                    row.get(key) == document.get(key)
                    for key in ("user_id", "decision_date", "market_phase", "revision")
                )
                same_hash = all(
                    row.get(key) == document.get(key)
                    for key in ("user_id", "decision_date", "market_phase", "material_hash")
                )
                if same_revision or same_hash:
                    raise FakeDuplicateKeyError()
            self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document["decision_id"])

    def find(self, query, projection=None):
        return FakeCursor([row for row in self.rows if _matches(row, query)])


class FakeJobLocks:
    def __init__(self, *, steal_on_renew=False):
        self.rows = {}
        self._guard = asyncio.Lock()
        self.steal_on_renew = steal_on_renew

    async def find_one_and_update(self, query, update, upsert=False, return_document=None):
        async with self._guard:
            key = query["_id"]
            existing = self.rows.get(key)
            owner = update["$set"].get("owner") or query.get("owner")
            now = update["$set"]["updated_at"]
            if self.steal_on_renew and "fence" in query and existing:
                self.steal_on_renew = False
                existing["owner"] = "newer-worker"
                existing["fence"] = int(existing.get("fence") or 0) + 1
            if "fence" in query:
                allowed = bool(
                    existing
                    and existing.get("owner") == owner
                    and existing.get("fence") == query.get("fence")
                    and existing.get("lease_until")
                    > query["lease_until"]["$gt"]
                )
            else:
                allowed = bool(
                    existing is None
                    or existing.get("lease_until") <= now
                    or existing.get("owner") == owner
                )
            if not allowed:
                return None
            row = {**(existing or {"_id": key}), **update["$set"]}
            for field, increment in (update.get("$inc") or {}).items():
                row[field] = int(row.get(field) or 0) + int(increment)
            for field, floor in (update.get("$max") or {}).items():
                row[field] = max(int(row.get(field) or 0), int(floor))
            self.rows[key] = row
            return deepcopy(row)

    async def update_one(self, query, update):
        async with self._guard:
            row = self.rows.get(query["_id"])
            if self.steal_on_renew and "lease_until" in query and row:
                self.steal_on_renew = False
                row["owner"] = "newer-worker"
                row["fence"] = int(row.get("fence") or 0) + 1
            lease_query = query.get("lease_until")
            lease_valid = not isinstance(lease_query, dict) or row.get(
                "lease_until"
            ) > lease_query.get("$gt")
            matches = bool(
                row
                and row.get("owner") == query.get("owner")
                and (
                    "fence" not in query or row.get("fence") == query.get("fence")
                )
                and lease_valid
            )
            if matches:
                row.update(update["$set"])
        return SimpleNamespace(modified_count=1 if matches else 0)


class FakeDuplicateKeyError(Exception):
    pass


class FakeTrackingService:
    def __init__(self, *, fail=False):
        self.packets = []
        self.fail = fail

    async def register_decision(self, packet):
        if self.fail:
            raise RuntimeError("tracking unavailable")
        self.packets.append(deepcopy(packet))
        return []


class FakeDatabase:
    def __init__(
        self,
        *,
        fail_insert=False,
        duplicate_once=False,
        insert_delay=0.0,
        steal_lease_on_renew=False,
    ):
        self.decisions = FakeDailyDecisions(
            fail_insert=fail_insert,
            duplicate_once=duplicate_once,
            insert_delay=insert_delay,
        )
        self.locks = FakeJobLocks(steal_on_renew=steal_lease_on_renew)

    def __getitem__(self, name):
        return {"daily_decisions": self.decisions, "job_locks": self.locks}[name]


def _matches(row, query):
    return all(row.get(key) == value for key, value in query.items())


def _service(*, phase="live_am", run=None, briefing=None, profiles=None, db=None, tracking=None):
    return DailyDecisionService(
        briefing_service=FakeBriefingService(briefing),
        candidate_service=FakeCandidateService(run),
        market_session_policy=FakeSessionPolicy(phase),
        profile_resolver=FakeProfileResolver(profiles),
        diversification_service=FakeDiversificationService(),
        tracking_service=tracking or FakeTrackingService(),
        db=db or FakeDatabase(),
        duplicate_key_errors=(FakeDuplicateKeyError,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_bucket"),
    [
        ("pre_open", "wait"),
        ("live_am", "buy_now"),
        ("midday_break", "wait"),
        ("live_pm", "buy_now"),
        ("post_close", "wait"),
        ("closed_day", "wait"),
        ("calendar_unknown", "wait"),
    ],
)
async def test_all_market_phases_are_exhaustively_bucketed(phase, expected_bucket):
    packet = await _service(phase=phase).today("user-1", now=NOW)

    assert sum(len(packet[name]) for name in ("avoid", "wait", "buy_now", "condition_order")) == 1
    assert packet[expected_bucket][0]["identity"]["code"] == "000977"


@pytest.mark.asyncio
async def test_daily_decision_explicitly_identifies_software_baseline_authority():
    packet = await _service().today("user-1", now=NOW)

    assert packet["authority"] == "software_baseline"
    assert packet["is_final_decision"] is False


@pytest.mark.asyncio
async def test_packet_separates_current_briefing_time_from_candidate_generation_time():
    packet = await _service().today("user-1", now=NOW)

    assert packet["briefing_as_of"] == "2026-07-22T10:00:00+08:00"
    assert packet["candidate_generated_at"] == "2026-07-22T09:55:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason_code",
    [
        "plan_invalidated",
        "plan_expired",
        "target_reached",
        "blocking_event",
        "market_red",
        "objective_mismatch",
        "hard_data_failure",
    ],
)
async def test_every_hard_reason_is_avoid_and_beats_wait_and_buy(reason_code):
    candidate = _candidate(decision_reason_codes=[reason_code, "profile_incomplete"])
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    assert packet["avoid"][0]["reason_codes"][0] == reason_code
    assert not packet["wait"] and not packet["buy_now"] and not packet["condition_order"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason_code",
    [
        "account_blocked",
        "calendar_unknown",
        "profile_incomplete",
        "one_lot_unaffordable",
        "holding_valuation_missing",
        "holding_taxonomy_missing",
        "concentration_limit",
        "correlation_limit",
        "loss_budget_exhausted",
    ],
)
async def test_every_wait_reason_beats_live_buy(reason_code):
    candidate = _candidate(decision_reason_codes=[reason_code])
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    assert packet["wait"][0]["reason_codes"][0] == reason_code
    assert not packet["buy_now"] and not packet["condition_order"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_updates", "expected_reason"),
    [
        ({"actionability": "invalidated"}, "plan_invalidated"),
        ({"actionability": "expired"}, "plan_expired"),
        ({"plan_expires_at": "2026-07-22T09:59:59+08:00"}, "plan_expired"),
        ({"actionability": "target_reached"}, "target_reached"),
        (
            {"risk_flags": [{"code": "suspension", "severity": "high"}]},
            "blocking_event",
        ),
        (
            {"risk_flags": [{"code": "corporate_action", "severity": "error"}]},
            "blocking_event",
        ),
        ({"market_regime": "red"}, "market_red"),
        ({"objective_tier": "non_core"}, "objective_mismatch"),
        ({"hard_data_failure": True}, "hard_data_failure"),
    ],
)
async def test_real_candidate_fields_derive_avoid_reasons(candidate_updates, expected_reason):
    packet = await _service(run=_run([_candidate(**candidate_updates)])).today(
        "user-1", now=NOW
    )

    assert packet["avoid"][0]["reason_codes"][0] == expected_reason


@pytest.mark.asyncio
async def test_incomplete_real_profile_derives_wait_reason():
    packet = await _service(
        profiles={"000977": _profile("000977", complete=False)}
    ).today("user-1", now=NOW)

    assert packet["wait"][0]["reason_codes"][0] == "profile_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allocation_reason", "expected_reason"),
    [
        ("shared_capital_budget_exhausted", "one_lot_unaffordable"),
        ("holding_quote_stale", "holding_valuation_missing"),
        ("holding_taxonomy_missing", "holding_taxonomy_missing"),
        ("hard_single_symbol_cap", "concentration_limit"),
        ("correlation_limit", "correlation_limit"),
        ("shared_loss_budget_exhausted", "loss_budget_exhausted"),
    ],
)
async def test_real_diversification_reasons_map_to_exact_wait_codes(
    allocation_reason, expected_reason
):
    candidate = _candidate(
        test_allocation={
            "status": "wait",
            "reason": allocation_reason,
            "reason_codes": [allocation_reason],
        }
    )
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    assert packet["wait"][0]["reason_codes"][0] == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("trade_at", [None, "stale"])
async def test_missing_or_stale_live_quote_waits_for_recheck(trade_at):
    candidate = _candidate(trade_at=trade_at)
    candidate["quote"]["trade_at"] = trade_at
    if trade_at == "stale":
        candidate["quote_fresh"] = False
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    item = packet["wait"][0]
    assert "live_quote_recheck_required" in item["reason_codes"]
    assert not packet["condition_order"]


@pytest.mark.asyncio
async def test_top_level_reference_price_without_bound_quote_snapshot_is_not_actionable():
    candidate = _candidate()
    candidate.pop("quote")

    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    item = packet["wait"][0]
    assert item["reason_codes"] == ["live_quote_recheck_required"]
    assert item["quote"]["price"] is None
    assert item["quote"]["trade_at"] is None
    assert not packet["buy_now"]
    assert not packet["condition_order"]


@pytest.mark.asyncio
async def test_unverified_star_market_permission_prefilters_candidate_before_decision():
    packet = await _service(run=_run([_candidate(code="688115")])).today(
        "user-1",
        now=NOW,
    )

    assert not packet["avoid"]
    assert not packet["wait"]
    assert not packet["buy_now"]
    assert not packet["condition_order"]
    assert packet["summary"]["permission_prefilter_excluded_count"] == 1
    assert packet["permission_prefilter_excluded"] == [
        {
            "code": "688115",
            "name": "浪潮信息",
            "board": "STAR",
            "reason_code": "star_market_permission_unverified",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permission", "reason_code"),
    [
        (None, "beijing_stock_exchange_permission_unverified"),
        (
            {"verified": True, "tradable": False},
            "beijing_stock_exchange_permission_denied",
        ),
    ],
)
async def test_beijing_permission_prefilters_candidate_before_decision(
    permission,
    reason_code,
):
    briefing = _briefing()
    if permission is not None:
        briefing["account"]["execution_capabilities"] = {
            "market_permissions": {
                "beijing_stock_exchange": permission,
            }
        }

    packet = await _service(
        run=_run([_candidate(code="920493", name="并行科技")]),
        briefing=briefing,
    ).today("user-1", now=NOW)

    assert not packet["avoid"]
    assert not packet["wait"]
    assert not packet["buy_now"]
    assert not packet["condition_order"]
    assert packet["permission_prefilter_excluded"] == [
        {
            "code": "920493",
            "name": "并行科技",
            "board": "BSE",
            "reason_code": reason_code,
        }
    ]
    permission_result = packet["execution_capabilities"][
        "market_permissions"
    ]["beijing_stock_exchange"]
    assert permission_result["eligible"] is False
    assert permission_result["reason_code"] in {
        "permission_unverified",
        "permission_denied",
    }


@pytest.mark.asyncio
async def test_verified_beijing_permission_keeps_candidate_in_decision_scope():
    briefing = _briefing()
    briefing["account"]["execution_capabilities"] = {
        "market_permissions": {
            "beijing_stock_exchange": {
                "verified": True,
                "tradable": True,
            }
        }
    }

    packet = await _service(
        run=_run([_candidate(code="920493", name="并行科技")]),
        briefing=briefing,
    ).today("user-1", now=NOW)

    assert packet["permission_prefilter_excluded"] == []
    assert sum(len(packet[bucket]) for bucket in ("avoid", "wait", "buy_now")) == 1


@pytest.mark.asyncio
async def test_decision_propagates_candidate_run_permission_audit():
    run = _run([_candidate()])
    run["permission_prefilter_excluded"] = [
        {
            "code": "688208",
            "name": "道通科技",
            "board": "STAR",
            "reason_code": "star_market_permission_denied",
        }
    ]
    briefing = _briefing()
    briefing["account"]["execution_capabilities"] = {
        "market_permissions": {
            "star_market": {"verified": True, "tradable": False}
        }
    }

    packet = await _service(run=run, briefing=briefing).today(
        "user-1",
        now=NOW,
    )

    assert packet["summary"]["permission_prefilter_excluded_count"] == 1
    assert packet["permission_prefilter_excluded"] == [
        {
            "code": "688208",
            "name": "道通科技",
            "board": "STAR",
            "reason_code": "star_market_permission_denied",
        }
    ]


@pytest.mark.asyncio
async def test_user_exclusion_prefilters_candidate_before_profile_and_allocation():
    briefing = _briefing()
    briefing["account"]["excluded_codes"] = ["600406"]
    service = _service(
        run=_run(
            [
                _candidate(code="600406", name="国电南瑞"),
                _candidate(code="000977", name="浪潮信息"),
            ]
        ),
        briefing=briefing,
    )

    packet = await service.today("user-1", now=NOW)

    assert packet["permission_prefilter_excluded"] == [
        {
            "code": "600406",
            "name": "国电南瑞",
            "board": "A_SHARE",
            "reason_code": "user_excluded",
        }
    ]
    resolved_codes, _ = service.profile_resolver.calls[0]
    assert resolved_codes == ("000977",)
    assert [
        item["identity"]["code"]
        for bucket in ("avoid", "wait", "buy_now", "condition_order")
        for item in packet[bucket]
    ] == ["000977"]


@pytest.mark.asyncio
async def test_live_quote_without_positive_price_waits_for_recheck():
    candidate = _candidate(reference_price=None)
    candidate["quote"]["price"] = None
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    item = packet["wait"][0]
    assert "live_quote_recheck_required" in item["reason_codes"]
    assert not packet["buy_now"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_updates",
    [
        {"quote_source": "akshare"},
        {"trade_at": "2026-07-21T14:59:59+08:00"},
    ],
)
async def test_non_tencent_or_wrong_day_quote_never_becomes_buy_now(candidate_updates):
    candidate = _candidate(**candidate_updates)
    if "quote_source" in candidate_updates:
        candidate["quote"]["source"] = candidate_updates["quote_source"]
    if "trade_at" in candidate_updates:
        candidate["quote"]["trade_at"] = candidate_updates["trade_at"]
    packet = await _service(run=_run([candidate])).today(
        "user-1", now=NOW
    )

    assert packet["wait"][0]["reason_codes"] == ["live_quote_recheck_required"]
    assert not packet["buy_now"]


@pytest.mark.asyncio
async def test_unmet_live_price_condition_is_condition_order():
    candidate = _candidate()
    candidate["price_plan"]["price_condition_met"] = False
    candidate["price_plan"]["order_limit_price"] = 63.0
    packet = await _service(
        run=_run([candidate]),
        briefing=_condition_order_briefing(),
    ).today("user-1", now=NOW)

    assert packet["condition_order"][0]["action"] == "condition_order"


@pytest.mark.asyncio
async def test_unmet_live_price_waits_when_condition_order_capability_is_unverified():
    candidate = _candidate()
    candidate["price_plan"]["price_condition_met"] = False
    briefing = _briefing()
    briefing["account"].pop("execution_capabilities", None)

    packet = await _service(
        run=_run([candidate]),
        briefing=briefing,
    ).today("user-1", now=NOW)

    assert not packet["condition_order"]
    assert packet["wait"][0]["reason_codes"] == [
        "condition_order_capability_unverified"
    ]


@pytest.mark.asyncio
async def test_fresh_price_at_or_below_stop_invalidates_plan_before_buying():
    candidate = _candidate(reference_price=61.50)
    candidate["quote"]["price"] = 61.50

    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    assert packet["avoid"][0]["reason_codes"] == ["plan_invalidated"]
    assert not packet["buy_now"]
    assert not packet["condition_order"]


@pytest.mark.asyncio
async def test_item_contract_and_half_up_normalization_are_complete():
    packet = await _service().today("user-1", now=NOW)
    item = packet["buy_now"][0]

    assert item["quote"]["price"] == 63.01
    assert {
        "identity",
        "action",
        "reason_codes",
        "calibration_features",
        "quote",
        "plans",
        "profile",
        "allocation",
        "portfolio_impact",
        "planned_loss",
        "invalidation",
        "versions",
        "plan_id",
    } <= set(item)
    assert set(item["calibration_features"]) == {
        "objective_match",
        "reward_risk",
        "evidence_completeness",
        "actionability",
    }
    assert set(item["plans"]) == {"short", "swing", "position"}
    assert item["profile"]["provider_sector_evidence"]["source_endpoint"] == "stock_basic"
    assert packet["effective_policy"]["fee_policy_version"] == "cn_a_v1"
    assert packet["effective_policy"]["provider_versions"] == {
        "company_profile": "company-profile-adapters-v1",
        "market_quote": "tencent-quote-v1",
        "trading_calendar": "a-share-calendar-v1",
    }
    assert item["versions"]["provider_versions"] == packet["effective_policy"]["provider_versions"]


@pytest.mark.asyncio
async def test_persisted_snapshot_is_registered_for_tracking_before_return():
    tracking = FakeTrackingService()

    packet = await _service(tracking=tracking).today("user-1", now=NOW)

    assert len(tracking.packets) == 1
    assert tracking.packets[0]["decision_id"] == packet["decision_id"]
    assert tracking.packets[0]["revision"] == packet["revision"]


@pytest.mark.asyncio
async def test_tracking_failure_never_returns_a_disconnected_decision():
    service = _service(tracking=FakeTrackingService(fail=True))

    with pytest.raises(DecisionPersistenceError, match="decision_tracking_failed"):
        await service.today("user-1", now=NOW)


def test_canonical_hash_excludes_only_explicit_volatile_paths():
    packet = {
        "decision_id": "one",
        "as_of": "one",
        "created_at": "one",
        "persisted_at": "one",
        "persistence": {"owner": "one"},
        "briefing_as_of": "material",
        "quote": {
            "price": 10.0,
            "trade_at": "2026-07-22T10:00:00+08:00",
            "quote_checked_at": "one",
            "age_seconds": 1.1,
            "event_age_seconds": 0.2,
        },
        "profile": {
            "industry_evidence": {
                "value": "计算机设备",
                "source": "tushare",
                "source_endpoint": "stock_basic",
                "source_record_key": "000977.SZ",
                "source_updated_at": "2026-07-21T00:00:00+00:00",
                "retrieved_at": "one",
                "report_period": "2025-12-31",
            }
        },
        "effective_policy": {
            "policy_version": "p1",
            "taxonomy_version": "t1",
            "fee_policy_version": "f1",
        },
        "audit": {"quote_checked_at": "material-outside-transport"},
    }
    baseline = material_hash(packet)
    volatile = deepcopy(packet)
    volatile.update(decision_id="two", as_of="two", created_at="two", persisted_at="two", persistence={"owner": "two"})
    volatile["quote"]["quote_checked_at"] = "two"
    volatile["quote"]["age_seconds"] = 89.9
    volatile["quote"]["event_age_seconds"] = 3.2
    volatile["profile"]["industry_evidence"]["retrieved_at"] = "two"
    assert material_hash(volatile) == baseline

    unrelated_retrieval = deepcopy(packet)
    unrelated_retrieval["metadata"] = {"retrieved_at": "material-one"}
    unrelated_changed = deepcopy(unrelated_retrieval)
    unrelated_changed["metadata"]["retrieved_at"] = "material-two"
    assert material_hash(unrelated_changed) != material_hash(unrelated_retrieval)

    material_paths = [
        ("quote", "trade_at"),
        ("profile", "industry_evidence", "source_updated_at"),
        ("profile", "industry_evidence", "report_period"),
        ("profile", "industry_evidence", "source"),
        ("profile", "industry_evidence", "source_endpoint"),
        ("profile", "industry_evidence", "source_record_key"),
        ("profile", "industry_evidence", "value"),
        ("effective_policy", "policy_version"),
        ("effective_policy", "taxonomy_version"),
        ("effective_policy", "fee_policy_version"),
        ("audit", "quote_checked_at"),
    ]
    for path in material_paths:
        changed = deepcopy(packet)
        cursor = changed
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = "changed"
        assert material_hash(changed) != baseline, path


@pytest.mark.asyncio
async def test_plan_id_and_material_hash_are_stable_for_same_material():
    service = _service()
    first = await service.today("user-1", now=NOW)
    second = await service.today("user-1", now=NOW)

    assert first["buy_now"][0]["plan_id"] == second["buy_now"][0]["plan_id"]
    assert first["material_hash"] == second["material_hash"]
    assert first["decision_id"] == second["decision_id"]


@pytest.mark.asyncio
async def test_request_and_briefing_build_times_do_not_create_new_revision():
    db = FakeDatabase()
    service = _service(db=db)
    first = await service.today("user-1", now=NOW)
    service.briefing_service.payload["as_of"] = "2026-07-22T10:00:30+08:00"
    second = await service.today(
        "user-1",
        now=NOW.replace(second=30),
    )

    assert first["decision_id"] == second["decision_id"]
    assert len(db.decisions.rows) == 1
    assert "classified_at" not in first["market_session"]
    assert first["briefing_as_of"] == "2026-07-22T10:00:00+08:00"
    assert first["candidate_generated_at"] == "2026-07-22T09:55:00+08:00"


@pytest.mark.asyncio
async def test_current_candidate_live_gate_overrides_stale_briefing_regime():
    run = _run()
    run["market"] = {
        "domestic_regime": "green",
        "regime": "green",
        "live_gate": {
            "usable": True,
            "status": "ok",
            "source": "tencent_major_indices+akshare_sina_public_breadth",
            "trade_date": "2026-07-22",
            "checked_at": "2026-07-22T10:00:00+08:00",
            "market_gate": {
                "status": "ok",
                "level": "green",
                "trade_date": "2026-07-22",
            },
        },
        "discovery_snapshot": {
            "domestic_regime": "red",
            "trade_date": "2026-07-21",
        },
    }
    briefing = _briefing()
    briefing["market"] = {
        "domestic_regime": "red",
        "combined_regime": "red",
    }

    packet = await _service(run=run, briefing=briefing).today("user-1", now=NOW)

    assert packet["market"]["domestic_regime"] == "green"
    assert packet["market"]["combined_regime"] == "green"
    assert packet["market"]["live_gate"]["usable"] is True
    assert packet["market"]["discovery_snapshot"]["domestic_regime"] == "red"
    assert all(
        "market_red" not in item["reason_codes"]
        for bucket in ("buy_now", "condition_order", "wait", "avoid")
        for item in packet[bucket]
    )


@pytest.mark.asyncio
async def test_missing_candidate_generated_time_never_falls_back_to_build_time():
    db = FakeDatabase()
    run = _run()
    run.pop("generated_at")
    run["candidates"] = []
    service = _service(db=db, run=run)
    first = await service.today("user-1", now=NOW)
    service.briefing_service.payload["as_of"] = "2026-07-22T10:01:00+08:00"
    second = await service.today("user-1", now=NOW.replace(minute=1))

    assert first["decision_id"] == second["decision_id"]
    assert first["candidate_generated_at"] is None
    assert second["candidate_generated_at"] is None
    assert first["briefing_as_of"] == "2026-07-22T10:00:00+08:00"
    assert second["briefing_as_of"] == first["briefing_as_of"]


@pytest.mark.asyncio
async def test_plan_id_changes_when_material_allocation_changes():
    first = await _service().today("user-1", now=NOW)
    changed_candidate = _candidate(
        test_allocation={
            "status": "allocated",
            "reason": "allocated",
            "quantity": 200,
            "amount": 12600.0,
        }
    )
    second = await _service(run=_run([changed_candidate])).today("user-1", now=NOW)

    assert first["buy_now"][0]["plan_id"] != second["buy_now"][0]["plan_id"]
    assert first["material_hash"] != second["material_hash"]


@pytest.mark.asyncio
async def test_profile_lists_and_candidate_order_are_canonical():
    first_profile = _profile("000977")
    first_profile["revenue_composition"]["items"] = [
        {"report_period": "2024-12-31", "composition_type": "P", "name": "软件"},
        {"report_period": "2025-12-31", "composition_type": "P", "name": "服务器"},
    ]
    first_profile["data_quality"]["provider_errors"] = [
        {"field": "industry", "source": "akshare", "source_endpoint": "late", "error_code": "z"},
        {"field": "industry", "source": "tushare", "source_endpoint": "early", "error_code": "a"},
    ]
    second_profile = deepcopy(first_profile)
    second_profile["revenue_composition"]["items"].reverse()
    second_profile["data_quality"]["provider_errors"].reverse()
    candidates = [_candidate("600406", name="国电南瑞"), _candidate("000977")]
    reverse_candidates = list(reversed(candidates))

    first = await _service(
        run=_run(candidates),
        profiles={"000977": first_profile, "600406": _profile("600406")},
    ).today("user-1", now=NOW)
    second = await _service(
        run=_run(reverse_candidates),
        profiles={"000977": second_profile, "600406": _profile("600406")},
    ).today("user-1", now=NOW)

    assert [item["identity"]["code"] for item in first["buy_now"]] == [
        "000977",
        "600406",
    ]
    assert first["material_hash"] == second["material_hash"]
    revenue_items = first["buy_now"][0]["profile"]["revenue_composition"]["items"]
    assert [item["report_period"] for item in revenue_items] == [
        "2025-12-31",
        "2024-12-31",
    ]


@pytest.mark.asyncio
async def test_concurrent_identical_packets_reuse_one_snapshot():
    db = FakeDatabase(insert_delay=0.01)
    first, second = await asyncio.gather(
        _service(db=db).today("user-1", now=NOW),
        _service(db=db).today("user-1", now=NOW),
    )

    assert first["decision_id"] == second["decision_id"]
    assert len(db.decisions.rows) == 1
    assert db.decisions.rows[0]["revision"] == 1


@pytest.mark.asyncio
async def test_concurrent_different_packets_allocate_distinct_revisions():
    db = FakeDatabase(insert_delay=0.01)
    first_run = _run([_candidate(code="000977")])
    second_run = _run([_candidate(code="600406", name="国电南瑞")])
    first, second = await asyncio.gather(
        _service(db=db, run=first_run).today("user-1", now=NOW),
        _service(db=db, run=second_run).today("user-1", now=NOW),
    )

    assert first["material_hash"] != second["material_hash"]
    assert {row["revision"] for row in db.decisions.rows} == {1, 2}
    assert len(db.decisions.rows) == 2


@pytest.mark.asyncio
async def test_duplicate_key_retry_returns_hash_winner():
    db = FakeDatabase(duplicate_once=True)
    service = _service(db=db)
    packet = await service.today("user-1", now=NOW)

    assert packet["decision_id"] == "decision_competing_winner"
    assert len(db.decisions.rows) == 1


@pytest.mark.asyncio
async def test_unknown_unallocated_state_fails_closed_to_wait():
    candidate = _candidate(
        test_allocation={
            "status": "wait",
            "reason": "unexpected_allocator_state",
            "reason_codes": ["unexpected_allocator_state"],
        }
    )
    packet = await _service(run=_run([candidate])).today("user-1", now=NOW)

    assert packet["wait"][0]["reason_codes"] == ["account_blocked"]
    assert not packet["condition_order"]


@pytest.mark.asyncio
async def test_persistence_failure_never_returns_unaudited_packet():
    with pytest.raises(DecisionPersistenceError):
        await _service(db=FakeDatabase(fail_insert=True)).today("user-1", now=NOW)


@pytest.mark.asyncio
async def test_lost_lease_fence_prevents_stale_worker_insert():
    db = FakeDatabase(steal_lease_on_renew=True)
    service = _service(db=db)
    packet = await service._compose_packet("user-1", refresh=True, now=NOW)

    with pytest.raises(DecisionPersistenceError, match="decision_lease_lost"):
        await service._persist_packet(packet, recompute=None)

    assert db.decisions.rows == []


@pytest.mark.asyncio
async def test_revision_reservation_remains_ordered_after_lease_takeover():
    db = FakeDatabase()
    service = _service(db=db)
    packet = await service._compose_packet("user-1", refresh=True, now=NOW)
    old_fence = await service._acquire_lease(packet, "old-worker")
    assert old_fence == 1
    old_revision = await service._reserve_revision(
        packet, "old-worker", old_fence
    )
    lock_id = next(iter(db.locks.rows))
    db.locks.rows[lock_id]["lease_until"] = datetime.now(timezone.utc)

    new_fence = await service._acquire_lease(packet, "new-worker")
    new_revision = await service._reserve_revision(
        packet, "new-worker", new_fence
    )

    assert new_fence > old_fence
    assert old_revision == 1
    assert new_revision == 2


@pytest.mark.asyncio
async def test_history_is_user_scoped_sorted_and_limited():
    db = FakeDatabase()
    service = _service(db=db)
    await service.today("user-1", now=NOW)
    changed = _run([_candidate(code="600406", name="国电南瑞")])
    await _service(db=db, run=changed).today("user-1", now=NOW)
    await _service(db=db).today("user-2", now=NOW)

    history = await service.history("user-1", limit=1)

    assert len(history) == 1
    assert history[0]["user_id"] == "user-1"
    assert history[0]["revision"] == 2


@pytest.mark.asyncio
async def test_composition_uses_latest_run_without_full_market_scan_and_enriches_holdings():
    briefing = _briefing()
    briefing["holdings"]["items"] = [
        {
            "code": "600406",
            "name": "国电南瑞",
            "quantity": 100,
            "market_value": 2200.0,
            "quote_trade_at": "2026-07-22T10:00:00+08:00",
        }
    ]
    service = _service(briefing=briefing)

    await service.today("user-1", refresh=True, now=NOW)

    assert service.candidate_service.calls == [("user-1", True)]
    assert service.briefing_service.calls == [("user-1", False)]
    resolved_codes, refresh = service.profile_resolver.calls[0]
    assert set(resolved_codes) == {"000977", "600406"}
    assert refresh is True
