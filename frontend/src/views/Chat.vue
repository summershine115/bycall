<template>
  <div>
    <el-alert
      v-if="actAsInfo"
      type="warning"
      :closable="false"
      style="margin-bottom: 16px"
      show-icon
    >
      当前以 <strong>{{ actAsInfo }}</strong> 身份测试。所有权限判定按该用户的角色生效。
      <el-button size="small" @click="onClearActAs" style="margin-left: 12px">恢复 admin</el-button>
    </el-alert>

    <el-card>
      <template #header>
        <div class="chat-header">
          <span>对话测试（数据面 /chat · 多轮）</span>
          <el-button size="small" @click="onNewConversation">新对话</el-button>
        </div>
      </template>

      <div class="chat-messages" ref="msgContainer">
        <div v-for="(msg, i) in chatHistory" :key="i" class="chat-msg" :class="msg.role">
          <div class="chat-role">{{ roleLabel(msg.role) }}</div>
          <div class="chat-content">{{ msg.content }}</div>
        </div>
        <div v-if="loading" class="chat-msg assistant">
          <div class="chat-role">助手</div>
          <div class="chat-content muted">思考中...</div>
        </div>
        <div v-if="!chatHistory.length && !loading" class="muted center">
          问一个业务问题开始对话，如：上季度销售额是多少？
        </div>
      </div>

      <el-divider />

      <div class="chat-input">
        <el-input
          v-model="question"
          placeholder="输入问题，回车发送..."
          @keyup.enter="onAsk"
          :disabled="loading"
        >
          <template #append>
            <el-button :loading="loading" @click="onAsk">发送</el-button>
          </template>
        </el-input>
      </div>

      <el-collapse v-if="lastResult" style="margin-top: 16px">
        <el-collapse-item title="治理决策（透明化）" name="1">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="当前用户">{{ lastResult.user }}</el-descriptions-item>
            <el-descriptions-item label="角色">{{ lastResult.role }}</el-descriptions-item>
            <el-descriptions-item label="知识域权限">
              <el-tag v-for="t in lastResult.knowledge_scope" :key="t" size="small" type="success" style="margin-right: 4px">
                {{ t }}
              </el-tag>
              <span v-if="!lastResult.knowledge_scope.length" class="muted">无（默认隔离）</span>
            </el-descriptions-item>
            <el-descriptions-item label="命中授权 chunk">{{ lastResult.retrieved_chunks }}</el-descriptions-item>
            <el-descriptions-item label="决策">
              <el-tag :type="lastResult.decision === 'answered' ? 'success' : 'danger'">{{ lastResult.decision }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { admin, chat } from '../api'

const question = ref('')
const loading = ref(false)
const chatHistory = ref([])
const lastResult = ref(null)
const actAsInfo = ref('')
const msgContainer = ref(null)

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
    // 显示助手回答
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
  return { user: '我', assistant: '助手' }[r] || r
}
</script>

<style scoped>
.chat-header { display: flex; align-items: center; justify-content: space-between; }
.chat-messages { height: 400px; overflow-y: auto; padding: 8px; }
.chat-msg { margin-bottom: 16px; }
.chat-role { font-size: 12px; color: #909399; margin-bottom: 4px; }
.chat-content { padding: 10px 14px; border-radius: 8px; line-height: 1.7; white-space: pre-wrap; }
.chat-msg.user .chat-content { background: #ecf5ff; color: #303133; }
.chat-msg.assistant .chat-content { background: #f4f4f5; color: #303133; }
.muted { color: #909399; }
.center { text-align: center; padding: 40px; }
</style>
