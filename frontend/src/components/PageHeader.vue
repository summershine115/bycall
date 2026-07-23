<template>
  <!-- 页面头部：标题 + 描述 + 右侧操作区。所有页面统一的第一视觉层级。 -->
  <header class="page-header">
    <div class="page-header__main">
      <h1 class="page-header__title">{{ title }}</h1>
      <p v-if="description" class="page-header__desc">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="page-header__actions">
      <slot />
    </div>
  </header>
</template>

<script setup>
// 页面头部公共组件：统一标题层级与操作区布局
defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-6);
}
.page-header__title {
  font-size: var(--bc-text-2xl);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  letter-spacing: -0.01em;
}
.page-header__desc {
  margin: var(--bc-space-2) 0 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  line-height: var(--bc-leading-normal);
  max-width: 640px;
}
.page-header__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-shrink: 0;
  padding-top: var(--bc-space-1);
}
@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
  }
}
</style>
