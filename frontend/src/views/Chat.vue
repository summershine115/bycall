<template>
  <div class="bc-page chat-page">
    <PageHeader
      title="对话测试"
      description="在控制台直接走数据面 /chat 多轮对话；左侧切换历史会话，右侧流式输出、逐轮展开治理决策。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新会话列表" @click="loadConversations" />
    </PageHeader>

    <!-- act-as 身份条 -->
    <div v-if="actAsInfo" class="actas-banner" role="status">
      <el-icon :size="16" aria-hidden="true"><View /></el-icon>
      <span>
        当前以 <strong>{{ actAsInfo }}</strong> 身份测试，所有权限判定按该用户的角色生效
      </span>
      <el-button size="small" @click="onClearActAs">恢复 admin</el-button>
    </div>

    <div class="chat-layout">
      <!-- 会话列表侧栏 -->
      <aside class="conv-sidebar">
        <div class="conv-sidebar__head">
          <span>历史会话</span>
          <el-button size="small" type="primary" :icon="Plus" plain @click="onNewConversation">
            新对话
          </el-button>
        </div>
        <div v-loading="convListLoading" class="conv-list">
          <button
            v-for="c in displayItems"
            :key="c.id"
            type="button"
            class="conv-item"
            :class="{
              'conv-item--active': c.isNew || c.id === currentConvId,
              'conv-item--new': c.isNew,
            }"
            :aria-current="c.isNew || c.id === currentConvId ? 'true' : undefined"
            @click="c.isNew || selectConversation(c.id)"
          >
            <div class="conv-item__preview">
              <el-icon v-if="c.isNew" :size="12"><EditPen /></el-icon>
              {{ c.preview }}
            </div>
            <div v-if="!c.isNew" class="conv-item__meta">
              <span>{{ c.message_count }} 条</span>
              <span>{{ formatConvTime(c.last_message_at) }}</span>
              <el-icon
                class="conv-item__del"
                aria-label="删除会话"
                @click.stop="onDeleteConversation(c.id)"
              ><Delete /></el-icon>
            </div>
          </button>
          <div v-if="!conversations.length && !convListLoading" class="conv-empty bc-muted">
            暂无历史会话
          </div>
        </div>
      </aside>

      <!-- 聊天区 -->
      <div class="chat-shell bc-panel">
        <!-- 对话对象选择（ADR-0012 多 Bot：按实例测试，能力边界随实例生效） -->
        <div class="agent-picker">
          <span class="agent-picker__label">对话对象</span>
          <el-select
            :model-value="selectedAgentId"
            size="small"
            style="width: 220px"
            :disabled="loading || streaming"
            @change="onAgentChange"
          >
            <el-option label="默认实例（未指定）" :value="null" />
            <el-option
              v-for="a in enabledAgents"
              :key="a.id"
              :label="(a.display_name || a.name) + (a.is_default ? '（默认）' : '')"
              :value="a.id"
            />
          </el-select>
          <span class="bc-muted bc-text-xs">
            切换后开新对话；选择实例后其人格 / 能力边界 / 模型绑定即时生效
          </span>
        </div>

        <!-- 消息流 -->
        <div ref="msgContainer" class="chat-stream">
          <EmptyState
            v-if="!chatHistory.length && !streaming"
            icon="ChatDotRound"
            :title="currentConvId ? '该会话暂无消息' : '开始一轮新对话'"
            description="问一个业务问题（如「我们技术部有哪些代码规范？」），展开答案下的治理决策面板查看工具调用与引用来源。"
          />
          <template v-else>
            <div
              v-for="(msg, i) in chatHistory"
              :key="i"
              class="msg"
              :class="`msg--${msg.role}`"
            >
              <div class="msg__avatar" aria-hidden="true">
                <el-icon :size="15">
                  <User v-if="msg.role === 'user'" />
                  <MagicStick v-else />
                </el-icon>
              </div>
              <div class="msg__bubble">
                <div class="msg__role">{{ roleLabel(msg.role) }}</div>
                <div v-if="msg.role === 'user'" class="msg__content">{{ msg.content }}</div>
                <MarkdownBody v-else :content="msg.content" class="msg__content" />
                <!-- 治理决策（仅实时对话的 assistant 消息携带 meta；历史消息无） -->
                <button
                  v-if="msg.role === 'assistant' && msg.meta"
                  class="gov-trigger"
                  type="button"
                  :aria-expanded="msg.govOpen"
                  @click="toggleGov(i)"
                >
                  <el-icon :size="12">
                    <ArrowDown v-if="msg.govOpen" />
                    <ArrowRight v-else />
                  </el-icon>
                  治理决策
                  <el-tag :type="decisionType(msg.meta.decision)" size="small">
                    {{ msg.meta.decision }}
                  </el-tag>
                  <span v-if="msg.meta.tool_calls?.length" class="gov-trigger__chip">
                    <el-icon :size="11"><Tools /></el-icon>{{ msg.meta.tool_calls.length }} 工具
                  </span>
                  <span v-if="dedupCitations(msg.meta.citations).length" class="gov-trigger__chip">
                    <el-icon :size="11"><Link /></el-icon>{{ dedupCitations(msg.meta.citations).length }} 引用
                  </span>
                </button>
                <div v-if="msg.govOpen && msg.meta" class="gov-detail">
                  <div class="gov-grid">
                    <div class="gov-item">
                      <span class="gov-item__label">用户 / 角色</span>
                      <span class="gov-item__value">{{ msg.meta.user }} · {{ msg.meta.role }}</span>
                    </div>
                    <div class="gov-item">
                      <span class="gov-item__label">命中授权 chunk</span>
                      <span class="gov-item__value bc-strong">{{ msg.meta.retrieved_chunks }}</span>
                    </div>
                    <div class="gov-item gov-item--wide">
                      <span class="gov-item__label">知识域权限</span>
                      <span class="gov-item__value">
                        <el-tag
                          v-for="t in msg.meta.knowledge_scope"
                          :key="t"
                          size="small"
                          type="success"
                        >{{ t }}</el-tag>
                        <span v-if="!msg.meta.knowledge_scope?.length" class="bc-muted">
                          无（默认隔离）
                        </span>
                      </span>
                    </div>
                  </div>

                  <!-- 工具调用记录 -->
                  <div v-if="msg.meta.tool_calls?.length" class="gov-section">
                    <div class="gov-section__title">
                      <el-icon :size="13"><Tools /></el-icon>工具调用（{{ msg.meta.tool_calls.length }}）
                    </div>
                    <div v-for="(tc, j) in msg.meta.tool_calls" :key="j" class="tool-call">
                      <div class="tool-call__name">
                        <el-icon :size="12"><Tools /></el-icon>{{ tc.name }}
                      </div>
                      <details class="tool-call__detail">
                        <summary>参数 / 结果</summary>
                        <div class="tool-call__block">
                          <span class="bc-muted">参数</span>
                          <pre>{{ formatJson(tc.args) }}</pre>
                        </div>
                        <div class="tool-call__block">
                          <span class="bc-muted">结果</span>
                          <pre>{{ tc.result }}</pre>
                        </div>
                      </details>
                    </div>
                  </div>

                  <!-- 引用来源卡片 -->
                  <div v-if="dedupCitations(msg.meta.citations).length" class="gov-section">
                    <div class="gov-section__title">
                      <el-icon :size="13"><Link /></el-icon>引用来源（{{ dedupCitations(msg.meta.citations).length }}）
                    </div>
                    <div class="citation-grid">
                      <div
                        v-for="(c, j) in dedupCitations(msg.meta.citations)"
                        :key="j"
                        class="citation"
                      >
                        <div class="citation__title">
                          <a v-if="c.url" :href="c.url" target="_blank" rel="noopener">
                            {{ c.title || c.source || c.url }}
                          </a>
                          <span v-else>{{ c.title || c.source || '未命名来源' }}</span>
                        </div>
                        <div v-if="c.source && c.source !== c.title" class="citation__source">
                          {{ c.source }}
                        </div>
                        <div v-if="c.snippet" class="citation__snippet">{{ c.snippet }}</div>
                      </div>
                    </div>
                  </div>

                  <!-- 上下文快照（排障级，默认折叠） -->
                  <div v-if="msg.meta.context_trace" class="gov-section">
                    <details class="trace">
                      <summary>上下文快照 context_trace（模型这一轮看到了什么）</summary>
                      <pre>{{ formatTrace(msg.meta.context_trace) }}</pre>
                    </details>
                  </div>
                </div>
              </div>
            </div>

            <!-- 流式输出气泡 -->
            <div v-if="streaming" class="msg msg--assistant">
              <div class="msg__avatar" aria-hidden="true">
                <el-icon :size="15"><MagicStick /></el-icon>
              </div>
              <div class="msg__bubble">
                <div class="msg__role">数字员工</div>
                <MarkdownBody v-if="streamText" :content="streamText" class="msg__content" />
                <div v-else class="msg__content msg__content--thinking">
                  <span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" />
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- 输入区（多行：Enter 发送，Shift+Enter 换行） -->
        <div class="chat-composer">
          <el-input
            v-model="question"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 5 }"
            resize="none"
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            :disabled="loading"
            @keydown.enter.exact.prevent="onAsk"
          />
          <el-button
            type="primary"
            size="large"
            :loading="loading"
            :icon="Promotion"
            @click="onAsk"
          >发送</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus,
  User,
  MagicStick,
  View,
  ArrowDown,
  ArrowRight,
  Tools,
  Link,
  Promotion,
  Delete,
  Refresh,
  EditPen,
} from '@element-plus/icons-vue'
import { admin, chat } from '../api'
import { formatTime } from '../utils/format'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import MarkdownBody from '../components/MarkdownBody.vue'

const question = ref('')
const loading = ref(false)
const conversations = ref([]) // 当前用户的 api 会话列表（倒序）
const currentConvId = ref(null) // null = 新会话模式（发首条消息时后端新建）
// 列表展示项：新建态时顶部插一个虚拟「新对话」指示项（非真实会话，不可删除）
const displayItems = computed(() => {
  if (currentConvId.value === null) {
    return [{ id: '__new__', preview: '新对话', isNew: true }, ...conversations.value]
  }
  return conversations.value
})
const chatHistory = ref([]) // [{role, content, meta?, govOpen?}]
const streaming = ref(false)
const streamText = ref('')
const actAsInfo = ref('')
const msgContainer = ref(null)
const convListLoading = ref(false)
// 数字员工选择（ADR-0012）：null = 默认实例；仅对「新对话」生效，选中历史会话时随会话归属联动
const agents = ref([])
const selectedAgentId = ref(null)
const enabledAgents = computed(() => agents.value.filter((a) => a.is_enabled))

onMounted(async () => {
  // 进入即恢复最近一次会话（无会话则停留在新会话模式）。
  const list = await loadConversations()
  if (list.length) await selectConversation(list[0].id)
  admin.listAgents().then((all) => (agents.value = all)).catch(() => {})
})

async function loadConversations() {
  convListLoading.value = true
  try {
    conversations.value = await chat.conversations()
    return conversations.value
  } catch {
    return []
  } finally {
    convListLoading.value = false
  }
}

async function selectConversation(id) {
  if (loading.value || streaming.value) return
  currentConvId.value = id
  // 选择器随会话归属联动（会话的实例绑定优先于全局选择）
  const conv = conversations.value.find((c) => c.id === id)
  if (conv) selectedAgentId.value = conv.agent_id ?? null
  chatHistory.value = []
  streamText.value = ''
  loading.value = true
  try {
    const msgs = await chat.conversationMessages(id)
    // 响应返回时用户可能已切到别的会话：只接受仍指向当前会话的迟到响应
    if (currentConvId.value !== id) return
    chatHistory.value = msgs.map((m) => ({ role: m.role, content: m.content }))
    await scrollToBottom()
  } catch {
    ElMessage.error('加载会话消息失败')
  } finally {
    loading.value = false
  }
}

function onNewConversation() {
  if (loading.value || streaming.value) return
  currentConvId.value = null
  chatHistory.value = []
  streamText.value = ''
}

function onAgentChange(val) {
  selectedAgentId.value = val
  // 切实例 = 换对话对象：开新对话（会话按实例隔离，沿用旧会话会被后端 400 拒绝）
  onNewConversation()
}

async function onAsk() {
  const q = question.value.trim()
  if (!q || loading.value) return
  question.value = ''
  chatHistory.value.push({ role: 'user', content: q })
  await scrollToBottom()

  const wasNew = currentConvId.value === null // 标记是否新建会话
  loading.value = true
  streaming.value = true
  streamText.value = ''
  try {
    await chat.stream(q, currentConvId.value, {
      // 仅新建会话时显式指定实例；继续会话走会话已有归属（跨实例不一致会被后端拒绝）
      agentId: wasNew ? selectedAgentId.value : undefined,
      onToken: async (text) => {
        streamText.value += text
        await scrollToBottom()
      },
      onFlush: () => {
        streamText.value = ''
      },
      onDone: async (data) => {
        chatHistory.value.push({
          role: 'assistant',
          content: data.answer || streamText.value,
          meta: data,
          govOpen: false,
        })
        streamText.value = ''
        streaming.value = false
        // 新建会话：拿到后端分配的 id，切到该会话。
        if (wasNew) currentConvId.value = data.conversation_id
        await loadConversations() // 刷新侧栏（preview/count/时间）
        updateActAs(data)
        if (data.decision === 'denied_no_access') {
          ElMessage.warning('该用户无权访问相关知识（ACL 预过滤拦截）')
        }
        await scrollToBottom()
      },
      onError: (e) => {
        streaming.value = false
        if (streamText.value) {
          chatHistory.value.push({ role: 'assistant', content: streamText.value })
          streamText.value = ''
        }
        ElMessage.error(e.message || '对话失败')
      },
    })
  } finally {
    loading.value = false
    streaming.value = false
  }
}

async function onDeleteConversation(id) {
  // 流式/加载进行中不允许删除当前会话：删除后 onNewConversation 会被 streaming
  // 拦截，currentConvId 残留指向已删会话，后续消息推送/发送全部失效
  if (id === currentConvId.value && (loading.value || streaming.value)) {
    ElMessage.warning('当前会话正在响应，请稍后再删除')
    return
  }
  try {
    await ElMessageBox.confirm('确定删除该会话？消息将永久清除（不可恢复）。', '删除会话', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await chat.deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) onNewConversation()
    ElMessage.success('已删除')
  } catch {
    ElMessage.error('删除失败')
  }
}

async function onClearActAs() {
  await admin.clearActAs()
  actAsInfo.value = ''
  currentConvId.value = null
  chatHistory.value = []
  streamText.value = ''
  await loadConversations()
}

function updateActAs(data) {
  if (data.role !== 'admin' && !actAsInfo.value) {
    actAsInfo.value = data.user
  } else if (data.role === 'admin' && actAsInfo.value) {
    actAsInfo.value = ''
  }
}

function toggleGov(i) {
  const msg = chatHistory.value[i]
  if (msg) msg.govOpen = !msg.govOpen
}

// 引用来源去重：多个 chunk 可能同源（按 title/source 合并）
function dedupCitations(citations) {
  if (!citations?.length) return []
  const seen = new Set()
  const out = []
  for (const c of citations) {
    const key = c.title || c.source || c.url || ''
    if (key && seen.has(key)) continue
    if (key) seen.add(key)
    out.push(c)
  }
  return out
}

function formatJson(v) {
  try {
    return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function formatTrace(trace) {
  try {
    return JSON.stringify(JSON.parse(trace), null, 2)
  } catch {
    return trace
  }
}

function formatConvTime(iso) {
  return formatTime(iso)
}

function decisionType(d) {
  return (
    { answered: 'success', denied_no_access: 'warning', denied: 'danger', error: 'danger', task: 'info' }[d] ||
    'info'
  )
}

async function scrollToBottom() {
  await nextTick()
  if (msgContainer.value) {
    msgContainer.value.scrollTop = msgContainer.value.scrollHeight
  }
}

function roleLabel(r) {
  return { user: '我', assistant: '数字员工' }[r] || r
}
</script>

<style scoped>
.chat-page {
  max-width: 1100px;
}

/* act-as 身份条 */
.actas-banner {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-4);
  background: var(--bc-warning-bg);
  border: 1px solid var(--bc-warning-border);
  border-radius: var(--bc-radius-md);
  color: var(--bc-warning-strong);
  font-size: var(--bc-text-sm);
  margin-bottom: var(--bc-space-4);
}
.actas-banner .el-button {
  margin-left: auto;
}

/* 两栏布局：会话列表 + 聊天区 */
.chat-layout {
  display: flex;
  gap: var(--bc-space-4);
  height: calc(100vh - 250px);
  min-height: 480px;
}

/* 会话列表侧栏 */
.conv-sidebar {
  width: 248px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bc-bg-surface);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-lg);
  overflow: hidden;
}
.conv-sidebar__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-3);
  border-bottom: 1px solid var(--bc-border);
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-2);
}
.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--bc-space-2);
}
.conv-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: var(--bc-space-2) var(--bc-space-3);
  margin-bottom: 3px;
  border: 1px solid transparent;
  border-radius: var(--bc-radius-md);
  background: transparent;
  cursor: pointer;
  font-family: inherit;
  transition: background var(--bc-duration-fast) var(--bc-ease-out);
}
.conv-item:hover {
  background: var(--bc-gray-2);
}
.conv-item--active {
  background: var(--bc-primary-1);
  border-color: var(--bc-primary-2);
}
.conv-item--new {
  border-style: dashed;
}
.conv-item--new .conv-item__preview {
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
  color: var(--bc-primary-6);
  font-weight: var(--bc-weight-medium);
}
.conv-item__preview {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 3px;
}
.conv-item__meta {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.conv-item__del {
  margin-left: auto;
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--bc-duration-fast);
}
.conv-item:hover .conv-item__del {
  opacity: 0.7;
}
.conv-item__del:hover {
  opacity: 1;
  color: var(--bc-danger);
}
.conv-empty {
  padding: var(--bc-space-6) var(--bc-space-3);
  text-align: center;
  font-size: var(--bc-text-xs);
}

/* 聊天区 */
.chat-shell {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
/* 实例选择条 */
.agent-picker {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-2) var(--bc-space-6);
  border-bottom: 1px solid var(--bc-border);
  flex-shrink: 0;
}
.agent-picker__label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  flex-shrink: 0;
}
.chat-stream {
  flex: 1;
  overflow-y: auto;
  padding: var(--bc-space-6);
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-5);
}
.chat-stream .empty-state {
  margin: auto;
}

/* 消息气泡 */
.msg {
  display: flex;
  gap: var(--bc-space-3);
  max-width: 86%;
}
.msg--user {
  align-self: flex-end;
  flex-direction: row-reverse;
}
.msg__avatar {
  width: 30px;
  height: 30px;
  border-radius: var(--bc-radius-full);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: var(--bc-gray-2);
  color: var(--bc-text-3);
}
.msg--user .msg__avatar {
  background: var(--bc-primary-6);
  color: var(--bc-bg-surface);
}
.msg__bubble {
  min-width: 0;
}
.msg__role {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin-bottom: var(--bc-space-1);
}
.msg--user .msg__role {
  text-align: right;
}
.msg__content {
  padding: var(--bc-space-3) var(--bc-space-4);
  border-radius: var(--bc-radius-lg);
  font-size: var(--bc-text-md);
  line-height: var(--bc-leading-relaxed);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bc-gray-2);
  color: var(--bc-text-1);
}
.msg--user .msg__content {
  background: var(--bc-primary-1);
  border: 1px solid var(--bc-primary-2);
}
.msg--assistant .msg__content {
  border: 1px solid var(--bc-border);
  background: var(--bc-bg-surface);
  white-space: normal;
}
.msg__content--thinking {
  display: flex;
  gap: var(--bc-space-1);
  align-items: center;
  min-height: 24px;
}
.thinking-dot {
  width: 6px;
  height: 6px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-text-4);
  animation: bc-think 1.2s infinite var(--bc-ease-out);
}
.thinking-dot:nth-child(2) {
  animation-delay: 0.15s;
}
.thinking-dot:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes bc-think {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

/* 治理决策触发条 */
.gov-trigger {
  display: inline-flex;
  align-items: center;
  gap: var(--bc-space-2);
  margin-top: var(--bc-space-2);
  padding: 4px var(--bc-space-2);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  background: var(--bc-gray-1);
  font-family: inherit;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-2);
  cursor: pointer;
  transition: background var(--bc-duration-fast) var(--bc-ease-out);
}
.gov-trigger:hover {
  background: var(--bc-gray-2);
}
.gov-trigger__chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--bc-text-3);
}

/* 治理详情展开区 */
.gov-detail {
  margin-top: var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-4);
  background: var(--bc-gray-1);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  font-size: var(--bc-text-sm);
}
.gov-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--bc-space-3) var(--bc-space-4);
}
.gov-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gov-item--wide {
  grid-column: 1 / -1;
}
.gov-item__label {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.gov-item__value {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.gov-section {
  margin-top: var(--bc-space-4);
  padding-top: var(--bc-space-3);
  border-top: 1px dashed var(--bc-border);
}
.gov-section__title {
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  margin-bottom: var(--bc-space-2);
}

/* 工具调用记录 */
.tool-call {
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-sm);
  background: var(--bc-bg-surface);
  margin-bottom: var(--bc-space-2);
}
.tool-call__name {
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
  padding: var(--bc-space-2) var(--bc-space-3);
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-1);
}
.tool-call__detail summary {
  padding: var(--bc-space-1) var(--bc-space-3);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  cursor: pointer;
}
.tool-call__block {
  padding: 0 var(--bc-space-3) var(--bc-space-2);
}
.tool-call__block span {
  font-size: var(--bc-text-xs);
}
.tool-call__block pre {
  margin: 2px 0 0;
  padding: var(--bc-space-2);
  background: var(--bc-gray-1);
  border-radius: var(--bc-radius-sm);
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-xs);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

/* 引用来源卡片 */
.citation-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--bc-space-2);
}
.citation {
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-sm);
  background: var(--bc-bg-surface);
  padding: var(--bc-space-2) var(--bc-space-3);
}
.citation__title {
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-1);
  word-break: break-word;
}
.citation__title a {
  color: var(--bc-primary-6);
  text-decoration: none;
}
.citation__title a:hover {
  text-decoration: underline;
}
.citation__source {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin-top: 2px;
}
.citation__snippet {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin-top: var(--bc-space-1);
  line-height: var(--bc-leading-normal);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* context_trace */
.trace summary {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  cursor: pointer;
}
.trace pre {
  margin: var(--bc-space-2) 0 0;
  padding: var(--bc-space-3);
  background: var(--bc-gray-1);
  border-radius: var(--bc-radius-sm);
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-xs);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow-y: auto;
}

/* 输入区 */
.chat-composer {
  display: flex;
  gap: var(--bc-space-3);
  align-items: flex-end;
  padding: var(--bc-space-4) var(--bc-space-6);
  border-top: 1px solid var(--bc-border);
  background: var(--bc-bg-surface);
}
.chat-composer .el-textarea {
  flex: 1;
}
</style>
