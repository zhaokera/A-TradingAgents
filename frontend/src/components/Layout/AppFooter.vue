<template>
  <div class="app-footer">
    <div class="footer-content">
      <div class="copyright">
        <span>© 2025 A-TradingAgents v1.0.1</span>
        <span class="rights">All rights reserved.</span>
      </div>
      <div class="disclaimer-text">
        Agent管理平台是一个 AI 多智能体股票研究工具。平台中的分析结果、评分与参考意见由 AI 基于历史和公开市场数据生成，仅供研究参考，不作为交易指令。
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// Footer组件逻辑
</script>

<style lang="scss" scoped>
.app-footer {
  .footer-content {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    gap: 6px;
    color: var(--el-text-color-regular);
    font-size: 14px;
    padding: 12px 16px;
    text-align: center;
  }

  .rights {
    margin-left: 4px;
  }

  .disclaimer-text {
    color: var(--el-text-color-secondary);
    font-size: 12px;
    line-height: 1.6;
    max-width: 1100px;
  }
}
</style>
