// 用户状态：token 同时写入 pinia 持久化与 localStorage（http/chat 层直接读 localStorage，
// 避免 store 循环依赖）。登录/登出负责两者的同步。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { http } from '@/apis/http'

export type UserRole = 'user' | 'staff' | 'admin'
export interface UserInfo { id: number; username: string; email: string | null; role: UserRole }

// pinia-plugin-persistedstate 默认用 store id 作 localStorage 键
const PERSIST_KEY = 'user'

export const ROLE_LABELS: Record<UserRole, string> = {
  user: '普通成员',
  staff: '客服',
  admin: '管理员',
}

export const useUserStore = defineStore('user', () => {
  const token = ref<string>('')
  const user = ref<UserInfo | null>(null)

  const isLoggedIn = computed(() => !!token.value)
  // 知识库重入库/删除、MCP 服务器增删改等破坏性操作仅管理员可用（后端同样有 403 门禁）
  const isAdmin = computed(() => user.value?.role === 'admin')
  // 客服及以上：跨用户会话运营台、工单看板、MCP 确认卡片、外发箱
  const isStaff = computed(() => user.value?.role === 'admin' || user.value?.role === 'staff')

  function setToken(t: string | null) {
    token.value = t ?? ''
    if (t) localStorage.setItem('cs_token', t)
    else localStorage.removeItem('cs_token')
  }

  async function login(username: string, password: string) {
    const resp = await http.post<{ access_token: string; user: UserInfo }>('/auth/login', { username, password })
    setToken(resp.access_token)
    user.value = resp.user
  }

  async function register(username: string, password: string, email?: string) {
    await http.post<UserInfo>('/auth/register', { username, password, email })
    await login(username, password)
  }

  /** 拉取服务端最新身份：角色被变更后刷新页面即可生效（旧持久化数据没有 role 字段）。 */
  async function fetchMe() {
    if (!token.value) return
    user.value = await http.get<UserInfo>('/auth/me')
  }

  function logout() {
    setToken(null)
    user.value = null
    // 持久化插件只在 state 变化时回写，光靠上面的赋值仍可能留下 'user' 键的旧快照；
    // 直接删键才能保证刷新后是干净的未登录态。
    localStorage.removeItem(PERSIST_KEY)
  }

  return { token, user, isLoggedIn, isAdmin, isStaff, login, register, fetchMe, logout }
}, { persist: true })

