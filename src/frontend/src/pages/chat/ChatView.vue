<template>
  <!-- 运营台（P8）：全渠道「客↔我」会话视图 + 人工代答输入框 -->
  <aside ref="sideRef" class="conv-side" :style="{ width: sideWidth + 'px' }">
    <div class="side-head">
      <b>买家会话</b>
      <el-button size="small" text :loading="refreshing" @click="poll(true)">刷新</el-button>
    </div>
    <div class="conv-items">
      <div
        v-for="c in store.list" :key="c.id" class="conv-item" :class="{ active: c.id === store.currentId }"
        @click="onSelect(c.id)"
      >
        <div class="line1">
          <el-tag size="small" :type="channelType(c.channel)" disable-transitions>{{ channelName(c.channel) }}</el-tag>
          <span class="title">{{ c.title }}</span>
          <span class="dot" :class="dotClass(c)" />
        </div>
        <div class="line2">
          <span class="preview">{{ c.last_role === 'user' ? '客：' : '我：' }}{{ c.last_message || '—' }}</span>
          <span v-if="c.status === 'closed'" class="closed-mark">已结束</span>
          <span class="time">{{ fmtShort(c.updated_at) }}</span>
        </div>
      </div>
      <el-empty v-if="!store.list.length" description="暂无买家会话（平台消息或网页咨询会自动建档）" :image-size="60" />
    </div>
  </aside>

  <!-- 会话列宽拖拽条：指针拖动/方向键改宽度，双击或 Home 恢复默认 -->
  <div
    class="side-resizer" :class="{ dragging }" title="拖动或用 ←/→ 调整会话列表宽度，双击恢复默认"
    role="separator" aria-orientation="vertical" aria-label="调整会话列表宽度：左右方向键，Shift 加速，Home 恢复默认"
    :aria-valuenow="sideWidth" :aria-valuemin="SIDE_MIN" :aria-valuemax="SIDE_MAX" tabindex="0"
    @pointerdown="startDrag" @pointermove="onDrag" @pointerup="endDrag" @pointercancel="endDrag"
    @keydown="onResizeKeys" @dblclick="resetWidth"
  />

  <section class="chat-area">
    <template v-if="store.currentId">
      <el-alert v-if="banner" class="status-banner" :type="banner.type" :closable="false" :title="banner.text" />
      <MessageList :messages="store.messages" />
      <div class="input-bar" :class="{ disabled: isClosed }">
        <div v-if="pending.length" class="attach-row">
          <el-tag
            v-for="(a, i) in pending" :key="i" closable size="small" type="info"
            @close="pending.splice(i, 1)"
          >
            {{ a.type === 'image' ? '图片' : '文件' }}：{{ a.name }}
          </el-tag>
        </div>
        <div class="input-line">
          <el-button :disabled="!canSend" plain aria-label="选择图片上传" @click="pickFile('image/*')">图片</el-button>
          <el-button :disabled="!canSend" plain aria-label="选择文件上传" @click="pickFile('')">文件</el-button>
          <el-input
            v-model="draft" type="textarea" :autosize="{ minRows: 1, maxRows: 5 }"
            aria-label="以客服身份回复买家"
            :placeholder="isClosed ? '会话已结束，无法回复' : '以客服身份回复买家，Enter 发送 / Shift+Enter 换行；支持直接粘贴截图'"
            resize="none" :disabled="!canSend"
            @keydown.enter.exact.prevent="send" @paste="onPaste"
          />
          <el-button type="primary" aria-label="发送回复给买家"
                     :loading="store.replying" :disabled="!canSend || (!draft.trim() && !pending.length)" @click="send">
            发送
          </el-button>
        </div>
      </div>
      <input ref="fileRef" type="file" hidden tabindex="-1" aria-hidden="true" @change="onFileChosen" />
    </template>
    <el-empty v-else description="选择一个买家会话查看完整对话" class="placeholder" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MessageList from '@/components/MessageList.vue'
import { useConsoleStore, type AttachmentItem } from '@/stores/console'
import { fmtShort } from '@/utils/time'

const store = useConsoleStore()
const route = useRoute()
const router = useRouter()
const draft = ref('')
const pending = ref<AttachmentItem[]>([])
const fileRef = ref<HTMLInputElement>()
const refreshing = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

// 会话列表宽度：可拖拽，localStorage 记忆；MIN/MAX 防止挤没聊天区或超出窄屏
const SIDE_DEFAULT = 262
const SIDE_MIN = 180
const SIDE_MAX = 480
const sideRef = ref<HTMLElement>()
const sideWidth = ref(Number(localStorage.getItem('cs_side_w')) || SIDE_DEFAULT)
const dragging = ref(false)
let dragLeft = 0 // 拖拽开始时列表的视口左边缘，用于把 clientX 换算成宽度

function startDrag(e: PointerEvent) {
  e.preventDefault() // 阻止拖动过程中选中会话文字
  dragging.value = true
  document.body.style.userSelect = 'none'
  dragLeft = sideRef.value?.getBoundingClientRect().left ?? 0
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
}
function onDrag(e: PointerEvent) {
  if (!dragging.value) return
  sideWidth.value = clampWidth(e.clientX - dragLeft)
}
/** 窄屏下按视口 45% 收敛，避免聊天区被挤没。 */
function clampWidth(w: number) {
  const max = Math.min(SIDE_MAX, Math.floor(window.innerWidth * 0.45))
  return Math.min(max, Math.max(SIDE_MIN, Math.round(w)))
}
/** role=separator + tabindex=0 只有配合方向键才算真的可键盘操作：←/→ 微调，Shift 加速，Home 回默认。 */
function onResizeKeys(e: KeyboardEvent) {
  const step = e.shiftKey ? 40 : 12
  if (e.key === 'ArrowLeft') sideWidth.value = clampWidth(sideWidth.value - step)
  else if (e.key === 'ArrowRight') sideWidth.value = clampWidth(sideWidth.value + step)
  else if (e.key === 'Home') sideWidth.value = SIDE_DEFAULT
  else return
  e.preventDefault()
  localStorage.setItem('cs_side_w', String(sideWidth.value))
}
function endDrag(e: PointerEvent) {
  if (!dragging.value) return
  dragging.value = false
  document.body.style.userSelect = ''
  const el = e.currentTarget as HTMLElement
  if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
  localStorage.setItem('cs_side_w', String(sideWidth.value))
}
function resetWidth() {
  sideWidth.value = SIDE_DEFAULT
  localStorage.setItem('cs_side_w', String(SIDE_DEFAULT))
}

const currentConv = computed(() => store.current())
const isClosed = computed(() => currentConv.value?.status === 'closed')
const canSend = computed(() => !!currentConv.value && !isClosed.value && !store.replying)

const CHANNELS: Record<string, [string, 'warning' | 'success' | 'primary' | 'info' | 'danger']> = {
  taobao: ['淘宝', 'danger'], jd: ['京东', 'danger'], pdd: ['拼多多', 'primary'],
  douyin: ['抖店', 'info'], web: ['网页', 'success'],
}
function channelName(ch: string) { return CHANNELS[ch]?.[0] ?? ch }
function channelType(ch: string) { return CHANNELS[ch]?.[1] ?? 'info' }
function dotClass(c: { status: string; handoff_status: string }) {
  if (c.status === 'closed') return 'closed'
  return c.handoff_status === 'pending_human' ? 'queued'
    : c.handoff_status === 'human' ? 'serving'
      : c.handoff_status === 'awaiting_confirm' ? 'confirming' : 'bot'
}

const banner = computed(() => {
  const c = currentConv.value
  if (!c) return null
  if (c.status === 'closed') return { type: 'info' as const, text: '该会话已超时结束（未结工单已自动关闭），买家重新咨询会新建会话' }
  if (c.handoff_status === 'awaiting_confirm') return { type: 'warning' as const, text: 'Agent 正在等待买家确认是否转人工；买家回复「是」后自动建单并推送企业微信' }
  if (c.handoff_status === 'pending_human') return { type: 'warning' as const, text: '买家正在排队等待人工；您在本页的回复会以客服身份直接送达买家（买家无法区分人工/机器人）' }
  if (c.handoff_status === 'human') return { type: 'success' as const, text: '人工服务中；工单在「工作台」结单后恢复机器人应答' }
  return null
})

async function onSelect(id: number) {
  await store.select(id)
  router.push(`/chat/${id}`)
}

async function poll(manual = false) {
  refreshing.value = manual
  try {
    await store.fetchList()
    await store.refreshMessages()
    // 当前会话在列表刷新后可能状态变化，无需额外处理：banner/输入态均由列表驱动
  } catch { /* 后端瞬断下轮重试，不打扰运营 */ } finally { refreshing.value = false }
}

onMounted(async () => {
  await store.fetchList()
  const id = Number(route.params.id)
  if (id && store.list.some((c) => c.id === id)) await store.select(id)
  else if (store.list.length) await store.select(store.list[0].id)
  timer = setInterval(() => poll(false), 4000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })

async function uploadOne(file: File) {
  const att = await store.uploadMedia(file)
  pending.value.push(att)
}

function pickFile(accept: string) {
  if (!fileRef.value) return
  fileRef.value.accept = accept
  fileRef.value.value = ''
  fileRef.value.click()
}
async function onFileChosen(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (file) { try { await uploadOne(file) } catch { /* http 层已弹错 */ } }
}

// 粘贴截图直接上传（需求4：允许粘贴图片）
async function onPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items ?? []).filter((i) => i.kind === 'file')
  if (!items.length) return
  e.preventDefault()
  for (const item of items) {
    const file = item.getAsFile()
    if (file) { try { await uploadOne(file) } catch { /* http 层已弹错 */ } }
  }
}

async function send() {
  const text = draft.value.trim()
  if (!text && !pending.value.length) return
  const atts = pending.value.slice()
  draft.value = ''
  pending.value = []
  try { await store.reply(text, atts) } catch { draft.value = text; pending.value = atts }
}
</script>

<style scoped>
.conv-side {
  flex-shrink: 0; display: flex; flex-direction: column;
  background: var(--ac-surface); border-right: 1px solid var(--ac-border);
}
.side-resizer {
  width: 5px; flex-shrink: 0; cursor: col-resize; background: transparent;
}
.side-resizer:hover, .side-resizer.dragging { background: var(--ac-brand); opacity: .35; }
.side-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 14px; border-bottom: 1px solid var(--ac-border); font-size: 14px; }
.conv-items { flex: 1; overflow-y: auto; padding: 6px; }
.conv-item { padding: 10px 10px 8px; border-radius: 10px; cursor: pointer; }
.conv-item:hover { background: var(--ac-bg); }
.conv-item.active { background: var(--ac-brand-light); }
.line1 { display: flex; align-items: center; gap: 6px; }
.title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
/* 状态点颜色统一取主题/Element 状态色，避免页面里散落十六进制 */
.dot.bot { background: var(--ac-dot-idle); } .dot.confirming { background: var(--el-color-warning); }
.dot.queued { background: var(--el-color-danger); } .dot.serving { background: var(--el-color-success); }
.dot.closed { background: var(--ac-text-secondary); }
.line2 { display: flex; justify-content: space-between; margin-top: 4px; font-size: 12px; color: var(--ac-text-secondary); }
.time { flex-shrink: 0; margin-left: 8px; }
.preview { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.closed-mark { flex-shrink: 0; margin-left: 8px; font-size: 11px; color: var(--ac-text-secondary); }
.chat-area { flex: 1; min-width: 0; display: flex; flex-direction: column; background: var(--ac-bg); }
.placeholder { margin: auto; }
.status-banner { border-radius: 0; }
.input-bar { background: var(--ac-surface); border-top: 1px solid var(--ac-border); padding: 10px 24px 14px; }
.attach-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.input-line { display: flex; gap: 10px; align-items: flex-end; }
.input-line :deep(.el-textarea__inner) { border-radius: 10px; }
</style>
