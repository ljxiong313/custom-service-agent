// http.ts 的行为契约：Bearer 自动注入、401 强制登出（含持久化快照清理）、
// 非 2xx 用后端 detail 文案提示、204 不解析 body。
//
// 隔离手段：
//   - vi.stubGlobal('fetch', ...) 伪造 Response，全程离线，不打真后端；
//   - vi.mock('@/router')：@/router 是 createWebHistory 的单例，且带全局守卫，
//     真用它会让 forceLogout 触发一次真实导航并在无 app 上下文时报警；换成最小桩，
//     currentRoute 用 getter 暴露，方便逐例切换「是否在 login 路由」；
//   - vi.mock('element-plus')：ElMessage.error 会往 document.body 挂 toast，
//     这里只关心它被用什么文案调用；
//   - setActivePinia(createPinia())：forceLogout 内部动态 import @/stores/user，
//     需要活动的 pinia 实例才能拿到 store（拿不到时源码是 try/catch 兜底，只清 cs_token）。
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// fetch 的桩签名只覆盖 http.ts 的实际调用形式 fetch(url, RequestInit)：
// 参数化之后 mock.calls 就是 [string, RequestInit?]，取参数不用再写 as string / as RequestInit。
type FetchStub = (input: string, init?: RequestInit) => Promise<Response>
type JsonStub = () => Promise<unknown>

const mocks = vi.hoisted((): { push: Mock; currentRouteName: string; elError: Mock } => ({
  push: vi.fn(),
  currentRouteName: 'chat',
  elError: vi.fn(),
}))

vi.mock('@/router', () => ({
  default: {
    get currentRoute() {
      return { value: { name: mocks.currentRouteName } }
    },
    push: mocks.push,
  },
}))

vi.mock('element-plus', () => ({ ElMessage: { error: mocks.elError } }))

// 被测试模块要在 mock 注册之后引入，保证它拿到的是桩 router / 桩 ElMessage
import { ApiError, http } from '@/apis/http'
import { useUserStore } from '@/stores/user'

const fetchMock = vi.fn<FetchStub>()

/** 伪造 fetch 响应；jsonSpy 用来断言 204 分支确实没去解析 body。 */
function respondWith(
  status: number,
  body?: unknown,
  options: { jsonThrows?: boolean } = {},
): { resp: Response; jsonSpy: Mock<JsonStub> } {
  const jsonSpy = vi.fn<JsonStub>(async () => {
    if (options.jsonThrows) throw new TypeError('Unexpected token < in JSON')
    return body
  })
  // 只造 http.ts 真正读到的三个成员：status / ok / json()
  const resp = { status, ok: status >= 200 && status < 300, json: jsonSpy } as unknown as Response
  return { resp, jsonSpy }
}

/**
 * http.ts 每个请求只调一次 fetch，参数顺序固定 (url, init)，
 * 所以「最后一次调用」就足以定位当前用例发出的请求。
 */
function lastRequest(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
  return { url, init: init ?? {} }
}

function headersOf(init: RequestInit): Record<string, string> {
  return (init.headers ?? {}) as Record<string, string>
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  mocks.currentRouteName = 'chat'
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('http：请求头注入', () => {
  it('有 cs_token 时带上 Authorization: Bearer，并走 /api/v1 前缀', async () => {
    localStorage.setItem('cs_token', 'tok-123')
    fetchMock.mockResolvedValue(respondWith(200, { username: 'ops' }).resp)

    const data = await http.get<{ username: string }>('/auth/me')

    expect(data).toEqual({ username: 'ops' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const { url, init } = lastRequest()
    expect(url).toBe('/api/v1/auth/me')
    expect(headersOf(init).Authorization).toBe('Bearer tok-123')
  })

  it('未登录（无 cs_token）时不注入 Authorization', async () => {
    fetchMock.mockResolvedValue(respondWith(200, { detail: 'ok' }).resp)

    await http.get('/auth/me')

    expect(headersOf(lastRequest().init).Authorization).toBeUndefined()
  })

  it('普通请求默认带 application/json，且只从 localStorage 读 token（不依赖 store）', async () => {
    localStorage.setItem('cs_token', 'from-local-storage')
    fetchMock.mockResolvedValue(respondWith(200, {}).resp)

    await http.post('/auth/login', { username: 'a', password: 'b' })

    const { init } = lastRequest()
    expect(headersOf(init)['Content-Type']).toBe('application/json')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ username: 'a', password: 'b' }))
    expect(headersOf(init).Authorization).toBe('Bearer from-local-storage')
  })

  it('upload 传 FormData 时绝不手设 Content-Type（boundary 要交给浏览器生成）', async () => {
    localStorage.setItem('cs_token', 'tok-upload')
    fetchMock.mockResolvedValue(respondWith(200, { url: '/api/files/x.png' }).resp)
    const fd = new FormData()
    fd.append('file', new File(['png-bytes'], 'x.png', { type: 'image/png' }))

    await http.upload('/console/media', fd)

    const { url, init } = lastRequest()
    expect(url).toBe('/api/v1/console/media')
    expect(init.method).toBe('POST')
    expect(headersOf(init)['Content-Type']).toBeUndefined()
    expect(headersOf(init).Authorization).toBe('Bearer tok-upload')
    expect(init.body).toBeInstanceOf(FormData)
    // 透传的是调用方给的那个 FormData 实例（不是新建一份），文件也只有一个
    expect((init.body as FormData).get('file')).toBeInstanceOf(File)
    expect((init.body as FormData).getAll('file')).toHaveLength(1)
  })

  it('patch / del 用对应 HTTP 方法', async () => {
    fetchMock.mockResolvedValue(respondWith(200, { ok: true }).resp)
    await http.patch('/conversations/1', { title: 't' })
    expect(lastRequest().init.method).toBe('PATCH')

    await http.del('/conversations/1')
    expect(lastRequest().init.method).toBe('DELETE')
  })

  it('204 直接返回 undefined，不去解析 body', async () => {
    const { resp, jsonSpy } = respondWith(204)
    fetchMock.mockResolvedValue(resp)

    const data = await http.del<void>('/conversations/9')

    expect(data).toBeUndefined()
    expect(jsonSpy).not.toHaveBeenCalled()
  })
})

describe('http：401 强制登出', () => {
  it('非 login 路由收到 401：清 cs_token、清 store 与持久化快照、提示过期并跳 /login', async () => {
    const store = useUserStore()
    store.token = 'expired-token'
    store.user = { id: 7, username: 'ops', email: null, role: 'staff' }
    localStorage.setItem('cs_token', 'expired-token')
    // pinia-plugin-persistedstate 用 store id 作键，这里预置一份「刷新后仍是已登录」的脏快照
    localStorage.setItem('user', JSON.stringify({ token: 'expired-token', user: { id: 7, role: 'staff' } }))
    fetchMock.mockResolvedValue(respondWith(401, { detail: 'token expired' }).resp)

    const err = await http.get('/console/conversations').catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect((err as Error).message).toBe('登录已过期，请重新登录')

    expect(localStorage.getItem('cs_token')).toBeNull()
    expect(localStorage.getItem('user')).toBeNull()
    expect(store.token).toBe('')
    expect(store.user).toBeNull()
    expect(mocks.elError).toHaveBeenCalledWith('登录已过期，请重新登录')
    expect(mocks.push).toHaveBeenCalledWith('/login')
  })

  it('已经在 /login 时 401 是「用户名或密码错误」，不清登录态也不跳转', async () => {
    mocks.currentRouteName = 'login'
    localStorage.setItem('cs_token', 'whatever')
    fetchMock.mockResolvedValue(respondWith(401, { detail: 'bad credentials' }).resp)

    await expect(http.post('/auth/login', { username: 'a', password: 'b' })).rejects.toThrow('用户名或密码错误')

    expect(mocks.elError).toHaveBeenCalledWith('用户名或密码错误')
    expect(mocks.push).not.toHaveBeenCalled()
    expect(localStorage.getItem('cs_token')).toBe('whatever')
  })

  it('非 login 路由时只跳转一次 /login', async () => {
    mocks.currentRouteName = 'settings'
    fetchMock.mockResolvedValue(respondWith(401).resp)

    await expect(http.get('/console/conversations')).rejects.toBeInstanceOf(ApiError)
    expect(mocks.push).toHaveBeenCalledTimes(1)
    expect(mocks.push).toHaveBeenCalledWith('/login')
  })
})

describe('http：非 2xx 的错误提示', () => {
  it('detail 是字符串时原文提示并抛出同一个文案的 ApiError', async () => {
    fetchMock.mockResolvedValue(respondWith(500, { detail: '知识库索引重建失败' }).resp)

    await expect(http.post('/knowledge/reindex')).rejects.toBeInstanceOf(ApiError)
    expect(mocks.elError).toHaveBeenCalledWith('知识库索引重建失败')
  })

  it('detail 是对象（FastAPI 校验错误数组）时序列化后提示', async () => {
    const detail = [{ loc: ['body', 'text'], msg: 'field required', type: 'missing' }]
    fetchMock.mockResolvedValue(respondWith(422, { detail }).resp)

    await expect(http.post('/console/conversations/1/reply', {})).rejects.toThrow(JSON.stringify(detail))
    expect(mocks.elError).toHaveBeenCalledWith(JSON.stringify(detail))
  })

  it('响应不是 JSON 时退回「请求失败（状态码）」', async () => {
    fetchMock.mockResolvedValue(respondWith(502, '<html>Bad Gateway</html>', { jsonThrows: true }).resp)

    await expect(http.get('/auth/me')).rejects.toThrow('请求失败（502）')
    expect(mocks.elError).toHaveBeenCalledWith('请求失败（502）')
  })

  it('403 的 detail 会被透传（前端藏入口，但后端门禁才是权威）', async () => {
    fetchMock.mockResolvedValue(respondWith(403, { detail: '需要管理员权限' }).resp)

    await expect(http.del('/knowledge/files/1')).rejects.toThrow('需要管理员权限')
    expect(mocks.elError).toHaveBeenCalledWith('需要管理员权限')
  })
})
