<template>
  <!-- B3-10 运营看板：机器人解决率、转人工率、时长与模型成本。受众是业务负责人，不是运维 -->
  <div class="page">
    <div class="head">
      <h2 class="page-title">运营看板</h2>
      <div class="toolbar">
        <el-radio-group v-model="days" size="small" @change="load">
          <el-radio-button :value="7">近 7 天</el-radio-button>
          <el-radio-button :value="30">近 30 天</el-radio-button>
          <el-radio-button :value="90">近 90 天</el-radio-button>
        </el-radio-group>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon class="alert">
      <template #title>看板数据加载失败</template>
      <template #default>{{ error }}</template>
    </el-alert>

    <div v-loading="loading">
      <div class="stats">
        <div class="stat">
          <span class="stat-num">{{ conv.conversations }}</span>
          <span class="stat-label">会话数（进行中 {{ conv.active }}）</span>
        </div>
        <div class="stat">
          <span class="stat-num ok">{{ pct(conv.bot_resolved_rate) }}</span>
          <span class="stat-label">机器人独立解决率（未转人工）</span>
        </div>
        <div class="stat">
          <span class="stat-num warn">{{ pct(conv.handoff_rate) }}</span>
          <span class="stat-label">转人工率</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ conv.avg_messages_per_conversation ?? '—' }}</span>
          <span class="stat-label">平均轮次 / 会话</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ pct(conv.close_rate) }}</span>
          <span class="stat-label">会话关闭率</span>
        </div>
      </div>

      <p class="hint">
        「机器人独立解决率」的口径是窗口内没有产生任何转人工工单的会话占比。
        它<b>不等于</b>满意率：买家没转人工也可能只是离开了，所以要和平均轮次、关闭率一起看。
        窗口内没有会话时所有比率显示「—」而不是 0%。
      </p>

      <div class="cards">
        <section class="card">
          <h3>人工侧效率</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="工单数">{{ handoff.tickets }}</el-descriptions-item>
            <el-descriptions-item label="状态分布">
              待接 {{ handoff.by_status?.open ?? 0 }} / 处理中 {{ handoff.by_status?.handling ?? 0 }}
              / 已结 {{ handoff.by_status?.closed ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="平均首响">
              {{ seconds(handoff.avg_first_response_seconds) }}
              <span class="muted">（{{ handoff.responded }} 单已响应）</span>
            </el-descriptions-item>
            <el-descriptions-item label="平均处理时长">
              {{ seconds(handoff.avg_handling_seconds) }}
              <span class="muted">（{{ handoff.closed }} 单已结）</span>
            </el-descriptions-item>
          </el-descriptions>
          <p class="hint">首响只统计<b>人工</b>回复，机器人自动应答不计，否则这个数会被刷得很漂亮。</p>
        </section>

        <section class="card">
          <h3>会话时长</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="平均时长">{{ seconds(dur.avg_session_seconds) }}</el-descriptions-item>
            <el-descriptions-item label="中位时长">{{ seconds(dur.median_session_seconds) }}</el-descriptions-item>
            <el-descriptions-item label="样本数">{{ dur.closed_with_duration }}</el-descriptions-item>
          </el-descriptions>
          <h4 class="sub">渠道分布</h4>
          <el-table :data="channelRows" size="small" empty-text="暂无数据">
            <el-table-column prop="channel" label="渠道" min-width="100" />
            <el-table-column prop="count" label="会话数" width="90" />
          </el-table>
        </section>

        <section class="card">
          <h3>满意度</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="均分">
              {{ sat.avg_rating === null ? '—' : `${sat.avg_rating} / 5` }}
            </el-descriptions-item>
            <el-descriptions-item label="参评">
              {{ sat.rated }} 份 / 已关闭 {{ sat.closed_conversations }} 个会话
              <span class="muted">（参评率 {{ pct(sat.response_rate) }}）</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="dist">
            <span v-for="n in [5, 4, 3, 2, 1]" :key="n" class="dist-item">
              {{ n }}分 <b>{{ sat.distribution?.[String(n)] ?? 0 }}</b>
            </span>
          </div>
          <p class="hint">
            评价来自结单时发出的签名回访链接（`session.rate_survey_enabled` 开启后才有）。
            一个会话只记第一票，所以均分不受"改主意重填"影响；参评率低时均分没有代表性。
          </p>
        </section>

        <section class="card wide">
          <h3>模型用量与成本</h3>
          <el-table :data="llmRows" size="small" v-loading="loading" empty-text="窗口内没有模型调用">
            <el-table-column prop="kind" label="能力" width="110" />
            <el-table-column label="调用" width="80">
              <template #default="{ row }">{{ row.calls }}</template>
            </el-table-column>
            <el-table-column label="失败" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.not_ok ? 'danger' : 'success'">
                  {{ row.not_ok }}（{{ pct(rate(row)) }}）
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="token" min-width="120">
              <template #default="{ row }">
                {{ row.total_tokens }}
                <span v-if="estimated(row)" class="muted">（估算）</span>
              </template>
            </el-table-column>
            <el-table-column label="估算成本" width="120">
              <template #default="{ row }">{{ cost(row.cost_est) }}</template>
            </el-table-column>
          </el-table>
          <p class="hint">
            合计 {{ llm.totals.calls }} 次调用、{{ llm.totals.total_tokens }} tokens，
            整体失败率 {{ pct(llm.totals.error_rate) }}，成本 {{ cost(llm.totals.cost_est) }}。
            {{ llm.pricing_configured
              ? '成本按配置 llm.pricing 估算。'
              : '未配置 llm.pricing，成本一律显示"未配价"而不是 0 元。' }}
            embedding/rerank 接口不回报用量，这两类 token 是本地估算值。
          </p>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { http } from '@/apis/http'

interface ConvBlock {
  conversations: number; closed: number; active: number; close_rate: number | null;
  messages: number; avg_messages_per_conversation: number | null;
  conversations_with_handoff: number; handoff_rate: number | null;
  bot_resolved_rate: number | null; by_channel: Record<string, number>
}
interface HandoffBlock {
  tickets: number; by_status: Record<string, number>; responded: number;
  avg_first_response_seconds: number | null; closed: number; avg_handling_seconds: number | null
}
interface DurationBlock { closed_with_duration: number; avg_session_seconds: number | null; median_session_seconds: number | null }
interface LlmItem { kind: string; calls: number; total_tokens: number; cost_est: number | null; not_ok: number }
interface SatisfactionBlock {
  rated: number; closed_conversations: number; response_rate: number | null;
  avg_rating: number | null; distribution: Record<string, number>
}
interface LlmBlock {
  totals: { calls: number; input_tokens: number; output_tokens: number; total_tokens: number;
            cost_est: number | null; not_ok: number; error_rate: number | null };
  by_kind: Record<string, { calls: number; total_tokens: number; cost_est: number | null; not_ok: number }>;
  pricing_configured: boolean; estimated_only_kinds: string[]
}
interface Dashboard {
  since: string; days: number; conversations: ConvBlock; handoff: HandoffBlock;
  session_duration: DurationBlock; satisfaction: SatisfactionBlock; llm: LlmBlock
}

const EMPTY: Dashboard = {
  since: '', days: 7,
  conversations: { conversations: 0, closed: 0, active: 0, close_rate: null, messages: 0,
    avg_messages_per_conversation: null, conversations_with_handoff: 0, handoff_rate: null,
    bot_resolved_rate: null, by_channel: {} },
  handoff: { tickets: 0, by_status: {}, responded: 0, avg_first_response_seconds: null, closed: 0, avg_handling_seconds: null },
  session_duration: { closed_with_duration: 0, avg_session_seconds: null, median_session_seconds: null },
  satisfaction: { rated: 0, closed_conversations: 0, response_rate: null, avg_rating: null, distribution: {} },
  llm: { totals: { calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_est: null, not_ok: 0, error_rate: null },
    by_kind: {}, pricing_configured: false, estimated_only_kinds: [] },
}

const days = ref(7)
const loading = ref(false)
const error = ref('')
const data = ref<Dashboard>(EMPTY)

const conv = computed(() => data.value.conversations)
const handoff = computed(() => data.value.handoff)
const dur = computed(() => data.value.session_duration)
const llm = computed(() => data.value.llm)
const sat = computed(() => data.value.satisfaction)

const channelRows = computed(() => Object.entries(conv.value.by_channel ?? {})
  .map(([channel, count]) => ({ channel: channel === 'web' ? '网页' : channel, count })))

const llmRows = computed(() => Object.entries(llm.value.by_kind ?? {})
  .map(([kind, v]) => ({ kind, ...v })))

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await http.get<Dashboard>(`/insights/dashboard?days=${days.value}`)
  } catch (e) {
    // 看板读不到时保留上一帧而不是清空：业务方在页面上看到的是空白会误以为"数据归零"
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function rate(row: LlmItem & { calls: number }) {
  return row.calls ? row.not_ok / row.calls : null
}

function estimated(row: { kind: string }) {
  return llm.value.estimated_only_kinds.includes(row.kind)
}

function pct(v: number | null | undefined) {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`
}

function cost(v: number | null | undefined) {
  return v === null || v === undefined ? '未配价' : `${v.toFixed(4)} 元`
}

function seconds(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  if (v < 60) return `${v.toFixed(1)} 秒`
  if (v < 3600) return `${(v / 60).toFixed(1)} 分钟`
  return `${(v / 3600).toFixed(1)} 小时`
}

onMounted(load)
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.page-title { margin: 0 0 12px; font-size: 18px; }
.toolbar { display: flex; align-items: center; gap: 8px; }
.alert { margin-bottom: 12px; }
.stats { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 8px; }
.stat {
  background: var(--ac-surface); border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 12px 16px; min-width: 150px; display: flex; flex-direction: column; gap: 4px;
}
.stat-num { font-size: 22px; font-weight: 600; }
.stat-num.ok { color: var(--el-color-success); }
.stat-num.warn { color: var(--el-color-warning); }
.stat-label { font-size: 12px; color: var(--ac-text-secondary); }
.cards { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; }
.card {
  background: var(--ac-surface); border: 1px solid var(--ac-border); border-radius: var(--ac-radius);
  padding: 16px 18px; flex: 1 1 320px;
}
.card.wide { flex: 1 1 100%; }
.card h3 { margin: 0 0 12px; font-size: 15px; }
.sub { margin: 14px 0 8px; font-size: 13px; }
.hint { font-size: 12px; color: var(--ac-text-secondary); margin: 10px 0 0; line-height: 1.6; }
.muted { color: var(--ac-text-secondary); font-size: 12px; }
.dist { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
.dist-item { font-size: 12px; color: var(--ac-text-secondary); }
.dist-item b { font-size: 14px; color: var(--ac-text); margin-left: 2px; }
</style>
