# Codex Decision Loop Design

## Status

Approved approach: deterministic decision packet plus Codex synthesis.

The product owns data collection, freshness checks, hard risk gates, portfolio
allocation, audit history, and outcome measurement. Codex reads the packet and
produces the final daily conclusion. The backend does not add another LLM call.

## Goals

1. Add `agentctl decision today` with four stable buckets: `buy_now`,
   `condition_order`, `wait`, and `avoid`.
2. Enrich candidate profiles with evidence-backed industry, business scope, and
   revenue composition.
3. Separate pre-open, live-session, midday break, and post-close semantics.
4. Constrain allocation by industry, objective segment, and recent return
   correlation.
5. Grow true-trigger shadow-trade samples without mixing generated-price
   baselines into performance metrics.
6. Persist every decision packet and close the loop from decision to outcome and
   bounded ranking calibration.

## Non-Goals

- No broker order placement.
- No second in-product LLM that competes with Codex.
- No automatic ranking changes from small samples.
- No changes to the holdings analysis page.
- No fabricated company profile when an upstream source is unavailable.

## Architecture

### Services

- `CompanyProfileEnrichmentService`: reads only locally cached fields with an
  allowed provenance record, then enriches stale or missing fields through
  configured providers. Every field carries source endpoint, source record key,
  report period where applicable, retrieval time, and source update time.
- `MarketSessionPolicyService`: maps China market time and trading calendar to
  `pre_open`, `live_am`, `midday_break`, `live_pm`, `post_close`, or
  `closed_day`, then applies quote freshness requirements.
- `PortfolioDiversificationService`: calculates industry and objective-segment
  exposure and a 60-session return-correlation matrix. Missing price history
  falls back to taxonomy concentration.
- `DailyDecisionService`: consumes `DailyBriefingService`, applies deterministic
  gates, creates the four decision buckets, and persists an immutable snapshot.
- `DecisionReviewService`: links snapshots to shadow-trade outcomes and produces
  grouped performance and calibration recommendations.

The candidate research service remains responsible for discovering and tracking
candidates. The decision service is a read-model and policy layer; it must not
run a second full-market scan.

## Decision Contract

`GET /api/decision/today?refresh=true` returns:

```json
{
  "decision_id": "...",
  "as_of": "...",
  "market_session": {
    "phase": "pre_open",
    "is_trading_day": true,
    "quote_freshness_required_seconds": 0
  },
  "account": {},
  "market": {},
  "portfolio_constraints": {},
  "summary": {
    "buy_now_count": 0,
    "condition_order_count": 2,
    "wait_count": 2,
    "avoid_count": 1
  },
  "buy_now": [],
  "condition_order": [],
  "wait": [],
  "avoid": [],
  "data_quality": {},
  "rule_version": "decision-v1"
}
```

Each item includes candidate identity, action, reason codes, current quote and
timestamp, short/swing/position plans, evidence-backed company profile,
allocation, concentration impact, planned loss, and invalidation conditions.

The endpoint is authenticated with `Depends(get_current_user)` and every query
and unique key includes `current_user["id"]`. It cannot accept a caller-supplied
user id.

### Deterministic Bucket Rules

Every candidate appears in exactly one bucket. Rules execute in this precedence:

1. `avoid` hard gates;
2. `wait` data, affordability, and diversification gates;
3. `buy_now` live-session entry test;
4. `condition_order` for every remaining valid plan.

- `buy_now`: live trading phase only; Tencent quote passes freshness; price
  condition is met; market and candidate gates pass; evidence is sufficient;
  allocation remains valid after concentration checks.
- `condition_order`: price plan is valid and allocated, but the entry condition
  is not met or the market is outside the live phase. The packet states that a
  live quote recheck is required at trigger time.
- `wait`: candidate is researchable but currently lacks one executable
  condition, one-lot affordability, evidence completeness, or portfolio room.
- `avoid`: plan invalidated/expired, blocking event, red market gate, objective
  mismatch, or hard data-quality failure.

The system never upgrades a stale quote to `buy_now`. Pre-open and post-close
packets cannot contain `buy_now` items.

All numeric normalization uses decimal arithmetic with `ROUND_HALF_UP`: money
and percentages use two decimals and prices use the exchange tick precision.
Items are ordered by allocation rank, descending candidate rank score, then
six-digit code. Revenue components sort by report period descending, composition
type, then item name; conflicts and provider errors sort by field, source
priority, endpoint, then error code. The packet captures the complete effective
policy and provider versions.

`material_hash` is SHA-256 over canonical UTF-8 JSON with sorted keys and compact
separators. The only excluded paths are top-level `decision_id`, `as_of`,
`created_at`, `persisted_at`, and `persistence`, plus field-level `retrieved_at`
and transport-level `quote_checked_at`. Exchange `trade_at`, source update time,
report period, evidence values, and source identity remain material. The same
material input must produce the same buckets, item order, numeric values, and
hash.

## Company Evidence

Evidence is field-level rather than record-level:

```json
{
  "provider_sector": {
    "value": "信息技术",
    "raw_taxonomy_value": "计算机",
    "normalization_version": "cn-sector-v1",
    "source": "tushare",
    "source_endpoint": "stock_basic",
    "source_record_key": "000001.SZ",
    "source_updated_at": "...",
    "retrieved_at": "..."
  },
  "industry": {
    "value": "计算机设备",
    "source": "tushare",
    "source_endpoint": "stock_basic",
    "source_record_key": "000001.SZ",
    "source_updated_at": "...",
    "retrieved_at": "..."
  },
  "main_business": {
    "value": "...",
    "source": "tushare",
    "source_endpoint": "stock_company",
    "source_record_key": "000001.SZ",
    "source_updated_at": "...",
    "retrieved_at": "..."
  },
  "revenue_composition": {
    "items": [],
    "source": "tushare",
    "source_endpoint": "fina_mainbz",
    "source_record_key": "000001.SZ:20251231:P",
    "report_period": "...",
    "retrieved_at": "..."
  }
}
```

The allowed evidence endpoints are Tushare `stock_basic`, `stock_company`, and
`fina_mainbz`; BaoStock `query_stock_basic`; and AKShare
`stock_individual_info_em`. Source
priority is Tushare, BaoStock, then AKShare. Each field independently selects the
first valid, unexpired value in that order; lower-priority fields fill only a
missing field and are never concatenated into a higher-priority value. A local
value without an
allowed `source`, `source_endpoint`, and retrieval timestamp is display-only and
cannot satisfy an evidence gate. Company/industry fields expire after 30 days;
revenue composition is valid when it is the latest available report period and
not older than 550 days. A fresher lower-priority value cannot override a valid
Tushare value; conflicts are retained in `data_quality.profile_conflicts`.
Provider errors are recorded in `data_quality`; they do not invent fallback
text. A name-only match cannot produce `objective_tier=core` unless the stock is
an explicitly reviewed anchor whose anchor record includes reviewer, evidence
source, and review time.

Enriched profiles are cached in `stock_company_profiles` with source-specific
documents and freshness windows. Candidate payloads use a selected merged view
while retaining evidence provenance.

## Session and Quote Policy

All boundaries use `Asia/Shanghai` and the configured A-share trading calendar:

- `pre_open`: `[00:00, 09:30)`, including call auction; use the last completed
  trading-session close for planning and never emit `buy_now`.
- `live_am`: `[09:30, 11:30)` and `live_pm`: `[13:00, 15:00)`; a Tencent exchange
  `trade_at` from the current trading date and no more than 90 seconds behind the
  classification time is required for `buy_now`.
- `midday_break`: `[11:30, 13:00)`; preserve the last morning quote but do not
  upgrade a condition until a `live_pm` quote arrives.
- `post_close`: `[15:00, 24:00)`; treat a Tencent quote timestamped at or after
  `15:00:00` on the current trading date as the final-close observation and generate
  next-session condition plans.
- `closed_day`: preserve plans and mark the next valid trading session.

Freshness is calculated from Tencent's exchange `trade_at`, never HTTP fetch
time. If the authoritative calendar cannot be loaded and no unexpired cached
calendar is available, the phase becomes `calendar_unknown` and all candidates
are at most `wait`. Boundary times are tested exactly.

`decision today --refresh` refreshes candidate quotes before classification.
The five-minute scheduler refreshes active runs and appends a new decision
snapshot revision during live sessions only when material state changes. It does
not mutate an existing snapshot or create duplicate revisions for the same hash.

## Diversification and Correlation

Allocation adds four shared limits:

- internal objective-theme exposure cap: 35% of total assets;
- normalized provider-sector exposure cap: 40% of total assets;
- detailed-industry exposure cap: 30% of total assets;
- pairwise 60-session return correlation cap: 0.80 for simultaneous new
  allocations.

The objective segment is explicitly an internal theme taxonomy, not an exchange
or provider sector. `provider_sector` is a broad, versioned normalization of the
authoritative provider taxonomy; `industry` retains the narrower provider value.
Existing holdings are mapped by the same versioned taxonomy service, valued with
the same quote session as the decision, and count against all caps. The
correlation check compares each new candidate against every valued holding and
every higher-ranked new allocation.

Correlation uses split-adjusted daily close-to-close simple returns for the most
recent 60 completed sessions, with at least 40 overlapping observations. Zero
returns are retained for suspensions; if either series has more than 20% zero
returns or fewer than 40 overlaps, empirical correlation is unavailable.
Taxonomy fallback assigns `1.00` to the same detailed industry, `0.85` to the
same internal theme, and `0.50` otherwise, and reports
`correlation_basis=taxonomy_fallback`.

Holding valuation includes `quote_trade_at`, `valuation_phase`, and total-assets
denominator. Any positive-quantity holding with a missing valuation, provider
sector, or detailed industry blocks all new allocations because neither its
weight nor both concentration limits can be proved. During live phases, any
holding quote older than five minutes does the same. Rejected allocations retain
their plan but move to `wait` with an exact reason code.

## Decision Snapshots

Collection: `daily_decisions`.

Key fields:

- `user_id`, `decision_date`, `market_phase`, `revision`, `material_hash`;
- `candidate_run_id`, `briefing_as_of`, `rule_version`;
- complete bucket payloads and data-quality status;
- creation and refresh timestamps;
- no mutable outcome field; outcomes live in a separate collection.

Snapshots are append-only. The unique identities are
`(user_id, decision_date, market_phase, revision)` and
`(user_id, decision_date, market_phase, material_hash)`. A short Mongo lease in
`job_locks` serializes GET and scheduler persistence. The lease holder allocates
`revision=max(existing)+1`. A caller that loses the lease waits for the holder,
then returns an existing snapshot only when its hash matches; for a different
hash it reacquires the lease, recomputes against current inputs, and allocates a
new revision. A duplicate revision or hash retries this algorithm and never
returns a materially different winner. Materially identical refreshes reuse the
existing snapshot. Outcomes are appended to `decision_outcomes` and reference
`decision_id`; neither refresh nor review rewrites a decision snapshot.

## Shadow Trades and Review

Each candidate plan has a stable `plan_id`, hashed from user, candidate run,
code, entry strategy/prices, expiry, allocation, and rule version. Its tracking
record stores `origin_decision_id`, `eligibility_at`, origin action bucket, and
origin market phase. Repeated revisions referencing the same `plan_id` append
their decision ids without creating another trade. At trigger time the tracker
stores `trigger_context_decision_id`, action bucket, and market phase from the
latest still-valid revision preceding the observation. A changed plan supersedes
a waiting plan as `superseded_untriggered`; an active trade remains attached to
its original plan until exit.

During trading sessions, a distributed-lock poller fetches Tencent last-trade
snapshots every 15 seconds only for active plans and current holdings. It also
persists closed one-minute bars for gap recovery; the full candidate and
decision refresh remains every five minutes. Polling is disabled outside trading
sessions.

Shadow eligibility begins with the first Tencent last-trade observation whose
exchange timestamp is strictly later than `eligibility_at`. Session high and low
are not used because they may contain pre-decision prices. For pre-open plans,
same-day one-minute bars beginning at `09:30` are eligible after they close. For
live-created plans, only a closed minute whose interval start is at or after
`eligibility_at` is eligible; the partial minute containing the decision is
excluded. A crossing that cannot be established by a post-decision last-trade
snapshot or an eligible closed minute remains unobserved rather than being
backfilled from an unsafe interval. Quantity comes from the diversified
portfolio allocation.

For a pullback, an open between stop and entry fills at the open; otherwise a
cross fills at entry. For a breakout, a gap above entry fills at the open;
otherwise a cross fills at entry. Buy slippage is then applied. An open at or
through the stop invalidates an unfilled plan. When entry and stop/target occur
inside the same later bar and ordering is unknowable, the conservative order is
entry then stop before target. A corporate action before entry invalidates the
plan; after entry it closes the sample as `invalidated_corporate_action` rather
than silently adjusting prices. Untriggered plans expire at `plan_expires_at`.

For an active trade, a session open at or below stop fills at the open minus sell
slippage; an open at or above target fills at the target minus sell slippage.
Otherwise a stop/target cross fills at that boundary minus sell slippage. If both
cross in one bar, stop wins. Outcome observations use unique
`(plan_id, observation_sequence)` keys and compare-and-set the prior state;
allowed transitions are `waiting_entry -> active -> closed_*` or
`waiting_entry -> expired_untriggered/superseded_untriggered/invalidated_*`.

Fee policy is versioned in every outcome. The initial configurable default is
`cn_a_v1`: 0.03% commission each side with a CNY 5 minimum, 0.05% seller stamp
duty, and 0.05% modeled slippage each side. The stamp-duty default follows the
currently effective 2023 Ministry of Finance/State Taxation Administration
half-rate announcement; policy values remain configuration, not scattered
constants. CSI300 is sampled at the first benchmark observation at or after the
entry timestamp and exit timestamp, with a maximum five-minute lag intraday or
the same session date for daily observations. Missing aligned benchmark data
sets alpha to unavailable rather than substituting another date.

Fees, exits, maximum adverse/favorable excursion, and CSI300 alpha are stored in
append-only `decision_outcomes` records.

`decision performance` groups true-trigger outcomes by:

- action bucket and horizon;
- objective segment and detailed industry;
- domestic/macro regime and market phase;
- entry strategy and reason code.

Every outcome has immutable `metric_basis=shadow_trade_v1`. Legacy rows are
read-only views with `metric_basis=legacy_generated_baseline`; they are never
copied into `decision_outcomes`. All triggered, PnL, win-rate, alpha, and
calibration queries contain the mandatory predicate
`metric_basis=shadow_trade_v1`. No historical migration infers an entry trigger.

## Bounded Calibration

The review service creates a recommendation only after at least 30 closed shadow
trades overall and 10 closed samples in a subgroup. Only soft ranking weights
(`objective_match`, reward/risk, evidence completeness, and actionability) are
eligible. Hard gates, capital limits, stop budgets, freshness, and diversification
limits are never calibrated.

The training set is the newest 500 eligible closed outcomes from the preceding
180 days, sorted by exit time and `plan_id`; outcomes without aligned CSI300
alpha are excluded. The objective is net return alpha with maximum drawdown and
stop rate as guardrails. A fixed ridge regression (`alpha=1.0`) fits standardized
eligible features. Five deterministic expanding chronological folds use the
first 50% as the initial training set and split the remaining 50% into five
contiguous validation windows.

For each fit, a feature's relative delta is
`clamp(coefficient / max_absolute_coefficient, -1, 1) * 10%` of its baseline
weight; all-zero coefficients produce no change. A bounded-simplex projection
then repeatedly clips weights to their individual baseline `+/-10%` bounds and
redistributes the remaining total across unclipped weights in ascending feature
key order until the original total is restored. This algorithm terminates with
every weight inside its bound and is covered by exact-value tests.

A proposal is emitted only when, across decision cohorts containing at least two
closed candidates, the proposed score improves median out-of-fold Spearman rank
correlation with net alpha by at least `0.05` and top-half mean net alpha by at
least 0.25 percentage points. Maximum drawdown may worsen by no more than 2.0
percentage points and stop rate by no more than 5.0 percentage points.
Proposals store training window, sample ids, fold assignments, baseline version,
proposed version, metrics, and rollback target. The initial implementation only
exposes proposals. Activation requires an explicit authenticated admin action,
creates a new version, and rollback selects the prior version without deleting
history.

## CLI

```bash
agentctl --pretty decision today
agentctl --pretty decision today --no-refresh
agentctl --pretty decision history --limit 20
agentctl --pretty decision performance
```

`briefing today` remains the raw daily read model. `decision today` is the
policy-filtered packet intended as Codex input.

## Error Handling

- Missing account settings: return `data_quality.account=blocked`; no allocation.
- Missing/old quote: never `buy_now`; retain condition plan when otherwise valid.
- Missing profile evidence: downgrade to `wait` unless an explicit anchor has
  reviewed evidence.
- Missing bars: use taxonomy fallback and disclose it.
- Provider failure: return partial packet with structured provider errors.
- Mongo write failure: API returns HTTP 503 with structured
  `decision_persistence_failed`; CLI exits non-zero. Codex must not treat an
  unaudited packet as a decision result.
- Every decision/history/performance endpoint requires password-backed auth and
  filters by the current user. Admin status does not bypass user ownership.
- Outcome progression is append-only: `waiting_entry -> active -> closed_*` is
  represented as separate observations with the latest state selected by time.

## Tests

- Unit tests for bucket classification across all market phases.
- Provider-selection and field-provenance tests.
- Allocation tests for existing holdings, industry/segment caps, board lots,
  total loss budget, and correlation fallback.
- Snapshot idempotency and revision tests.
- Shadow-trade trigger, fee, exit, benchmark, and legacy-separation tests.
- Calibration sample-threshold and bounded-change tests.
- CLI authentication and JSON contract tests.
- Docker integration check for `briefing today`, `decision today`, scheduler,
  and Mongo indexes.

## Rollout

1. Add the profile, session, diversification, decision, and review services
   behind the authenticated API and CLI without changing existing briefing
   output or the holdings page.
2. Create Mongo indexes before enabling scheduled persistence. Existing
   candidate and legacy performance collections remain readable and are not
   migrated into the new metric basis.
3. Enable the five-minute live decision refresh first, then the 15-second
   active-plan poller after health checks confirm bounded symbol counts and
   lock ownership.
4. Keep calibration proposals inactive until an administrator explicitly
   activates a version. Rollback disables the new scheduler jobs and selects
   the previous ranking version; append-only snapshots and outcomes remain for
   audit.

1. Add collections and indexes without migrating historical candidate runs.
2. Enrich current candidate profiles on demand and cache results.
3. Introduce decision API/CLI while keeping briefing backward compatible.
4. Enable live-session snapshot refresh after deterministic tests pass.
5. Start collecting shadow outcomes immediately; calibration remains inactive
   until thresholds are met.
