<template>
  <!-- P4 MCP 管理：服务器 CRUD、连接测试（即时工具发现）、写操作确认开关 -->
  <div class="page">
    <div class="head-row">
      <h2 class="page-title">MCP 服务</h2>
      <el-tooltip :content="ADMIN_ONLY" placement="bottom" :disabled="isAdmin">
        <span class="btn-wrap">
          <el-button type="primary" :disabled="!isAdmin" @click="openDialog()">添加服务器</el-button>
        </span>
      </el-tooltip>
    </div>

    <!-- 待确认的工具调用：不只网页 SSE，任何渠道的会话排队都靠这里替买家点确认 -->
    <section class="card" aria-label="待确认的工具调用">
      <div class="card-head">
        <h3 class="card-title">待确认的工具调用</h3>
        <span class="muted-note">3 秒轮询；超过 3 分钟无人应答后端会按「拒绝」处理</span>
        <el-button size="small" :loading="loadingPending" @click="loadPending(true)">刷新</el-button>
      </div>
      <p v-if="!pending.length && !loadingPending" class="muted-note">暂无待确认调用</p>
      <div v-for="p in pending" :key="p.request_id" class="pending-card">
        <div class="pending-head">
          <el-tag size="small" type="warning">{{ p.tool_name }}</el-tag>
          <router-link class="conv-link" :to="`/chat/${p.conversation_id}`">会话 #{{ p.conversation_id }}</router-link>
          <span class="muted-note">发起于 {{ fmtDateTime(p.created_at) }}</span>
        </div>
        <pre class="pending-args">{{ argsText(p.args) }}</pre>
        <div class="pending-actions">
          <el-button type="primary" size="small" :loading="actingId === p.request_id"
                     aria-label="同意执行该工具调用" @click="resolve(p, true)">同意执行</el-button>
          <el-button size="small" :loading="actingId === p.request_id"
                     aria-label="拒绝该工具调用" @click="resolve(p, false)">拒绝</el-button>
        </div>
      </div>
    </section>

    <el-table :data="servers" v-loading="loading">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="transport" label="传输" width="130" />
      <el-table-column label="端点/命令" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.transport === 'stdio' ? `${row.command} ${row.args}` : row.url }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'connected' ? 'success' : row.status === 'error' ? 'danger' : 'info'">
            {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="工具" min-width="160">
        <template #default="{ row }">
          <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
            <el-tag size="small" type="danger">error</el-tag>
          </el-tooltip>
          <el-tag v-for="t in row.tools.slice(0, 4)" :key="t.name ?? t" size="small" class="tool-tag">
            {{ t.name ?? t }}
          </el-tag>
          <span v-if="!row.tools.length" class="muted">未连接</span>
        </template>
      </el-table-column>
      <el-table-column label="需确认" width="90">
        <template #default="{ row }">
          <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
            <el-switch :model-value="row.require_confirm" size="small" :disabled="!isAdmin"
                       @change="(v: boolean) => toggle(row, v)" />
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
            <el-switch :model-value="row.enabled" size="small" :disabled="!isAdmin"
                       @change="(v: boolean) => toggle(row, undefined, v)" />
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170">
        <template #default="{ row }">
          <!-- 禁用态 button 不派发鼠标事件，tooltip 要包一层 span 才显示得出「需要管理员权限」 -->
          <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
            <span class="btn-wrap">
              <el-button link type="primary" size="small" :disabled="!isAdmin" :loading="testingId === row.id"
                         @click="test(row)">测试</el-button>
            </span>
          </el-tooltip>
          <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
            <span class="btn-wrap">
              <el-button link size="small" :disabled="!isAdmin" @click="openDialog(row)">编辑</el-button>
            </span>
          </el-tooltip>
          <el-popconfirm title="删除该服务器配置？" @confirm="remove(row)">
            <template #reference>
              <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
                <span class="btn-wrap">
                  <el-button link type="danger" size="small" :disabled="!isAdmin">删除</el-button>
                </span>
              </el-tooltip>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑服务器' : '添加服务器'" width="480px">
      <el-form label-width="110px">
        <el-form-item label="名称"><el-input v-model="form.name" maxlength="32" /></el-form-item>
        <el-form-item label="传输方式">
          <el-select v-model="form.transport" style="width: 100%">
            <el-option label="stdio（本地命令）" value="stdio" />
            <el-option label="sse" value="sse" />
            <el-option label="streamable_http" value="streamable_http" />
          </el-select>
        </el-form-item>
        <template v-if="form.transport === 'stdio'">
          <el-form-item label="命令"><el-input v-model="form.command" placeholder="如 py" /></el-form-item>
          <el-form-item label="参数"><el-input v-model="form.args" placeholder="空格分隔，如 scripts/demo_mcp_server.py" /></el-form-item>
        </template>
        <el-form-item v-else label="URL">
          <el-input v-model="form.url" placeholder="https://… 或 http://127.0.0.1:…" />
        </el-form-item>
        <el-form-item label="写操作需确认">
          <el-switch v-model="form.require_confirm" />
          <span class="muted-note">开启后每次工具调用前弹出确认卡片</span>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/apis/http'
import { useUserStore } from '@/stores/user'
import { fmtDateTime } from '@/utils/time'

interface McpToolMeta { name?: string; description?: string }
interface ServerRow {
  id: number; name: string; transport: string; command: string; args: string;
  url: string; enabled: boolean; require_confirm: boolean; status: string;
  tools: McpToolMeta[]; last_error: string | null; created_at: string
}
interface ServerForm {
  id?: number; name: string; transport: string; command: string;
  args: string; url: string; enabled: boolean; require_confirm: boolean
}
// created_at 是后端 time.time() 的秒级浮点（非 ISO 串），fmtDateTime 已兼容
interface PendingCall {
  request_id: string; conversation_id: number; tool_name: string;
  args: Record<string, unknown>; created_at: number
}

const servers = ref<ServerRow[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const dialogVisible = ref(false)
const saving = ref(false)
const ADMIN_ONLY = '需要管理员权限'
const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin)

const pending = ref<PendingCall[]>([])
const loadingPending = ref(false)
const actingId = ref<string | null>(null)
let pendingKey = ''
let pendingTimer: ReturnType<typeof setInterval> | undefined
const form = reactive<ServerForm>({
  name: '', transport: 'stdio', command: '', args: '', url: '',
  enabled: true, require_confirm: true,
})

function argsText(args: Record<string, unknown>) {
  try { return JSON.stringify(args ?? {}, null, 2) } catch { return String(args) }
}

/** 轮询只在请求集合变化时重赋值：卡片重渲染会打断运营阅读参数 */
async function loadPending(manual = false) {
  if (manual) loadingPending.value = true
  try {
    const r = await http.get<{ items: PendingCall[] }>('/mcp/pending')
    const key = r.items.map((i) => i.request_id).join('|')
    if (key !== pendingKey) {
      pendingKey = key
      pending.value = r.items
    }
  } catch { /* 后端瞬断/旧版本无此接口：下一轮再试，不打扰运营 */ }
  finally { loadingPending.value = false }
}

async function resolve(p: PendingCall, approved: boolean) {
  actingId.value = p.request_id
  try {
    await http.post('/mcp/confirm', { request_id: p.request_id, approved })
    ElMessage.success(approved ? '已同意，工具将立即执行' : '已拒绝该工具调用')
  } catch { /* 410＝已被其他端处理或超时：下面的刷新会把卡片清掉 */ }
  finally {
    actingId.value = null
    pendingKey = ''
    await loadPending()
  }
}

async function load() {
  loading.value = true
  try { servers.value = await http.get<ServerRow[]>('/mcp/servers') }
  finally { loading.value = false }
}

function openDialog(row?: ServerRow) {
  if (!isAdmin.value) return
  form.id = row?.id
  form.name = row?.name ?? ''
  form.transport = row?.transport ?? 'stdio'
  form.command = row?.command ?? ''
  form.args = row?.args ?? ''
  form.url = row?.url ?? ''
  form.enabled = row?.enabled ?? true
  form.require_confirm = row?.require_confirm ?? true
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  try {
    const { id, ...body } = form
    if (id) await http.patch(`/mcp/servers/${id}`, body)
    else await http.post('/mcp/servers', body)
    dialogVisible.value = false
    ElMessage.success('已保存，工具将在下次对话或测试时重新发现')
    await load()
  } finally { saving.value = false }
}

// PATCH 接口收全量 ServerBody，这里基于当前行合并开关值提交
async function toggle(row: ServerRow, requireConfirm?: boolean, enabled?: boolean) {
  if (!isAdmin.value) return
  await http.patch(`/mcp/servers/${row.id}`, {
    name: row.name, transport: row.transport, command: row.command, args: row.args,
    url: row.url, enabled: enabled ?? row.enabled,
    require_confirm: requireConfirm ?? row.require_confirm,
  })
  Object.assign(row, { enabled: enabled ?? row.enabled, require_confirm: requireConfirm ?? row.require_confirm })
}

async function test(row: ServerRow) {
  if (!isAdmin.value) return
  testingId.value = row.id
  try {
    const r = await http.post<{ status: string; tool_count: number; error: string | null }>(
      `/mcp/servers/${row.id}/test`)
    row.status = r.status
    if (r.tool_count) ElMessage.success(`连接成功，发现 ${r.tool_count} 个工具`)
    else ElMessage.warning(r.error ?? '已连接，但未发现工具')
    await load()
  } finally { testingId.value = null }
}

async function remove(row: ServerRow) {
  if (!isAdmin.value) return
  await http.del(`/mcp/servers/${row.id}`)
  servers.value = servers.value.filter((s) => s.id !== row.id)
}

onMounted(async () => {
  await Promise.all([load(), loadPending(true)])
  pendingTimer = setInterval(() => { void loadPending() }, 3000)
})
// 组件卸载必须清 timer，否则离开页面后仍在轮询 /mcp/pending
onBeforeUnmount(() => { if (pendingTimer) clearInterval(pendingTimer) })
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.head-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.page-title { margin: 0; font-size: 18px; }
.tool-tag { margin-right: 4px; }
.btn-wrap { display: inline-flex; }
.muted { font-size: 12px; color: var(--ac-text-secondary); }
.muted-note { font-size: 12px; color: var(--ac-text-secondary); }
.card {
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 14px 16px; margin-bottom: 16px;
}
.card-head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.card-title { margin: 0; font-size: 15px; }
.pending-card {
  border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 10px 12px; margin-bottom: 10px; background: var(--ac-bg);
}
.pending-head { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.conv-link { color: var(--ac-brand); text-decoration: none; }
.conv-link:hover { text-decoration: underline; }
.pending-args {
  margin: 8px 0 0; padding: 8px 10px; background: var(--ac-surface);
  border: 1px solid var(--ac-border); border-radius: 8px;
  font-size: 12px; max-height: 180px; overflow: auto; white-space: pre-wrap; word-break: break-all;
}
.pending-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
</style>
