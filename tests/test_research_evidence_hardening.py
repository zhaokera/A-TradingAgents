from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.company_profile_enrichment_service import normalize_provider_sector, select_evidence_profile
from app.services.candidate_research_priority import research_account_fit, research_plan_account_fit, research_account_priority
from app.services.daily_structured_analysis import apply_daily_analysis_execution_gate
from app.services.global_macro_risk_service import GlobalMacroRiskService
from app.services.premarket_intelligence_service import PremarketIntelligenceService
from app.services.public_candidate_discovery_service import rank_public_candidate_universe
from app.services.public_candidate_notice_review import review_public_candidate_notices


@pytest.mark.parametrize("raw, expected", [
    ("互联网和相关服务", "信息技术"),
    ("游戏Ⅱ", "传媒"),
    ("游戏", "传媒"),
])
def test_explicit_provider_taxonomy_aliases_keep_provenance(raw, expected):
    result = normalize_provider_sector(raw)
    assert result is not None
    assert result["value"] == expected
    assert result["raw_taxonomy_value"] == raw


def test_taxonomy_does_not_guess_from_stock_name():
    assert normalize_provider_sector("恺英网络") is None


def test_authoritative_profile_reclassifies_without_overwriting_business_or_conflicts():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    source = {"code": "002517", "source": "cninfo", "source_endpoint": "stock_profile_cninfo",
              "source_record_key": "002517:stock_profile_cninfo", "retrieved_at": now,
              "industry": "互联网和相关服务", "main_business": "游戏业务"}
    profile = select_evidence_profile("002517", [source, {
        **source, "source": "akshare", "source_endpoint": "stock_individual_info_em",
        "source_record_key": "002517:stock_individual_info_em", "industry": "游戏Ⅱ",
    }], now=now)
    assert profile["data_quality"]["decision_critical_complete"] is True
    assert profile["main_business"] == "游戏业务"
    assert profile["provider_sector_evidence"]["raw_taxonomy_value"] == "互联网和相关服务"
    assert profile["data_quality"]["profile_conflicts"]


def test_plan_risk_recheck_does_not_relax_stop_budget():
    definition = {"research_account_fit": research_account_fit(20, "600001", {
        "total_assets": 10000, "available_cash": 10000})}
    fit = research_plan_account_fit({"price_plan": {"entry_price": 20, "stop_price": 18}}, definition)
    assert fit["one_lot_stop_loss"] == 200
    assert fit["status"] == "one_lot_above_stop_budget"
    assert research_account_priority({"research_account_fit": fit}) == 1
    assert fit["execution_authorized"] is False


def test_raw_guarded_plan_uses_same_risk_budget_before_normalization():
    definition = {"research_account_fit": research_account_fit(20, "600001", {
        "total_assets": 10000, "available_cash": 10000})}
    fit = research_plan_account_fit({"guarded_price_plan": {
        "suggested_buy_price": 20, "stop_loss_price": 18}}, definition)
    assert fit["status"] == "one_lot_above_stop_budget"


def test_research_account_audit_survives_public_cli_sanitizer():
    from app.services.holdings_cli import _sanitize_public_deep_check_candidate
    raw = {"code": "600001", "research_account_fit": {
        "status": "requires_plan_review", "one_lot_amount": 1000,
        "execution_authorized": True, "private_data": "not-public"}}
    result = _sanitize_public_deep_check_candidate(raw)
    assert result["research_account_fit"]["one_lot_amount"] == 1000
    assert result["research_account_fit"]["execution_authorized"] is False
    assert "private_data" not in result["research_account_fit"]


def test_account_budget_survives_discovery_normalization_into_plan_review():
    from app.services.holdings_cli import (
        _normalize_public_discovery_definitions, _sanitize_public_deep_check_candidate,
    )
    raw = {"code": "600001", "research_account_fit": research_account_fit(40, "600001", {
        "total_assets": 10000, "available_cash": 10000, "current_exposure_pct": 0,
    })}
    raw["research_account_fit"]["private_data"] = "must_not_escape"
    definition = _normalize_public_discovery_definitions([raw])[0]
    plan = {"guarded_price_plan": {"suggested_buy_price": 39, "stop_loss_price": 38.5}}
    audit = research_plan_account_fit(plan, definition)
    candidate = _sanitize_public_deep_check_candidate({"code": "600001", "research_account_fit": audit})
    assert candidate["research_account_fit"]["status"] == "one_lot_above_research_ceiling"
    assert candidate["research_account_fit"]["maximum_new_amount"] == 3000
    assert candidate["research_account_fit"]["one_lot_amount"] == 3900
    assert research_account_priority(candidate) == 1
    assert "private_data" not in definition["research_account_fit"]


def test_formal_research_prioritizes_account_fit_before_distance():
    from app.services.daily_decision_service import _select_formal_research_candidates
    candidates = [
        {"code": "600001", "price_plan": {"distance_to_entry_pct": 0},
         "research_account_fit": {"status": "one_lot_above_research_ceiling"}},
        {"code": "600002", "price_plan": {"distance_to_entry_pct": 1},
         "research_account_fit": {"status": "requires_plan_review"}},
    ]
    selected, audit = _select_formal_research_candidates(candidates, capacity=1)
    assert [item["code"] for item in selected] == ["600002"]
    assert len(audit) == 2


def test_structured_sources_survive_cli_sanitizer():
    from app.services.holdings_cli import _sanitize_public_deep_check_candidate
    stages = {"technical": {"status": "passed", "source": "tencent_daily_bars"},
              "earnings": {"status": "clear", "source": "earnings-provider"},
              "notice": {"status": "unavailable", "source": "notice-provider", "error_type": "SSLError"}}
    result = _sanitize_public_deep_check_candidate({"code": "600001", "structured_review": stages})
    for stage in stages:
        assert result["structured_review"][stage]["source"] == stages[stage]["source"]
    assert result["structured_review"]["notice"]["error_type"] == "SSLError"


@pytest.mark.parametrize("code", ["notice_evidence_unavailable", "notice_risk_blocked"])
def test_blocking_notice_flag_survives_public_and_candidate_normalization(code):
    from app.services.holdings_cli import _sanitize_public_deep_check_candidate
    from app.services.ai_candidate_service import _normalize_risk_flags
    raw = {"code": "600001", "risk_flags": [{"code": code, "severity": "blocked", "message": "test"}]}
    clean = _sanitize_public_deep_check_candidate(raw)
    flags = _normalize_risk_flags(clean["risk_flags"])
    assert flags[0]["code"] == code
    assert flags[0]["severity"] == "blocked"


def test_afternoon_still_uses_same_a_share_overnight_window():
    now = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)
    macro = {"status": "ok", "snapshot": {"semiconductor": 100, "semiconductor_change_pct": 1,
        "asset_evidence": {"semiconductor": {"data_at": "2026-09-08", "session_date": "2026-09-08",
                                              "time_semantics": "daily_bar_session_date"}}}}
    items, _ = PremarketIntelligenceService._cross_assets(macro, checked_at=now, expires_at=now + timedelta(minutes=30))
    item = next(i for i in items if i["key"] == "semiconductor")
    assert item["status"] == "ok"
    assert item["overnight_signal_usable"] is True


def test_daily_coverage_does_not_veto_individually_complete_research():
    good = {"code": "600001", "execution_actionable": True,
            "condition_order_ready": False, "execution_status": "ready"}
    bad = {**good, "code": "600002"}
    doc = {
        "candidates": [good, bad],
        "execution": {"actionable": False, "requires_daily_decision": True},
        "daily_structured_analysis": {
            "minimum_met": False, "trade_date": "2026-09-08",
            "items": [
                {"code": "600001", "status": "completed"},
                {"code": "600002", "status": "incomplete",
                 "missing_reasons": ["notice_evidence_unavailable"]},
            ],
        },
    }
    apply_daily_analysis_execution_gate(doc)
    assert good["execution_actionable"] is True
    assert bad["execution_actionable"] is False
    assert bad["execution_status"] == "candidate_structured_analysis_incomplete"
    assert doc["research_coverage"]["status"] == "below_target"
    assert doc["execution"]["actionable"] is False


def test_missing_individual_audit_is_not_authorized_by_other_completions():
    doc = {"candidates": [{"code": "600001", "execution_actionable": True}],
           "daily_structured_analysis": {"minimum_met": True, "items": []}}
    apply_daily_analysis_execution_gate(doc)
    assert doc["candidates"][0]["execution_actionable"] is False


def test_yfinance_daily_bar_preserves_actual_session_date(monkeypatch):
    frame = pd.DataFrame({"Close": [100.0, 103.37]},
                         index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        SimpleNamespace(download=lambda *a, **k: frame))
    snapshot = GlobalMacroRiskService._fetch_with_yfinance()
    evidence = snapshot["asset_evidence"]["semiconductor"]
    assert evidence["session_date"] == "2026-09-04"
    assert evidence["time_semantics"] == "daily_bar_session_date"
    assert evidence["data_at"] == "2026-09-04"


@pytest.mark.parametrize("session_date, expected", [
    ("2026-09-04", "prior_session"),
    ("2026-09-09", "future_data"),
    (None, "time_unverified"),
])
def test_cross_assets_never_relabel_old_or_unknown_quotes_as_overnight(session_date, expected):
    now = datetime(2026, 9, 8, 1, 30, tzinfo=timezone.utc)
    evidence = {"session_date": session_date, "data_at": session_date,
                "time_semantics": "daily_bar_session_date"}
    macro = {"checked_at": now.isoformat(), "snapshot": {
        "semiconductor": 100.0, "semiconductor_change_pct": 3.37,
        "asset_evidence": {"semiconductor": evidence},
    }}
    items, _ = PremarketIntelligenceService._cross_assets(
        macro, checked_at=now, expires_at=now + timedelta(minutes=30))
    item = next(i for i in items if i["key"] == "semiconductor")
    assert item["data_at"] == session_date
    assert item["status"] == expected
    assert item["impact"]["signal"] == "neutral"
    assert item["overnight_signal_usable"] is False


def test_research_ranking_accounts_for_board_lot_concentration_before_cutoff():
    rows = [
        {"code": code, "name": "电子", "exchange": "SZ", "trade_date": "2026-09-08",
         "close": price, "pct_chg": 2.0, "amount": amount}
        for code, price, amount in [("000021", 35.4, 900000000), ("002185", 16.1, 500000000)]
    ]
    result = rank_public_candidate_universe(
        rows, benchmark_trade_date="2026-09-08", limit=1,
        account_context={"total_assets": 10685.41, "available_cash": 10685.41,
                         "holding_values": {}, "current_exposure_pct": 0},
    )
    assert result["definitions"][0]["code"] == "002185"
    audit = result["definitions"][0]["research_account_fit"]
    assert audit["status"] == "requires_plan_review"
    assert audit["execution_authorized"] is False
    assert audit["one_lot_amount"] == 1610.0
    assert audit["maximum_new_amount"] == pytest.approx(3205.623)


def test_notice_transient_disconnect_retries_without_treating_unknown_as_clear(monkeypatch):
    from requests.exceptions import SSLError
    calls = {}
    def loader(day):
        calls[day] = calls.get(day, 0) + 1
        if day == "20260908" and calls[day] == 1:
            raise SSLError("must never appear in audit")
        return []
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = review_public_candidate_notices(["600001"], as_of_date="2026-09-08", loader=loader)
    assert result["status"] == "ok"
    assert calls["20260908"] == 2
    audit = result["provider_attempts"][-1]
    assert audit == {"date": "2026-09-08", "attempt_count": 2, "error_types": ["SSLError"]}
    assert "must never appear" not in str(result)


def test_notice_persistent_tls_failure_stays_unavailable(monkeypatch):
    from requests.exceptions import SSLError
    calls = []
    def loader(day):
        calls.append(day)
        raise SSLError("private transport detail")
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = review_public_candidate_notices(["600001"], as_of_date="2026-09-08", loader=loader)
    assert result["status"] == "notice_source_unavailable"
    assert result["results"] == []
    assert len(calls) == 14
