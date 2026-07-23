<template>
  <!-- 治理动态：近 7 天审计决策柱状图（纯 CSS bar）+ 放行率概览 -->
  <div class="audit-chart bc-panel">
    <div class="audit-chart__head">
      <div class="bc-kv">
        <span class="bc-kv__label">近 7 天放行率</span>
        <span class="audit-chart__rate">
          {{ answeredRate }}<small>%</small>
        </span>
      </div>
      <div class="audit-chart__legend">
        <span class="audit-chart__legend-item">
          <i class="audit-chart__swatch audit-chart__swatch--answered" aria-hidden="true" />已回答
        </span>
        <span class="audit-chart__legend-item">
          <i class="audit-chart__swatch audit-chart__swatch--denied" aria-hidden="true" />已拒绝
        </span>
        <span class="audit-chart__legend-item">
          <i class="audit-chart__swatch audit-chart__swatch--error" aria-hidden="true" />错误
        </span>
      </div>
    </div>

    <div v-if="total === 0" class="audit-chart__empty">
      <span class="bc-muted bc-text-sm">近 7 天暂无审计记录</span>
    </div>
    <div v-else class="audit-chart__bars" role="img" :aria-label="`近7天共${total}条审计记录，放行率${answeredRate}%`">
      <div v-for="d in days" :key="d.key" class="audit-chart__col">
        <div class="audit-chart__stack">
          <div
            v-if="d.error"
            class="audit-chart__seg audit-chart__seg--error"
            :style="{ height: pct(d.error) }"
            :title="`${d.label} 错误 ${d.error} 条`"
          />
          <div
            v-if="d.denied"
            class="audit-chart__seg audit-chart__seg--denied"
            :style="{ height: pct(d.denied) }"
            :title="`${d.label} 拒绝 ${d.denied} 条`"
          />
          <div
            v-if="d.answered"
            class="audit-chart__seg audit-chart__seg--answered"
            :style="{ height: pct(d.answered) }"
            :title="`${d.label} 回答 ${d.answered} 条`"
          />
          <div v-if="!d.total" class="audit-chart__seg audit-chart__seg--none" />
        </div>
        <span class="audit-chart__day">{{ d.label }}</span>
        <span class="audit-chart__count">{{ d.total || '' }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

// 审计日志 → 近 7 天按日聚合（answered/denied/error 三类）
const props = defineProps({
  logs: { type: Array, default: () => [] },
})

const days = computed(() => {
  const buckets = []
  const now = new Date()
  for (let i = 6; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(now.getDate() - i)
    const key = `${d.getMonth() + 1}/${d.getDate()}`
    buckets.push({ key, label: key, answered: 0, denied: 0, error: 0, total: 0 })
  }
  const start = new Date(now)
  start.setDate(now.getDate() - 6)
  start.setHours(0, 0, 0, 0)

  for (const log of props.logs) {
    const t = new Date(log.created_at)
    if (Number.isNaN(t.getTime()) || t < start) continue
    const diff = Math.floor((now - t) / 86400000)
    const idx = 6 - diff
    if (idx < 0 || idx > 6) continue
    const b = buckets[idx]
    if (log.decision === 'answered') b.answered += 1
    else if (log.decision === 'error') b.error += 1
    else b.denied += 1
    b.total += 1
  }
  return buckets
})

const total = computed(() => days.value.reduce((s, d) => s + d.total, 0))
const answered = computed(() => days.value.reduce((s, d) => s + d.answered, 0))
const answeredRate = computed(() =>
  total.value ? Math.round((answered.value / total.value) * 100) : 0,
)
const maxDay = computed(() => Math.max(1, ...days.value.map((d) => d.total)))
const pct = (n) => `${Math.max(4, (n / maxDay.value) * 100)}%`
</script>

<style scoped>
.audit-chart {
  padding: var(--bc-space-5) var(--bc-space-6);
}
.audit-chart__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: var(--bc-space-5);
}
.audit-chart__rate {
  font-size: var(--bc-text-3xl);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.audit-chart__rate small {
  font-size: var(--bc-text-md);
  color: var(--bc-text-3);
  font-weight: var(--bc-weight-regular);
  margin-left: 2px;
}
.audit-chart__legend {
  display: flex;
  gap: var(--bc-space-4);
}
.audit-chart__legend-item {
  display: inline-flex;
  align-items: center;
  gap: var(--bc-space-1);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.audit-chart__swatch {
  width: 10px;
  height: 10px;
  border-radius: var(--bc-radius-sm);
  display: inline-block;
}
.audit-chart__swatch--answered { background: var(--bc-primary-5); }
.audit-chart__swatch--denied { background: var(--bc-gray-4); }
.audit-chart__swatch--error { background: var(--bc-danger); }

.audit-chart__bars {
  display: flex;
  gap: var(--bc-space-3);
  align-items: flex-end;
}
.audit-chart__col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--bc-space-1);
  min-width: 0;
}
.audit-chart__stack {
  width: 100%;
  max-width: 44px;
  height: 120px;
  display: flex;
  flex-direction: column-reverse;
  justify-content: flex-start;
  border-radius: var(--bc-radius-sm);
  overflow: hidden;
  background: var(--bc-gray-2);
}
.audit-chart__seg {
  width: 100%;
  transition: height var(--bc-duration-normal) var(--bc-ease-out);
}
.audit-chart__seg--answered { background: var(--bc-primary-5); }
.audit-chart__seg--denied { background: var(--bc-gray-4); }
.audit-chart__seg--error { background: var(--bc-danger); }
.audit-chart__seg--none { height: 3px; background: transparent; }
.audit-chart__day {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.audit-chart__count {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
  font-variant-numeric: tabular-nums;
  min-height: 14px;
}
.audit-chart__empty {
  padding: var(--bc-space-10) 0;
  text-align: center;
}
</style>
