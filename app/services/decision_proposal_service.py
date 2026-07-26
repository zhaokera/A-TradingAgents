"""Persist structured Codex proposals and run their initial hard validation."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.models.decision import CodexDecisionProposalInput
from app.services.decision_research_service import decision_research_service
from app.services.decision_validation_service import decision_validation_service
from app.services.decision_workflow_errors import DecisionWorkflowError


def _serialize(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = deepcopy(dict(value))
    result.pop("_id", None)
    return result


def _proposal_hash(
    user_id: str,
    research_packet_id: str,
    payload: Mapping[str, Any],
) -> str:
    encoded = json.dumps(
        {
            "user_id": user_id,
            "research_packet_id": research_packet_id,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DecisionProposalService:
    """Store immutable Codex proposals without calling an LLM."""

    def __init__(
        self,
        *,
        db: Any = None,
        research_service: Any = None,
        validator: Any = None,
    ) -> None:
        self.db = db
        self.research_service = research_service or decision_research_service
        self.validator = validator or decision_validation_service

    async def _get_db(self) -> Any:
        if self.db is None:
            self.db = get_mongo_db()
        if inspect.isawaitable(self.db):
            self.db = await self.db
        return self.db

    @staticmethod
    def _normalize(proposal: Any) -> CodexDecisionProposalInput:
        if isinstance(proposal, CodexDecisionProposalInput):
            return proposal
        try:
            return CodexDecisionProposalInput.model_validate(proposal)
        except ValidationError as exc:
            raise DecisionWorkflowError(
                "decision_proposal_invalid",
                "Codex 提案不符合结构契约",
                status_code=422,
                details={"errors": exc.errors(include_url=False)},
            ) from exc

    async def submit(
        self,
        user_id: str,
        proposal: Any,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        owner = str(user_id or "").strip()
        if not owner:
            raise DecisionWorkflowError(
                "user_required", "user_id is required", status_code=401
            )
        model = self._normalize(proposal)
        payload = model.model_dump(mode="json")
        packet = await self.research_service.get(owner, model.research_packet_id)
        digest = _proposal_hash(owner, model.research_packet_id, payload)
        db = await self._get_db()
        collection = db["codex_decision_proposals"]
        query = {"user_id": owner, "proposal_hash": digest}
        existing = await collection.find_one(query)
        if existing:
            document = _serialize(existing)
        else:
            created_at = (now or datetime.now(timezone.utc)).isoformat()
            document = {
                "proposal_id": f"proposal_{uuid.uuid4().hex}",
                "user_id": owner,
                "research_packet_id": model.research_packet_id,
                "proposal_schema_version": model.proposal_schema_version,
                "prompt_version": model.prompt_version,
                "proposal_hash": digest,
                "payload": payload,
                "created_at": created_at,
                "source": "codex",
                "status": "submitted",
            }
            try:
                await collection.insert_one(deepcopy(document))
            except DuplicateKeyError:
                winner = await collection.find_one(query)
                if not winner:
                    raise
                document = _serialize(winner)

        validation = await self.validator.validate_document(
            owner,
            document,
            packet,
            now=now,
            proposal_id=document["proposal_id"],
        )
        validation = await self.validator.persist(validation)
        return {
            "proposal": _serialize(document),
            "validation": _serialize(validation),
        }

    async def get(self, user_id: str, proposal_id: str) -> Dict[str, Any]:
        db = await self._get_db()
        row = await db["codex_decision_proposals"].find_one(
            {
                "user_id": str(user_id),
                "proposal_id": str(proposal_id),
            }
        )
        if not row:
            raise DecisionWorkflowError(
                "decision_proposal_not_found",
                "Codex 提案不存在或不属于当前用户",
                status_code=404,
                details={"proposal_id": str(proposal_id)},
            )
        return _serialize(row)

    async def latest(
        self,
        user_id: str,
        *,
        research_packet_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        query: Dict[str, Any] = {"user_id": str(user_id)}
        if research_packet_id:
            query["research_packet_id"] = str(research_packet_id)
        db = await self._get_db()
        row = await db["codex_decision_proposals"].find_one(
            query,
            sort=[("created_at", -1)],
        )
        return _serialize(row) if row else None


decision_proposal_service = DecisionProposalService()
