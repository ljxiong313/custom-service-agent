<template>
  <!-- 应用壳：左侧窄导航 + 主内容区（布局对齐 AgentChat pages/index.vue） -->
  <div class="shell">
    <AppSidebar />
    <main class="shell-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import AppSidebar from '@/components/AppSidebar.vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
// 持久化快照里的 user 可能没有 role（角色是后加的字段），进壳层先拉一次 /auth/me 校准；
// token 过期时 401 分支会自动登出并跳转登录页。
onMounted(() => { userStore.fetchMe().catch(() => undefined) })
</script>

<style scoped>
.shell { display: flex; height: 100vh; overflow: hidden; }
.shell-main { flex: 1; min-width: 0; display: flex; }
</style>
