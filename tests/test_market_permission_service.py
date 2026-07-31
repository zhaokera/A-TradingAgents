from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.market_permission_service import MarketPermissionService


@pytest.mark.asyncio
async def test_update_permission_is_owner_scoped_and_atomically_audited():
    before = {
        "user_id": "owner-1",
        "execution_capabilities": {
            "source": "user_confirmed",
            "market_permissions": {
                "star_market": {
                    "verified": True,
                    "tradable": False,
                }
            },
        },
    }
    after = deepcopy(before)
    after["execution_capabilities"]["market_permissions"][
        "chi_next_market"
    ] = {
        "verified": True,
        "tradable": False,
        "source": "user_confirmed",
        "updated_by": "owner-1",
        "updated_at": "2026-07-31T09:00:00+08:00",
    }
    collection = SimpleNamespace(
        find_one=AsyncMock(side_effect=[before, after]),
        update_one=AsyncMock(),
    )
    service = MarketPermissionService(
        db=SimpleNamespace(user_holding_settings=collection),
        now=lambda: "2026-07-31T09:00:00+08:00",
        audit_id_factory=lambda: "permission-audit-1",
    )

    result = await service.update(
        "owner-1",
        username="admin",
        permission_key="chi_next_market",
        state="denied",
    )

    first_filter = collection.find_one.await_args_list[0].args[0]
    assert first_filter == {"user_id": "owner-1"}
    update_filter, update_document = collection.update_one.await_args.args
    assert update_filter == {"user_id": "owner-1"}
    stored = update_document["$set"][
        "execution_capabilities.market_permissions.chi_next_market"
    ]
    assert stored == {
        "verified": True,
        "tradable": False,
        "source": "user_confirmed",
        "updated_by": "owner-1",
        "updated_at": "2026-07-31T09:00:00+08:00",
    }
    audit = update_document["$push"][
        "execution_capabilities.market_permission_history"
    ]["$each"][0]
    assert audit["audit_id"] == "permission-audit-1"
    assert audit["permission_key"] == "chi_next_market"
    assert audit["state"] == "denied"
    assert audit["actor_user_id"] == "owner-1"
    assert audit["source"] == "user_confirmed"
    assert update_document["$push"][
        "execution_capabilities.market_permission_history"
    ]["$slice"] == -100
    assert result["updated_permission"]["eligible"] is False
    assert result["updated_permission"]["reason_code"] == "permission_denied"
    assert result["updated_permission"]["source"] == "user_confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "verified", "tradable", "reason_code"),
    [
        ("allowed", True, True, None),
        ("denied", True, False, "permission_denied"),
        ("unverified", False, False, "permission_unverified"),
    ],
)
async def test_permission_update_supports_all_three_states(
    state,
    verified,
    tradable,
    reason_code,
):
    collection = SimpleNamespace(
        find_one=AsyncMock(
            side_effect=[
                None,
                {
                    "user_id": "owner-1",
                    "execution_capabilities": {
                        "market_permissions": {
                            "star_market": {
                                "verified": verified,
                                "tradable": tradable,
                                "source": "user_confirmed",
                            }
                        }
                    },
                },
            ]
        ),
        update_one=AsyncMock(),
    )
    service = MarketPermissionService(
        db=SimpleNamespace(user_holding_settings=collection),
        now=lambda: "2026-07-31T09:00:00+08:00",
    )

    result = await service.update(
        "owner-1",
        username="admin",
        permission_key="star_market",
        state=state,
    )

    permission = result["updated_permission"]
    assert permission["verified"] is verified
    assert permission["tradable"] is tradable
    assert permission["eligible"] is (verified and tradable)
    assert permission["reason_code"] == reason_code


@pytest.mark.asyncio
async def test_unknown_permission_key_is_rejected_before_database_write():
    collection = SimpleNamespace(
        find_one=AsyncMock(),
        update_one=AsyncMock(),
    )
    service = MarketPermissionService(
        db=SimpleNamespace(user_holding_settings=collection)
    )

    with pytest.raises(ValueError, match="unsupported market permission"):
        await service.update(
            "owner-1",
            username="admin",
            permission_key="main_board",
            state="denied",
        )

    collection.update_one.assert_not_awaited()
