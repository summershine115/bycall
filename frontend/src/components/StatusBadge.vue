<template>
  <!-- 状态徽标：点 + 文字（状态不只靠颜色传达，满足可访问性）。 -->
  <span class="status-badge" :class="`status-badge--${type}`">
    <span class="status-badge__dot" aria-hidden="true" />
    <span class="status-badge__text"><slot>{{ text }}</slot></span>
  </span>
</template>

<script setup>
// 状态徽标：ok/warn/err/idle 四态，点+文字双编码
defineProps({
  type: { type: String, default: 'idle', validator: (v) => ['ok', 'warn', 'err', 'idle'].includes(v) },
  text: { type: String, default: '' },
})
</script>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-sm);
  line-height: 1.4;
}
.status-badge__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--bc-radius-full);
  flex-shrink: 0;
}
.status-badge--ok .status-badge__dot {
  background: var(--bc-success);
  box-shadow: 0 0 0 3px var(--bc-success-bg);
}
.status-badge--ok .status-badge__text {
  color: var(--bc-success);
}
.status-badge--warn .status-badge__dot {
  background: var(--bc-warning);
  box-shadow: 0 0 0 3px var(--bc-warning-bg);
}
.status-badge--warn .status-badge__text {
  color: var(--bc-warning);
}
.status-badge--err .status-badge__dot {
  background: var(--bc-danger);
  box-shadow: 0 0 0 3px var(--bc-danger-bg);
}
.status-badge--err .status-badge__text {
  color: var(--bc-danger);
}
.status-badge--idle .status-badge__dot {
  background: var(--bc-gray-4);
}
.status-badge--idle .status-badge__text {
  color: var(--bc-text-3);
}
</style>
