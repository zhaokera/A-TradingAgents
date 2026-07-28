# Agent CLI

`agentctl` 是 A-TradingAgents 面向 Hermes 和本地自动化 Agent 的稳定 JSON CLI。
它通过本机 `8331` 后端复用 Web 的认证、持仓、任务、报告、筛选和系统服务。首次使用
必须通过系统账号密码登录；默认访问会话有效 7 天，刷新会话有效 30 天。

## 前置条件

```bash
cd /Users/zhaok/Desktop/TradingAgents-CN
docker compose up -d
uv sync --frozen
.venv/bin/agentctl auth login --username admin --password '你的密码'
.venv/bin/agentctl auth status
.venv/bin/agentctl health
```

默认 API 地址为 `http://localhost:8331`。可通过全局参数 `--api-url` 或环境变量
`A_TRADINGAGENTS_API_URL` 修改。CLI 默认拒绝向非本机地址发送管理令牌。

登录成功后，会话保存在 `~/.config/a-tradingagents/agentctl-session.json`，目录权限为
`0700`、文件权限为 `0600`。文件只保存 token，不保存密码；CLI 输出也不会显示 token。
会话文件可通过 `A_TRADINGAGENTS_SESSION_FILE` 或 `--session-file` 修改。

不建议把密码直接写入长期提示词。首次授权 Hermes 时可临时设置环境变量，登录完成后
删除该变量：

```bash
export A_TRADINGAGENTS_PASSWORD='你的密码'
.venv/bin/agentctl auth login --username admin
unset A_TRADINGAGENTS_PASSWORD
```

## 命令组

```bash
agentctl dashboard
agentctl doctor
agentctl briefing --help
agentctl decision --help
agentctl capabilities
agentctl auth --help
agentctl holdings --help
agentctl candidates --help
agentctl favorites --help
agentctl analysis --help
agentctl reports --help
agentctl screening --help
agentctl stocks --help
agentctl profile --help
agentctl notifications --help
agentctl admin routes
```

`agentctl doctor` 会实际验证另一个 Agent 完成决策所需的 12 个只读 API 契约，包括认证、
持仓、简报、研究包、软件基线、最终决策工作台、历史、绩效、候选、自选和报告。
`agentctl capabilities` 返回稳定的机器
可读能力清单；`admin routes` 返回已登记的管理
接口。尚未封装成一等命令的 JSON API 可通过 `admin raw` 调用。浏览器登录、SSE/WebSocket
展示和文件上传等 UI/传输层能力不作为 Hermes 命令暴露，分析进度统一使用 `analysis status`
轮询。

全局参数必须放在命令组之前：

```bash
agentctl --pretty holdings summary
```

## 常用工作流

读取今日研究包，并完成“Codex 提案 -> 软件硬校验 -> 用户确认”闭环：

```bash
agentctl --pretty briefing today
agentctl --pretty decision research --refresh
agentctl --pretty decision baseline --no-refresh
agentctl decision propose --payload-json '{"research_packet_id":"research_...","selections":[],"portfolio_rationale":"当前不新开仓","no_action_reason":"等待有效价格计划"}'
agentctl --pretty decision validate --proposal-id PROPOSAL_ID --refresh-quote
agentctl --pretty decision final
agentctl --pretty decision history --limit 20
agentctl --pretty decision performance
```

`briefing today` 返回账户资产、实时持仓盈亏、
国内市场与国际宏观风险、组合级候选资金分配、AI 自选股生命周期和未读通知。使用
`--no-refresh` 可以只读取最近缓存，避免主动刷新行情和候选状态。

`decision research` 返回不可变事实底稿、证据 ID、硬约束、软警告、账户与组合风险边界。
`decision baseline` 把候选划分为 `buy_now`、`condition_order`、`wait`、`avoid`，但仅是
软件对照基线，不再冒充 Codex 最终结论。`decision propose` 保存严格结构化的 Codex 提案并
立即执行首次硬风控校验；`decision validate` 使用最新时间敏感数据再次校验；
`decision final` 并列返回研究包、软件基线、Codex 提案、校验与用户确认状态。
已有提案时，`decision final --no-refresh` 固定读取该提案绑定的不可变研究包；若软件基线
已变化、校验已过期或研究包缺失，返回 `revalidation_required=true` 和明确原因，不会
静默切换研究包或把已保存提案清空。

`condition_order` 不是普通限价单。只有实时行情有效、券商能力已核实支持独立触发价和
独立委托限价、且计划同时提供 `trigger_price` 与 `order_limit_price` 时才会出现。
若界面只有一个“委托价”字段，不能使用该动作；研究触发价不得当作实际委托价。

软件校验器只接受或拒绝提案，不会静默修改股票、数量、价格或操作方式。只有校验为
`valid` 且未过期的提案才能由用户运行以下命令确认：

```bash
agentctl decision confirm \
  --proposal-id PROPOSAL_ID \
  --validation-id VALIDATION_ID \
  --accept \
  --reason '我已自行核对' \
  --confirm
```

该命令只保存确认事件，`execution_status` 仍为 `not_executed`，不会连接券商或自动下单。
拒绝时改用 `--reject`；`--accept` 与 `--reject` 必须二选一。Codex 或 Hermes 不应代替
用户运行确认命令。

旧 `decision today` 仍保留兼容，用于读取软件四分类快照并登记盘中跟踪计划。
`decision history`
读取修订历史；`decision performance` 只统计真实触发并已关闭的 `shadow_trade_v1`，输出
净收益、沪深 300 alpha、分组表现和受限校准状态。旧的生成后涨跌估算不会混入这些指标。

`--view full` 保留完整证据包，也是默认值；`--view summary` 返回四个决策桶的紧凑字段；
`--view actionable` 只返回 `buy_now` 和 `condition_order`。需要核查某只股票时使用
`decision explain --code CODE`，它返回所属决策桶以及完整价格计划、画像证据、风险原因和
组合影响。三个视图均来自同一份 `/api/decision/today` 快照，不会在 CLI 中重新计算结论。

查询账户和持仓：

```bash
agentctl --pretty dashboard
agentctl --pretty holdings summary
agentctl --pretty holdings list
agentctl --pretty holdings trades
```

研究“科技 + 新质生产力”候选并加入自选：

```bash
agentctl --pretty candidates run --max-candidates 5
agentctl --pretty candidates latest --refresh
agentctl --pretty candidates performance
agentctl candidates add-favorites --run-id RUN_ID --code 600406 --code 601138
agentctl --pretty favorites list
```

查询实时行情和研究资料：

```bash
agentctl --pretty stocks quote --code 600406
agentctl --pretty stocks fundamentals --code 600406
agentctl --pretty stocks kline --code 600406 --period day --limit 120
agentctl --pretty stocks news --code 600406 --days 30
agentctl --pretty holdings market-status
agentctl --pretty holdings earnings --code 600406
agentctl --pretty holdings notices --code 600406
agentctl --pretty holdings opportunities --refresh
```

以上持仓研究命令均通过认证 API 在 Docker 后端执行；CLI 不再在调用方本机计算市场门禁、
业绩或公告，也不直接读取 MongoDB。

提交并跟踪 Web 分析任务：

```bash
agentctl analysis start --code 600406 --research-depth 标准
agentctl --pretty analysis list --limit 10
agentctl --pretty analysis status --task-id TASK_ID
agentctl --pretty analysis result --task-id TASK_ID
agentctl --pretty reports list --code 600406
agentctl --pretty reports get --report-id REPORT_ID
```

系统管理动作先用 `admin routes` 查询。所有非只读管理动作必须显式确认：

```bash
agentctl --pretty admin call --resource cache --action stats
agentctl admin call --resource cache --action cleanup \
  --query-json '{"days":7}' --confirm
```

## Hermes 提示词

```text
你可以使用本机 A-TradingAgents CLI 读取我的真实账户和研究结果。

命令入口：/Users/zhaok/Desktop/TradingAgents-CN/.venv/bin/agentctl
后端地址：http://localhost:8331

CLI 通过 Docker 后端 API 读取账户数据，不依赖当前工作目录，也不要直接连接 MongoDB。

规则：
1. 下文的 agentctl 均指上面的绝对路径。所有事实数据必须通过 agentctl 查询，不要猜测持仓、价格、报告或候选。
2. 第一次工作先运行 agentctl auth status。若 authenticated=false，停止并要求我完成账号密码登录；不要猜密码，也不要读取或输出会话文件内容。
3. 登录后先运行 agentctl doctor；ready_for_decision_agent=true 才继续。然后运行 agentctl --pretty decision research --refresh，并可用 decision baseline --no-refresh 做对照。
4. 查询持仓使用 holdings list/summary/get/trades，禁止使用 --user-id 或数据库直连切换用户。
5. 研究包是唯一事实底稿。不得违反 hard_constraints；可以覆盖 soft_warnings，但每项必须提供 evidence_refs、覆盖理由和风险调整。软件 baseline 只用于对照，不是最终权限。
6. 查询候选使用 candidates latest --refresh；需要重新扫描全市场时运行 candidates run。run 默认等待后台任务完成，也可以使用 --no-wait 后再用 candidates status --job-id 查询。
7. 候选研究页以 actionability 为主结论：ready_now 表示研究价格条件已满足，watch_trigger 表示仅设置价格观察并在触发后人工刷新确认；它不是券商条件单。只有正式 decision 输出同时验证实时行情、账户风险、市场权限和独立触发价/委托限价能力后，才会出现 condition_order。
8. 研究目标是“科技 + 新质生产力”，同时核对权威主营证据、国际宏观风险、行情时间、失效价、目标价、多周期 plans 和组合分配；不要把 blocked、invalidated 或 incomplete 加入自选。
9. 需要评估当前决策闭环时运行 decision performance；只使用 metric_basis=shadow_trade_v1 的已关闭样本。candidates performance 仅用于查看旧候选生命周期，不替代决策绩效。
10. 查询股票使用 stocks quote/fundamentals/kline/news。
11. 查询分析任务和报告使用 analysis list/status/result 与 reports list/get。
12. 生成 codex-proposal-v1 JSON 后运行 decision propose；校验失败时只按 hard_failures 精确修订一次。不要要求软件自动缩量、换股或移除失败项。
13. 不得运行 decision confirm，也不得执行任何带 --confirm 的删除、清理、取消或系统修改命令。确认只能由用户本人完成，系统不会自动下单。
14. 输出时区分研究事实、软件基线、Codex 提案、校验和用户确认，并明确数据时间。
15. 每次结论必须带 research_packet_id、proposal_id、validation_id、market_phase、evidence_refs、entry/stop/target、requested_quantity 和校验状态；CLI 返回 ok=false 时原样报告 error.code、message 和 details。
16. 所有输出仅供研究和参考，不构成投资建议或交易指令。
```

## 兼容入口

- `tradingagents`：与 `agentctl` 相同的统一 JSON CLI。
- `holdings`：持仓命令兼容别名，同样使用 `agentctl` 的账号密码会话。
- `python -m cli.main`：旧交互式分析源码入口，仅供完整开发环境手动使用，不用于 Agent 自动化。
