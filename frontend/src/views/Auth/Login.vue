<template>
  <div class="login-page">
    <div class="login-container">
      <div class="login-header">
        <img src="/logo.svg" alt="TradingAgents-CN" class="logo" />
        <h1 class="title">TradingAgents-CN</h1>
        <p class="subtitle">多智能体股票分析学习平台</p>
      </div>

      <el-card class="login-card" shadow="always">
        <el-form
          :model="loginForm"
          :rules="loginRules"
          ref="loginFormRef"
          label-position="top"
          size="large"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="loginForm.username"
              placeholder="请输入用户名"
              prefix-icon="User"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="loginForm.password"
              type="password"
              placeholder="请输入密码"
              prefix-icon="Lock"
              show-password
              @keyup.enter="handleLogin"
            />
          </el-form-item>

          <el-form-item>
            <div class="form-options">
              <el-checkbox v-model="loginForm.rememberMe">
                记住我
              </el-checkbox>
            </div>
          </el-form-item>

          <el-form-item>
            <el-button
              type="primary"
              size="large"
              style="width: 100%"
              :loading="loginLoading"
              @click="handleLogin"
            >
              登录
            </el-button>
          </el-form-item>

          <el-form-item>
            <div class="login-tip">
              <el-text type="info" size="small">
                开源版使用默认账号：admin / admin123
              </el-text>
            </div>
          </el-form-item>
        </el-form>
      </el-card>

      <div class="login-footer">
        <p>&copy; 2025 TradingAgents-CN. All rights reserved.</p>
        <p class="disclaimer">
          TradingAgents-CN 是一个 AI 多 Agents 的股票分析学习平台。平台中的分析结论、观点和“投资建议”均由 AI 自动生成，仅用于学习、研究与交流，不构成任何形式的投资建议或承诺。用户据此进行的任何投资行为及其产生的风险与后果，均由用户自行承担。市场有风险，入市需谨慎。
        </p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

const loginFormRef = ref()
const loginLoading = ref(false)

const loginForm = reactive({
  username: '',
  password: '',
  rememberMe: false
})

const loginRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码长度不能少于6位', trigger: 'blur' }
  ]
}

const handleLogin = async () => {
  // 防止重复提交
  if (loginLoading.value) {
    console.log('⏭️ 登录请求进行中，跳过重复点击')
    return
  }

  try {
    await loginFormRef.value.validate()

    loginLoading.value = true
    console.log('🔐 开始登录流程...')

    // 调用真实的登录API
    const success = await authStore.login({
      username: loginForm.username,
      password: loginForm.password
    })

    if (success) {
      console.log('✅ 登录成功')
      ElMessage.success('登录成功')

      // 跳转到重定向路径或仪表板
      const redirectPath = authStore.getAndClearRedirectPath()
      console.log('🔄 重定向到:', redirectPath)
      router.push(redirectPath)
    } else {
      ElMessage.error('用户名或密码错误')
    }

  } catch (error) {
    const err = error as Error
    console.error('登录失败:', err)
    // 只有在不是表单验证错误时才显示错误消息
    if (err.message && !err.message.includes('validate')) {
      ElMessage.error('登录失败，请重试')
    }
  } finally {
    loginLoading.value = false
  }
}


</script>

<style lang="scss" scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background:
    linear-gradient(180deg, #f8fafc 0%, #eef3fa 100%);
}

.login-container {
  width: 100%;
  max-width: 420px;
}

.login-header {
  text-align: center;
  margin-bottom: 20px;
  color: #111827;

  .logo {
    width: 48px;
    height: 48px;
    margin-bottom: 12px;
  }

  .title {
    font-size: 24px;
    font-weight: 700;
    margin: 0 0 6px 0;
  }

  .subtitle {
    color: #526072;
    font-size: 14px;
    margin: 0;
  }
}

.login-card {
  border: 1px solid #e4eaf3;
  border-radius: 8px;
  box-shadow: 0 18px 45px rgba(18, 38, 63, 0.08);

  :deep(.el-card__body) {
    padding: 24px;
  }

  .form-options {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
  }

  .login-tip {
    text-align: center;
    width: 100%;
    color: var(--el-text-color-regular);
  }
}

.login-footer {
  text-align: center;
  margin-top: 20px;
  color: #526072;

  p {
    margin: 0;
    font-size: 12px;
  }

  .disclaimer {
    margin-top: 8px;
    font-size: 12px;
    line-height: 1.6;
    max-width: 800px;
    margin-left: auto;
    margin-right: auto;
    color: #6b7280;
  }
}
</style>
