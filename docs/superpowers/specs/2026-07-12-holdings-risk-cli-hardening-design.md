# Holdings Risk and CLI Hardening Design

## Scope

This change hardens the holdings backend and the machine-readable CLI used by Hermes. It does not change the holdings frontend and does not rebuild or restart Docker services.

The change keeps existing response fields where possible and adds explicit metadata. Existing manual price plans remain authoritative. Report and technical prices are references only and become non-actionable when their source data is stale or incomplete.

## Data Freshness

Tencent quote parsing must preserve two different timestamps:

- `trade_at`: the provider's market timestamp, normalized to an ISO-8601 value in `Asia/Shanghai`.
- `received_at`: when this application received the response.

Freshness is determined from `trade_at`, never from `received_at`. The API and CLI carry a shared quote snapshot with source, market time, receive time, status, and reason instead of collapsing every source to a scalar. During an A-share trading session, a quote is actionable only when it is from the current trading date, no older than five minutes, and no more than 60 seconds in the future relative to the evaluator clock. Outside a trading session, quote data may still be useful for research, but it is not actionable for a position-size calculation. Missing or malformed provider timestamps, excessive future clock skew, and Mongo/AKShare fallback prices are display-only.

The latest report includes `created_at` and `analysis_date`. New report writes persist the requested/result analysis date as the market-data session instead of replacing it with generation time; the generation date remains a legacy fallback. A report price plan is actionable for at most one subsequent started exchange session: a Friday report is valid through Monday and expires when Tuesday's session starts. Session counting uses Tencent `sh000001` benchmark dates and falls back to conservative weekday counting with the fallback recorded in metadata. Per-stock bars are not used as the calendar because suspensions would incorrectly extend validity.

Older reports remain under `historical_report_price_plan`, but the top-level `ai_advice` price fields read by the current frontend are set to `null` or replaced by a valid Tencent technical plan. This enforces expiry without modifying frontend code.

## Technical Price Plan

For A shares, the service calls `ak.stock_zh_a_hist_tx` with an exchange-prefixed symbol, `adjust="qfq"`, and a bounded 120-calendar-day window. Rows are normalized, invalid rows removed, duplicate dates replaced by the last value, and dates sorted. At least 60 valid rows are required. A fresh quote replaces the row for the same trade date or appends the next trade date; it is never merged when the quote/history price scale differs by more than 25%, which is reported as a corporate-action/adjustment gate.

The calculator uses MA5/10/20/60, 20-session Bollinger bands at plus/minus two sample standard deviations, 5-session lows, and 20-session lows/highs. Support candidates must be below the current price and resistance candidates above it. The nearest support is the reference support, the lowest 20-session/Bollinger support is the invalidation basis, and ordered distinct resistance candidates map exactly as resistance 1 = breakout, resistance 2 = intermediate sell, resistance 3 = target. Invalidation is rounded down to the ¥0.01 tick after a 0.5% buffer using `Decimal.ROUND_FLOOR`; breakout is rounded up after a 0.3% buffer using `Decimal.ROUND_CEILING`; other prices use `Decimal.ROUND_HALF_UP`. Missing ordered levels make the plan non-actionable. Hard-coded candidate levels remain labelled configured historical references and never become an actionable fallback after Tencent-bar failure.

Technical levels validate report levels and provide mandatory fallback fields. Resolution is field-level: a manual value wins; otherwise a fresh report value wins only when it is within 10% of the matching technical value and the final tuple remains ordered; otherwise the technical value is used. Missing manual/report fields are completed from the technical plan. The executable tuple is `entry = manual_buy/report_buy/technical_breakout`, `stop = manual_stop/report_stop/technical_invalidation`, and `target = manual_target/report_target/technical_target`. `manual_sell_price` and report sell price are intermediate display exits and do not participate in RR. Stale report values never participate. A price plan is actionable only when:

- the current quote is actionable;
- enough daily bars exist to calculate the required levels;
- stop/invalidation is below entry;
- target is above entry;
- reward-to-risk after estimated costs is at least 1.5.

Manual prices remain the selected display source even when report prices expire. A partial manual plan follows the same field-level chain: manual first, then a fresh report value that passes technical validation, then the valid current technical value. Manual values cannot bypass quote freshness, corporate-action scale checks, stop/entry/target ordering, net RR, cash, external-risk, or account-loss gates.

## Risk Gates and Position Sizing

Hermes supplies an external-risk level when requesting opportunities. Omission is explicitly `unknown`:

- `green`: maximum new exposure 20% of estimated equity;
- `yellow`: maximum new exposure 12% of estimated equity;
- `red` or `unknown`: 0%, so no suggested lots.

This is a hard gate, not checklist text. The response records the selected level, cap, and reason.

Candidates are evaluated in deterministic configured priority order and share one plan-wide new-exposure budget and one plan-wide loss budget. Risk sizing uses `configured_total_assets` when present. Without it, actionable equity is cash plus market value only when every existing holding has an actionable quote; incomplete valuation or same-symbol exposure fails closed to zero lots. Display-only prices never enter the denominator. The plan-wide new-purchase amount is capped at equity times 20%/12%/0% according to external risk; existing holdings do not consume that new-exposure budget. Each candidate's new amount is also capped at 35% of cash, while its existing fresh market value plus proposed purchase must not exceed a separate 20% of equity post-trade symbol cap. The existing 50%-of-cash initial deployment cap remains as a further compatibility ceiling. Maximum planned account loss across the proposed plan is 0.75% of estimated equity, including estimated round-trip fees and slippage. A-share quantity is always evaluated in whole 100-share lots; the CLI no longer accepts an arbitrary A-share lot-size override. If one lot exceeds any hard cap or RR is below 1.5, `suggested_lots` is 0.

Sizing uses the guarded plan's breakout/entry price. Fee estimates are deterministic pure functions: 0.03% commission with a ¥5 minimum on each side, 0.05% sell-side stamp duty, 0.001% transfer fee on each side, and 5 basis points of adverse slippage on entry and exit. Amounts round to cents after each order-side total. Net RR is `(target_net_proceeds - entry_total_cost) / (entry_total_cost - stop_net_proceeds)`. Cash checks include buy costs. Because minimum fees are nonlinear, the sizing loop evaluates each whole 100-share lot count. Recorded sales retain an explicitly supplied total fee and expose gross proceeds, net proceeds, cost basis, and realized PnL.

Every candidate returns all failed constraints in `failed_gates`; `risk_gate` remains as a compatibility summary rather than masking later failures. Opportunity schema metadata is bumped.

## Trades and CLI

Trade lists and recent-trade context sort primarily by an effective business timestamp derived from `sold_at`, with `created_at` as a compatibility fallback. ISO timestamps are validated and normalized to UTC before storage. New records also store an indexable BSON `effective_at` value. For legacy records, all matching records are loaded, normalized, and sorted before applying the requested `limit`; limiting by `created_at` first is forbidden because it can omit the latest business trade.

`typer` becomes a direct project dependency and an installable `holdings` console script calls `app.services.holdings_cli:main`. Package discovery includes the proprietary `app` and compatibility `cli` packages, includes `app/LICENSE`, and explicitly packages the root `holdings_cli` module. The existing `python -m holdings_cli` entry remains supported. A wheel smoke test runs both entry forms outside the checkout. A globally installed CLI requires explicit Mongo host/database environment configuration; data commands return a structured `mongo_config_required` error instead of silently using localhost/default database. Running from a repository directory with `.env` may continue using that file.

CLI commands continue to default to the local `admin` record and bypass Web login/JWT. They directly read the configured MongoDB database. This is suitable only for a trusted local machine and does not expose a network endpoint. Reports are classified as shared stock research because the current report schema has no `user_id`; this is acceptable under the system's current single-admin deployment but is not a future multi-user isolation guarantee.

## Error Handling and Compatibility

Network or historical-bar failures return structured, non-actionable states instead of silently using request time as market time. Existing quote-price fields and report metadata remain available. New metadata uses stable snake_case JSON keys and all responses preserve the research-only disclaimer.

No migration is required for old trades: records without `sold_at` fall back to `created_at`. Sale recording remains a trusted single-admin, single-writer workflow; full transactional/idempotent execution on a replica set is explicitly outside this change and remains a documented residual risk. No current frontend file is modified.

## Verification

Development follows red-green-refactor. Focused tests cover provider timestamps, Friday-to-Monday staleness, report age, technical-level calculation, external-risk caps, RR, fee-aware risk sizing, `sold_at` ordering, and installed CLI invocation. Existing holdings, price-plan, and Tencent tests must remain green, followed by syntax compilation of changed Python modules.
