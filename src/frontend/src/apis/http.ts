// REST 请求统一封装：注入 Bearer token、401 自动登出、错误抛 ElMessage。
import { ElMessage } from 'element-plus'
import router from '@/router'

const BASE = '/api/v1'

export class ApiError extends Error {}

/**
 * 401 时彻底清登录态：store 是 persist:true 的，只删 cs_token 会让 user store 里
 * 残留过期 token，路由守卫据此仍判定「已登录」，结果接口调不通又被留在控制台。
 * stores/user.ts 顶层 import 了本模块，这里必须动态 import 打破循环依赖。
 */
async function forceLogout() {
  localStorage.removeItem('cs_token')
  try {
    const { useUserStore } = await import('@/stores/user')
    useUserStore().logout()
  } catch { /* pinia 尚未就绪时至少已清掉 cs_token */ }
  ElMessage.error('登录已过期，请重新登录')
  if (router.currentRoute.value.name !== 'login') await router.push('/login')
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem('cs_token')
  const headers: Record<string, string> = {
    // FormData 交给浏览器自动生成 multipart boundary，不能手设 JSON Content-Type
    ...(typeof options.body === 'string' || options.body === undefined
      ? { 'Content-Type': 'application/json' } : {}),
    ...(options.headers as Record<string, string>),
  }
  if (token) headers.Authorization = `Bearer ${token}`

  const resp = await fetch(BASE + path, { ...options, headers })
  if (resp.status === 401) {
    // 登录接口自身的 401 是「用户名或密码错误」，不能走登出分支（否则清空 store 并提示过期）
    if (router.currentRoute.value.name === 'login') {
      ElMessage.error('用户名或密码错误')
      throw new ApiError('用户名或密码错误')
    }
    await forceLogout()
    throw new ApiError('登录已过期，请重新登录')
  }
  if (!resp.ok) {
    let detail = `请求失败（${resp.status}）`
    try {
      const body = await resp.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch { /* 非 JSON 响应保留默认文案 */ }
    ElMessage.error(detail)
    throw new ApiError(detail)
  }
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

export const http = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(p: string, body: unknown) =>
    request<T>(p, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T>(p: string) => request<T>(p, { method: 'DELETE' }),
  upload: <T>(p: string, formData: FormData) => request<T>(p, { method: 'POST', body: formData }),
}
