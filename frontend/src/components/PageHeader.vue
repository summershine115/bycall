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
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--bc-space-6);
  /* hero 头部：充足下方留白，撑起整页的第一视觉层级 */
  margin-bottom: var(--bc-space-10);
}
.page-header__main {
  min-width: 0;
}
.page-header__title {
  font-size: var(--bc-text-3xl);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  letter-spacing: -0.02em;
  line-height: 1.15;
}
.page-header__desc {
  margin: var(--bc-space-3) 0 0;
  font-size: var(--bc-text-md);
  color: var(--bc-text-3);
  line-height: var(--bc-leading-relaxed);
  max-width: 620px;
}
.page-header__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-shrink: 0;
  padding-bottom: var(--bc-space-1);
}
@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--bc-space-4);
  }
  .page-header__actions {
    padding-bottom: 0;
  }
}
</style>
