<template>
  <!-- 页面头部：标题 + 描述 + 右侧操作区。所有页面统一的第一视觉层级。
       Hub 内（tab 容器，见 ADR-0014）标题与 tab 名重复，故省略标题、只留描述与操作区。 -->
  <header class="page-header" :class="{ 'page-header--in-hub': inHub }">
    <div class="page-header__main">
      <h1 v-if="!inHub" class="page-header__title">{{ title }}</h1>
      <p v-if="description" class="page-header__desc">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="page-header__actions">
      <slot />
    </div>
  </header>
</template>

<script setup>
// 页面头部公共组件：统一标题层级与操作区布局
import { inject } from 'vue'

defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
})

// Hub 上下文：tab 容器内的页面不再重复渲染标题（tab 名即标题）
const inHub = inject('bcHubMode', false)
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
/* Hub 内：tab 条已占据视觉首层，这里降为「描述 + 操作」一行，与各页 .page-head 同节奏 */
.page-header--in-hub {
  align-items: center;
  margin-bottom: var(--bc-space-4);
}
.page-header--in-hub .page-header__desc {
  margin-top: 0;
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-normal, 1.5);
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
