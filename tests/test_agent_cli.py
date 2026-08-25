from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from typer.testing import CliRunner

import cli.agent as agent_cli
from cli.agent_client import AgentIdentity, AgentSession


runner = CliRunner()


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[Dict[str, Any]] = []
        self.download_content = b"report"
        self.responses: Dict[str, Any] = {}

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if path in self.responses:
            data = self.responses[path]
        elif path == "/api/favorites/":
            data: Any = []
        elif path == "/api/analysis/tasks":
            data = {"tasks": [], "total": 0}
        elif path == "/api/news-data/latest":
            data = {"news": [], "total_count": 0}
        else:
            data = {"path": path}
        return {"ok": True, "status_code": 200, "data": data, "message": "ok"}

    def download(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[bytes, Dict[str, str]]:
        self.calls.append(
            {
                "method": "GET",
                "path": path,
                "params": params,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.download_content, {"content-disposition": "attachment; filename=test.md"}


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()

    def fake_build_api_client(**_kwargs: Any) -> tuple[AgentIdentity, FakeClient]:
        return AgentIdentity(user_id="1", username="admin", is_admin=True), client

    monkeypatch.setattr(agent_cli, "build_api_client", fake_build_api_client)
    return client


def test_candidates_latest_calls_backend(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["candidates", "latest"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True
    assert fake_client.calls[0]["path"] == "/api/screening/ai-candidates/latest"
    assert fake_client.calls[0]["params"] == {"refresh": "true"}


def test_holdings_opportunities_is_candidate_api_alias(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["holdings", "opportunities"])

    assert result.exit_code == 0
    assert [call["path"] for call in fake_client.calls] == [
        "/api/screening/ai-candidates/latest"
    ]


def test_candidates_performance_calls_backend(fake_client: FakeClient) -> None:
    fake_client.responses["/api/screening/ai-candidates/performance"] = {
        "statistics_scope": "candidate_shadow_diagnostics",
        "governed_decision_sample_count": 0,
        "learning_eligible_count": 0,
        "items": [],
    }
    result = runner.invoke(agent_cli.app, ["candidates", "performance"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/screening/ai-candidates/performance"
    data = json.loads(result.stdout)["data"]
    assert data["statistics_scope"] == "candidate_shadow_diagnostics"
    assert data["governed_decision_sample_count"] == 0
    assert data["learning_eligible_count"] == 0


def test_briefing_today_calls_unified_backend_contract(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["briefing", "today", "--no-refresh"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/briefing/today"
    assert fake_client.calls[0]["params"] == {"refresh": "false"}


def test_account_permissions_calls_authenticated_backend(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["account", "permissions"])

    assert result.exit_code == 0
    assert fake_client.calls[0] == {
        "method": "GET",
        "path": "/api/holdings/market-permissions",
        "params": None,
        "payload": None,
        "timeout_seconds": None,
    }


def test_account_set_permission_requires_confirmation(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "account",
            "set-permission",
            "--market",
            "chi_next_market",
            "--state",
            "denied",
        ],
    )

    assert result.exit_code != 0
    assert fake_client.calls == []
    assert json.loads(result.stderr)["error"]["code"] == (
        "confirmation_required"
    )


def test_account_set_permission_updates_one_supported_market(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "account",
            "set-permission",
            "--market",
            "chi_next_market",
            "--state",
            "denied",
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.calls[0] == {
        "method": "PATCH",
        "path": (
            "/api/holdings/market-permissions/chi_next_market"
        ),
        "params": None,
        "payload": {"state": "denied"},
        "timeout_seconds": None,
    }


def test_decision_today_calls_authenticated_backend_contract(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["decision", "today", "--no-refresh"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/decision/today"
    assert fake_client.calls[0]["params"] == {"refresh": "false"}


def _sample_decision() -> Dict[str, Any]:
    item = {
        "identity": {
            "code": "600406",
            "name": "国电南瑞",
            "objective_segment": "电力设备",
            "objective_match_score": 1.0,
        },
        "action": "condition_order",
        "reason_codes": ["waiting_entry"],
        "quote": {
            "price": 22.5,
            "source": "tencent",
            "trade_at": "2026-07-23T09:44:59+08:00",
            "status": "fresh",
            "quote_checked_at": "2026-07-23T09:45:00+08:00",
        },
        "execution": {
            "status": "condition_order_eligible",
            "order_limit_price": 22.1,
        },
        "plans": {
            "short": {
                "entry_price": 22.1,
                "stop_price": 21.4,
                "target_price": 23.8,
                "entry_status": "waiting_pullback",
            }
        },
        "allocation": {
            "quantity": 200,
            "amount": 4420.0,
            "position_pct": 41.0,
        },
        "planned_loss": {"amount": 140.0, "pct_of_assets": 1.3},
        "profile": {"status": "complete", "confidence": "high"},
        "plan_id": "plan-1",
    }
    return {
        "decision_id": "decision-1",
        "revision": 2,
        "decision_date": "2026-07-23",
        "market_phase": "live_am",
        "as_of": "2026-07-23T09:45:00+08:00",
        "candidate_run_id": "run-1",
        "briefing_as_of": "2026-07-23T09:44:00+08:00",
        "account": {"total_assets": 10_685.41},
        "execution_capabilities": {
            "condition_order": {
                "verified": True,
                "independent_trigger_price_supported": True,
                "separate_order_limit_price_supported": True,
                "eligible": True,
            }
        },
        "market": {"combined_regime": "green"},
        "rolling_pool": {
            "capacity": 100,
            "total_count": 70,
            "formal_research_capacity": 15,
            "formal_research_count": 15,
            "candidates": [
                {
                    "code": "600406",
                    "selected_for_formal_research": True,
                    "selection_reason": "dynamic_formal_research_selected",
                }
            ],
        },
        "summary": {
            "buy_now_count": 0,
            "condition_order_count": 1,
            "wait_count": 0,
            "avoid_count": 0,
        },
        "buy_now": [],
        "condition_order": [item],
        "wait": [],
        "avoid": [],
        "rule_version": "decision-v1",
        "material_hash": "hash-1",
        "authority": "software_baseline",
        "is_final_decision": False,
    }


def test_decision_today_summary_is_compact_and_machine_readable(
    fake_client: FakeClient,
) -> None:
    fake_client.responses["/api/decision/today"] = _sample_decision()

    result = runner.invoke(
        agent_cli.app,
        ["decision", "today", "--no-refresh", "--view", "summary"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["view"] == "summary"
    assert data["authority"] == "software_baseline"
    assert data["is_final_decision"] is False
    assert data["execution_capabilities"]["condition_order"]["eligible"] is True
    assert data["rolling_pool"]["total_count"] == 70
    assert data["rolling_pool"]["formal_research_count"] == 15
    assert data["condition_order"][0] == {
        "code": "600406",
        "name": "国电南瑞",
        "action": "condition_order",
        "reason_codes": ["waiting_entry"],
        "objective_segment": "电力设备",
        "objective_match_score": 1.0,
        "price": 22.5,
        "current_price": 22.5,
        "current_price_trade_at": "2026-07-23T09:44:59+08:00",
        "quote_source": "tencent",
        "quote_status": "fresh",
        "quote_checked_at": "2026-07-23T09:45:00+08:00",
        "entry_price": 22.1,
        "entry_reference_price": 22.1,
        "order_limit_price": 22.1,
        "execution_status": "condition_order_eligible",
        "stop_price": 21.4,
        "target_price": 23.8,
        "plan_status": "waiting_pullback",
        "quantity": 200,
        "amount": 4420.0,
        "position_pct": 41.0,
        "planned_loss_amount": 140.0,
        "planned_loss_pct_of_assets": 1.3,
        "profile_status": "complete",
        "profile_confidence": "high",
        "plan_id": "plan-1",
    }
    assert "plans" not in data["condition_order"][0]


def test_decision_today_actionable_omits_wait_and_avoid(fake_client: FakeClient) -> None:
    fake_client.responses["/api/decision/today"] = _sample_decision()

    result = runner.invoke(
        agent_cli.app,
        ["decision", "today", "--view", "actionable"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert set(data).isdisjoint({"wait", "avoid"})
    assert data["condition_order"][0]["code"] == "600406"
    assert data["rolling_pool"]["capacity"] == 100


def test_decision_explain_returns_one_symbol_with_bucket(fake_client: FakeClient) -> None:
    fake_client.responses["/api/decision/today"] = _sample_decision()

    result = runner.invoke(
        agent_cli.app,
        ["decision", "explain", "--code", "SH600406"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["bucket"] == "condition_order"
    assert data["item"]["identity"]["code"] == "600406"
    assert fake_client.calls[0]["params"] == {"refresh": "false"}


def test_decision_explain_returns_stable_not_found_error(fake_client: FakeClient) -> None:
    fake_client.responses["/api/decision/today"] = _sample_decision()

    result = runner.invoke(
        agent_cli.app,
        ["decision", "explain", "--code", "000001"],
    )

    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "decision_symbol_not_found"
    assert error["details"]["available_codes"] == ["600406"]


def test_decision_history_calls_authenticated_backend_contract(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["decision", "history", "--limit", "7"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/decision/history"
    assert fake_client.calls[0]["params"] == {"limit": 7}


def test_decision_performance_calls_authenticated_backend_contract(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(agent_cli.app, ["decision", "performance"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/decision/performance"
    assert fake_client.calls[0]["params"] is None


def test_decision_research_and_baseline_call_governed_workflow(
    fake_client: FakeClient,
) -> None:
    research = runner.invoke(
        agent_cli.app,
        ["decision", "research", "--no-refresh"],
    )
    baseline = runner.invoke(
        agent_cli.app,
        ["decision", "baseline", "--no-refresh"],
    )

    assert research.exit_code == baseline.exit_code == 0
    assert fake_client.calls[0] == {
        "method": "GET",
        "path": "/api/decision/research/today",
        "params": {"refresh": "false"},
        "payload": None,
        "timeout_seconds": 180.0,
    }
    assert fake_client.calls[1]["method"] == "GET"
    assert fake_client.calls[1]["path"] == "/api/decision/baseline/today"
    assert fake_client.calls[1]["params"] == {"refresh": "false"}


def test_decision_propose_posts_exact_json_object(fake_client: FakeClient) -> None:
    payload = {
        "research_packet_id": "research-1",
        "selections": [],
        "portfolio_rationale": "当前不新开仓",
        "no_action_reason": "等待有效价格计划",
    }

    result = runner.invoke(
        agent_cli.app,
        [
            "decision",
            "propose",
            "--payload-json",
            json.dumps(payload, ensure_ascii=False),
        ],
    )

    assert result.exit_code == 0
    assert fake_client.calls[0]["method"] == "POST"
    assert fake_client.calls[0]["path"] == "/api/decision/proposals"
    assert fake_client.calls[0]["payload"] == payload


def test_decision_propose_invalid_json_uses_structured_error(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        ["decision", "propose", "--payload-json", "not-json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == {
        "code": "invalid_json",
        "message": "--payload-json 不是有效 JSON: Expecting value",
        "details": {"option": "--payload-json"},
    }
    assert fake_client.calls == []


def test_decision_validate_and_final_call_exact_endpoints(
    fake_client: FakeClient,
) -> None:
    validated = runner.invoke(
        agent_cli.app,
        [
            "decision",
            "validate",
            "--proposal-id",
            "proposal-1",
            "--refresh-quote",
        ],
    )
    final = runner.invoke(agent_cli.app, ["decision", "final", "--no-refresh"])

    assert validated.exit_code == final.exit_code == 0
    assert fake_client.calls[0]["method"] == "POST"
    assert (
        fake_client.calls[0]["path"]
        == "/api/decision/proposals/proposal-1/validate"
    )
    assert fake_client.calls[0]["params"] == {"refresh_quote": "true"}
    assert fake_client.calls[1]["method"] == "GET"
    assert fake_client.calls[1]["path"] == "/api/decision/final/today"
    assert fake_client.calls[1]["params"] == {"refresh": "false"}


def test_decision_confirm_requires_explicit_confirm_before_api_call(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "decision",
            "confirm",
            "--proposal-id",
            "proposal-1",
            "--validation-id",
            "validation-1",
            "--accept",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "confirmation_required"
    assert fake_client.calls == []


def test_decision_confirm_posts_explicit_acceptance_without_execution(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "decision",
            "confirm",
            "--proposal-id",
            "proposal-1",
            "--validation-id",
            "validation-1",
            "--accept",
            "--reason",
            "我已自行核对",
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.calls[0]["method"] == "POST"
    assert (
        fake_client.calls[0]["path"]
        == "/api/decision/proposals/proposal-1/confirm"
    )
    assert fake_client.calls[0]["payload"] == {
        "validation_id": "validation-1",
        "accepted": True,
        "reason": "我已自行核对",
    }


def test_decision_confirm_accept_and_reject_are_mutually_exclusive(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "decision",
            "confirm",
            "--proposal-id",
            "proposal-1",
            "--validation-id",
            "validation-1",
            "--accept",
            "--reject",
            "--confirm",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "confirmation_choice_invalid"
    assert fake_client.calls == []


def test_capabilities_are_machine_readable_without_backend() -> None:
    result = runner.invoke(agent_cli.app, ["capabilities"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["contract"] == "json-v1"
    assert "latest" in payload["data"]["groups"]["candidates"]
    assert "performance" in payload["data"]["groups"]["decision"]
    assert "explain" in payload["data"]["groups"]["decision"]
    assert "research" in payload["data"]["groups"]["decision"]
    assert "propose" in payload["data"]["groups"]["decision"]
    assert "confirm" in payload["data"]["groups"]["decision"]
    assert payload["data"]["decision_contract"]["execution"] == "docker_backend_api_only"
    assert payload["data"]["safety"]["initial_password_login_required"] is True
    assert payload["data"]["safety"]["minimum_session_days"] == 7


def test_auth_login_never_prints_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_login_session(**_kwargs: Any) -> AgentSession:
        return AgentSession(
            schema_version=1,
            api_url="http://localhost:8331",
            username="admin",
            user_id="1",
            is_admin=True,
            access_token="private-access-token",
            refresh_token="private-refresh-token",
            access_expires_at=2_000_000_000,
            refresh_expires_at=2_100_000_000,
            session_days=7,
        )

    monkeypatch.setattr(agent_cli, "login_session", fake_login_session)
    result = runner.invoke(
        agent_cli.app,
        [
            "--session-file",
            str(tmp_path / "session.json"),
            "auth",
            "login",
            "--username",
            "admin",
            "--password",
            "secret",
        ],
    )

    assert result.exit_code == 0
    assert "private-access-token" not in result.stdout
    assert "private-refresh-token" not in result.stdout
    assert json.loads(result.stdout)["data"]["session_days"] == 7


def test_unified_holdings_list_uses_backend_api(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["holdings", "list"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/holdings/snapshot"
    assert fake_client.calls[0]["params"] == {"analysis": "true"}


def test_unified_holdings_summary_uses_backend_api(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["holdings", "summary"])

    assert result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/holdings/snapshot"
    assert fake_client.calls[0]["params"] == {"summary_only": "true"}


def test_holding_research_commands_use_backend_api(fake_client: FakeClient) -> None:
    market = runner.invoke(agent_cli.app, ["holdings", "market-status"])
    earnings = runner.invoke(
        agent_cli.app,
        ["holdings", "earnings", "--code", "600406"],
    )
    notices = runner.invoke(
        agent_cli.app,
        ["holdings", "notices", "--code", "600406", "--lookback-days", "14"],
    )

    assert market.exit_code == earnings.exit_code == notices.exit_code == 0
    assert [call["path"] for call in fake_client.calls] == [
        "/api/holdings/research/market-status",
        "/api/holdings/research/earnings",
        "/api/holdings/research/notices",
    ]
    assert fake_client.calls[1]["payload"] == {"codes": ["600406"]}
    assert fake_client.calls[2]["payload"] == {
        "codes": ["600406"],
        "lookback_days": 14,
    }


def test_unified_agent_cli_does_not_import_local_holdings_research_runtime() -> None:
    source = (Path(__file__).resolve().parents[1] / "cli" / "agent.py").read_text(
        encoding="utf-8"
    )

    assert "from app.services import holdings_cli" not in source
    assert "legacy_holdings." not in source


def test_doctor_validates_decision_agent_contract(fake_client: FakeClient) -> None:
    fake_client.responses["/api/decision/today"] = _sample_decision()
    fake_client.responses["/api/decision/research/today"] = {
        "research_packet_id": "research-1",
        "candidates": [],
        "hard_risk_policy": {},
    }
    fake_client.responses["/api/decision/final/today"] = {
        "authority": "software_baseline",
        "is_final_decision": False,
    }

    result = runner.invoke(agent_cli.app, ["doctor"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["ready_for_decision_agent"] is True
    assert data["passed"] == data["total"] == 12
    assert {item["name"] for item in data["checks"]} >= {
        "decision_research",
        "decision_final",
    }


def test_unified_holdings_does_not_expose_user_switching_options() -> None:
    result = runner.invoke(agent_cli.app, ["holdings", "list", "--help"])

    assert result.exit_code == 0
    assert "--user-id" not in result.stdout
    assert "--email" not in result.stdout


def test_dashboard_aggregates_same_web_sources(fake_client: FakeClient) -> None:
    result = runner.invoke(agent_cli.app, ["dashboard"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["summary"] == {
        "analysis_tasks": 0,
        "completed_tasks": 0,
        "favorites": 0,
    }
    assert [call["path"] for call in fake_client.calls] == [
        "/api/favorites/",
        "/api/analysis/tasks",
        "/api/news-data/latest",
    ]


def test_candidates_add_favorites_sends_selected_codes(fake_client: FakeClient) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "candidates",
            "add-favorites",
            "--run-id",
            "run-1",
            "--code",
            "600406",
            "--code",
            "601138",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.calls[0]["payload"] == {"codes": ["600406", "601138"]}


def test_holdings_create_uses_web_holding_endpoint(fake_client: FakeClient) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "holdings",
            "create",
            "--code",
            "600406",
            "--quantity",
            "100",
            "--cost-price",
            "21.8",
        ],
    )

    assert result.exit_code == 0
    call = fake_client.calls[0]
    assert call["path"] == "/api/holdings/"
    assert call["payload"]["quantity"] == 100
    assert call["payload"]["cost_price"] == 21.8


def test_analysis_start_builds_web_task_payload(fake_client: FakeClient) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "analysis",
            "start",
            "--code",
            "600406",
            "--research-depth",
            "深度",
            "--analyst",
            "market",
            "--analyst",
            "fundamentals",
        ],
    )

    assert result.exit_code == 0
    call = fake_client.calls[0]
    assert call["path"] == "/api/analysis/single"
    assert call["payload"]["symbol"] == "600406"
    assert call["payload"]["parameters"]["selected_analysts"] == ["market", "fundamentals"]


def test_admin_mutation_requires_confirmation(fake_client: FakeClient) -> None:
    result = runner.invoke(
        agent_cli.app,
        ["admin", "call", "--resource", "cache", "--action", "clear"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "confirmation_required"
    assert fake_client.calls == []


def test_admin_confirmed_mutation_calls_registered_route(fake_client: FakeClient) -> None:
    result = runner.invoke(
        agent_cli.app,
        [
            "admin",
            "call",
            "--resource",
            "cache",
            "--action",
            "cleanup",
            "--query-json",
            '{"days":7}',
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    call = fake_client.calls[0]
    assert call["method"] == "DELETE"
    assert call["path"] == "/api/cache/cleanup"
    assert call["params"] == {"days": 7}


def test_report_download_writes_requested_file(
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "report.md"
    result = runner.invoke(
        agent_cli.app,
        [
            "reports",
            "download",
            "--report-id",
            "report-1",
            "--output",
            str(destination),
        ],
    )

    assert result.exit_code == 0
    assert destination.read_bytes() == b"report"
    payload = json.loads(result.stdout)
    assert payload["data"]["output"] == str(destination)


def test_profile_and_notifications_have_first_class_commands(fake_client: FakeClient) -> None:
    profile_result = runner.invoke(agent_cli.app, ["profile", "get"])
    notification_result = runner.invoke(
        agent_cli.app,
        ["notifications", "list", "--status", "unread"],
    )

    assert profile_result.exit_code == 0
    assert notification_result.exit_code == 0
    assert fake_client.calls[0]["path"] == "/api/auth/me"
    assert fake_client.calls[1]["path"] == "/api/notifications"
    assert fake_client.calls[1]["params"]["status"] == "unread"


def test_admin_routes_are_machine_readable() -> None:
    result = runner.invoke(agent_cli.app, ["admin", "routes"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] >= 50
    assert any(
        row["resource"] == "scheduler" and row["action"] == "trigger"
        for row in payload["data"]["routes"]
    )


def test_project_entrypoints_use_unified_cli() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

    assert 'agentctl = "cli.agent:main"' in pyproject
    assert 'tradingagents = "cli.agent:main"' in pyproject
    assert 'holdings = "cli.agent:holdings_main"' in pyproject
    assert 'tradingagents-interactive = "cli.main:main"' not in pyproject


def test_holdings_compatibility_entrypoint_keeps_unified_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    monkeypatch.setattr(agent_cli, "_run", lambda args=None: captured.extend(args or []))
    monkeypatch.setattr(
        agent_cli.sys,
        "argv",
        ["holdings", "list", "--pretty", "--username", "admin"],
    )

    agent_cli.holdings_main()

    assert captured == ["--pretty", "--username", "admin", "holdings", "list"]


def test_backend_image_installs_cli_after_copying_source() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.backend").read_text(
        encoding="utf-8"
    )

    assert "COPY cli ./cli" in dockerfile
    assert "COPY VERSION ./VERSION" in dockerfile
    assert dockerfile.index("COPY cli ./cli") < dockerfile.index("RUN pip install --no-deps .")
