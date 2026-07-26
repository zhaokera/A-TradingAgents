"""Password-authenticated API client and persistent session storage for agentctl."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

import httpx


DEFAULT_API_URL = "http://localhost:8331"
DEFAULT_CLI_USERNAME = "admin"
DEFAULT_SESSION_DAYS = 7
MAX_SESSION_DAYS = 30
LOCAL_API_HOSTS = {"localhost", "127.0.0.1", "::1"}
SESSION_EXPIRY_SKEW_SECONDS = 30
SESSION_SCHEMA_VERSION = 1


class AgentCLIError(RuntimeError):
    """Structured error surfaced by the JSON CLI."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "cli_error",
        exit_code: int = 2,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.details = dict(details or {})

    def payload(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


@dataclass(frozen=True)
class AgentIdentity:
    user_id: str
    username: str
    is_admin: bool


@dataclass(frozen=True)
class AgentSession:
    schema_version: int
    api_url: str
    username: str
    user_id: str
    is_admin: bool
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int
    session_days: int

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            user_id=self.user_id,
            username=self.username,
            is_admin=self.is_admin,
        )

    def access_is_valid(self, *, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else now
        return self.access_expires_at > current + SESSION_EXPIRY_SKEW_SECONDS

    def refresh_is_valid(self, *, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else now
        return self.refresh_expires_at > current + SESSION_EXPIRY_SKEW_SECONDS

    def public_payload(self) -> Dict[str, Any]:
        return {
            "api_url": self.api_url,
            "username": self.username,
            "user_id": self.user_id,
            "is_admin": self.is_admin,
            "session_days": self.session_days,
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "access_valid": self.access_is_valid(),
            "refresh_valid": self.refresh_is_valid(),
        }


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def normalize_api_url(api_url: Optional[str], *, allow_remote: bool = False) -> str:
    value = (
        api_url
        or os.getenv("A_TRADINGAGENTS_API_URL")
        or os.getenv("TRADINGAGENTS_API_URL")
        or DEFAULT_API_URL
    ).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentCLIError(
            f"无效的后端 API 地址: {value}",
            code="invalid_api_url",
        )
    remote_allowed = allow_remote or _env_flag("A_TRADINGAGENTS_CLI_ALLOW_REMOTE")
    if parsed.hostname not in LOCAL_API_HOSTS and not remote_allowed:
        raise AgentCLIError(
            "默认拒绝向非本机地址发送登录凭据或会话令牌；如确有需要请显式使用 --allow-remote-api",
            code="remote_api_not_allowed",
            details={"api_url": value},
        )
    return value


def resolve_session_file(session_file: Optional[str | Path] = None) -> Path:
    configured = session_file or os.getenv("A_TRADINGAGENTS_SESSION_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".config" / "a-tradingagents" / "agentctl-session.json"


def _validate_session_days(session_days: int) -> int:
    if not DEFAULT_SESSION_DAYS <= session_days <= MAX_SESSION_DAYS:
        raise AgentCLIError(
            f"会话有效期必须在 {DEFAULT_SESSION_DAYS}-{MAX_SESSION_DAYS} 天之间",
            code="invalid_session_days",
        )
    return session_days


def load_session(session_file: Optional[str | Path] = None) -> Optional[AgentSession]:
    path = resolve_session_file(session_file)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        session = AgentSession(**payload)
    except (OSError, ValueError, TypeError) as exc:
        raise AgentCLIError(
            "CLI 会话文件无效，请先执行 agentctl auth logout 后重新登录",
            code="invalid_session_file",
            exit_code=3,
            details={"session_file": str(path)},
        ) from exc
    if session.schema_version != SESSION_SCHEMA_VERSION:
        raise AgentCLIError(
            "CLI 会话版本不兼容，请重新登录",
            code="incompatible_session_file",
            exit_code=3,
            details={"session_file": str(path)},
        )
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise AgentCLIError(
            "无法收紧 CLI 会话文件权限",
            code="session_permission_failed",
            exit_code=4,
            details={"session_file": str(path)},
        ) from exc
    return session


def save_session(
    session: AgentSession,
    session_file: Optional[str | Path] = None,
) -> Path:
    path = resolve_session_file(session_file)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AgentCLIError(
            "CLI 会话保存失败",
            code="session_storage_failed",
            exit_code=4,
            details={"session_file": str(path)},
        ) from exc
    return path


def delete_session(session_file: Optional[str | Path] = None) -> bool:
    path = resolve_session_file(session_file)
    try:
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed
    except OSError as exc:
        raise AgentCLIError(
            "CLI 会话删除失败",
            code="session_storage_failed",
            exit_code=4,
            details={"session_file": str(path)},
        ) from exc


def parse_json_object(value: Optional[str], *, option_name: str) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AgentCLIError(
            f"{option_name} 不是有效 JSON: {exc.msg}",
            code="invalid_json",
            details={"option": option_name},
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentCLIError(
            f"{option_name} 必须是 JSON 对象",
            code="invalid_json_type",
            details={"option": option_name},
        )
    return parsed


def _response_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            pass
    return response.text or None


def _auth_request(
    *,
    base_url: str,
    path: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, Any]:
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        ) as client:
            response = client.post(path, json=dict(payload))
    except httpx.TimeoutException as exc:
        raise AgentCLIError(
            "认证请求超时",
            code="authentication_timeout",
            exit_code=4,
            details={"api_url": base_url},
        ) from exc
    except httpx.HTTPError as exc:
        raise AgentCLIError(
            "无法连接后端认证接口",
            code="api_unavailable",
            exit_code=4,
            details={"api_url": base_url, "error_type": type(exc).__name__},
        ) from exc

    body = _response_body(response)
    if response.is_error:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        code = "invalid_credentials" if response.status_code == 401 and path.endswith("/login") else "authentication_failed"
        raise AgentCLIError(
            "用户名或密码错误" if code == "invalid_credentials" else "CLI 会话认证失败",
            code=code,
            exit_code=3,
            details={"status_code": response.status_code, "response": detail},
        )
    if not isinstance(body, dict) or body.get("success") is not True:
        raise AgentCLIError(
            "认证接口返回格式无效",
            code="invalid_auth_response",
            exit_code=4,
        )
    data = body.get("data")
    if not isinstance(data, dict):
        raise AgentCLIError(
            "认证接口缺少会话数据",
            code="invalid_auth_response",
            exit_code=4,
        )
    return data


def _session_from_login_data(
    *,
    base_url: str,
    username: str,
    data: Mapping[str, Any],
    session_days: int,
    previous: Optional[AgentSession] = None,
) -> AgentSession:
    user = data.get("user")
    if not isinstance(user, Mapping) and previous is None:
        raise AgentCLIError(
            "登录响应缺少用户信息",
            code="invalid_auth_response",
            exit_code=4,
        )
    user = user if isinstance(user, Mapping) else {}
    access_token = str(data.get("access_token") or "")
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token and previous is not None:
        refresh_token = previous.refresh_token
    if not access_token or not refresh_token:
        raise AgentCLIError(
            "登录响应缺少访问令牌或刷新令牌",
            code="invalid_auth_response",
            exit_code=4,
        )
    try:
        access_expires_in = int(data.get("expires_in") or session_days * 86400)
        refresh_expires_in = int(data.get("refresh_expires_in") or MAX_SESSION_DAYS * 86400)
    except (TypeError, ValueError) as exc:
        raise AgentCLIError(
            "登录响应中的有效期无效",
            code="invalid_auth_response",
            exit_code=4,
        ) from exc
    now = int(time.time())
    return AgentSession(
        schema_version=SESSION_SCHEMA_VERSION,
        api_url=base_url,
        username=str(user.get("username") or (previous.username if previous else username)),
        user_id=str(user.get("id") or (previous.user_id if previous else "")),
        is_admin=bool(user.get("is_admin") if user else previous.is_admin if previous else False),
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=now + access_expires_in,
        refresh_expires_at=now + refresh_expires_in,
        session_days=session_days,
    )


def login_session(
    *,
    api_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    session_days: int = DEFAULT_SESSION_DAYS,
    session_file: Optional[str | Path] = None,
    allow_remote: bool = False,
    timeout_seconds: float = 60.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> AgentSession:
    base_url = normalize_api_url(api_url, allow_remote=allow_remote)
    normalized_days = _validate_session_days(session_days)
    effective_username = (
        username or os.getenv("A_TRADINGAGENTS_USERNAME") or DEFAULT_CLI_USERNAME
    ).strip()
    effective_password = password or os.getenv("A_TRADINGAGENTS_PASSWORD")
    if not effective_username or not effective_password:
        raise AgentCLIError(
            "登录需要用户名和密码",
            code="credentials_required",
            exit_code=3,
            details={
                "command": "agentctl auth login --username admin --password '<密码>'",
                "password_env": "A_TRADINGAGENTS_PASSWORD",
            },
        )
    data = _auth_request(
        base_url=base_url,
        path="/api/auth/login",
        payload={
            "username": effective_username,
            "password": effective_password,
            "session_days": normalized_days,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    session = _session_from_login_data(
        base_url=base_url,
        username=effective_username,
        data=data,
        session_days=normalized_days,
    )
    save_session(session, session_file)
    return session


def refresh_session(
    session: AgentSession,
    *,
    session_file: Optional[str | Path] = None,
    timeout_seconds: float = 60.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> AgentSession:
    if not session.refresh_is_valid():
        raise AgentCLIError(
            "CLI 会话已过期，请重新使用账号密码登录",
            code="authentication_required",
            exit_code=3,
            details={"command": "agentctl auth login --username admin --password '<密码>'"},
        )
    data = _auth_request(
        base_url=session.api_url,
        path="/api/auth/refresh",
        payload={
            "refresh_token": session.refresh_token,
            "session_days": session.session_days,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    refreshed = _session_from_login_data(
        base_url=session.api_url,
        username=session.username,
        data=data,
        session_days=session.session_days,
        previous=session,
    )
    save_session(refreshed, session_file)
    return refreshed


class AgentApiClient:
    """Thin sync client that normalizes all backend responses to JSON."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgentApiClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @staticmethod
    def _normalize_success(response: httpx.Response, body: Any) -> Dict[str, Any]:
        if isinstance(body, dict) and "success" in body:
            ok = bool(body.get("success"))
            data = body.get("data")
            message = body.get("message", "ok")
        else:
            ok = True
            data = body
            message = "ok"
        return {
            "ok": ok,
            "status_code": response.status_code,
            "data": data,
            "message": message,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        request_params = {key: value for key, value in (params or {}).items() if value is not None}
        try:
            response = self._client.request(
                method.upper(),
                path,
                params=request_params or None,
                json=dict(payload) if payload is not None else None,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise AgentCLIError(
                "后端请求超时",
                code="api_timeout",
                exit_code=4,
                details={"method": method.upper(), "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentCLIError(
                f"无法连接后端 API: {exc}",
                code="api_unavailable",
                exit_code=4,
                details={"api_url": self.base_url},
            ) from exc

        body = _response_body(response)
        if response.is_error:
            detail = body.get("detail", body) if isinstance(body, dict) else body
            backend_code = (
                str(detail.get("code"))
                if isinstance(detail, dict) and detail.get("code")
                else "api_request_failed"
            )
            backend_message = (
                str(detail.get("message"))
                if isinstance(detail, dict) and detail.get("message")
                else f"后端请求失败: HTTP {response.status_code}"
            )
            raise AgentCLIError(
                backend_message,
                code=backend_code,
                exit_code=4,
                details={
                    "method": method.upper(),
                    "path": path,
                    "status_code": response.status_code,
                    "response": detail,
                },
            )
        return self._normalize_success(response, body)

    def download(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        try:
            response = self._client.get(path, params=params, timeout=timeout_seconds)
        except httpx.HTTPError as exc:
            raise AgentCLIError(
                f"报告下载失败: {exc}",
                code="download_failed",
                exit_code=4,
            ) from exc
        if response.is_error:
            raise AgentCLIError(
                f"报告下载失败: HTTP {response.status_code}",
                code="download_failed",
                exit_code=4,
                details={"response": _response_body(response)},
            )
        return response.content, response.headers


def build_api_client(
    *,
    api_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    session_days: int = DEFAULT_SESSION_DAYS,
    session_file: Optional[str | Path] = None,
    require_admin: bool = False,
    allow_remote: bool = False,
    timeout_seconds: float = 60.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> tuple[AgentIdentity, AgentApiClient]:
    # Validate the destination before reading credentials or session tokens.
    base_url = normalize_api_url(api_url, allow_remote=allow_remote)
    normalized_days = _validate_session_days(session_days)
    effective_username = (
        username or os.getenv("A_TRADINGAGENTS_USERNAME") or DEFAULT_CLI_USERNAME
    ).strip()
    session = load_session(session_file)
    if session is not None and (
        session.api_url != base_url or session.username != effective_username
    ):
        session = None

    if session is None:
        effective_password = password or os.getenv("A_TRADINGAGENTS_PASSWORD")
        if not effective_password:
            raise AgentCLIError(
                "尚未登录 CLI，请先使用账号密码登录",
                code="authentication_required",
                exit_code=3,
                details={
                    "command": f"agentctl auth login --username {effective_username} --password '<密码>'",
                    "session_file": str(resolve_session_file(session_file)),
                },
            )
        session = login_session(
            api_url=base_url,
            username=effective_username,
            password=effective_password,
            session_days=normalized_days,
            session_file=session_file,
            allow_remote=allow_remote,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
    elif not session.access_is_valid():
        try:
            session = refresh_session(
                session,
                session_file=session_file,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
        except AgentCLIError:
            effective_password = password or os.getenv("A_TRADINGAGENTS_PASSWORD")
            if not effective_password:
                raise
            session = login_session(
                api_url=base_url,
                username=effective_username,
                password=effective_password,
                session_days=normalized_days,
                session_file=session_file,
                allow_remote=allow_remote,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )

    identity = session.identity
    if require_admin and not identity.is_admin:
        raise AgentCLIError(
            "该命令需要管理员身份",
            code="admin_required",
            exit_code=3,
        )
    client = AgentApiClient(
        base_url=base_url,
        token=session.access_token,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    return identity, client
