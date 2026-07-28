# Hermes 使用 A-TradingAgents

统一入口是 `.venv/bin/agentctl`。`holdings` 只保留为兼容别名；候选研究、网页和
Hermes 使用同一个后端契约，不再由 CLI 单独计算另一套结果。

## 首次登录

```bash
cd /Users/zhaok/Desktop/TradingAgents-CN
.venv/bin/agentctl auth login --username admin --password '你的密码'
.venv/bin/agentctl auth status
.venv/bin/agentctl health
```

登录会话默认有效 7 天。之后 Hermes 不需要读取 Docker 数据库，也不需要进入容器。
所有账户数据均通过 `http://localhost:8331` 的认证 API 读取，执行命令时不要求切换到
仓库工作目录。

## 查询持仓

每天开始研究时先读取一站式简报：

```bash
.venv/bin/agentctl --pretty briefing today
.venv/bin/agentctl --pretty decision today --view summary
.venv/bin/agentctl --pretty decision today --view actionable
.venv/bin/agentctl --pretty decision explain --code 600406
.venv/bin/agentctl --pretty decision history --limit 20
.venv/bin/agentctl --pretty decision performance
```

该命令同时返回账户、实时持仓、国内市场与国际宏观风险、按账户资金和总止损预算分配
后的可执行候选、AI 自选股生命周期和通知。通常不需要再分别拼接多个查询命令。

`briefing today` 是完整事实底稿，`decision today` 是确定性结论。后者会保存不可变快照，
把候选分成 `buy_now`、`condition_order`、`wait`、`avoid`，并自动登记可执行计划供盘中
腾讯行情跟踪。`decision performance` 只统计真实触发并已关闭的影子交易，旧估算样本不会
混入净收益、胜率或沪深 300 alpha。

```bash
.venv/bin/agentctl --pretty holdings summary
.venv/bin/agentctl --pretty holdings list
.venv/bin/agentctl --pretty holdings trades
```

## 查询和生成候选

```bash
# 使用腾讯行情刷新最近一批候选的生命周期
.venv/bin/agentctl --pretty candidates latest --refresh

# 后台扫描全市场，并默认等待任务完成
.venv/bin/agentctl --pretty candidates run --max-candidates 5

# 不等待时，使用返回的 job_id 查询
.venv/bin/agentctl candidates run --max-candidates 5 --no-wait
.venv/bin/agentctl --pretty candidates status --job-id JOB_ID

# 查看近 90 天跟踪表现
.venv/bin/agentctl --pretty candidates performance

# 加入可跟踪的 AI 候选
.venv/bin/agentctl candidates add-favorites \
  --run-id RUN_ID --code 600406 --code 601138
```

`holdings opportunities --refresh` 是 `candidates latest --refresh` 的兼容别名，两者返回
同一份数据。

候选的 `actionability` 是主状态：

- `ready_now`：价格条件已满足。
- `watch_trigger`：候选研究价尚未触发，只能设置价格观察并在触发后人工刷新确认。
- `condition_order`：仅存在于正式 decision 包，且券商独立触发价与委托限价能力、实时行情和账户风险门槛均已验证。
- `blocked`：存在阻断风险。
- `invalidated`：价格计划已失效。
- `expired`：计划超过有效窗口，需要重新分析。
- `target_reached`：已达到原计划目标价。
- `quote_unavailable` / `incomplete`：行情或价格计划不完整。

只有 `can_add_to_favorites=true` 的候选可以通过 AI 候选接口加入自选。

## 给 Hermes 的提示词

```text
你可以使用本机 A-TradingAgents CLI 查询我的真实持仓、自选股、分析报告和 AI 候选。

CLI：/Users/zhaok/Desktop/TradingAgents-CN/.venv/bin/agentctl
后端：http://localhost:8331

CLI 通过 Docker 后端 API 读取账户数据，不依赖当前工作目录，也不要直接连接 MongoDB。

执行规则：
1. 下文的 agentctl 均指上面的绝对路径。先运行 auth status 和 doctor；未登录或 ready_for_decision_agent=false 时停止并让我修复环境。
2. 每日研究依次运行 agentctl --pretty briefing today 和 agentctl --pretty decision today；前者是事实底稿，后者是最终软件状态。
3. 持仓事实可用 holdings summary、holdings list 和 holdings trades 继续下钻，不要猜测。
4. 严格按 decision today 的 buy_now、condition_order、wait、avoid 四类输出，不自行把 wait 或 avoid 升级。buy_now 只在盘中腾讯行情新鲜且价格条件已经满足时出现。讨论具体股票前运行 decision explain --code CODE。
5. 候选先运行 candidates latest --refresh；只有需要重新扫描全市场时才运行 candidates run。
6. 以 actionability、腾讯行情时间、权威主营证据、macro_risk、plans、entry_price、stop_price、target_price、risk_flags 和 portfolio_allocation 为主结论。
7. 候选研究中的 ready_now 单独列为“研究价格条件已满足”，watch_trigger 单独列为“价格观察，触发后人工确认”。只有正式 decision 包中的 condition_order 才可列为“已验证条件单”；blocked、invalidated、expired 和 incomplete 不得混入前三类。
8. 需要查看决策效果时运行 decision performance，只使用 metric_basis=shadow_trade_v1 的已关闭样本，并报告净收益、胜率、最大回撤、止损率和沪深 300 alpha。样本不足时原样说明 calibration.status。
9. 加入自选前确认 can_add_to_favorites=true，并保留 AI 来源和 lifecycle_state 标记。
10. 查询单股使用 stocks quote、fundamentals、kline、news；查询报告使用 reports list/get。
11. 输出时必须带 decision_id、revision、market_phase、as_of、reason_codes、entry/stop/target、quantity 和 planned_loss，并明确区分 CLI 事实、软件状态和补充分析。
12. CLI 返回 ok=false 时，原样报告 error.code、message 和 details。
13. 未经我明确要求，不执行删除、清理、取消或其他带 --confirm 的命令。
```

完整命令说明见 [cli/AGENTCTL.md](cli/AGENTCTL.md)。
