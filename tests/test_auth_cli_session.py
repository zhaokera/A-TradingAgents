from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.routers.auth_db import LoginRequest, RefreshTokenRequest, _token_lifetimes, login


def test_cli_session_lifetime_is_at_least_one_week() -> None:
    access_seconds, refresh_seconds = _token_lifetimes(7)

    assert access_seconds == 7 * 24 * 60 * 60
    assert refresh_seconds >= access_seconds


def test_web_session_keeps_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.routers.auth_db.settings.ACCESS_TOKEN_EXPIRE_MINUTES", 480)
    monkeypatch.setattr("app.routers.auth_db.settings.REFRESH_TOKEN_EXPIRE_DAYS", 30)

    assert _token_lifetimes(None) == (480 * 60, 30 * 24 * 60 * 60)


@pytest.mark.parametrize("model", [LoginRequest, RefreshTokenRequest])
def test_requested_cli_session_cannot_be_shorter_than_week(model: type) -> None:
    payload = (
        {"username": "admin", "password": "secret", "session_days": 6}
        if model is LoginRequest
        else {"refresh_token": "token", "session_days": 6}
    )

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.asyncio
async def test_login_endpoint_issues_requested_week_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id="user-1",
        username="admin",
        email="admin@example.com",
        is_admin=True,
    )
    token_calls: list[int] = []

    async def authenticate_user(_username: str, _password: str) -> SimpleNamespace:
        return user

    async def log_operation(**_kwargs: object) -> None:
        return None

    def create_access_token(*, sub: str, expires_delta: int) -> str:
        assert sub == "admin"
        token_calls.append(expires_delta)
        return f"token-{expires_delta}"

    monkeypatch.setattr(
        "app.routers.auth_db.user_service.authenticate_user",
        authenticate_user,
    )
    monkeypatch.setattr("app.routers.auth_db.log_operation", log_operation)
    monkeypatch.setattr(
        "app.routers.auth_db.AuthService.create_access_token",
        create_access_token,
    )
    monkeypatch.setattr("app.routers.auth_db.settings.REFRESH_TOKEN_EXPIRE_DAYS", 30)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )

    result = await login(
        LoginRequest(username="admin", password="secret", session_days=7),
        request,
    )

    assert token_calls == [7 * 86400, 30 * 86400]
    assert result["data"]["expires_in"] == 7 * 86400
    assert result["data"]["refresh_expires_in"] == 30 * 86400
