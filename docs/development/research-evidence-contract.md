# Research Evidence Contract

## Scope

This change separates research coverage, single-stock evidence, and execution
permission. It does not change account balances, holdings, market permissions,
position limits, the daily scheduler, or brokerage capabilities.

## Coverage Is Not Permission

- The daily target remains 100 completed structured reviews. Aging entries do
  not count as today's completion. An unavailable announcement review is not a
  completed review.
- `research_coverage.status=below_target` is a coverage warning, not a blanket
  veto of independently complete stock research.
- When a daily contract exists, an execution candidate must have its own
  completed item for the current trade date. Missing, old, or incomplete items
  produce `candidate_structured_analysis_incomplete`.
- The existing fresh quote, permission, formal deep research, source/resolved
  profile, pullback confirmation, account, concentration, correlation and
  brokerage checks remain mandatory.
- The coverage helper only revokes permission. It never creates permission.
- Automatic favorites replacement still waits for coverage completion. A
  partial run must not evict existing AI favorites on incomplete evidence.
- Decision packets expose `decision_diagnostics.data_blockers` separately from
  investment conditions. A data failure is not evidence of a bad investment.

## Account-Aware Research Ordering

The authenticated candidate service passes an account snapshot into the shared
research pipeline. No caller-supplied identity or authentication bypass is added.
Public research without an account remains available.

Before the bounded technical shortlist, one-lot cost is compared with available
cash, dynamic single-symbol headroom, total headroom, and the existing industry,
theme and provider-sector caps. This is an optimistic upper bound, not an
allocation: unknown holding taxonomy and correlation still need final review.
The initial audit uses the current quote; the deep-layer audit rechecks the
actual entry/stop plan and the existing rounded stop-loss budget.

Definite one-lot infeasibility lowers research priority and prevents occupying
an expensive deep-research slot. It does not erase a stock from technical audit
or declare the stock fundamentally unsuitable. The structured pool remains up
to 100; deep research remains up to 15. No quality threshold is reduced.
Discovery-definition sanitization preserves the same bounded account audit as
candidate output. Formal decision research also ranks definite account
infeasibility below feasible candidates before considering entry distance.

## Provider Evidence

- Explicit CNInfo `互联网和相关服务` and provider `游戏`/`游戏Ⅱ` aliases are
  versioned in `cn-sector-v2`. Raw taxonomy, endpoint, record key and retrieval
  time remain visible. Different source classifications remain in the conflict
  audit; no company-name heuristic creates sector evidence.
- A notice date fetch retries transport failures once with a 250 ms backoff,
  within the existing worker deadline. TLS verification is not disabled.
  Persistent failure remains unavailable with no partial safe result.
- `provider_attempts` records query dates, counts and exception types only.
  Exception messages, credentials and raw transport details are not exposed.
- CLI sanitization preserves technical/earnings/notice sources and the worker's
  `code`/`severity` risk flags (alongside legacy `key`/`level`). A blocking notice
  flag must not silently become a generic warning after normalization.
- Aggregated pipeline capacities are not summed across batches. Call counts
  are additive; worker count is a concurrent upper bound; total time is wall
  time, not the sum of concurrent stage durations.

## Cross-Asset Time

Daily Yahoo bars expose their actual `session_date` as `data_at` with
`time_semantics=daily_bar_session_date`. A daily index label is not an exact
exchange trade timestamp. `checked_at` remains the fetch time.

US index bars are mapped to the prior calendar-date overnight window for the
current Shanghai day. Older bars (including holiday/weekend carry-forward),
future labels, missing time metadata and unavailable sources cannot generate
new positive/negative overnight mappings. Their values remain historical
context, with `overnight_signal_usable=false` and an explicit status.

This is **not** a complete overseas exchange-calendar implementation. It does
not certify a carried-forward bar as the latest official trading session or
infer a holiday from an absent bar. Such certification remains a separate
follow-up. Macro risk scoring thresholds are unchanged.

## Operating Limits

The discovery liquidity rule still uses at least CNY 100 million of cumulative
daily turnover. An immediate opening scan can therefore return fewer eligible
stocks than the scheduled scan at 09:40. The system must report that difference,
not invent extra technical passes or relabel aging entries as freshly reviewed.
This change does not promise 100 completed stocks at every minute of the day,
nor guarantee recovery from persistent upstream outage. Production acceptance
must be performed after explicit merge/deployment approval.
