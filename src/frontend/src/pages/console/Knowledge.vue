<template>
  <!-- P1 知识库控制台：文档入库管理 / 结构化商品浏览 / 检索测试（所见即 Agent 所得） -->
  <div class="page">
    <h2 class="page-title">知识库</h2>
    <el-tabs v-model="tab" class="page-tabs">
      <el-tab-pane label="文档管理" name="files">
        <div class="toolbar">
          <el-upload
            :show-file-list="false" :http-request="doUpload"
            accept=".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.txt,.md,.docx"
          >
            <el-button type="primary" :loading="uploading">
              {{ uploading ? '解析入库中…' : '上传文件' }}
            </el-button>
          </el-upload>
          <span class="hint">支持 Excel/CSV/PDF/图片/txt/md/docx，≤20MB；商品 Excel 同时写入结构化商品表</span>
        </div>
        <el-table :data="files" v-loading="loadingFiles">
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="文件名" min-width="180" show-overflow-tooltip />
          <el-table-column prop="file_type" label="类型" width="90" />
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ (row.size / 1024).toFixed(1) }} KB</template>
          </el-table-column>
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small">{{ fileStatusLabel(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="chunk_count" label="分块" width="70" />
          <el-table-column label="错误" min-width="140">
            <template #default="{ row }">
              <el-tooltip v-if="row.error" :content="row.error" placement="top">
                <span class="error-text">{{ row.error }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch
                :model-value="row.enabled" size="small"
                @change="(v: boolean) => toggle(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="250">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openChunks(row)">分块</el-button>
              <!-- 禁用态的 button 不派发鼠标事件，tooltip 需要包一层 span 才显示得出原因 -->
              <el-tooltip :content="isAdmin ? '重跑解析+分块+向量化（解析器改动后修复旧切片、failed 重试）' : ADMIN_ONLY" placement="top">
                <span class="btn-wrap">
                  <el-button link type="warning" size="small" :disabled="!isAdmin || isBusy(row)" @click="reingest(row)">
                    重新入库
                  </el-button>
                </span>
              </el-tooltip>
              <el-tooltip :content="isAdmin ? '只补向量，不重跑解析/分块（换 embedding 模型、向量化降级后修复）' : ADMIN_ONLY" placement="top">
                <span class="btn-wrap">
                  <el-button link type="primary" size="small" :disabled="!isAdmin || isBusy(row)" @click="reindex(row)">
                    重建向量
                  </el-button>
                </span>
              </el-tooltip>
              <el-popconfirm title="删除文件及其全部检索内容？" @confirm="removeFile(row)">
                <template #reference>
                  <el-tooltip :content="ADMIN_ONLY" placement="top" :disabled="isAdmin">
                    <span class="btn-wrap">
                      <el-button link type="danger" size="small" :disabled="!isAdmin || isBusy(row)">删除</el-button>
                    </span>
                  </el-tooltip>
                </template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>

        <!-- 重建任务：入库/重建向量已改为持久化队列，进度与失败原因只能在这里看 -->
        <div class="jobs-block">
          <div class="jobs-head">
            <h3 class="sub-title">重建任务</h3>
            <span class="hint">队列中 {{ jobsPending }} 个 · 展示最近 {{ jobs.length }} 条</span>
            <el-button link type="primary" size="small" :loading="loadingJobs" aria-label="刷新重建任务列表" @click="loadJobs(true)">
              刷新
            </el-button>
          </div>
          <el-table :data="jobs" size="small" empty-text="暂无任务记录（上传、重新入库、重建向量都会在这里排队）">
            <el-table-column prop="id" label="任务" width="70" />
            <el-table-column label="类型" width="100">
              <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
            </el-table-column>
            <el-table-column prop="file_name" label="文件" min-width="150" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="jobStatus(row).type">{{ jobStatus(row).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="进度" width="110">
              <template #default="{ row }">{{ row.processed }}/{{ row.total }}</template>
            </el-table-column>
            <el-table-column label="更新时间" width="130">
              <template #default="{ row }">{{ fmtShort(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="错误原因" min-width="150">
              <template #default="{ row }">
                <el-tooltip v-if="row.error" :content="row.error" placement="top">
                  <span class="error-text">{{ row.error }}</span>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-tooltip v-if="canRetry(row)" :content="isAdmin ? '重新排队执行该任务' : ADMIN_ONLY" placement="top">
                  <span class="btn-wrap">
                    <el-button link type="warning" size="small" :disabled="!isAdmin || retryingId === row.id"
                               @click="retryJob(row)">重试</el-button>
                  </span>
                </el-tooltip>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="商品数据" name="products">
        <div class="toolbar">
          <el-input v-model="productQ" placeholder="按名称/SKU 搜索" clearable class="search-input"
                    @keyup.enter="searchProducts" @clear="searchProducts" />
          <el-button @click="searchProducts">搜索</el-button>
          <span class="hint">共 {{ productTotal }} 条</span>
        </div>
        <el-table :data="products" v-loading="loadingProducts">
          <el-table-column prop="sku" label="SKU" width="140" />
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column prop="price" label="价格" width="90" />
          <el-table-column prop="stock" label="库存" width="80" />
          <el-table-column prop="category" label="类目" width="110" />
          <el-table-column prop="specs" label="规格" min-width="180" show-overflow-tooltip />
          <el-table-column prop="tags" label="标签" min-width="120" show-overflow-tooltip />
          <el-table-column prop="file_id" label="来源文件" width="90" />
        </el-table>
        <el-empty v-if="!productTotal && !loadingProducts" description="没有匹配的商品行" />
        <el-pagination
          v-if="productTotal > productLimit" layout="prev, pager, next, jumper" :total="productTotal"
          :page-size="productLimit" :current-page="productOffset / productLimit + 1"
          @current-change="(p: number) => { productOffset = (p - 1) * productLimit; loadProducts() }"
        />
      </el-tab-pane>

      <el-tab-pane label="检索测试" name="search">
        <div class="toolbar">
          <el-input v-model="searchQ" placeholder="输入问题，验证召回效果" class="search-input"
                    @keyup.enter="doSearch" />
          <el-button type="primary" :loading="searching" @click="doSearch">检索</el-button>
        </div>
        <el-empty v-if="!searchResults.length && !searching" description="检索结果将显示在这里" />
        <div v-for="(r, i) in searchResults" :key="i" class="search-hit">
          <div class="hit-head">
            <el-tag size="small" type="success">score {{ r.score }}</el-tag>
            <span class="hit-file">《{{ r.file_name }}》 #{{ r.chunk_id }}</span>
          </div>
          <div class="hit-text">{{ r.text }}</div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 分块浏览：切片质量直接决定召回，排查「答非所问」时先看这里 -->
    <el-drawer v-model="chunksOpen" size="55%" :title="chunkTitle">
      <div class="chunks-meta">
        共 {{ chunkTotal }} 块 · 每页 {{ chunkLimit }} ·
        本页已建向量 {{ pageVectorized }}/{{ chunks.length }}
        <el-button link type="primary" size="small" :loading="loadingChunks" @click="loadChunks">刷新</el-button>
      </div>
      <el-alert v-if="chunks.length && pageVectorized < chunks.length"
                type="warning" :closable="false" class="chunks-warn"
                title="本页存在未建向量的分块：该文件只能靠关键词召回，点「重建向量」修复" />
      <div v-for="c in chunks" :key="c.chunk_id" class="chunk-item">
        <div class="chunk-head">
          <span class="chunk-seq">#{{ c.seq }}</span>
          <el-tag size="small" :type="c.vectorized ? 'success' : 'info'">
            {{ c.vectorized ? '向量' : '仅关键词' }}
          </el-tag>
          <span class="chunk-len">{{ c.chars }} 字 / ~{{ c.tokens }} token</span>
        </div>
        <div class="chunk-text">{{ c.text }}</div>
      </div>
      <el-empty v-if="!chunks.length && !loadingChunks" description="该文件还没有已入库的文本分块" />
      <el-pagination
        v-if="chunkTotal > chunkLimit" layout="prev, pager, next" :total="chunkTotal"
        :page-size="chunkLimit" :current-page="chunkOffset / chunkLimit + 1"
        @current-change="(p: number) => { chunkOffset = (p - 1) * chunkLimit; loadChunks() }"
      />
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/apis/http'
import { useUserStore } from '@/stores/user'
import { fmtShort } from '@/utils/time'

interface KnowledgeFileRow {
  id: number; name: string; file_type: string; size: number; status: string
  error: string | null; enabled: boolean; chunk_count: number; created_at: string
}
interface IngestJobRow {
  id: number; file_id: number; file_name: string; kind: string; status: string
  processed: number; total: number; error: string | null; created_at: string; updated_at: string
}
interface JobsPage { pending: number; items: IngestJobRow[] }
interface ProductRow {
  id: number; sku: string; name: string; price: number | null; stock: number | null
  category: string; specs: string; tags: string; file_id: number | null
}
interface ProductPage { total: number; limit: number; offset: number; items: ProductRow[] }
interface ChunkRow {
  chunk_id: number; seq: number; tokens: number; chars: number; vectorized: boolean; text: string
}
interface ChunkPage {
  file_id: number; file_name: string; status: string; total: number
  limit: number; offset: number; items: ChunkRow[]
}
interface SearchHit { chunk_id: number; file_id: number; file_name: string; score: number; text: string }

const tab = ref('files')
const files = ref<KnowledgeFileRow[]>([])
const loadingFiles = ref(false)
const uploading = ref(false)

const userStore = useUserStore()
// 破坏性操作（重新入库/重建向量/删除/重试任务）后端已做 admin 403 门禁，前端提前禁用避免误点
const ADMIN_ONLY = '需要管理员权限'
const isAdmin = computed(() => userStore.isAdmin)

const jobs = ref<IngestJobRow[]>([])
const jobsPending = ref(0)
const loadingJobs = ref(false)
const retryingId = ref<number | null>(null)
const jobsSupported = ref(true)
let jobsKey = ''

const products = ref<ProductRow[]>([])
const productQ = ref('')
const productTotal = ref(0)
const productLimit = 20
const productOffset = ref(0)
const loadingProducts = ref(false)

const searchQ = ref('')
const searchResults = ref<SearchHit[]>([])
const searching = ref(false)

const chunksOpen = ref(false)
const chunkFile = ref<KnowledgeFileRow | null>(null)
const chunks = ref<ChunkRow[]>([])
const chunkTotal = ref(0)
const chunkLimit = 20
const chunkOffset = ref(0)
const loadingChunks = ref(false)

const chunkTitle = computed(() =>
  chunkFile.value ? `分块预览 · ${chunkFile.value.name}` : '分块预览')
const pageVectorized = computed(() => chunks.value.filter((c) => c.vectorized).length)

function statusType(s: string) {
  if (s === 'ready') return 'success'
  if (s === 'failed') return 'danger'
  return 'warning'  // pending / parsing：后台任务进行中
}
function isBusy(row: KnowledgeFileRow) {
  return row.status === 'pending' || row.status === 'parsing'
}

const KIND_LABELS: Record<string, string> = {
  upload: '上传入库', reingest: '重新入库', reindex: '重建向量',
}
const JOB_STATUS: Record<string, { label: string; type: 'success' | 'info' | 'warning' | 'danger' }> = {
  queued: { label: '排队中', type: 'info' },
  running: { label: '执行中', type: 'warning' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  interrupted: { label: '已中断', type: 'danger' },
}
function kindLabel(k: string) { return KIND_LABELS[k] ?? k }
function jobStatus(j: IngestJobRow) { return JOB_STATUS[j.status] ?? { label: j.status, type: 'info' as const } }
function canRetry(j: IngestJobRow) { return j.status === 'interrupted' || j.status === 'failed' }
function jobsInFlight() {
  return jobs.value.some((j) => j.status === 'queued' || j.status === 'running')
}
/** 找到该文件当前活跃的队列任务：文件状态 parsing 本身分不清「还在排队」还是「真的在跑」 */
function activeJob(fileId: number) {
  return jobs.value.find((j) => j.file_id === fileId && (j.status === 'queued' || j.status === 'running')) ?? null
}
function fileStatusLabel(row: KnowledgeFileRow) {
  const j = activeJob(row.id)
  if (j) return j.status === 'queued' ? '排队中' : `执行中 ${j.processed}/${j.total}`
  return row.status
}

async function loadFiles() {
  loadingFiles.value = true
  try { files.value = await http.get<KnowledgeFileRow[]>('/knowledge/files') }
  finally { loadingFiles.value = false }
}

/** manual=false 用于轮询：不点亮刷新按钮的 loading，也静默失败避免每 2 秒弹一次错误 */
async function loadJobs(manual = false) {
  if (!jobsSupported.value) return
  if (manual) loadingJobs.value = true
  try {
    const page = await http.get<JobsPage>('/knowledge/jobs?limit=30')
    // 集合与进度无变化时不重新赋值，防止表格重渲染打断悬停与滚动
    const key = page.items.map((j) => `${j.id}:${j.status}:${j.processed}/${j.total}:${j.error ?? ''}`).join(',')
    if (key !== jobsKey) {
      jobsKey = key
      jobs.value = page.items
    }
    jobsPending.value = page.pending
  } catch {
    // 接口不可用（旧后端/持续故障）就停掉这一块：既不空转轮询，也避免每 2 秒重复弹错
    jobsSupported.value = false
    jobs.value = []
    jobsPending.value = 0
  } finally { loadingJobs.value = false }
}

async function retryJob(job: IngestJobRow) {
  retryingId.value = job.id
  try {
    const kf = await http.post<KnowledgeFileRow>(`/knowledge/jobs/${job.id}/retry`)
    const row = files.value.find((f) => f.id === kf.id)
    if (row) Object.assign(row, kf)
    else await loadFiles()
    ElMessage.success(`任务 #${job.id} 已重新排队`)
    await loadJobs(true)
    pollUntilIdle()
  } finally { retryingId.value = null }
}

let pollTimer: ReturnType<typeof setInterval> | undefined
/** 重新入库/重建向量是后台任务：文件状态与队列任务都清空了才算跑完 */
function pollUntilIdle() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    try { await Promise.all([loadFiles(), loadJobs()]) }
    catch { /* 后端瞬断：本轮跳过，下一轮再试，避免未处理的 Promise 拒绝 */ }
    if (!files.value.some(isBusy) && !jobsInFlight() && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = undefined
    }
  }, 2000)
}

async function doUpload(opt: { file: File }) {
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', opt.file)
    const kf = await http.upload<KnowledgeFileRow>('/knowledge/files', fd)
    files.value.unshift(kf)
    ElMessage.success(kf.status === 'ready' ? '入库成功' : `已上传，状态：${kf.status}`)
    if (kf.status === 'failed') ElMessage.warning(kf.error || '入库失败，可点「重新入库」重试')
    await loadJobs(true)
    if (isBusy(kf)) pollUntilIdle()
  } finally { uploading.value = false }
}

async function toggle(row: KnowledgeFileRow, enabled: boolean) {
  const updated = await http.patch<KnowledgeFileRow>(`/knowledge/files/${row.id}`, { enabled })
  Object.assign(row, updated)
}

async function reingest(row: KnowledgeFileRow) {
  const updated = await http.post<KnowledgeFileRow>(`/knowledge/files/${row.id}/reingest`)
  Object.assign(row, updated)
  ElMessage.success('已提交重新入库，后台解析中…')
  await loadJobs(true)
  pollUntilIdle()
}

async function reindex(row: KnowledgeFileRow) {
  const updated = await http.post<KnowledgeFileRow>(`/knowledge/files/${row.id}/reindex`)
  Object.assign(row, updated)
  ElMessage.success('已提交向量重建，后台向量化中…')
  await loadJobs(true)
  pollUntilIdle()
}

async function openChunks(row: KnowledgeFileRow) {
  chunkFile.value = row
  chunkOffset.value = 0
  chunksOpen.value = true
  await loadChunks()
}

async function loadChunks() {
  if (!chunkFile.value) return
  loadingChunks.value = true
  try {
    const page = await http.get<ChunkPage>(
      `/knowledge/files/${chunkFile.value.id}/chunks?limit=${chunkLimit}&offset=${chunkOffset.value}`)
    chunks.value = page.items
    chunkTotal.value = page.total
  } finally { loadingChunks.value = false }
}

async function removeFile(row: KnowledgeFileRow) {
  await http.del(`/knowledge/files/${row.id}`)
  files.value = files.value.filter((f) => f.id !== row.id)
}

async function loadProducts() {
  loadingProducts.value = true
  try {
    const p = new URLSearchParams({ limit: String(productLimit), offset: String(productOffset.value) })
    if (productQ.value.trim()) p.set('q', productQ.value.trim())
    const page = await http.get<ProductPage>(`/knowledge/products?${p}`)
    products.value = page.items
    productTotal.value = page.total
  } finally { loadingProducts.value = false }
}

function searchProducts() {
  productOffset.value = 0
  loadProducts()
}

async function doSearch() {
  if (!searchQ.value.trim()) return
  searching.value = true
  try { searchResults.value = await http.post<SearchHit[]>('/knowledge/search',
    { query: searchQ.value.trim() }) }
  finally { searching.value = false }
}

onMounted(async () => {
  await Promise.all([loadFiles(), loadJobs(true)])
  await loadProducts()
  if (files.value.some(isBusy) || jobsInFlight()) pollUntilIdle()
})
onBeforeUnmount(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.page-title { margin: 0 0 12px; font-size: 18px; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.hint { font-size: 12px; color: var(--ac-text-secondary); }
.search-input { max-width: 360px; }
.jobs-block { margin-top: 18px; }
.btn-wrap { display: inline-flex; }
.jobs-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.sub-title { margin: 0; font-size: 14px; }
.muted { font-size: 12px; color: var(--ac-text-secondary); }
.error-text {
  color: var(--el-color-danger); display: inline-block; max-width: 200px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
}
.search-hit {
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 12px 14px; margin-bottom: 10px;
}
.hit-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.hit-file { font-size: 12px; color: var(--ac-text-secondary); }
.hit-text { font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
.chunks-meta { display: flex; align-items: center; gap: 8px; font-size: 12px;
  color: var(--ac-text-secondary); margin-bottom: 10px; }
.chunks-warn { margin-bottom: 12px; }
.chunk-item { border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 10px 12px; margin-bottom: 10px; background: var(--ac-surface); }
.chunk-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.chunk-seq { font-size: 12px; color: var(--ac-text-secondary); }
.chunk-len { font-size: 12px; color: var(--ac-text-secondary); }
.chunk-text { font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
</style>
