"""Explicit user confirmation and composite decision-workspace reads."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_mongo_db
from app.models.decision import DecisionConfirmationInput
from app.services.daily_decision_service import daily_decision_service
from app.services.decision_proposal_service import decision_proposal_service
from app.services.decision_research_service import decision_research_service
from app.services.decision_validation_service import decision_validation_service
from app.services.decision_workflow_errors import DecisionWorkflowError


def _serialize(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    result = deepcopy(dict(value))
    result.pop("_id", None)
    return result


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _confirmation_hash(
    user_id: str,
    proposal_id: str,
    payload: DecisionConfirmationInput,
) -> str:
    encoded = json.dumps(
        {
            "user_id": user_id,
            "proposal_id": proposal_id,
            "payload": payload.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DecisionConfirmationService:
    """Record a human decision without placing an order."""

    def __init__(
        self,
        *,
        db: Any = None,
        proposal_service: Any = None,
        validation_service: Any = None,
        research_service: Any = None,
        baseline_service: Any = None,
        authority_mode: Optional[str] = None,
    ) -> None:
        self.db = db
        self.proposal_service = proposal_service or decision_proposal_service
        self.validation_service = validation_service or decision_validation_service
        self.research_service = research_service or decision_research_service
        self.baseline_service = baseline_service or daily_decision_service
        self.authority_mode = str(
            authority_mode
            if authority_mode is not None
            else getattr(settings, "DECISION_AUTHORITY_MODE", "software_baseline")
        )

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        if inspect.isawaitable(self.db):
            self.db = await self.db
        return self.db

    async def confirm(
        self,
        user_id: str,
        proposal_id: str,
        payload: DecisionConfirmationInput,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        normalized_proposal_id = str(proposal_id or "").strip()
        await self.proposal_service.get(owner, normalized_proposal_id)
        validation = await self.validation_service.get(
            owner, payload.validation_id
        )
        if validation.get("proposal_id") != normalized_proposal_id:
            raise DecisionWorkflowError(
                "decision_validation_mismatch",
                "校验结果不属于该提案",
                status_code=409,
            )
        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        if payload.accepted:
            if validation.get("status") != "valid":
                raise DecisionWorkflowError(
                    "decision_validation_not_confirmable",
                    "只有校验通过的提案可以确认接受",
                    status_code=409,
                    details={"validation_status": validation.get("status")},
                )
            valid_until = _parse_datetime(validation.get("valid_until"))
            if valid_until is not None and valid_until <= effective_now:
                raise DecisionWorkflowError(
                    "decision_validation_expired",
                    "校验结果已过期，请重新校验",
                    status_code=409,
                )

        digest = _confirmation_hash(owner, normalized_proposal_id, payload)
        db = await self._get_db()
        collection = db["decision_confirmations"]
        existing = await collection.find_one(
            {"user_id": owner, "confirmation_hash": digest}
        )
        if existing:
            return _serialize(existing) or {}
        document = {
            "confirmation_id": f"confirmation_{uuid.uuid4().hex}",
            "user_id": owner,
            "proposal_id": normalized_proposal_id,
            "validation_id": payload.validation_id,
            "accepted": payload.accepted,
            "reason": payload.reason,
            "confirmed_at": effective_now.isoformat(),
            "confirmation_hash": digest,
            "execution_status": "not_executed",
            "disclaimer": "仅供研究和参考，不构成投资建议或交易指令。",
        }
        try:
            await collection.insert_one(deepcopy(document))
        except DuplicateKeyError:
            winner = await collection.find_one(
                {"user_id": owner, "confirmation_hash": digest}
            )
            if not winner:
                raise
            return _serialize(winner) or {}
        return document

    async def latest_confirmation(
        self, user_id: str, proposal_id: str
    ) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        row = await db["decision_confirmations"].find_one(
            {
                "user_id": str(user_id),
                "proposal_id": str(proposal_id),
            },
            sort=[("confirmed_at", -1)],
        )
        return _serialize(row)

    async def _latest_validation(
        self, user_id: str, proposal_id: str
    ) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        row = await db["decision_validations"].find_one(
            {
                "user_id": str(user_id),
                "proposal_id": str(proposal_id),
            },
            sort=[("validated_at", -1)],
        )
        return _serialize(row)

    async def workspace(
        self,
        user_id: str,
        *,
        refresh: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        research = (
            await self.research_service.today(owner, refresh=True, now=now)
            if refresh
            else await self.research_service.latest(owner)
        )
        if research is None:
            research = await self.research_service.today(
                owner, refresh=False, now=now
            )
        baseline = self.baseline_service.today(
            owner,
            refresh=False,
            now=now,
        )
        if inspect.isawaitable(baseline):
            baseline = await baseline
        baseline = _serialize(baseline) or {}
        baseline.setdefault("authority", "software_baseline")
        baseline.setdefault("is_final_decision", False)
        baseline_id = str(baseline.get("decision_id") or "")
        research_baseline = research.get("software_baseline")
        research_baseline = (
            research_baseline if isinstance(research_baseline, Mapping) else {}
        )
        research_baseline_id = str(
            research.get("source_baseline_id")
            or research_baseline.get("baseline_id")
            or ""
        )
        if baseline_id and research_baseline_id != baseline_id:
            research = await self.research_service.today(
                owner,
                refresh=False,
                now=now,
            )

        proposal = await self.proposal_service.latest(
            owner,
            research_packet_id=str(research.get("research_packet_id") or ""),
        )
        validation = (
            await self._latest_validation(owner, proposal["proposal_id"])
            if proposal
            else None
        )
        confirmation = (
            await self.latest_confirmation(owner, proposal["proposal_id"])
            if proposal
            else None
        )
        valid_codex = bool(validation and validation.get("status") == "valid")
        codex_is_primary = bool(
            self.authority_mode == "codex_validated" and valid_codex
        )
        confirmed = bool(confirmation and confirmation.get("accepted") is True)
        return {
            "authority_mode": self.authority_mode,
            "authority": (
                "codex_validated" if codex_is_primary else "software_baseline"
            ),
            "is_final_decision": codex_is_primary,
            "requires_user_confirmation": bool(codex_is_primary and not confirmed),
            "is_confirmed": confirmed,
            "research_packet": deepcopy(research),
            "software_baseline": baseline,
            "codex_proposal": proposal,
            "validation": validation,
            "confirmation": confirmation,
            "primary_decision": proposal if codex_is_primary else None,
            "disclaimer": "仅供研究和参考，不构成投资建议或交易指令；系统不会自动下单。",
        }


decision_confirmation_service = DecisionConfirmationService()
