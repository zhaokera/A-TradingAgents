<template>
  <div class="dashboard">
    <div class="page-header dashboard-header">
      <div>
        <h1 class="page-title">
          <el-icon><Odometer /></el-icon>
          仪表板
        </h1>
        <p class="page-description">关注最近任务、账户概览和自选股状态。</p>
      </div>
      <el-button @click="reloadDashboard" :loading="dashboardLoading">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <el-row :gutter="16" class="dashboard-metrics">
      <el-col :xs="24" :sm="12" :lg="8">
        <div class="metric-card">
          <div class="metric-label">分析任务</div>
          <div class="metric-value">{{ userStats.totalAnalyses }}</div>
          <div class="metric-meta">最近任务总数</div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <div class="metric-card">
          <div class="metric-label">已完成</div>
          <div class="metric-value">{{ userStats.successfulAnalyses }}</div>
          <div class="metric-meta">可查看报告</div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <div class="metric-card">
          <div class="metric-label">自选股</div>
          <div class="metric-value">{{ favoriteStocks.length }}</div>
          <div class="metric-meta">当前关注</div>
        </div>
      </el-col>
    </el-row>

    <!-- 主要功能区域 -->
    <el-row :gutter="24" class="main-content">
      <!-- 左侧：最近分析 -->
      <el-col :span="16">
        <el-card class="recent-analyses-card" header="最近分析">
          <el-table :data="recentAnalyses" style="width: 100%">
            <el-table-column prop="stock_code" label="股票代码" width="120" />
            <el-table-column prop="stock_name" label="股票名称" width="150" />
            <el-table-column prop="status" label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="getStatusType(row.status)">
                  {{ getStatusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="start_time" label="创建时间" width="180">
              <template #default="{ row }">
                {{ formatTime(row.start_time) }}
              </template>
            </el-table-column>
            <el-table-column label="操作">
              <template #default="{ row }">
                <el-button type="text" size="small" @click="viewAnalysis(row)">
                  查看
                </el-button>
                <el-button
                  v-if="row.status === 'completed'"
                  type="text"
                  size="small"
                  @click="downloadReport(row)"
                >
                  下载
                </el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="table-footer">
            <el-button type="text" @click="goToHistory">
              查看全部历史 <el-icon><ArrowRight /></el-icon>
            </el-button>
          </div>
        </el-card>

        <!-- 市场快讯 -->
        <el-card class="market-news-card" style="margin-top: 24px;">
          <template #header>
            <span>市场快讯</span>
          </template>
          <div v-if="marketNews.length > 0" class="news-list">
            <div
              v-for="news in marketNews"
              :key="news.id"
              class="news-item"
              @click="openNewsUrl(news.url)"
            >
              <div class="news-title">{{ news.title }}</div>
              <div class="news-time">{{ formatTime(news.time) }}</div>
            </div>
          </div>
          <div v-else class="empty-state">
            <el-icon class="empty-icon"><InfoFilled /></el-icon>
            <p>暂无市场快讯</p>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：自选股和快讯 -->
      <el-col :span="8">
        <!-- 我的自选股 -->
        <el-card class="favorites-card">
          <template #header>
            <div class="card-header">
              <span>我的自选股</span>
              <el-button type="text" size="small" @click="goToFavorites">
                查看全部 <el-icon><ArrowRight /></el-icon>
              </el-button>
            </div>
          </template>

          <div v-if="favoriteStocks.length === 0" class="empty-favorites">
            <el-empty description="暂无自选股" :image-size="60">
              <el-button type="primary" size="small" @click="goToFavorites">
                添加自选股
              </el-button>
            </el-empty>
          </div>

          <div v-else class="favorites-list">
            <div
              v-for="stock in favoriteStocks.slice(0, 5)"
              :key="stock.stock_code"
              class="favorite-item"
              @click="viewStockDetail(stock)"
            >
              <div class="stock-info">
                <div class="stock-code">{{ stock.stock_code }}</div>
                <div class="stock-name">{{ stock.stock_name }}</div>
              </div>
              <div class="stock-price">
                <div class="current-price">¥{{ stock.current_price }}</div>
                <div
                  class="change-percent"
                  :class="getPriceChangeClass(stock.change_percent)"
                >
                  {{ stock.change_percent > 0 ? '+' : '' }}{{ Number(stock.change_percent).toFixed(2) }}%
                </div>
              </div>
            </div>
          </div>

          <div v-if="favoriteStocks.length > 5" class="favorites-footer">
            <el-button type="text" size="small" @click="goToFavorites">
              查看全部 {{ favoriteStocks.length }} 只自选股
            </el-button>
          </div>
        </el-card>

        <!-- 多数据源同步 -->
        <MultiSourceSyncCard style="margin-top: 24px;" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ArrowRight, InfoFilled, Odometer, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { AnalysisTask, AnalysisStatus } from '@/types/analysis'
import MultiSourceSyncCard from '@/components/Dashboard/MultiSourceSyncCard.vue'
import { favoritesApi } from '@/api/favorites'
import { analysisApi } from '@/api/analysis'
import { newsApi } from '@/api/news'

const router = useRouter()
const authStore = useAuthStore()

// 响应式数据
const userStats = ref({
  totalAnalyses: 0,
  successfulAnalyses: 0,
  dailyQuota: 1000,
  dailyUsed: 0,
  concurrentLimit: 3
})

const recentAnalyses = ref<AnalysisTask[]>([])

// 自选股数据
const favoriteStocks = ref<any[]>([])

// 市场快讯数据
const marketNews = ref<any[]>([])

const dashboardLoading = ref(false)



const goToHistory = () => {
  router.push('/tasks?tab=completed')
}

const viewAnalysis = (analysis: any) => {
  const status = (analysis as any)?.status
  if (status === 'completed') {
    router.push({ name: 'ReportDetail', params: { id: analysis.task_id } })
  } else {
    // 未完成任务跳转到任务中心的“进行中”标签页
    router.push('/tasks?tab=running')
  }
}

const downloadReport = async (analysis: any) => {
  try {
    const reportId = analysis.task_id
    const res = await fetch(`/api/reports/${reportId}/download?format=markdown`, {
      headers: {
        'Authorization': `Bearer ${authStore.token}`
      }
    })
    if (!res.ok) {
      const msg = `下载失败：HTTP ${res.status}`
      console.error(msg)
      ElMessage.error('下载失败，报告可能尚未生成')
      return
    }
    const blob = await res.blob()
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const code = (analysis as any).stock_code || (analysis as any).stock_symbol || 'stock'
    const dateStr = (analysis as any).analysis_date || (analysis as any).start_time || ''
    // 🔥 统一文件名格式：{code}_分析报告_{date}.md
    a.download = `${code}_分析报告_${String(dateStr).slice(0,10)}.md`
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
    ElMessage.success('报告已开始下载')
  } catch (err) {
    console.error('下载报告出错:', err)
    ElMessage.error('下载失败，请稍后重试')
  }
}

const openNewsUrl = (url?: string) => {
  if (url) {
    window.open(url, '_blank')
  } else {
    ElMessage.info('该新闻暂无详情链接')
  }
}

const getStatusType = (status: string | AnalysisStatus): 'success' | 'info' | 'warning' | 'danger' => {
  const statusMap: Record<string, 'success' | 'info' | 'warning' | 'danger'> = {
    pending: 'info',
    processing: 'warning',
    running: 'warning',
    completed: 'success',
    failed: 'danger',
    cancelled: 'info'
  }
  return statusMap[status] || 'info'
}

const getStatusText = (status: string | AnalysisStatus) => {
  const statusMap: Record<string, string> = {
    pending: '等待中',
    processing: '处理中',
    running: '处理中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消'
  }
  return statusMap[status] || String(status)
}

import { formatDateTime } from '@/utils/datetime'

const formatTime = (time: string) => {
  return formatDateTime(time)
}

// 自选股相关方法
const goToFavorites = () => {
  router.push('/favorites')
}

const viewStockDetail = (stock: any) => {
  // 可以跳转到股票详情页或分析页
  router.push(`/analysis/single?stock_code=${stock.stock_code}`)
}

const getPriceChangeClass = (changePercent: number) => {
  if (changePercent > 0) return 'price-up'
  if (changePercent < 0) return 'price-down'
  return 'price-neutral'
}

const loadFavoriteStocks = async () => {
  try {
    const response = await favoritesApi.list()
    if (response.success && response.data) {
      favoriteStocks.value = response.data.map((item: any) => ({
        stock_code: item.stock_code,
        stock_name: item.stock_name,
        current_price: item.current_price || 0,
        change_percent: item.change_percent || 0
      }))
    }
  } catch (error) {
    console.error('加载自选股失败:', error)
  }
}

const loadRecentAnalyses = async () => {
  try {
    // 使用任务中心的用户任务接口，获取最近10条
    const res = await analysisApi.getTaskList({
      limit: 10,
      offset: 0,
      // 不限定状态，展示最近任务；如需仅展示已完成可设为 'completed'
      status: undefined
    })

    // 兼容不同返回结构（ApiResponse 或直接 data）
    const body: any = (res as any)?.data?.data || (res as any)?.data || res || {}
    const tasks = body.tasks || []

    recentAnalyses.value = tasks
    userStats.value.totalAnalyses = body.total ?? tasks.length
    userStats.value.successfulAnalyses = tasks.filter((item: any) => item.status === 'completed').length
  } catch (error) {
    console.error('加载最近分析失败:', error)
    recentAnalyses.value = []
  }
}

const loadMarketNews = async () => {
  try {
    // 先尝试获取最近 24 小时的新闻
    let response = await newsApi.getLatestNews(undefined, 10, 24)

    // 如果最近 24 小时没有新闻，则获取最新的 10 条（不限时间）
    if (response.success && response.data && response.data.news.length === 0) {
      console.log('最近 24 小时没有新闻，获取最新的 10 条新闻（不限时间）')
      response = await newsApi.getLatestNews(undefined, 10, 24 * 365) // 回溯 1 年
    }

    if (response.success && response.data) {
      marketNews.value = response.data.news.map((item: any) => ({
        id: item.id || item.title,
        title: item.title,
        time: item.publish_time,
        url: item.url,
        source: item.source
      }))
    }
  } catch (error) {
    console.error('加载市场快讯失败:', error)
    // 如果加载失败，显示提示信息
    marketNews.value = []
  }
}

const reloadDashboard = async () => {
  dashboardLoading.value = true
  try {
    await Promise.all([
      loadFavoriteStocks(),
      loadRecentAnalyses(),
      loadMarketNews()
    ])
  } finally {
    dashboardLoading.value = false
  }
}

// 生命周期
onMounted(async () => {
  await reloadDashboard()
})
</script>

<style lang="scss" scoped>
.dashboard {
  .dashboard-header {
    .el-button {
      flex-shrink: 0;
    }
  }

  .dashboard-metrics {
    margin-bottom: 16px;

    .el-col {
      margin-bottom: 12px;
    }

    .metric-card {
      .metric-label {
        color: var(--ta-text-secondary, var(--el-text-color-regular));
        font-size: 13px;
        font-weight: 600;
      }

      .metric-value {
        margin-top: 8px;
        font-variant-numeric: tabular-nums;
      }

      .metric-meta {
        margin-top: 6px;
        color: var(--ta-text-muted, var(--el-text-color-secondary));
        font-size: 12px;
      }
    }
  }

  .recent-analyses-card {
    .table-footer {
      text-align: center;
      margin-top: 16px;
    }
  }

  .system-status-card {
    .status-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 0;

      &:not(:last-child) {
        border-bottom: 1px solid var(--el-border-color-lighter);
      }

      .status-label {
        color: var(--el-text-color-regular);
      }

      .status-value {
        font-weight: 600;
        color: var(--el-text-color-primary);
      }
    }
  }

  .market-news-card {
    .news-list {
      .news-item {
        padding: 12px 0;
        cursor: pointer;
        border-bottom: 1px solid var(--el-border-color-lighter);

        &:last-child {
          border-bottom: none;
        }

        &:hover {
          background-color: var(--el-fill-color-lighter);
          margin: 0 -16px;
          padding: 12px 16px;
          border-radius: 4px;
        }

        .news-title {
          font-size: 14px;
          color: var(--el-text-color-primary);
          margin-bottom: 4px;
          line-height: 1.4;
        }

        .news-time {
          font-size: 12px;
          color: var(--el-text-color-placeholder);
        }
      }
    }

    .news-footer {
      text-align: center;
      margin-top: 16px;
    }
  }

  .tips-card {
    .tip-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 0;
      font-size: 14px;
      color: var(--el-text-color-regular);

      .tip-icon {
        color: var(--el-color-primary);
      }
    }
  }

  .favorites-card {
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .empty-favorites {
      text-align: center;
      padding: 20px 0;
    }

    .favorites-list {
      .favorite-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px 0;
        border-bottom: 1px solid var(--el-border-color-lighter);
        cursor: pointer;
        transition: background-color 0.3s ease;

        &:hover {
          background-color: var(--el-fill-color-lighter);
          margin: 0 -16px;
          padding: 12px 16px;
          border-radius: 6px;
        }

        &:last-child {
          border-bottom: none;
        }

        .stock-info {
          .stock-code {
            font-weight: 600;
            font-size: 14px;
            color: var(--el-text-color-primary);
          }

          .stock-name {
            font-size: 12px;
            color: var(--el-text-color-regular);
            margin-top: 2px;
          }
        }

        .stock-price {
          text-align: right;

          .current-price {
            font-weight: 600;
            font-size: 14px;
            color: var(--el-text-color-primary);
          }

          .change-percent {
            font-size: 12px;
            margin-top: 2px;

            &.price-up {
              color: #f56c6c;
            }

            &.price-down {
              color: #67c23a;
            }

            &.price-neutral {
              color: var(--el-text-color-regular);
            }
          }
        }
      }
    }

    .favorites-footer {
      text-align: center;
      padding-top: 12px;
      border-top: 1px solid var(--el-border-color-lighter);
      margin-top: 12px;
    }
  }
}

// 响应式设计
@media (max-width: 768px) {
  .dashboard {
    .main-content {
      .el-col {
        margin-bottom: 24px;
      }
    }
  }
}
</style>
