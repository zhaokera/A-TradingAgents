from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from cli.agent_client import (
    AgentApiClient,
    AgentCLIError,
    AgentSession,
    build_api_client,
    login_session,
    normalize_api_url,
    parse_json_object,
    save_session,
)


def _session(*, access_expires_at: int, refresh_expires_at: int) -> AgentSession:
    return AgentSession(
        schema_version=1,
        api_url="http://localhost:8331",
        username="admin",
        user_id="user-1",
        is_admin=True,
        access_token="cached-access",
        refresh_token="cached-refresh",
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        session_days=7,
    )


def test_normalize_api_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A_TRADINGAGENTS_API_URL", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_API_URL", raising=False)

    assert normalize_api_url(None) == "http://localhost:8331"


def test_normalize_api_url_rejects_remote_http_by_default() -> None:
    with pytest.raises(AgentCLIError) as exc_info:
        normalize_api_url("http://example.com:8331")

    assert exc_info.value.code == "remote_api_not_allowed"


def test_build_client_rejects_remote_before_reading_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_session_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("session must not be read")

    monkeypatch.setattr("cli.agent_client.load_session", unexpected_session_read)

    with pytest.raises(AgentCLIError) as exc_info:
        build_api_client(api_url="https://example.com")

    assert exc_info.value.code == "remote_api_not_allowed"


def test_parse_json_object_rejects_array() -> None:
    with pytest.raises(AgentCLIError) as exc_info:
        parse_json_object("[]", option_name="--payload-json")

    assert exc_info.value.code == "invalid_json_type"


def test_login_uses_password_and_saves_secure_week_session(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/login"
        payload = json.loads(request.content)
        assert payload == {
            "username": "admin",
            "password": "secret-password",
            "session_days": 7,
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 7 * 86400,
                    "refresh_expires_in": 30 * 86400,
                    "user": {"id": "user-1", "username": "admin", "is_admin": True},
                },
                "message": "登录成功",
            },
        )

    session = login_session(
        username="admin",
        password="secret-password",
        session_file=session_file,
        transport=httpx.MockTransport(handler),
    )

    assert session.access_expires_at >= int(time.time()) + 7 * 86400 - 2
    assert session.refresh_expires_at >= int(time.time()) + 30 * 86400 - 2
    assert session_file.stat().st_mode & 0o777 == 0o600
    stored = session_file.read_text(encoding="utf-8")
    assert "secret-password" not in stored
    assert "access-token" in stored


def test_build_client_uses_cached_session_without_password(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"
    save_session(
        _session(
            access_expires_at=int(time.time()) + 3600,
            refresh_expires_at=int(time.time()) + 86400,
        ),
        session_file,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer cached-access"
        return httpx.Response(200, json={"success": True, "data": {"value": 1}})

    identity, client = build_api_client(
        username="admin",
        session_file=session_file,
        transport=httpx.MockTransport(handler),
    )
    with client:
        result = client.request("GET", "/api/test")

    assert identity.username == "admin"
    assert result["data"] == {"value": 1}


def test_build_client_refreshes_expired_access_token(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"
    save_session(
        _session(
            access_expires_at=int(time.time()) - 1,
            refresh_expires_at=int(time.time()) + 86400,
        ),
        session_file,
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/auth/refresh":
            assert json.loads(request.content) == {
                "refresh_token": "cached-refresh",
                "session_days": 7,
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 7 * 86400,
                        "refresh_expires_in": 30 * 86400,
                    },
                },
            )
        assert request.headers["authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"success": True, "data": {}})

    _identity, client = build_api_client(
        username="admin",
        session_file=session_file,
        transport=httpx.MockTransport(handler),
    )
    with client:
        client.request("GET", "/api/test")

    assert requests == ["/api/auth/refresh", "/api/test"]
    assert "new-access" in session_file.read_text(encoding="utf-8")


def test_login_error_never_includes_password(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "用户名或密码错误"})

    with pytest.raises(AgentCLIError) as exc_info:
        login_session(
            username="admin",
            password="do-not-leak",
            session_file=tmp_path / "session.json",
            transport=httpx.MockTransport(handler),
        )

    assert exc_info.value.code == "invalid_credentials"
    assert "do-not-leak" not in json.dumps(exc_info.value.payload(), ensure_ascii=False)


def test_api_client_unwraps_standard_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(
            200,
            json={"success": True, "data": {"value": 1}, "message": "done"},
        )

    with AgentApiClient(
        base_url="http://localhost:8331",
        token="token",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.request("GET", "/api/test")

    assert result == {
        "ok": True,
        "status_code": 200,
        "data": {"value": 1},
        "message": "done",
    }


def test_api_client_returns_structured_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "missing"})

    with AgentApiClient(
        base_url="http://localhost:8331",
        token="token",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentCLIError) as exc_info:
            client.request("GET", "/api/missing")

    assert exc_info.value.code == "api_request_failed"
    assert exc_info.value.details["status_code"] == 404


def test_api_client_preserves_backend_error_code_and_message() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "market_data_unavailable",
                    "message": "市场数据不可用",
                    "stage": "tencent_market_context",
                }
            },
        )

    with AgentApiClient(
        base_url="http://localhost:8331",
        token="token",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentCLIError) as exc_info:
            client.request("GET", "/api/holdings/research/market-status")

    assert exc_info.value.code == "market_data_unavailable"
    assert exc_info.value.message == "市场数据不可用"
    assert exc_info.value.details["response"]["stage"] == "tencent_market_context"
