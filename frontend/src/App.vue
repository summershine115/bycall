<template>
  <router-view v-slot="{ Component }">
    <component :is="Component" />
  </router-view>
</template>

<script setup>
// 根组件：router-view 出口 + 窄屏表格堆叠标签注入。
import { onMounted, onBeforeUnmount } from 'vue'

// 窄屏下表格退化为堆叠行（见 styles/element-overrides.css），
// 此处把每列表头文案写入对应单元格的 data-label，供 ::before 呈现列名。
let observer = null

function labelTable(table) {
  const heads = [...table.querySelectorAll('thead th .cell')].map((c) => c.textContent.trim())
  if (!heads.length) return
  for (const tr of table.querySelectorAll('tbody tr')) {
    ;[...tr.querySelectorAll('td > .cell')].forEach((cell, i) => {
      cell.dataset.label = heads[i] || ''
    })
  }
}
function relabelAll() {
  document.querySelectorAll('.bc-panel .el-table').forEach(labelTable)
}

onMounted(() => {
  relabelAll()
  observer = new MutationObserver(() => relabelAll())
  observer.observe(document.body, { childList: true, subtree: true })
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<style>
body {
  margin: 0;
  font-family: var(--bc-font-family);
}
</style>
