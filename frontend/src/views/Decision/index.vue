<template>
  <div class="decision-workspace" v-loading="loading">
    <header class="command-header">
      <div class="header-copy">
        <p class="eyebrow">GOVERNED DECISION · 受约束主决策</p>
        <h1>决策工作台</h1>
        <p class="header-description">
          软件整理事实与硬边界，Codex 提交选择，校验器只接受或拒绝，最终由你确认。
        </p>
      </div>
      <div class="header-actions">
        <button
          class="terminal-button terminal-button--ghost"
          type="button"
          :disabled="loading"
          @click="loadWorkspace(false)"
        >
          <el-icon><Refresh /></el-icon>
          读取最新状态
        </button>
        <button
          class="terminal-button"
          type="button"
          :disabled="loading"
          @click="loadWorkspace(true)"
        >
          刷新研究包
        </button>
      </div>
      <div v-if="workspace" class="authority-seal">
        <span
          class="authority-seal__dot"
          :class="{ 'authority-seal__dot--active': workspace.is_final_decision }"
        />
        <div>
          <small>当前展示权限</small>
          <strong>{{ authorityLabel(workspace.authority) }}</strong>
        </div>
        <span class="authority-mode">{{ workspace.authority_mode }}</span>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      class="load-error"
      type="error"
      :closable="false"
      show-icon
      :title="loadError"
    />

    <template v-if="workspace">
      <section class="status-grid" aria-label="决策概览">
        <article class="metric-card">
          <span class="metric-card__index">01 / ACCOUNT</span>
          <small>账户总资产</small>
          <strong>{{ formatMoney(research.account.total_assets) }}</strong>
          <p>可用资金 {{ formatMoney(research.account.available_cash) }}</p>
        </article>
        <article class="metric-card">
          <span class="metric-card__index">02 / MARKET</span>
          <small>市场状态</small>
          <strong :class="regimeClass">
            {{ regimeLabel(research.market.combined_regime) }}
          </strong>
          <p>{{ phaseLabel(marketPhase) }} · {{ marketPhase || 'unknown' }}</p>
        </article>
        <article class="metric-card">
          <span class="metric-card__index">03 / BUDGET</span>
          <small>可新增敞口</small>
          <strong>
            {{ formatPercent(research.hard_risk_policy.available_new_exposure_pct) }}
          </strong>
          <p>
            单票硬上限
            {{ formatPercent(research.hard_risk_policy.hard_single_symbol_cap_pct) }}
          </p>
        </article>
        <article class="metric-card">
          <span class="metric-card__index">04 / EVIDENCE</span>
          <small>研究数据状态</small>
          <strong :class="{ 'text-warning': unclassifiedCodes.length }">
            {{ unclassifiedCodes.length ? '需要复核' : '已完成分类' }}
          </strong>
          <p>{{ formatTime(research.created_at) }}</p>
        </article>
      </section>

      <section class="decision-rail" aria-label="决策责任链">
        <div class="rail-step rail-step--done">
          <span>01</span>
          <div>
            <small>FACTS</small>
            <strong>研究包</strong>
            <p>{{ research.candidates.length }} 个候选 · 不可变快照</p>
          </div>
        </div>
        <div class="rail-line" />
        <div class="rail-step rail-step--reference">
          <span>02</span>
          <div>
            <small>REFERENCE</small>
            <strong>软件基线</strong>
            <p>只对照，不是最终权限</p>
          </div>
        </div>
        <div class="rail-line" />
        <div
          class="rail-step"
          :class="workspace.codex_proposal ? 'rail-step--done' : 'rail-step--pending'"
        >
          <span>03</span>
          <div>
            <small>JUDGEMENT</small>
            <strong>Codex 提案</strong>
            <p>{{ workspace.codex_proposal ? '已提交结构化选择' : '等待外部 Codex 提交' }}</p>
          </div>
        </div>
        <div class="rail-line" />
        <div
          class="rail-step"
          :class="validationStepClass"
        >
          <span>04</span>
          <div>
            <small>GUARDRAIL</small>
            <strong>硬风控校验</strong>
            <p>{{ validationLabel(workspace.validation?.status) }}</p>
          </div>
        </div>
        <div class="rail-line" />
        <div
          class="rail-step"
          :class="workspace.confirmation ? 'rail-step--done' : 'rail-step--pending'"
        >
          <span>05</span>
          <div>
            <small>HUMAN</small>
            <strong>用户确认</strong>
            <p>{{ confirmationLabel }}</p>
          </div>
        </div>
      </section>

      <section class="workspace-section">
        <div class="section-heading">
          <div>
            <p class="section-kicker">RESEARCH PACKET</p>
            <h2>候选事实与风险边界</h2>
          </div>
          <div class="packet-id" :title="research.research_packet_id">
            <span>PACKET</span>
            {{ shortId(research.research_packet_id) }}
          </div>
        </div>

        <div v-if="unclassifiedCodes.length" class="quality-warning">
          <el-icon><WarningFilled /></el-icon>
          存在未分类原因码：{{ unclassifiedCodes.join('、') }}。涉及候选会被硬拦截，需人工核验。
        </div>

        <div v-if="research.candidates.length" class="candidate-grid">
          <article
            v-for="candidate in research.candidates"
            :key="candidate.symbol"
            class="candidate-card"
          >
            <div class="candidate-card__top">
              <div>
                <p class="symbol">{{ candidate.symbol }}</p>
                <h3>{{ candidate.name }}</h3>
                <span class="segment">
                  {{ candidate.identity.objective_segment || '未分类主题' }}
                </span>
              </div>
              <span
                class="action-chip"
                :class="`action-chip--${candidate.software_baseline_action}`"
              >
                软件基线 · {{ actionLabel(candidate.software_baseline_action) }}
              </span>
            </div>

            <div class="quote-row">
              <div>
                <small>腾讯参考价</small>
                <strong>{{ formatPrice(candidate.quote.price) }}</strong>
              </div>
              <div class="quote-meta">
                <span>{{ candidate.quote.source || '无来源' }}</span>
                <span :class="{ 'text-warning': candidate.quote.status !== 'fresh' }">
                  {{ candidate.quote.status || 'unknown' }}
                </span>
                <small>{{ formatTime(candidate.quote.trade_at) }}</small>
              </div>
            </div>

            <div class="price-ladder">
              <div>
                <span>触发 / 入场</span>
                <strong>{{ formatPrice(shortPlan(candidate).entry_price) }}</strong>
              </div>
              <div>
                <span>失效 / 止损</span>
                <strong>{{ formatPrice(shortPlan(candidate).stop_price) }}</strong>
              </div>
              <div>
                <span>目标</span>
                <strong>{{ formatPrice(shortPlan(candidate).target_price) }}</strong>
              </div>
            </div>

            <div class="risk-strip">
              <div>
                <small>最大允许数量</small>
                <strong>{{ candidate.risk_envelope.max_allowed_quantity || 0 }} 股</strong>
              </div>
              <div>
                <small>最大计划亏损</small>
                <strong>
                  {{ formatMoney(candidate.risk_envelope.max_planned_loss_amount) }}
                </strong>
              </div>
              <div>
                <small>证据</small>
                <strong>{{ candidate.evidence.length }} 项</strong>
              </div>
            </div>

            <div class="constraint-block">
              <div>
                <small>硬约束</small>
                <div class="tag-stack">
                  <span
                    v-for="constraint in candidate.hard_constraints"
                    :key="constraint.code"
                    class="constraint-tag constraint-tag--hard"
                  >
                    {{ constraint.code }}
                  </span>
                  <span
                    v-if="!candidate.hard_constraints.length"
                    class="constraint-tag constraint-tag--clear"
                  >
                    无候选级硬拦截
                  </span>
                </div>
              </div>
              <div>
                <small>可解释软警告</small>
                <div class="tag-stack">
                  <span
                    v-for="warning in candidate.soft_warnings"
                    :key="warning.code"
                    class="constraint-tag constraint-tag--soft"
                  >
                    {{ warning.code }}
                  </span>
                  <span
                    v-if="!candidate.soft_warnings.length"
                    class="constraint-tag constraint-tag--quiet"
                  >
                    无
                  </span>
                </div>
              </div>
            </div>
          </article>
        </div>
        <el-empty v-else description="当前研究包没有候选股票" />
      </section>

      <section class="workspace-section proposal-section">
        <div class="section-heading">
          <div>
            <p class="section-kicker">CODEX PROPOSAL</p>
            <h2>最终软决策提案</h2>
          </div>
          <span
            v-if="workspace.codex_proposal"
            class="proposal-id"
            :title="workspace.codex_proposal.proposal_id"
          >
            {{ shortId(workspace.codex_proposal.proposal_id) }}
          </span>
        </div>

        <template v-if="proposalPayload">
          <div class="proposal-thesis">
            <small>组合理由</small>
            <p>{{ proposalPayload.portfolio_rationale }}</p>
            <p v-if="proposalPayload.no_action_reason" class="no-action-reason">
              空仓理由：{{ proposalPayload.no_action_reason }}
            </p>
          </div>

          <div v-if="proposalPayload.selections.length" class="selection-list">
            <article
              v-for="selection in proposalPayload.selections"
              :key="selection.symbol"
              class="selection-card"
            >
              <div class="selection-card__identity">
                <span class="role-stamp" :class="`role-stamp--${selection.position_role}`">
                  {{ roleLabel(selection.position_role) }}
                </span>
                <div>
                  <strong>{{ selectionName(selection.symbol) }}</strong>
                  <small>{{ selection.symbol }} · {{ actionLabel(selection.action) }}</small>
                </div>
                <span class="confidence">
                  信心 {{ Math.round(selection.confidence * 100) }}%
                </span>
              </div>
              <p class="selection-thesis">{{ selection.thesis }}</p>
              <div class="selection-plan">
                <span>数量 <strong>{{ selection.requested_quantity || 0 }} 股</strong></span>
                <span>触发 <strong>{{ formatPrice(selection.trigger_price) }}</strong></span>
                <span>止损 <strong>{{ formatPrice(selection.stop_price) }}</strong></span>
                <span>目标 <strong>{{ formatPrice(selection.target_price) }}</strong></span>
              </div>
              <div class="evidence-line">
                <span>EVIDENCE</span>
                <code v-for="reference in selection.evidence_refs" :key="reference">
                  {{ reference }}
                </code>
              </div>
              <div v-if="selection.overrides.length" class="override-list">
                <div
                  v-for="override in selection.overrides"
                  :key="override.warning_code"
                >
                  <strong>覆盖 {{ override.warning_code }}</strong>
                  <p>{{ override.reason }}</p>
                  <small>风险调整：{{ override.risk_adjustment }}</small>
                </div>
              </div>
            </article>
          </div>
          <div v-else class="no-action-panel">
            <DocumentChecked />
            <div>
              <strong>Codex 选择不新增仓位</strong>
              <p>这是合法的结构化结论，仍需通过校验并由用户确认。</p>
            </div>
          </div>
        </template>

        <div v-else class="codex-pending">
          <div class="codex-pending__mark">C</div>
          <div>
            <strong>等待 Codex 提交结构化提案</strong>
            <p>
              请由 Codex 通过 <code>agentctl decision research</code> 读取事实，再使用
              <code>decision propose</code> 提交。此页面不内置第二个 LLM，也不接受自由文本。
            </p>
          </div>
        </div>
      </section>

      <section class="workspace-section validation-section">
        <div class="section-heading">
          <div>
            <p class="section-kicker">DETERMINISTIC VALIDATOR</p>
            <h2>确定性硬风控</h2>
          </div>
          <button
            v-if="workspace.codex_proposal"
            class="revalidate-button"
            type="button"
            :disabled="revalidating"
            @click="revalidate"
          >
            <el-icon :class="{ spinning: revalidating }"><Refresh /></el-icon>
            {{ revalidating ? '校验中' : '用最新行情重校验' }}
          </button>
        </div>

        <template v-if="workspace.validation">
          <div
            class="validation-banner"
            :class="`validation-banner--${workspace.validation.status}`"
          >
            <div class="validation-icon">
              <CircleCheckFilled v-if="workspace.validation.status === 'valid'" />
              <WarningFilled v-else />
            </div>
            <div>
              <small>VALIDATION STATUS</small>
              <strong>{{ validationLabel(workspace.validation.status) }}</strong>
              <p>
                校验于 {{ formatTime(workspace.validation.validated_at) }}
                <template v-if="workspace.validation.valid_until">
                  · 有效至 {{ formatTime(workspace.validation.valid_until) }}
                </template>
              </p>
            </div>
            <span v-if="isValidationExpired" class="expired-stamp">已过期</span>
          </div>

          <div class="validation-metrics">
            <div>
              <small>重新计算资金</small>
              <strong>{{ formatMoney(workspace.validation.recalculated.total_cost) }}</strong>
            </div>
            <div>
              <small>重新计算计划亏损</small>
              <strong>
                {{ formatMoney(workspace.validation.recalculated.total_planned_loss) }}
              </strong>
            </div>
            <div>
              <small>新增仓位占比</small>
              <strong>
                {{ formatPercent(workspace.validation.recalculated.total_position_weight_pct) }}
              </strong>
            </div>
            <div>
              <small>触发时重校验</small>
              <strong>
                {{ workspace.validation.trigger_time_revalidation_required ? '需要' : '不需要' }}
              </strong>
            </div>
          </div>

          <div
            v-if="workspace.validation.hard_failures.length"
            class="failure-ledger"
          >
            <div class="failure-ledger__head">
              <span>硬失败清单</span>
              <strong>{{ workspace.validation.hard_failures.length }}</strong>
            </div>
            <div
              v-for="failure in workspace.validation.hard_failures"
              :key="`${failure.symbol || 'portfolio'}-${failure.code}`"
              class="failure-row"
            >
              <span>{{ failure.symbol || '组合级' }}</span>
              <code>{{ failure.code }}</code>
              <p>{{ failure.message || failureDetail(failure.details) }}</p>
            </div>
          </div>
          <div v-else class="validation-clear">
            <CircleCheckFilled />
            <div>
              <strong>未发现硬约束冲突</strong>
              <p>校验器没有改写股票、数量、操作方式或价格。</p>
            </div>
          </div>

          <div v-if="workspace.confirmation" class="confirmation-receipt">
            <div>
              <small>USER CONFIRMATION</small>
              <strong>
                {{ workspace.confirmation.accepted ? '用户已接受' : '用户已拒绝' }}
              </strong>
              <p>{{ workspace.confirmation.reason || '未填写原因' }}</p>
            </div>
            <div class="receipt-meta">
              <span>{{ formatTime(workspace.confirmation.confirmed_at) }}</span>
              <code>{{ workspace.confirmation.execution_status }}</code>
            </div>
          </div>

          <div v-else-if="canConfirm" class="confirmation-zone">
            <div>
              <small>HUMAN GATE</small>
              <strong>校验有效，等待你本人决定</strong>
              <p>接受或拒绝都只保存审计记录，不会自动下单。</p>
            </div>
            <div class="confirmation-actions">
              <el-button
                type="danger"
                plain
                :loading="confirming"
                @click="recordConfirmation(false)"
              >
                拒绝提案
              </el-button>
              <el-button
                type="success"
                :loading="confirming"
                @click="recordConfirmation(true)"
              >
                我已核对并接受
              </el-button>
            </div>
          </div>
          <div v-else-if="workspace.codex_proposal" class="confirmation-locked">
            <Lock />
            <div>
              <strong>当前不可确认</strong>
              <p>请先解决硬失败或使用最新行情重新校验。</p>
            </div>
          </div>
        </template>
        <div v-else class="validation-empty">
          <Timer />
          <p>Codex 提案提交后，这里会展示独立的硬风控校验记录。</p>
        </div>
      </section>

      <footer class="research-disclaimer">
        <span>RESEARCH ONLY</span>
        <p>
          {{ workspace.disclaimer }}
          页面确认仅形成审计记录，不连接券商、不自动执行，也不替代你的独立判断。
        </p>
      </footer>
    </template>

    <div v-else-if="!loading" class="empty-workspace">
      <DataAnalysis />
      <h2>尚未取得决策工作台</h2>
      <p>确认后端已启动并登录，再刷新研究包。</p>
      <el-button type="primary" @click="loadWorkspace(true)">重新获取</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CircleCheckFilled,
  DataAnalysis,
  DocumentChecked,
  Lock,
  Refresh,
  Timer,
  WarningFilled
} from '@element-plus/icons-vue'
import {
  decisionApi,
  type CodexDecisionProposalPayload,
  type DecisionAction,
  type DecisionCandidate,
  type DecisionValidationStatus,
  type DecisionWorkspace
} from '@/api/decision'

const workspace = ref<DecisionWorkspace | null>(null)
const loading = ref(false)
const revalidating = ref(false)
const confirming = ref(false)
const loadError = ref('')

const research = computed(() => workspace.value!.research_packet)
const proposalPayload = computed<CodexDecisionProposalPayload | null>(
  () => workspace.value?.codex_proposal?.payload || null
)
const marketPhase = computed(
  () =>
    research.value.market_session?.phase ||
    research.value.software_baseline?.market_phase ||
    ''
)
const unclassifiedCodes = computed(
  () => research.value.data_quality?.unclassified_reason_codes || []
)
const regimeClass = computed(() => {
  const regime = String(research.value.market.combined_regime || '').toLowerCase()
  return {
    'text-danger': regime === 'red',
    'text-warning': regime === 'yellow',
    'text-success': regime === 'green'
  }
})
const isValidationExpired = computed(() => {
  const validation = workspace.value?.validation
  if (!validation?.valid_until) return false
  const validUntil = new Date(validation.valid_until).getTime()
  return !Number.isFinite(validUntil) || validUntil <= Date.now()
})
const isValidationCurrent = computed(() => {
  const validation = workspace.value?.validation
  return Boolean(
    validation &&
    validation.status === 'valid' &&
    !isValidationExpired.value
  )
})
const canConfirm = computed(
  () =>
    Boolean(workspace.value?.codex_proposal) &&
    isValidationCurrent.value &&
    !workspace.value?.confirmation
)
const validationStepClass = computed(() => {
  const status = workspace.value?.validation?.status
  if (status === 'valid') return 'rail-step--done'
  if (status === 'invalid') return 'rail-step--failed'
  return 'rail-step--pending'
})
const confirmationLabel = computed(() => {
  const confirmation = workspace.value?.confirmation
  if (!confirmation) return '等待用户本人确认'
  return confirmation.accepted ? '已接受 · 未执行' : '已拒绝 · 未执行'
})

const loadWorkspace = async (refreshResearch = false) => {
  loading.value = true
  loadError.value = ''
  try {
    if (refreshResearch) {
      await decisionApi.getResearch(true)
    }
    const response = await decisionApi.getFinal(false)
    workspace.value = response.data
  } catch (error: any) {
    loadError.value = error?.message || '无法读取决策工作台'
  } finally {
    loading.value = false
  }
}

const revalidate = async () => {
  const proposalId = workspace.value?.codex_proposal?.proposal_id
  if (!proposalId) return
  revalidating.value = true
  try {
    await decisionApi.revalidate(proposalId, true)
    await loadWorkspace(false)
    ElMessage.success('已使用最新时间敏感数据重新校验')
  } catch (error: any) {
    ElMessage.error(error?.message || '重新校验失败')
  } finally {
    revalidating.value = false
  }
}

const recordConfirmation = async (accepted: boolean) => {
  const proposal = workspace.value?.codex_proposal
  const validation = workspace.value?.validation
  if (!proposal || !validation || !canConfirm.value) return
  try {
    const result = await ElMessageBox.prompt(
      accepted
        ? '确认已独立核对研究包、价格、数量和止损。此操作不会自动下单。'
        : '请说明拒绝该提案的原因，系统只保存审计记录。',
      accepted ? '接受 Codex 提案' : '拒绝 Codex 提案',
      {
        confirmButtonText: accepted ? '确认接受' : '确认拒绝',
        cancelButtonText: '取消',
        inputPlaceholder: accepted ? '填写你的核对说明' : '填写拒绝原因',
        inputValidator: value => Boolean(String(value || '').trim()) || '请填写确认原因',
        type: accepted ? 'warning' : 'error'
      }
    )
    confirming.value = true
    await decisionApi.confirm(proposal.proposal_id, {
      validation_id: validation.validation_id,
      accepted,
      reason: String(result.value).trim()
    })
    await loadWorkspace(false)
    ElMessage.success(accepted ? '已记录接受；系统未执行交易' : '已记录拒绝')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error?.message || '记录确认失败')
    }
  } finally {
    confirming.value = false
  }
}

const shortPlan = (candidate: DecisionCandidate) => candidate.plans?.short || {}

const formatMoney = (value?: number | null) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return '¥—'
  }
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2
  }).format(Number(value))
}

const formatPrice = (value?: number | string | null) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? `¥${parsed.toFixed(2)}` : '—'
}

const formatPercent = (value?: number | null) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}%` : '—'
}

const formatTime = (value?: string | null) => {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(parsed)
}

const shortId = (value?: string | null) => {
  const text = String(value || '')
  return text.length > 20 ? `${text.slice(0, 12)}…${text.slice(-6)}` : text
}

const authorityLabel = (value?: string) =>
  value === 'codex_validated' ? 'Codex 已校验提案' : '软件参考基线'

const actionLabel = (value?: DecisionAction | string) =>
  ({
    buy_now: '立即买入',
    condition_order: '条件单',
    wait: '等待',
    avoid: '回避'
  })[String(value)] || value || '未知'

const roleLabel = (value: string) =>
  ({ primary: '主仓', secondary: '辅仓', none: '不建仓' })[value] || value

const validationLabel = (value?: DecisionValidationStatus) =>
  ({
    valid: '校验通过',
    invalid: '硬约束拒绝',
    stale_revalidation_required: '数据已变化，需重校验'
  })[String(value)] || '尚未校验'

const phaseLabel = (value?: string) =>
  ({
    live_am: '上午盘中',
    live_pm: '下午盘中',
    pre_open: '开盘前',
    lunch_break: '午间休市',
    off_session: '非交易时段'
  })[String(value)] || '交易阶段未知'

const regimeLabel = (value?: string) =>
  ({ red: '红灯', yellow: '黄灯', green: '绿灯' })[
    String(value || '').toLowerCase()
  ] || '未判定'

const selectionName = (symbol: string) => {
  const candidate = research.value.candidates.find(item => item.symbol === symbol)
  return candidate?.name || symbol
}

const failureDetail = (details?: Record<string, unknown>) => {
  if (!details || !Object.keys(details).length) return '请按失败码修订提案'
  return Object.entries(details)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(' · ')
}

onMounted(() => loadWorkspace(false))
</script>

<style lang="scss" scoped>
.decision-workspace {
  --ink: #10212b;
  --muted: #60717a;
  --paper: #f4f1e9;
  --panel: #fffdf7;
  --line: rgba(16, 33, 43, 0.14);
  --cyan: #2d8f88;
  --cyan-dark: #14645f;
  --amber: #e99a2a;
  --red: #c44a46;
  --green: #3f8863;
  min-height: calc(100vh - 170px);
  color: var(--ink);
  font-family: "Avenir Next", "Noto Sans SC", "PingFang SC", sans-serif;
}

.command-header {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 26px;
  overflow: hidden;
  padding: 34px 36px 94px;
  color: #f7f1df;
  background:
    linear-gradient(rgba(255, 255, 255, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.035) 1px, transparent 1px),
    radial-gradient(circle at 76% 20%, rgba(45, 143, 136, 0.32), transparent 34%),
    #0e2029;
  background-size: 28px 28px, 28px 28px, auto, auto;
  border-radius: 18px 18px 6px 6px;
  box-shadow: 0 24px 60px rgba(7, 22, 29, 0.18);

  &::after {
    position: absolute;
    right: -45px;
    bottom: -104px;
    width: 310px;
    height: 310px;
    border: 1px solid rgba(255, 181, 69, 0.4);
    border-radius: 50%;
    content: "";
  }
}

.eyebrow,
.section-kicker {
  margin: 0 0 9px;
  color: #7de0d6;
  font-family: "DIN Alternate", "Avenir Next Condensed", sans-serif;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.16em;
}

.header-copy h1 {
  margin: 0;
  font-family: "Noto Serif SC", "Songti SC", serif;
  font-size: clamp(32px, 4vw, 56px);
  font-weight: 700;
  letter-spacing: 0.04em;
  line-height: 1.12;
}

.header-description {
  max-width: 720px;
  margin: 14px 0 0;
  color: rgba(247, 241, 223, 0.72);
  font-size: 15px;
  line-height: 1.8;
}

.header-actions {
  z-index: 1;
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.terminal-button,
.revalidate-button {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  min-height: 42px;
  padding: 0 17px;
  color: #10212b;
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  background: #f0ab43;
  border: 1px solid #f0ab43;
  border-radius: 4px;
  transition: transform 160ms ease, box-shadow 160ms ease, opacity 160ms ease;

  &:hover:not(:disabled) {
    box-shadow: 0 9px 24px rgba(233, 154, 42, 0.22);
    transform: translateY(-2px);
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
}

.terminal-button--ghost {
  color: #f7f1df;
  background: rgba(255, 255, 255, 0.05);
  border-color: rgba(255, 255, 255, 0.24);
}

.authority-seal {
  position: absolute;
  bottom: 22px;
  left: 36px;
  display: flex;
  gap: 12px;
  align-items: center;

  small {
    display: block;
    color: rgba(247, 241, 223, 0.55);
    font-size: 10px;
    letter-spacing: 0.12em;
  }

  strong {
    font-size: 14px;
  }
}

.authority-seal__dot {
  width: 10px;
  height: 10px;
  background: #89969b;
  border: 2px solid rgba(255, 255, 255, 0.24);
  border-radius: 50%;
  box-shadow: 0 0 0 5px rgba(255, 255, 255, 0.04);
}

.authority-seal__dot--active {
  background: #79d9a7;
  box-shadow: 0 0 0 5px rgba(121, 217, 167, 0.12);
}

.authority-mode {
  padding: 5px 8px;
  margin-left: 8px;
  color: #91a5ae;
  font-family: "DIN Alternate", monospace;
  font-size: 11px;
  border: 1px solid rgba(255, 255, 255, 0.14);
}

.load-error {
  margin-top: 14px;
}

.status-grid {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin: -4px 18px 0;
  background: var(--line);
  border: 1px solid var(--line);
  box-shadow: 0 16px 38px rgba(16, 33, 43, 0.1);
}

.metric-card {
  min-width: 0;
  padding: 22px 24px;
  background: var(--panel);

  small {
    display: block;
    margin-bottom: 8px;
    color: var(--muted);
    font-size: 12px;
  }

  strong {
    display: block;
    overflow: hidden;
    font-family: "DIN Alternate", "Avenir Next Condensed", sans-serif;
    font-size: clamp(21px, 2vw, 30px);
    font-weight: 700;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  p {
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 12px;
  }
}

.metric-card__index {
  display: block;
  margin-bottom: 18px;
  color: var(--cyan);
  font-family: "DIN Alternate", monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.13em;
}

.text-danger { color: var(--red) !important; }
.text-warning { color: var(--amber) !important; }
.text-success { color: var(--green) !important; }

.decision-rail {
  display: grid;
  grid-template-columns: auto minmax(18px, 1fr) auto minmax(18px, 1fr) auto minmax(18px, 1fr) auto minmax(18px, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 32px 18px 10px;
}

.rail-line {
  height: 1px;
  background: var(--line);
}

.rail-step {
  display: flex;
  gap: 10px;
  align-items: center;
  min-width: 112px;

  > span {
    display: grid;
    flex: 0 0 30px;
    width: 30px;
    height: 30px;
    place-items: center;
    color: #7f8c92;
    font-family: "DIN Alternate", monospace;
    font-size: 11px;
    border: 1px solid #aab4b8;
    border-radius: 50%;
  }

  small {
    display: block;
    color: #859399;
    font-size: 9px;
    letter-spacing: 0.1em;
  }

  strong {
    display: block;
    font-size: 13px;
  }

  p {
    max-width: 140px;
    margin: 2px 0 0;
    color: var(--muted);
    font-size: 10px;
  }
}

.rail-step--done > span {
  color: #fff;
  background: var(--green);
  border-color: var(--green);
}

.rail-step--reference > span {
  color: var(--cyan-dark);
  background: #e4f2ef;
  border-color: #8fbfba;
}

.rail-step--failed > span {
  color: #fff;
  background: var(--red);
  border-color: var(--red);
}

.rail-step--pending {
  opacity: 0.66;
}

.workspace-section {
  padding: 30px;
  margin-top: 24px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 12px 36px rgba(16, 33, 43, 0.06);
}

.section-heading {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 24px;

  h2 {
    margin: 0;
    font-family: "Noto Serif SC", "Songti SC", serif;
    font-size: 25px;
    letter-spacing: 0.03em;
  }
}

.section-kicker {
  color: var(--cyan-dark);
  font-size: 10px;
}

.packet-id,
.proposal-id {
  max-width: 280px;
  padding: 8px 11px;
  overflow: hidden;
  color: var(--muted);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: #f0eee7;
  border-left: 3px solid var(--amber);

  span {
    margin-right: 6px;
    color: var(--cyan-dark);
    font-family: "DIN Alternate", sans-serif;
    font-weight: 700;
  }
}

.quality-warning {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 14px;
  margin: -6px 0 20px;
  color: #7d5010;
  font-size: 12px;
  line-height: 1.6;
  background: #fff3d9;
  border: 1px solid #efcf91;
}

.candidate-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.candidate-card {
  position: relative;
  overflow: hidden;
  padding: 22px;
  background: #faf8f1;
  border: 1px solid var(--line);
  border-top: 3px solid #173b49;

  &::after {
    position: absolute;
    top: 0;
    right: 0;
    width: 54px;
    height: 54px;
    clip-path: polygon(100% 0, 0 0, 100% 100%);
    background: rgba(45, 143, 136, 0.08);
    content: "";
  }
}

.candidate-card__top {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;

  h3 {
    margin: 3px 0 4px;
    font-size: 20px;
  }
}

.symbol {
  margin: 0;
  color: var(--cyan-dark);
  font-family: "DIN Alternate", monospace;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.1em;
}

.segment {
  color: var(--muted);
  font-size: 11px;
}

.action-chip {
  z-index: 1;
  flex: 0 0 auto;
  padding: 6px 9px;
  color: #4a5a61;
  font-size: 11px;
  font-weight: 700;
  background: #edf0ee;
  border: 1px solid #d1d8d4;
}

.action-chip--buy_now {
  color: #8b3b34;
  background: #fbe5df;
  border-color: #edb9ad;
}

.action-chip--condition_order {
  color: #89601c;
  background: #fff0ce;
  border-color: #ebca83;
}

.action-chip--wait {
  color: #355f73;
  background: #e5f0f4;
  border-color: #afd0de;
}

.action-chip--avoid {
  color: #596168;
  background: #eceeec;
  border-color: #ced3d0;
}

.quote-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding: 18px 0 14px;
  margin-top: 16px;
  border-bottom: 1px solid var(--line);

  small {
    display: block;
    color: var(--muted);
    font-size: 10px;
  }

  strong {
    display: block;
    margin-top: 2px;
    font-family: "DIN Alternate", sans-serif;
    font-size: 29px;
  }
}

.quote-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--muted);
  font-size: 10px;

  span {
    padding: 3px 5px;
    background: #ecebe5;
  }
}

.price-ladder,
.risk-strip,
.selection-plan,
.validation-metrics {
  display: grid;
  gap: 10px;
}

.price-ladder {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  padding: 14px 0;

  div {
    padding-left: 10px;
    border-left: 2px solid #d8d3c5;
  }

  span,
  small {
    display: block;
    color: var(--muted);
    font-size: 10px;
  }

  strong {
    display: block;
    margin-top: 4px;
    font-family: "DIN Alternate", sans-serif;
    font-size: 15px;
  }
}

.risk-strip {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  padding: 12px;
  background: #132c36;

  small {
    display: block;
    color: #8ea4ad;
    font-size: 9px;
  }

  strong {
    color: #f8f0dc;
    font-family: "DIN Alternate", sans-serif;
    font-size: 13px;
  }
}

.constraint-block {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding-top: 16px;

  small {
    display: block;
    margin-bottom: 7px;
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
  }
}

.tag-stack {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.constraint-tag {
  padding: 3px 6px;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 9px;
  border: 1px solid;
}

.constraint-tag--hard {
  color: #983c36;
  background: #fce7e3;
  border-color: #e5a7a1;
}

.constraint-tag--soft {
  color: #86580c;
  background: #fff1d4;
  border-color: #e6c277;
}

.constraint-tag--clear {
  color: #31745a;
  background: #e1f2e9;
  border-color: #a8d3bd;
}

.constraint-tag--quiet {
  color: #647178;
  background: #eff0ec;
  border-color: #d4d7d2;
}

.proposal-section {
  background:
    linear-gradient(90deg, rgba(45, 143, 136, 0.03), transparent 34%),
    var(--panel);
}

.proposal-thesis {
  padding: 18px 20px;
  margin-bottom: 16px;
  background: #eef4f1;
  border-left: 3px solid var(--cyan);

  small {
    color: var(--cyan-dark);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
  }

  p {
    margin: 7px 0 0;
    line-height: 1.7;
  }

  .no-action-reason {
    color: var(--muted);
    font-size: 12px;
  }
}

.selection-list {
  display: grid;
  gap: 14px;
}

.selection-card {
  padding: 20px;
  background: #fffdf8;
  border: 1px solid var(--line);
}

.selection-card__identity {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;

  strong {
    display: block;
    font-size: 17px;
  }

  small {
    color: var(--muted);
    font-size: 11px;
  }
}

.role-stamp {
  display: grid;
  width: 46px;
  height: 46px;
  place-items: center;
  color: #fff;
  font-family: "Noto Serif SC", serif;
  font-size: 12px;
  background: #173b49;
  border-radius: 50%;
}

.role-stamp--secondary { background: var(--cyan); }
.role-stamp--none { background: #7b8589; }

.confidence {
  color: var(--cyan-dark);
  font-family: "DIN Alternate", sans-serif;
  font-size: 12px;
}

.selection-thesis {
  margin: 15px 0;
  color: #32454e;
  line-height: 1.7;
}

.selection-plan {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 12px 14px;
  background: #f2f0e9;

  span {
    color: var(--muted);
    font-size: 10px;
  }

  strong {
    display: block;
    margin-top: 3px;
    color: var(--ink);
    font-family: "DIN Alternate", sans-serif;
    font-size: 13px;
  }
}

.evidence-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 13px;

  > span {
    color: var(--cyan-dark);
    font-family: "DIN Alternate", sans-serif;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
  }

  code {
    padding: 3px 6px;
    color: #4e626b;
    font-size: 9px;
    background: #edf1ef;
  }
}

.override-list {
  display: grid;
  gap: 8px;
  margin-top: 13px;

  > div {
    padding: 10px 12px;
    color: #704d15;
    background: #fff3d9;
    border-left: 2px solid var(--amber);
  }

  strong,
  p,
  small {
    display: block;
    margin: 0;
    font-size: 10px;
  }

  p { margin-top: 4px; }
  small { margin-top: 3px; color: #8d682c; }
}

.no-action-panel,
.codex-pending,
.validation-clear,
.confirmation-locked,
.validation-empty {
  display: flex;
  gap: 16px;
  align-items: center;
  padding: 24px;
  color: var(--muted);
  background: #f1f1eb;
  border: 1px dashed #b8c0bc;

  svg {
    flex: 0 0 34px;
    width: 34px;
    height: 34px;
    color: var(--cyan);
  }

  strong {
    display: block;
    color: var(--ink);
  }

  p {
    margin: 5px 0 0;
    font-size: 12px;
    line-height: 1.7;
  }
}

.codex-pending__mark {
  display: grid;
  flex: 0 0 46px;
  width: 46px;
  height: 46px;
  place-items: center;
  color: #f7f1df;
  font-family: "Noto Serif SC", serif;
  font-size: 22px;
  background: #173b49;
}

.codex-pending code {
  padding: 2px 4px;
  color: #1e6863;
  background: #e2ebe7;
}

.revalidate-button {
  min-height: 38px;
  color: var(--cyan-dark);
  background: transparent;
  border-color: #80afa9;
}

.spinning {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.validation-banner {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 15px;
  align-items: center;
  padding: 18px;
  color: #2c5d48;
  background: #e4f3ea;
  border: 1px solid #afd7bf;

  small {
    display: block;
    font-size: 9px;
    letter-spacing: 0.12em;
  }

  strong {
    display: block;
    margin-top: 2px;
    font-size: 18px;
  }

  p {
    margin: 4px 0 0;
    font-size: 11px;
  }
}

.validation-banner--invalid,
.validation-banner--stale_revalidation_required {
  color: #843f38;
  background: #fae8e4;
  border-color: #e7b3ab;
}

.validation-icon svg {
  width: 30px;
  height: 30px;
}

.expired-stamp {
  padding: 5px 8px;
  color: #8f3933;
  font-size: 11px;
  font-weight: 700;
  border: 1px solid #c97d76;
}

.validation-metrics {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin-top: 12px;
  background: var(--line);
  border: 1px solid var(--line);

  div {
    padding: 15px;
    background: #faf8f1;
  }

  small {
    display: block;
    color: var(--muted);
    font-size: 10px;
  }

  strong {
    display: block;
    margin-top: 5px;
    font-family: "DIN Alternate", sans-serif;
    font-size: 16px;
  }
}

.failure-ledger {
  margin-top: 14px;
  border: 1px solid #e1aaa4;
}

.failure-ledger__head,
.failure-row {
  display: grid;
  grid-template-columns: 110px 240px minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 11px 14px;
}

.failure-ledger__head {
  color: #fff;
  background: #963f3a;

  strong {
    grid-column: 3;
    justify-self: end;
  }
}

.failure-row {
  font-size: 11px;
  border-top: 1px solid #efd3cf;

  code {
    color: #963f3a;
    font-weight: 700;
  }

  p {
    margin: 0;
    color: var(--muted);
  }
}

.validation-clear {
  margin-top: 14px;
  color: #2f6d52;
  background: #edf6f0;
  border-style: solid;
  border-color: #bddbc7;
}

.confirmation-receipt,
.confirmation-zone {
  display: flex;
  gap: 20px;
  align-items: center;
  justify-content: space-between;
  padding: 20px;
  margin-top: 18px;
  background: #102630;
  border-left: 4px solid var(--amber);

  small {
    display: block;
    color: #87a1aa;
    font-size: 9px;
    letter-spacing: 0.12em;
  }

  strong {
    display: block;
    margin-top: 3px;
    color: #f8f0dc;
    font-size: 17px;
  }

  p {
    margin: 5px 0 0;
    color: #aab9bf;
    font-size: 11px;
  }
}

.receipt-meta {
  display: flex;
  flex-direction: column;
  gap: 5px;
  align-items: flex-end;
  color: #aab9bf;
  font-size: 10px;

  code {
    padding: 4px 6px;
    color: #f0b45c;
    border: 1px solid #715b39;
  }
}

.confirmation-actions {
  display: flex;
  gap: 9px;
}

.confirmation-locked {
  margin-top: 16px;
  color: #7d5010;
  background: #fff4de;
  border-color: #e8c98a;
}

.validation-empty {
  justify-content: center;
  min-height: 130px;
}

.research-disclaimer {
  display: flex;
  gap: 18px;
  align-items: flex-start;
  padding: 18px 22px;
  margin: 24px 0 8px;
  color: #465c66;
  background: #e9e6dd;
  border: 1px solid #d2cec1;

  span {
    flex: 0 0 auto;
    padding: 4px 7px;
    color: #fff;
    font-family: "DIN Alternate", sans-serif;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    background: #173b49;
  }

  p {
    margin: 0;
    font-size: 11px;
    line-height: 1.8;
  }
}

.empty-workspace {
  display: grid;
  min-height: 430px;
  place-items: center;
  align-content: center;
  padding: 40px;
  text-align: center;
  background: var(--panel);
  border: 1px solid var(--line);

  > svg {
    width: 52px;
    height: 52px;
    color: var(--cyan);
  }

  h2 {
    margin: 15px 0 4px;
    font-family: "Noto Serif SC", serif;
  }

  p {
    margin: 0 0 18px;
    color: var(--muted);
  }
}

@media (max-width: 1180px) {
  .status-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .decision-rail {
    grid-template-columns: repeat(5, minmax(0, 1fr));
  }

  .rail-line {
    display: none;
  }

  .rail-step {
    min-width: 0;
  }

  .rail-step p {
    display: none;
  }
}

@media (max-width: 900px) {
  .command-header {
    grid-template-columns: 1fr;
  }

  .candidate-grid {
    grid-template-columns: 1fr;
  }

  .decision-rail {
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .validation-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .failure-ledger__head,
  .failure-row {
    grid-template-columns: 100px minmax(0, 1fr);
  }

  .failure-row p {
    grid-column: 1 / -1;
  }
}

@media (max-width: 640px) {
  .command-header {
    padding: 26px 22px 104px;
  }

  .header-actions {
    flex-wrap: wrap;
  }

  .authority-seal {
    right: 22px;
    left: 22px;
  }

  .authority-mode {
    display: none;
  }

  .status-grid {
    grid-template-columns: 1fr;
    margin-right: 8px;
    margin-left: 8px;
  }

  .decision-rail {
    grid-template-columns: 1fr;
  }

  .workspace-section {
    padding: 20px 16px;
  }

  .section-heading,
  .confirmation-zone,
  .confirmation-receipt {
    align-items: flex-start;
    flex-direction: column;
  }

  .packet-id,
  .proposal-id {
    max-width: 100%;
  }

  .quote-row,
  .candidate-card__top {
    gap: 12px;
    align-items: flex-start;
    flex-direction: column;
  }

  .quote-meta {
    flex-wrap: wrap;
  }

  .price-ladder,
  .risk-strip,
  .constraint-block,
  .selection-plan,
  .validation-metrics {
    grid-template-columns: 1fr;
  }

  .selection-card__identity {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .confidence {
    grid-column: 2;
  }

  .confirmation-actions {
    flex-direction: column-reverse;
    width: 100%;

    :deep(.el-button) {
      width: 100%;
      margin-left: 0;
    }
  }

  .receipt-meta {
    align-items: flex-start;
  }
}
</style>
