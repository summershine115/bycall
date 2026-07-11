<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button type="primary" @click="load">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
      <span class="muted">会话管理：查看各用户对话历史、重置或删除会话</span>
    </div>

    <el-table :data="conversations" border @row-click="onRowClick">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="user_id" label="用户ID" width="90" />
      <el-table-column prop="channel" label="渠道" width="100">
        <template #default="{ row }">
          <el-tag :type="channelType(row.channel)">{{ row.channel }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="session_key" label="会话标识" min-width="160" />
      <el-table-column prop="message_count" label="消息数" width="90" />
      <el-table-column prop="last_message_at" label="最后消息" width="170">
        <template #default="{ row }">
          {{ formatTime(row.last_message_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click.stop="onViewMessages(row)">消息</el-button>
          <el-button size="small" type="warning" @click.stop="onReset(row)">重置</el-button>
          <el-button size="small" type="danger" @click.stop="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 消息历史对话框 -->
    <el-dialog v-model="msgDialogVisible" :title="`会话消息历史 #${currentConv?.id}`" width="700px">
      <div v-loading="msgLoading" class="msg-list">
        <div v-for="msg in messages" :key="msg.id" class="msg-item">
          <div class="msg-header">
            <el-tag :type="roleType(msg.role)" size="small">{{ roleLabel(msg.role) }}</el-tag>
            <span class="msg-time">{{ formatTime(msg.created_at) }}</span>
            <span class="msg-tokens">{{ msg.token_count }} tokens</span>
          </div>
          <div class="msg-content">{{ msg.content }}</div>
        </div>
        <div v-if="!messages.length && !msgLoading" class="muted">暂无消息</div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

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
.toolbar { margin-bottom: 16px; display: flex; align-items: center; gap: 12px; }
.muted { color: #909399; }
.msg-list { max-height: 500px; overflow-y: auto; }
.msg-item { margin-bottom: 16px; padding: 12px; border: 1px solid #ebeef5; border-radius: 4px; }
.msg-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.msg-time { font-size: 12px; color: #909399; }
.msg-tokens { font-size: 12px; color: #c0c4cc; margin-left: auto; }
.msg-content { white-space: pre-wrap; line-height: 1.6; color: #303133; }
</style>
