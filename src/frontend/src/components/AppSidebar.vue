<template>
  <!-- 一级导航（AgentChat AppSidebar 风格）：聊天 + P1~P5 各控制台入口 -->
  <nav class="sidebar" aria-label="主导航">
    <div class="logo" aria-hidden="true">客</div>
    <el-tooltip v-for="item in navItems" :key="item.path" :content="item.label" placement="right">
      <router-link :to="item.path" class="nav-link" :aria-label="item.label">
        <div class="nav-item" :class="{ active: isActive(item.path) }">
          <el-icon :size="20" aria-hidden="true"><component :is="item.icon" /></el-icon>
        </div>
      </router-link>
    </el-tooltip>
    <div class="spacer" />
    <!-- 角色徽标：窄侧栏没有文字，运营需要一眼确认自己是不是管理员（决定按钮是否可点） -->
    <el-tooltip :content="`${userStore.user?.username ?? '未登录'}（${userStore.isAdmin ? '管理员' : '普通成员'}）`" placement="right">
      <el-dropdown trigger="click" @command="onCommand">
        <div class="nav-item avatar" role="button" tabindex="0" aria-label="账号菜单">
          <el-avatar :size="30">{{ (userStore.user?.username ?? '?')[0] }}</el-avatar>
        </div>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="logout">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-tooltip>
    <div class="role-badge" :class="userStore.isAdmin ? 'admin' : 'member'"
         :aria-label="userStore.isAdmin ? '当前角色：管理员' : '当前角色：普通成员'">
      {{ userStore.isAdmin ? '管理' : '成员' }}
    </div>
  </nav>
</template>

<script setup lang="ts">
import { ChatDotRound, Collection, Connection, Reading, Service, Setting } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const route = useRoute()
const router = useRouter()

const navItems = [
  { path: '/chat', label: '聊天', icon: ChatDotRound },
  { path: '/knowledge', label: '知识库', icon: Reading },
  { path: '/handoff', label: '转人工工作台', icon: Service },
  { path: '/memory', label: '记忆', icon: Collection },
  { path: '/mcp', label: 'MCP 服务', icon: Connection },
  { path: '/settings', label: '设置', icon: Setting },
]

function isActive(path: string) {
  return route.path === path || route.path.startsWith(path + '/')
}

function onCommand(cmd: string) {
  if (cmd === 'logout') {
    userStore.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.sidebar {
  width: 56px; display: flex; flex-direction: column; align-items: center;
  padding: 12px 0; background: var(--ac-surface);
  border-right: 1px solid var(--ac-border);
}
.logo {
  width: 34px; height: 34px; border-radius: 10px; margin-bottom: 16px;
  background: var(--ac-brand); color: #fff; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.nav-link { display: block; }
.nav-item {
  width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center;
  justify-content: center; cursor: pointer; color: var(--ac-text-secondary); margin-bottom: 8px;
}
.nav-item:hover { background: var(--ac-brand-light); color: var(--ac-brand); }
.nav-item.active { background: var(--ac-brand-light); color: var(--ac-brand); }
.spacer { flex: 1; }
.role-badge {
  margin-top: 2px; padding: 1px 4px; border-radius: 6px; font-size: 10px; line-height: 1.5;
  background: var(--ac-brand-light); color: var(--ac-brand);
}
.role-badge.member { background: var(--ac-bg); color: var(--ac-text-secondary); }
</style>
