<template>
  <!-- P2 记忆管理：长期画像事实（可删，向量同步清理）+ 中期会话摘要（只读） -->
  <div class="page">
    <h2 class="page-title">记忆</h2>
    <el-tabs v-model="tab">
      <el-tab-pane label="长期记忆（用户画像）" name="facts">
        <p class="hint">
          对话结束后由后台管道让模型抽取的稳定事实（偏好/身份/结论），
          新会话中 Agent 通过 recall_memory 工具按需向量召回。删除会同步清理向量索引。
        </p>
        <el-table :data="facts" v-loading="loadingFacts">
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="fact" label="事实" min-width="280" />
          <el-table-column label="类别" width="120">
            <template #default="{ row }">
              <el-tag size="small">{{ categoryLabel(row.category) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="source_conversation_id" label="来源会话" width="90" />
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-popconfirm title="删除该条记忆及其向量？" @confirm="removeFact(row)">
                <template #reference>
                  <el-button link type="danger" size="small">删除</el-button>
                </template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!facts.length && !loadingFacts"
                  description="暂无画像事实（需配置有效模型 key，对话若干轮后自动抽取）" />
      </el-tab-pane>

      <el-tab-pane label="中期记忆（会话摘要）" name="summaries">
        <p class="hint">短期滑窗之外的历史被滚动压缩为摘要，随系统提示注入上下文；此页只读展示。</p>
        <div v-for="s in summaries" :key="s.conversation_id" class="sum-card">
          <div class="sum-head">会话 #{{ s.conversation_id }} · 已摘要至消息 {{ s.until_message_id }} ·
            {{ fmtDateTime(s.updated_at) }}</div>
          <div class="sum-body">{{ s.summary }}</div>
        </div>
        <el-empty v-if="!summaries.length" description="暂无摘要（历史超过短期窗口后自动生成）" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { http } from '@/apis/http'
import { fmtDateTime } from '@/utils/time'

interface FactRow {
  id: number; fact: string; category: string
  source_conversation_id: number | null; created_at: string
}
interface SummaryRow {
  conversation_id: number; summary: string; until_message_id: number; updated_at: string
}

const tab = ref('facts')
const facts = ref<FactRow[]>([])
const summaries = ref<SummaryRow[]>([])
const loadingFacts = ref(false)

function categoryLabel(c: string) {
  return c === 'preference' ? '偏好' : c === 'profile' ? '身份' : c === 'case' ? '工单结论' : c
}

async function load() {
  loadingFacts.value = true
  try {
    facts.value = await http.get<FactRow[]>('/memory/facts')
    summaries.value = await http.get<SummaryRow[]>('/memory/summaries')
  } finally { loadingFacts.value = false }
}

async function removeFact(row: FactRow) {
  await http.del(`/memory/facts/${row.id}`)
  facts.value = facts.value.filter((f) => f.id !== row.id)
}

onMounted(load)
</script>

<style scoped>
.page { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; background: var(--ac-bg); }
.page-title { margin: 0 0 12px; font-size: 18px; }
.hint { font-size: 12px; color: var(--ac-text-secondary); margin: 0 0 14px; }
.sum-card {
  background: var(--ac-surface); border: 1px solid var(--ac-border);
  border-radius: var(--ac-radius); padding: 12px 14px; margin-bottom: 10px;
}
.sum-head { font-size: 12px; color: var(--ac-text-secondary); margin-bottom: 6px; }
.sum-body { font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
</style>
