<template>
  <!-- 统计卡：大数字 + 标签 + 可选趋势/脚注。左竖线点缀，克制不花哨。 -->
  <div class="stat-card" :class="{ 'stat-card--link': clickable }" @click="clickable && $emit('click')">
    <div class="stat-card__accent" :style="{ background: accentColor }" aria-hidden="true" />
    <div class="stat-card__body">
      <div class="stat-card__label">{{ label }}</div>
      <div class="stat-card__value">
        <span class="stat-card__num">{{ displayValue }}</span>
        <span v-if="unit" class="stat-card__unit">{{ unit }}</span>
      </div>
      <div v-if="hint" class="stat-card__hint">
        <el-icon v-if="hintIcon" :size="12" aria-hidden="true"><component :is="hintIcon" /></el-icon>
        {{ hint }}
      </div>
    </div>
    <el-icon v-if="icon" class="stat-card__icon" :size="20" aria-hidden="true">
      <component :is="icon" />
    </el-icon>
  </div>
</template>

<script setup>
import { computed } from 'vue'

// 统计卡公共组件。accent 语义：primary/success/warning/danger/info
const props = defineProps({
  label: { type: String, required: true },
  value: { type: [Number, String], default: '—' },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
  hintIcon: { type: String, default: '' },
  icon: { type: String, default: '' },
  accent: { type: String, default: 'primary' },
  clickable: { type: Boolean, default: false },
})
defineEmits(['click'])

const accentColor = computed(
  () =>
    ({
      primary: 'var(--bc-primary-6)',
      success: 'var(--bc-success)',
      warning: 'var(--bc-warning)',
      danger: 'var(--bc-danger)',
      info: 'var(--bc-gray-5)',
    })[props.accent] || 'var(--bc-primary-6)',
)

const displayValue = computed(() => {
  if (typeof props.value === 'number') return props.value.toLocaleString('zh-CN')
  return props.value
})
</script>

<style scoped>
.stat-card {
  position: relative;
  display: flex;
  align-items: flex-start;
  background: var(--bc-bg-surface);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-lg);
  padding: var(--bc-space-5);
  box-shadow: var(--bc-shadow-xs);
  transition:
    box-shadow var(--bc-duration-fast) var(--bc-ease-out),
    border-color var(--bc-duration-fast) var(--bc-ease-out);
  overflow: hidden;
}
.stat-card--link {
  cursor: pointer;
}
.stat-card--link:hover {
  border-color: var(--bc-border-strong);
  box-shadow: var(--bc-shadow-sm);
}
.stat-card__accent {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
}
.stat-card__body {
  flex: 1;
  min-width: 0;
}
.stat-card__label {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  font-weight: var(--bc-weight-medium);
  letter-spacing: 0.02em;
}
.stat-card__value {
  margin-top: var(--bc-space-2);
  display: flex;
  align-items: baseline;
  gap: var(--bc-space-1);
}
.stat-card__num {
  font-size: var(--bc-text-3xl);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.stat-card__unit {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
}
.stat-card__hint {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
}
.stat-card__icon {
  color: var(--bc-text-4);
  flex-shrink: 0;
  margin-top: 2px;
}
</style>
