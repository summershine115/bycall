<template>
  <el-config-provider :locale="zhCn">
    <router-view v-slot="{ Component }">
      <component :is="Component" />
    </router-view>
  </el-config-provider>
</template>

<script setup>
// 根组件：router-view 出口 + 窄屏表格堆叠标签注入。
import { onMounted, onBeforeUnmount } from 'vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

// 窄屏下表格退化为堆叠行（见 styles/element-overrides.css），
// 此处把每列表头文案写入对应单元格的 data-label，供 ::before 呈现列名。
// 优化：observer 收窄到 #app（EP 弹窗 teleport 到 body，不在 .bc-panel 内），
// 并用 rAF 合并同一帧内的多次 mutation 回调。
let observer = null
let rafId = null

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
function scheduleRelabel() {
  if (rafId) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    relabelAll()
  })
}

onMounted(() => {
  relabelAll()
  observer = new MutationObserver(scheduleRelabel)
  observer.observe(document.getElementById('app'), { childList: true, subtree: true })
})
onBeforeUnmount(() => {
  observer?.disconnect()
  if (rafId) cancelAnimationFrame(rafId)
})
</script>

<style>
body {
  margin: 0;
  font-family: var(--bc-font-family);
}
</style>
