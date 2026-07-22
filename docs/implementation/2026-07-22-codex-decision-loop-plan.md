# Codex Decision Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an authenticated, deterministic `agentctl decision` workflow that turns current candidates, holdings, market state, evidence, and portfolio limits into auditable daily decisions and measured shadow outcomes.

**Architecture:** Add focused policy services beside the existing briefing and candidate services. `DailyDecisionService` composes their results into append-only snapshots; a bounded live poller records only post-decision observations and `DecisionReviewService` reads those immutable events for performance and calibration proposals. Existing candidate discovery remains the only full-market scanner, and the holdings page is unchanged.

**Tech Stack:** Python 3.10+, FastAPI, Motor/MongoDB, APScheduler, Typer/httpx CLI, pandas/numpy-compatible calculations, pytest/pytest-asyncio, Docker Compose.

---

## File Map

- Create `app/services/market_session_policy_service.py`: Shanghai session classification and quote freshness.
- Modify `app/services/a_share_calendar_service.py`: expose authoritative source and bounded cache freshness.
- Create `app/services/company_profile_enrichment_service.py`: field-level provider evidence and cache selection.
- Modify `app/services/stock_master_data_service.py`: expose selected evidence-backed profiles to candidates and holdings.
- Create `app/services/portfolio_diversification_service.py`: taxonomy exposure, history correlation, and board-lot allocation gates.
- Create `app/services/daily_decision_service.py`: deterministic classification, canonical hashes, and append-only snapshots.
- Create `app/services/decision_tracking_service.py`: stable plans, 15-second observations, minute aggregation, fills, and exits.
- Create `app/services/decision_review_service.py`: outcome metrics and bounded calibration proposals.
- Create `app/routers/decision.py`: authenticated today/history/performance endpoints.
- Modify `app/core/config.py`: independent decision-refresh/tracking flags and bounded symbol count.
- Modify `app/main.py`: router registration and bounded scheduler jobs.
- Modify `app/core/database.py`: unique snapshot, outcome, minute-bar, profile, and calibration indexes.
- Modify `pyproject.toml`: declare NumPy directly for deterministic ridge fitting.
- Modify `cli/agent.py`: `decision today`, `decision history`, and `decision performance` commands.
- Create focused tests under `tests/` for each service and the CLI/API contracts.
- Modify `docs/cli/README.md` and `docs/HERMES_HOLDINGS_CLI.md`: Codex/Hermes command contract.

### Task 1: Market Session and Quote Policy

**Files:**
- Create: `app/services/market_session_policy_service.py`
- Modify: `app/services/a_share_calendar_service.py`
- Modify: `app/core/config.py`
- Test: `tests/test_market_session_policy_service.py`

- [ ] **Step 1: Write failing boundary and freshness tests**

```python
@pytest.mark.parametrize(("clock", "phase"), [
    ("09:29:59", "pre_open"),
    ("09:30:00", "live_am"),
    ("11:30:00", "midday_break"),
    ("13:00:00", "live_pm"),
    ("15:00:00", "post_close"),
])
def test_session_boundaries(clock, phase): ...

def test_live_quote_requires_same_trade_date_and_at_most_90_seconds_old(): ...
async def test_calendar_fallback_is_calendar_unknown_and_fail_closed(): ...
async def test_calendar_cache_older_than_configured_168_hours_is_unknown(): ...
```

- [ ] **Step 2: Run `python -m pytest tests/test_market_session_policy_service.py -q` and confirm missing-module failure.**
- [ ] **Step 3: Extend `AShareCalendarService` results with source, `verified_at`, and `authoritative`; add `A_SHARE_CALENDAR_CACHE_MAX_AGE_HOURS=168`. Its weekday fallback and an older cached verification are not authoritative.**
- [ ] **Step 4: Implement `MarketSessionPolicyService.classify()` and `quote_status()` using `Asia/Shanghai`, the extended calendar contract, and exchange `trade_at`; any non-authoritative calendar result maps to `calendar_unknown`.**
- [ ] **Step 5: Run the focused test and confirm all boundary cases pass.**

### Task 2: Evidence-Backed Company Profiles

**Files:**
- Create: `app/services/company_profile_enrichment_service.py`
- Modify: `app/services/stock_master_data_service.py`
- Test: `tests/test_company_profile_enrichment_service.py`

- [ ] **Step 1: Write failing tests for per-field source priority, expiry, conflicts, and incomplete profiles.**

```python
async def test_tushare_industry_wins_while_akshare_fills_missing_business(): ...
async def test_unproven_local_text_is_display_only(): ...
async def test_provider_failure_never_creates_fallback_business_text(): ...
```

- [ ] **Step 2: Run `python -m pytest tests/test_company_profile_enrichment_service.py -q` and confirm failure.**
- [ ] **Step 3: Implement source adapters for only Tushare `stock_basic`/`stock_company`/`fina_mainbz`, BaoStock `query_stock_basic`, and AKShare `stock_individual_info_em`.**
- [ ] **Step 4: Persist source documents in `stock_company_profiles`; select each field independently with Tushare > BaoStock > AKShare and attach raw taxonomy plus `cn-sector-v1`.**
- [ ] **Step 5: Replace tuple anchors with explicit reviewed anchor records containing code, reviewer, evidence source, and fixed review time; exact-code anchors retain their tier, while name-only keyword matching is capped at `related`. Add a compatibility migration that reads old tuples as unreviewed and never promotes them to `core`.**
- [ ] **Step 6: Update `StockMasterDataService` to return selected evidence, completeness, conflicts, and provider errors without changing existing candidate keys.**
- [ ] **Step 7: Run profile tests plus `python -m pytest tests/test_ai_candidate_service.py tests/test_investment_policy.py -q`.**

### Task 3: Portfolio Diversification and Correlation

**Files:**
- Create: `app/services/portfolio_diversification_service.py`
- Modify: `app/services/investment_policy.py`
- Test: `tests/test_portfolio_diversification_service.py`

- [ ] **Step 1: Write failing tests for 35% theme, 40% broad sector, 30% industry, existing total-capital and total-loss budgets, exact 0.80 pairwise cap, board-lot reduction, and candidate ordering.**
- [ ] **Step 2: Add tests for 60 completed-session split-adjusted simple-return correlation, 40-overlap minimum, zero returns retained, empirical correlation rejected above 20% zero returns, and exact taxonomy fallbacks 1.00/0.85/0.50.**
- [ ] **Step 3: Add holding tests requiring positive-quantity holdings to have current valuation, provider sector, detailed industry, `quote_trade_at`, `valuation_phase`, and total-assets denominator; live quotes older than five minutes block every new allocation.**
- [ ] **Step 4: Run `python -m pytest tests/test_portfolio_diversification_service.py -q` and confirm failure.**
- [ ] **Step 5: Implement exposure ledgers that seed existing holdings, then evaluate candidates in stable rank/code order and reduce quantity to the largest legal board lot while reapplying shared capital and loss budgets.**
- [ ] **Step 6: Implement correlation using completed adjusted closes; return `correlation_basis`, compared symbols, overlap, value, exposure before/after, valuation audit fields, and exact reason codes with every decision.**
- [ ] **Step 7: Run diversification tests and existing investment-policy tests.**

### Task 4: Deterministic Daily Decisions and Snapshots

**Files:**
- Create: `app/services/daily_decision_service.py`
- Test: `tests/test_daily_decision_service.py`

- [ ] **Step 1: Write failing tests for exhaustive bucket precedence `avoid > wait > buy_now > condition_order` and every phase: `pre_open`, `live_am`, `midday_break`, `live_pm`, `post_close`, `closed_day`, and `calendar_unknown`.**
- [ ] **Step 2: Enumerate hard `avoid` reason codes (`plan_invalidated`, `plan_expired`, `target_reached`, `blocking_event`, `market_red`, `objective_mismatch`, `hard_data_failure`) and `wait` codes (`account_blocked`, `calendar_unknown`, `profile_incomplete`, `one_lot_unaffordable`, `holding_valuation_missing`, `holding_taxonomy_missing`, `concentration_limit`, `correlation_limit`, `loss_budget_exhausted`). Test every code and precedence collision.**
- [ ] **Step 3: Test `buy_now` only for fresh same-day Tencent quotes in `live_am/live_pm` with met price conditions; test all remaining valid allocated plans become `condition_order`, including pre-open, midday, post-close, closed day, unmet live triggers, and missing/stale live quotes. The last case carries `live_quote_recheck_required`, never unconditional `wait`.**
- [ ] **Step 4: Assert every item includes identity, action, reason codes, quote/trade time, short/swing/position plans, field-level profile evidence, allocation, exposure/correlation impact, planned loss, invalidation, source/policy versions, and `plan_id`.**
- [ ] **Step 5: Add canonicalization tests for the complete exclusion list (`decision_id`, `as_of`, `created_at`, `persisted_at`, `persistence`, field `retrieved_at`, transport `quote_checked_at`) and prove `trade_at`, source update time, report period, source identity, evidence value, policy, taxonomy, and fee versions remain material.**
- [ ] **Step 6: Add concurrent persistence tests for identical-hash reuse and different-hash revision allocation.**
- [ ] **Step 7: Implement packet composition from `DailyBriefingService` plus the latest candidate run, session policy, enriched profiles, and diversification result.**
- [ ] **Step 8: Implement Decimal `ROUND_HALF_UP`, stable list ordering, complete effective policy capture, and SHA-256 canonical JSON with an explicit path-aware sanitizer matching Step 5.**
- [ ] **Step 9: Implement a `job_locks` lease and append-only `daily_decisions` persistence with duplicate-key retry. Mongo persistence failure must raise `DecisionPersistenceError`.**
- [ ] **Step 10: Run `python -m pytest tests/test_daily_decision_service.py -q`.**

### Task 5: Authenticated Today/History API and CLI

**Files:**
- Create: `app/routers/decision.py`
- Modify: `app/main.py`
- Modify: `cli/agent.py`
- Test: `tests/test_decision_router.py`
- Modify: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing today/history router tests proving password-authenticated ownership and HTTP 503 on unaudited persistence failure.**
- [ ] **Step 2: Write CLI tests for the exact paths and options below.**

```text
GET /api/decision/today?refresh=true
GET /api/decision/history?limit=20
```

- [ ] **Step 3: Implement the router with `Depends(get_current_user)` and no caller-supplied user id.**
- [ ] **Step 4: Add the Typer `decision` group with `today --refresh/--no-refresh` and `history --limit`.**
- [ ] **Step 5: Run `python -m pytest tests/test_decision_router.py tests/test_agent_cli.py -q`.**

### Task 6: Post-Decision Shadow Plan Tracking

**Files:**
- Create: `app/services/decision_tracking_service.py`
- Test: `tests/test_decision_tracking_service.py`

- [ ] **Step 1: Write failing tests for stable `plan_id`, `origin_decision_id`/eligibility/bucket/phase, appended decision references, latest-preceding `trigger_context_*`, superseding an untriggered plan, and preserving an already active plan.**
- [ ] **Step 2: Add fill tests for pullback, breakout gap, invalidating stop gap, partial-minute exclusion, and conservative same-bar ordering.**
- [ ] **Step 3: Add exit/fee tests for configurable `cn_a_v1`, commission minimum, seller stamp duty, slippage, benchmark alignment, missing-alpha behavior, MAE/MFE, expiry, and pre/post-entry corporate actions.**
- [ ] **Step 4: Run `python -m pytest tests/test_decision_tracking_service.py -q` and confirm failure.**
- [ ] **Step 5: Implement append-only state transitions with compare-and-set sequence allocation, exact allowed transitions, revision attribution, versioned fee-policy snapshots, MAE/MFE, benchmark observations, and `metric_basis=shadow_trade_v1`.**
- [ ] **Step 6: Implement the distributed-lock bounded symbol poller: current holdings plus waiting/active plans only, Tencent every 15 seconds during live phases, and own-tick OHLC aggregation into closed one-minute bars.**
- [ ] **Step 7: Implement eligible-bar recovery rules and leave unprovable crossings as unobserved.**
- [ ] **Step 8: Run the focused tracking tests.**

### Task 7: Performance and Bounded Calibration

**Files:**
- Create: `app/services/decision_review_service.py`
- Modify: `app/routers/decision.py`
- Modify: `cli/agent.py`
- Modify: `pyproject.toml`
- Test: `tests/test_decision_review_service.py`
- Modify: `tests/test_decision_router.py`
- Modify: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing aggregation tests that exclude all `legacy_generated_baseline` rows and group only closed `shadow_trade_v1` outcomes.**
- [ ] **Step 2: Write threshold tests for fewer than 30 overall or fewer than 10 subgroup outcomes.**
- [ ] **Step 3: Write exact-value tests for deterministic folds, ridge deltas, +/-10% bounds, simplex total preservation, and proposal guardrails. Assert the strict feature allowlist is exactly `objective_match`, `reward_risk`, `evidence_completeness`, and `actionability`; injected capital, loss-budget, freshness, stop, and diversification fields are ignored.**
- [ ] **Step 4: Add NumPy as a direct dependency and implement history linkage, grouped performance, and deterministic ridge proposal generation over the newest 500 aligned-alpha outcomes from 180 days. Build the feature matrix only from the immutable four-key allowlist.**
- [ ] **Step 5: Persist inactive proposal versions and rollback targets; do not auto-activate them.**
- [ ] **Step 6: Add authenticated `GET /api/decision/performance` and `agentctl decision performance`; test ownership and exact CLI path.**
- [ ] **Step 7: Run `python -m pytest tests/test_decision_review_service.py tests/test_decision_router.py tests/test_agent_cli.py -q`.**

### Task 8: Database Indexes and Schedulers

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/core/database.py`
- Modify: `app/main.py`
- Test: `tests/test_decision_scheduler.py`

- [ ] **Step 1: Write tests asserting unique indexes, independent `DECISION_REFRESH_ENABLED`/`DECISION_TRACKING_ENABLED` flags, `DECISION_TRACKING_MAX_SYMBOLS`, scheduler IDs/options, and disabled-job rollback behavior.**
- [ ] **Step 2: Add unique decision revision/hash indexes, unique outcome sequence indexes, active-plan/profile/minute-bar query indexes, and calibration version indexes.**
- [ ] **Step 3: Register the five-minute live decision refresh first. Register the 15-second `max_instances=1`, `coalesce=True` observation poller only when its flag is enabled and startup readiness confirms indexes, lock access, and active symbol count within the configured bound; both self-disable outside applicable sessions.**
- [ ] **Step 4: Run scheduler/index tests and `python -m compileall app cli`.**

### Task 9: Documentation, Regression, and Docker Verification

**Files:**
- Modify: `docs/cli/README.md`
- Modify: `docs/HERMES_HOLDINGS_CLI.md`

- [ ] **Step 1: Document login once and the decision workflow without Docker-internal fallback language.**

```bash
.venv/bin/agentctl auth login --username admin
.venv/bin/agentctl --pretty decision today
.venv/bin/agentctl --pretty decision history --limit 20
.venv/bin/agentctl --pretty decision performance
```

- [ ] **Step 2: Run all focused tests from Tasks 1-8.**
- [ ] **Step 3: Run `python -m pytest -q` and report failures without weakening assertions.**
- [ ] **Step 4: Run `cd frontend && yarn type-check && yarn build` to prove the untouched frontend still compiles.**
- [ ] **Step 5: Run `docker compose build backend frontend` and `docker compose up -d`; verify backend health on port 8331 and frontend on port 3000.**
- [ ] **Step 6: Log in through `agentctl`, run briefing and all three decision commands, and verify Mongo has auditable snapshots with no duplicate same-hash revisions.**
- [ ] **Step 7: Review `git diff --check`, `git status --short`, and the exact diff before any final commit or push.**
