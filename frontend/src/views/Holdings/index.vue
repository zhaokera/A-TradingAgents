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
        <el-button type="primary" :loading="loading" @click="loadHoldings">
          <el-icon><Refresh /></el-icon>
          刷新分析
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

          <el-table :data="holdings" v-loading="loading" row-key="id" empty-text="还没有持仓">
            <el-table-column prop="code" label="股票" min-width="130">
              <template #default="{ row }">
                <div class="stock-cell">
                  <strong>{{ row.code }}</strong>
                  <span>{{ row.name || row.code }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="成本/现价" min-width="130">
              <template #default="{ row }">
                <div>{{ formatPrice(row.cost_price) }}</div>
                <div class="muted">{{ formatPrice(row.current_price) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="数量" prop="quantity" width="90" />
            <el-table-column label="盈亏" min-width="130">
              <template #default="{ row }">
                <strong :class="profitClass(row.analysis?.profit_loss)">
                  {{ formatMoney(row.analysis?.profit_loss, true) }}
                </strong>
                <div class="muted">{{ formatPct(row.analysis?.profit_loss_pct) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="持仓占比" min-width="150">
              <template #default="{ row }">
                <strong>{{ formatMoney(rowCostValue(row)) }}</strong>
                <div class="muted">占总资产 {{ formatPct(rowAssetPct(row)) }}</div>
                <div class="muted">占股票持仓 {{ formatPct(rowHoldingPct(row)) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="目标进度" min-width="170">
              <template #default="{ row }">
                <el-progress
                  :percentage="progressPercent(row.analysis?.monthly_target_progress_pct)"
                  :status="progressStatus(row.analysis)"
                  :stroke-width="10"
                />
                <div class="target-meta">
                  月目标 {{ row.target_monthly_return_pct }}%，日均还需 {{ row.analysis?.required_daily_return_pct ?? '-' }}%
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
            <el-table-column label="原因" min-width="220">
              <template #default="{ row }">
                <span class="reason">{{ row.analysis?.reason }}</span>
              </template>
            </el-table-column>
            <el-table-column label="AI模型建议" min-width="260">
              <template #default="{ row }">
                <div v-if="row.ai_advice" class="ai-advice-cell">
                  <div class="advice-line">
                    <el-tag :type="actionTag(row.ai_advice.action)">
                      {{ actionText(row.ai_advice.action) }}
                    </el-tag>
                    <span class="muted">{{ row.ai_advice.model_name }}</span>
                  </div>
                  <div class="price-grid">
                    <span>买 {{ formatAdvicePrice(row.ai_advice.suggested_buy_price) }}</span>
                    <span>卖 {{ formatAdvicePrice(row.ai_advice.suggested_sell_price) }}</span>
                    <span>目标 {{ formatAdvicePrice(row.ai_advice.target_price) }}</span>
                    <span>止损 {{ formatAdvicePrice(row.ai_advice.stop_loss_price) }}</span>
                  </div>
                  <div class="suggestion">{{ row.ai_advice.position_suggestion }}</div>
                  <div class="reason">{{ row.ai_advice.reason }}</div>
                </div>
                <span v-else class="muted">点击“AI建议”后由已配置模型生成</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="210" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="success"
                  :loading="aiLoadingId === row.id"
                  @click="generateAiAdvice(row)"
                >
                  AI建议
                </el-button>
                <el-button link type="primary" @click="editHolding(row)">编辑</el-button>
                <el-button link type="danger" @click="deleteHolding(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <el-alert type="warning" :closable="false" class="risk-note">
            持仓分析仅用于仓位管理参考，不构成投资建议或交易指令。
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

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
import { ElMessage, ElMessageBox } from 'element-plus'
import { Edit, Plus, Refresh } from '@element-plus/icons-vue'
import { holdingsApi, type HoldingItem, type HoldingPayload, type HoldingAnalysis, type HoldingSettings } from '@/api/holdings'
import { stocksApi } from '@/api/stocks'

const holdings = ref<HoldingItem[]>([])
const holdingSettings = ref<HoldingSettings | null>(null)
const loading = ref(false)
const saving = ref(false)
const settingsSaving = ref(false)
const nameLoading = ref(false)
const aiLoadingId = ref('')
const formDialogVisible = ref(false)
const settingsDialogVisible = ref(false)
const nameTip = ref('输入股票代码后自动识别股票名称')
const editingId = ref('')
const lastLookedUpCode = ref('')
let codeLookupTimer: number | undefined

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

const settingsForm = reactive({
  total_assets: 0
})

const totalMarketValue = computed(() =>
  holdings.value.reduce((sum, item) => sum + Number(item.analysis?.market_value || 0), 0)
)

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

const previewStockPositionPct = computed(() => {
  if (!settingsForm.total_assets) return 0
  return totalHoldingCost.value / Number(settingsForm.total_assets) * 100
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

const loadHoldings = async () => {
  loading.value = true
  try {
    const res = await holdingsApi.list()
    holdings.value = res.data.items || []
    holdingSettings.value = res.data.settings || null
  } finally {
    loading.value = false
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

const editHolding = (row: HoldingItem) => {
  editingId.value = row.id
  lastLookedUpCode.value = row.code
  nameTip.value = row.name ? `已识别：${row.name}` : '输入股票代码后自动识别股票名称'
  Object.assign(form, {
    code: row.code,
    name: row.name,
    quantity: row.quantity,
    cost_price: row.cost_price,
    target_monthly_return_pct: row.target_monthly_return_pct,
    stop_loss_pct: row.stop_loss_pct,
    take_profit_pct: row.take_profit_pct || null,
    strategy: row.strategy || 'swing',
    notes: row.notes || ''
  })
  formDialogVisible.value = true
}

const deleteHolding = async (row: HoldingItem) => {
  await ElMessageBox.confirm(`确认删除 ${row.name || row.code} 的持仓？`, '删除持仓', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
  await holdingsApi.remove(row.id)
  ElMessage.success('持仓已删除')
  await loadHoldings()
}

const generateAiAdvice = async (row: HoldingItem) => {
  aiLoadingId.value = row.id
  try {
    const res = await holdingsApi.aiAdvice(row.id)
    const index = holdings.value.findIndex(item => item.id === row.id)
    if (index >= 0) {
      holdings.value[index] = res.data.item
    }
    ElMessage.success(`已使用 ${res.data.advice.model_name} 生成AI建议`)
  } finally {
    aiLoadingId.value = ''
  }
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

const formatPct = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '待获取'
  return `${Number(value).toFixed(2)}%`
}

const rowCostValue = (row: HoldingItem) => Number(row.cost_price || 0) * Number(row.quantity || 0)

const rowAssetPct = (row: HoldingItem) => {
  if (!effectiveTotalAssets.value) return 0
  return rowCostValue(row) / effectiveTotalAssets.value * 100
}

const rowHoldingPct = (row: HoldingItem) => {
  if (!totalHoldingCost.value) return 0
  return rowCostValue(row) / totalHoldingCost.value * 100
}

const profitClass = (value: number | null | undefined) => {
  if (value === null || value === undefined) return ''
  return Number(value) >= 0 ? 'profit-up' : 'profit-down'
}

const progressPercent = (value: number | null | undefined) => {
  if (value === null || value === undefined) return 0
  return Math.max(0, Math.min(100, Number(value)))
}

const progressStatus = (analysis?: HoldingAnalysis) => {
  if (!analysis) return undefined
  if (analysis.status === '目标已达成') return 'success'
  if (analysis.status.includes('止损')) return 'exception'
  return undefined
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
  padding: 24px;
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

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  font-weight: 600;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 12px;
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
  gap: 2px;

  span {
    color: #64748b;
    font-size: 12px;
  }
}

.muted,
.target-meta {
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
}

.suggestion {
  margin-top: 6px;
  font-size: 13px;
  color: #111827;
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

.price-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 10px;
  color: #374151;
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
