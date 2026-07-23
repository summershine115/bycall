<template>
  <!-- 近期审计动态：最新 N 条记录，紧凑列表 -->
  <div class="recent-audit bc-panel">
    <div v-if="!logs.length" class="recent-audit__empty bc-muted bc-text-sm">
      暂无审计记录
    </div>
    <ul v-else class="recent-audit__list">
      <li v-for="log in logs" :key="log.id ?? log.created_at" class="recent-audit__item">
        <el-tag :type="decisionType(log.decision)" size="small" class="recent-audit__tag">
          {{ decisionLabel(log.decision) }}
        </el-tag>
        <div class="recent-audit__main">
          <span class="recent-audit__q">{{ log.question || log.detail || '—' }}</span>
          <span class="recent-audit__meta">
            {{ log.user_name || '未知用户' }} · {{ fmtTime(log.created_at) }}
          </span>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
// 近期审计动态列表（决策标签 + 问题摘要 + 人/时间）
defineProps({
  logs: { type: Array, default: () => [] },
})

function decisionType(d) {
  if (d === 'answered') return 'success'
  if (d?.startsWith('denied')) return 'danger'
  if (d === 'error') return 'warning'
  return 'info'
}
function decisionLabel(d) {
  if (d === 'answered') return '已回答'
  if (d === 'denied_no_access') return '无权限'
  if (d?.startsWith('denied')) return '已拒绝'
  if (d === 'error') return '错误'
  return d || '未知'
}
function fmtTime(s) {
  if (!s) return ''
  const t = new Date(s)
  const now = new Date()
  const sameDay = t.toDateString() === now.toDateString()
  return sameDay
    ? t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : t.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.recent-audit__list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.recent-audit__item {
  display: flex;
  align-items: flex-start;
  gap: var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-5);
  border-bottom: 1px solid var(--bc-border);
}
.recent-audit__item:last-child {
  border-bottom: none;
}
.recent-audit__tag {
  flex-shrink: 0;
  margin-top: 2px;
}
.recent-audit__main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.recent-audit__q {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recent-audit__meta {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
}
.recent-audit__empty {
  padding: var(--bc-space-8);
  text-align: center;
}
</style>
