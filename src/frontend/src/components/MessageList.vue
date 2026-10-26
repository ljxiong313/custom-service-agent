<template>
  <!-- 消息列表（P8 运营视角）：「客」=买家（左），「我」=Agent/人工代答（右） -->
  <div ref="scrollRef" class="msg-list">
    <div v-for="m in messages" :key="m.id ?? -1" class="msg-row" :class="m.role">
      <div class="avatar" :class="m.role">{{ m.role === 'user' ? '客' : '我' }}</div>
      <div class="bubble" :class="m.role">
        <!-- 来源徽标仅运营侧可见，买家/客户无感知（meta.from 标记） -->
        <el-tag v-if="fromTag(m.from)" size="small" :type="fromTag(m.from)!.type" class="src-tag">
          {{ fromTag(m.from)!.label }}{{ m.staff ? ` · ${m.staff}` : '' }}
        </el-tag>
        <!-- 工具调用过程（P1/P4：检索、商品查询、MCP 等，历史由 meta_json 还原） -->
        <div v-if="m.tools?.length" class="tool-line">
          <el-tag
            v-for="(t, i) in m.tools" :key="i" size="small" type="info" class="tool-tag"
            :title="t.output ?? ''"
          >
            {{ t.name }}
          </el-tag>
        </div>
        <!-- 助手消息走 Markdown；已 XSS 过滤（见 renderMd） -->
        <div v-if="m.role === 'assistant'" class="md-body" v-html="renderMd(m.content)" />
        <div v-else class="user-text">{{ m.content }}</div>
        <!-- 图片/文件附件（P8 需求4） -->
        <div v-if="m.attachments?.length" class="attach-list">
          <template v-for="(a, i) in m.attachments" :key="i">
            <img v-if="a.type === 'image'" :src="a.url" class="attach-img" :alt="a.name" />
            <a v-else :href="a.url" target="_blank" rel="noopener" class="attach-file">
              📎 {{ a.name }}
            </a>
          </template>
        </div>
        <!-- 引用来源（meta_json.citations） -->
        <div v-if="m.citations?.length" class="cites">
          <div class="cites-title">参考来源</div>
          <div v-for="c in m.citations" :key="c.index" class="cite-item">
            [{{ c.index }}] {{ c.file_name }}：{{ c.snippet }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { ChatMessage } from '@/stores/console'

const props = defineProps<{ messages: ChatMessage[] }>()
const scrollRef = ref<HTMLElement>()

// 模型输出可能包含链接/HTML，统一过 DOMPurify 再注入，防止存储型 XSS
function renderMd(text: string): string {
  return DOMPurify.sanitize(marked.parse(text, { async: false }) as string)
}

// meta.from → 运营侧来源徽标（买家侧内容本身即"客服话术"，无差异）
const FROM_TAGS: Record<string, { label: string; type: 'warning' | 'info' | 'success' }> = {
  human_agent: { label: '人工客服（企微）', type: 'warning' },
  human_operator: { label: '人工代答', type: 'warning' },
  system_handoff: { label: '转人工提示', type: 'info' },
  system_handoff_ask: { label: '转人工确认询问', type: 'info' },
  system_idle_prompt: { label: '超时结束提示', type: 'info' },
  system_session_closed: { label: '会话已结束', type: 'info' },
}
function fromTag(from?: string) { return from ? FROM_TAGS[from] : undefined }

// 新消息时保持贴底
watch(
  () => [props.messages.length, props.messages[props.messages.length - 1]?.content],
  () => nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  }),
)
</script>

<style scoped>
.msg-list { flex: 1; overflow-y: auto; padding: 20px 24px; }
.msg-row { display: flex; gap: 10px; margin-bottom: 16px; }
.msg-row.user { flex-direction: row; }
.msg-row.assistant { flex-direction: row-reverse; }
.avatar {
  width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; color: #fff;
}
.avatar.assistant { background: var(--ac-brand); }
.avatar.user { background: #8fa3b8; }
.bubble { max-width: 72%; padding: 10px 14px; border-radius: var(--ac-radius); font-size: 14px; }
.bubble.assistant { background: var(--ac-brand); color: #fff; }
.bubble.user { background: var(--ac-surface); border: 1px solid var(--ac-border); }
.bubble.assistant .md-body :deep(a) { color: #fff; text-decoration: underline; }
.bubble.assistant .cites { border-top-color: rgba(255, 255, 255, 0.35); color: rgba(255, 255, 255, 0.85); }
.bubble.assistant .tool-tag { background: rgba(255, 255, 255, 0.15); color: #fff; border-color: transparent; }
.user-text { white-space: pre-wrap; word-break: break-word; }
.src-tag { margin-bottom: 6px; }
.tool-line { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.tool-tag { font-family: var(--el-font-family-mono, monospace); }
.attach-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.attach-img { max-width: 220px; max-height: 160px; border-radius: 8px; display: block; }
.attach-file { font-size: 13px; color: inherit; text-decoration: underline; }
.cites {
  margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--ac-border);
  font-size: 12px; color: var(--ac-text-secondary);
}
.cites-title { font-weight: 600; margin-bottom: 4px; }
.cite-item { line-height: 1.6; word-break: break-all; }
</style>
