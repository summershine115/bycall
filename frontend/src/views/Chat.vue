<template>
  <div class="bc-page chat-page">
    <PageHeader
      title="对话测试"
      description="在控制台直接走数据面 /chat 多轮对话；配合用户页的「扮演」验证目标角色的权限链路。"
    >
      <el-button :icon="Plus" @click="onNewConversation">新对话</el-button>
    </PageHeader>

    <!-- act-as 身份条：状态不只靠颜色（图标 + 文字 + 操作） -->
    <div v-if="actAsInfo" class="actas-banner" role="status">
      <el-icon :size="16" aria-hidden="true"><View /></el-icon>
      <span>
        当前以 <strong>{{ actAsInfo }}</strong> 身份测试，所有权限判定按该用户的角色生效
      </span>
      <el-button size="small" @click="onClearActAs">恢复 admin</el-button>
    </div>

    <div class="chat-shell bc-panel">
      <!-- 消息流 -->
      <div ref="msgContainer" class="chat-stream">
        <EmptyState
          v-if="!chatHistory.length && !loading"
          icon="ChatDotRound"
          title="开始一轮测试对话"
          description="问一个业务问题（如「我们技术部有哪些代码规范？」），观察下方治理决策面板如何判定。"
        />
        <template v-else>
          <div v-for="(msg, i) in chatHistory" :key="i" class="msg" :class="`msg--${msg.role}`">
            <div class="msg__avatar" aria-hidden="true">
              <el-icon :size="15">
                <User v-if="msg.role === 'user'" />
                <MagicStick v-else />
              </el-icon>
            </div>
            <div class="msg__bubble">
              <div class="msg__role">{{ roleLabel(msg.role) }}</div>
              <div class="msg__content">{{ msg.content }}</div>
            </div>
          </div>
          <div v-if="loading" class="msg msg--assistant">
            <div class="msg__avatar" aria-hidden="true">
              <el-icon :size="15"><MagicStick /></el-icon>
            </div>
            <div class="msg__bubble">
              <div class="msg__role">助手</div>
              <div class="msg__content msg__content--thinking">
                <span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" />
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 输入区 -->
      <div class="chat-composer">
        <el-input
          v-model="question"
          placeholder="输入问题，回车发送..."
          :disabled="loading"
          size="large"
          @keyup.enter="onAsk"
        >
          <template #append>
            <el-button type="primary" :loading="loading" @click="onAsk">发送</el-button>
          </template>
        </el-input>
      </div>

      <!-- 治理决策透明化面板 -->
      <div v-if="lastResult" class="gov-panel">
        <button class="gov-panel__toggle" type="button" @click="govOpen = !govOpen">
          <el-icon :size="14" aria-hidden="true">
            <ArrowDown v-if="govOpen" />
            <ArrowRight v-else />
          </el-icon>
          治理决策（透明化）
          <el-tag :type="lastResult.decision === 'answered' ? 'success' : 'danger'" size="small">
            {{ lastResult.decision }}
          </el-tag>
        </button>
        <div v-if="govOpen" class="gov-panel__body">
          <div class="gov-item">
            <span class="gov-item__label">当前用户</span>
            <span class="gov-item__value bc-strong">{{ lastResult.user }}</span>
          </div>
          <div class="gov-item">
            <span class="gov-item__label">角色</span>
            <span class="gov-item__value">{{ lastResult.role }}</span>
          </div>
          <div class="gov-item">
            <span class="gov-item__label">知识域权限</span>
            <span class="gov-item__value">
              <el-tag
                v-for="t in lastResult.knowledge_scope"
                :key="t"
                size="small"
                type="success"
                class="gov-tag"
              >
                {{ t }}
              </el-tag>
              <span v-if="!lastResult.knowledge_scope.length" class="bc-muted">无（默认隔离）</span>
            </span>
          </div>
          <div class="gov-item">
            <span class="gov-item__label">命中授权 chunk</span>
            <span class="gov-item__value bc-strong">{{ lastResult.retrieved_chunks }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, User, MagicStick, View, ArrowDown, ArrowRight } from '@element-plus/icons-vue'
import { admin, chat } from '../api'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'

const question = ref('')
const loading = ref(false)
const chatHistory = ref([])
const lastResult = ref(null)
const actAsInfo = ref('')
const msgContainer = ref(null)
const govOpen = ref(true)

async function onAsk() {
  if (!question.value.trim() || loading.value) return
  const q = question.value
  question.value = ''

  // 先显示用户消息
  chatHistory.value.push({ role: 'user', content: q })
  await scrollToBottom()

  loading.value = true
  try {
    const result = await chat.ask(q)
    lastResult.value = result
    chatHistory.value.push({ role: 'assistant', content: result.answer })

    if (result.decision === 'denied_no_access') {
      ElMessage.warning('该用户无权访问相关知识（ACL 预过滤拦截）')
    }

    // 检测 act-as 状态
    if (result.role !== 'admin' && !actAsInfo.value) {
      actAsInfo.value = result.user
    } else if (result.role === 'admin' && actAsInfo.value) {
      actAsInfo.value = ''
    }

    await scrollToBottom()
  } finally {
    loading.value = false
  }
}

function onNewConversation() {
  chatHistory.value = []
  lastResult.value = null
  ElMessage.info('已清空当前对话界面（后端会话历史仍保留，新问题会带上历史）')
}

async function scrollToBottom() {
  await nextTick()
  if (msgContainer.value) {
    msgContainer.value.scrollTop = msgContainer.value.scrollHeight
  }
}

async function onClearActAs() {
  await admin.clearActAs()
  actAsInfo.value = ''
  chatHistory.value = []
  lastResult.value = null
  ElMessage.success('已恢复 admin 身份')
}

function roleLabel(r) {
  return { user: '我', assistant: '数字员工' }[r] || r
}
</script>

<style scoped>
.chat-page {
  max-width: 960px;
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

/* 对话外壳 */
.chat-shell {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 250px);
  min-height: 480px;
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
  max-width: 82%;
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
.thinking-dot:nth-child(2) { animation-delay: 0.15s; }
.thinking-dot:nth-child(3) { animation-delay: 0.3s; }
@keyframes bc-think {
  0%, 60%, 100% { opacity: 0.35; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-3px); }
}

/* 输入区 */
.chat-composer {
  padding: var(--bc-space-4) var(--bc-space-6);
  border-top: 1px solid var(--bc-border);
  background: var(--bc-bg-surface);
}

/* 治理决策面板 */
.gov-panel {
  border-top: 1px solid var(--bc-border);
  background: var(--bc-gray-1);
}
.gov-panel__toggle {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  width: 100%;
  padding: var(--bc-space-3) var(--bc-space-6);
  border: none;
  background: transparent;
  font-family: inherit;
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-2);
  cursor: pointer;
}
.gov-panel__body {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--bc-space-4);
  padding: 0 var(--bc-space-6) var(--bc-space-4);
}
.gov-item {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-1);
}
.gov-item__label {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.gov-item__value {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
}
.gov-tag {
  margin-right: var(--bc-space-1);
}
</style>
