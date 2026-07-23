<template>
  <!-- 当前激活配置一览：提示词 / 三类模型 / IM 渠道 -->
  <div class="active-config bc-panel">
    <ul class="active-config__list">
      <li class="active-config__item">
        <span class="active-config__label">系统提示词</span>
        <span class="active-config__value">
          <template v-if="prompt">
            <strong>v{{ prompt.version }}</strong>
            <span class="bc-muted">{{ prompt.label || '未命名版本' }}</span>
          </template>
          <span v-else class="bc-muted">未激活</span>
        </span>
      </li>
      <li v-for="m in modelRows" :key="m.type" class="active-config__item">
        <span class="active-config__label">{{ m.label }}</span>
        <span class="active-config__value">
          <template v-if="m.model">
            <strong>{{ m.model.display_name || m.model.name }}</strong>
            <span class="bc-muted">{{ m.model.provider }}</span>
          </template>
          <span v-else class="bc-muted">未配置</span>
        </span>
      </li>
      <li class="active-config__item">
        <span class="active-config__label">IM 渠道</span>
        <span class="active-config__value">
          <StatusBadge :type="imConnected ? 'ok' : 'idle'" :text="imConnected ? '已连接' : '未连接'" />
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'

// 激活配置一览：从仪表盘聚合数据派生
const props = defineProps({
  prompt: { type: Object, default: null },
  models: { type: Array, default: () => [] },
  imStatus: { type: Object, default: null },
})

const MODEL_TYPES = [
  { type: 'chat', label: '对话模型' },
  { type: 'embedding', label: '向量模型' },
  { type: 'rerank', label: '重排模型' },
]

const modelRows = computed(() =>
  MODEL_TYPES.map((t) => ({
    ...t,
    model: props.models.find((m) => m.model_type === t.type && m.is_active) || null,
  })),
)

const imConnected = computed(() => {
  const s = props.imStatus
  if (!s) return false
  return Boolean(s.connected || s.running || s.enabled)
})
</script>

<style scoped>
.active-config__list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.active-config__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-4);
  padding: var(--bc-space-3) var(--bc-space-5);
  border-bottom: 1px solid var(--bc-border);
}
.active-config__item:last-child {
  border-bottom: none;
}
.active-config__label {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  flex-shrink: 0;
}
.active-config__value {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
  min-width: 0;
  text-align: right;
}
.active-config__value .bc-muted {
  font-size: var(--bc-text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
</style>
