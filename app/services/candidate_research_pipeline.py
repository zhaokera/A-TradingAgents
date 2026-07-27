"""Account-independent research pipeline used by Web and CLI candidates.

The legacy holdings module still owns the bounded market discovery builders.
Keeping that dependency behind this service prevents new callers from coupling
to the holdings CLI surface while those builders are extracted incrementally.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def run_candidate_research(
    *,
    external_risk_level: Optional[str] = None,
    excluded_code_reasons: Optional[Mapping[str, str]] = None,
    star_market_exclusion_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the shared, account-independent full-market research workflow."""

    # Lazy import keeps the heavy legacy CLI module outside API startup paths.
    from app.services.holdings_cli import run_public_full_market_research

    return run_public_full_market_research(
        external_risk_level=external_risk_level,
        excluded_code_reasons=excluded_code_reasons,
        star_market_exclusion_reason=star_market_exclusion_reason,
    )
