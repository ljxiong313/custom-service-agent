<template>
  <!-- P5 设置：账号信息与运行环境状态总览 -->
  <div class="page">
    <h2 class="page-title">设置</h2>

    <section class="card">
      <h3>账号</h3>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="用户名">{{ userStore.user?.username ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="邮箱">{{ userStore.user?.email ?? '未绑定' }}</el-descriptions-item>
        <el-descriptions-item label="角色">
          <el-tag size="small" :type="userStore.isAdmin ? 'success' : 'info'">
            {{ userStore.isAdmin ? '管理员' : '普通成员' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <el-button class="logout-btn" @click="logout">退出登录</el-button>
    </section>

    <!-- 仅管理员可见：角色是知识库重建/MCP 配置等破坏性操作的门禁 -->
    <section v-if="userStore.isAdmin" class="card">
      <h3>成员与角色</h3>
      <el-table :data="members" size="small" v-loading="loadingMembers" empty-text="暂无成员">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column label="邮箱" min-width="140">
          <template #default="{ row }">{{ row.email || '未绑定' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="170">
          <template #default="{ row }">
            <el-tooltip v-if="isSelf(row)" content="不能降级自己，否则没人能再改角色" placement="top">
              <el-tag size="small" type="success">管理员（本人）</el-tag>
            </el-tooltip>
            <el-select v-else :model-value="row.role" size="small"
                       :disabled="roleSavingId === row.id" aria-label="切换成员角色"
                       @change="(v: string) => setRole(row, v)">
              <el-option label="普通成员" value="user" />
              <el-option label="管理员" value="admin" />
            </el-select>
          </template>
        </el-table-column>
      </el-table>
      <p class="hint">角色只影响控制台的写操作门禁（重新入库/删除/MCP 配置等），后端同步做 403 校验。</p>
    </section>

    <section class="card">
      <h3>服务状态</h3>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="模型 api_key">
          <el-tag size="small" :type="status.model_ready ? 'success' : 'warning'">
            {{ status.model_ready ? '已配置（实际可用性以模型调用结果为准）' : '未配置（对话与图片解析不可用，检索降级 BM25）' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="企微通道">
          <el-tag size="small" :type="status.wecom_mock ? 'warning' : 'success'">
            {{ status.wecom_mock ? 'mock（未配置真实企微应用）' : '真实自建应用' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="知识库任务">
          <el-tag size="small" :type="jobTag">
            {{ status.ingest_jobs.queued_running }} 进行中 / {{ status.ingest_jobs.done }} 完成
            / {{ status.ingest_jobs.failed }} 失败 / {{ status.ingest_jobs.interrupted }} 中断
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="外发箱">
          <el-tag size="small" :type="outboxTag">
            {{ status.outbox.total }} 条：sent {{ status.outbox.sent }}、mocked {{ status.outbox.mocked }}、
            skipped {{ status.outbox.skipped }}、failed {{ status.outbox.failed }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="待确认工具">
          <el-tag size="small" :type="status.mcp_pending_confirm ? 'warning' : 'info'">
            {{ status.mcp_pending_confirm }} 个调用正在等待确认（超过 180 秒自动按拒绝处理）
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <p class="hint">
        本页只读展示模型配置状态：后端没有写配置的接口，<b>改模型需编辑 src/backend/config.local.yaml
        并重启后端服务</b>（该文件不入库）；未配置有效 api_key 时对话与图片解析会提示模型未配置，
        检索自动降级为 BM25。
      </p>
    </section>

    <section class="card">
      <h3>电商平台渠道（P7）</h3>
      <el-table :data="platforms" size="small" empty-text="未配置平台">
        <el-table-column prop="platform" label="平台" width="90" />
        <el-table-column label="模式" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.mock ? 'warning' : 'success'">
              {{ row.mock ? 'mock 演示' : '真实对接' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="凭证">
          <template #default="{ row }">
            <el-tag size="small" :type="row.has_credentials ? 'success' : 'info'">
              {{ row.has_credentials ? '已填 app_key' : '未填（见 docs/平台接入.md）' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="conversations" label="买家会话数" width="90" align="right" />
      </el-table>
      <p class="hint">
        买家消息经 POST /api/platform/{platform}/callback 进入同一 Agent 管道；
        mock 模式下自动回复与客服回复记录在外发箱表 `outboxrecord`（落库，重启不丢，仅供演示不代表已送达买家）。
      </p>
    </section>

    <!-- 就绪自检：把「为什么买家收不到回复」这类配置缺口静态列清楚，全程只显示是否已配，不回显密钥值 -->
    <section class="card wide">
      <h3>平台就绪自检</h3>
      <el-empty v-if="!readiness.length" description="后端未提供 /platforms/readiness，或读取失败" :image-size="60" />
      <template v-else>
        <div class="ready-summary">
          <el-tag size="small" type="info">平台 {{ readSummary?.total ?? 0 }}</el-tag>
          <el-tag size="small" :type="badgeType(readSummary?.ready_in, readSummary?.total)">
            入站就绪 {{ readSummary?.ready_in ?? 0 }}/{{ readSummary?.total ?? 0 }}
          </el-tag>
          <el-tag size="small" :type="badgeType(readSummary?.ready_out, readSummary?.total)">
            出站就绪 {{ readSummary?.ready_out ?? 0 }}/{{ readSummary?.total ?? 0 }}
          </el-tag>
          <el-tag size="small" :type="badgeType(readSummary?.ready_media, readSummary?.total)">
            媒体就绪 {{ readSummary?.ready_media ?? 0 }}/{{ readSummary?.total ?? 0 }}
          </el-tag>
          <el-tag size="small" :type="readSummary?.all_mock ? 'warning' : 'success'">
            {{ readSummary?.all_mock ? '当前全部为 mock 演示' : '存在真实外呼通道' }}
          </el-tag>
          <el-tag v-if="readSummary?.risks" size="small" type="danger">风险提示 {{ readSummary.risks }}</el-tag>
        </div>
        <p class="hint">{{ readSummary?.hint }}</p>
        <div v-for="p in readiness" :key="p.platform" class="ready-row" :class="{ 'is-risk': hasHardRisk(p) }">
          <div class="ready-head">
            <b class="ready-name">{{ p.platform }}</b>
            <el-tag size="small" :type="readyTag(p, p.ready_in).type">
              入站 {{ readyTag(p, p.ready_in).text }}
            </el-tag>
            <el-tag size="small" :type="readyTag(p, p.ready_out).type">
              出站 {{ readyTag(p, p.ready_out).text }}
            </el-tag>
            <el-tag size="small" :type="readyTag(p, p.ready_media, 'warning').type">
              媒体 {{ readyTag(p, p.ready_media, 'warning').text }}
            </el-tag>
            <el-tag size="small" :type="p.enabled ? 'success' : 'info'">{{ p.enabled ? '已启用' : '未启用' }}</el-tag>
            <el-tag size="small" :type="p.mock ? 'warning' : 'success'">{{ p.mock ? 'mock' : '真实' }}</el-tag>
            <el-tag size="small" :type="p.callback_verify === 'none' ? 'danger' : 'info'">
              验签 {{ p.callback_verify || '（未设）' }}
            </el-tag>
          </div>
          <div class="missing-row">
            <span class="missing-label">缺失配置项</span>
            <template v-if="missingKeys(p).length">
              <el-tag v-for="k in missingKeys(p)" :key="k" size="small" type="info" class="miss-tag">{{ k }}</el-tag>
            </template>
            <span v-else class="missing-none">无</span>
          </div>
          <ul v-if="p.risks.length" class="risk-list">
            <li v-for="(r, i) in p.risks" :key="i" :class="{ 'risk-text': r.level === 'risk' }">
              <b>{{ riskLabel(r.level) }}（{{ r.key }}）：</b>{{ r.message }}
            </li>
          </ul>
          <el-collapse class="items-collapse">
            <el-collapse-item :title="`配置项明细（${p.items.length} 项）`" :name="p.platform">
              <el-table :data="p.items" size="small">
                <el-table-column prop="key" label="配置键" width="130" />
                <el-table-column prop="label" label="作用" min-width="200" show-overflow-tooltip />
                <el-table-column label="链路" width="70">
                  <template #default="{ row }">{{ CHAIN_LABELS[row.chain as string] ?? row.chain }}</template>
                </el-table-column>
                <el-table-column label="必需" width="60">
                  <template #default="{ row }">{{ row.required ? '是' : '否' }}</template>
                </el-table-column>
                <el-table-column label="已配置" width="80">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.present ? 'success' : 'info'">
                      {{ row.present ? '是' : '否' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="note" label="备注" min-width="140" show-overflow-tooltip />
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </div>
      </template>
    </section>

    <section class="card wide">
      <h3>平台外发箱（mock 送达回执）</h3>
      <div class="outbox-tabs">
        <el-radio-group v-model="outboxPlatform" size="small">
          <el-radio-button v-for="name in outboxKeys" :key="name" :value="name">
            {{ name }} ({{ outbox[name]?.length ?? 0 }})
          </el-radio-button>
        </el-radio-group>
        <el-button size="small" :loading="loadingOutbox" @click="loadOutbox">刷新</el-button>
      </div>
      <p v-if="!outboxKeys.length || !(outbox[outboxPlatform] || []).length" class="hint">
        外发箱为空：mock 通道发出的每一条买家可见回复都会记录在这里（落库，重启不丢）。
      </p>
      <div v-for="(o, i) in outbox[outboxPlatform] || []" :key="i" class="outbox-item">
        <div class="outbox-head">
          <el-tag size="small" type="warning">mock</el-tag>
          <span>to: {{ o.to }} · api: {{ o.api || '（未配置 reply_method）' }}</span>
        </div>
        <div class="outbox-text">{{ o.text }}</div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore, type UserRole } from '@/stores/user'
import { http } from '@/apis/http'

interface PlatformStatus {
  platform: string
  enabled: boolean
  mock: boolean
  has_credentials: boolean
  gateway: string
  conversations: number
}
interface MockRecord { platform: string; to: string; text: string; api: string; mock: boolean }
interface ReadinessItem {
  key: string; path: string; label: string; chain: string;
  required: boolean; present: boolean; secret: boolean; note: string
}
interface ReadinessRisk { key: string; level: string; message: string }
interface PlatformReadiness {
  platform: string; enabled: boolean; mock: boolean; callback_verify: string; gateway: string;
  ready_in: boolean; ready_out: boolean; ready_media: boolean;
  missing_in: string[]; missing_out: string[]; missing_media: string[];
  items: ReadinessItem[]; risks: ReadinessRisk[]
}
interface ReadinessSummary {
  total: number; ready_in: number; ready_out: number; ready_media: number;
  all_mock: boolean; risks: number; missing_keys: string[];
  outbox_counts: Record<string, number>; hint: string
}
interface ReadinessResp { platforms: PlatformReadiness[]; summary: ReadinessSummary }
interface MemberRow { id: number; username: string; email: string | null; role: UserRole }

const CHAIN_LABELS: Record<string, string> = {
  in: '入站', out: '出站', media: '媒体', sign: '验签',
}

const userStore = useUserStore()
const router = useRouter()
const status = reactive({
  model_ready: false,
  wecom_mock: true,
  ingest_jobs: { queued_running: 0, done: 0, failed: 0, interrupted: 0 },
  outbox: { total: 0, mocked: 0, sent: 0, skipped: 0, failed: 0 } as
    { total: number; mocked: number; sent: number; skipped: number; failed: number; by_channel?: Record<string, number> },
  mcp_pending_confirm: 0,
})
// 队列里剩 interrupted 就说明上次重启打断了重建；外发 skipped 说明"以为发出去了其实没发"
const jobTag = computed(() => (status.ingest_jobs.failed || status.ingest_jobs.interrupted ? 'warning'
  : status.ingest_jobs.queued_running ? 'primary' : 'success'))
const outboxTag = computed(() => (status.outbox.failed ? 'danger'
  : status.outbox.skipped ? 'warning' : 'success'))
const platforms = ref<PlatformStatus[]>([])
const readiness = ref<PlatformReadiness[]>([])
const readSummary = ref<ReadinessSummary | null>(null)
const members = ref<MemberRow[]>([])
const loadingMembers = ref(false)
const roleSavingId = ref<number | null>(null)
const outbox = reactive<Record<string, MockRecord[]>>({})
const outboxPlatform = ref('')
const loadingOutbox = ref(false)
const outboxKeys = computed(() => Object.keys(outbox))

function badgeType(ready?: number, total?: number) {
  if (!total) return 'info'
  return (ready ?? 0) >= total ? 'success' : (ready ?? 0) === 0 ? 'danger' : 'warning'
}
function missingKeys(p: PlatformReadiness) {
  return Array.from(new Set([...p.missing_in, ...p.missing_out, ...p.missing_media]))
}
/**
 * mock=true 时后端把出站/媒体也算「就绪」（链路能跑通，只是不外呼），
 * 徽标不带 mock 字样会被读成「已经可以真外呼」，所以降级成 warning 并加后缀。
 */
function readyTag(p: PlatformReadiness, ok: boolean, off: 'danger' | 'warning' = 'danger'):
  { type: 'success' | 'warning' | 'danger'; text: string } {
  return {
    type: ok ? (p.mock ? 'warning' : 'success') : off,
    text: ok ? (p.mock ? '就绪（mock）' : '就绪') : '未就绪',
  }
}
function hasHardRisk(p: PlatformReadiness) {
  return p.risks.some((r) => r.level === 'risk')
}
function riskLabel(level: string) {
  return level === 'risk' ? '风险' : level === 'warn' ? '注意' : '说明'
}

function isSelf(row: MemberRow) {
  return row.id === userStore.user?.id
}

async function loadMembers() {
  if (!userStore.isAdmin) return
  loadingMembers.value = true
  try { members.value = await http.get<MemberRow[]>('/auth/users') }
  finally { loadingMembers.value = false }
}

async function setRole(row: MemberRow, role: string) {
  if (role === row.role) return
  roleSavingId.value = row.id
  try {
    const updated = await http.patch<MemberRow>(`/auth/users/${row.id}/role`, { role })
    Object.assign(row, updated)
    ElMessage.success(`${updated.username} 角色已改为 ${updated.role === 'admin' ? '管理员' : '普通成员'}`)
    // 改的是自己时同步本地身份，否则侧栏徽标与按钮门禁还停留在旧角色
    if (updated.id === userStore.user?.id) userStore.user = updated
  } finally { roleSavingId.value = null }
}

async function loadReadiness() {
  const data = await http.get<ReadinessResp>('/platforms/readiness')
  readiness.value = data.platforms
  readSummary.value = data.summary
}

async function loadOutbox() {
  loadingOutbox.value = true
  try {
    const data = await http.get<Record<string, MockRecord[]>>('/platforms/outbox')
    outboxKeys.value.forEach((k) => delete outbox[k])
    Object.assign(outbox, data)
    // 默认定位到最近有送达回执的渠道，避免打开就是个空 tab
    const latest = Object.keys(data).find((k) => data[k].length)
    if (latest) outboxPlatform.value = latest
    if (!outboxPlatform.value && Object.keys(data).length) outboxPlatform.value = Object.keys(data)[0]
  } finally { loadingOutbox.value = false }
}

onMounted(async () => {
  try { Object.assign(status, await http.get<typeof status>('/system/status')) }
  catch { /* 后端不可用时保持默认警示状态 */ }
  try { platforms.value = await http.get<PlatformStatus[]>('/platforms/status') }
  catch { /* 平台节缺失时留空表 */ }
  try { await loadReadiness() }
  catch { /* 旧后端无此接口时卡片显示空态，不影响其余设置项 */ }
  try { await loadOutbox() }
  catch { /* 外发箱读不到不影响其余设置项 */ }
  try { await loadMembers() }
  catch { /* 非管理员拿不到成员表，卡片本身也已隐藏 */ }
})

function logout() {
  userStore.logout()
  router.push('/login')
}
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.page-title { margin: 0 0 12px; font-size: 18px; }
.card {
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 16px 18px; margin-bottom: 16px; max-width: 640px;
}
.card h3 { margin: 0 0 12px; font-size: 15px; }
.card.wide { max-width: 860px; }
.logout-btn { margin-top: 12px; }
.hint { font-size: 12px; color: var(--ac-text-secondary); margin: 12px 0 0; }
.ready-summary { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.ready-row {
  border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 10px 12px; margin-top: 10px; background: var(--ac-bg);
}
.ready-row.is-risk { border-color: var(--el-color-danger); }
.ready-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.ready-name { font-size: 13px; }
.missing-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; }
.missing-label { font-size: 12px; color: var(--ac-text-secondary); }
.missing-none { font-size: 12px; color: var(--ac-text-secondary); }
.miss-tag { font-family: var(--el-font-family-mono, monospace); }
.risk-list { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: var(--ac-text-secondary); }
.risk-list li { margin-bottom: 4px; line-height: 1.6; }
.risk-list li.risk-text { color: var(--el-color-danger); }
.items-collapse { margin-top: 8px; }
.outbox-tabs { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.outbox-item {
  border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 10px 12px; margin-bottom: 8px; background: var(--ac-bg);
}
.outbox-head { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--ac-text-secondary); }
.outbox-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; margin-top: 6px; }
</style>
