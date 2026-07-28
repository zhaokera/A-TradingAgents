import os
import sys
from pathlib import Path

import pytest

# 将项目根目录加入 sys.path，确保 `import tradingagents` 可用
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# These files target APIs removed before the current architecture, or execute
# database/network diagnostics during import. They remain in git as historical
# migration evidence and are intentionally outside normal pytest collection.
collect_ignore = [
    "test_akshare_debug.py",
    "test_akshare_priority.py",
    "test_amount_fix.py",
    "test_dashscope_token_tracking.py",
    "test_data_config_cli.py",
    "test_financial_data_validation.py",
    "test_finnhub_news_fix.py",
    "test_news_timeout_fix.py",
    "test_query.py",
    "test_user_check.py",
    "test_tushare_unified/test_tushare_provider.py",
    "unit/dataflows/test_unified_dataframe.py",
]


MAINTAINED_DIRECTORIES = {
    "config",
    "dataflows",
    "middleware",
    "services",
    "system",
    "tradingagents",
}

MAINTAINED_ROOT_TESTS = {
    "test_a_share_market_regime.py",
    "test_agent_cli.py",
    "test_agent_client.py",
    "test_analysis_runtime_contract.py",
    "test_ai_candidate_service.py",
    "test_auth_cli_session.py",
    "test_candidate_discovery_service.py",
    "test_company_profile_enrichment_service.py",
    "test_daily_decision_service.py",
    "test_decision_review_service.py",
    "test_decision_proposal_service.py",
    "test_decision_router.py",
    "test_decision_scheduler.py",
    "test_decision_tracking_service.py",
    "test_cli_holdings.py",
    "test_database_service_paths.py",
    "test_holding_ai_advice.py",
    "test_holding_analysis.py",
    "test_holding_price_guardrails.py",
    "test_holdings_research_router.py",
    "test_holdings_cli_entrypoint.py",
    "test_investment_policy.py",
    "test_logging_manager.py",
    "test_market_session_policy_service.py",
    "test_portfolio_diversification_service.py",
    "test_portfolio_target_analysis.py",
    "test_premarket_intelligence_service.py",
    "test_product_optimization_contracts.py",
    "test_public_candidate_discovery_service.py",
    "test_stocks_kline_contract.py",
    "test_public_candidate_pipeline.py",
    "test_public_candidate_pipeline_contract.py",
    "test_public_candidate_pipeline_integration.py",
    "test_tencent_quote_service.py",
}

LEGACY_LIVE_FILES = {
    "dataflows/test_realtime_metrics.py",
    "services/test_quotes_backfill.py",
    "services/test_quotes_ingestion_and_enrichment.py",
    "system/test_config_summary.py",
    "tradingagents/test_app_cache_toggle.py",
}


def pytest_collection_modifyitems(config, items):
    tests_root = Path(__file__).parent.resolve()
    for item in items:
        path = Path(str(item.fspath)).resolve()
        try:
            relative = path.relative_to(tests_root)
        except ValueError:
            continue
        parts = relative.parts
        if relative.as_posix() in LEGACY_LIVE_FILES:
            item.add_marker(pytest.mark.live)
            continue
        maintained = (
            (len(parts) > 1 and parts[0] in MAINTAINED_DIRECTORIES)
            or (len(parts) > 2 and parts[:2] == ("unit", "tools"))
            or relative.name in MAINTAINED_ROOT_TESTS
        )
        if not maintained:
            item.add_marker(pytest.mark.live)
