# A-TradingAgents Hermes 持仓 CLI

Hermes 在本机直接调用仓库 CLI，不需要 Web 登录，也不得编造账户或持仓：

```bash
cd /Users/zhaok/Desktop/TradingAgents-CN
.venv/bin/holdings market-status --pretty
.venv/bin/holdings summary --pretty
.venv/bin/holdings list --pretty
.venv/bin/holdings opportunities --external-risk-level red --pretty
.venv/bin/holdings opportunities --candidate-code 601728 --external-risk-level red --pretty
.venv/bin/holdings opportunities --candidate-code 601688 --candidate-code 600547 --candidate-code 600028 --external-risk-level red --target-exposure-pct 60 --deployment-deadline 2026-07-21 --pretty
.venv/bin/holdings earnings --code 002318 --code 000100 --pretty
.venv/bin/holdings notices --code 002318 --code 000100 --pretty
.venv/bin/holdings notices --code 600346 --lookback-days 90 --pretty
```

所有输出仅供研究参考，不构成投资建议或交易指令。

## 命令契约

- `market-status` 查询腾讯主要指数，并优先使用 Mongo 市场宽度；Mongo 宽度不可用、过期或覆盖不足时尝试新浪公开宽度。新浪快照使用 8 个有界 worker 并发读取每页 100 只，任一缺页仍失败关闭。首次精确返回 `public_breadth_timeout` 时，命令内部只重试一次；第二次成功会在 `breadth_regime.public_snapshot_attempt_count=2` 和 `retried_after_status=public_breadth_timeout` 留下证据。其他失败不重试。只有日期、时效和总量/沪深京覆盖验证通过才能返回 `indices_and_public_breadth`，否则保持 `indices_only` 和 `decision.action=wait`。成功时 `breadth_regime` 直接输出实际/预期数量及覆盖率，不能只相信状态词。`data.market_session` 使用同一命令时钟标明盘前、连续交易、午休或收盘，盘前/午休/收盘的 `quote_stale_risk=true`，并给出 `next_refresh_at`；此时基准交易日只能称为最近可用交易日，不能称为当前实时盘面。
- `summary`、`list`、`trades` 依赖 Mongo。它们成功时才可用于当前账户、现金、持仓和近期交易；失败时不得推断这些数据。
- `earnings` 不需要登录或 Mongo，可重复传入 `--code`，去重后一次最多核对 8 只 A 股。命令先用腾讯主要指数确定同一基准交易日，再一次性读取最近已完成报告期业绩预告和最近法定披露期实际业绩；成功结果仅是业绩证据，固定 `data.mode=public_research_only`、`actionable=false`、建议数量为 0，不能单独构成候选或交易条件。
- `notices` 不需要登录或 Mongo，可重复传入 `--code`，去重后一次最多核对 8 只 A 股。命令先用腾讯主要指数确定基准交易日，再按命令所在上海日期回看公告；默认 7 个自然日，`--lookback-days` 可设为 1 至 90。默认窗口读取东方财富按日全市场公告，超过 7 天时改用按代码和日期区间查询的个股公告接口，每只只发一次请求；基准交易日和公告截止日在盘前或周末可以不同。成功结果固定为 `public_research_only`、不可执行和 0 股。公告标题标签仅提示需要人工阅读原文，不自动判定利好、利空、候选升级或交易阻断。
- `opportunities` 不传 `--candidate-code` 时优先走 Mongo 动态初筛。Mongo 不可用，或 Mongo 候选发现状态为 `candidate_discovery_unavailable`、`quote_universe_empty`、`stale_quote_universe`、`quote_universe_too_small` 时，自动执行公开全市场研究筛选；不再必然返回 `database_error`。
- 公开全市场路径最多预选 160 只，腾讯实时行情按每批 40 只、最多 4 批复核；随后用最多 6 个并发 worker 对腾讯硬门禁通过者做前复权日线技术初筛，并继续受 50 秒技术阶段预算约束。技术结构和费后净收益风险比通过的最多 8 只会一次性核对最新已完成报告期业绩预告，并核对最近法定披露期的实际业绩；明确预亏、预告同比变动不高于 -30%、最近实际亏损或缺失、实际营收或净利润同比不高于 -30% 者被剔除，剩余股票才进入公司行动深检。任一批行情、任一日线请求或任一业绩数据源发生真实抓取错误时整段失败关闭，不返回部分候选；技术漏斗达到 50 秒时返回 `technical_deep_check_timeout`，不伪装成空候选或普通发现失败。
- Mongo 与账户已经成功读取、仅动态行情候选池触发公开降级时，输出 `data.mode=account_context_research_only`：保留真实账户、持仓和近期交易，为公开候选增加一手资金适配，但仍禁止仓位计算。Mongo 不可用时继续输出纯 `research_only`，不得推断账户。
- `--candidate-code` 是用户明确指定候选的手工路径，可重复传入，去重后最多 8 只。命令会对全部手工候选做一次与公开漏斗相同的业绩复核，并在完整账户构建与 Mongo 读取失败后的 `research_only` 降级之间复用结果，不重复请求业绩提供方；它不会触发公开全市场候选发现。
- 手工候选输出在 `data.earnings_review` 保留整批证据，并在每只候选的 `earnings_review`、`earnings_gate` 和价格计划阻断项中传播。业绩硬门禁命中时为 `earnings_risk_gate`；提供方或上下文不可用时为 `earnings_review_unavailable`，两者都必须使仓位计划保持 0。Mongo 和账户可用时，显式手工候选在休市期间仍会返回腾讯日线计算的不可执行技术参考价位，并结合真实现金校验一手金额；常规模式仍受行情时效、市场、外部风险、业绩和价格计划门禁约束。休市参考计划只保留费后风险收益指标，不包含入场、止损或目标模拟订单数量。
- `--external-risk-level` 只接受 `green`、`yellow`、`red`。外部风险证据不完整时保持 `unknown` 并省略该参数。
- 用户明确给出仓位截止目标时，`--target-exposure-pct` 和 `--deployment-deadline YYYY-MM-DD` 必须成对出现，并且必须显式传入 `--candidate-code`。只有 Mongo、账户、持仓和现金均可核验时才计算整手数量；数据库不可用时返回 `deployment_objective.status=account_data_unavailable` 和 0 股，不得沿用旧本金冒充当前账户。
- 截止日模式输出 `cash_deployment_plan.mode=deadline_target`。目标上方保留最多 5 个百分点的整手取整空间，单票上限为权益的 25%，组合止损预算为权益的 3.5%。该预算覆盖 60% 目标仓位、约 5% 技术止损以及费用和滑点，避免 3% 上限在费用后与目标形成数学冲突。`external_risk_gate` 和 `a_share_market_gate` 只降级为限价、分批和风险提示；账户数据、行情时效、亏损或缺失业绩、公司行动调价、无有效价格计划、趋势修复、追涨/大分歧和最近卖出冷静期仍是硬阻断。Hermes 必须报告 `deployment_objective.status/target_met/projected_exposure_pct/target_shortfall_amount`，不能只复述目标值。
- 技术计划先评估突破，再评估距现价不超过 3% 的支撑回踩。回踩的减仓位和目标位必须来自两个严格不同的原始压力层级；两位小数显示相同不能被误当成不同层级。任一路径仍需通过含佣金、印花税、过户费和滑点的净收益风险比 `>=1.5`。

`opportunities` 常规成功响应使用 `meta.schema_version=7`；带已验证账户上下文的公开降级响应使用 Schema 8。`earnings` 和 `notices` 各自使用独立 Schema 1；`market-status`、`summary`、`list`、`trades` 等命令的 schema 版本不因此改变。

## 公开全市场模式

自动公开扫描成功时，两种模式都必须保持不可执行：

- `ok=true`、进程退出码 `0`、`data.context.source=public_full_market`、`data.candidate_discovery.mode=public_full_market`。
- `data.mode=research_only`、Schema 7：Mongo/账户不可用，`data.account.status=unavailable`，本金、现金和权益为 `null`；不得读取、推断或反推账户。
- `data.mode=account_context_research_only`、Schema 8：Mongo 和用户账户已在降级前成功解析，`data.account`、`data.holdings_context`、`data.recent_trade_context` 可作为真实本地上下文；仍不得生成仓位或数量。
- 公开候选只是观察池，不是买入建议。`data.decision`、`data.candidates[]` 及其所有嵌套对象必须保持不可执行和 0 股。
- 递归检查 `actionable=false`、`reference_actionable=false`、`new_position_allowed=false`。
- 递归检查 `suggested_lots=0`、`suggested_quantity=0`、`new_position_lots=0`、`new_position_quantity=0`、`max_new_exposure_amount=0`、`max_new_exposure_pct=0`、`external_new_exposure_amount=0`、`market_adjusted_new_exposure_cap=0`。

Hermes 必须检查 `data.candidate_discovery` 的完整证据：

- `status`、`benchmark_trade_date` 和 `source`。
- 实际总量 `universe_count`、实际沪深京数量 `exchange_counts.sh/sz/bj`。
- 预期总量 `provider_expected_count`、预期沪深京数量 `provider_expected_exchange_counts.sh/sz/bj`。
- 总覆盖 `total_coverage_ratio`、沪深京覆盖 `exchange_coverage_ratio.sh/sz/bj`。
- `stage_sources.public_snapshot`、`stage_sources.tencent_verification`、`stage_sources.technical_screen`、`stage_sources.earnings_forecast_review`、`stage_sources.technical_deep_check`。没有技术通过者时，技术阶段仍可成功但候选为空，业绩和公司行动阶段会明确记录未调用状态。
- 腾讯请求与验证计数：`tencent_requested_count`、`tencent_minimum_verified_count`、`tencent_verified_count`、`tencent_rank_population_count`。
- `public_preselected_count` 是公开预选数；`selected_count` 是腾讯硬门禁后进入技术初筛的数量，不是最终候选数。
- `technical_screened_count`、`technical_passed_count`、`technical_selected_count`、`technical_screen_status_counts` 分别表示实际技术初筛数、技术通过数、送入业绩证据核对数和各技术淘汰状态。
- `technical_closest_rejection_count` 和 `technical_closest_rejections` 最多保留 5 只因费后风险收益比低于 1.5 而被淘汰、但数值最接近门槛的股票，用于解释漏斗。逐项只有代码、名称、当前风险收益比、门槛差值和腾讯排序分；`earnings_review_status` 固定为 `not_reviewed`，`actionable=false`、`is_reference_only=true`。它们不是候选，未通过技术门禁，也没有接受本轮业绩复核。
- `earnings_screened_count`、`earnings_blocked_count`、`earnings_selected_count`、`earnings_report_period`、`earnings_actual_report_period`、`earnings_screen_status_counts`、`earnings_actual_status_counts` 和 `earnings_screen_results` 是业绩复核审计证据；`technical_checked_count` 是通过业绩门禁后完成公司行动及候选组装的数量。技术送审、业绩送审和最终深检数最多为 8。
- `earnings_screen_results[].status=loss_forecast` 会阻断该股票；任何预告证据的 `forecast_change_pct<=-30` 也会阻断，即使预告仍为盈利。`no_forecast` 只表示该报告期没有找到预告，必须继续检查 `latest_actual`。实际业绩只有归母净利润为正且营收同比、净利润同比均大于 -30% 才不触发硬门禁；`actual_loss`、`actual_missing` 或任一同比不高于 -30% 均阻断。小于 0 但高于 -30% 的利润同比下降和负经营现金流目前是警告，不单独放行或升级股票。实际结果保留公告日、收入/利润及同比环比、每股收益、净资产收益率、每股经营现金流、毛利率、行业和风险标记。

每个 `data.candidates[]` 还会保留可追溯的入选依据：

- `priority` 是该股票在腾讯硬门禁候选池中的稳定顺序，从 1 开始；技术漏斗最终按费后净收益风险比、腾讯分数、一手金额和代码选择最多 8 只，因此返回候选的 `priority` 可以不连续。它不是买入优先级。
- `discovery.public_rank` 与 `priority` 一致，`discovery.trade_date` 是筛选快照交易日。
- `discovery.public` 记录新浪公开预选的桶、分数、成交额百分位和涨跌质量。
- `discovery.tencent` 记录腾讯复核的桶、分数、成交额/市值百分位，以及换手率、量比、振幅和对应质量分。`quality_rank` 是分层前的腾讯质量名次，`one_lot_amount` 是腾讯价格计算的一手金额，`selection_lane` 说明候选来自 `quality_core`、`one_lot_diversity` 或不足时的 `quality_fill`。分层只使用公开价格，不代表账户可以买入。
- `quote.pe_ratio`、`quote.pb_ratio`、`quote.circ_mv` 和 `quote.total_mv` 是与同一腾讯行情快照绑定的估值证据；PE/PB 可为空，流通/总市值必须为正，任何字段都不允许被中间层改写。它们只用于解释估值，不构成买入信号。
- `guarded_price_plan.trend_context` 记录短期均线排列、现价低于哪些关键均线、距 20 日高点回撤和距技术入场价的距离。当 `recovery_required=true` 时，表示回撤至少 20%、且满足 `现价 < MA5 < MA10 < MA20`；候选必须带 `trend_recovery_required` 阻断，`actionable/reference_actionable=false`、数量为 0。止损、突破和目标价仍仅作为等待趋势修复的研究参考。
- Schema 8 下的 `account_fit` 记录一手金额、现金/权益占比、已有同代码市值、20% 单票上限和 `blocking_reasons`。`passes_account_size_checks=true` 只表示金额可达，不表示可以买入；`blocking_reasons` 必须始终包含 `public_research_only`。若候选命中最近两交易日卖出冷静期，还必须包含 `recent_sale_cooldown`，并与顶层 `recent_sale_policy.matched_candidate_codes` 一致。`account_context_research_only` 已确认账户可用，因此 `guarded_price_plan.execution_blocked_by` 不得残留 `account_data_unavailable`；行情过期等真实阻断仍须保留。

这些字段只能用于回答“为什么进入观察池”和比较数据质量。分数高、排名靠前都不能覆盖
`market_gate`、技术价格计划、业绩预告、实际业绩、估值、公司行动、账户资金约束或 research-only 的 0 股安全门禁。

两种结果不得混淆：

- `data.candidate_discovery.status=no_eligible_candidates`：完整公开扫描和覆盖验证成功，但没有合格候选。输出到 stdout，`ok=true`，退出码 `0`，`data.candidates=[]`。这不是数据源失败。
- `error.code=candidate_discovery_unavailable`：公开数据源、覆盖、腾讯验证或技术深检失败。技术漏斗达到固定阶段预算时使用更具体的 `technical_deep_check_timeout` 和 `stage=technical_deep_check`；业绩阶段也可能以 `EarningsForecastFetchError`、`EarningsActualFetchError`、`EarningsForecastScreenError` 或 `InvalidEarningsScreenMetadata` 失败。此类错误输出到 stderr，`ok=false`，退出码 `4`；原样报告完整 `error` 对象，并特别检查实际存在的 `stage`、`details` 和候选覆盖证据。

## 关键字段

- `market-status.data.market_gate.level`：`green`、`yellow`、`red` 或 `unknown`。
- `market-status.data.market_session`：命令所在的上海市场时段、是否连续交易、行情陈旧风险和下一次刷新时间。必须将其 `local_time` 与 `market_gate.benchmark_trade_date` 一起报告；`session` 不是 `morning` 或 `afternoon` 时不得声称读取了当日实时盘面。
- `opportunities.data.market_status.market_session`：候选响应复用同一个命令上下文中的交易时段证据。Hermes 直接调用 `opportunities` 时也必须检查它，不能因为候选存在就跳过时段和基准交易日校验。
- `market-status.data.market_gate.breadth_confirmation_required`：是否仍缺少有效全市场宽度。
- `market-status.data.data_completeness`：`indices_and_breadth`、`indices_and_public_breadth`、`indices_only` 或 `unavailable`。
- `market-status.data.market_gate.breadth_regime.public_snapshot_attempt_count`：仅在新浪公开宽度首次超时、第二次抓取完成时出现，值固定为 `2`；它不表示数据校验通过，仍须同时检查 `status` 和 `data_completeness`。
- `market-status.data.market_gate.breadth_regime.provider_expected_count/exchange_counts/provider_expected_exchange_counts/total_coverage_ratio/exchange_coverage_ratio`：新浪成功快照的覆盖审计证据。实际总量及沪深京各自覆盖率均须至少为 95%；`excluded_future_time_count` 表示当日只有时间、但晚于命令时间 2 分钟以上而被排除的行数。`risk_triggers` 明确黄色/红色由下跌家数比例、深跌比例还是近跌停尾部数量触发，`limit_down_like_ratio_pct` 给出后者占全市场比例；不得在只有尾部风险触发时把盘面误写成多数股票下跌。
- `summary.data.summary`：仅在命令成功时表示当前账户本金、持仓市值、现金、浮动盈亏和月目标。
- `list.data.items`：仅在命令成功时表示当前真实持仓；已全部卖出的股票不属于当前持仓。
- `earnings.data.earnings_review`：按请求代码顺序返回 `report_period`、`actual_report_period`、逐只预告状态和 `latest_actual`。`blocked_codes` 表示预告亏损或同比重挫、实际亏损或缺失、实际营收或净利润同比重挫；当前“重挫”阈值固定为不高于 -30%。`selected_codes` 只表示业绩证据未触发该门禁，不表示技术通过、进入观察池或可以买入。
- 手工 `opportunities.data.earnings_review` 与 `opportunities.data.candidates[].earnings_gate`：前者是整批审计证据，后者是逐只结果。只有 `earnings_gate.status=passed` 才表示该门禁未阻断；`blocked` 或 `unavailable` 都必须对应 0 数量，且 `passed` 也不能覆盖其他门禁。
- `notices.data.notice_review`：按请求代码顺序返回实际回看窗口、公告总数、实际返回数、截断状态、人工核查标签和原文 URL。每只最多返回最新 20 条；默认 7 天时 `meta.source=akshare.eastmoney.stock_notice_report`，扩展窗口时为 `akshare.eastmoney.stock_individual_notice_report`。`sanctions_or_trade_restrictions` 标记制裁、SDN/实体清单、出口管制、贸易限制或禁运标题；它和其他标签一样只要求阅读原文。`no_recent_notices` 只表示该窗口未检索到公告，不表示公司没有风险。`attention_tag_code_counts` 统计命中每类标签的股票数，不是公告数或利好/利空计数。
- 完整账户模式下，`opportunities.data.brief.candidate_decision_matrix.rows` 给出候选结论；`candidate_lot_plan[].blocking_failed_gates` 是当前最直接阻断项，`failed_gates` 是兼容的全量约束。
- 公开模式下，`opportunities.data.candidates[].priority/discovery` 只解释观察池排序依据，不表示买入优先级、目标价或可交易信号。
- `account_context_research_only` 下，`account_fit.passes_account_size_checks` 只回答“一手金额是否适合当前账户”，不能覆盖技术、业绩预告、实际业绩、估值、市场、外部风险、公司行动、近期卖出冷静期或 `public_research_only` 门禁。

## 给 Hermes 的提示词

```text
你可以直接运行我的 A-TradingAgents 本地 CLI。不要询问账号密码，不要调用网页，不要编造账户、持仓、现金、近期交易、价格或市场宽度。

工作目录固定为 /Users/zhaok/Desktop/TradingAgents-CN，CLI 固定为 .venv/bin/holdings。

执行顺序：
1. 运行 `.venv/bin/holdings market-status --pretty`，检查 ok、进程退出码、data.market_session、data.market_gate.benchmark_trade_date、data.data_completeness、data.decision.actionable 和 data.decision.action。若为 `indices_and_public_breadth`，还必须报告实际/预期总量、沪深京实际/预期数量、总量及分交易所覆盖率和 `excluded_future_time_count`，不得只复述 `status=ok`。必须同时报告上海本地时间、session、quote_stale_risk 和 next_refresh_at；盘前、午休或收盘结果只能描述为最近可用交易日基线，并在 next_refresh_at 后刷新，不能写成当日实时盘面。该命令已对精确的新浪 worker 超时内置一次重试，Hermes 不得再为 `market-status` 追加循环重试。
2. 运行 `.venv/bin/holdings summary --pretty` 和 `.venv/bin/holdings list --pretty`。命令成功时用于当前账户、现金和真实持仓；已卖出股票不得当作当前持仓。任一命令返回任何错误（包括配置、连接、用户解析等）时都不得推断账户数据，但仍必须继续运行不传 `--candidate-code` 的 opportunities 自动公开扫描。
3. 只有证据支持 green、yellow 或 red 中的明确等级时才传 `--external-risk-level`；证据不完整时保持 unknown 并省略该参数，不得传 `--external-risk-level unknown`。
4. 无论 Mongo 是否可用，都可以不传 candidate 运行 opportunities。等级明确时运行 `.venv/bin/holdings opportunities --external-risk-level <green|yellow|red> --pretty`；保持 unknown 时运行 `.venv/bin/holdings opportunities --pretty`。不传 candidate 时优先 Mongo；Mongo 不可用或行情候选池不可用、为空、过期、覆盖不足时，CLI 自动执行公开全市场研究。
5. 只有我明确指定候选时才重复传入 `--candidate-code <代码>`，把它作为手工候选路径，去重后最多 8 只。必须检查顶层 `data.earnings_review`、每只 `earnings_review` 和 `earnings_gate`；列出 `blocked_codes`、`selected_codes`、两个报告期及阻断原因。`earnings_gate.status=blocked` 或 `unavailable` 时明确写成业绩风险阻断或业绩证据不可用，不得输出建议数量；`passed` 也只能写“业绩门禁未阻断”。
6. 每次检查 stdout/stderr、ok、进程退出码、meta.schema_version、data.mode 和 data.market_status.market_session。直接读取 opportunities 时也必须报告 session、quote_stale_risk、next_refresh_at 与 candidate_discovery.benchmark_trade_date；非连续交易时段的候选只能作为最近可用交易日观察项。`research_only` 必须按无账户公开研究解释，不得读取、推断或编造本金、现金、持仓和近期交易；`account_context_research_only` 可以使用响应内已验证的 account、holdings_context 和 recent_trade_context，但仍不得把公开观察池描述成买入建议。
7. 两种公开模式都必须检查 candidate_discovery.status、实际/预期总量、沪深京实际/预期/覆盖率、stage_sources、腾讯请求/最低验证/实际验证/排名样本计数，以及 technical_screened_count、technical_passed_count、technical_selected_count、technical_closest_rejection_count、technical_closest_rejections、earnings_screened_count、earnings_blocked_count、earnings_selected_count、technical_checked_count、technical_screen_status_counts、earnings_screen_status_counts 和 earnings_actual_status_counts；不得只看最终候选数就声称完成全市场研究。`technical_closest_rejections` 只能写成“最接近技术门槛的淘汰样本”，必须同时报告 risk/reward、gap、`earnings_review_status=not_reviewed`，不得混入 candidates 或描述为基本面已通过。逐项读取 earnings_screen_results：列出 loss_forecast 被阻断代码及其公告证据；把 no_forecast 明确写成“该报告期未发现预告”，并继续报告 latest_actual 的报告期、公告日、利润、收入、同比、现金流、毛利率和 risk_flags。只有 latest_actual.status=positive_profit 才可进入最终观察池。逐只读取 candidates[].priority、discovery.trade_date、discovery.public、discovery.tencent，以及 quote.pe_ratio/pb_ratio/circ_mv/total_mv；Schema 8 还要逐只读取 account_fit，并检查 recent_sale_policy，报告一手现金/权益占比、是否通过账户金额检查和完整 blocking_reasons。matched_candidate_codes 中的候选必须包含 recent_sale_cooldown。明确 priority、quality_rank、估值字段和 passes_account_size_checks 都不是买入优先级。递归确认布尔键 actionable、reference_actionable、new_position_allowed 均为 false；零值键 suggested_lots、suggested_quantity、new_position_lots、new_position_quantity、max_new_exposure_amount、max_new_exposure_pct、external_new_exposure_amount、market_adjusted_new_exposure_cap 均为 0。quote.volume、quote.quote_volume、historical_volume、amount 等行情成交量和成交额必须保留，不得误清零。
8. 当 `technical_closest_rejections` 非空且需要继续核对这些淘汰样本的基本面时，提取其中代码并一次运行 `.venv/bin/holdings earnings --code <代码1> --code <代码2> ... --pretty`，最多 8 只。检查退出码、Schema 1、`public_research_only`、腾讯基准交易日、两个报告期、逐只预告证据、`latest_actual` 和风险标记。即使代码出现在 `selected_codes`，也只能写“业绩门禁未阻断”；原股票仍是技术淘汰样本，不能移入 candidates、不能输出买入优先级或数量。该命令不得用于猜测账户数据。
9. 对 `technical_closest_rejections` 可先按每批最多 8 只运行默认 7 天 `.venv/bin/holdings notices --code <代码1> ... --pretty`；对最终 `candidates` 必须运行 `.venv/bin/holdings notices --code <代码1> ... --lookback-days 90 --pretty`，覆盖仍在持续的制裁、重组和其他重大事件。检查退出码、Schema 1、`public_research_only`、腾讯基准交易日、market_session、实际 start/end/lookback、逐只 total/returned/truncated、attention_tags、manual_review_required 和每条公告原文 URL。标签 `risk_warning`、`sanctions_or_trade_restrictions`、`material_asset_restructuring`、`financing_or_dilution`、`shareholding_change`、`share_repurchase`、`financial_disclosure`、`major_contract` 只决定是否需要人工读原文；不得仅凭标签推断涨跌。技术淘汰样本查完公告后仍是技术淘汰样本；最终候选命中制裁或贸易限制时，必须单列为持续事件风险，不得只依据业绩和技术结构给出买入结论。
10. `no_eligible_candidates` 表示完整扫描成功但没有合格候选：报告 ok=true、退出码 0 和空候选，不得描述为数据源失败。
11. 结构化 error 必须原样报告完整 error 对象，不得删减或改写：保留 code、message，以及实际存在的 stage 和 details。若唯一失败是 `candidate_discovery_unavailable`，且 `details.stage=sina_snapshot`、公开快照阶段状态为 `public_breadth_timeout`，等待 3 秒后只重试一次相同的 opportunities 命令；第二次仍失败就原样报告，不得无限重试。其他错误不重试。`technical_deep_check_timeout` 表示技术漏斗未在 50 秒内完成，不能写成“0 只候选”。`candidate_discovery_unavailable` 还要特别报告 details.stage，并引用 details.candidate_discovery 证据，不得改写为“没有候选”；`details.stage=earnings_forecast_review` 表示公开漏斗的业绩门禁不可用，不得把未核对的股票继续描述为候选。手工路径可能成功返回但 `data.earnings_review.status!=ok`，此时逐只 `earnings_gate.status=unavailable`、数量必须为 0，同样不得解释为通过。独立 `earnings` 命令返回 `EarningsForecastFetchError`、`EarningsActualFetchError`、`EarningsForecastScreenError` 或 `InvalidEarningsScreenMetadata` 时同样原样报告且不重试；独立 `notices` 命令返回 `NoticeReviewFetchError`、`NoticeReviewError` 或 `InvalidNoticeReviewMetadata` 时原样报告 `recent_notice_review` 阶段及实际存在的 `failed_date` 或 `failed_code`，也不重试或使用部分结果。
12. 完整 Mongo 候选模式只有在市场门禁、行情时效、企业行动、业绩、价格计划、趋势修复、费后盈亏比和资金约束全部通过时才能写“可继续评估”。若 `guarded_price_plan.trend_context.recovery_required=true`，必须报告回撤幅度、均线位置、距技术入场价距离以及 `trend_recovery_required`，不得因为费后盈亏比达标而忽略该阻断；公开 `research_only`、`account_context_research_only` 或手工 research-only 一律只能写“观察”，不得输出买入手数、仓位建议或交易指令。
13. 所有结论注明“仅供研究参考，不构成投资建议或交易指令”。

如果 market-status 返回 indices_only，不得把指数结果描述成完整市场结论；返回 indices_and_public_breadth 时，必须注明市场宽度来自新浪公开行情，而不是本地 Mongo 快照。
```
