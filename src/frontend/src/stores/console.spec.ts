// console store：meta_json → 运营侧展示字段（parseMessage，模块内私有，通过 select/refreshMessages
// 的返回值间接断言，不动源码导出）、uploadMedia 走 /console/media 并原样返回带 sig= 的签名 url、
// reply 的载荷形状与「发送后以服务端为准重拉」的顺序。
//
// 隔离手段：vi.mock('@/apis/http')（http 层自身的行为在 http.spec.ts 单独覆盖）；
// 这里只断言「调到哪个路径、传了什么 body、返回怎么落到 state」。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// get/post/... 用真实 http.ts 的调用形式参数化，mock.calls 取出来就是 [string, FormData] 这种
// 元组，断言里不用再手写 as 断言。
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

import { useConsoleStore, type AttachmentItem } from '@/stores/console'

// 与后端 app/utils/media_url.py::sign_url 同形：/api/files/<fname>?exp=&sig=
const SIGNED_IMAGE = '/api/files/3f2a1b4c.png?exp=1790000000&sig=9c1d2e3f4a5b'
const SIGNED_FILE = '/api/files/report.pdf?exp=1790000000&sig=77aa55bb33cc11dd'

// 与后端 /console/conversations/{id}/messages 的返回形状一致
// （也是 console.ts 里 parseMessage 的入参类型：meta 是解过 meta_json 的对象或 null）。
type RawMessage = {
  id: number
  role: string
  content: string
  meta: Record<string, unknown> | null
  created_at?: string
}

function rawAssistant(overrides: Partial<RawMessage> = {}): RawMessage {
  return {
    id: 11,
    role: 'assistant',
    content: '已为您查询到订单',
    created_at: '2026-02-11T10:00:00Z',
    meta: null,
    ...overrides,
  }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

afterEach(() => {
  localStorage.clear()
})

describe('console store：meta_json 解析（经 select）', () => {
  it('把 tools/citations/attachments/from/staff 还原成展示字段', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([
      rawAssistant({
        meta: {
          tools: [{ name: 'search_knowledge', status: 'done', output: '命中 3 条' }],
          citations: [{ index: 1, file_name: '售后政策.md', snippet: '7 天无理由' }],
          attachments: [{ url: SIGNED_IMAGE, name: 'proof.png', type: 'image' }],
          from: 'human_operator',
          staff: 'ops-a',
        },
      }),
    ])

    await store.select(42)

    expect(httpMock.get).toHaveBeenCalledWith('/console/conversations/42/messages')
    expect(store.currentId).toBe(42)
    const [msg] = store.messages
    expect(msg.role).toBe('assistant')
    expect(msg.created_at).toBe('2026-02-11T10:00:00Z')
    expect(msg.tools).toEqual([{ name: 'search_knowledge', status: 'done', output: '命中 3 条' }])
    expect(msg.citations).toEqual([{ index: 1, file_name: '售后政策.md', snippet: '7 天无理由' }])
    // 附件 url 必须是接口给的签名链接，MessageList 直接拿来渲染
    expect(msg.attachments).toEqual([{ url: SIGNED_IMAGE, name: 'proof.png', type: 'image' }])
    expect(msg.from).toBe('human_operator')
    expect(msg.staff).toBe('ops-a')
  })

  it('meta 为 null 的历史消息不报错，扩展字段全为 undefined', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant({ id: 3, role: 'user', content: '退货运费谁出？' })])

    await store.select(1)

    const [msg] = store.messages
    expect(msg).toMatchObject({ id: 3, role: 'user', content: '退货运费谁出？' })
    expect(msg.tools).toBeUndefined()
    expect(msg.citations).toBeUndefined()
    expect(msg.attachments).toBeUndefined()
    expect(msg.from).toBeUndefined()
    expect(msg.staff).toBeUndefined()
  })

  it('meta 只带部分字段时按需解析（系统提示消息只有 from）', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant({ meta: { from: 'system_session_closed' } })])

    await store.select(1)

    expect(store.messages[0].from).toBe('system_session_closed')
    expect(store.messages[0].attachments).toBeUndefined()
  })

  it('空数组会话把 messages 置空，不会残留上一条会话的消息', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant()])
    await store.select(1)
    expect(store.messages).toHaveLength(1)

    httpMock.get.mockResolvedValue([])
    await store.select(2)
    expect(store.messages).toEqual([])
    expect(store.currentId).toBe(2)
  })
})

describe('console store：refreshMessages 的「无变化不重赋值」', () => {
  it('条数与末条内容都没变时保持同一数组引用（不打断滚动位置）', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant()])
    await store.select(5)
    const before = store.messages

    await store.refreshMessages()

    expect(store.messages).toBe(before)
    expect(httpMock.get).toHaveBeenCalledTimes(2)
  })

  it('末条内容变化（同一 id，如人工补发）也会触发重新赋值', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant({ content: '旧文案' })])
    await store.select(5)
    const before = store.messages

    httpMock.get.mockResolvedValue([rawAssistant({ content: '新文案' })])
    await store.refreshMessages()

    expect(store.messages).not.toBe(before)
    expect(store.messages[0].content).toBe('新文案')
  })

  it('没有选中会话时不发请求', async () => {
    const store = useConsoleStore()
    await store.refreshMessages()
    expect(httpMock.get).not.toHaveBeenCalled()
  })
})

describe('console store：current()', () => {
  it('按 currentId 在列表里取会话，取不到返回 null', async () => {
    const store = useConsoleStore()
    httpMock.get.mockResolvedValue([rawAssistant()])
    await store.select(7)
    httpMock.get.mockResolvedValue([
      { id: 7, title: 't7', username: 'u', channel: 'taobao', shop_id: 's', buyer_id: 'b', handoff_status: 'bot', status: 'active', updated_at: '', last_message: '', last_role: 'user' },
    ])
    await store.fetchList()

    expect(store.current()?.id).toBe(7)
    store.currentId = 999
    expect(store.current()).toBeNull()
  })
})

describe('console store：uploadMedia', () => {
  it('以 multipart 打到 /console/media，并原样返回带 sig= 的签名 url', async () => {
    const store = useConsoleStore()
    httpMock.upload.mockResolvedValue({ url: SIGNED_IMAGE, name: 'proof.png', type: 'image' })
    const file = new File(['bytes'], 'proof.png', { type: 'image/png' })

    const att = await store.uploadMedia(file)

    expect(httpMock.upload).toHaveBeenCalledTimes(1)
    // uploadMedia 自己 new 了一个 FormData（不是把调用方的实例透传），
    // 所以断言形态：打到 /console/media + multipart 体里就是那个 File。
    const [path, body] = httpMock.upload.mock.calls[0]
    expect(path).toBe('/console/media')
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)
    expect(att.url).toBe(SIGNED_IMAGE)
    expect(att.url).toContain('sig=')
    expect(att).toEqual<AttachmentItem>({ url: SIGNED_IMAGE, name: 'proof.png', type: 'image' })
  })

  it('非图片文件返回 type=file 的签名链接', async () => {
    const store = useConsoleStore()
    httpMock.upload.mockResolvedValue({ url: SIGNED_FILE, name: 'report.pdf', type: 'file' })

    const att = await store.uploadMedia(new File(['%PDF'], 'report.pdf', { type: 'application/pdf' }))

    expect(att.type).toBe('file')
    expect(att.url).toContain('sig=')
  })
})

describe('console store：reply', () => {
  it('载荷是 {text, attachments} 打到 /console/conversations/{id}/reply，随后重拉消息和列表', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    const attachments: AttachmentItem[] = [{ url: SIGNED_IMAGE, name: 'proof.png', type: 'image' }]
    httpMock.post.mockResolvedValue({ message_id: 88 })
    httpMock.get.mockResolvedValue([rawAssistant({ id: 88, meta: { from: 'human_operator', attachments } })])

    await store.reply('已为您登记', attachments)

    expect(httpMock.post).toHaveBeenCalledWith('/console/conversations/42/reply', { text: '已为您登记', attachments })
    // 非乐观插入：消息与列表都以服务端为准（来源徽标/meta 由后端补）
    expect(httpMock.get).toHaveBeenCalledWith('/console/conversations/42/messages')
    expect(httpMock.get).toHaveBeenCalledWith('/console/conversations')
    expect(store.replying).toBe(false)
  })

  it('选中会话变化时使用最新 id', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    httpMock.post.mockResolvedValue({ message_id: 1 })
    httpMock.get.mockResolvedValue([])

    await store.reply('第一条', [])
    store.currentId = 100
    await store.reply('第二条', [])

    expect(httpMock.post.mock.calls[0][0]).toBe('/console/conversations/42/reply')
    expect(httpMock.post.mock.calls[1][0]).toBe('/console/conversations/100/reply')
  })

  it('正文与附件都为空时直接返回，不发送也不置 replying', async () => {
    const store = useConsoleStore()
    store.currentId = 42

    await store.reply('   ', [])

    expect(httpMock.post).not.toHaveBeenCalled()
    expect(httpMock.get).not.toHaveBeenCalled()
    expect(store.replying).toBe(false)
  })

  it('只有附件（纯发图）也允许提交，附件数组原样带上', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    httpMock.post.mockResolvedValue({ message_id: 2 })
    httpMock.get.mockResolvedValue([])
    const attachments: AttachmentItem[] = [{ url: SIGNED_FILE, name: 'report.pdf', type: 'file' }]

    await store.reply('', attachments)

    expect(httpMock.post).toHaveBeenCalledWith('/console/conversations/42/reply', { text: '', attachments })
  })

  it('未选中会话时即使有正文也不发（避免打到 /conversations/null/reply）', async () => {
    const store = useConsoleStore()
    store.currentId = null

    await store.reply('你好', [])

    expect(httpMock.post).not.toHaveBeenCalled()
  })

  it('发送中 replying 为 true、结束后复位（按钮 loading 的唯一依据）', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    httpMock.get.mockResolvedValue([])
    let releasePost: (value: { message_id: number }) => void = () => { /* 占位，下面立即赋值 */ }
    httpMock.post.mockImplementationOnce(
      () => new Promise<{ message_id: number }>((resolve) => { releasePost = resolve }),
    )

    const pending = store.reply('发送中', [])
    expect(store.replying).toBe(true)

    releasePost({ message_id: 1 })
    await pending
    expect(store.replying).toBe(false)
  })

  it('接口失败时 replying 必须复位（否则发送按钮永久 loading）', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    httpMock.post.mockRejectedValue(new Error('请求失败（500）'))

    await expect(store.reply('会失败的文案', [])).rejects.toThrow('请求失败（500）')

    expect(store.replying).toBe(false)
    // 失败后不再重拉消息
    expect(httpMock.get).not.toHaveBeenCalled()
  })

  it('正文按原样提交（守卫只看 trim 结果，不裁剪发送内容）', async () => {
    const store = useConsoleStore()
    store.currentId = 42
    httpMock.post.mockResolvedValue({ message_id: 3 })
    httpMock.get.mockResolvedValue([])

    await store.reply('  带空格的回复  ', [])

    expect(httpMock.post.mock.calls[0][1]).toMatchObject({ text: '  带空格的回复  ' })
  })
})
