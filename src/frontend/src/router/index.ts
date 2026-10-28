// 路由与全局守卫：未登录跳转 /login；已登录访问 /login 回聊天页。
import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/Login.vue') },
    {
      path: '/',
      component: () => import('@/pages/Shell.vue'),
      children: [
        { path: '', redirect: '/chat' },
        { path: 'chat', name: 'chat', component: () => import('@/pages/chat/ChatView.vue') },
        { path: 'chat/:id', name: 'chat-detail', component: () => import('@/pages/chat/ChatView.vue') },
        { path: 'knowledge', name: 'knowledge', component: () => import('@/pages/console/Knowledge.vue') },
        { path: 'handoff', name: 'handoff', component: () => import('@/pages/console/Handoff.vue') },
        { path: 'dashboard', name: 'dashboard', component: () => import('@/pages/console/Dashboard.vue') },
        { path: 'memory', name: 'memory', component: () => import('@/pages/console/Memory.vue') },
        { path: 'mcp', name: 'mcp', component: () => import('@/pages/console/Mcp.vue') },
        { path: 'settings', name: 'settings', component: () => import('@/pages/console/Settings.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const user = useUserStore()
  if (to.name !== 'login' && !user.token) return { name: 'login' }
  if (to.name === 'login' && user.token) return { name: 'chat' }
})

export default router
