<template>
  <div class="bc-page">
    <PageHeader title="会话管理" description="数字员工与各渠道用户的对话会话 · 可查看全部消息与摘要">
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="5" />

    <template v-else>
      <!-- ============ 组合筛选（设计稿：时间分段 + 渠道 + 状态 + 关键词） ============ -->
      <div class="toolbar">
        <div class="segmented" role="tablist" aria-label="时间范围">
          <button
            v-for="r in RANGES"
            :key="r.value"
            type="button"
            :class="['segmented__btn', { 'is-on': range === r.value }]"
            role="tab"
            :aria-selected="range === r.value"
            @click="range = r.value"
          >
            {{ r.label }}
          </button>
        </div>

        <el-select v-model="channelFilter" class="toolbar__select" aria-label="渠道筛选">
          <el-option label="全部渠道" value="all" />
          <el-option v-for="c in channels" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>

        <el-select v-model="statusFilter" class="toolbar__select" aria-label="状态筛选">
          <el-option label="全部状态" value="all" />
          <el-option label="活跃" value="active" />
          <el-option label="已结束" value="closed" />
        </el-select>

        <el-input
          v-model="keyword"
          class="toolbar__search"
          placeholder="搜索会话标识 / 用户"
          clearable
          :prefix-icon="Search"
        />

        <span class="toolbar__count">
          匹配 <b>{{ filtered.length }}</b> / {{ conversations.length }} 条
        </span>
      </div>

      <div class="bc-panel">
        <EmptyState
          v-if="!filtered.length"
          icon="ChatLineRound"
          :title="conversations.length ? '暂无匹配的会话' : '暂无会话'"
          :description="
            conversations.length
              ? '当前筛选条件下没有匹配的会话记录'
              : '员工通过企微或对话测试与数字员工的对话会在此形成会话记录。'
          "
        >
          <el-button v-if="conversations.length" @click="clearFilter">清除筛选</el-button>
        </EmptyState>

        <template v-else>
          <el-table :data="filtered">
            <el-table-column label="会话标识" min-width="170">
              <template #default="{ row }">
                <span class="mono conv-key">{{ shortKey(row.session_key) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="渠道" width="112">
              <template #default="{ row }">
                <el-tag size="small" :type="channelType(row.channel)">{{ channelLabel(row.channel) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用户" min-width="110">
              <template #default="{ row }">
                <span class="bc-strong">{{ userName(row.user_id) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="消息数" width="88" align="right">
              <template #default="{ row }">
                <span class="mono bc-strong">{{ row.message_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="96">
              <template #default="{ row }">
                <el-tag size="small" :type="isLive(row) ? 'success' : 'info'">
                  {{ isLive(row) ? '活跃' : '已结束' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近活跃" width="110" align="right">
              <template #default="{ row }">
                <span class="mono bc-muted bc-text-sm">{{ fmtClock(row.last_message_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="168" fixed="right" align="right">
              <template #default="{ row }">
                <el-button size="small" text @click="onViewMessages(row)">消息</el-button>
                <el-button size="small" text @click="onReset(row)">重置</el-button>
                <el-button size="small" text class="op-danger" @click="onDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="table-foot">
            <span>{{ rangeLabel }} · 匹配 {{ filtered.length }} 条会话记录 · 状态按最近 15 分钟活跃判定</span>
            <span class="mono table-foot__num">共 {{ conversations.length }} 个会话</span>
          </div>
        </template>
      </div>
    </template>

    <!-- 消息历史对话框 -->
    <el-dialog v-model="msgDialogVisible" :title="`会话消息历史 · ${currentConv ? shortKey(currentConv.session_key) : ''}`" width="720px">
      <div v-loading="msgLoading" class="msg-list">
        <div v-for="msg in messages" :key="msg.id" class="msg-item">
          <div class="msg-item__header">
            <el-tag :type="roleType(msg.role)" size="small">{{ roleLabel(msg.role) }}</el-tag>
            <span class="msg-item__time mono">{{ fmtClock(msg.created_at) }}</span>
            <span class="msg-item__tokens mono">{{ msg.token_count }} tokens</span>
          </div>
          <div class="msg-item__content">{{ msg.content }}</div>
        </div>
        <EmptyState v-if="!messages.length && !msgLoading" icon="ChatDotRound" title="暂无消息" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

/* ---------------- 状态 ---------------- */
const loading = ref(true)
const conversations = ref([])
const users = ref([])
const range = ref(1) // 天数：1 / 7 / 30
const channelFilter = ref('all')
const statusFilter = ref('all')
const keyword = ref('')

const msgDialogVisible = ref(false)
const msgLoading = ref(false)
const messages = ref([])
const currentConv = ref(null)

const RANGES = [
  { value: 1, label: '今天' },
  { value: 7, label: '近 7 天' },
  { value: 30, label: '近 30 天' },
]

/* ---------------- 派生：渠道选项（按真实数据出现过的渠道生成，不预设） ---------------- */
const CHANNEL_LABELS = { wecom: '企业微信', api: '对话测试', feishu: '飞书' }
const channels = computed(() => {
  const set = new Set(conversations.value.map((c) => c.channel))
  return [...set].map((value) => ({ value, label: CHANNEL_LABELS[value] || value }))
})

/* ---------------- 展示辅助 ---------------- */
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
const isLive = (row) => row.last_message_at && Date.now() - new Date(row.last_message_at).getTime() < 15 * 60 * 1000

const userName = (id) => {
  const u = users.value.find((u) => u.id === id)
  return (u && (u.display_name || u.name)) || `#${id}`
}
const shortKey = (key) => (key?.length > 16 ? `${key.slice(0, 14)}…` : key || '—')

function fmtClock(s) {
  if (!s) return '—'
  const t = new Date(s)
  const sameDay = t.toDateString() === new Date().toDateString()
  const hm = t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return sameDay ? `今天 ${hm}` : `${t.getMonth() + 1}-${String(t.getDate()).padStart(2, '0')} ${hm}`
}

function channelType(ch) {
  return { api: 'info', wecom: 'success', feishu: 'primary' }[ch] || 'info'
}
function channelLabel(ch) {
  return CHANNEL_LABELS[ch] || ch
}
function roleType(r) {
  return { user: 'primary', assistant: 'success', system: 'info' }[r] || 'info'
}
function roleLabel(r) {
  return { user: '用户', assistant: '助手', system: '系统' }[r] || r
}

/* ---------------- 组合过滤（时间 + 渠道 + 状态 + 关键词，全部前端即时生效） ---------------- */
const rangeLabel = computed(() => RANGES.find((r) => r.value === range.value)?.label || '')

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const todayStart = startOfDay(new Date()).getTime()
  return conversations.value.filter((c) => {
    // 时间分段：N 天窗口（今天 = 当日 00:00 起）
    const t = c.last_message_at ? new Date(c.last_message_at).getTime() : 0
    const from = range.value === 1 ? todayStart : Date.now() - range.value * 86400000
    if (t < from) return false
    if (channelFilter.value !== 'all' && c.channel !== channelFilter.value) return false
    if (statusFilter.value === 'active' && !isLive(c)) return false
    if (statusFilter.value === 'closed' && isLive(c)) return false
    if (kw) {
      const hay = `${c.session_key || ''} ${userName(c.user_id)}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
})

function clearFilter() {
  keyword.value = ''
  channelFilter.value = 'all'
  statusFilter.value = 'all'
  range.value = 30
}

/* ---------------- 数据加载 ---------------- */
async function load() {
  loading.value = true
  try {
    const safe = (p, fallback) => p.catch(() => fallback)
    const [convList, userList] = await Promise.all([safe(admin.listConversations(), []), safe(admin.listUsers(), [])])
    conversations.value = convList
    users.value = userList
  } finally {
    loading.value = false
  }
}

/* ---------------- 操作（保留原有确认流） ---------------- */
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
    `确定重置会话 ${shortKey(row.session_key)}？将清空全部消息和摘要，但保留会话本身。`,
    '确认重置',
    { type: 'warning' },
  )
  await admin.resetConversation(row.id)
  ElMessage.success('已重置')
  await load()
}

async function onDelete(row) {
  await ElMessageBox.confirm(
    `确定删除会话 ${shortKey(row.session_key)}？将永久删除全部消息和会话记录（不可恢复）。`,
    '确认删除',
    { type: 'warning' },
  )
  await admin.deleteConversation(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<style scoped>
/* ============ 筛选栏（设计稿：分段控件 + 下拉 + 搜索 + 计数） ============ */
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.segmented {
  display: inline-flex;
  background: var(--bc-bg-sunken);
  border-radius: var(--bc-radius-md);
  padding: 2px;
}
.segmented__btn {
  border: 0;
  background: transparent;
  font: 500 var(--bc-text-xs) / 1 inherit;
  color: var(--bc-text-2);
  padding: 6px 12px;
  border-radius: var(--bc-radius-sm);
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
  font-family: inherit;
}
.segmented__btn.is-on {
  background: var(--bc-bg-surface);
  color: var(--bc-text-1);
  box-shadow: var(--bc-shadow-xs);
}
.toolbar__select {
  width: 128px;
}
.toolbar__search {
  width: 240px;
}
.toolbar__count {
  margin-left: auto;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.toolbar__count b {
  color: var(--bc-text-2);
  font-weight: var(--bc-weight-medium);
  font-variant-numeric: tabular-nums;
}

/* ============ 表格细节 ============ */
.conv-key {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
}
.op-danger:hover {
  color: var(--bc-danger-text, var(--el-color-danger));
}

/* ============ 表脚（设计稿：hairline 上边 + 左右两端信息） ============ */
.table-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  border-top: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-4);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.table-foot__num {
  font-variant-numeric: tabular-nums;
}

/* ============ 消息历史（沿用原结构，令牌对齐） ============ */
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
  color: var(--bc-text-3);
  margin-left: auto;
}
.msg-item__content {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: var(--bc-leading-relaxed);
}

@media (max-width: 768px) {
  .toolbar__search {
    width: 100%;
    order: 5;
  }
  .toolbar__count {
    margin-left: 0;
    width: 100%;
  }
}
</style>
