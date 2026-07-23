<template>
  <!-- 仪表盘区块容器：标题 + 右侧操作 + 内容插槽 -->
  <section class="dash-section" :aria-labelledby="`ds-${uid}`">
    <header class="dash-section__head">
      <h2 :id="`ds-${uid}`" class="dash-section__title">{{ title }}</h2>
      <div v-if="$slots.extra" class="dash-section__extra">
        <slot name="extra" />
      </div>
    </header>
    <slot />
  </section>
</template>

<script setup>
// 仪表盘区块容器：统一各版块标题与间距
let seed = 0
const uid = `s${++seed}-${Math.random().toString(36).slice(2, 6)}`
defineProps({ title: { type: String, required: true } })
</script>

<style scoped>
.dash-section {
  margin-top: var(--bc-space-8);
}
.dash-section__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--bc-space-4);
}
.dash-section__title {
  font-size: var(--bc-text-lg);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.dash-section__extra {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
</style>
