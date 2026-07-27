import asyncio
from types import SimpleNamespace

import pytest

from app.models.analysis import SingleAnalysisRequest
from app.routers import analysis as analysis_router
from app.services.model_capability_service import (
    ModelCapabilityService,
    ModelRecommendationError,
)
from app.services.simple_analysis_service import (
    ModelConnectivityError,
    _credential_candidates,
    _propagate_trading_graph,
    _select_verified_model_credential,
)


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_symbol_request_is_used_for_graph_propagation():
    calls = []

    class Graph:
        def propagate(self, ticker, date, **kwargs):
            calls.append((ticker, date, kwargs))
            return {"ticker": ticker}, {"action": "hold"}

    state, decision = _propagate_trading_graph(
        Graph(),
        SingleAnalysisRequest(symbol="600562", stock_code=None),
        "2026-07-27",
        progress_callback="callback",
        task_id="task-1",
    )

    assert state == {"ticker": "600562"}
    assert decision == {"action": "hold"}
    assert calls == [
        (
            "600562",
            "2026-07-27",
            {"progress_callback": "callback", "task_id": "task-1"},
        )
    ]


def test_recommendation_accepts_string_roles_and_features(monkeypatch):
    enabled_model = SimpleNamespace(
        model_name="qwen3.7-max",
        enabled=True,
        suitable_roles=["both"],
        features=["tool_calling", "reasoning", "long_context"],
        capability_level=5,
        performance_metrics={"cost": 3, "quality": 5},
    )
    disabled_model = SimpleNamespace(
        model_name="qwen-plus",
        enabled=False,
        suitable_roles=["both"],
        features=["tool_calling"],
        capability_level=5,
        performance_metrics={"cost": 1, "quality": 5},
    )
    monkeypatch.setattr(
        ModelCapabilityService,
        "_get_enabled_model_configs",
        lambda _self: [disabled_model, enabled_model],
    )

    assert ModelCapabilityService().recommend_models_for_depth("标准") == (
        "qwen3.7-max",
        "qwen3.7-max",
    )


def test_recommendation_never_falls_back_to_unenabled_model(monkeypatch):
    incompatible = SimpleNamespace(
        model_name="qwen3.7-plus",
        enabled=True,
        suitable_roles=["deep_analysis"],
        features=[],
        capability_level=5,
        performance_metrics={},
    )
    monkeypatch.setattr(
        ModelCapabilityService,
        "_get_enabled_model_configs",
        lambda _self: [incompatible],
    )

    with pytest.raises(ModelRecommendationError, match="已启用模型不满足"):
        ModelCapabilityService().recommend_models_for_depth("标准")


def test_invalid_model_key_falls_back_to_verified_environment_key():
    calls = []

    def post(_url, *, headers, **_kwargs):
        api_key = headers["Authorization"].removeprefix("Bearer ")
        calls.append(api_key)
        if api_key == "bad-model-key":
            return _Response(401, {"code": "invalid_api_key"})
        return _Response(200, {"choices": [{"message": {"content": "ok"}}]})

    candidates = _credential_candidates(
        "bad-model-key",
        None,
        "valid-environment-key",
    )
    api_key, source, diagnostics = _select_verified_model_credential(
        model_name="qwen3.7-max",
        provider="qwen",
        backend_url="https://example.invalid/v1",
        candidates=candidates,
        request_post=post,
    )

    assert calls == ["bad-model-key", "valid-environment-key"]
    assert api_key == "valid-environment-key"
    assert source == "environment"
    assert diagnostics == [
        {
            "source": "model_config",
            "http_status": 401,
            "error_code": "invalid_api_key",
        },
        {"source": "environment", "http_status": 200},
    ]
    assert "bad-model-key" not in repr(diagnostics)
    assert "valid-environment-key" not in repr(diagnostics)


def test_connectivity_failure_is_diagnostic_without_exposing_keys():
    def post(_url, **_kwargs):
        return _Response(403, {"code": "Model.AccessDenied"})

    with pytest.raises(ModelConnectivityError) as exc_info:
        _select_verified_model_credential(
            model_name="qwen3.7-max",
            provider="qwen",
            backend_url="https://example.invalid/v1",
            candidates=[("model_config", "secret-model-key")],
            request_post=post,
        )

    message = str(exc_info.value)
    assert "model_config:403" in message
    assert "secret-model-key" not in message


def test_analysis_details_uses_the_same_contract_as_status(monkeypatch):
    calls = []

    async def fake_status(task_id, user):
        calls.append((task_id, user))
        return {
            "success": True,
            "data": {"task_id": task_id, "status": "running"},
            "message": "任务状态获取成功",
        }

    monkeypatch.setattr(analysis_router, "get_task_status_new", fake_status)
    user = {"id": "admin"}
    result = asyncio.run(
        analysis_router.get_task_details(task_id="visible-task", user=user)
    )

    assert calls == [("visible-task", user)]
    assert result["data"] == {
        "task_id": "visible-task",
        "status": "running",
    }
