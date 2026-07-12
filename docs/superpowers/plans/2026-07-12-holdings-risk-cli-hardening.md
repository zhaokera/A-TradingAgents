# Holdings Risk and CLI Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make holdings and Hermes CLI outputs depend on fresh Tencent market data, fresh reports, explicit external-risk gates, fee-aware RR, and risk-based position sizing.

**Architecture:** Extend the Tencent adapter with provider-time freshness and daily bars, isolate deterministic technical/risk calculations in a focused service, then integrate those contracts into report advice and the existing holdings CLI. Preserve compatibility fields while forcing non-actionable states to produce zero suggested lots.

**Tech Stack:** Python 3.10, FastAPI, Typer, PyMongo/Motor, Requests, AKShare/Pandas, Pytest.

---

### Task 0: Preserve the Dirty-Worktree Baseline

**Files:**
- Record only: current `git status --short`, `git diff --stat`, and hashes for already modified files.
- Preserve and regression-test: `app/routers/paper.py`, `app/routers/stocks.py`, `app/services/quotes_service.py`, `app/services/simple_analysis_service.py`, `app/services/holdings_cli.py`, and all current frontend edits.

- [ ] Capture the current branch, status, diff stat, and focused baseline test output before production edits.
- [ ] Keep frontend files untouched and never revert existing user changes.
- [ ] Include Tencent-consumer tests for paper price lookup, stocks quotes, and `QuotesService` fallback behavior in final verification.

### Task 1: Tencent Market Time and Daily Bars

**Files:**
- Modify: `app/services/tencent_quote_service.py`
- Modify: `tests/test_tencent_quote_service.py`

- [ ] Add failing tests for `trade_at`, `received_at`, malformed/future provider time, exact five-minute and +60-second boundaries, Friday quotes during a Monday session, off-session research-only status, and display-only fallback status.
- [ ] Run `tests/test_tencent_quote_service.py` and confirm the new assertions fail for missing freshness behavior.
- [ ] Implement provider timestamp normalization and a pure quote-freshness evaluator.
- [ ] Add failing tests for exact `stock_zh_a_hist_tx(symbol, start_date, end_date, adjust="qfq")` parameters, invalid-row removal, duplicate-date replacement, sorting, 60-row minimum, same-day quote replacement, next-session append, and 25% adjustment-scale rejection.
- [ ] Add a lazy AKShare `stock_zh_a_hist_tx` fetcher with exchange-prefixed symbols, 60-row validation, normalized OHLCV rows, benchmark session dates, and structured failure states.
- [ ] Re-run the focused tests until green.

### Task 2: Technical Price Plan and Report Freshness

**Files:**
- Create: `app/services/holding_price_guardrails.py`
- Create: `tests/test_holding_price_guardrails.py`
- Modify: `app/services/holding_ai_advice.py`
- Modify: `app/routers/holdings.py`
- Modify: `app/services/simple_analysis_service.py`
- Modify: `tests/test_holding_ai_advice.py`
- Create or modify: `tests/test_report_persistence.py`

- [ ] Add failing pure tests for exact MA/Bollinger/recent-low/high levels, sample-deviation Bollinger bounds, Decimal floor/ceiling/half-up buffers, resistance 1/2/3 mapping, adjustment-scale rejection, invalid price ordering, RR, Friday-through-Monday validity, Tuesday expiry, weekday calendar fallback metadata, 10% report-field validation, and a manual-stop-only plan completed by validated report/technical fields.
- [ ] Implement deterministic technical-level and report-freshness helpers.
- [ ] Add failing advice/API tests proving stale report prices are visible only as history, cannot populate actionable price fields, and the holdings response preserves the full quote snapshot and structured failure reason.
- [ ] Preserve a shared quote snapshot in the holdings API and integrate freshness metadata and guarded technical/report plans into `build_holding_report_advice()`.
- [ ] Add a failing report-persistence test, then persist the requested/result market-data date for a newly generated backdated report.
- [ ] Run `tests/test_holding_price_guardrails.py`, `tests/test_holding_ai_advice.py`, the holdings router tests, and `tests/test_report_persistence.py` until green.

### Task 3: CLI Quote Metadata and Guarded Holdings Plans

**Files:**
- Modify: `app/services/holdings_cli.py`
- Modify: `tests/test_cli_holdings.py`

- [ ] Add failing tests proving `list/get` expose quote freshness and stale reports do not become active report rows.
- [ ] Preserve full Tencent quote metadata when resolving current prices.
- [ ] Integrate guarded report/technical plans while keeping manual prices authoritative.
- [ ] Confirm old response fields remain present and focused tests pass.

### Task 4: External Risk, Fees, RR, and Risk Sizing

**Files:**
- Modify: `app/services/holdings_cli.py`
- Modify: `tests/test_cli_holdings.py`

- [ ] Add failing tests for green 20%, yellow 12%, omitted/red/unknown 0%, invalid-level JSON errors, configured priority order, actionable-equity fail-closed behavior, shared plan budgets, separate 35%-of-cash candidate/20%-of-equity post-trade-symbol/50%-of-cash initial caps, plan-wide 0.75% account-loss allocation, RR 1.5, and one-lot rejection.
- [ ] Add failing fee tests for 0.03%/¥5 commission, 0.05% sell stamp duty, 0.001% transfer fee, 5bp two-sided slippage, cent rounding, nonlinear whole-lot evaluation, and buy-cost cash rejection.
- [ ] Implement pure A-share fee estimates and risk-based lot sizing.
- [ ] Add `--external-risk-level` to opportunities and include the structured gate in JSON.
- [ ] Remove the A-share `--lot-size` override, replace fixed-one-lot logic with a whole-lot nonlinear fee/risk loop, return every failed constraint while retaining compatibility `risk_gate`, and bump the opportunity schema version.
- [ ] Run focused CLI tests until green.

### Task 5: Trade Timestamp and Sale Accounting

**Files:**
- Modify: `app/services/holdings_cli.py`
- Modify: `tests/test_cli_holdings.py`

- [ ] Add failing tests for canonical UTC `sold_at`, persisted BSON `effective_at`, effective business-time ordering before `limit`, malformed timestamps, non-zero fees, and net proceeds.
- [ ] Normalize sale timestamps, store `effective_at`, and sort recent/history records by business time with compatibility fallback.
- [ ] Expose gross proceeds, net proceeds, cost basis, total fees, and realized PnL.
- [ ] Run focused CLI tests until green.

### Task 6: Installable CLI Contract

**Files:**
- Modify: `pyproject.toml`
- Create or modify: `tests/test_holdings_cli_entrypoint.py`

- [ ] Add failing metadata/runner tests for the `holdings` console entry point, root module support, packaged `app`, packaged `cli`, and included `app/LICENSE`.
- [ ] Add direct `typer` dependency, installable script, package discovery, root module declaration, and proprietary license package data.
- [ ] Add a failing command test, then implement structured `mongo_config_required` enforcement for installed data commands without explicit Mongo configuration while retaining repository `.env` discovery.
- [ ] Build a wheel in a temporary directory and verify both help entry forms, wheel contents, missing-config JSON failure, repository `.env` compatibility, and one configured data command outside the checkout.

### Task 7: Integration Verification

**Files:**
- Review all files changed above.

- [ ] Run focused holdings/CLI/Tencent tests.
- [ ] Run existing holding-analysis and portfolio-target tests.
- [ ] Run Tencent-consumer regression tests for paper, stocks, and `QuotesService` without modifying their existing user changes.
- [ ] Run `python -m compileall` for changed service and test modules.
- [ ] Inspect `git diff --check` and `git diff` for unrelated edits.
- [ ] Do not rebuild or restart Docker unless the user explicitly asks.
