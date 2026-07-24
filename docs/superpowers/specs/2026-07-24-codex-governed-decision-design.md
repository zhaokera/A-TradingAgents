# Codex 受约束主决策架构设计

## 状态

- 高层方案已由用户于 2026-07-24 确认。
- 本文定义后续实施范围和验收边界。
- 当前阶段只完成设计，不修改现有业务决策行为。

## 1. 背景

当前链路把 `decision today` 生成的 `buy_now`、`condition_order`、`wait`
和 `avoid` 当作最终软件状态，Codex 只能归纳和解释，不能把 `wait` 或
`avoid` 升级。这样虽然确定、稳定、可审计，但实际决策权仍然在软件规则中，
与“软件做分析、Codex 做决策”的目标不一致。

目标架构必须同时避免两个极端：

1. 软件先完成全部选股和仓位决策，Codex 只改写文字；
2. Codex 直接读取零散原始资料并自由决策，绕过行情新鲜度、资金、整手、
   仓位和计划亏损约束。

采用的方案是“受约束的 Codex 主决策”：

- 软件拥有事实生产权和硬风险否决权；
- Codex 拥有最终选股、操作方式和请求仓位的决策权；
- 软件只验证 Codex 提案，不静默改写提案；
- 用户拥有最终确认权；
- 不接入券商自动下单。

所有投资相关输出继续明确为研究和参考用途，不构成投资建议或交易指令。

## 2. 目标与非目标

### 2.1 功能目标

1. 软件生成结构化、带来源、带时间戳的 `ResearchPacket`。
2. 软件明确区分不可覆盖的 `hard_constraints` 和可解释覆盖的
   `soft_warnings`。
3. Codex 基于一个确定版本的研究包提交结构化 `CodexDecisionProposal`。
4. 软件使用确定性 `DecisionValidator` 校验提案。
5. 软件保存研究包、软件基线、Codex 提案、校验、用户确认和结果追踪之间的
   完整引用关系。
6. 保留现有确定性四分类作为 `software_baseline`，用于降级展示、对照和回测，
   但不再冒充 Codex 的最终结论。
7. 允许 Codex 覆盖市场红灯、技术偏弱等软警告，但必须记录理由和相应风险调整。
8. 任何立即买入结论都必须在展示给用户前重新验证实时行情和账户约束。

### 2.2 非功能目标

- **确定性**：相同研究包、提案、策略版本和校验时点输入必须得到相同校验结果。
- **可审计**：所有重要产物不可变，使用 ID、版本号和内容哈希关联。
- **隔离性**：所有 API 按 `current_user["id"]` 隔离，不能接受调用方指定
  `user_id`。
- **可降级**：Codex 不可用时只展示软件基线，不把软件基线静默提升为 Codex
  最终决策。
- **兼容性**：现有行情、候选、持仓、分析、软件决策快照和结果追踪能力继续复用。
- **安全性**：Codex 不直接访问数据库、API Key 或券商；外部新闻和报告正文按
  不可信数据处理。
- **可运维**：可以通过单一配置切换 `software_baseline`、`codex_shadow` 和
  `codex_validated` 三种权威模式。

### 2.3 非目标

- 不自动提交券商订单或条件单。
- 不在后端增加另一个 LLM 来替代 Codex。
- 不让校验器根据自己的偏好重新选股或修改数量。
- 不重写现有候选发现、多智能体分析、行情、持仓或结果追踪系统。
- 不根据少量样本自动调整硬风险规则。
- 不把缺失的数据编造为完整证据。

## 3. 总体架构

```text
行情/基本面/新闻/技术面/持仓/账户
                  |
                  v
          ResearchPacketBuilder
                  |
        +---------+------------------+
        |                            |
        v                            v
SoftwareBaselineDecider       Codex Decision Agent
        |                            |
        |                    CodexDecisionProposal
        |                            |
        |                            v
        |                    DecisionValidator
        |                            |
        +------------+---------------+
                     v
              Decision Workspace
                     |
                     v
                 用户确认
                     |
                     v
             影子结果与效果评估
```

### 3.1 权责边界

| 层级 | 职责 | 不允许做的事 |
|---|---|---|
| 软件研究层 | 采集数据、建立证据、生成价格计划候选、计算风险边界 | 不提前替 Codex 完成最终选择 |
| 软件基线层 | 按现有规则生成四分类，供对照、降级和回测 | 不作为 Codex 必须服从的最终状态 |
| Codex 决策层 | 选股、确定主辅仓、选择立即/回调/突破/等待、请求数量并解释软警告覆盖 | 不引用研究包外的虚构事实，不违反硬约束 |
| 软件校验层 | 重算现金、数量、价格、止损、计划亏损、时效和组合上限 | 不静默减少数量、换股票或改变操作方式 |
| 用户确认层 | 接受或拒绝经过验证的提案 | 不由 Codex 代替用户执行带确认的操作 |

## 4. 决策产物

### 4.1 ResearchPacket

研究包是 Codex 决策的唯一事实底稿。新增
`decision_research_packets` 集合，按修订追加，不覆盖旧记录。

顶层字段：

```json
{
  "research_packet_id": "rp_...",
  "material_hash": "...",
  "as_of": "...",
  "market_session": {},
  "account": {},
  "holdings": [],
  "market": {},
  "decision_objective": {},
  "hard_constraints": [],
  "soft_warnings": [],
  "candidates": [],
  "data_quality": {},
  "versions": {
    "research_schema": "research-v1",
    "policy": "policy-v1",
    "evidence": "evidence-v1"
  }
}
```

每个候选至少包含：

- 股票代码、名称、交易所和交易状态；
- 最新价格、交易所成交时间、数据源和新鲜度；
- 基本面、技术面、新闻、公司画像及字段级证据；
- 回调、突破等可选价格计划；
- 已识别风险和数据缺口；
- 最小交易单位、最大允许数量、最大仓位和最大计划亏损边界；
- 与现有持仓及其他候选的行业、主题和相关性信息；
- 可供 Codex 引用的稳定 `evidence_id`。

缺失值使用 `null` 和结构化错误码表示，不能用模型推断补齐事实字段。

### 4.2 SoftwareBaseline

现有 `DailyDecisionService` 的四分类继续存在，但重新定义为软件基线：

```json
{
  "baseline_id": "...",
  "research_packet_id": "rp_...",
  "authority": "software_baseline",
  "is_final_decision": false,
  "buy_now": [],
  "condition_order": [],
  "wait": [],
  "avoid": [],
  "rule_version": "decision-v1"
}
```

现有 `daily_decisions` 集合继续保存基线快照，新增
`research_packet_id` 引用。现有 `decision today` 在兼容期继续返回基线，
但必须明确输出 `authority=software_baseline` 和
`is_final_decision=false`，文档不再称其为最终状态。

### 4.3 CodexDecisionProposal

Codex 提案必须符合固定 JSON Schema。后端不保存无法通过结构校验的自由文本。

```json
{
  "research_packet_id": "rp_...",
  "proposal_schema_version": "codex-proposal-v1",
  "decision_scope": {
    "max_new_positions": 2,
    "primary_position_count": 1
  },
  "selections": [
    {
      "symbol": "600406",
      "action": "condition_order",
      "position_role": "primary",
      "requested_quantity": 300,
      "entry_strategy": "pullback",
      "trigger_price": 21.20,
      "stop_price": 20.10,
      "target_price": 23.80,
      "expires_at": "...",
      "confidence": 0.78,
      "thesis": "...",
      "evidence_refs": ["evidence_quote_1", "evidence_technical_3"],
      "overrides": [
        {
          "warning_code": "market_red",
          "reason": "...",
          "risk_adjustment": "reduced_position"
        }
      ]
    }
  ],
  "portfolio_rationale": "...",
  "no_action_reason": null,
  "prompt_version": "codex-decision-v1"
}
```

约束：

- `decision_scope` 来自研究包中的用户决策策略，示例中的数量不是固定常量。
- `action` 只能为 `buy_now`、`condition_order`、`wait` 或 `avoid`。
- `buy_now` 和 `condition_order` 必须包含数量、价格计划和失效时间。
- `wait` 和 `avoid` 不得带可执行数量。
- Codex 请求数量，校验器只接受或拒绝，不能静默修改。
- `confidence` 仅作解释和统计，不能绕过仓位或亏损上限。
- 每个事实性理由必须引用研究包中的 `evidence_id`。
- 软警告覆盖必须逐项写明理由；未声明的覆盖视为提案格式错误。
- 空仓是合法最终决策，必须给出 `no_action_reason`。

提案保存在 `codex_decision_proposals` 集合中。相同
`research_packet_id`、提案规范版本和规范化提案内容使用内容哈希去重。

### 4.4 DecisionValidation

校验结果单独追加到 `decision_validations` 集合：

```json
{
  "validation_id": "dv_...",
  "proposal_id": "cp_...",
  "research_packet_id": "rp_...",
  "validated_at": "...",
  "status": "valid",
  "hard_failures": [],
  "accepted_overrides": [],
  "recalculated": {
    "total_cost": 6360.00,
    "planned_loss": 330.00,
    "position_weight_pct": 12.50
  },
  "quote_check": {},
  "valid_until": "...",
  "trigger_time_revalidation_required": true,
  "validator_version": "decision-validator-v1"
}
```

状态只有：

- `valid`：当前校验通过；
- `invalid`：存在硬约束失败；
- `stale_revalidation_required`：提案结构有效，但时间敏感数据已过期。

如果任意一个可执行选择失败，整个提案不成为最终可执行决策。系统返回精确失败码，
由 Codex 重新提交修正版；系统不自动删除失败股票，也不保留部分提案作为最终结果。

### 4.5 UserConfirmation

用户确认作为独立追加事件保存，至少包含：

- `proposal_id` 和最新 `validation_id`；
- `accepted` 或 `rejected`；
- 操作用户和时间；
- 拒绝原因；
- 确认时校验是否仍在有效期内。

确认只能由已认证用户通过前端或显式带 `--confirm` 的 CLI 操作完成。Codex 的标准
决策任务禁止执行带 `--confirm` 的命令。

## 5. 硬约束与软警告

### 5.1 分类原则

只有法律、市场机制、数据真实性、账户能力和用户明确配置的风险上限可以成为硬约束。
模型判断、市场观点、打分和启发式偏好默认只能成为软警告。

同一个规则可以因用户策略而改变级别，但必须在研究包中记录
`classification_source` 和策略版本，不能由 Codex 临时改变。

### 5.2 默认硬约束

1. 认证失败或无法证明数据属于当前用户。
2. 总资产、可用现金或现有持仓等关键账户数据缺失。
3. 股票代码无效、停牌、退市整理状态不允许交易，或存在必须重新定价的公司行动。
4. `buy_now` 不在可交易时段，或交易所行情时间超过当前新鲜度限制。
5. 数量不是 A 股整手规则允许的数量，或价格不符合交易所最小变动单位。
6. 现金不足，或超过用户明确配置的单股仓位、总仓位、单笔计划亏损和新增仓位
   总计划亏损上限。
7. 做多计划中入场价、止损价、目标价和失效时间的数学关系不合法。
8. 达到用户明确配置为硬限制的行业、主题或相关性上限。
9. 提案引用不存在的研究包、候选或证据。
10. 研究包材料哈希、用户、策略版本与提案不匹配。

### 5.3 默认软警告

1. 国内指数或综合市场状态为红灯或黄灯。
2. 技术形态偏弱、趋势未确认或量价不理想。
3. 估值偏高、盈利预期偏弱或催化剂不确定。
4. 外部宏观风险尚未核验，但账户、股票和实时行情等关键数据完整。
5. 公司画像、主营构成或非关键新闻字段不完整。
6. 行业或主题集中度偏高但未超过用户硬上限。
7. 软件排名较低或软件基线给出 `wait`、`avoid`，且原因不属于硬约束。

`market_red` 默认不再直接令新增仓位预算归零。若用户显式配置
`market_red_blocks_new_positions=true`，它才成为硬约束；否则 Codex 可以选择
空仓，也可以在说明理由和风险调整后提交小仓提案。

## 6. API 与 CLI 契约

### 6.1 API

新增：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/decision/research/today` | 获取或刷新不可变研究包 |
| GET | `/api/decision/baseline/today` | 获取软件基线 |
| POST | `/api/decision/proposals` | 提交并进行首次校验 |
| POST | `/api/decision/proposals/{proposal_id}/validate` | 使用最新时间敏感数据重新校验 |
| GET | `/api/decision/final/today` | 获取最新 Codex 提案、校验和确认状态 |
| POST | `/api/decision/proposals/{proposal_id}/confirm` | 用户显式接受或拒绝 |

所有接口使用 `Depends(get_current_user)`。写接口需要幂等键，所有查询条件包含
`current_user["id"]`。

### 6.2 CLI

新增：

```bash
agentctl --pretty decision research --refresh
agentctl --pretty decision baseline
agentctl --pretty decision propose --payload-json '{...}'
agentctl --pretty decision validate --proposal-id cp_... --refresh-quote
agentctl --pretty decision final
agentctl --pretty decision confirm --proposal-id cp_... --confirm
```

兼容行为：

- `briefing today` 继续作为账户、持仓和市场摘要；
- `decision today` 在兼容期作为 `decision baseline` 的别名；
- `decision explain --code CODE` 增加研究证据、硬约束、软警告、最新 Codex
  选择和校验结果，但保留当前软件基线信息；
- CLI 错误继续返回结构化 `error.code`、`message` 和 `details`。

## 7. 服务边界与代码映射

### 7.1 新增服务

1. `DecisionResearchService`
   - 从现有 `DailyBriefingService`、候选研究、公司画像、行情和组合约束中构建
     `ResearchPacket`；
   - 负责材料哈希、快照修订和持久化；
   - 不生成最终四分类。

2. `CodexDecisionProposalService`
   - 校验 JSON Schema；
   - 验证研究包引用和用户归属；
   - 规范化、去重并持久化 Codex 提案；
   - 不调用 LLM。

3. `DecisionValidationService`
   - 执行所有硬约束；
   - 重新读取必要的账户和实时行情；
   - 计算现金、权重和计划亏损；
   - 返回接受或拒绝，不修改提案。

4. `DecisionConfirmationService`
   - 保存用户接受或拒绝事件；
   - 只允许确认仍处于有效期且校验通过的提案。

### 7.2 调整现有服务

- `DailyDecisionService`
  - 初期保留为软件基线实现；
  - 将 `_compose_packet` 中的事实组装逐步提取到 `DecisionResearchService`；
  - 基线只消费 `ResearchPacket`，避免再次独立拉取一套可能不同的数据。

- `DecisionReviewService`
  - 同时记录 `software_baseline` 和 `codex_validated` 的分组表现；
  - 只把已验证且由用户确认或明确进入影子跟踪的 Codex 提案纳入 Codex 指标；
  - 不用 Codex 少量结果自动修改硬约束。

- `app/routers/decision.py`
  - 增加研究、提案、校验、最终状态和确认接口；
  - 保留当前历史与表现接口。

- `cli/agent.py`
  - 增加对应命令和紧凑视图；
  - 所有修改性命令遵循现有 `--confirm` 保护。

- `docs/cli/CODEX_DECISION_PROMPT.md`
  - 删除“不得升级 `wait`/`avoid`”；
  - 改为“不得违反硬约束；可以覆盖软警告，但必须给出引用、理由和风险调整”；
  - Codex 先取研究包，再提交提案并读取校验结果。

- 前端
  - 同时展示软件事实/基线、Codex 提案、硬风控校验；
  - 对软警告覆盖显示显著标记；
  - 只有校验通过且未过期时显示用户确认入口。

## 8. 决策流程

### 8.1 正常流程

1. 软件刷新账户、持仓、候选、市场和行情。
2. `DecisionResearchService` 生成不可变 `ResearchPacket`。
3. `DailyDecisionService` 基于同一研究包生成软件基线。
4. Codex 读取研究包和基线；基线仅供参考。
5. Codex 提交结构化提案。
6. 后端验证提案 Schema、证据引用、用户和研究包哈希。
7. `DecisionValidationService` 刷新必要行情并执行硬约束。
8. 通过后生成带有效期的最终候选决策；失败则返回精确失败码。
9. Codex 最多基于失败码提交一个修正版；仍失败则停止，不循环猜测。
10. 用户在前端或 CLI 显式确认或拒绝。
11. 已确认提案或启用影子模式的提案进入结果追踪。

### 8.2 即时买入

- 必须处于 `live_am` 或 `live_pm`；
- 必须使用交易所成交时间判断新鲜度；
- 校验结果包含短有效期 `valid_until`；
- 用户确认时超过有效期，必须重新校验；
- 重新校验后价格条件不再满足，状态变为
  `stale_revalidation_required`，不能沿用旧结论。

### 8.3 条件单

- 盘前、午间和盘后可以校验计划结构、资金边界和条件价格；
- 结果必须标记 `trigger_time_revalidation_required=true`；
- 条件触发时仍需验证停牌、公司行动、资金、仓位和最新行情；
- 当前版本只提供研究用途的条件设置参数，不直接连接券商。

## 9. 失败处理

| 失败场景 | 系统行为 |
|---|---|
| 关键账户或实时行情缺失 | 生成硬失败，不产生可执行数量 |
| 非关键画像或宏观数据缺失 | 保留研究包并生成软警告 |
| Codex 不可用 | 展示软件基线，明确 `is_final_decision=false` |
| Codex 输出不是合法 Schema | 返回 422，不保存为正式提案 |
| 提案引用旧研究包 | 返回 `research_packet_stale` 并要求刷新 |
| 校验期间价格变化 | 返回 `stale_revalidation_required` |
| 任一选择违反硬约束 | 整体提案无效，不静默保留部分选择 |
| Mongo 持久化失败 | 返回 503，不把未审计结果展示为最终决策 |
| Validator 不可用 | 不允许用户确认 |
| 软件基线与 Codex 冲突 | 并列展示，最终权威取经过验证的 Codex 提案 |
| 用户未确认 | 保持研究或影子状态，不描述为已执行 |

## 10. 安全设计

1. Codex 只通过密码会话认证后的 `agentctl` 或 API 工作。
2. Codex 不读取 MongoDB、JWT 密钥、LLM Key 或本地会话文件。
3. 研究包和日志不包含 API Key、Token、密码或完整认证头。
4. 新闻、研报和网页正文作为不可信数据，只能成为有边界的证据文本，不能被解释为
   CLI 命令或系统指令。
5. Prompt 使用结构化字段和明确分隔，忽略证据正文中的指令性内容。
6. 后端按用户过滤研究包、提案、校验和确认；管理员身份不绕过数据归属。
7. Codex 不运行带 `--confirm` 的命令，不拥有自动下单权限。
8. 每个产物保存 Schema、Prompt、策略和校验器版本，支持事后复现。

## 11. 可观测性与审计

每次链路至少关联：

- `research_packet_id`
- `baseline_id`
- `proposal_id`
- `validation_id`
- `confirmation_id`
- `plan_id`
- `material_hash`
- `policy_version`
- `prompt_version`
- `validator_version`

需要统计：

- 研究包生成成功率和关键数据缺失率；
- Codex 提案格式拒绝率；
- 各硬约束失败次数；
- 软警告覆盖率及覆盖原因；
- 软件基线与 Codex 的分歧率；
- 用户接受率；
- 影子触发率、净收益、止损率、最大回撤和相对基准表现；
- `software_baseline` 与 `codex_validated` 在相同研究包上的对照表现。

日志只记录 ID、错误码和必要元数据，不记录秘密或完整模型输入。

## 12. 测试策略

### 12.1 单元测试

- 硬约束和软警告分类；
- 市场红灯默认是软警告，用户显式禁止时变为硬约束；
- 提案 JSON Schema 和证据引用；
- 现金、整手、价格精度、仓位、计划亏损和价格关系；
- 校验器不修改 Codex 股票、数量或操作方式；
- 立即买入与条件单的不同时效规则；
- 内容哈希、去重和版本匹配。

### 12.2 契约测试

- API 认证、用户隔离、错误结构和幂等性；
- CLI `research`、`baseline`、`propose`、`validate`、`final` 输出；
- 现有 `decision today` 兼容别名；
- 前后端共享类型和枚举一致。

### 12.3 集成测试

- 使用模拟 Mongo、Redis、行情和候选数据跑完整链路；
- Codex 提案通过、失败、过期和修正一次的路径；
- 并发刷新研究包与重复提交提案；
- 数据库或行情服务失败时的安全降级；
- 多用户不能读取或引用彼此产物。

### 12.4 端到端验收场景

1. 软件基线因唯一原因 `market_red` 给出 `avoid`，Codex 在未违反其他硬约束时可提交
   小仓提案，校验通过并记录覆盖理由。
2. Codex 请求数量超过现金或计划亏损上限时，校验必须拒绝且不能静默缩量。
3. `buy_now` 行情过期时必须重新验证，不能继续显示可确认。
4. Codex 不选择任何股票时，空仓结论可以合法保存和确认。
5. Codex 不可用时只显示软件基线，界面不得标注为 Codex 最终决策。
6. 相同研究包、提案和策略输入得到相同的非时间敏感校验结果。

## 13. 发布与迁移

通过 `decision_authority_mode` 分阶段启用：

### 阶段一：契约拆分

- 新增研究包、硬/软分类和软件基线标识；
- 保持现有页面和 `decision today` 行为兼容；
- 建立新集合和索引；
- 默认模式为 `software_baseline`。

### 阶段二：Codex 影子模式

- 启用提案、校验和审计；
- Codex 结果与软件基线并列展示，但不作为默认结论；
- 收集格式失败、硬失败、分歧和影子表现；
- 模式为 `codex_shadow`。

### 阶段三：Codex 验证模式

- 经过身份校验和硬风控验证的 Codex 提案成为主要决策展示；
- 用户确认仍是强制步骤；
- 软件基线保留为对照和故障展示；
- 模式为 `codex_validated`。

### 阶段四：效果复盘

- 比较相同研究包上的软件基线和 Codex 结果；
- 仅对软排名权重提出有界校准建议；
- 硬约束修改必须由用户显式配置并产生新策略版本。

回滚只需切回 `software_baseline`，不删除研究包、提案、校验、确认或结果历史。

## 14. 关键架构决策

### ADR-001：Codex 是外部决策权威

- **决定**：Codex 通过认证 CLI/API 读取研究包和提交提案，后端不再调用另一个 LLM
  生成最终结论。
- **原因**：符合“Codex 做决策”的目标，避免两个模型互相覆盖，并复用现有
  `agentctl` 边界。
- **代价**：Codex 不可用时没有新的最终结论。
- **缓解**：保留明确标识的预测软件基线，但不静默冒充最终决策。

### ADR-002：软件保留硬风险否决权

- **决定**：软件可以拒绝提案，但不能替 Codex 修改股票、数量或操作方式。
- **原因**：把确定性交易约束与主观判断分离，保持安全和可审计。
- **代价**：Codex 提案可能需要修正一次。
- **缓解**：研究包提前提供每只候选的最大允许数量和精确失败条件。

### ADR-003：市场红灯默认是软警告

- **决定**：`market_red` 默认不再把新增仓位预算强制归零。
- **原因**：市场状态是模型判断而非交易机制，直接归零会重新把最终决策权交给软件。
- **代价**：Codex 可以在弱市场中选择开仓。
- **缓解**：强制记录覆盖理由、风险调整和结果；用户可以显式把红灯配置成硬约束。

### ADR-004：保留软件基线

- **决定**：不删除现有四分类和历史快照，只改变其权威语义。
- **原因**：降低迁移风险，并允许公平对照、回测和故障展示。
- **代价**：界面和文档必须同时解释两个结论。
- **缓解**：使用清晰的 `authority` 和 `is_final_decision` 字段。

### ADR-005：所有决策产物追加保存

- **决定**：研究包、提案、校验和确认分别追加保存并通过 ID 引用。
- **原因**：校验依赖时间，不能覆盖旧结果，否则无法复现当时结论。
- **代价**：新增集合和索引。
- **缓解**：沿用现有不可变快照、修订和材料哈希模式。

## 15. 验收标准

本架构实现完成必须同时满足：

1. Codex 能在只有软警告时与软件基线做出不同选择。
2. Codex 不能绕过现金、整手、行情新鲜度、价格合法性和用户风险上限。
3. 校验器不会静默改变 Codex 提案。
4. 页面和 CLI 能清楚区分事实、软件基线、Codex 提案、校验和用户确认。
5. 每个结论可以追溯到确定研究包、证据、策略、Prompt 和校验器版本。
6. Codex、行情、数据库或校验服务失败时不会产生伪造的可执行结论。
7. 现有软件基线、历史和表现接口保持可用。
8. 多用户数据严格隔离。
9. 不自动下单，所有投资输出保持研究参考用途声明。
