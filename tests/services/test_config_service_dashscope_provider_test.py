"""Regression tests for the DashScope provider connectivity check."""

import asyncio
import time
from types import SimpleNamespace

import requests

from app.models.config import LLMConfig
from app.services.config_service import ConfigService


class _SuccessfulDashScopeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {
            "choices": [
                {
                    "message": {
                        "content": "OK",
                    }
                }
            ]
        }


def test_dashscope_provider_test_defaults_to_qwen_3_7_max(monkeypatch):
    captured_request = {}

    def fake_post(url, *, json, headers, timeout):
        captured_request.update(
            {
                "url": url,
                "payload": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _SuccessfulDashScopeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    result = ConfigService()._test_dashscope_api(
        api_key="test-api-key",
        display_name="阿里百炼",
    )

    assert result == {
        "success": True,
        "message": "阿里百炼 API连接测试成功",
    }
    assert captured_request["payload"]["model"] == "qwen3.7-max"
    assert captured_request["timeout"] == 30


def test_dashscope_llm_config_test_does_not_block_event_loop():
    service = ConfigService()
    events = []

    class _ProviderCollection:
        @staticmethod
        async def find_one(_query):
            return {}

    async def get_fake_db():
        return SimpleNamespace(llm_providers=_ProviderCollection())

    def slow_dashscope_test(_api_key, _display_name, _model_name):
        time.sleep(0.1)
        events.append("llm-test-finished")
        return {
            "success": True,
            "message": "阿里百炼 API连接测试成功",
        }

    service._get_db = get_fake_db
    service._test_dashscope_api = slow_dashscope_test

    async def run_scenario():
        async def heartbeat():
            await asyncio.sleep(0.01)
            events.append("event-loop-responsive")

        result, _ = await asyncio.gather(
            service.test_llm_config(
                LLMConfig(
                    provider="dashscope",
                    model_name="qwen3.7-max",
                    api_key="test-api-key",
                    api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
                )
            ),
            heartbeat(),
        )
        return result

    result = asyncio.run(run_scenario())

    assert result["success"] is True
    assert events == [
        "event-loop-responsive",
        "llm-test-finished",
    ]
