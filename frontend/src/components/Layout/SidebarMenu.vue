<template>
  <el-menu
    :default-active="activeMenu"
    :collapse="appStore.sidebarCollapsed"
    :unique-opened="true"
    router
    class="sidebar-menu"
  >
    <el-menu-item index="/dashboard">
      <el-icon><Odometer /></el-icon>
      <template #title>仪表板</template>
    </el-menu-item>

    <el-sub-menu index="/assets">
      <template #title>
        <el-icon><Wallet /></el-icon>
        <span>我的资产</span>
      </template>
      <el-menu-item index="/analysis/holding">持仓分析</el-menu-item>
      <el-menu-item index="/favorites">我的自选股</el-menu-item>
    </el-sub-menu>

    <el-sub-menu index="/analysis">
      <template #title>
        <el-icon><TrendCharts /></el-icon>
        <span>股票分析</span>
      </template>
      <el-menu-item index="/analysis/single">单股分析</el-menu-item>
      <el-menu-item index="/analysis/batch">批量分析</el-menu-item>
      <!-- 新增：将分析报告作为股票分析的子菜单 -->
      <el-menu-item index="/reports">分析报告</el-menu-item>
    </el-sub-menu>

    <el-menu-item index="/tasks">
      <el-icon><List /></el-icon>
      <template #title>任务中心</template>
    </el-menu-item>

    <el-menu-item index="/screening">
      <el-icon><Search /></el-icon>
      <template #title>股票筛选</template>
    </el-menu-item>

    <!-- 分析报告已移至“股票分析”子菜单，保留注释便于追踪 -->
    <!--
    <el-menu-item index="/reports">
      <el-icon><Document /></el-icon>
      <template #title>分析报告</template>
    </el-menu-item>
    -->

    <!-- 个人设置 -->
    <el-sub-menu index="/settings-personal">
      <template #title>
        <el-icon><User /></el-icon>
        <span>个人设置</span>
      </template>
      <el-menu-item index="/settings">通用设置</el-menu-item>
      <el-menu-item index="/settings?tab=appearance">外观设置</el-menu-item>
      <el-menu-item index="/settings?tab=analysis">分析偏好</el-menu-item>
      <el-menu-item index="/settings?tab=notifications">通知设置</el-menu-item>
      <el-menu-item index="/settings?tab=security">安全设置</el-menu-item>
    </el-sub-menu>

    <!-- 系统配置 -->
    <el-sub-menu index="/settings-config">
      <template #title>
        <el-icon><Tools /></el-icon>
        <span>系统配置</span>
      </template>
      <el-menu-item index="/settings/config">配置管理</el-menu-item>
      <el-menu-item index="/settings/cache">缓存管理</el-menu-item>
    </el-sub-menu>

    <!-- 系统管理 -->
    <el-sub-menu index="/settings-admin">
      <template #title>
        <el-icon><Management /></el-icon>
        <span>系统管理</span>
      </template>
      <el-menu-item index="/settings/database">数据库管理</el-menu-item>
      <el-menu-item index="/settings/logs">操作日志</el-menu-item>
      <el-menu-item index="/settings/system-logs">系统日志</el-menu-item>
      <el-menu-item index="/settings/sync">多数据源同步</el-menu-item>
      <el-menu-item index="/settings/scheduler">定时任务</el-menu-item>
      <el-menu-item index="/settings/usage">使用统计</el-menu-item>
    </el-sub-menu>

    <el-menu-item index="/about">
      <el-icon><InfoFilled /></el-icon>
      <template #title>关于</template>
    </el-menu-item>
  </el-menu>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'
import {
  Odometer,
  Wallet,
  TrendCharts,
  Search,
  List,
  /* Document 移除：不再使用顶级分析报告菜单图标 */
  User,
  Tools,
  Management,
  InfoFilled
} from '@element-plus/icons-vue'

const route = useRoute()
const appStore = useAppStore()

const activeMenu = computed(() => route.path)
</script>

<style lang="scss" scoped>
.sidebar-menu {
  border: none;
  height: 100%;

  :deep(.el-menu-item),
  :deep(.el-sub-menu__title) {
    height: 48px;
    line-height: 48px;
  }

  :deep(.el-menu-item.is-active) {
    background-color: var(--el-color-primary-light-9);
    color: var(--el-color-primary);
  }
}
</style>
