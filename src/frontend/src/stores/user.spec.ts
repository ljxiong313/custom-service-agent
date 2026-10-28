// user store：三档角色计算、ROLE_LABELS 完整性、login/register/fetchMe/logout。
// logout 这条挂了真 pinia-plugin-persistedstate（生产 main.ts 就这么装），用来验源码注释里
// 「删掉 'user' 持久化键才能保证刷新后未登录」这条：实测键会在下一个 tick 被插件以空态回写，
// 兜底的是「快照里没有 token」而不是「键一定不存在」——断言按真实行为写。
//
// 隔离手段：vi.mock('@/apis/http') 换掉整层网络请求（get/post 为 spy），
// 网络断言在 http.spec.ts 里单独覆盖；localStorage 每个用例前后清空。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'

// 按 http.ts 实际调用形状参数化，mock.calls 取出来是 [string, unknown] 这种元组
const httpMock = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
  patch: vi.fn<(path: string, body: unknown) => Promise<unknown>>(),
  del: vi.fn<(path: string) => Promise<unknown>>(),
  upload: vi.fn<(path: string, formData: FormData) => Promise<unknown>>(),
}))

vi.mock('@/apis/http', () => ({
  http: httpMock,
  ApiError: class ApiError extends Error {},
}))

import { ROLE_LABELS, useUserStore, type UserInfo, type UserRole } from '@/stores/user'

const PERSIST_KEY = 'user'

/** pinia-plugin-persistedstate 写进 localStorage 的快照形状（store id 即键名）。 */
type PersistedSnapshot = { token?: string; user?: UserInfo | null }

const ROLES: UserRole[] = ['user', 'staff', 'admin']

function makeUser(role: UserRole): UserInfo {
  return { id: 1, username: `u-${role}`, email: `u-${role}@example.com`, role }
}

/**
 * 生产 main.ts 是 `app.use(createPinia().use(piniaPluginPersistedstate))`，
 * 必须把 pinia 装到 app 上插件才会真正启用（setActivePinia 不会跑插件），
 * 所以这里造一个空挂载的 app，让 'user' 持久化键的行为和浏览器里一致。
 */
function createPersistedPinia(): Pinia {
  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  createApp({ render: () => null }).use(pinia)
  setActivePinia(pinia)
  return pinia
}

function readPersistedUser(): PersistedSnapshot | null {
  const raw = localStorage.getItem(PERSIST_KEY)
  return raw ? (JSON.parse(raw) as PersistedSnapshot) : null
}

beforeEach(() => {
  localStorage.clear()
  createPersistedPinia()
})

afterEach(() => {
  localStorage.clear()
})

describe('user store：角色计算', () => {
  it('未登录（user 为 null）时 isAdmin / isStaff 都为假', () => {
    const store = useUserStore()
    expect(store.user).toBeNull()
    expect(store.isAdmin).toBe(false)
    expect(store.isStaff).toBe(false)
    expect(store.isLoggedIn).toBe(false)
  })

  it('user 角色：既不是管理员也不是客服（看不到转人工工作台、删不了知识库）', () => {
    const store = useUserStore()
    store.user = makeUser('user')
    expect(store.isAdmin).toBe(false)
    expect(store.isStaff).toBe(false)
  })

  it('staff 角色：isStaff 为真、isAdmin 为假（可运营但不能做破坏性操作）', () => {
    const store = useUserStore()
    store.user = makeUser('staff')
    expect(store.isAdmin).toBe(false)
    expect(store.isStaff).toBe(true)
  })

  it('admin 角色：isStaff 蕴含 isAdmin（管理员一定能进客服入口）', () => {
    const store = useUserStore()
    store.user = makeUser('admin')
    expect(store.isAdmin).toBe(true)
    expect(store.isStaff).toBe(true)
  })

  it('isAdmin/isStaff 随 user 替换而响应式更新', () => {
    const store = useUserStore()
    store.user = makeUser('user')
    expect(store.isStaff).toBe(false)
    store.user = makeUser('admin')
    expect(store.isStaff).toBe(true)
    expect(store.isAdmin).toBe(true)
    store.user = null
    expect(store.isAdmin).toBe(false)
  })

  it('isLoggedIn 只由 token 决定，与角色无关', () => {
    const store = useUserStore()
    store.user = makeUser('admin')
    expect(store.isLoggedIn).toBe(false)
    store.token = 't'
    expect(store.isLoggedIn).toBe(true)
  })
})

describe('user store：ROLE_LABELS', () => {
  it('覆盖全部三档角色、文案非空且互不相同', () => {
    for (const role of ROLES) {
      expect(typeof ROLE_LABELS[role]).toBe('string')
      expect(ROLE_LABELS[role].length).toBeGreaterThan(0)
    }
    expect(Object.keys(ROLE_LABELS)).toHaveLength(ROLES.length)
    expect(new Set(Object.values(ROLE_LABELS)).size).toBe(ROLES.length)
  })

  it('具体文案与侧栏徽标语义一致：普通成员/客服/管理员', () => {
    expect(ROLE_LABELS.user).toBe('普通成员')
    expect(ROLE_LABELS.staff).toBe('客服')
    expect(ROLE_LABELS.admin).toBe('管理员')
  })
})

describe('user store：登录 / 注册 / 拉取身份', () => {
  it('login 走 /auth/login，token 同时写入 store 与 localStorage(cs_token)', async () => {
    const store = useUserStore()
    httpMock.post.mockResolvedValue({ access_token: 'jwt-abc', user: makeUser('staff') })

    await store.login('ops', 'pw')

    expect(httpMock.post).toHaveBeenCalledWith('/auth/login', { username: 'ops', password: 'pw' })
    expect(store.token).toBe('jwt-abc')
    expect(store.user?.role).toBe('staff')
    expect(localStorage.getItem('cs_token')).toBe('jwt-abc')
  })

  it('register 先注册再自动登录，email 可缺省', async () => {
    const store = useUserStore()
    httpMock.post
      .mockResolvedValueOnce(makeUser('user'))
      .mockResolvedValueOnce({ access_token: 'jwt-reg', user: makeUser('user') })

    await store.register('newbie', 'pw', 'newbie@example.com')

    expect(httpMock.post.mock.calls[0]).toEqual(['/auth/register', { username: 'newbie', password: 'pw', email: 'newbie@example.com' }])
    expect(httpMock.post.mock.calls[1]).toEqual(['/auth/login', { username: 'newbie', password: 'pw' }])
    expect(store.token).toBe('jwt-reg')

    httpMock.post.mockResolvedValueOnce(makeUser('user'))
    httpMock.post.mockResolvedValueOnce({ access_token: 'jwt-reg2', user: makeUser('user') })
    await store.register('noemail', 'pw')
    expect(httpMock.post.mock.calls[2]).toEqual(['/auth/register', { username: 'noemail', password: 'pw', email: undefined }])
  })

  it('fetchMe 有 token 时用服务端身份覆盖本地（角色变更刷新即生效）', async () => {
    const store = useUserStore()
    store.token = 'jwt'
    store.user = makeUser('staff')
    httpMock.get.mockResolvedValue(makeUser('admin'))

    await store.fetchMe()

    expect(httpMock.get).toHaveBeenCalledWith('/auth/me')
    expect(store.user?.role).toBe('admin')
    expect(store.isAdmin).toBe(true)
  })

  it('fetchMe 无 token 时不发请求（避免匿名打 /auth/me 触发 401 登出）', async () => {
    const store = useUserStore()
    await store.fetchMe()
    expect(httpMock.get).not.toHaveBeenCalled()
    expect(store.user).toBeNull()
  })
})

describe('user store：logout 清理登录态', () => {
  it('logout 清 token、清 user、清 cs_token', async () => {
    const store = useUserStore()
    httpMock.post.mockResolvedValue({ access_token: 'jwt-x', user: makeUser('admin') })
    await store.login('admin', 'pw')
    expect(localStorage.getItem('cs_token')).toBe('jwt-x')

    store.logout()
    await nextTick()

    expect(store.token).toBe('')
    expect(store.user).toBeNull()
    expect(store.isLoggedIn).toBe(false)
    expect(localStorage.getItem('cs_token')).toBeNull()
  })

  it(`持久化键 ${PERSIST_KEY}：logout 同步删键，插件回写的也只是空态，刷新后不可能复活登录态`, async () => {
    const store = useUserStore()
    httpMock.post.mockResolvedValue({ access_token: 'jwt-y', user: makeUser('staff') })
    await store.login('ops', 'pw')
    await nextTick()

    // 前提：持久化插件确实写了快照（否则这条用例没意义）
    expect(readPersistedUser()?.token).toBe('jwt-y')

    store.logout()
    // 源码里 removeItem(PERSIST_KEY) 的效果（同步可见）
    expect(localStorage.getItem(PERSIST_KEY)).toBeNull()
    expect(localStorage.getItem('cs_token')).toBeNull()

    await nextTick()
    // 实测行为：persistedstate 的 $subscribe 是 pre-flush，会在下一个 tick 把当前 state
    // 再写回同一个键，所以「删键」只是短暂生效；真正兜底的是回写内容里没有可用 token。
    const snapshot: PersistedSnapshot | null = readPersistedUser()
    if (snapshot === null) {
      expect(localStorage.getItem(PERSIST_KEY)).toBeNull()
    } else {
      expect(snapshot.token).toBe('')
      expect(snapshot.user).toBeNull()
    }
    expect(localStorage.getItem(PERSIST_KEY) ?? '').not.toContain('jwt-y')

    // 模拟刷新：新 pinia 从 localStorage 水合，必须仍是未登录
    createPersistedPinia()
    const reloaded = useUserStore()
    expect(reloaded.isLoggedIn).toBe(false)
    expect(reloaded.user).toBeNull()
    expect(reloaded.isStaff).toBe(false)
  })

  it('setToken 语义（经 login/logout 间接验证）：写入即落盘、置空即删除', async () => {
    const store = useUserStore()
    httpMock.post.mockResolvedValue({ access_token: 'first', user: makeUser('user') })
    await store.login('a', 'b')
    httpMock.post.mockResolvedValue({ access_token: 'second', user: makeUser('user') })
    await store.login('a', 'b')
    expect(localStorage.getItem('cs_token')).toBe('second')

    store.logout()
    await nextTick()
    expect(localStorage.getItem('cs_token')).toBeNull()
  })
})
