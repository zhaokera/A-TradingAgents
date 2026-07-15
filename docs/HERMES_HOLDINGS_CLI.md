# A-TradingAgents Hermes 持仓 CLI

本文档描述的是当前仓库 `zhaokera/A-TradingAgents` 的本地 CLI。磁盘目录暂时仍名为
`/Users/zhaok/Desktop/TradingAgents-CN`，该名称只是本机路径，不表示操作上游参考仓库。

Hermes 直接调用本机已安装的仓库 CLI，不需要 Web 登录，也不调用前端接口：

```bash
cd /Users/zhaok/Desktop/TradingAgents-CN
.venv/bin/holdings market-status --pretty
.venv/bin/holdings summary --pretty
.venv/bin/holdings list --pretty
.venv/bin/holdings opportunities --external-risk-level red --pretty
```

## 数据边界

- `market-status`：始终先查询腾讯四个主要指数。Mongo 可用时再读取 `market_quotes` 计算全市场涨跌宽度；Mongo 不可用时仍返回退出码 `0`，并标记 `data_completeness=indices_only`、`decision.action=wait`。
- `summary`、`list`、`trades`、`opportunities`：直接读取 Docker 映射到本机 `127.0.0.1:27017` 的 MongoDB。默认读取唯一的 `admin` 账户，不需要 Token 或登录态。
- `opportunities`：`--external-risk-level` 只接受 `green`、`yellow`、`red`。没有完成国际形势核查时不要传入乐观等级。
- 所有输出仅供研究参考，不构成投资建议或交易指令。

## 关键字段

`market-status`：

- `data.market_gate.level`：`green`、`yellow`、`red` 或 `unknown`。
- `data.market_gate.breadth_confirmation_required`：是否缺少有效全市场宽度。
- `data.data_completeness`：`indices_and_breadth`、`indices_only` 或 `unavailable`。
- `data.decision.actionable`：市场门禁证据是否足以继续评估个股。
- `data.decision.action`：`evaluate_candidates` 或 `wait`。

持仓命令：

- `summary.data.summary`：账户本金、持仓市值、现金、浮动盈亏和月目标汇总。
- `list.data.items`：当前真实持仓；已经全部卖出的股票不会继续作为当前持仓。
- `opportunities.data.a_share_market_gate`：A 股指数和市场宽度门禁。
- `opportunities.data.brief.candidate_decision_matrix.rows`：候选股逐项结论和失败门槛。
- `opportunities.data.brief.cash_deployment_plan`：通过全部门槛后才可能出现的手数参考。
- `candidate_lot_plan[].failed_gates`：兼容字段，汇总所有已评估手数出现过的约束。
- `candidate_lot_plan[].blocking_failed_gates`：最接近可执行手数的直接阻断项；解释“为什么当前是 0 手”时优先使用该字段。对应字段也会传递到 `candidate_decision_matrix.rows[]`。

## 给 Hermes 的提示词

```text
你可以直接读取我的 A-TradingAgents 本地数据。不要询问账号密码，不要调用网页，也不要编造持仓、价格或市场宽度。

工作目录固定为 /Users/zhaok/Desktop/TradingAgents-CN，CLI 固定为 .venv/bin/holdings。

执行顺序：
1. 运行 `.venv/bin/holdings market-status`，先检查 data.data_completeness、data.decision.actionable 和 data.decision.action。
2. 运行 `.venv/bin/holdings summary` 和 `.venv/bin/holdings list`，读取当前本金、现金和真实持仓。已经卖出的股票不得当作当前持仓。
3. 结合最新国际形势确定 external risk level；证据不完整时按 red 或保持 unknown，不得擅自按 green。
4. 运行 `.venv/bin/holdings opportunities --external-risk-level <green|yellow|red>`。
5. 只有 market-status 的 decision.actionable=true，且 opportunities 中候选同时通过行情时效、市场门禁、企业行动、价格计划、费后盈亏比和资金约束，才能列为“可继续评估”；否则统一写“等待”。
6. 回答必须列出 CLI 返回的生成时间、行情交易日、数据完整度、账户快照、每个候选的 `blocking_failed_gates`、兼容的全量 `failed_gates` 和下一次刷新条件。不得把只在更大手数出现的全量约束误写成一手的直接失败原因。
7. 所有结论注明“仅供研究参考，不构成投资建议或交易指令”。

如果 CLI 返回结构化 error，原样说明 error.code；如果 market-status 返回 indices_only，不得把指数结果描述成完整市场结论。
```
