<template>
  <div class="bc-page">
    <PageHeader title="会话管理" description="各渠道用户的对话会话：查看消息历史、重置上下文或删除会话。">
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="5" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!conversations.length"
        icon="ChatLineRound"
        title="暂无会话"
        description="员工通过企微或对话测试与数字员工的对话会在此形成会话记录。"
      />

      <el-table v-else :data="conversations">
        <el-table-column prop="id" label="ID" width="64" />
        <el-table-column prop="user_id" label="用户ID" width="90" />
        <el-table-column label="渠道" width="100">
          <template #default="{ row }">
            <el-tag :type="channelType(row.channel)" size="small">{{ row.channel }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="session_key" label="会话标识" min-width="180">
          <template #default="{ row }">
            <span class="mono bc-text-sm">{{ row.session_key }}</span>
          </template>
        </el-table-column>
        <el-table-column label="消息数" width="90" align="center">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.message_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最后消息" width="160">
          <template #default="{ row }">
            <span class="bc-text-sm bc-muted">{{ formatTime(row.last_message_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="onViewMessages(row)">消息</el-button>
            <el-button size="small" type="warning" plain @click="onReset(row)">重置</el-button>
            <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 消息历史对话框 -->
    <el-dialog v-model="msgDialogVisible" :title="`会话消息历史 #${currentConv?.id ?? ''}`" width="720px">
      <div v-loading="msgLoading" class="msg-list">
        <div v-for="msg in messages" :key="msg.id" class="msg-item">
          <div class="msg-item__header">
            <el-tag :type="roleType(msg.role)" size="small">{{ roleLabel(msg.role) }}</el-tag>
            <span class="msg-item__time">{{ formatTime(msg.created_at) }}</span>
            <span class="msg-item__tokens">{{ msg.token_count }} tokens</span>
          </div>
          <div class="msg-item__content">{{ msg.content }}</div>
        </div>
        <EmptyState v-if="!messages.length && !msgLoading" icon="ChatDotRound" title="暂无消息" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const conversations = ref([])
const msgDialogVisible = ref(false)
const msgLoading = ref(false)
const messages = ref([])
const currentConv = ref(null)

async function load() {
  loading.value = true
  try {
    conversations.value = await admin.listConversations()
  } finally {
    loading.value = false
  }
}

async function onViewMessages(row) {
  currentConv.value = row
  msgDialogVisible.value = true
  msgLoading.value = true
  try {
    messages.value = await admin.listMessages(row.id)
  } finally {
    msgLoading.value = false
  }
}

async function onReset(row) {
  await ElMessageBox.confirm(
    `确定重置会话 #${row.id}？将清空全部消息和摘要，但保留会话本身。`,
    '确认重置',
    { type: 'warning' },
  )
  await admin.resetConversation(row.id)
  ElMessage.success('已重置')
  await load()
}

async function onDelete(row) {
  await ElMessageBox.confirm(
    `确定删除会话 #${row.id}？将永久删除全部消息和会话记录（不可恢复）。`,
    '确认删除',
    { type: 'warning' },
  )
  await admin.deleteConversation(row.id)
  ElMessage.success('已删除')
  await load()
}

function channelType(ch) {
  return { api: 'info', wecom: 'success', feishu: 'primary' }[ch] || 'info'
}
function roleType(r) {
  return { user: 'primary', assistant: 'success', system: 'info' }[r] || 'info'
}
function roleLabel(r) {
  return { user: '用户', assistant: '助手', system: '系统' }[r] || r
}
function formatTime(t) {
  if (!t) return ''
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<style scoped>
.msg-list {
  max-height: 56vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-3);
}
.msg-item {
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  padding: var(--bc-space-3) var(--bc-space-4);
  background: var(--bc-gray-1);
}
.msg-item__header {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  margin-bottom: var(--bc-space-2);
}
.msg-item__time {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.msg-item__tokens {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
  margin-left: auto;
}
.msg-item__content {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: var(--bc-leading-relaxed);
}
</style>
