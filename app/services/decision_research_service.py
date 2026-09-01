"""Build immutable fact packets for Codex without granting software final authority."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_mongo_db
from app.services.daily_decision_service import daily_decision_service
from app.services.decision_workflow_errors import DecisionWorkflowError
from app.services.investment_policy import calculate_candidate_position_sizing


RESEARCH_SCHEMA_VERSION = "research-v2"
HARD_POLICY_VERSION = "codex-hard-risk-v1"
BUCKETS = ("buy_now", "condition_order", "wait", "avoid")

SOFT_WARNING_CODES = frozenset(
    {
        "market_red",
        "objective_mismatch",
        "profile_incomplete",
        "external_risk_unverified",
        "earnings_risk",
        "valuation_high",
        "technical_weakness",
    }
)
STATUS_REASON_CODES = frozenset(
    {
        "live_price_condition_met",
        "valid_allocated_plan",
        "live_quote_recheck_required",
        "entry_condition_not_met",
        "waiting_entry",
    }
)
HARD_REASON_CODES = frozenset(
    {
        "plan_invalidated",
        "plan_expired",
        "target_reached",
        "blocking_event",
        "hard_data_failure",
        "account_blocked",
        "one_lot_unaffordable",
        "holding_valuation_missing",
        "holding_taxonomy_missing",
        "candidate_taxonomy_missing",
        "formal_research_required",
        "concentration_limit",
        "correlation_limit",
        "loss_budget_exhausted",
        "invalid_portfolio_policy",
        "price_plan_or_account_unavailable",
        "candidate_code_invalid",
    }
)
ACTION_SCOPED_HARD_CODES = {
    "calendar_unknown": ("buy_now",),
    "pullback_reversal_confirmation_required": ("buy_now",),
    "condition_order_capability_unverified": ("condition_order",),
    "condition_order_order_price_missing": ("condition_order",),
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _serialize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _serialize(item)
            for key, item in value.items()
            if str(key) != "_id"
        }
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    excluded = {"research_packet_id", "created_at", "persisted_at"}
    canonical = {
        key: item for key, item in value.items() if str(key) not in excluded
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _market_is_red(market: Mapping[str, Any]) -> bool:
    return any(
        str(market.get(field) or "").strip().lower() == "red"
        for field in ("combined_regime", "domestic_regime", "regime")
    )


def _reason_record(
    reason: str,
    *,
    market_red_blocks_new_positions: bool,
) -> tuple[str, Optional[Dict[str, Any]]]:
    code = str(reason or "").strip()
    if not code or code in STATUS_REASON_CODES:
        return "status", None
    if code == "market_red":
        if market_red_blocks_new_positions:
            return (
                "hard",
                {
                    "code": code,
                    "overrideable": False,
                    "applies_to": ["buy_now", "condition_order"],
                    "source": "software_baseline",
                },
            )
        return (
            "soft",
            {
                "code": code,
                "severity": "warning",
                "overrideable": True,
                "source": "software_baseline",
            },
        )
    if code in SOFT_WARNING_CODES:
        return (
            "soft",
            {
                "code": code,
                "severity": "warning",
                "overrideable": True,
                "source": "software_baseline",
            },
        )
    if code in ACTION_SCOPED_HARD_CODES:
        return (
            "hard",
            {
                "code": code,
                "overrideable": False,
                "applies_to": list(ACTION_SCOPED_HARD_CODES[code]),
                "source": "software_baseline",
            },
        )
    if code in HARD_REASON_CODES:
        return (
            "hard",
            {
                "code": code,
                "overrideable": False,
                "applies_to": ["buy_now", "condition_order"],
                "source": "software_baseline",
            },
        )
    return (
        "hard",
        {
            "code": "unclassified_gate",
            "overrideable": False,
            "applies_to": ["buy_now", "condition_order"],
            "source": "software_baseline",
            "details": {"original_code": code},
        },
    )


def _dedupe_records(records: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in records:
        record = deepcopy(dict(raw))
        key = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _candidate_evidence(code: str, item: Mapping[str, Any]) -> list[Dict[str, Any]]:
    evidence: list[Dict[str, Any]] = []

    def add(evidence_id: str, kind: str, value: Any) -> None:
        if value in (None, {}, []):
            return
        evidence.append(
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "value": deepcopy(value),
            }
        )

    quote = item.get("quote")
    add(f"{code}:quote", "quote", quote if isinstance(quote, Mapping) else {})
    plans = item.get("plans")
    plans = plans if isinstance(plans, Mapping) else {}
    add(f"{code}:plan:short", "price_plan", plans.get("short") or {})
    allocation = item.get("allocation")
    add(
        f"{code}:allocation",
        "allocation",
        allocation if isinstance(allocation, Mapping) else {},
    )

    profile = item.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    for field in (
        "provider_sector_evidence",
        "industry_evidence",
        "main_business_evidence",
        "revenue_composition",
    ):
        value = profile.get(field)
        if isinstance(value, Mapping) and value:
            add(f"{code}:profile:{field}", "profile", value)
    return evidence


class DecisionResearchService:
    """Convert one software baseline into an immutable Codex research packet."""

    def __init__(
        self,
        *,
        baseline_service: Any = None,
        db: Any = None,
        market_red_blocks_new_positions: Optional[bool] = None,
        max_new_positions: Optional[int] = None,
        primary_position_count: Optional[int] = None,
    ) -> None:
        self.baseline_service = baseline_service or daily_decision_service
        self.db = db
        self.market_red_blocks_new_positions = bool(
            getattr(settings, "MARKET_RED_BLOCKS_NEW_POSITIONS", False)
            if market_red_blocks_new_positions is None
            else market_red_blocks_new_positions
        )
        self.max_new_positions = int(
            max_new_positions
            if max_new_positions is not None
            else getattr(settings, "CODEX_DECISION_MAX_NEW_POSITIONS", 2)
        )
        self.primary_position_count = int(
            primary_position_count
            if primary_position_count is not None
            else getattr(settings, "CODEX_DECISION_PRIMARY_POSITION_COUNT", 1)
        )

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        if inspect.isawaitable(self.db):
            self.db = await self.db
        return self.db

    @staticmethod
    def _hard_policy(baseline: Mapping[str, Any], *, block_red: bool) -> Dict[str, Any]:
        account = baseline.get("account")
        account = account if isinstance(account, Mapping) else {}
        policy = baseline.get("effective_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        market = baseline.get("market")
        market = market if isinstance(market, Mapping) else {}

        assets = max(0.0, _finite(account.get("total_assets")))
        cash = max(0.0, _finite(account.get("available_cash")))
        current_exposure = max(0.0, _finite(account.get("current_exposure_pct")))
        hard_new_cap = max(
            0.0,
            _finite(
                policy.get("codex_new_exposure_cap_pct"),
                _finite(policy.get("green_new_exposure_cap_pct"), 60.0),
            ),
        )
        if block_red and _market_is_red(market):
            hard_new_cap = 0.0
        cash_cap = cash / assets * 100 if assets > 0 else 0.0
        available_exposure = max(
            0.0,
            min(max(0.0, hard_new_cap - current_exposure), cash_cap),
        )
        hard_symbol_cap = max(
            0.0, _finite(policy.get("hard_single_symbol_cap_pct"), 40.0)
        )
        return {
            **deepcopy(dict(policy)),
            "policy_version": HARD_POLICY_VERSION,
            "market_red_blocks_new_positions": bool(block_red),
            "new_exposure_cap_pct": round(hard_new_cap, 2),
            "available_new_exposure_pct": round(available_exposure, 2),
            "preferred_single_symbol_pct": round(hard_symbol_cap, 2),
            "hard_single_symbol_cap_pct": round(hard_symbol_cap, 2),
            "per_position_loss_budget_pct": round(
                max(
                    0.0,
                    _finite(policy.get("per_position_loss_budget_pct"), 1.0),
                ),
                2,
            ),
            "total_new_position_loss_budget_pct": round(
                max(
                    0.0,
                    _finite(
                        policy.get("total_new_position_loss_budget_pct"), 2.0
                    ),
                ),
                2,
            ),
        }

    @staticmethod
    def _risk_envelope(
        item: Mapping[str, Any],
        *,
        account: Mapping[str, Any],
        hard_policy: Mapping[str, Any],
    ) -> Dict[str, Any]:
        plans = item.get("plans")
        plans = plans if isinstance(plans, Mapping) else {}
        short = plans.get("short")
        short = short if isinstance(short, Mapping) else {}
        impact = item.get("portfolio_impact")
        impact = impact if isinstance(impact, Mapping) else {}
        symbol = impact.get("symbol_exposure")
        symbol = symbol if isinstance(symbol, Mapping) else {}
        sizing = calculate_candidate_position_sizing(
            entry_price=short.get("entry_price"),
            stop_price=short.get("stop_price"),
            total_assets=account.get("total_assets"),
            available_cash=account.get("available_cash"),
            current_symbol_value=symbol.get("before_amount") or 0,
            policy=dict(hard_policy),
        )
        max_quantity = (
            int(sizing.get("suggested_quantity") or 0)
            if sizing.get("status") == "sized"
            else 0
        )
        return {
            "status": sizing.get("status"),
            "reason": sizing.get("reason"),
            "lot_size": int(sizing.get("lot_size") or 100),
            "max_allowed_quantity": max_quantity,
            "max_allowed_amount": sizing.get("suggested_amount") or 0.0,
            "max_position_pct": sizing.get("suggested_position_pct") or 0.0,
            "max_planned_loss_amount": sizing.get("planned_loss_amount") or 0.0,
            "max_planned_loss_pct_of_assets": sizing.get(
                "planned_loss_pct_of_assets"
            )
            or 0.0,
            "calculation": deepcopy(sizing),
        }

    def _build_packet(
        self, user_id: str, baseline: Mapping[str, Any]
    ) -> Dict[str, Any]:
        owner = str(baseline.get("user_id") or user_id)
        if owner != user_id:
            raise DecisionWorkflowError(
                "baseline_owner_mismatch",
                "软件基线不属于当前用户",
                status_code=403,
            )
        account = baseline.get("account")
        account = deepcopy(dict(account)) if isinstance(account, Mapping) else {}
        hard_policy = self._hard_policy(
            baseline,
            block_red=self.market_red_blocks_new_positions,
        )
        candidates: list[Dict[str, Any]] = []
        packet_hard: list[Dict[str, Any]] = []
        packet_soft: list[Dict[str, Any]] = []
        unclassified: list[str] = []

        for bucket in BUCKETS:
            rows = baseline.get(bucket)
            rows = rows if isinstance(rows, list) else []
            for raw in rows:
                if not isinstance(raw, Mapping):
                    continue
                item = deepcopy(dict(raw))
                identity = item.get("identity")
                identity = identity if isinstance(identity, Mapping) else {}
                code = str(identity.get("code") or "").strip()
                hard_constraints: list[Dict[str, Any]] = []
                soft_warnings: list[Dict[str, Any]] = []
                for reason in item.get("reason_codes") or []:
                    kind, record = _reason_record(
                        str(reason),
                        market_red_blocks_new_positions=(
                            self.market_red_blocks_new_positions
                        ),
                    )
                    if not record:
                        continue
                    if kind == "hard":
                        hard_constraints.append(record)
                        if record["code"] == "unclassified_gate":
                            unclassified.append(
                                str((record.get("details") or {}).get("original_code"))
                            )
                    elif kind == "soft":
                        soft_warnings.append(record)
                hard_constraints = _dedupe_records(hard_constraints)
                soft_warnings = _dedupe_records(soft_warnings)
                packet_hard.extend(
                    {**record, "symbol": code} for record in hard_constraints
                )
                packet_soft.extend(
                    {**record, "symbol": code} for record in soft_warnings
                )
                profile = item.get("profile")
                profile = profile if isinstance(profile, Mapping) else {}
                candidates.append(
                    {
                        "symbol": code,
                        "name": identity.get("name") or code,
                        "identity": deepcopy(dict(identity)),
                        "software_baseline_action": bucket,
                        "software_reason_codes": list(
                            item.get("reason_codes") or []
                        ),
                        "quote": deepcopy(item.get("quote") or {}),
                        "plans": deepcopy(item.get("plans") or {}),
                        "profile": deepcopy(dict(profile)),
                        "candidate_source_profile": deepcopy(
                            item.get("candidate_source_profile") or {}
                        ),
                        "resolved_profile": deepcopy(
                            item.get("resolved_profile") or profile
                        ),
                        "profile_contract": deepcopy(
                            item.get("profile_contract") or {}
                        ),
                        "candidate_reason_summary": str(
                            item.get("candidate_reason_summary") or ""
                        ),
                        "allocation": deepcopy(item.get("allocation") or {}),
                        "portfolio_impact": deepcopy(
                            item.get("portfolio_impact") or {}
                        ),
                        "planned_loss": deepcopy(item.get("planned_loss") or {}),
                        "invalidation": deepcopy(item.get("invalidation") or {}),
                        "plan_id": item.get("plan_id"),
                        "hard_constraints": hard_constraints,
                        "soft_warnings": soft_warnings,
                        "risk_envelope": self._risk_envelope(
                            item,
                            account=account,
                            hard_policy=hard_policy,
                        ),
                        "evidence": _candidate_evidence(code, item),
                    }
                )

        baseline_id = str(baseline.get("decision_id") or "")
        if not baseline_id:
            raise DecisionWorkflowError(
                "baseline_invalid",
                "软件基线缺少 decision_id",
                status_code=503,
            )
        created_at = datetime.now(timezone.utc).isoformat()
        packet: Dict[str, Any] = {
            "user_id": user_id,
            "source_baseline_id": baseline_id,
            "source_baseline_material_hash": baseline.get("material_hash"),
            "candidate_run_id": baseline.get("candidate_run_id"),
            "candidate_research": deepcopy(
                baseline.get("candidate_research") or {}
            ),
            "as_of": baseline.get("as_of"),
            "created_at": created_at,
            "market_session": deepcopy(baseline.get("market_session") or {}),
            "account": account,
            "execution_capabilities": deepcopy(
                baseline.get("execution_capabilities") or {}
            ),
            "market": deepcopy(baseline.get("market") or {}),
            "decision_objective": {
                "max_new_positions": self.max_new_positions,
                "primary_position_count": self.primary_position_count,
            },
            "hard_risk_policy": hard_policy,
            "hard_constraints": _dedupe_records(packet_hard),
            "soft_warnings": _dedupe_records(packet_soft),
            "candidates": candidates,
            "rolling_pool": deepcopy(baseline.get("rolling_pool") or {}),
            "daily_structured_analysis": deepcopy(
                baseline.get("daily_structured_analysis") or {}
            ),
            "portfolio_constraints": deepcopy(
                baseline.get("portfolio_constraints") or {}
            ),
            "data_quality": {
                **deepcopy(dict(baseline.get("data_quality") or {})),
                "unclassified_reason_codes": sorted(
                    {code for code in unclassified if code}
                ),
            },
            "software_baseline": {
                "baseline_id": baseline_id,
                "authority": "software_baseline",
                "is_final_decision": False,
                "market_phase": baseline.get("market_phase"),
                "revision": baseline.get("revision"),
                "summary": deepcopy(baseline.get("summary") or {}),
                "rule_version": baseline.get("rule_version"),
            },
            "versions": {
                "research_schema": RESEARCH_SCHEMA_VERSION,
                "hard_policy": HARD_POLICY_VERSION,
                "software_rule": baseline.get("rule_version"),
            },
            "disclaimer": "仅供研究和参考，不构成投资建议或交易指令。",
        }
        packet["material_hash"] = _canonical_hash(packet)
        packet["research_packet_id"] = "research_" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{user_id}:{baseline_id}:{packet['material_hash']}",
        ).hex
        return packet

    async def _persist(self, packet: Mapping[str, Any]) -> Dict[str, Any]:
        db = await self._get_db()
        collection = db["decision_research_packets"]
        query = {
            "user_id": packet["user_id"],
            "source_baseline_id": packet["source_baseline_id"],
        }
        existing = await collection.find_one(query)
        if existing:
            return _serialize(existing)
        document = deepcopy(dict(packet))
        document["persisted_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await collection.insert_one(document)
        except DuplicateKeyError:
            existing = await collection.find_one(query)
            if existing:
                return _serialize(existing)
            raise
        return _serialize(document)

    async def today(
        self,
        user_id: str,
        *,
        refresh: bool = True,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        if not owner:
            raise DecisionWorkflowError(
                "user_required", "user_id is required", status_code=401
            )
        baseline = self.baseline_service.today(owner, refresh=refresh, now=now)
        if inspect.isawaitable(baseline):
            baseline = await baseline
        if not isinstance(baseline, Mapping):
            raise DecisionWorkflowError(
                "baseline_unavailable",
                "无法生成软件基线",
                status_code=503,
            )
        return await self._persist(self._build_packet(owner, baseline))

    async def get(self, user_id: str, research_packet_id: str) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        packet_id = str(research_packet_id or "").strip()
        db = await self._get_db()
        row = await db["decision_research_packets"].find_one(
            {"user_id": owner, "research_packet_id": packet_id}
        )
        if not row:
            raise DecisionWorkflowError(
                "research_packet_not_found",
                "研究包不存在或不属于当前用户",
                status_code=404,
                details={"research_packet_id": packet_id},
            )
        return _serialize(row)

    async def latest(self, user_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        row = await db["decision_research_packets"].find_one(
            {"user_id": str(user_id)},
            sort=[("created_at", -1)],
        )
        return _serialize(row) if row else None


decision_research_service = DecisionResearchService()
