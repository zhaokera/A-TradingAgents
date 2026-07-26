# 公开全市场研究候选发现设计

日期：2026-07-15
状态：已实施，持续迭代
适用产品：A-TradingAgents
适用入口：`holdings opportunities`

## 1. 背景

当前 `opportunities` 的自动候选发现依赖 MongoDB 中的
`market_quotes + stock_basic_info`。MongoDB 不可用时，CLI 只有在调用方显式传入
`--candidate-code` 时才会进入只读研究模式；不传代码会返回数据库错误。

市场宽度已经具备新浪公开全市场快照降级能力，但该快照目前只用于判断涨跌家数，不能证明系统逐股完成了候选筛选。现有 Mongo 候选排序还主要按涨幅降序排列，容易让涨幅接近 5% 的股票占据首位，把“当日强势”误当成“未来两个交易日适合买入”。

本设计采用用户确认的 A 方案：MongoDB 不可用时，CLI 可以从公开行情完成全 A 股研究筛选，但所有结果只能观察，固定输出 0 股，不得推断账户、持仓、现金或仓位。

## 2. 目标

1. MongoDB 不可用且没有显式候选代码时，仍能对沪、深、京 A 股普通股票生成公开全市场研究观察池。
2. 一次命令只抓取一次新浪全市场快照，同时服务市场宽度与候选发现。
3. 新浪负责全市场覆盖和第一阶段过滤，腾讯负责候选行情复核，腾讯日线负责技术价格计划。
4. 明确区分“没有合格候选”和“数据源不可用”。
5. 公开研究模式始终不可执行，所有候选数量和手数为 0。
6. 输出完整覆盖统计和淘汰原因，使 Hermes 能判断本次是否真正完成了全市场筛选。
7. 正常网络下单次命令目标耗时不超过 90 秒。

## 3. 非目标

- 不在 MongoDB 不可用时接受手工本金或现金用于仓位计算。
- 不根据历史页面、旧报告或默认值猜测当前持仓。
- 不对约 5500 只股票逐只运行完整多智能体分析。
- 不使用新浪价格直接生成可执行价格或仓位。
- 不改变显式 `--candidate-code` 模式的现有语义。
- 不改变 MongoDB 正常且行情完整时的账户和持仓决策链路。
- 不修改持仓分析前端页面。

## 4. 方案选择

### 4.1 采用方案：新浪全市场初筛 + 腾讯复核

新浪一次提供全市场代码、名称、价格、涨跌幅、成交额和提供方时间。系统先验证交易日和完整度，再做低成本初筛；随后对最多 160 只预选股票按每批 40 只调用腾讯行情。腾讯硬门禁通过者全部进入并发日线技术初筛，只有技术结构及费后净收益风险比通过的最多 8 只进入最新业绩证据核对。明确预亏、最近实际亏损或最近法定披露期实际业绩缺失者被剔除，剩余股票再读取公司行动并组装完整候选。

该方案在覆盖、准确性、运行时间和数据源压力之间最平衡。

### 4.2 未采用：完全依赖新浪

优点是实现和运行速度较简单，但新浪标准接口缺少稳定的换手率、量比等关键字段，不能完成现有风险门禁需要的复核。

### 4.3 未采用：腾讯逐股扫描全部股票

腾讯单股行情字段更完整，但扫描全部股票会产生数千次请求，运行时间和限流风险不可接受。

## 5. 架构

### 5.1 公开快照提供方与基准日期

公开快照提供方负责：

- 在独立子进程中使用最多 8 个有界 worker 并发读取新浪全 A 股分页，每页 100 只；
- 获取带日期和时间的新浪指数锚点；
- 以腾讯上证指数和深证成指行情中一致的交易日作为基准日期；任一日期缺失或两个市场日期不一致时失败关闭；
- 校验新浪锚点交易日与腾讯基准交易日一致；
- 规范化代码、名称、最新价、涨跌幅、成交额和提供方时间；
- 去重并报告重复数量；
- 限时 25 秒，超时后终止子进程；
- 返回本次命令内可复用的内存快照，不写数据库或磁盘缓存。

工作进程先读取提供方总数并计算精确页数；每一页都必须返回预期行数，再按页码恢复稳定
顺序。HTTP、JSON、任一页缺失或总页数不完整都使整次快照失败。该实现避免 AKShare
`stock_zh_a_spot()` 串行读取约 56 至 70 页而偶发超过 25 秒，但不放宽原来的提供方、
日期、覆盖和失败关闭边界。

独立 `market-status` 命令在首次结果精确为 `public_breadth_timeout` 时，可以复用同一份
腾讯指数上下文再抓取一次新浪快照。两次抓取各自仍受 25 秒上限约束；非超时错误、
日期/时效/覆盖失败均不得重试。第二次结果无论成功或失败都必须缓存并终止重试；成功
结果增加 `public_snapshot_attempt_count=2` 和原始失败状态，失败结果继续保持
`indices_only` 与 `decision.action=wait`。`opportunities` 仍受 90 秒硬截止时间约束，
不继承该独立命令重试行为。

快照是命令级不可变 DTO，至少包含 `provider_expected_count`、
`provider_expected_exchange_counts`、`raw_row_count`、`unique_row_count`、
`exchange_counts`、总量和分交易所覆盖率、基准日期、提供方时间和规范化行。市场宽度和公开候选发现必须通过依赖注入消费同一个 DTO，禁止在同一次 `opportunities` 调用中重复抓取新浪。

日期和时间门禁沿用公开市场宽度规则：盘中允许的提供方最大滞后为 20 分钟，最大未来偏差为 2 分钟；收盘后必须是同一交易日且提供方时间不早于 14:55。新浪个股行只有时间没有日期，若锚点已切到当日但个别停牌/未刷新行仍显示前一日收盘时间，则晚于命令本地时间 2 分钟以上的行单独排除并计入 `excluded_future_time_count`，剩余实际总量和分交易所覆盖仍须全部达到 95%。日期不一致、时间无法验证或排除后覆盖不足均失败关闭。

公开全市场声明只覆盖沪、深、京 A 股普通股票。支持代码包括：

- 上海：`6` 开头；
- 深圳：`0` 或 `3` 开头；
- 北京：`43`、`83`、`87`、`88` 或 `92` 开头。

快照不能只用“已返回行数”证明完整性。工作进程必须先读取新浪
`Market_Center.getHQNodeStockCount` 的提供方预期数量：`hs_a` 为总量，
`sh_a` 和 `sz_a` 为沪深数量，北京预期数为三者差额。实际总量及沪、深、京各自数量都必须达到对应提供方预期数量的 95%，实际唯一总量同时不得少于 500。预期数量接口不可用、差额为负数、缺少任一交易所或任一覆盖率不足时，不能声称完成公开全市场筛选。2026-07-15 的可行性探测结果为 5527 个原始唯一代码，其中上海 2307、深圳 2893、北京 327。

### 5.2 第一阶段：全市场公开初筛

第一阶段必须遍历快照中的全部规范化股票，并记录每类淘汰原因。

基础过滤：

- 代码符合上述沪、深、京普通股票代码集合；
- 名称不包含 ST 或退市标记；
- 交易日与基准交易日一致；
- 最新价和成交额为有限正数，涨跌幅为允许正数、零或负数的有限值；
- 代码在同一交易日内唯一；
- 当日成交额至少 1 亿元。

候选分桶：

- 温和强势：涨跌幅 `0.3% <= x <= 3.0%`；
- 可控回撤：涨跌幅 `-1.5% <= x < 0.3%`；
- 涨幅高于 3.0% 标记为追高风险，不进入主要预选；
- 跌幅低于 -1.5% 标记为深回撤风险，不进入主要预选。

预选结果按 75% 温和强势、25% 可控回撤的配额组合，生产公开扫描最多返回 160 只。温和强势目标点为 `+1.5%`，可控回撤目标点为 `-0.5%`；离目标点越远，涨跌位置质量越低。每个桶内先计算：

```text
public_score = 0.65 * amount_percentile + 0.35 * move_quality
```

其中 `amount_percentile` 是 `public_rank_population` 内的成交额百分位；`move_quality` 为以桶内目标点为 1、桶边界为 0 的分段线性分数，并截断到 `[0, 1]`。排序依次使用
`public_score` 降序、成交额降序、一手金额升序、代码升序。生产扫描先取 120 只温和强势和 40 只可控回撤；某桶不足时，由另一桶按自身顺序补足。纯排名函数仍允许调用方传入更小上限。该规则禁止直接按涨幅从高到低排序。

第一阶段的 `amount_percentile` 只在 `public_rank_population` 中计算。该集合是通过全部基础过滤、且涨跌幅落在 `[-1.5%, 3.0%]` 的沪深京股票，不受分桶配额影响。所有百分位统一使用中位秩公式。设参与集合大小为 `N`，某值为 `v`，`L` 为严格小于 `v` 的数量，`E` 为等于 `v` 的数量：

```text
percentile(v) = (L + 0.5 * (E - 1)) / (N - 1),  N > 1
percentile(v) = 1.0,                              N == 1
```

相等是指完成单位归一化后的数值完全相等，不使用浮点容差。该公式使并列值获得相同分数，且单元素集合确定为 1。集合为空时不计算百分位，直接返回
`no_eligible_candidates`。

公开排名必须由独立的 `rank_public_candidate_universe` 及其 DTO 实现，不复用要求现金、行业和可买一手校验的 Mongo 排名函数。由于公开模式没有账户信息，一手金额只展示，不参与过滤；现有 Mongo 和手动排名函数保持不变。

### 5.3 第二阶段：腾讯批量复核

腾讯批量行情接口按受控批次复核公开预选股票，并返回以请求代码为键的不可变映射，补充：

- 精确价格和涨跌幅；
- 成交额；
- 换手率；
- 量比；
- 振幅；
- 流通市值和总市值；
- 提供方交易时间。

腾讯映射的有效性和覆盖率按以下规范性规则计算：

- 只接受本批次请求中的代码；额外代码忽略并计入 `unexpected_code`；
- 同一请求代码出现多条响应时，该代码全部不计入成功数并计入
  `duplicate_code`，不能任选一条；
- 请求代码与响应交易所前缀必须一致，错配计入 `code_mismatch`；
- 价格和成交额必须为有限正数，交易日期和时间必须可解析；
- 盘中行情最多滞后命令级市场时间 5 分钟，最多领先当前时间 2 分钟；
- 收盘后必须与基准交易日一致、提供方时间不早于 14:55，且不晚于当前时间 2 分钟；
- 错误交易日、陈旧、未来时间、无效价格及上述重复或错配响应均从成功数中排除，并分别记录原因。

设公开预选数为 `n`，腾讯最低成功数为
`max(ceil(0.8 * n), min(20, n))`。成功数仅统计唯一、属于请求集合且通过上述行情有效性门禁的代码。`n == 0` 时直接返回
`no_eligible_candidates`，不调用腾讯，也不视为数据源失败。

成功数低于上述门槛时，公开候选发现整体不可用，不能把剩余少量股票当作全市场结论。

腾讯复核后的排序优先考虑：

1. 处于温和强势或可控回撤区间；
2. 成交额和市值足以支持稳定交易；
3. 换手率不高于 10%；
4. 日内振幅不高于 8%；
5. 量比不表现为极端放量；
6. 不接近涨停或异常价格状态。

腾讯主候选硬过滤规则：

- 腾讯涨跌幅必须仍处于 `[-1.5%, 3.0%]`；
- 换手率必须存在且不高于 10%；
- 日内振幅必须存在且不高于 8%；
- 总市值必须存在且不少于 20 亿元，流通市值必须存在且不少于 10 亿元；
- 如果腾讯返回涨停价，当前价格距离涨停价不足或等于 0.5% 时淘汰；
- 量比缺失不直接淘汰，但质量分为 0 并记录 `missing_volume_ratio`；量比在 `[0.8, 2.0]` 时质量为 1，在 `[0.5, 0.8)` 或 `(2.0, 3.0]` 时质量为 0.5，其他有效值为 0。

同一腾讯行情快照还保留 `pe_ratio`、`pb_ratio`、`circ_mv` 和 `total_mv`。估值字段允许
为空，但存在时必须为有限数；流通市值和总市值必须与进入技术漏斗的原始腾讯快照精确
绑定。估值只作为解释证据，不参与可执行性判断，也不能被账户适配或 DTO 清洗阶段改写。

通过硬过滤后计算：

```text
tencent_score =
    0.30 * amount_percentile
  + 0.25 * move_quality
  + 0.15 * turnover_quality
  + 0.10 * volume_ratio_quality
  + 0.10 * amplitude_quality
  + 0.10 * market_cap_percentile
```

`turnover_quality` 在 `[0.5%, 5%]` 为 1，向 0% 或 10% 线性下降；
`amplitude_quality` 在不高于 4% 时为 1，在 4% 至 8% 线性下降；市值分使用总市值百分位。`amount_percentile` 和
`market_cap_percentile` 均在通过腾讯行情有效性门禁及全部主候选硬过滤后的
`tencent_rank_population` 中计算，并复用 §5.2 的中位秩公式和相等规则。最终依次按 `tencent_score` 降序、成交额降序、振幅升序、代码升序。所有硬过滤、缺失字段和降分原因必须进入淘汰或质量统计，不得静默消失。

生产公开扫描不会在腾讯质量排名后先截取 8 只。最多 160 只腾讯硬门禁候选全部保留其
`tencent_quality_rank`、`tencent_one_lot_amount` 和 `selection_lane`，并进入下一阶段技术
初筛。这样避免流动性或市值质量排名与可用技术结构相关性不足时漏掉后排候选。一手金额
仍只使用腾讯公开价格计算，不读取账户、本金、现金或持仓，也不把一手金额解释为估值
高低。

### 5.4 第三阶段：并发技术初筛、业绩证据门禁与幸存者深检

腾讯硬门禁候选在受限子进程中使用 4 个线程并发读取腾讯前复权日线，先完成不读取公司
行动的纯技术初筛：支撑/压力/突破/止损/目标计划、趋势修复门禁和一手手续费感知的净
风险收益比。只有 `status=ok`、费后净收益风险比不低于 1.5 且不需要趋势修复的候选算
技术通过。技术通过者按净收益风险比降序、腾讯分数降序、一手金额升序、代码升序稳定
排序，最多取 8 只进入业绩证据门禁。预告部分根据基准交易日计算最近已完成报告期：1 至 3 月
查上一年度年报期、4 至 6 月查一季报期、7 至 9 月查半年报期、10 至 12 月查三季报期；
通过 `akshare.eastmoney.stock_yjyg_em` 一次性读取该报告期预告，不逐股发起请求。

门禁只检查归母净利润和扣非净利润相关指标。预测数值为负、预告类型属于首亏/续亏/增亏/
减亏，或预告文字明确包含亏损时，状态为 `loss_forecast` 并阻断新候选；任何相关预告证据
的同比变动幅度不高于 -30% 时也阻断，即使预告利润仍为正。没有找到预告时状态为
`no_forecast`，只能表示该期未发现预告；其他已找到的预告记为
`non_loss_forecast`。状态描述预告类型，`blocks_new_position` 才是最终门禁结果。

无论有无预告，同一批候选还必须通过 `akshare.eastmoney.stock_yjbb_em` 一次性核对最近
法定披露期限已经结束的实际业绩期：1 至 4 月查上一年度三季报，5 至 8 月查当年一季报，
9 至 10 月查当年半年报，11 至 12 月查当年三季报。只有归母净利润为正且公告日不晚于
基准交易日时标记 `positive_profit`；归母净利润小于或等于 0 标记 `actual_loss`，缺少对应
股票、公告日、归母净利润或只有未来公告则标记 `actual_missing`，两者都阻断。即使状态为
`positive_profit`，营收同比或净利润同比不高于 -30% 仍视为严重恶化并阻断。高于 -30%
的利润同比下降和每股经营现金流为负保留为警告，不单独阻断，也不能把股票升级为买入
信号。任一预告/实际业绩提供方请求失败、结构异常、报告期错配或审计计数不一致时整段
失败关闭。

技术初筛还保留最多 5 个 `status=net_rr_below_1_5` 且风险收益比最接近 1.5 门槛的
淘汰样本，排序仍为风险收益比降序、腾讯分数降序、一手金额升序、代码升序。每项只输出
代码、名称、风险收益比、距门槛差值和腾讯分数，并固定为 `earnings_review_status=not_reviewed`、
`actionable=false`、`is_reference_only=true`。这组数据仅解释严格漏斗的信息损失，不进入
业绩门禁、公司行动深检或最终 `candidates`，也不能被 Hermes 描述为次优买入候选。

候选构建函数只接收业绩门禁幸存者，复用已验证腾讯行情和已经计算的技术计划，只额外
读取公司行动、除权除息信息并组装风险标记，禁止重复请求单股实时行情或重复抓取日线。

技术深检还必须输出趋势修复上下文。当现价较 20 日最高价回撤至少 20%，并满足
`现价 < MA5 < MA10 < MA20` 时，标记 `trend_recovery_required`。价格计划和费后盈亏比
可以保留用于说明重新评估条件，但 `actionable`、`reference_actionable` 和建议数量必须
失败关闭；刷新后不再满足该条件时，才能继续经过其他门禁。

技术结构未通过的股票进入 `technical_screen_status_counts` 统计；其中风险收益比最接近
门槛的最多 5 只另保留上述解释性快照，但仍不进入最终候选。任一
日线发生真实 `fetch_error` 时整段失败关闭，不能把未检查股票当成普通淘汰；历史不足、
价格层级不足、净收益风险比不足和趋势修复要求则属于正常技术淘汰。

公开模式设置 90 秒命令级硬截止时间，阶段预算如下：

- Mongo 可用性判断：最多 5 秒；
- 命令级腾讯市场上下文：最多 10 秒；
- 新浪快照：最多 25 秒；
- 腾讯批量复核：最多 10 秒；
- 最多 160 只并发日线初筛、一次最多 8 只预告及实际业绩核对和幸存者公司行动深检：最多 50 秒；
- 编排和序列化余量：5 秒。

技术漏斗在一个可终止的受限工作进程中运行，最多使用 6 个并发 worker 获取腾讯前复权日线；父进程在 50 秒或剩余命令截止时间到达时终止它。新技术漏斗超时后以 `technical_deep_check_timeout` 失败关闭，不得由外层重新包装为普通候选发现失败，也不返回大批只有实时行情、没有完整日线检查的半成品候选。所有底层 HTTP 调用仍需设置明确超时，禁止仅依赖父进程等待。

命令级腾讯市场上下文一次性获取主要指数行情和基准交易日，随后注入市场门禁、公开快照和候选复核；下游禁止重复请求指数或基准历史。技术工作进程的实际父进程超时为 50 秒，并受剩余命令级截止时间约束。超过 90 秒必须终止可终止子进程并返回明确的阶段超时状态。

## 6. 命令行为

### 6.1 MongoDB 正常

1. 显式传入 `--candidate-code`：保持手动候选来源，但去重后最多 8 只，并强制执行同一套
   预告与实际业绩门禁。
2. 未传候选代码：优先使用 MongoDB 动态候选发现。
3. 只有 Mongo 动态发现返回 `candidate_discovery_unavailable`、
   `quote_universe_empty`、`stale_quote_universe` 或
   `quote_universe_too_small` 时，才允许降级到公开全市场发现。
4. `no_eligible_candidates` 是已完成的 Mongo 筛选，不能触发公开降级；
   `cash_unavailable` 和 `benchmark_calendar_unavailable` 是独立门禁失败，也不能触发公开降级。
5. 一旦候选来源为 `public_full_market`，就不得生成仓位、数量或可执行状态。若 Mongo
   不可用，保持纯公开 `research_only`，账户字段全部不可用；若 Mongo、用户和账户已经
   成功解析，仅动态行情候选池触发允许降级的状态，则进入
   `account_context_research_only`，复用前一阶段已读取的账户、持仓和近期交易，只计算
   一手现金可达性、占权益比例和 20% 单票上限。该叠加层不得重新请求数据库或行情，
   不得调用仓位计算，也不得移除 `public_research_only` 阻断原因。

命令编排层负责创建一次公开快照并把它同时注入市场宽度和公开候选发现；公开候选发现负责创建一次腾讯批量映射并把它注入最终候选构建。手工候选在命令级只创建一次严格校验后的业绩复核，并在完整账户构建和 Mongo 懒读取失败后的 research-only 降级间复用。三个阶段都不得在下游隐式重复抓取。

### 6.2 MongoDB 不可用

1. 显式传入 `--candidate-code`：保持手动只读研究模式，同时复用命令级业绩复核；业绩数据
   不可用时必须保留研究行情但阻断全部建议数量。
2. 未传候选代码：运行公开全市场候选发现。

### 6.3 账户上下文叠加

`account_context_research_only` 使用 Schema 8，并在每个候选增加 `account_fit`：腾讯现价
一手金额、现金/权益占比、已有同代码市值、交易后一手单票占比、现金可达性和 20% 单票
上限结果。已有同代码持仓只有在估值行情仍可执行时才能参与计算；陈旧或缺失估值必须
返回 `account_fit_data_incomplete`。无论其他门禁是否通过，`blocking_reasons` 都必须包含
`public_research_only`，所有可执行布尔值保持 false、所有建议数量保持 0。

账户叠加必须复用 Mongo 阶段已读取的最近交易，通过现有 `_build_recent_sale_policy` 将
最近两交易日内卖出的代码与公开候选重新匹配。命中时输出 `recent_sale_policy`，并在该
候选 `account_fit.blocking_reasons` 增加 `recent_sale_cooldown`。不得仅展示最近卖出记录
而遗漏候选级阻断，也不得为此再次查询交易流水。
3. 公开发现成功：返回研究观察池。
4. 公开发现失败：命令返回非零退出码和
   `candidate_discovery_unavailable`，不能返回空成功结果。

### 6.4 独立公开业绩核对

`holdings earnings --code ...` 为 Hermes 提供不依赖登录、账户或 Mongo 的公开证据入口。
命令去重后最多接受 8 只合法 A 股代码，先通过腾讯主要指数建立命令级基准交易日，再复用
与全市场漏斗相同的预告和实际业绩服务及严格 DTO 校验。无效代码和超量输入必须在请求
腾讯或业绩提供方之前返回退出码 2；预告提供方、实际业绩提供方或 DTO 失败返回退出码 4
及 `earnings_forecast_review` 阶段证据。

成功结果使用独立 Schema 1，固定 `mode=public_research_only`、`actionable=false`、建议手数
和数量为 0。`blocked_codes` 包括预告亏损或同比不高于 -30%、实际亏损或缺失、实际营收
或净利润同比不高于 -30%；`selected_codes` 只表示本次业绩证据未
阻断，不能证明技术结构、市场、估值、公司行动或账户条件通过。该命令主要用于补查
`technical_closest_rejections`，补查后也不得把技术淘汰样本移入最终候选。

### 6.5 独立市场门禁的交易时段证据

`market-status` 必须把命令级上海市场时段与市场门禁一起输出。该时段复用
`OpportunityMarketContext.now`，不能在同一命令中再次读取不一致的系统时间。输出至少
包含 `local_time`、`session`、`is_trading_hours`、`quote_stale_risk`、`next_refresh_at`
和 `next_refresh_session`。盘前、午休和收盘结果仍可用于研究最近已完成交易日，但不能
被 Hermes 描述为当日实时行情；下一刷新时间是下一次重新获取腾讯指数和市场宽度的
下限，不代表刷新后门禁必然放行。公开全市场和手工 research-only 的 opportunities
响应通过 `data.market_status.market_session` 原样传播这组证据，账户叠加不得删除或
改写它。

新浪公开宽度成功时，`market-status.data.market_gate.breadth_regime` 还必须传播快照的
预期总量、实际和预期沪深京数量、总量/分交易所覆盖率、原始/唯一行数以及被排除的陈旧
或未来时间行数。`indices_and_public_breadth` 只能建立在这些证据已经通过门禁的基础上；
Hermes 不得只根据 `status=ok` 宣称全市场宽度完整。

### 6.6 独立近期公告核查

`holdings notices --code ...` 为 Hermes 提供不依赖登录、账户或 Mongo 的近期公告证据
入口。命令去重后最多接受 8 只合法 A 股代码；无效或超量输入在任何外部请求前失败。
命令复用腾讯主要指数建立基准交易日，同时使用同一命令时钟的上海本地日期作为公告
窗口终点。默认回看 7 个自然日；`--lookback-days` 可设为 1 至 90。这样周末或周一盘前仍
可覆盖最近交易日收盘后发布的公告，也能对最终候选回看仍在持续的制裁、重组等重大事件，
且不会把最近交易日错误当作公告截止日期。

默认 7 天窗口按自然日读取东方财富全市场批次，服务层只保留请求代码；超过 7 天时改用
东方财富个股公告接口，每只代码只请求一次明确的起止日期。两条路径均按 URL 去重并按
日期、标题和 URL 稳定倒序，每只代码最多返回最新 20 条，同时保留总数、返回数和截断
状态。任一请求异常、缺字段、代码或日期越界、URL 非 HTTP(S) 或输出 DTO 计数/顺序/标签
不自洽时，整个 `recent_notice_review` 阶段失败关闭，不得返回此前已抓取的部分公告。

标题和公告类型只生成有限白名单标签：风险提示、制裁或贸易限制、重大资产重组、融资或
摊薄、持股变动、股份回购、财务披露和重大合同。制裁标签覆盖制裁、SDN/实体清单、出口
管制、贸易限制和禁运关键词。标签只标记是否需要人工阅读原文，不判断情绪，也不产生
`blocks_new_position`。成功响应使用独立 Schema 1，固定
`mode=public_research_only`、不可执行和 0 股；`no_recent_notices` 仅表示窗口内未检索到
公告，不能证明公司没有风险。

## 7. 输出契约

顶层保持现有 JSON 结构。Mongo、手动候选和无账户公开研究成功模式使用
`schema_version=7`；带已验证账户上下文的公开降级模式使用 `schema_version=8`。独立
`earnings` 和 `notices` 各自使用 Schema 1；`market-status` 命令若返回字段不变，则保持
自身现有版本。

公开研究模式的 `candidate_discovery` 无论是 `ok` 还是
`no_eligible_candidates`，都使用同一个完整 DTO。它必须同时包含提供方预期总量和分交易所预期量、原始和唯一行数、实际分交易所数量、总量和分交易所覆盖率、各阶段计数、淘汰原因及阶段数据源状态。下面数值仅用于展示结构：

```json
{
  "data": {
    "mode": "research_only",
    "candidate_discovery": {
      "mode": "public_full_market",
      "status": "ok",
      "source": "akshare.sina.stock_zh_a_spot+tencent_batch_quotes",
      "benchmark_trade_date": "YYYY-MM-DD",
      "provider_expected_count": 5527,
      "provider_expected_exchange_counts": {"sh": 2307, "sz": 2893, "bj": 327},
      "raw_row_count": 5527,
      "unique_row_count": 5527,
      "universe_count": 5527,
      "exchange_counts": {"sh": 2307, "sz": 2893, "bj": 327},
      "total_coverage_ratio": 1.0,
      "exchange_coverage_ratio": {"sh": 1.0, "sz": 1.0, "bj": 1.0},
      "eligible_count": 2134,
      "public_preselected_count": 160,
      "tencent_requested_count": 160,
      "tencent_verified_count": 156,
      "tencent_rank_population_count": 134,
      "selected_count": 134,
      "technical_screened_count": 134,
      "technical_passed_count": 3,
      "technical_selected_count": 3,
      "technical_closest_rejection_count": 5,
      "technical_closest_rejections": [
        {
          "code": "002318",
          "name": "久立特材",
          "status": "net_rr_below_1_5",
          "net_reward_risk": 1.381,
          "min_net_reward_risk": 1.5,
          "gap_to_min_net_reward_risk": 0.119,
          "tencent_score": 0.6231453634085213,
          "earnings_review_status": "not_reviewed",
          "actionable": false,
          "is_reference_only": true
        },
        {
          "code": "300803",
          "name": "指南针",
          "status": "net_rr_below_1_5",
          "net_reward_risk": 1.0653,
          "min_net_reward_risk": 1.5,
          "gap_to_min_net_reward_risk": 0.4347,
          "tencent_score": 0.6963978696741855,
          "earnings_review_status": "not_reviewed",
          "actionable": false,
          "is_reference_only": true
        },
        {
          "code": "000100",
          "name": "TCL科技",
          "status": "net_rr_below_1_5",
          "net_reward_risk": 1.0607,
          "min_net_reward_risk": 1.5,
          "gap_to_min_net_reward_risk": 0.4393,
          "tencent_score": 0.7081904761904763,
          "earnings_review_status": "not_reviewed",
          "actionable": false,
          "is_reference_only": true
        },
        {
          "code": "301291",
          "name": "明阳电气",
          "status": "net_rr_below_1_5",
          "net_reward_risk": 1.0361,
          "min_net_reward_risk": 1.5,
          "gap_to_min_net_reward_risk": 0.4639,
          "tencent_score": 0.46533671679198,
          "earnings_review_status": "not_reviewed",
          "actionable": false,
          "is_reference_only": true
        },
        {
          "code": "000777",
          "name": "中核科技",
          "status": "net_rr_below_1_5",
          "net_reward_risk": 1.0174,
          "min_net_reward_risk": 1.5,
          "gap_to_min_net_reward_risk": 0.4826,
          "tencent_score": 0.5715037593984962,
          "earnings_review_status": "not_reviewed",
          "actionable": false,
          "is_reference_only": true
        }
      ],
      "earnings_screened_count": 3,
      "earnings_blocked_count": 3,
      "earnings_selected_count": 0,
      "earnings_report_period": "20260630",
      "earnings_actual_report_period": "20260331",
      "technical_checked_count": 0,
      "technical_screen_status_counts": {"ok": 3, "net_rr_below_1_5": 131},
      "earnings_screen_status_counts": {"loss_forecast": 2, "no_forecast": 1},
      "earnings_actual_status_counts": {"actual_loss": 2, "positive_profit": 1},
      "earnings_screen_results": [
        {
          "code": "688599",
          "status": "loss_forecast",
          "blocks_new_position": true,
          "announcement_date": "2026-07-17",
          "forecast_types": ["减亏", "续亏"],
          "loss_metrics": ["归属于上市公司股东的净利润", "扣除非经常性损益后的净利润"],
          "reason_summary": "报告期业绩预告原因摘要",
          "evidence": [
            {
              "metric": "归属于上市公司股东的净利润",
              "forecast_type": "减亏",
              "forecast_value": -270000000.0,
              "forecast_change_pct": 90.745,
              "forecast_text": "预计2026年1-6月归母净利润亏损"
            }
          ],
          "latest_actual": {
            "status": "actual_loss",
            "report_period": "20260331",
            "announcement_date": "2026-04-30",
            "net_profit": -283070624.77,
            "risk_flags": ["actual_net_loss"]
          }
        },
        {
          "code": "002165",
          "status": "loss_forecast",
          "blocks_new_position": true,
          "announcement_date": "2026-07-14",
          "forecast_types": ["首亏", "增亏"],
          "loss_metrics": ["归属于上市公司股东的净利润", "扣除非经常性损益后的净利润"],
          "reason_summary": "报告期业绩预告原因摘要",
          "evidence": [
            {
              "metric": "归属于上市公司股东的净利润",
              "forecast_type": "首亏",
              "forecast_value": -7500000.0,
              "forecast_change_pct": -130.75,
              "forecast_text": "预计2026年1-6月归母净利润亏损"
            }
          ],
          "latest_actual": {
            "status": "actual_loss",
            "report_period": "20260331",
            "announcement_date": "2026-04-23",
            "net_profit": -24011985.47,
            "risk_flags": [
              "actual_net_loss",
              "net_profit_yoy_decline",
              "negative_operating_cash_flow"
            ]
          }
        },
        {
          "code": "300113",
          "status": "no_forecast",
          "blocks_new_position": true,
          "announcement_date": null,
          "forecast_types": [],
          "loss_metrics": [],
          "reason_summary": null,
          "evidence": [],
          "latest_actual": {
            "status": "positive_profit",
            "report_period": "20260331",
            "announcement_date": "2026-04-29",
            "net_profit": 80491519.51,
            "net_profit_yoy_pct": 9.56,
            "revenue": 284584509.08,
            "revenue_yoy_pct": -50.7654542495,
            "operating_cash_flow_per_share": 0.022666037912,
            "gross_margin_pct": 69.0031286716,
            "risk_flags": ["severe_revenue_contraction"]
          }
        }
      ],
      "rejection_counts": {"outside_move_window": 1200},
      "stage_sources": {
        "public_snapshot": {
          "provider": "akshare.sina.stock_zh_a_spot",
          "status": "ok"
        },
        "tencent_verification": {
          "provider": "tencent_batch_quotes",
          "status": "ok"
        },
        "technical_screen": {
          "provider": "tencent_daily_bars",
          "status": "ok"
        },
        "earnings_forecast_review": {
          "provider": "akshare.eastmoney.stock_yjyg_em+akshare.eastmoney.stock_yjbb_em",
          "status": "ok"
        },
        "technical_deep_check": {
          "provider": "cninfo_dividend_calendar",
          "status": "not_called_no_earnings_survivors"
        }
      }
    },
    "decision": {
      "action": "observe",
      "actionable": false,
      "suggested_lots": 0,
      "suggested_quantity": 0
    }
  }
}
```

每个候选必须满足：

- `decision.action == "observe"`；
- `decision.actionable == false`；
- `decision.suggested_lots == 0`；
- `decision.suggested_quantity == 0`；
- `affordable_with_cash == null`；
- `cash_usage_pct == null`；
- `is_reference_only == true`；
- 包含公开发现、腾讯复核和技术计划的数据来源或状态。

公开研究模式还必须满足整棵输出树的安全不变量：

- 任意名为 `actionable` 或 `reference_actionable` 的字段均为 `false`；
- 任意建议手数、数量和新仓数量均为 `0`；
- `new_position_allowed == false`；
- 所有最大新增敞口金额和比例均为 `0`；
- 市场数据是否完整改用 `data_complete`、`gate_evaluated` 等非交易字段表达，不能通过 `actionable=true` 表达；
- 技术价格可以作为研究参考展示，但任何嵌套技术计划都不能带可执行状态。

成功但没有候选时使用成功信封和零退出码：

```json
{
  "ok": true,
  "data": {
    "mode": "research_only",
    "candidate_discovery": {
      "mode": "public_full_market",
      "status": "no_eligible_candidates",
      "source": "akshare.sina.stock_zh_a_spot",
      "benchmark_trade_date": "YYYY-MM-DD",
      "provider_expected_count": 5527,
      "provider_expected_exchange_counts": {"sh": 2307, "sz": 2893, "bj": 327},
      "raw_row_count": 5527,
      "unique_row_count": 5527,
      "universe_count": 5527,
      "exchange_counts": {"sh": 2307, "sz": 2893, "bj": 327},
      "total_coverage_ratio": 1.0,
      "exchange_coverage_ratio": {"sh": 1.0, "sz": 1.0, "bj": 1.0},
      "eligible_count": 0,
      "public_preselected_count": 0,
      "tencent_requested_count": 0,
      "tencent_verified_count": 0,
      "tencent_rank_population_count": 0,
      "technical_checked_count": 0,
      "technical_screened_count": 0,
      "technical_passed_count": 0,
      "technical_selected_count": 0,
      "technical_screen_status_counts": {},
      "earnings_screened_count": 0,
      "earnings_blocked_count": 0,
      "earnings_selected_count": 0,
      "earnings_report_period": null,
      "earnings_screen_status_counts": {},
      "earnings_screen_results": [],
      "selected_count": 0,
      "rejection_counts": {"below_min_amount": 5527},
      "stage_sources": {
        "public_snapshot": {
          "provider": "akshare.sina.stock_zh_a_spot",
          "status": "ok"
        },
        "tencent_verification": {
          "provider": "tencent_batch_quotes",
          "status": "not_called_no_preselection"
        },
        "earnings_forecast_review": {
          "provider": "akshare.eastmoney.stock_yjyg_em",
          "status": "not_called_no_candidates"
        },
        "technical_deep_check": {
          "provider": "tencent_daily_bars",
          "status": "not_called_no_candidates"
        }
      }
    },
    "candidates": [],
    "decision": {
      "action": "observe",
      "actionable": false,
      "suggested_lots": 0,
      "suggested_quantity": 0
    }
  }
}
```

`source` 必须由本次实际调用成功的数据源动态组成。公开预选为零时不得包含
`tencent_batch_quotes`。如果腾讯已被调用但复核后没有合格候选，则
`source` 包含腾讯，`stage_sources.tencent_verification.status` 为 `ok`，并保留实际请求数、验证数和淘汰统计。

数据源失败使用非零退出码 4 和错误信封：

```json
{
  "ok": false,
  "error": {
    "code": "candidate_discovery_unavailable",
    "message": "公开全市场候选发现不可用",
    "details": {
      "stage": "sina_snapshot",
      "candidate_discovery": {
        "status": "candidate_discovery_unavailable",
        "rejection_counts": {}
      }
    }
  }
}
```

`error.details.stage` 必须是单一失败阶段枚举，例如 `sina_expected_counts`、
`sina_snapshot`、`tencent_market_context`、`tencent_verification`、
`earnings_forecast_review` 或 `technical_deep_check`，不能把多个阶段拼成一个模糊值。

Hermes 必须能通过 `ok`、退出码和状态区分数据源失败与真实无候选。

## 8. 失败处理

| 场景 | 行为 |
| --- | --- |
| 新浪锚点日期缺失或错配 | 公开发现不可用，非零退出 |
| 新浪子进程超过 25 秒 | 终止进程；独立 `market-status` 只重试一次，公开发现仍按原有失败关闭规则处理 |
| 实际总量或任一交易所数量少于提供方预期数 95%、总量少于 500 或缺少任一交易所 | 公开发现不可用 |
| 腾讯复核低于动态最低成功数 | 公开发现不可用 |
| 部分腾讯股票失败但覆盖率合格 | 记录失败数量，继续 |
| 上涨家数占优但深跌或近跌停尾部达到黄色/红色阈值 | 保留既有风险预算门禁，并通过 `risk_triggers`、深跌比例和近跌停比例明确尾部触发，不描述为普跌 |
| 单只股票技术日线不足 | 保留次级观察并标记计划不可用 |
| 最近报告期业绩预告明确亏损 | 从本轮候选中剔除并保留逐只审计证据 |
| 最近报告期非亏损预告的同比变动不高于 -30% | 标记严重业绩恶化并阻断，不能因利润仍为正放行 |
| 最近报告期没有业绩预告 | 标记 `no_forecast`，继续检查最近法定披露期实际业绩，不将其解释为盈利 |
| 最近法定披露期归母净利润小于或等于 0 | 标记 `actual_loss`，从本轮候选中剔除并保留逐只证据 |
| 最近法定披露期实际业绩缺失、晚于基准日或归母净利润缺失 | 标记 `actual_missing` 并阻断，不猜测盈利状态 |
| 最近法定披露期营收或净利润同比不高于 -30% | 即使归母净利润为正也阻断；温和利润下降和负经营现金流只保留警告 |
| 业绩预告或实际业绩提供方/结构不可用 | 业绩阶段失败关闭，不放行未核对候选 |
| 公司行动不可用 | 标记未知，不声称不存在公司行动 |
| 全部股票被规则淘汰 | 返回 `no_eligible_candidates`，不是数据源错误 |
| MongoDB 和公开发现均不可用 | 返回非零退出，不生成候选或仓位 |

所有路径保留“仅供研究参考，不构成投资建议或交易指令”的语义。

## 9. 兼容性

- 保留 `holdings opportunities --candidate-code ...` 参数和既有字段；新增最多 8 只限制、
  顶层/逐只业绩证据及新仓门禁属于向后兼容的安全增强。
- 保留 MongoDB 动态候选优先级。
- 保留现有腾讯单股行情和技术价格计划逻辑。
- 数据库可用且用户明确传入手工候选时，休市行情仍生成不可执行的腾讯日线参考计划，
  同时结合真实账户校验一手金额和资金上限；业绩硬门禁或业绩证据不可用必须传播到仓位
  阻断，`actionable` 与建议数量继续失败关闭。
- 休市参考计划可保留费后风险额、收益额和盈亏比，但必须删除入场、止损、目标模拟
  订单对象，任何嵌套层级都不得出现非零订单数量。
- 上述账户模式的休市参考计划只受 `quote_freshness` 阻断，不得误报
  `account_data_unavailable`；数据库不可用的 research-only 手工路径仍保留该阻断项。
- 不放宽现有外部风险、市场门禁、公司行动和手续费感知风险收益比规则。
- 不因个别深回撤股票出现有序价格层级而放宽 `[-1.5%, 3.0%]` 主候选窗口；深回撤手工
  候选也必须先通过趋势修复门禁。
- 公开候选来源不能伪装为 MongoDB 来源。
- 现有 Hermes 文档增加无候选代码的公开研究调用和状态解释。
- 为 Mongo 和手动路径保留返回契约快照测试，防止公开排名规则渗入旧路径。

## 10. 测试设计

### 10.1 公开快照

- 正确解析价格、涨跌幅、成交额和提供方时间；
- 正确解析提供方预期总量、沪深数量，并由差额得到北京预期数量；
- 拒绝日期错配；
- 拒绝无法验证日期的快照；
- 去除重复代码并报告数量；
- 接受同日收盘后的研究快照；
- 超时后确认子进程被终止；
- 验证沪、深、京交易所均有覆盖且覆盖率达到 95%；
- 500 行截断响应即使自身无重复也必须因低于提供方预期数量而失败；
- 一次编排只调用一次公开快照抓取函数。

### 10.2 公开候选排名

- 过滤不支持代码、ST、退市、无效价格和低成交额；
- 温和强势与可控回撤按配额进入预选；
- 接近 5% 的股票不会因涨幅最高排到首位；
- 深回撤股票不会被当成正常回撤；
- 公开模式不按未知现金过滤一手金额；
- 百分位参与集合只包含基础过滤后且位于两个候选桶的股票；
- 单元素百分位为 1，唯一递增值映射到 0 至 1，并列值使用相同中位秩；
- 所有淘汰原因均可统计。

### 10.3 腾讯复核

- 批量响应按代码正确拆分；
- 只计算请求代码；拒绝额外代码、重复代码、错误交易日和无效价格；
- 盘中腾讯行情最多滞后 5 分钟、最多未来偏差 2 分钟；收盘后必须为同一交易日、时间不早于 14:55 且不晚于当前时间 2 分钟；不满足者不计入覆盖率；
- 覆盖率不足时整体失败；
- 部分失败但覆盖率合格时继续并报告；
- 换手率和振幅风险参与排序或淘汰；
- 腾讯成交额和市值百分位只在通过行情门禁及全部硬过滤的集合内计算；
- 腾讯百分位覆盖单元素、唯一递增值和并列值边界；
- 已验证腾讯映射进入候选构建后不再触发单股行情请求。

### 10.4 CLI 集成

- MongoDB 不可用、无显式候选时返回公开全市场研究模式；
- 全部候选固定 `observe / false / 0股`；
- 递归遍历整个 payload，断言所有 `actionable/reference_actionable` 为 `false`、所有新增敞口和建议数量为 0；
- 不出现账户、现金、持仓或仓位推断；
- 数据源失败返回非零退出和明确错误码；
- `no_eligible_candidates` 与数据源错误可区分；
- `no_eligible_candidates` 仍包含来源、基准日、提供方预期总量、分交易所覆盖率、阶段计数和淘汰统计；
- `ok` 与 `no_eligible_candidates` 都返回完整的总量、分交易所覆盖证据和阶段数据源状态；
- 未产生公开预选时不调用腾讯，`source` 不包含腾讯且阶段状态为
  `not_called_no_preselection`；
- 手动候选行为保持兼容；
- MongoDB 正常路径保持兼容；
- Meta 来源和 Schema 版本正确。
- 通过模拟单调时钟、市场上下文复用、子进程超时和分阶段耗时，确定性验证 90 秒硬截止时间；测试不能真实等待 90 秒。

## 11. 验证

实施后至少执行：

```bash
.venv/bin/python -m pytest tests/test_public_market_breadth.py tests/test_candidate_discovery_service.py tests/test_cli_holdings.py -q
.venv/bin/python -m compileall app/services tests
git diff --check
```

在网络可用时补充一次真实命令冒烟验证：

```bash
.venv/bin/holdings opportunities --external-risk-level red --pretty
```

验收时记录总耗时、全市场数量、腾讯复核覆盖率、最终候选数量和所有候选的 0 股不变量。真实外部请求只用于冒烟验证，不进入单元测试。

## 12. 已知限制

- 公开研究模式不能替代真实账户和持仓分析。
- 新浪和腾讯均属于外部公开行情，可能限流、延迟或调整字段。
- 不具备行业基础数据时，公开模式不能证明行业分散度；输出必须明确这一点。
- 第一阶段是全市场规则筛选，不等同于对每只股票运行完整基本面、新闻和多智能体分析。
- 收盘后技术计划只能作为下一交易日观察参考，开盘后仍需刷新腾讯行情。

## 13. 候选入选依据适配

真实公开发现服务返回的候选定义使用扁平排名字段，例如 `bucket`、
`public_score`、`amount_percentile`、`move_quality`、`tencent_bucket`、
`tencent_score`、腾讯成交额/市值百分位和换手率、量比、振幅质量。CLI 不能假设
上游已经提供 `priority` 或嵌套 `discovery`，否则真实命令会丢失候选排序和入选依据，
而仅使用人工构造字段的测试会掩盖该问题。

在公开发现结果通过一致性校验后，CLI 使用一个规范化适配器按最终候选顺序生成：

- `priority`：从 1 开始的最终腾讯复核排名；
- `discovery.source`：固定为 `public_full_market`；
- `discovery.trade_date`：公开快照交易日；
- `discovery.public_rank`：与 `priority` 一致；
- `discovery.public`：公开预选桶、分数、成交额百分位、涨跌质量和公开快照价格数据；
- `discovery.tencent`：腾讯复核桶、分数、成交额/市值百分位、涨跌质量、换手率、量比、振幅及其质量分。

适配后的定义同时供正常技术深检和超时降级使用。正常深检结果按股票代码重新绑定
`priority/discovery`，不能依赖深检服务回传这些字段；超时路径直接从规范化定义构造
观察候选。无候选路径仍返回空数组。适配器只解释候选为何进入观察池，不生成止损、
目标价、买入信号或仓位，也不读取账户数据；Schema 保持 7，整棵 payload 继续通过
research-only 安全清洗。

适配器只从白名单扁平字段构造 `discovery`，不得复制上游已有的未知嵌套对象。候选
进入正常深检或超时降级前，必须校验交易日、公开/腾讯候选桶、分数、百分位、质量分
及正值行情字段；缺失、非有限数或越界值统一视为 `candidate_discovery_unavailable`。
腾讯 `volume_ratio` 和 `limit_up` 是明确允许为 `null` 的可选证据，但对应质量分仍须
有效；两个键必须由发现服务明确返回，键缺失不等同于显式 `null`。候选定义交易日、
腾讯成交时间、已验证行情成交时间和本次市场上下文基准交易日必须指向同一个上海
交易日。成交时间必须是精确到秒且带 `Z` 或明确 UTC 偏移的完整 ISO 时间戳；仅日期、
缺少秒或缺少时区均视为无效，时间不可解析或不一致时失败关闭。

公开成功响应同时是严格 DTO 边界。`candidate_discovery`、正常深检候选和超时行情均
从字段白名单重新构造；行情新鲜度、技术价格计划、业绩预告与实际业绩证据、公司行动、风险标记和触发器的
嵌套对象也只保留声明字段。任何上游未知字段、账户快照、现金诊断或私有载荷都不能
通过整对象复制进入 Hermes JSON。这样上游字段漂移不会静默产生 `ok=true` 但解释
字段为 `null` 的半完整响应，也不会扩大 CLI 的公开数据面。

白名单不是“缺什么就少输出什么”。成功的 `candidate_discovery` 必须包含完整总量、
沪深北分项、覆盖率、阶段计数和实际调用的来源阶段，计数须为非布尔非负整数，覆盖率须为
有限的 0 至 1 数值，并须分别等于实际数量除以提供方预期数量；腾讯请求数须等于公开
预选数，最小验证数按发现服务公式重算，实际验证数不得低于该下限。缺字段、布尔伪装
数字、`NaN/Inf`、低于最低覆盖率或计数关系不一致都失败关闭。
技术筛选成功后，业绩结果顺序必须与最多 8 只技术幸存者一致，阻断数必须等于预告亏损
或实际业绩非正利润的候选并集，阻断代码和放行代码必须完整且互斥，预告与实际报告期
都必须由基准交易日确定。`no_forecast` 的公告、类型、指标、原因和证据必须为空，不能
携带互相矛盾的信息；每只结果必须有 `latest_actual`，其状态、利润符号、公告日期、风险
标记和聚合计数必须自洽。
最近技术淘汰样本数必须等于 `min(net_rr_below_1_5 状态数, 5)`；代码不得与技术通过代码
重叠，风险收益比必须有限且小于 1.5，门槛和差值必须可重算，腾讯分数必须与原始定义
一致，排序必须稳定。任一样本可执行、声称已做业绩复核、包含未知字段或顺序/计数不一致
都视为 `InvalidTechnicalScreenMetadata` 并失败关闭。
`technical_deep_check=ok` 也必须同时具有有效腾讯价格、成交额、成交量和带状态的技术
价格计划，仅返回股票代码不能计作完成。深检行情的代码、成交日、成交时刻、价格、
成交额和成交量须与进入 worker 的腾讯验证行情一致；扁平腾讯证据也须绑定同一原始
行情。腾讯 `volume` 以及存在时的 `quote_volume` 在 `_quote_snapshot` 阶段保留，避免
在最终 DTO 清洗前提前丢失公开行情证据。

## 14. 显式截止日仓位目标

公开全市场发现仍然只能返回研究观察池，不能自动转成仓位。只有用户明确传入一组手工
候选，并同时给出 `target-exposure-pct` 与 `deployment-deadline` 时，完整 Mongo 账户
路径才进入 `deadline_target`。该模式解决的是“用户要求在指定日期达到目标仓位”与旧的
全局零仓门禁互相冲突，不改变公开研究模式的 0 股边界。

截止日模式以已验证权益和当前持仓市值计算目标缺口，允许目标上方最多 5 个百分点用于
A 股整手取整；单票市值上限为权益的 25%，组合价格计划止损预算为权益的 3.5%。该预算
用于覆盖 60% 目标仓位、约 5% 技术止损以及费用和滑点，避免费用后风险略高于 3% 时与
显式仓位目标形成数学冲突。外部风险
和 A 股市场门禁保留原始证据，但只影响限价、分批和排序，不再单独把总新仓额度归零。
账户/现金/持仓估值、腾讯行情时效、业绩证据、公司行动调价、技术价格计划、趋势修复、
追涨或大分歧、最近卖出冷静期仍失败关闭。响应必须同时给出当前、计划后仓位、目标缺口、
整手上浮和是否达到目标；不能因为用户设定目标就伪造当前账户或跳过价格条件。

价格计划按突破和回踩两条路径评估。突破不满足费用后净收益风险比 1.5 时，可使用距现价
不超过 3% 的最近支撑回踩；止损取下一有效支撑或保守失效位，减仓位取至少 2% 上方的
第一压力层，目标位取至少 5% 上方且严格高于减仓位的下一原始压力层。层级比较使用未
舍入值，最后才按 0.01 元显示，避免同一个压力位因四舍五入误作两个层级。

全市场技术漏斗允许剔除少量单票日线请求失败，但有效日线覆盖率必须至少为 90%；低于
该阈值时整批失败关闭。单票瞬时失败不得再把其余已完成技术核验的股票全部作废。
