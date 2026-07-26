"""Append-only shadow-plan tracking for persisted daily decisions."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

from app.core.database import get_mongo_db


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
LIVE_PHASES = frozenset({"live_am", "live_pm"})
METRIC_BASIS = "shadow_trade_v1"

DEFAULT_CN_A_FEE_POLICY: Dict[str, Any] = {
    "version": "cn_a_v1",
    "commission_rate": 0.0003,
    "commission_minimum": 5.0,
    "seller_stamp_duty_rate": 0.0005,
    "slippage_rate_each_side": 0.0005,
}

WAITING_TERMINAL_STATES = frozenset(
    {
        "expired_untriggered",
        "superseded_untriggered",
        "invalidated_stop_gap",
        "invalidated_corporate_action",
        "invalidated_plan",
    }
)
ACTIVE_TERMINAL_STATES = frozenset(
    {
        "closed_stop",
        "closed_target",
        "closed_invalidated_corporate_action",
    }
)
TRACKED_STATES = frozenset({"waiting_entry", "active"})
CALIBRATION_FEATURES = frozenset(
    {"objective_match", "reward_risk", "evidence_completeness", "actionability"}
)


class ObservationConflictError(RuntimeError):
    """The observation sequence changed before the CAS transition completed."""


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError("datetime_required")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed.astimezone(SHANGHAI_TIMEZONE)


def _decimal(value: Any, *, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field}_invalid")
    return parsed


def _positive_float(value: Any, *, field: str) -> float:
    parsed = _decimal(value, field=field)
    if parsed <= 0:
        raise ValueError(f"{field}_invalid")
    return float(parsed)


def _normalise_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    for prefix in ("SH.", "SZ.", "SH", "SZ"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    for suffix in (".SH", ".SZ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _item_short_plan(item: Mapping[str, Any]) -> Mapping[str, Any]:
    plans = item.get("plans")
    plans = plans if isinstance(plans, Mapping) else {}
    short = plans.get("short")
    return short if isinstance(short, Mapping) else {}


def stable_plan_id_from_item(
    item: Mapping[str, Any],
    *,
    user_id: str,
    candidate_run_id: Any,
) -> str:
    """Return the decision's plan id, or deterministically derive it."""

    provided = str(item.get("plan_id") or "").strip()
    if provided:
        return provided
    identity = item.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    allocation = item.get("allocation")
    allocation = allocation if isinstance(allocation, Mapping) else {}
    invalidation = item.get("invalidation")
    invalidation = invalidation if isinstance(invalidation, Mapping) else {}
    versions = item.get("versions")
    versions = versions if isinstance(versions, Mapping) else {}
    plan = _item_short_plan(item)
    payload = {
        "user_id": str(user_id),
        "candidate_run_id": str(candidate_run_id or ""),
        "code": _normalise_code(identity.get("code")),
        "entry_strategy": plan.get("entry_strategy"),
        "entry_price": plan.get("entry_price"),
        "stop_price": plan.get("stop_price") or invalidation.get("stop_price"),
        "target_price": plan.get("target_price"),
        "plan_expires_at": invalidation.get("plan_expires_at"),
        "allocation": {
            key: allocation.get(key)
            for key in ("status", "quantity", "amount", "position_pct")
        },
        "rule_version": versions.get("rule_version"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return f"plan_{hashlib.sha256(encoded).hexdigest()}"


def _fee_policy(policy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    result = {**DEFAULT_CN_A_FEE_POLICY, **dict(policy or {})}
    if str(result.get("version") or "") != "cn_a_v1":
        raise ValueError("unsupported_fee_policy")
    for key in (
        "commission_rate",
        "commission_minimum",
        "seller_stamp_duty_rate",
        "slippage_rate_each_side",
    ):
        value = _decimal(result.get(key), field=key)
        if value < 0:
            raise ValueError(f"{key}_invalid")
        result[key] = float(value)
    return result


def _aligned_benchmark_price(
    observations: Sequence[Mapping[str, Any]], event_at: datetime
) -> Optional[float]:
    event = _parse_datetime(event_at)
    eligible: list[tuple[datetime, float]] = []
    for raw in observations:
        try:
            observed_at = _parse_datetime(raw.get("at") or raw.get("trade_at"))
            price = _positive_float(raw.get("price") or raw.get("close"), field="benchmark_price")
        except (TypeError, ValueError):
            continue
        interval = str(raw.get("interval") or "intraday").lower()
        if interval in {"daily", "day", "1d"}:
            if observed_at.date() == event.date():
                eligible.append((observed_at, price))
        elif observed_at >= event and observed_at - event <= timedelta(minutes=5):
            eligible.append((observed_at, price))
    if not eligible:
        return None
    eligible.sort(key=lambda pair: pair[0])
    return eligible[0][1]


def calculate_trade_metrics(
    *,
    entry_raw_price: Any,
    exit_raw_price: Any,
    quantity: Any,
    entry_at: datetime,
    exit_at: datetime,
    fee_policy: Optional[Mapping[str, Any]] = None,
    mae_price: Any = None,
    mfe_price: Any = None,
    benchmark_observations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculate one immutable, fee-aware shadow-trade outcome."""

    policy = _fee_policy(fee_policy)
    entry_raw = _decimal(entry_raw_price, field="entry_raw_price")
    exit_raw = _decimal(exit_raw_price, field="exit_raw_price")
    qty = _decimal(quantity, field="quantity")
    if entry_raw <= 0 or exit_raw <= 0 or qty <= 0 or qty != qty.to_integral_value():
        raise ValueError("trade_values_invalid")
    commission_rate = Decimal(str(policy["commission_rate"]))
    commission_min = Decimal(str(policy["commission_minimum"]))
    stamp_rate = Decimal(str(policy["seller_stamp_duty_rate"]))
    slippage = Decimal(str(policy["slippage_rate_each_side"]))
    entry_execution = entry_raw * (Decimal("1") + slippage)
    exit_execution = exit_raw * (Decimal("1") - slippage)
    entry_notional = entry_execution * qty
    exit_notional = exit_execution * qty
    entry_commission = max(commission_min, entry_notional * commission_rate)
    exit_commission = max(commission_min, exit_notional * commission_rate)
    seller_stamp = exit_notional * stamp_rate
    total_entry_cost = entry_notional + entry_commission
    net_pnl = exit_notional - exit_commission - seller_stamp - total_entry_cost
    net_return_pct = net_pnl / total_entry_cost * Decimal("100")

    def excursion(value: Any) -> Optional[float]:
        if value is None:
            return None
        parsed = _decimal(value, field="excursion_price")
        return float((parsed / entry_execution - Decimal("1")) * Decimal("100"))

    observations = list(benchmark_observations or [])
    benchmark_entry = _aligned_benchmark_price(observations, entry_at)
    benchmark_exit = _aligned_benchmark_price(observations, exit_at)
    benchmark_return_pct: Optional[float] = None
    alpha_pct: Optional[float] = None
    if benchmark_entry is not None and benchmark_exit is not None:
        benchmark_return_pct = (benchmark_exit / benchmark_entry - 1.0) * 100.0
        alpha_pct = float(net_return_pct) - benchmark_return_pct

    return {
        "entry_raw_price": float(entry_raw),
        "exit_raw_price": float(exit_raw),
        "entry_execution_price": float(entry_execution),
        "exit_execution_price": float(exit_execution),
        "quantity": int(qty),
        "entry_commission": float(entry_commission),
        "exit_commission": float(exit_commission),
        "seller_stamp_duty": float(seller_stamp),
        "total_fees": float(entry_commission + exit_commission + seller_stamp),
        "net_pnl": float(net_pnl),
        "net_return_pct": float(net_return_pct),
        "mae_price": float(_decimal(mae_price, field="mae_price")) if mae_price is not None else None,
        "mfe_price": float(_decimal(mfe_price, field="mfe_price")) if mfe_price is not None else None,
        "mae_pct": excursion(mae_price),
        "mfe_pct": excursion(mfe_price),
        "benchmark_entry_price": benchmark_entry,
        "benchmark_exit_price": benchmark_exit,
        "benchmark_return_pct": benchmark_return_pct,
        "alpha_pct": alpha_pct,
        "fee_policy": deepcopy(policy),
        "metric_basis": METRIC_BASIS,
    }


class DecisionTrackingService:
    """Register stable plans and append CAS-ordered outcome transitions."""

    def __init__(
        self,
        *,
        db: Any = None,
        fee_policy: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._db = db
        self.fee_policy = _fee_policy(fee_policy)

    async def _get_db(self) -> Any:
        if self._db is None:
            self._db = get_mongo_db()
        if inspect.isawaitable(self._db):
            self._db = await self._db
        return self._db

    async def register_decision(self, packet: Mapping[str, Any]) -> list[Dict[str, Any]]:
        user_id = str(packet.get("user_id") or "").strip()
        decision_id = str(packet.get("decision_id") or "").strip()
        if not user_id or not decision_id:
            raise ValueError("decision_identity_required")
        decision_at = _parse_datetime(packet.get("as_of"))
        phase = str(packet.get("market_phase") or "")
        market = packet.get("market")
        market = market if isinstance(market, Mapping) else {}
        revision = int(packet.get("revision") or 0)
        db = await self._get_db()
        registered: list[Dict[str, Any]] = []
        seen_codes: set[str] = set()
        for bucket in ("buy_now", "condition_order", "wait", "avoid"):
            for raw_item in packet.get(bucket) or []:
                if not isinstance(raw_item, Mapping):
                    continue
                item = dict(raw_item)
                identity = item.get("identity")
                identity = identity if isinstance(identity, Mapping) else {}
                code = _normalise_code(identity.get("code"))
                if not (code.isdigit() and len(code) == 6):
                    raise ValueError("plan_code_invalid")
                seen_codes.add(code)
                if bucket not in {"buy_now", "condition_order"}:
                    continue
                plan_id = stable_plan_id_from_item(
                    item,
                    user_id=user_id,
                    candidate_run_id=packet.get("candidate_run_id"),
                )
                reference = {
                    "decision_id": decision_id,
                    "revision": revision,
                    "as_of": decision_at,
                    "action_bucket": bucket,
                    "market_phase": phase,
                }
                existing = await db["decision_plans"].find_one({"plan_id": plan_id})
                if existing:
                    if existing.get("user_id") != user_id:
                        raise ValueError("plan_id_owner_conflict")
                    await db["decision_plans"].update_one(
                        {"plan_id": plan_id, "user_id": user_id},
                        {
                            "$addToSet": {"decision_refs": reference},
                            "$set": {"last_referenced_at": decision_at},
                        },
                    )
                else:
                    try:
                        plan = self._build_plan(
                            item=item,
                            plan_id=plan_id,
                            user_id=user_id,
                            origin_decision_id=decision_id,
                            origin_bucket=bucket,
                            origin_phase=phase,
                            eligibility_at=decision_at,
                            reference=reference,
                            market=market,
                        )
                    except ValueError:
                        if bucket in {"wait", "avoid"}:
                            continue
                        raise
                    try:
                        await db["decision_plans"].insert_one(plan)
                    except Exception:
                        winner = await db["decision_plans"].find_one(
                            {"plan_id": plan_id}
                        )
                        if not winner or winner.get("user_id") != user_id:
                            raise
                        await db["decision_plans"].update_one(
                            {"plan_id": plan_id, "user_id": user_id},
                            {
                                "$addToSet": {"decision_refs": reference},
                                "$set": {"last_referenced_at": decision_at},
                            },
                        )
                registered.append(
                    await db["decision_plans"].find_one(
                        {"plan_id": plan_id, "user_id": user_id}
                    )
                )

        for code in sorted(seen_codes):
            active_plan_ids = {
                row["plan_id"] for row in registered if row.get("code") == code
            }
            cursor = db["decision_plans"].find(
                {
                    "user_id": user_id,
                    "code": code,
                    "latest_state": "waiting_entry",
                }
            )
            for old in await cursor.to_list(length=None):
                if old.get("plan_id") in active_plan_ids:
                    continue
                await self.transition(
                    old["plan_id"],
                    expected_sequence=int(old.get("observation_sequence") or 0),
                    new_state="superseded_untriggered",
                    observed_at=decision_at,
                    details={"superseded_by_decision_id": decision_id},
                )
        return registered

    def _build_plan(
        self,
        *,
        item: Mapping[str, Any],
        plan_id: str,
        user_id: str,
        origin_decision_id: str,
        origin_bucket: str,
        origin_phase: str,
        eligibility_at: datetime,
        reference: Mapping[str, Any],
        market: Mapping[str, Any],
    ) -> Dict[str, Any]:
        identity = item.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        allocation = item.get("allocation")
        allocation = allocation if isinstance(allocation, Mapping) else {}
        invalidation = item.get("invalidation")
        invalidation = invalidation if isinstance(invalidation, Mapping) else {}
        plan = _item_short_plan(item)
        strategy = str(plan.get("entry_strategy") or "").lower()
        if strategy not in {"pullback", "breakout"}:
            raise ValueError("entry_strategy_invalid")
        entry = _positive_float(plan.get("entry_price"), field="entry_price")
        stop = _positive_float(
            plan.get("stop_price") or invalidation.get("stop_price"), field="stop_price"
        )
        target = _positive_float(plan.get("target_price"), field="target_price")
        if not stop < entry < target:
            raise ValueError("price_plan_invalid")
        raw_quantity = _decimal(allocation.get("quantity"), field="quantity")
        if raw_quantity <= 0 or raw_quantity != raw_quantity.to_integral_value():
            raise ValueError("quantity_invalid")
        quantity = int(raw_quantity)
        expires_at = _parse_datetime(invalidation.get("plan_expires_at"))
        profile = item.get("profile")
        profile = profile if isinstance(profile, Mapping) else {}
        calibration = item.get("calibration_features")
        calibration = calibration if isinstance(calibration, Mapping) else {}
        calibration_features = {
            key: float(_decimal(calibration.get(key), field=key))
            for key in sorted(CALIBRATION_FEATURES)
        }
        reason_codes = item.get("reason_codes")
        if isinstance(reason_codes, str):
            reason_codes = [reason_codes]
        reason_codes = sorted({str(value) for value in reason_codes or [] if str(value)})
        return {
            "plan_id": plan_id,
            "user_id": user_id,
            "code": _normalise_code(identity.get("code")),
            "entry_strategy": strategy,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "quantity": quantity,
            "plan_expires_at": expires_at,
            "origin_decision_id": origin_decision_id,
            "eligibility_at": eligibility_at,
            "origin_action_bucket": origin_bucket,
            "origin_market_phase": origin_phase,
            "horizon": "short",
            "objective_segment": str(identity.get("objective_segment") or "unknown"),
            "industry": str(profile.get("industry") or "unknown"),
            "provider_sector": str(profile.get("provider_sector") or "unknown"),
            "domestic_regime": str(
                market.get("domestic_regime") or market.get("regime") or "unknown"
            ),
            "macro_regime": str(
                market.get("macro_regime")
                or market.get("global_regime")
                or "unknown"
            ),
            "reason_codes": reason_codes,
            "calibration_features": calibration_features,
            "decision_refs": [deepcopy(dict(reference))],
            "latest_state": "waiting_entry",
            "observation_sequence": 0,
            "fee_policy": deepcopy(self.fee_policy),
            "metric_basis": METRIC_BASIS,
            "created_at": datetime.now(timezone.utc),
            "last_referenced_at": eligibility_at,
        }

    async def latest_outcome(self, plan_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        return await db["decision_outcomes"].find_one(
            {"plan_id": plan_id}, sort=[("observation_sequence", -1)]
        )

    @staticmethod
    def _validate_transition(prior_state: str, new_state: str) -> None:
        valid = (
            prior_state == "waiting_entry"
            and (new_state == "active" or new_state in WAITING_TERMINAL_STATES)
        ) or (prior_state == "active" and new_state in ACTIVE_TERMINAL_STATES)
        if not valid:
            raise ValueError("invalid_state_transition")

    async def transition(
        self,
        plan_id: str,
        *,
        expected_sequence: int,
        new_state: str,
        observed_at: Any,
        details: Mapping[str, Any],
    ) -> Dict[str, Any]:
        db = await self._get_db()
        plan = await db["decision_plans"].find_one({"plan_id": plan_id})
        if not plan:
            raise ValueError("plan_not_found")
        if int(plan.get("observation_sequence") or 0) != expected_sequence:
            raise ObservationConflictError("observation_sequence_conflict")
        prior_state = str(plan.get("latest_state") or "")
        self._validate_transition(prior_state, new_state)
        observed = _parse_datetime(observed_at)
        sequence = expected_sequence + 1
        outcome = {
            **deepcopy(dict(details)),
            "plan_id": plan_id,
            "user_id": plan.get("user_id"),
            "code": plan.get("code"),
            "observation_sequence": sequence,
            "prior_state": prior_state,
            "state": new_state,
            "status": new_state,
            "observed_at": observed,
            "origin_decision_id": plan.get("origin_decision_id"),
            "origin_action_bucket": plan.get("origin_action_bucket"),
            "origin_market_phase": plan.get("origin_market_phase"),
            "decision_id": plan.get("trigger_context_decision_id")
            or plan.get("origin_decision_id"),
            "action_bucket": plan.get("trigger_context_action_bucket")
            or plan.get("origin_action_bucket"),
            "market_phase": plan.get("trigger_context_market_phase")
            or plan.get("origin_market_phase"),
            "horizon": plan.get("horizon"),
            "objective_segment": plan.get("objective_segment"),
            "industry": plan.get("industry"),
            "provider_sector": plan.get("provider_sector"),
            "domestic_regime": plan.get("domestic_regime"),
            "macro_regime": plan.get("macro_regime"),
            "entry_strategy": plan.get("entry_strategy"),
            "reason_codes": deepcopy(plan.get("reason_codes") or []),
            "calibration_features": deepcopy(plan.get("calibration_features") or {}),
            "fee_policy": deepcopy(plan.get("fee_policy") or self.fee_policy),
            "metric_basis": METRIC_BASIS,
        }
        plan_updates: Dict[str, Any] = {
            "latest_state": new_state,
            "pending_outcome": outcome,
            "updated_at": datetime.now(timezone.utc),
        }
        for key in (
            "entry_at",
            "entry_raw_price",
            "entry_execution_price",
            "entry_commission",
            "quantity",
            "mae_price",
            "mfe_price",
            "trigger_context_decision_id",
            "trigger_context_action_bucket",
            "trigger_context_market_phase",
            "benchmark_entry_price",
            "exit_at",
            "exit_raw_price",
            "exit_execution_price",
        ):
            if key in outcome:
                plan_updates[key] = deepcopy(outcome[key])
        reserved = await db["decision_plans"].find_one_and_update(
            {
                "plan_id": plan_id,
                "latest_state": prior_state,
                "observation_sequence": expected_sequence,
            },
            {"$set": plan_updates, "$inc": {"observation_sequence": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not reserved:
            raise ObservationConflictError("observation_sequence_conflict")
        try:
            await db["decision_outcomes"].insert_one(outcome)
        except Exception:
            existing = await db["decision_outcomes"].find_one(
                {"plan_id": plan_id, "observation_sequence": sequence}
            )
            comparable = (
                {key: value for key, value in existing.items() if key != "_id"}
                if existing
                else None
            )
            if comparable != outcome:
                raise
            outcome = comparable
        await db["decision_plans"].update_one(
            {
                "plan_id": plan_id,
                "observation_sequence": sequence,
                "pending_outcome.observation_sequence": sequence,
            },
            {"$unset": {"pending_outcome": ""}},
        )
        return deepcopy(outcome)

    async def recover_pending(self, plan_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        plan = await db["decision_plans"].find_one({"plan_id": plan_id})
        pending = (plan or {}).get("pending_outcome")
        if not isinstance(pending, Mapping):
            return None
        outcome = deepcopy(dict(pending))
        sequence = int(outcome.get("observation_sequence") or 0)
        existing = await db["decision_outcomes"].find_one(
            {"plan_id": plan_id, "observation_sequence": sequence}
        )
        if existing is not None and {
            key: value for key, value in existing.items() if key != "_id"
        } != outcome:
            raise ObservationConflictError("outcome_sequence_conflict")
        if existing is None:
            await db["decision_outcomes"].insert_one(outcome)
        await db["decision_plans"].update_one(
            {
                "plan_id": plan_id,
                "observation_sequence": sequence,
                "pending_outcome.observation_sequence": sequence,
            },
            {"$unset": {"pending_outcome": ""}},
        )
        return outcome if existing is None else None

    @staticmethod
    def _trigger_context(plan: Mapping[str, Any], observed_at: datetime) -> Dict[str, Any]:
        refs = [
            ref
            for ref in plan.get("decision_refs") or []
            if isinstance(ref, Mapping)
            and _parse_datetime(ref.get("as_of")) <= observed_at
        ]
        refs.sort(
            key=lambda ref: (_parse_datetime(ref.get("as_of")), int(ref.get("revision") or 0))
        )
        ref = refs[-1] if refs else {
            "decision_id": plan.get("origin_decision_id"),
            "action_bucket": plan.get("origin_action_bucket"),
            "market_phase": plan.get("origin_market_phase"),
        }
        return {
            "trigger_context_decision_id": ref.get("decision_id"),
            "trigger_context_action_bucket": ref.get("action_bucket"),
            "trigger_context_market_phase": ref.get("market_phase"),
        }

    @staticmethod
    def _observation_time(observation: Mapping[str, Any]) -> datetime:
        if observation.get("kind") == "minute_bar":
            return _parse_datetime(observation.get("interval_end"))
        return _parse_datetime(observation.get("trade_at"))

    @staticmethod
    def _eligible(plan: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        eligibility = _parse_datetime(plan.get("eligibility_at"))
        kind = str(observation.get("kind") or "")
        if kind == "last_trade":
            if str(observation.get("source") or "").lower() != "tencent":
                return False
            return _parse_datetime(observation.get("trade_at")) > eligibility
        if kind != "minute_bar" or observation.get("is_closed") is not True:
            return False
        if str(observation.get("source") or "") != "tencent_own_ticks":
            return False
        start = _parse_datetime(observation.get("interval_start"))
        end = _parse_datetime(observation.get("interval_end"))
        if end <= start:
            return False
        if plan.get("origin_market_phase") == "pre_open":
            opening = datetime.combine(eligibility.date(), time(9, 30), SHANGHAI_TIMEZONE)
            if start.date() == eligibility.date():
                return start >= opening
            return start.date() > eligibility.date()
        return start >= eligibility

    async def observe(
        self,
        plan_id: str,
        observation: Mapping[str, Any],
        *,
        benchmark_observations: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        await self.recover_pending(plan_id)
        db = await self._get_db()
        plan = await db["decision_plans"].find_one({"plan_id": plan_id})
        if not plan or plan.get("latest_state") not in TRACKED_STATES:
            return None
        try:
            if not self._eligible(plan, observation):
                return None
            observed_at = self._observation_time(observation)
        except (TypeError, ValueError):
            return None
        if observed_at >= _parse_datetime(plan.get("plan_expires_at")) and plan.get("latest_state") == "waiting_entry":
            return await self.transition(
                plan_id,
                expected_sequence=int(plan.get("observation_sequence") or 0),
                new_state="expired_untriggered",
                observed_at=observed_at,
                details={"reason_code": "plan_expired"},
            )
        if plan.get("latest_state") == "waiting_entry":
            return await self._observe_waiting(plan, observation, observed_at, benchmark_observations or [])
        return await self._observe_active(plan, observation, observed_at, benchmark_observations or [])

    async def _observe_waiting(
        self,
        plan: Mapping[str, Any],
        observation: Mapping[str, Any],
        observed_at: datetime,
        benchmark_observations: Sequence[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        entry = float(plan["entry_price"])
        stop = float(plan["stop_price"])
        strategy = plan["entry_strategy"]
        kind = observation.get("kind")
        raw_fill: Optional[float] = None
        if kind == "minute_bar":
            opening = _positive_float(observation.get("open"), field="open")
            high = _positive_float(observation.get("high"), field="high")
            low = _positive_float(observation.get("low"), field="low")
            if opening <= stop:
                return await self.transition(
                    plan["plan_id"],
                    expected_sequence=int(plan.get("observation_sequence") or 0),
                    new_state="invalidated_stop_gap",
                    observed_at=observed_at,
                    details={"reason_code": "open_at_or_below_stop", "observed_open": opening},
                )
            if strategy == "pullback":
                if stop < opening <= entry:
                    raw_fill = opening
                elif low <= entry <= high:
                    raw_fill = entry
            elif opening >= entry:
                raw_fill = opening
            elif high >= entry:
                raw_fill = entry
        else:
            price = _positive_float(observation.get("price"), field="price")
            if price <= stop:
                return None
            previous = plan.get("last_eligible_tick_price")
            if previous is not None:
                previous_price = _positive_float(previous, field="last_eligible_tick_price")
                if strategy == "pullback" and previous_price > entry >= price:
                    raw_fill = entry
                elif strategy == "breakout" and previous_price < entry <= price:
                    raw_fill = entry
            high = low = price
        if raw_fill is None:
            if kind == "last_trade":
                db = await self._get_db()
                await db["decision_plans"].update_one(
                    {
                        "plan_id": plan["plan_id"],
                        "latest_state": "waiting_entry",
                        "observation_sequence": plan["observation_sequence"],
                    },
                    {
                        "$set": {
                            "last_eligible_tick_price": price,
                            "last_eligible_tick_at": observed_at,
                        }
                    },
                )
            return None
        slippage = float(plan["fee_policy"]["slippage_rate_each_side"])
        entry_execution = raw_fill * (1.0 + slippage)
        quantity = int(plan["quantity"])
        entry_commission = max(
            float(plan["fee_policy"]["commission_minimum"]),
            entry_execution * quantity * float(plan["fee_policy"]["commission_rate"]),
        )
        context = self._trigger_context(plan, observed_at)
        benchmark_entry_price = _aligned_benchmark_price(
            benchmark_observations, observed_at
        )
        active = await self.transition(
            plan["plan_id"],
            expected_sequence=int(plan.get("observation_sequence") or 0),
            new_state="active",
            observed_at=observed_at,
            details={
                **context,
                "entry_at": observed_at,
                "entry_raw_price": raw_fill,
                "entry_execution_price": entry_execution,
                "entry_commission": entry_commission,
                "quantity": quantity,
                "mae_price": min(raw_fill, low),
                "mfe_price": max(raw_fill, high),
                "fill_rule": f"{strategy}_{'bar' if kind == 'minute_bar' else 'last_trade'}",
                "benchmark_entry_price": benchmark_entry_price,
            },
        )
        if kind != "minute_bar":
            return active
        refreshed = await (await self._get_db())["decision_plans"].find_one({"plan_id": plan["plan_id"]})
        exit_state, raw_exit = self._bar_exit(refreshed, observation, just_entered=True)
        if exit_state is None:
            return active
        return await self._close(refreshed, exit_state, raw_exit, observed_at, benchmark_observations)

    @staticmethod
    def _bar_exit(
        plan: Mapping[str, Any], observation: Mapping[str, Any], *, just_entered: bool = False
    ) -> tuple[Optional[str], Optional[float]]:
        opening = float(observation["open"])
        high = float(observation["high"])
        low = float(observation["low"])
        stop = float(plan["stop_price"])
        target = float(plan["target_price"])
        if not just_entered and opening <= stop:
            return "closed_stop", opening
        if not just_entered and opening >= target:
            return "closed_target", target
        if low <= stop:
            return "closed_stop", stop
        if high >= target:
            return "closed_target", target
        return None, None

    async def _update_excursions(
        self, plan: Mapping[str, Any], *, low: float, high: float
    ) -> Mapping[str, Any]:
        mae = min(float(plan.get("mae_price") or plan["entry_raw_price"]), low)
        mfe = max(float(plan.get("mfe_price") or plan["entry_raw_price"]), high)
        db = await self._get_db()
        await db["decision_plans"].update_one(
            {
                "plan_id": plan["plan_id"],
                "latest_state": "active",
                "observation_sequence": plan["observation_sequence"],
            },
            {"$set": {"mae_price": mae, "mfe_price": mfe}},
        )
        return {
            **dict(plan),
            "mae_price": mae,
            "mfe_price": mfe,
        }

    async def _observe_active(
        self,
        plan: Mapping[str, Any],
        observation: Mapping[str, Any],
        observed_at: datetime,
        benchmark_observations: Sequence[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if observation.get("kind") == "minute_bar":
            low = _positive_float(observation.get("low"), field="low")
            high = _positive_float(observation.get("high"), field="high")
            plan = await self._update_excursions(plan, low=low, high=high)
            state, raw_exit = self._bar_exit(plan, observation)
        else:
            price = _positive_float(observation.get("price"), field="price")
            plan = await self._update_excursions(plan, low=price, high=price)
            if price <= float(plan["stop_price"]):
                state, raw_exit = "closed_stop", float(plan["stop_price"])
            elif price >= float(plan["target_price"]):
                state, raw_exit = "closed_target", float(plan["target_price"])
            else:
                state, raw_exit = None, None
        if state is None or raw_exit is None:
            return None
        return await self._close(plan, state, raw_exit, observed_at, benchmark_observations)

    async def _close(
        self,
        plan: Mapping[str, Any],
        state: str,
        raw_exit: float,
        observed_at: datetime,
        benchmark_observations: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        aligned_observations = list(benchmark_observations)
        if plan.get("benchmark_entry_price") is not None:
            aligned_observations.append(
                {
                    "at": _parse_datetime(plan["entry_at"]),
                    "price": plan["benchmark_entry_price"],
                    "interval": "intraday",
                }
            )
        metrics = calculate_trade_metrics(
            entry_raw_price=plan["entry_raw_price"],
            exit_raw_price=raw_exit,
            quantity=plan["quantity"],
            entry_at=_parse_datetime(plan["entry_at"]),
            exit_at=observed_at,
            fee_policy=plan.get("fee_policy"),
            mae_price=plan.get("mae_price"),
            mfe_price=plan.get("mfe_price"),
            benchmark_observations=aligned_observations,
        )
        return await self.transition(
            plan["plan_id"],
            expected_sequence=int(plan["observation_sequence"]),
            new_state=state,
            observed_at=observed_at,
            details={
                **metrics,
                "entry_at": _parse_datetime(plan["entry_at"]),
                "exit_at": observed_at,
                "exit_reason": state.removeprefix("closed_"),
            },
        )

    async def expire_due(self, now: Any) -> list[Dict[str, Any]]:
        observed = _parse_datetime(now)
        db = await self._get_db()
        cursor = db["decision_plans"].find({"latest_state": "waiting_entry"})
        outcomes = []
        for plan in await cursor.to_list(length=None):
            if _parse_datetime(plan.get("plan_expires_at")) <= observed:
                try:
                    outcomes.append(
                        await self.transition(
                            plan["plan_id"],
                            expected_sequence=int(plan.get("observation_sequence") or 0),
                            new_state="expired_untriggered",
                            observed_at=observed,
                            details={"reason_code": "plan_expired"},
                        )
                    )
                except ObservationConflictError:
                    continue
        return outcomes

    async def recover_from_bars(
        self,
        plan_id: str,
        *,
        bars: Optional[Sequence[Mapping[str, Any]]] = None,
        benchmark_observations: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> list[Dict[str, Any]]:
        """Replay persisted closed bars; eligibility checks reject unsafe intervals."""

        await self.recover_pending(plan_id)
        db = await self._get_db()
        plan = await db["decision_plans"].find_one({"plan_id": plan_id})
        if not plan or plan.get("latest_state") not in TRACKED_STATES:
            return []
        if bars is None:
            cursor = db["decision_minute_bars"].find(
                {
                    "code": plan.get("code"),
                    "source": "tencent_own_ticks",
                }
            ).sort([("interval_start", 1)])
            replay = await cursor.to_list(length=None)
        else:
            replay = [deepcopy(dict(bar)) for bar in bars if isinstance(bar, Mapping)]
            replay.sort(key=lambda bar: _parse_datetime(bar.get("interval_start")))
        outcomes: list[Dict[str, Any]] = []
        for bar in replay:
            outcome = await self.observe(
                plan_id,
                bar,
                benchmark_observations=benchmark_observations,
            )
            if outcome is not None:
                outcomes.append(outcome)
            latest = await db["decision_plans"].find_one({"plan_id": plan_id})
            if not latest or latest.get("latest_state") not in TRACKED_STATES:
                break
        return outcomes

    async def apply_corporate_action(
        self, plan_id: str, action: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        await self.recover_pending(plan_id)
        db = await self._get_db()
        plan = await db["decision_plans"].find_one({"plan_id": plan_id})
        if not plan or plan.get("latest_state") not in TRACKED_STATES:
            return None
        observed = _parse_datetime(action.get("effective_at"))
        state = (
            "invalidated_corporate_action"
            if plan["latest_state"] == "waiting_entry"
            else "closed_invalidated_corporate_action"
        )
        return await self.transition(
            plan_id,
            expected_sequence=int(plan.get("observation_sequence") or 0),
            new_state=state,
            observed_at=observed,
            details={
                "reason_code": "corporate_action",
                "corporate_action": deepcopy(dict(action)),
            },
        )


class MinuteBarAggregator:
    """Aggregate Tencent last-trade ticks and emit only completed minutes."""

    def __init__(self) -> None:
        self._buckets: Dict[str, Dict[str, Any]] = {}

    def add(self, code: str, quote: Mapping[str, Any]) -> list[Dict[str, Any]]:
        if str(quote.get("source") or "").lower() != "tencent":
            return []
        try:
            at = _parse_datetime(quote.get("trade_at"))
            price = _positive_float(quote.get("price") or quote.get("close"), field="price")
        except (TypeError, ValueError):
            return []
        minute = at.replace(second=0, microsecond=0)
        normalized = _normalise_code(code)
        current = self._buckets.get(normalized)
        if current is None or minute > current["interval_start"]:
            emitted = [deepcopy(current)] if current is not None else []
            self._buckets[normalized] = {
                "kind": "minute_bar",
                "source": "tencent_own_ticks",
                "code": normalized,
                "interval_start": minute,
                "interval_end": minute + timedelta(minutes=1),
                "is_closed": True,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "tick_count": 1,
            }
            return emitted
        if minute < current["interval_start"]:
            return []
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        current["tick_count"] += 1
        return []

    def close_due(
        self, now: Any, *, code: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        """Close buckets whose interval has ended, without inventing missing ticks."""

        cutoff = _parse_datetime(now)
        selected = _normalise_code(code) if code is not None else None
        due = [
            key
            for key, bar in self._buckets.items()
            if (selected is None or key == selected) and bar["interval_end"] <= cutoff
        ]
        due.sort(key=lambda key: (self._buckets[key]["interval_start"], key))
        return [deepcopy(self._buckets.pop(key)) for key in due]


class TrackingPoller:
    """Distributed-lock, bounded 15-second live-session quote poller."""

    def __init__(
        self,
        *,
        db: Any = None,
        quote_fetcher: Any,
        market_session: Any,
        max_symbols: int,
        tracking_service: Optional[DecisionTrackingService] = None,
        aggregator: Optional[MinuteBarAggregator] = None,
        benchmark_fetcher: Any = None,
        lock_seconds: int = 14,
    ) -> None:
        if max_symbols <= 0:
            raise ValueError("max_symbols_invalid")
        self._db = db
        self.quote_fetcher = quote_fetcher
        self.market_session = market_session
        self.max_symbols = max_symbols
        self.tracking_service = tracking_service or DecisionTrackingService(db=db)
        self.aggregator = aggregator or MinuteBarAggregator()
        self.benchmark_fetcher = benchmark_fetcher
        if self.benchmark_fetcher is None and hasattr(quote_fetcher, "get_quote"):
            self.benchmark_fetcher = quote_fetcher.get_quote
        self.lock_seconds = lock_seconds

    async def _get_db(self) -> Any:
        if self._db is None:
            self._db = get_mongo_db()
        if inspect.isawaitable(self._db):
            self._db = await self._db
        return self._db

    async def _symbols(self) -> list[str]:
        db = await self._get_db()
        symbols: set[str] = set()
        holdings = await db["user_holdings"].find({}).to_list(length=None)
        for holding in holdings:
            quantity = holding.get("quantity", holding.get("shares", 0))
            try:
                if _decimal(quantity, field="quantity") > 0:
                    code = _normalise_code(holding.get("code") or holding.get("stock_code"))
                    if code.isdigit() and len(code) == 6:
                        symbols.add(code)
            except ValueError:
                continue
        plans = await db["decision_plans"].find(
            {"latest_state": {"$in": ["waiting_entry", "active"]}}
        ).to_list(length=None)
        for plan in plans:
            code = _normalise_code(plan.get("code"))
            if code.isdigit() and len(code) == 6:
                symbols.add(code)
        return sorted(symbols)

    async def _fetch(self, codes: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        fetch = self.quote_fetcher.get_quotes if hasattr(self.quote_fetcher, "get_quotes") else self.quote_fetcher
        result = fetch(codes)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, Mapping) else {}

    async def _fetch_benchmark(self) -> list[Dict[str, Any]]:
        if self.benchmark_fetcher is None:
            return []
        try:
            result = self.benchmark_fetcher("sh000300")
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                return []
            trade_at = result.get("trade_at")
            price = result.get("price") or result.get("close")
            if trade_at is None or price is None:
                return []
            return [
                {
                    "at": trade_at,
                    "price": price,
                    "interval": "intraday",
                    "source": "tencent",
                    "code": "000300",
                }
            ]
        except Exception:
            return []

    async def poll_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        local_now = _parse_datetime(now or datetime.now(SHANGHAI_TIMEZONE))
        try:
            session = self.market_session.classify(now=local_now)
            if inspect.isawaitable(session):
                session = await session
        except Exception:
            return {
                "status": "disabled_session_unavailable",
                "phase": "calendar_unknown",
                "fetched": 0,
            }
        phase = str((session or {}).get("phase") or "calendar_unknown")
        if phase not in LIVE_PHASES:
            return {"status": "disabled_market_phase", "phase": phase, "fetched": 0}
        symbols = await self._symbols()
        if len(symbols) > self.max_symbols:
            return {
                "status": "disabled_symbol_limit",
                "phase": phase,
                "symbol_count": len(symbols),
                "max_symbols": self.max_symbols,
                "fetched": 0,
            }
        if not symbols:
            return {"status": "polled", "phase": phase, "symbol_count": 0, "fetched": 0}
        db = await self._get_db()
        lock_id = "decision-tracking-poller"
        owner = uuid.uuid4().hex
        existing = await db["job_locks"].find_one({"_id": lock_id})
        if existing and existing.get("owner") != owner and existing.get("lease_until") and _parse_datetime(existing["lease_until"]) > local_now:
            return {"status": "lock_unavailable", "phase": phase, "fetched": 0}
        try:
            lock = await db["job_locks"].find_one_and_update(
                {
                    "_id": lock_id,
                    "$or": [
                        {"lease_until": {"$lte": local_now}},
                        {"lease_until": {"$exists": False}},
                        {"owner": owner},
                    ],
                },
                {
                    "$set": {
                        "owner": owner,
                        "lease_until": local_now + timedelta(seconds=self.lock_seconds),
                        "updated_at": local_now,
                    },
                    "$inc": {"fence": 1},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except Exception:
            return {"status": "lock_unavailable", "phase": phase, "fetched": 0}
        if not lock or lock.get("owner") != owner:
            return {"status": "lock_unavailable", "phase": phase, "fetched": 0}
        fence = int(lock.get("fence") or 0)
        try:
            try:
                quotes = await self._fetch(symbols)
            except Exception:
                return {"status": "quote_fetch_failed", "phase": phase, "fetched": 0}
            benchmark_observations = await self._fetch_benchmark()
            owned = await db["job_locks"].find_one(
                {
                    "_id": lock_id,
                    "owner": owner,
                    "fence": fence,
                    "lease_until": {"$gt": local_now},
                }
            )
            if not owned:
                return {"status": "lock_lost", "phase": phase, "fetched": 0}
            processed = 0
            closed_bars = 0
            for code in symbols:
                raw = quotes.get(code)
                if not isinstance(raw, Mapping):
                    continue
                quote = dict(raw)
                quote.setdefault("kind", "last_trade")
                quote.setdefault("source", "tencent")
                if str(quote.get("source") or "").lower() != "tencent":
                    continue
                if "price" not in quote and "close" in quote:
                    quote["price"] = quote["close"]
                try:
                    trade_at = _parse_datetime(quote.get("trade_at"))
                except (TypeError, ValueError):
                    continue
                if trade_at.date() != local_now.date() or trade_at > local_now + timedelta(seconds=5):
                    continue
                bars = self.aggregator.close_due(local_now, code=code)
                bars.extend(self.aggregator.add(code, quote))
                plans = await db["decision_plans"].find(
                    {"code": code, "latest_state": {"$in": ["waiting_entry", "active"]}}
                ).to_list(length=None)
                for bar in bars:
                    await db["decision_minute_bars"].update_one(
                        {
                            "code": code,
                            "interval_start": bar["interval_start"],
                            "source": "tencent_own_ticks",
                        },
                        {"$setOnInsert": deepcopy(bar)},
                        upsert=True,
                    )
                    for plan in plans:
                        await self.tracking_service.observe(
                            plan["plan_id"],
                            bar,
                            benchmark_observations=benchmark_observations,
                        )
                    closed_bars += 1
                plans = await db["decision_plans"].find(
                    {"code": code, "latest_state": {"$in": ["waiting_entry", "active"]}}
                ).to_list(length=None)
                for plan in plans:
                    await self.tracking_service.observe(
                        plan["plan_id"],
                        quote,
                        benchmark_observations=benchmark_observations,
                    )
                processed += 1
            return {
                "status": "polled",
                "phase": phase,
                "symbol_count": len(symbols),
                "fetched": processed,
                "closed_bars": closed_bars,
                "poll_interval_seconds": 15,
            }
        finally:
            await db["job_locks"].update_one(
                {"_id": lock_id, "owner": owner, "fence": fence},
                {"$set": {"lease_until": local_now, "released_at": local_now}},
            )


__all__ = [
    "DEFAULT_CN_A_FEE_POLICY",
    "DecisionTrackingService",
    "METRIC_BASIS",
    "MinuteBarAggregator",
    "ObservationConflictError",
    "TrackingPoller",
    "calculate_trade_metrics",
    "stable_plan_id_from_item",
]
