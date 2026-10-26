<template>
  <!-- P3 转人工工作台：工单列表/接单回复/结单，mock 模式下展示企微外发记录 -->
  <div class="page">
    <h2 class="page-title">转人工工作台</h2>

    <div class="stats">
      <div class="stat">
        <span class="stat-num warn">{{ stats.open }}</span>
        <span class="stat-label">待接单</span>
      </div>
      <div class="stat">
        <span class="stat-num ok">{{ stats.handling }}</span>
        <span class="stat-label">处理中</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ stats.created_today }}</span>
        <span class="stat-label">今日新建</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ avgFirstText }}</span>
        <span class="stat-label">平均首响（{{ stats.responded }} 单已响应）</span>
      </div>
    </div>

    <el-tabs v-model="tab" class="page-tabs" @tab-change="onTabChange">
      <el-tab-pane label="工单" name="tickets">
        <div class="toolbar">
          <el-radio-group v-model="statusFilter" size="small" @change="loadTickets">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="open">待接单</el-radio-button>
            <el-radio-button value="handling">处理中</el-radio-button>
            <el-radio-button value="closed">已结单</el-radio-button>
          </el-radio-group>
          <el-button size="small" @click="loadTickets">刷新</el-button>
          <el-button size="small" type="primary" @click="openCreate">手动建单</el-button>
        </div>
        <el-table :data="tickets" v-loading="loading" highlight-current-row @current-change="pick">
          <el-table-column prop="id" label="工单" width="70" />
          <el-table-column prop="conversation_id" label="会话" width="70" />
          <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
          <el-table-column prop="summary" label="摘要" min-width="180" show-overflow-tooltip />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'open' ? 'warning' : row.status === 'handling' ? 'success' : 'info'">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="staff" label="客服" width="100" />
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
          </el-table-column>
          <template #empty>
            <el-empty :description="statusFilter ? `「${statusLabel(statusFilter)}」下暂无工单` : '暂无转人工工单'"
                      :image-size="60" />
          </template>
        </el-table>

        <div v-if="current" class="reply-panel">
          <h3>工单 #{{ current.id }} 处理</h3>
          <p class="sum-line"><b>会话摘要：</b>{{ current.summary || '（无）' }}</p>
          <el-input v-model="replyText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }"
                    placeholder="以人工客服身份回复用户…" />
          <div class="reply-actions">
            <el-button type="primary" :loading="acting" :disabled="!replyText.trim()"
                       @click="reply">发送回复</el-button>
            <el-button v-if="current.status !== 'closed'" type="success" :loading="acting"
                       @click="close">结单并恢复机器人</el-button>
          </div>
        </div>
        <el-empty v-else description="选择上方工单进行回复或结单" />
      </el-tab-pane>

      <el-tab-pane label="企微外发记录" name="outbox">
        <p class="hint">mock 模式（未配置真实企微应用）下，转人工提醒卡片记录在此，用于演示通知送达。</p>
        <div v-for="(o, i) in outbox" :key="i" class="outbox-item">
          <div class="outbox-head">
            <el-tag size="small" type="danger">textcard</el-tag>
            <span>touser: {{ o.touser }} · agentid: {{ o.agentid }}</span>
          </div>
          <div class="outbox-title">{{ o.textcard.title }}</div>
          <pre class="outbox-desc">{{ o.textcard.description }}</pre>
        </div>
        <el-empty v-if="!outbox.length" description="暂无外发记录" />
      </el-tab-pane>
    </el-tabs>

    <!-- 手动建单：买家没点确认转人工、但运营判断需要人工介入时的开单入口 -->
    <el-dialog v-model="createOpen" title="手动建单（转人工）" width="520px">
      <el-form label-width="72px">
        <el-form-item label="会话">
          <el-select v-model="createConv" filterable placeholder="选择全渠道会话" class="full">
            <el-option v-for="cv in conversations" :key="cv.id" :value="cv.id"
                       :label="`#${cv.id} [${cv.channel}] ${cv.username} · ${cv.title || '（无标题）'}`"
                       :disabled="cv.status === 'closed'" />
          </el-select>
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="createReason" type="textarea" :rows="2" maxlength="200"
                    show-word-limit placeholder="如：买家投诉升级，运营判断需人工跟进" />
        </el-form-item>
      </el-form>
      <p class="hint">已终结的会话不可选；同一会话若已有未结工单则复用，不会重复推送企微卡片。</p>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :loading="creating" :disabled="!createConv || !createReason.trim()"
                   @click="submitCreate">建单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/apis/http'
import { fmtDateTime } from '@/utils/time'

interface Ticket {
  id: number; conversation_id: number; user_id: number; reason: string
  summary: string; status: string; staff: string; created_at: string; closed_at: string | null
}
interface OutboxItem {
  mock: boolean; touser: string; msgtype: string; agentid: number
  textcard: { title: string; description: string; url: string; btntxt: string }
}
interface HandoffStats {
  open: number; handling: number; closed: number
  created_today: number; responded: number; avg_first_response_seconds: number | null
}
interface ConsoleConv {
  id: number; title: string; username: string; channel: string
  status: string; handoff_status: string; buyer_id: string
}

const tab = ref('tickets')
const tickets = ref<Ticket[]>([])
const loading = ref(false)
const statusFilter = ref('')
const current = ref<Ticket | null>(null)
const replyText = ref('')
const acting = ref(false)
const outbox = ref<OutboxItem[]>([])
const stats = reactive<HandoffStats>({
  open: 0, handling: 0, closed: 0, created_today: 0, responded: 0,
  avg_first_response_seconds: null,
})
const createOpen = ref(false)
const createConv = ref<number | null>(null)
const createReason = ref('运营判断需人工介入')
const conversations = ref<ConsoleConv[]>([])
const creating = ref(false)

const avgFirstText = computed(() => {
  const s = stats.avg_first_response_seconds
  if (s === null || s === undefined) return '—'
  if (s < 60) return `${s.toFixed(0)} 秒`
  if (s < 3600) return `${Math.round(s / 60)} 分钟`
  return `${(s / 3600).toFixed(1)} 小时`
})

function statusLabel(s: string) {
  return s === 'open' ? '待接单' : s === 'handling' ? '处理中' : '已结单'
}

async function loadStats() {
  Object.assign(stats, await http.get<HandoffStats>('/handoff/stats'))
}

async function loadTickets() {
  loading.value = true
  try {
    tickets.value = await http.get<Ticket[]>(
      `/handoff/tickets${statusFilter.value ? `?status_filter=${statusFilter.value}` : ''}`)
    await loadStats()
  } finally { loading.value = false }
}

function pick(row: Ticket | null) { current.value = row }

async function openCreate() {
  createOpen.value = true
  createConv.value = null
  conversations.value = await http.get<ConsoleConv[]>('/console/conversations?limit=100')
}

async function submitCreate() {
  if (!createConv.value) return
  creating.value = true
  try {
    const r = await http.post<{ ticket_id: number; status: string }>('/handoff/tickets', {
      conversation_id: createConv.value, reason: createReason.value.trim(),
    })
    ElMessage.success(`已建单 #${r.ticket_id}，会话转入人工排队`)
    createOpen.value = false
    await loadTickets()
  } finally { creating.value = false }
}

async function reply() {
  if (!current.value) return
  acting.value = true
  try {
    await http.post(`/handoff/tickets/${current.value.id}/reply`, { text: replyText.value.trim() })
    replyText.value = ''
    ElMessage.success('回复已送达用户会话')
    await loadTickets()
  } finally { acting.value = false }
}

async function close() {
  if (!current.value) return
  acting.value = true
  try {
    await http.post(`/handoff/tickets/${current.value.id}/close`)
    ElMessage.success('已结单，会话恢复机器人应答')
    current.value = null
    await loadTickets()
  } finally { acting.value = false }
}

async function loadOutbox() {
  outbox.value = await http.get<OutboxItem[]>('/handoff/outbox')
}

function onTabChange(name: string) { if (name === 'outbox') loadOutbox() }

onMounted(loadTickets)
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.page-title { margin: 0 0 12px; font-size: 18px; }
.stats { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.stat {
  display: flex; flex-direction: column; gap: 2px; min-width: 130px;
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 10px 14px;
}
.stat-num { font-size: 20px; font-weight: 600; }
.stat-num.warn { color: var(--el-color-warning); }
.stat-num.ok { color: var(--el-color-success); }
.stat-label { font-size: 12px; color: var(--ac-text-secondary); }
.full { width: 100%; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.hint { font-size: 12px; color: var(--ac-text-secondary); }
.reply-panel {
  margin-top: 16px; padding: 16px; background: var(--ac-surface);
  border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
}
.reply-panel h3 { margin: 0 0 8px; font-size: 15px; }
.sum-line { margin: 0 0 10px; font-size: 13px; color: var(--ac-text-secondary); }
.reply-actions { display: flex; gap: 8px; margin-top: 10px; justify-content: flex-end; }
.outbox-item {
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 12px 14px; margin-bottom: 10px;
}
.outbox-head { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--ac-text-secondary); }
.outbox-title { font-weight: 600; margin: 6px 0 4px; }
.outbox-desc { margin: 0; font-size: 12px; white-space: pre-wrap; color: var(--ac-text-secondary); }
</style>
