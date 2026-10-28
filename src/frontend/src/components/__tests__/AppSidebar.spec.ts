// AppSidebar：navItems 对非 staff 隐藏「转人工工作台」，角色徽标短文案（管理/客服/成员）
// 与 ROLE_LABELS 长文案（aria-label）都对得上。
//
// 隔离手段：
//   - 不 import '@/router'（那份是 createWebHistory 单例 + 全局守卫，未登录会被弹去 /login），
//     改成用例内 createMemoryHistory 的同构路由表，路径名与真实路由一致；
//   - setActivePinia + 同一个 pinia 实例作为插件挂载，先把 user store 的 role 摆好；
//   - Element Plus 用真组件（el-tooltip 会原样渲染默认插槽里的 router-link，
//     弹层只在 hover 时创建，不影响这里的 DOM 断言）。
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import ElementPlus, { ElTooltip } from 'element-plus'

import AppSidebar from '@/components/AppSidebar.vue'
import { ROLE_LABELS, useUserStore, type UserRole } from '@/stores/user'

const BLANK = { template: '<div />' }

async function makeRouter(path = '/chat'): Promise<Router> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: BLANK },
      { path: '/login', name: 'login', component: BLANK },
      { path: '/chat', name: 'chat', component: BLANK },
      { path: '/chat/:id', name: 'chat-detail', component: BLANK },
      { path: '/knowledge', name: 'knowledge', component: BLANK },
      { path: '/handoff', name: 'handoff', component: BLANK },
      { path: '/memory', name: 'memory', component: BLANK },
      { path: '/mcp', name: 'mcp', component: BLANK },
      { path: '/settings', name: 'settings', component: BLANK },
    ],
  })
  // push 是异步的：不 await 就挂载，首帧 useRoute().path 还是起始值 '/'，
  // active 高亮断言会假失败（真实应用里路由早已解析完，所以这是测试侧时序问题）
  await router.push(path)
  await router.isReady()
  return router
}

async function mountSidebar(role: UserRole | null, path = '/chat') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useUserStore()
  store.token = role === null ? '' : 'jwt'
  store.user = role === null ? null : { id: 1, username: `u-${role}`, email: null, role }

  const router = await makeRouter(path)
  const wrapper = mount(AppSidebar, { global: { plugins: [pinia, router, ElementPlus] } })
  return { wrapper, store, router }
}

// mountSidebar 是 async 的，wrapper 类型要从 Promise 的解析结果里取
type SidebarWrapper = Awaited<ReturnType<typeof mountSidebar>>['wrapper']

function navLabels(wrapper: SidebarWrapper): string[] {
  return wrapper.findAll('.nav-link').map((link) => link.attributes('aria-label') ?? '')
}

function linkByLabel(wrapper: SidebarWrapper, label: string) {
  return wrapper.findAll('.nav-link').find((l) => l.attributes('aria-label') === label)
}

describe('AppSidebar：一级导航按角色过滤', () => {
  it('普通成员看不到「转人工工作台」（后端会 403，藏掉少一次报错弹窗）', async () => {
    const { wrapper } = await mountSidebar('user')
    const labels = navLabels(wrapper)

    expect(labels).toEqual(['聊天', '知识库', '记忆', 'MCP 服务', '设置'])
    expect(labels).not.toContain('运营看板')        // 看板是运营视角，普通成员看不到
    expect(labels).not.toContain('转人工工作台')
    expect(wrapper.text()).not.toContain('转人工工作台')
    wrapper.unmount()
  })

  it('客服看得到「转人工工作台」，且顺序仍在知识库之后', async () => {
    const { wrapper } = await mountSidebar('staff')
    const labels = navLabels(wrapper)

    expect(labels).toContain('转人工工作台')
    expect(labels).toHaveLength(7)
    expect(labels.indexOf('转人工工作台')).toBe(labels.indexOf('知识库') + 1)
    wrapper.unmount()
  })

  it('管理员看得到全部 7 个入口（isStaff 蕴含 isAdmin）', async () => {
    const { wrapper } = await mountSidebar('admin')
    expect(navLabels(wrapper)).toHaveLength(7)
    expect(navLabels(wrapper)).toEqual(
      ['聊天', '知识库', '转人工工作台', '运营看板', '记忆', 'MCP 服务', '设置'])
    wrapper.unmount()
  })

  it('未登录（user 为 null）时按普通成员处理，不泄露运营入口', async () => {
    const { wrapper } = await mountSidebar(null)
    expect(navLabels(wrapper)).not.toContain('转人工工作台')
    wrapper.unmount()
  })

  it('当前路径对应的入口带 active，其它入口不带（含 /handoff 这种一级命中）', async () => {
    const { wrapper } = await mountSidebar('staff', '/handoff')

    expect(linkByLabel(wrapper, '转人工工作台')).toBeTruthy()
    expect(linkByLabel(wrapper, '转人工工作台')!.find('.nav-item.active').exists()).toBe(true)
    expect(linkByLabel(wrapper, '聊天')!.find('.nav-item.active').exists()).toBe(false)
    expect(linkByLabel(wrapper, '设置')!.find('.nav-item.active').exists()).toBe(false)
    wrapper.unmount()
  })

  it('子路径也高亮一级入口（/chat/12 应该亮「聊天」）', async () => {
    const { wrapper } = await mountSidebar('staff', '/chat/12')
    expect(linkByLabel(wrapper, '聊天')!.find('.nav-item.active').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('AppSidebar：角色徽标', () => {
  async function badge(role: UserRole | null) {
    const { wrapper } = await mountSidebar(role)
    const found = wrapper.find('.role-badge')
    const result = { text: found.text(), classes: found.classes(), aria: found.attributes('aria-label') }
    wrapper.unmount()
    return result
  }

  it('短文案：admin=管理 / staff=客服 / user=成员', async () => {
    expect((await badge('admin')).text).toBe('管理')
    expect((await badge('staff')).text).toBe('客服')
    expect((await badge('user')).text).toBe('成员')
  })

  it('aria-label 用 ROLE_LABELS 的完整文案，屏幕阅读器能读到权限档', async () => {
    const cases: Array<[UserRole, string]> = [
      ['admin', ROLE_LABELS.admin],
      ['staff', ROLE_LABELS.staff],
      ['user', ROLE_LABELS.user],
    ]
    for (const [role, label] of cases) {
      expect((await badge(role)).aria).toBe(`当前角色：${label}`)
    }
  })

  it('样式类：staff 及以上用 admin 类，普通成员用 member 类', async () => {
    expect((await badge('admin')).classes).toContain('admin')
    expect((await badge('staff')).classes).toContain('admin')
    expect((await badge('user')).classes).toContain('member')
  })

  it('账号入口存在，且 tooltip 文案是「用户名（角色全名）」', async () => {
    const { wrapper } = await mountSidebar('staff')
    expect(wrapper.find('[aria-label="账号菜单"]').exists()).toBe(true)

    // 弹层内容是 hover 才挂到 DOM 上的，所以断言组件的 content prop 而不是文本
    const contents = wrapper.findAllComponents(ElTooltip).map((t) => String(t.props('content') ?? ''))
    expect(contents).toContain('u-staff（客服）')
    wrapper.unmount()
  })

  it('未登录时账号 tooltip 退回「未登录（普通成员）」', async () => {
    const { wrapper } = await mountSidebar(null)
    const contents = wrapper.findAllComponents(ElTooltip).map((t) => String(t.props('content') ?? ''))
    expect(contents).toContain('未登录（普通成员）')
    wrapper.unmount()
  })
})
