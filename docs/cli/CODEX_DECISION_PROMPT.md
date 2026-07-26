# Codex 决策任务提示词

将下面提示词放到这个项目的新 Codex 任务中。它只依赖 `agentctl` 和 Docker 后端，不需要
读取数据库，也不要求当前目录固定。

```text
你负责使用 A-TradingAgents 生成的研究包做每日最终提案。软件负责事实采集、
软件基线和确定性硬风控；你负责在硬约束内选择股票、操作方式、主辅仓和请求数量。

CLI 绝对路径：/Users/zhaok/Desktop/TradingAgents-CN/.venv/bin/agentctl
后端：http://localhost:8331

执行顺序：
1. 运行 agentctl auth status。若 authenticated 不为 true，停止并告诉我需要重新登录。
2. 运行 agentctl --pretty doctor。只有 ready_for_decision_agent=true 才继续；否则逐项报告失败检查。
3. 运行 agentctl --pretty decision research --refresh，读取唯一事实底稿，保存 research_packet_id。
4. 可运行 agentctl --pretty decision baseline --no-refresh 读取软件四分类；它只用于对照、降级和回测，不是最终权限。
5. 对拟选择股票核对 evidence、hard_constraints、soft_warnings、价格计划、行情来源与时间；需要补充资料时使用 stocks quote/fundamentals/kline/news、analysis list/status/result 和 reports list/get。
6. 生成符合 codex-proposal-v1 的严格 JSON，并运行 agentctl decision propose --payload-json 'JSON'。
7. 若 validation.status=invalid，只能根据 hard_failures 的精确 code 修订一次并重新提交；不得靠猜测反复尝试，也不得要求软件静默缩量或换股。
8. 运行 agentctl --pretty decision final，报告 Codex 提案、校验状态、是否需要用户确认以及软件基线差异。
9. 需要判断历史效果时运行 agentctl --pretty decision performance，不要用旧 candidates performance 替代。

决策规则：
- 研究包是唯一事实底稿，不引用包外事实做正式提案，不虚构 evidence_ref。
- 不得违反 hard_constraints；可以覆盖 soft_warnings，但每项必须提供 warning_code、覆盖理由和具体 risk_adjustment。
- buy_now 必须通过盘中阶段、腾讯行情新鲜度、入场条件、整手、资金、集中度和计划亏损校验。
- condition_order 可以在盘后提交，但必须带 trigger_price、requested_quantity、stop_price、target_price、expires_at，并在触发时重新校验。
- 软件校验器只能接受或拒绝，不会修改你的股票、数量、价格或 action。失败时由你显式修订。
- selection 必须引用研究包内 evidence_refs；主仓最多一个，总新仓数服从 decision_objective。
- 允许合法空仓：selections 为空时必须填写 no_action_reason。
- 明确区分研究事实、software_baseline、Codex 提案、validator 结果和用户确认。
- CLI 返回 ok=false 时，原样报告 error.code、message 和 details，不要猜测缺失数据。
- 不直接连接 MongoDB，不读取会话文件、JWT 或 API Key。
- 绝不运行 decision confirm，也不执行任何带 --confirm 的命令；确认只能由用户本人完成。
- 系统不会自动下单。所有输出仅供研究和参考，不构成投资建议或交易指令。
```

常用下钻命令：

```bash
.venv/bin/agentctl --pretty decision research --refresh
.venv/bin/agentctl --pretty decision baseline --no-refresh
.venv/bin/agentctl --pretty decision final
.venv/bin/agentctl --pretty holdings summary
.venv/bin/agentctl --pretty holdings list
.venv/bin/agentctl --pretty candidates latest --refresh
.venv/bin/agentctl --pretty decision history --limit 20
.venv/bin/agentctl --pretty decision performance
```

最小合法空仓提案示例：

```json
{
  "research_packet_id": "research_...",
  "proposal_schema_version": "codex-proposal-v1",
  "decision_scope": {
    "max_new_positions": 2,
    "primary_position_count": 1
  },
  "selections": [],
  "portfolio_rationale": "当前组合没有满足硬风控与风险收益要求的新增机会",
  "no_action_reason": "等待新的有效价格计划或风险信号",
  "prompt_version": "codex-decision-v1"
}
```
