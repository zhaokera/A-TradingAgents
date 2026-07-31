"""Authenticated account market-permission settings and audit history."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from app.core.database import get_mongo_db
from app.services.a_share_permissions import (
    BSE_PERMISSION_KEY,
    CHINEXT_PERMISSION_KEY,
    STAR_PERMISSION_KEY,
    normalize_market_permissions,
)
from app.utils.timezone import now_tz


MARKET_PERMISSION_KEYS = (
    STAR_PERMISSION_KEY,
    CHINEXT_PERMISSION_KEY,
    BSE_PERMISSION_KEY,
)
MARKET_PERMISSION_STATES = ("allowed", "denied", "unverified")

_STATE_VALUES = {
    "allowed": {"verified": True, "tradable": True},
    "denied": {"verified": True, "tradable": False},
    "unverified": {"verified": False, "tradable": False},
}


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class MarketPermissionService:
    """Read and update only the authenticated user's market permissions."""

    def __init__(
        self,
        *,
        db: Any = None,
        now: Optional[Callable[[], Any]] = None,
        audit_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self.db = db
        self._now = now or now_tz
        self._audit_id_factory = audit_id_factory or (
            lambda: f"permission_{uuid.uuid4().hex}"
        )

    def _db(self) -> Any:
        return self.db if self.db is not None else get_mongo_db()

    @staticmethod
    def _collection(db: Any) -> Any:
        try:
            return db["user_holding_settings"]
        except (TypeError, KeyError):
            return db.user_holding_settings

    @staticmethod
    def _capabilities(settings: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        raw = (
            settings.get("execution_capabilities")
            if isinstance(settings, Mapping)
            else {}
        )
        return deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}

    async def get(self, user_id: str) -> Dict[str, Any]:
        collection = self._collection(self._db())
        settings = await collection.find_one({"user_id": str(user_id)})
        capabilities = self._capabilities(settings)
        history = capabilities.get("market_permission_history")
        history = history if isinstance(history, list) else []
        return {
            "source": str(capabilities.get("source") or "unverified"),
            "market_permissions": normalize_market_permissions(
                capabilities.get("market_permissions")
            ),
            "history": deepcopy(history[-20:]),
        }

    async def update(
        self,
        user_id: str,
        *,
        username: str,
        permission_key: str,
        state: str,
    ) -> Dict[str, Any]:
        if permission_key not in MARKET_PERMISSION_KEYS:
            raise ValueError("unsupported market permission")
        if state not in MARKET_PERMISSION_STATES:
            raise ValueError("unsupported market permission state")

        owner_id = str(user_id)
        collection = self._collection(self._db())
        before_doc = await collection.find_one({"user_id": owner_id})
        before_capabilities = self._capabilities(before_doc)
        before_permissions = normalize_market_permissions(
            before_capabilities.get("market_permissions")
        )
        timestamp = _iso_timestamp(self._now())
        stored_permission = {
            **_STATE_VALUES[state],
            "source": "user_confirmed",
            "updated_by": owner_id,
            "updated_at": timestamp,
        }
        audit = {
            "audit_id": self._audit_id_factory(),
            "permission_key": permission_key,
            "state": state,
            "before": deepcopy(before_permissions[permission_key]),
            "after": deepcopy(stored_permission),
            "actor_user_id": owner_id,
            "actor_username": str(username or ""),
            "source": "user_confirmed",
            "changed_at": timestamp,
        }
        await collection.update_one(
            {"user_id": owner_id},
            {
                "$set": {
                    "user_id": owner_id,
                    "execution_capabilities.source": "user_confirmed",
                    (
                        "execution_capabilities.market_permissions."
                        f"{permission_key}"
                    ): stored_permission,
                    "execution_capabilities.updated_at": timestamp,
                    "updated_at": timestamp,
                },
                "$push": {
                    "execution_capabilities.market_permission_history": {
                        "$each": [audit],
                        "$slice": -100,
                    }
                },
            },
            upsert=True,
        )
        result = await self.get(owner_id)
        result["updated_permission"] = {
            "permission_key": permission_key,
            **deepcopy(result["market_permissions"][permission_key]),
        }
        result["audit"] = audit
        return result


market_permission_service = MarketPermissionService()
