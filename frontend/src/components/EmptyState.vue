<template>
  <!-- 空状态：引导用户下一步，而不是只说"没有数据"。 -->
  <div class="empty-state" role="status">
    <div class="empty-state__icon" aria-hidden="true">
      <el-icon :size="22"><component :is="icon" /></el-icon>
    </div>
    <p class="empty-state__title">{{ title }}</p>
    <p v-if="description" class="empty-state__desc">{{ description }}</p>
    <div v-if="$slots.default" class="empty-state__actions">
      <slot />
    </div>
  </div>
</template>

<script setup>
// 空状态公共组件：icon/title/description + 操作插槽（通常放"新建"按钮）
defineProps({
  icon: { type: String, default: 'Box' },
  title: { type: String, default: '暂无数据' },
  description: { type: String, default: '' },
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--bc-space-16) var(--bc-space-6);
  text-align: center;
}
.empty-state__icon {
  width: 48px;
  height: 48px;
  border-radius: var(--bc-radius-lg);
  background: var(--bc-gray-2);
  color: var(--bc-text-4);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: var(--bc-space-4);
}
.empty-state__title {
  margin: 0;
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-1);
}
.empty-state__desc {
  margin: var(--bc-space-2) 0 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  max-width: 400px;
  line-height: var(--bc-leading-relaxed);
}
.empty-state__actions {
  margin-top: var(--bc-space-5);
}
</style>
