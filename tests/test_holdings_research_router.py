from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.routers import holdings
from app.services.holdings_cli import CLIError


def _payload(kind: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": {"kind": kind},
        "meta": {"schema_version": 1, "source": "test"},
    }


@pytest.mark.asyncio
async def test_market_status_runs_in_backend_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        holdings,
        "_run_legacy_research_builder",
        lambda builder_name, **_kwargs: _payload(builder_name),
    )

    result = await holdings.get_holding_market_status({"id": "user-1"})

    assert result["success"] is True
    assert result["data"] == {
        "kind": "market_status",
        "meta": {"schema_version": 1, "source": "test"},
    }


@pytest.mark.asyncio
async def test_earnings_and_notices_forward_validated_request_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Dict[str, Any]]] = []

    def fake_builder(builder_name: str, **kwargs: Any) -> Dict[str, Any]:
        calls.append((builder_name, kwargs))
        return _payload(builder_name)

    monkeypatch.setattr(holdings, "_run_legacy_research_builder", fake_builder)

    earnings = await holdings.review_holding_earnings(
        holdings.HoldingResearchRequest(codes=["600406"]),
        {"id": "user-1"},
    )
    notices = await holdings.review_holding_notices(
        holdings.HoldingNoticeResearchRequest(
            codes=["600406"],
            lookback_days=14,
        ),
        {"id": "user-1"},
    )

    assert earnings["data"]["kind"] == "earnings"
    assert notices["data"]["kind"] == "notices"
    assert calls == [
        ("earnings", {"codes": ["600406"]}),
        ("notices", {"codes": ["600406"], "lookback_days": 14}),
    ]


@pytest.mark.asyncio
async def test_research_endpoint_keeps_stable_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise CLIError(
            "腾讯市场上下文不可用",
            code="market_data_unavailable",
            stage="tencent_market_context",
        )

    monkeypatch.setattr(holdings, "_run_legacy_research_builder", fail)

    with pytest.raises(HTTPException) as exc_info:
        await holdings.get_holding_market_status({"id": "user-1"})

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "market_data_unavailable",
        "message": "腾讯市场上下文不可用",
        "stage": "tencent_market_context",
    }
