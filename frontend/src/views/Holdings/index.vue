<template>
  <div class="holdings-page">
    <div class="page-header">
      <div>
        <h1>持仓分析</h1>
        <p>保存真实持仓，设置月收益目标，按当前盈亏和目标进度生成每日仓位参考。</p>
      </div>
      <div class="header-actions">
        <el-button @click="openSettingsDialog">
          <el-icon><Edit /></el-icon>
          设置总资产
        </el-button>
        <el-button :loading="reportSubmitting" @click="generateTodayReports">
          <el-icon><Document /></el-icon>
          生成今日报告
        </el-button>
        <el-button type="primary" :loading="loading" @click="loadHoldings">
          <el-icon><Refresh /></el-icon>
          刷新价格
        </el-button>
      </div>
    </div>

    <div class="summary-grid">
      <div class="summary-tile">
        <div class="summary-label">
          <span>总资产</span>
          <el-tag size="small" :type="totalAssetsIsAuto ? 'info' : 'success'">
            {{ totalAssetsIsAuto ? '按持仓估算' : '已设置' }}
          </el-tag>
        </div>
        <strong>{{ formatMoney(effectiveTotalAssets) }}</strong>
        <div class="summary-subline">账户总规模，用于计算仓位占比</div>
      </div>
      <div class="summary-tile">
        <span>股票持仓</span>
        <strong>{{ formatMoney(totalHoldingCost) }}</strong>
        <div class="summary-subline">成本仓位 {{ formatPct(stockPositionPct) }}，{{ holdings.length }} 只</div>
      </div>
      <div class="summary-tile">
        <span>{{ cashBalance >= 0 ? '可用现金' : '资金缺口' }}</span>
        <strong :class="cashBalance >= 0 ? '' : 'profit-down'">{{ formatMoney(Math.abs(cashBalance)) }}</strong>
        <div class="summary-subline">总资产 - 股票持仓</div>
      </div>
      <div class="summary-tile">
        <span>浮动盈亏</span>
        <strong :class="profitClass(totalProfitLoss)">{{ formatMoney(totalProfitLoss, true) }}</strong>
        <div class="summary-subline">相对总资产 {{ formatPct(totalProfitLossPctOfAssets) }}</div>
      </div>
      <div class="summary-tile target-tile">
        <div class="summary-label">
          <span>月目标盈利</span>
          <el-tag size="small" type="success">{{ formatPct(weightedMonthlyTargetPct) }}</el-tag>
        </div>
        <strong>{{ formatMoney(monthlyTargetProfit) }}</strong>
        <el-progress
          :percentage="progressPercent(monthlyTargetProgressPct)"
          :status="summaryProgressStatus"
          :stroke-width="10"
          :show-text="false"
          class="summary-progress"
        />
        <div class="summary-subline">
          已完成 {{ formatPct(monthlyTargetProgressPct) }}
        </div>
      </div>
    </div>

    <el-row :gutter="20" class="main-grid">
      <el-col :span="24">
        <el-card class="table-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>我的持仓</span>
              <div class="card-actions">
                <el-tag type="info">月目标默认 10%</el-tag>
                <el-button type="primary" @click="openCreateDialog">
                  <el-icon><Plus /></el-icon>
                  新增持仓
                </el-button>
              </div>
            </div>
          </template>

          <el-table
            :data="holdings"
            v-loading="loading"
            row-key="id"
            empty-text="还没有持仓"
            class="holdings-table"
            @row-click="openHoldingDetail"
          >
            <el-table-column prop="code" label="股票" min-width="120">
              <template #default="{ row }">
                <div class="stock-cell">
                  <strong>{{ row.code }}</strong>
                  <span>{{ row.name || row.code }}</span>
                  <el-tag size="small" type="info" effect="plain">{{ strategyText(row.strategy) }}</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="仓位" min-width="190">
              <template #default="{ row }">
                <div class="position-cell">
                  <div class="position-main">
                    <strong>市值 {{ formatMoney(holdingMarketValue(row)) }}</strong>
                    <span>仓位 {{ formatPct(holdingPositionPct(row)) }}</span>
                  </div>
                  <div class="position-subline">
                    <span>数量 {{ formatShares(row.quantity) }}</span>
                    <span>成本 {{ formatPrice(row.cost_price) }}</span>
                    <span>现价 {{ formatPrice(effectiveCurrentPrice(row)) }}</span>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="盈亏" min-width="112">
              <template #default="{ row }">
                <strong :class="profitClass(row.analysis?.profit_loss)">
                  {{ formatMoney(row.analysis?.profit_loss, true) }}
                </strong>
                <div class="pnl-percent">{{ formatPct(row.analysis?.profit_loss_pct) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="价格计划" min-width="320">
              <template #default="{ row }">
                <div class="price-plan-cell">
                  <div v-for="plan in pricePlanRows(row)" :key="plan.key" class="price-plan-row">
                    <span class="plan-label" :class="`plan-label--${plan.tone}`">{{ plan.label }}</span>
                    <div class="plan-primary">
                      <strong>{{ formatAdvicePrice(plan.activePrice) }}</strong>
                      <span class="plan-source" :class="`plan-source--${plan.activeSource}`">
                        {{ planSourceText(plan.activeSource) }}
                      </span>
                      <span class="plan-distance" :class="profitClass(plan.distancePct)">
                        {{ formatSignedPct(plan.distancePct) }}
                      </span>
                    </div>
                  </div>
                  <div v-if="row.price_plan_notes" class="plan-note">{{ row.price_plan_notes }}</div>
                  <div v-if="row.price_plan_updated_at" class="muted">更新 {{ formatShortTime(row.price_plan_updated_at) }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="今日参考" min-width="180">
              <template #default="{ row }">
                <el-tag :type="actionTag(row.analysis?.action)">
                  {{ actionText(row.analysis?.action) }}
                </el-tag>
                <div class="suggestion">{{ row.analysis?.suggested_ratio_text }} / {{ row.analysis?.suggested_shares_text }}</div>
                <div class="muted">{{ row.analysis?.status }}</div>
              </template>
            </el-table-column>
            <el-table-column label="报告" min-width="190">
              <template #default="{ row }">
                <div v-if="row.ai_advice" class="ai-advice-cell">
                  <div class="advice-line">
                    <el-tag :type="actionTag(row.ai_advice.action)">
                      {{ actionText(row.ai_advice.action) }}
                    </el-tag>
                  </div>
                  <div v-if="row.ai_advice.generated_at" class="muted">{{ formatShortTime(row.ai_advice.generated_at) }}</div>
                  <el-button link type="primary" @click.stop="openReportShortcut(row)">查看报告</el-button>
                </div>
                <div v-else class="ai-advice-cell">
                  <span class="muted">暂无报告提取结论</span>
                  <el-button link type="primary" @click.stop="openReportShortcut(row)">查找报告</el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <div class="table-actions">
                  <el-button link type="warning" @click.stop="openPlanDialog(row)">价格</el-button>
                  <el-button link type="primary" @click.stop="editHolding(row)">编辑</el-button>
                  <el-button link type="danger" @click.stop="deleteHolding(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>

          <el-alert type="warning" :closable="false" class="risk-note">
            持仓分析仅用于仓位管理参考，不构成投资建议或交易指令。
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-drawer
      v-model="detailDrawerVisible"
      :title="detailTitle"
      size="520px"
      append-to-body
      class="holding-detail-drawer"
    >
      <div v-if="selectedHolding" class="holding-detail">
        <div class="detail-head">
          <div>
            <div class="detail-code">{{ selectedHolding.code }}</div>
            <div class="detail-name">{{ selectedHolding.name || selectedHolding.code }}</div>
          </div>
          <el-tag type="info" effect="plain">{{ strategyText(selectedHolding.strategy) }}</el-tag>
        </div>

        <section class="detail-section">
          <div class="section-title">持仓明细</div>
          <div class="metric-grid">
            <div class="metric-item">
              <span>数量</span>
              <strong>{{ formatShares(selectedHolding.quantity) }}</strong>
            </div>
            <div class="metric-item">
              <span>成本价</span>
              <strong>{{ formatPrice(selectedHolding.cost_price) }}</strong>
            </div>
            <div class="metric-item">
              <span>现价</span>
              <strong>{{ formatPrice(effectiveCurrentPrice(selectedHolding)) }}</strong>
            </div>
            <div class="metric-item">
              <span>市值</span>
              <strong>{{ formatMoney(holdingMarketValue(selectedHolding)) }}</strong>
            </div>
            <div class="metric-item">
              <span>仓位占比</span>
              <strong>{{ formatPct(holdingPositionPct(selectedHolding)) }}</strong>
            </div>
            <div class="metric-item">
              <span>浮动盈亏</span>
              <strong :class="profitClass(selectedHolding.analysis?.profit_loss)">
                {{ formatMoney(selectedHolding.analysis?.profit_loss, true) }}
              </strong>
            </div>
          </div>
          <div v-if="selectedHolding.notes" class="detail-note">{{ selectedHolding.notes }}</div>
        </section>

        <section class="detail-section">
          <div class="section-title section-title--with-action">
            <span>价格计划</span>
            <el-button size="small" type="warning" plain @click="openPlanDialog(selectedHolding)">编辑价格</el-button>
          </div>
          <div class="detail-plan-list">
            <div v-for="plan in pricePlanRows(selectedHolding)" :key="plan.key" class="detail-plan-item">
              <span class="plan-label" :class="`plan-label--${plan.tone}`">{{ plan.label }}</span>
              <div class="detail-plan-main">
                <strong>{{ formatAdvicePrice(plan.activePrice) }}</strong>
                <span class="plan-source" :class="`plan-source--${plan.activeSource}`">
                  {{ planSourceText(plan.activeSource) }}
                </span>
                <span class="plan-distance" :class="profitClass(plan.distancePct)">
                  {{ formatSignedPct(plan.distancePct) }}
                </span>
              </div>
              <div class="detail-plan-compare">
                手动 {{ formatAdvicePrice(plan.manualPrice) }} / 报告 {{ formatAdvicePrice(plan.reportPrice) }}
              </div>
            </div>
          </div>
          <div v-if="selectedHolding.price_plan_notes" class="detail-note">{{ selectedHolding.price_plan_notes }}</div>
        </section>

        <section class="detail-section">
          <div class="section-title section-title--with-action">
            <span>报告结论</span>
            <el-button size="small" type="primary" plain @click="openReportShortcut(selectedHolding)">跳转报告</el-button>
          </div>
          <div v-if="selectedHolding.ai_advice" class="report-detail">
            <div class="advice-line">
              <el-tag :type="actionTag(selectedHolding.ai_advice.action)">
                {{ actionText(selectedHolding.ai_advice.action) }}
              </el-tag>
              <span class="confidence">{{ formatConfidence(selectedHolding.ai_advice.confidence) }}</span>
            </div>
            <div class="report-suggestion">{{ selectedHolding.ai_advice.position_suggestion }}</div>
            <div class="reason">{{ selectedHolding.ai_advice.reason }}</div>
            <div v-if="selectedHolding.ai_advice.risks?.length" class="risk-tags">
              <el-tag
                v-for="risk in selectedHolding.ai_advice.risks"
                :key="risk"
                size="small"
                type="danger"
                effect="plain"
              >
                {{ risk }}
              </el-tag>
            </div>
            <div class="muted">
              {{ selectedHolding.ai_advice.model_name }}
              <span v-if="selectedHolding.ai_advice.generated_at"> · {{ formatShortTime(selectedHolding.ai_advice.generated_at) }}</span>
            </div>
          </div>
          <span v-else class="muted">暂无报告提取结论，可先跳转报告列表查看该股票历史报告。</span>
        </section>

        <div class="detail-actions">
          <el-button @click="editHolding(selectedHolding)">编辑持仓</el-button>
          <el-button type="danger" plain @click="deleteHolding(selectedHolding)">删除持仓</el-button>
        </div>
      </div>
    </el-drawer>

    <el-dialog
      v-model="formDialogVisible"
      :title="editingId ? '编辑持仓' : '新增持仓'"
      width="520px"
      destroy-on-close
      append-to-body
      @closed="resetForm"
    >
      <el-form label-width="96px" class="holding-form">
        <el-form-item label="股票代码" required>
          <el-input
            v-model="form.code"
            :disabled="!!editingId"
            placeholder="如 000977"
            clearable
            @blur="lookupStockName(false)"
            @change="lookupStockName(false)"
          />
        </el-form-item>
        <el-form-item label="股票名称">
          <el-input v-model="form.name" placeholder="输入代码后自动获取" clearable />
          <div class="field-tip">{{ nameTip }}</div>
        </el-form-item>
        <el-form-item label="持股数量" required>
          <el-input-number v-model="form.quantity" :min="0" :step="100" controls-position="right" />
        </el-form-item>
        <el-form-item label="成本价" required>
          <el-input-number v-model="form.cost_price" :min="0" :precision="3" :step="0.1" controls-position="right" />
        </el-form-item>
        <el-form-item label="月目标" required>
          <el-input-number v-model="form.target_monthly_return_pct" :min="0.1" :precision="2" :step="1" controls-position="right" />
          <span class="unit">%</span>
        </el-form-item>
        <el-form-item label="止损线">
          <el-input-number v-model="form.stop_loss_pct" :min="0.1" :precision="2" :step="1" controls-position="right" />
          <span class="unit">%</span>
        </el-form-item>
        <el-form-item label="策略">
          <el-select v-model="form.strategy">
            <el-option label="短线" value="short" />
            <el-option label="波段" value="swing" />
            <el-option label="长线" value="long" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :rows="3" placeholder="记录买入理由或计划" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="formDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="saveHolding">
            {{ editingId ? '保存修改' : '添加持仓' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="planDialogVisible"
      :title="`编辑价格计划 - ${planEditingLabel}`"
      width="560px"
      destroy-on-close
      append-to-body
      @closed="resetPlanForm"
    >
      <el-form label-width="108px" class="holding-form">
        <div v-if="planSourceRow?.ai_advice" class="ai-plan-panel">
          <div class="ai-plan-head">
            <span>报告价格</span>
            <el-button size="small" type="primary" plain @click="applyReportPlan">
              采用报告价格
            </el-button>
          </div>
          <div class="ai-plan-grid">
            <span>止损 {{ formatAdvicePrice(planSourceRow.ai_advice.stop_loss_price) }}</span>
            <span>目标 {{ formatAdvicePrice(planSourceRow.ai_advice.target_price) }}</span>
            <span>卖出 {{ formatAdvicePrice(planSourceRow.ai_advice.suggested_sell_price) }}</span>
            <span>追入 {{ formatAdvicePrice(planSourceRow.ai_advice.suggested_buy_price) }}</span>
          </div>
        </div>
        <el-form-item label="止损价">
          <el-input-number v-model="planForm.manual_stop_loss_price" :min="0" :precision="3" :step="0.1" controls-position="right" />
        </el-form-item>
        <el-form-item label="目标价">
          <el-input-number v-model="planForm.manual_target_price" :min="0" :precision="3" :step="0.1" controls-position="right" />
        </el-form-item>
        <el-form-item label="卖出价">
          <el-input-number v-model="planForm.manual_sell_price" :min="0" :precision="3" :step="0.1" controls-position="right" />
        </el-form-item>
        <el-form-item label="买入/追入价">
          <el-input-number v-model="planForm.manual_buy_price" :min="0" :precision="3" :step="0.1" controls-position="right" />
        </el-form-item>
        <el-form-item label="计划备注">
          <el-input v-model="planForm.price_plan_notes" type="textarea" :rows="3" placeholder="记录触发条件、分批计划或复盘结论" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="planDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="planSaving" @click="savePricePlan">
            保存价格计划
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="settingsDialogVisible"
      title="设置总资产"
      width="420px"
      append-to-body
    >
      <el-form label-width="96px" class="holding-form">
        <el-form-item label="总资产" required>
          <el-input-number
            v-model="settingsForm.total_assets"
            :min="0"
            :precision="2"
            :step="1000"
            controls-position="right"
          />
        </el-form-item>
        <div class="settings-preview">
          <span>股票持仓 {{ formatMoney(totalHoldingCost) }}</span>
          <span>保存后仓位 {{ formatPct(previewStockPositionPct) }}</span>
        </div>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="settingsDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="settingsSaving" @click="saveHoldingSettings">
            保存
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import { holdingsApi, type HoldingItem, type HoldingPayload, type HoldingSettings } from '@/api/holdings'
import { analysisApi, type SingleAnalysisRequest } from '@/api/analysis'
import { stocksApi } from '@/api/stocks'
import { buildPricePlanRows, type PricePlanSource } from './pricePlan'
import { normalizeMarketForAnalysis } from '@/utils/market'

const router = useRouter()
const holdings = ref<HoldingItem[]>([])
const holdingSettings = ref<HoldingSettings | null>(null)
const loading = ref(false)
const saving = ref(false)
const settingsSaving = ref(false)
const planSaving = ref(false)
const reportSubmitting = ref(false)
const nameLoading = ref(false)
const formDialogVisible = ref(false)
const settingsDialogVisible = ref(false)
const planDialogVisible = ref(false)
const nameTip = ref('输入股票代码后自动识别股票名称')
const editingId = ref('')
const planEditingId = ref('')
const planEditingLabel = ref('')
const planSourceRow = ref<HoldingItem | null>(null)
const detailDrawerVisible = ref(false)
const selectedHolding = ref<HoldingItem | null>(null)
const lastLookedUpCode = ref('')
let codeLookupTimer: number | undefined

interface PricePlanForm {
  manual_stop_loss_price: number | null
  manual_target_price: number | null
  manual_sell_price: number | null
  manual_buy_price: number | null
  price_plan_notes: string
}

type PricePlanNumberField = Exclude<keyof PricePlanForm, 'price_plan_notes'>
type HoldingTableRow = Partial<HoldingItem> & Record<string, any>

const form = reactive<HoldingPayload>({
  code: '',
  name: '',
  quantity: 0,
  cost_price: 0,
  target_monthly_return_pct: 10,
  stop_loss_pct: 8,
  strategy: 'swing',
  notes: ''
})

const planForm = reactive<PricePlanForm>({
  manual_stop_loss_price: null,
  manual_target_price: null,
  manual_sell_price: null,
  manual_buy_price: null,
  price_plan_notes: ''
})

const settingsForm = reactive({
  total_assets: 0
})

const totalHoldingCost = computed(() =>
  holdings.value.reduce((sum, item) => sum + Number(item.cost_price || 0) * Number(item.quantity || 0), 0)
)

const totalProfitLoss = computed(() =>
  holdings.value.reduce((sum, item) => sum + Number(item.analysis?.profit_loss || 0), 0)
)

const totalAssetsIsAuto = computed(() => holdingSettings.value?.is_auto_total_assets !== false)

const effectiveTotalAssets = computed(() => {
  const configured = holdingSettings.value?.configured_total_assets
  if (configured !== null && configured !== undefined) return Number(configured)
  return Number(holdingSettings.value?.total_assets || totalHoldingCost.value)
})

const cashBalance = computed(() => effectiveTotalAssets.value - totalHoldingCost.value)

const stockPositionPct = computed(() => {
  if (!effectiveTotalAssets.value) return 0
  return totalHoldingCost.value / effectiveTotalAssets.value * 100
})

const totalProfitLossPctOfAssets = computed(() => {
  if (!effectiveTotalAssets.value) return 0
  return totalProfitLoss.value / effectiveTotalAssets.value * 100
})

const monthlyTargetProfit = computed(() =>
  holdings.value.reduce((sum, item) => {
    const costValue = Number(item.cost_price || 0) * Number(item.quantity || 0)
    const targetPct = Number(item.target_monthly_return_pct || 0)
    return sum + costValue * targetPct / 100
  }, 0)
)

const weightedMonthlyTargetPct = computed(() => {
  const totalCostValue = holdings.value.reduce(
    (sum, item) => sum + Number(item.cost_price || 0) * Number(item.quantity || 0),
    0
  )
  if (!totalCostValue) return 0
  return monthlyTargetProfit.value / totalCostValue * 100
})

const monthlyTargetProgressPct = computed(() => {
  if (!monthlyTargetProfit.value) return 0
  return totalProfitLoss.value / monthlyTargetProfit.value * 100
})

const summaryProgressStatus = computed(() => {
  if (monthlyTargetProgressPct.value >= 100) return 'success'
  if (monthlyTargetProgressPct.value < 0) return 'exception'
  return undefined
})

const previewStockPositionPct = computed(() => {
  if (!settingsForm.total_assets) return 0
  return totalHoldingCost.value / Number(settingsForm.total_assets) * 100
})

const detailTitle = computed(() => {
  if (!selectedHolding.value) return '持仓详情'
  return `${selectedHolding.value.code} ${selectedHolding.value.name || ''}`.trim()
})

const openCreateDialog = () => {
  resetForm()
  formDialogVisible.value = true
}

const openSettingsDialog = () => {
  settingsForm.total_assets = Number(effectiveTotalAssets.value.toFixed(2))
  settingsDialogVisible.value = true
}

const resetForm = () => {
  editingId.value = ''
  lastLookedUpCode.value = ''
  nameTip.value = '输入股票代码后自动识别股票名称'
  Object.assign(form, {
    code: '',
    name: '',
    quantity: 0,
    cost_price: 0,
    target_monthly_return_pct: 10,
    stop_loss_pct: 8,
    strategy: 'swing',
    notes: ''
  })
}

const resetPlanForm = () => {
  planEditingId.value = ''
  planEditingLabel.value = ''
  planSourceRow.value = null
  Object.assign(planForm, {
    manual_stop_loss_price: null,
    manual_target_price: null,
    manual_sell_price: null,
    manual_buy_price: null,
    price_plan_notes: ''
  })
}

const loadHoldings = async () => {
  loading.value = true
  try {
    const res = await holdingsApi.list()
    holdings.value = res.data.items || []
    holdingSettings.value = res.data.settings || null
    if (selectedHolding.value) {
      selectedHolding.value = holdings.value.find(item => item.id === selectedHolding.value?.id) || null
      if (!selectedHolding.value) detailDrawerVisible.value = false
    }
  } finally {
    loading.value = false
  }
}

const formatLocalDate = (date = new Date()) => {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const activePriceFrom = (...values: Array<number | null | undefined>) => {
  for (const value of values) {
    if (value !== null && value !== undefined && Number(value) > 0) {
      return Number(value)
    }
  }
  return undefined
}

const buildHoldingReportRequest = (holding: HoldingItem, analysisDate: string): SingleAnalysisRequest => {
  const stopLossPrice = activePriceFrom(
    holding.manual_stop_loss_price,
    holding.ai_advice?.stop_loss_price
  )
  const takeProfitPrice = activePriceFrom(
    holding.manual_target_price,
    holding.ai_advice?.target_price,
    holding.manual_sell_price,
    holding.ai_advice?.suggested_sell_price
  )
  const planText = [
    `策略：${strategyText(holding.strategy)}`,
    holding.price_plan_notes ? `价格计划：${holding.price_plan_notes}` : '',
    holding.notes ? `持仓备注：${holding.notes}` : ''
  ].filter(Boolean).join('\n')

  return {
    symbol: holding.code,
    stock_code: holding.code,
    parameters: {
      market_type: normalizeMarketForAnalysis(holding.market),
      analysis_date: analysisDate,
      research_depth: '全面',
      selected_analysts: ['market', 'fundamentals', 'news', 'social'],
      include_sentiment: true,
      include_risk: true,
      language: 'zh-CN',
      holding: {
        cost_price: Number(holding.cost_price),
        shares: Number(holding.quantity),
        take_profit_price: takeProfitPrice,
        stop_loss_price: stopLossPrice,
        plan: planText || strategyText(holding.strategy)
      }
    }
  }
}

const generateTodayReports = async () => {
  const reportTargets = holdings.value.filter(item => item.code)
  if (reportTargets.length === 0) {
    ElMessage.warning('请先新增持仓')
    return
  }
  if (reportTargets.length > 10) {
    ElMessage.warning('一次最多生成 10 只持仓的报告，请先精简持仓列表')
    return
  }

  const analysisDate = formatLocalDate()
  try {
    await ElMessageBox.confirm(
      `将为 ${reportTargets.length} 只持仓生成 ${analysisDate} 的全面分析报告，并刷新当前价格。报告完成后，持仓页会从最新报告提取关键价格区间。`,
      '生成今日报告',
      {
        confirmButtonText: '开始生成',
        cancelButtonText: '取消',
        type: 'info'
      }
    )
  } catch {
    return
  }

  reportSubmitting.value = true
  try {
    const results: Array<{ holding: HoldingItem; taskId?: string; error?: string }> = []
    for (const holding of reportTargets) {
      try {
        const response = await analysisApi.startSingleAnalysis(buildHoldingReportRequest(holding, analysisDate))
        const taskId = response?.data?.task_id
        if (!taskId) {
          throw new Error('任务ID为空')
        }
        results.push({ holding, taskId })
      } catch (error: any) {
        results.push({ holding, error: error?.message || '提交失败' })
      }
    }

    await loadHoldings()

    const successCount = results.filter(item => item.taskId).length
    const failedItems = results.filter(item => item.error)
    if (successCount > 0) {
      ElMessage.success(`已提交 ${successCount} 个今日持仓报告任务`)
    }
    if (failedItems.length > 0) {
      ElMessage.warning(`${failedItems.length} 个持仓报告提交失败`)
    }

    if (successCount > 0) {
      ElMessageBox.confirm(
        `今日持仓报告任务已提交 ${successCount} 个。报告会在任务完成后进入分析报告列表。`,
        '提交成功',
        {
          confirmButtonText: '去任务中心',
          cancelButtonText: '留在当前页',
          type: failedItems.length > 0 ? 'warning' : 'success',
          distinguishCancelAndClose: true
        }
      ).then(() => {
        router.push({ path: '/tasks', query: { tab: 'running' } })
      }).catch(() => {})
    }
  } finally {
    reportSubmitting.value = false
  }
}

const saveHoldingSettings = async () => {
  if (settingsForm.total_assets < 0) {
    ElMessage.warning('总资产不能小于 0')
    return
  }
  if (totalHoldingCost.value > 0 && settingsForm.total_assets <= 0) {
    ElMessage.warning('已有股票持仓时，总资产必须大于 0')
    return
  }
  settingsSaving.value = true
  try {
    const res = await holdingsApi.updateSettings({
      total_assets: Number(settingsForm.total_assets || 0)
    })
    holdingSettings.value = res.data.settings
    settingsDialogVisible.value = false
    ElMessage.success('总资产已保存')
  } finally {
    settingsSaving.value = false
  }
}

const handleCodeInput = () => {
  const code = form.code.trim().toUpperCase()
  if (code !== lastLookedUpCode.value) {
    form.name = ''
    nameTip.value = '输入股票代码后自动识别股票名称'
  }
}

const scheduleStockNameLookup = () => {
  handleCodeInput()
  if (codeLookupTimer) {
    window.clearTimeout(codeLookupTimer)
  }
  if (editingId.value) return

  const code = form.code.trim().toUpperCase()
  if (!/^\d{6}$/.test(code)) return

  codeLookupTimer = window.setTimeout(() => {
    lookupStockName(false)
  }, 450)
}

const lookupStockName = async (showMessage = true) => {
  const code = form.code.trim().toUpperCase()
  if (!code || editingId.value) return
  if (lastLookedUpCode.value === code && form.name) return

  nameLoading.value = true
  nameTip.value = '正在查询股票名称...'
  try {
    const res = await stocksApi.getQuote(code)
    const data = res.data
    if (data?.name) {
      form.name = data.name
      lastLookedUpCode.value = code
      nameTip.value = `已识别：${data.name}`
      if (showMessage) ElMessage.success(`已自动填充股票名称：${data.name}`)
    } else {
      nameTip.value = '暂未查到名称，保存时后端会再尝试识别'
    }
  } catch (error) {
    nameTip.value = '暂未查到名称，保存时后端会再尝试识别'
  } finally {
    nameLoading.value = false
  }
}

const validateForm = () => {
  if (!form.code.trim()) {
    ElMessage.warning('请输入股票代码')
    return false
  }
  if (!form.quantity || form.quantity <= 0) {
    ElMessage.warning('请输入持股数量')
    return false
  }
  if (!form.cost_price || form.cost_price <= 0) {
    ElMessage.warning('请输入成本价')
    return false
  }
  if (!form.target_monthly_return_pct || form.target_monthly_return_pct <= 0) {
    ElMessage.warning('请输入月收益目标')
    return false
  }
  return true
}

const saveHolding = async () => {
  if (!validateForm()) return
  saving.value = true
  try {
    if (!form.name) {
      await lookupStockName(false)
    }
    const payload = { ...form, code: form.code.trim().toUpperCase() }
    if (editingId.value) {
      await holdingsApi.update(editingId.value, payload)
      ElMessage.success('持仓已更新')
    } else {
      await holdingsApi.create(payload)
      ElMessage.success('持仓已添加')
    }
    formDialogVisible.value = false
    await loadHoldings()
  } finally {
    saving.value = false
  }
}

const openHoldingDetail = (row: HoldingTableRow) => {
  selectedHolding.value = row as HoldingItem
  detailDrawerVisible.value = true
}

const editHolding = (row: HoldingTableRow) => {
  const holding = row as HoldingItem
  editingId.value = holding.id
  lastLookedUpCode.value = holding.code
  nameTip.value = holding.name ? `已识别：${holding.name}` : '输入股票代码后自动识别股票名称'
  Object.assign(form, {
    code: holding.code,
    name: holding.name,
    quantity: holding.quantity,
    cost_price: holding.cost_price,
    target_monthly_return_pct: holding.target_monthly_return_pct,
    stop_loss_pct: holding.stop_loss_pct,
    take_profit_pct: holding.take_profit_pct || null,
    strategy: holding.strategy || 'swing',
    notes: holding.notes || ''
  })
  formDialogVisible.value = true
}

const openPlanDialog = (row: HoldingTableRow) => {
  const holding = row as HoldingItem
  planEditingId.value = holding.id
  planEditingLabel.value = `${holding.code} ${holding.name || ''}`.trim()
  planSourceRow.value = holding
  Object.assign(planForm, {
    manual_stop_loss_price: holding.manual_stop_loss_price ?? null,
    manual_target_price: holding.manual_target_price ?? null,
    manual_sell_price: holding.manual_sell_price ?? null,
    manual_buy_price: holding.manual_buy_price ?? null,
    price_plan_notes: holding.price_plan_notes || ''
  })
  planDialogVisible.value = true
}

const normalizePlanPrice = (value: number | null | undefined) => {
  if (value === null || value === undefined || Number(value) <= 0) return null
  return Number(value)
}

const applyReportPlan = () => {
  const advice = planSourceRow.value?.ai_advice
  if (!advice) {
    ElMessage.warning('当前没有报告价格可采用')
    return
  }

  let copied = false
  const copyPrice = (key: PricePlanNumberField, value: number | null | undefined) => {
    if (value === null || value === undefined) return
    planForm[key] = Number(value)
    copied = true
  }

  copyPrice('manual_stop_loss_price', advice.stop_loss_price)
  copyPrice('manual_target_price', advice.target_price)
  copyPrice('manual_sell_price', advice.suggested_sell_price)
  copyPrice('manual_buy_price', advice.suggested_buy_price)

  if (copied) {
    ElMessage.success('已采用报告价格')
  } else {
    ElMessage.warning('报告中没有可采用的价格')
  }
}

const openReportShortcut = (row: HoldingTableRow) => {
  const holding = row as HoldingItem
  router.push({
    name: 'ReportsHome',
    query: {
      search_keyword: holding.code
    }
  })
}

const savePricePlan = async () => {
  if (!planEditingId.value) return
  planSaving.value = true
  try {
    const res = await holdingsApi.update(planEditingId.value, {
      manual_stop_loss_price: normalizePlanPrice(planForm.manual_stop_loss_price),
      manual_target_price: normalizePlanPrice(planForm.manual_target_price),
      manual_sell_price: normalizePlanPrice(planForm.manual_sell_price),
      manual_buy_price: normalizePlanPrice(planForm.manual_buy_price),
      price_plan_notes: planForm.price_plan_notes.trim()
    })
    const index = holdings.value.findIndex(item => item.id === planEditingId.value)
    if (index >= 0) {
      holdings.value[index] = res.data.item
    }
    if (selectedHolding.value?.id === planEditingId.value) {
      selectedHolding.value = res.data.item
    }
    planDialogVisible.value = false
    ElMessage.success('价格计划已保存')
  } finally {
    planSaving.value = false
  }
}

const deleteHolding = async (row: HoldingTableRow) => {
  const holding = row as HoldingItem
  await ElMessageBox.confirm(`确认删除 ${holding.name || holding.code} 的持仓？`, '删除持仓', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
  await holdingsApi.remove(holding.id)
  ElMessage.success('持仓已删除')
  await loadHoldings()
}

const formatMoney = (value: number | null | undefined, signed = false) => {
  if (value === null || value === undefined) return '待获取'
  const n = Number(value)
  return `${signed && n > 0 ? '+' : ''}${n.toFixed(2)}`
}

const formatPrice = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '待获取'
  return Number(value).toFixed(3)
}

const formatAdvicePrice = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '-'
  return Number(value).toFixed(2)
}

const formatShares = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '-'
  return Number(value).toLocaleString('zh-CN')
}

const formatConfidence = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '置信度 -'
  return `置信度 ${Math.round(Number(value) * 100)}%`
}

const formatShortTime = (value: string | null | undefined) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getMonth() + 1}-${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

const formatPct = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '待获取'
  return `${Number(value).toFixed(2)}%`
}

const formatSignedPct = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '距现价 -'
  const n = Number(value)
  return `距现价 ${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

const effectiveCurrentPrice = (row: HoldingTableRow) =>
  row.current_price ?? row.analysis?.current_price ?? null

const holdingMarketValue = (row: HoldingTableRow) => {
  const analysisValue = row.analysis?.market_value
  if (analysisValue !== null && analysisValue !== undefined) return Number(analysisValue)
  const currentPrice = effectiveCurrentPrice(row)
  if (currentPrice === null || currentPrice === undefined) return null
  return Number(currentPrice) * Number(row.quantity || 0)
}

const holdingPositionPct = (row: HoldingTableRow) => {
  const marketValue = holdingMarketValue(row)
  if (marketValue === null || !effectiveTotalAssets.value) return null
  return marketValue / effectiveTotalAssets.value * 100
}

const strategyText = (strategy?: string) => {
  if (strategy === 'short') return '短线'
  if (strategy === 'long') return '长线'
  return '波段'
}

const planSourceText = (source: PricePlanSource) => {
  if (source === 'manual') return '手动'
  if (source === 'report') return '报告'
  return '未设'
}

const pricePlanRows = (row: HoldingTableRow) => buildPricePlanRows({
  currentPrice: effectiveCurrentPrice(row),
  manualStopLossPrice: row.manual_stop_loss_price,
  manualTargetPrice: row.manual_target_price,
  manualSellPrice: row.manual_sell_price,
  manualBuyPrice: row.manual_buy_price,
  reportStopLossPrice: row.ai_advice?.stop_loss_price,
  reportTargetPrice: row.ai_advice?.target_price,
  reportSellPrice: row.ai_advice?.suggested_sell_price,
  reportBuyPrice: row.ai_advice?.suggested_buy_price
})

const profitClass = (value: number | null | undefined) => {
  if (value === null || value === undefined) return ''
  return Number(value) >= 0 ? 'profit-up' : 'profit-down'
}

const progressPercent = (value: number | null | undefined) => {
  if (value === null || value === undefined) return 0
  return Math.max(0, Math.min(100, Number(value)))
}

const actionText = (action?: string) => {
  if (action === 'sell') return '卖出/减仓'
  if (action === 'buy') return '买入/补仓'
  return '持有观察'
}

const actionTag = (action?: string): 'success' | 'warning' | 'danger' | 'info' => {
  if (action === 'sell') return 'danger'
  if (action === 'buy') return 'success'
  if (action === 'hold') return 'warning'
  return 'info'
}

onMounted(() => {
  loadHoldings()
})

watch(() => form.code, scheduleStockNameLookup)

onBeforeUnmount(() => {
  if (codeLookupTimer) {
    window.clearTimeout(codeLookupTimer)
  }
})
</script>

<style scoped lang="scss">
.holdings-page {
  padding: 0;
  min-height: 100vh;
  background: var(--el-bg-color-page);
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding: 24px;
  margin-bottom: 20px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  background: var(--el-bg-color);

  h1 {
    margin: 0 0 8px;
    font-size: 28px;
    color: #111827;
  }

  p {
    margin: 0;
    color: #64748b;
    line-height: 1.6;
  }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 20px;
  margin-bottom: 20px;
}

.summary-tile {
  min-height: 92px;
  padding: 18px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  background: var(--el-bg-color);
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;

  span {
    color: #64748b;
    font-size: 13px;
  }

  strong {
    color: #111827;
    font-size: 24px;
  }
}

.summary-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.summary-subline {
  color: #64748b;
  font-size: 12px;
}

.target-tile {
  border-color: #bfdbfe;
  background: linear-gradient(180deg, #ffffff 0%, #eff6ff 100%);
}

.main-grid {
  align-items: flex-start;
}

.entry-card,
.table-card {
  border-radius: 8px;
}

.table-card {
  overflow: hidden;
  border: 1px solid #dbe4f0;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.05);

  :deep(.el-card__header) {
    padding: 16px 22px;
    border-bottom: 1px solid #e2e8f0;
    background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
  }

  :deep(.el-card__body) {
    padding: 0 22px 18px;
  }
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  min-height: 34px;

  > span {
    color: #0f172a;
    font-size: 16px;
    font-weight: 700;
  }
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 12px;

  :deep(.el-tag) {
    height: 28px;
    padding: 0 12px;
    border-radius: 999px;
    font-weight: 600;
  }
}

.holding-form {
  :deep(.el-input-number),
  :deep(.el-select) {
    width: 100%;
  }
}

.unit {
  margin-left: 8px;
  color: #64748b;
}

.field-tip {
  margin-top: 6px;
  color: #64748b;
  font-size: 12px;
  line-height: 1.4;
}

.submit-button {
  width: 100%;
  height: 42px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.settings-preview {
  margin-left: 96px;
  padding: 10px 12px;
  border-radius: 6px;
  background: #f8fafc;
  color: #64748b;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.stock-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;

  strong {
    color: #334155;
    font-size: 15px;
    letter-spacing: 0;
  }

  span {
    color: #64748b;
    font-size: 12px;
  }

  .el-tag {
    align-self: flex-start;
    margin-top: 4px;
    border-radius: 999px;
    background: #f8fafc;
  }
}

.holdings-table {
  --el-table-border-color: #e5e7eb;

  :deep(.el-table__row) {
    cursor: pointer;
    transition: background-color 0.15s ease;
  }

  :deep(.el-table__body .el-table__row:hover > td.el-table__cell) {
    background: #f8fbff;
  }

  :deep(.el-table__cell) {
    padding: 14px 0;
    vertical-align: top;
  }

  :deep(.cell) {
    padding: 0 14px;
  }

  :deep(.el-table__header-wrapper th.el-table__cell) {
    background: #f8fafc;
    color: #64748b;
    font-size: 12px;
    font-weight: 700;
  }

  :deep(.el-table__body-wrapper tr:last-child td.el-table__cell) {
    border-bottom: 0;
  }
}

.table-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;

  :deep(.el-button + .el-button) {
    margin-left: 0;
  }
}

.position-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #0f172a;
  font-size: 13px;
}

.position-main {
  display: flex;
  align-items: baseline;
  gap: 8px;

  strong {
    font-size: 16px;
    color: #0f172a;
  }

  span {
    padding: 2px 8px;
    border-radius: 999px;
    background: #eff6ff;
    color: #2563eb;
    font-size: 12px;
    font-weight: 600;
  }
}

.position-subline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;

  span {
    padding: 2px 7px;
    border-radius: 999px;
    background: #f8fafc;
    border: 1px solid #edf2f7;
  }
}

.summary-progress {
  margin: 10px 0 6px;
}

.pnl-percent {
  margin-top: 4px;
  color: #64748b;
  font-size: 13px;
}

.muted {
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
}

.suggestion {
  margin-top: 6px;
  font-size: 13px;
  color: #111827;
  font-weight: 600;
}

.ai-advice-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.advice-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.confidence {
  color: #475569;
  font-size: 12px;
}

.price-plan-cell {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
}

.price-plan-row {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  min-height: 26px;
}

.plan-label {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  background: #f1f5f9;
  color: #475569;
}

.plan-label--danger {
  background: #fef2f2;
  color: #b91c1c;
}

.plan-label--success {
  background: #ecfdf5;
  color: #047857;
}

.plan-label--warning {
  background: #fff7ed;
  color: #c2410c;
}

.plan-label--info {
  background: #eff6ff;
  color: #1d4ed8;
}

.plan-primary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  min-width: 0;

  strong {
    min-width: 46px;
    color: #334155;
    font-size: 13px;
  }
}

.plan-source,
.plan-distance {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
}

.plan-source {
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #475569;
}

.plan-source--manual {
  border-color: #c7d2fe;
  background: #eef2ff;
  color: #4338ca;
}

.plan-source--report {
  border-color: #a5f3fc;
  background: #ecfeff;
  color: #0e7490;
}

.plan-source--none {
  background: #f8fafc;
  color: #94a3b8;
}

.plan-distance {
  background: #ffffff;
  border: 1px solid #e5e7eb;
  color: #111827;
}

.plan-note {
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid #fde68a;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
  line-height: 1.5;
}

.risk-note {
  margin: 14px 0 0;
  border: 0;
  border-radius: 8px;
  background: #fffbeb;

  :deep(.el-alert__content) {
    padding: 0;
  }

  :deep(.el-alert__title) {
    color: #b45309;
    font-size: 13px;
  }
}

.holding-detail {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid #e5e7eb;
}

.detail-code {
  font-size: 22px;
  font-weight: 700;
  color: #111827;
}

.detail-name {
  margin-top: 4px;
  color: #64748b;
}

.detail-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-title {
  font-weight: 700;
  color: #111827;
}

.section-title--with-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.metric-item {
  min-height: 70px;
  padding: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f8fafc;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 6px;

  span {
    color: #64748b;
    font-size: 12px;
  }

  strong {
    color: #111827;
    font-size: 17px;
  }
}

.detail-note {
  padding: 10px 12px;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 13px;
  line-height: 1.6;
}

.detail-plan-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.detail-plan-item {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 6px 10px;
  align-items: center;
  padding: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
}

.detail-plan-main {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;

  strong {
    font-size: 16px;
  }
}

.detail-plan-compare {
  grid-column: 2;
  color: #64748b;
  font-size: 12px;
}

.report-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.report-suggestion {
  padding: 10px 12px;
  border-radius: 8px;
  background: #eff6ff;
  color: #1e3a8a;
  line-height: 1.6;
}

.risk-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.detail-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding-top: 14px;
  border-top: 1px solid #e5e7eb;
}

.ai-plan-panel {
  margin: 0 0 18px 108px;
  padding: 12px;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  background: #eff6ff;
}

.ai-plan-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  color: #1e3a8a;
  font-weight: 600;
}

.ai-plan-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 10px;
  color: #1f2937;
  font-size: 12px;
}

.reason {
  color: #374151;
  line-height: 1.6;
}

.profit-up {
  color: #dc2626;
}

.profit-down {
  color: #16a34a;
}

.risk-note {
  margin-top: 16px;
}

@media (max-width: 900px) {
  .page-header {
    flex-direction: column;
  }

  .header-actions {
    width: 100%;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .settings-preview {
    margin-left: 0;
  }
}
</style>
