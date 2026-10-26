<template>
  <!-- 登录/注册页：居中卡片，品牌蓝主按钮 -->
  <div class="login-page">
    <div class="login-card">
      <h1 class="brand">智能客服 Agent</h1>
      <p class="subtitle">RAG 知识库 · 三级记忆 · 企业微信转人工 · MCP</p>

      <el-tabs v-model="tab" stretch>
        <el-tab-pane label="登录" name="login" />
        <el-tab-pane label="注册" name="register" />
      </el-tabs>

      <el-form :model="form" @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码（至少 6 位）"
                    size="large" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-form-item v-if="tab === 'register'">
          <el-input v-model="form.email" placeholder="邮箱（可选）" size="large" />
        </el-form-item>
        <el-button type="primary" size="large" class="submit-btn" :loading="loading" @click="submit">
          {{ tab === 'login' ? '登 录' : '注 册' }}
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const tab = ref<'login' | 'register'>('login')
const loading = ref(false)
const form = reactive({ username: '', password: '', email: '' })

async function submit() {
  if (!form.username || form.password.length < 6) {
    ElMessage.warning('请填写用户名，且密码至少 6 位')
    return
  }
  loading.value = true
  try {
    if (tab.value === 'login') await userStore.login(form.username, form.password)
    else await userStore.register(form.username, form.password, form.email || undefined)
    router.push('/chat')
  } catch {
    /* http 层已弹错误提示 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #eef5fc 0%, #f7f9fb 60%);
}
.login-card {
  width: 380px; padding: 36px 32px; background: var(--ac-surface);
  border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  box-shadow: 0 8px 30px rgba(27, 124, 228, 0.08);
}
.brand { font-size: 22px; color: var(--ac-brand); margin: 0 0 4px; text-align: center; }
.subtitle { font-size: 12px; color: var(--ac-text-secondary); text-align: center; margin-bottom: 18px; }
.submit-btn { width: 100%; margin-top: 4px; }
</style>
