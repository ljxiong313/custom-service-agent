// 运营台状态（P8）：全渠道买家会话列表 + 消息轮询 + 人工代答 + 媒体上传。
// 语义：role=user 即「客」（买家），role=assistant 即「我」（Agent 自动回复或运营代答，
// 买家不可感知差异；meta.from 仅运营侧可见）。
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '@/apis/http'

export interface AttachmentItem { url: string; name: string; type: string }
export interface ToolEvent { name: string; status: string; args?: Record<string, unknown>; output?: string }
export interface CitationItem { index: number; file_name: string; snippet: string }

export interface OpsConversation {
  id: number
  title: string
  username: string
  channel: string          // taobao / jd / pdd / douyin / web
  shop_id: string
  buyer_id: string
  handoff_status: string   // bot / awaiting_confirm / pending_human / human
  status: string           // active / closed（P8 终结）
  updated_at: string
  last_message: string
  last_role: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at?: string
  // 由 meta 反解析的运营侧展示字段
  tools?: ToolEvent[]
  citations?: CitationItem[]
  attachments?: AttachmentItem[]
  from?: string  // human_agent / human_operator / system_handoff / system_handoff_ask / system_idle_prompt / system_session_closed
  staff?: string
}

function parseMessage(raw: { id: number; role: string; content: string; meta: Record<string, unknown> | null; created_at?: string }): ChatMessage {
  return {
    id: raw.id,
    role: raw.role as 'user' | 'assistant',
    content: raw.content,
    created_at: raw.created_at,
    tools: raw.meta?.tools as ToolEvent[] | undefined,
    citations: raw.meta?.citations as CitationItem[] | undefined,
    attachments: raw.meta?.attachments as AttachmentItem[] | undefined,
    from: raw.meta?.from as string | undefined,
    staff: raw.meta?.staff as string | undefined,
  }
}

export const useConsoleStore = defineStore('console', () => {
  const list = ref<OpsConversation[]>([])
  const currentId = ref<number | null>(null)
  const messages = ref<ChatMessage[]>([])
  const replying = ref(false)

  async function fetchList() {
    list.value = await http.get<OpsConversation[]>('/console/conversations')
  }

  const current = () => list.value.find((c) => c.id === currentId.value) ?? null

  async function select(id: number) {
    currentId.value = id
    const rows = await http.get<Parameters<typeof parseMessage>[0][]>(`/console/conversations/${id}/messages`)
    messages.value = rows.map(parseMessage)
  }

  /** 轮询刷新当前会话消息：内容无变化时不重新赋值，避免列表重渲染打断滚动。 */
  async function refreshMessages() {
    if (!currentId.value) return
    const rows = await http.get<Parameters<typeof parseMessage>[0][]>(`/console/conversations/${currentId.value}/messages`)
    const next = rows.map(parseMessage)
    if (next.length !== messages.value.length ||
        next[next.length - 1]?.content !== messages.value[messages.value.length - 1]?.content) {
      messages.value = next
    }
  }

  async function uploadMedia(file: File): Promise<AttachmentItem> {
    const fd = new FormData()
    fd.append('file', file)
    return http.upload<AttachmentItem>('/console/media', fd)
  }

  async function reply(text: string, attachments: AttachmentItem[]) {
    if (!currentId.value || (!text.trim() && !attachments.length)) return
    replying.value = true
    try {
      await http.post<{ message_id: number }>(
        `/console/conversations/${currentId.value}/reply`, { text, attachments })
      // 重新拉取（非乐观插入）：来源徽标/附件 meta 以服务端为准，轮询对比也据此感知变化
      await select(currentId.value)
      await fetchList()
    } finally {
      replying.value = false
    }
  }

  return { list, currentId, messages, replying, fetchList, select, refreshMessages, uploadMedia, reply, current }
})
