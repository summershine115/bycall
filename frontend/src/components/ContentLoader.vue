<template>
  <!-- 内容加载骨架：替代整页转圈，首屏感知更快、更精致。 -->
  <div class="content-loader" aria-busy="true" aria-label="加载中">
    <div v-if="rows" class="content-loader__table">
      <el-skeleton animated>
        <template #template>
          <el-skeleton-item variant="rect" class="content-loader__head" />
          <el-skeleton-item
            v-for="i in rows"
            :key="i"
            variant="rect"
            class="content-loader__row"
          />
        </template>
      </el-skeleton>
    </div>
    <div v-else class="content-loader__cards">
      <el-skeleton v-for="i in cards" :key="i" animated class="content-loader__card">
        <template #template>
          <el-skeleton-item variant="rect" class="content-loader__block" />
        </template>
      </el-skeleton>
    </div>
  </div>
</template>

<script setup>
// 加载骨架公共组件：rows=N 渲染表格骨架；否则渲染 N 张卡片骨架
defineProps({
  rows: { type: Number, default: 0 },
  cards: { type: Number, default: 3 },
})
</script>

<style scoped>
.content-loader__table,
.content-loader__cards {
  background: var(--bc-bg-surface);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-lg);
  padding: var(--bc-space-5);
}
.content-loader__cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--bc-space-4);
  border: none;
  background: transparent;
  padding: 0;
}
.content-loader__head {
  height: 36px;
  margin-bottom: var(--bc-space-3);
  border-radius: var(--bc-radius-md);
}
.content-loader__row {
  height: 44px;
  margin-bottom: var(--bc-space-2);
  border-radius: var(--bc-radius-md);
}
.content-loader__row:last-child {
  margin-bottom: 0;
}
.content-loader__block {
  height: 120px;
  border-radius: var(--bc-radius-lg);
}
</style>
