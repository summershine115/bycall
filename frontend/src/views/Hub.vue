<template>
  <div class="hub">
    <!-- tab 条即一级页面的分区：切 tab 只换 pane（lazy：首次激活才挂载，之后保留），路由层级不变 -->
    <el-tabs v-model="activeKey" class="hub__tabs">
      <el-tab-pane v-for="tab in hub.tabs" :key="tab.key" :label="tab.label" :name="tab.key" lazy>
        <component :is="tab.component" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
// 通用 tab 容器（ADR-0014）：把同源页面合并成一个一级页面，定义见 src/nav.js。
// 向子树提供 hubMode：页头据此去掉与 tab 名重复的标题，只保留描述与操作区。
import { computed, provide, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { hubs, resolveTab } from '../nav'

const route = useRoute()
const router = useRouter()

const hub = computed(() => hubs[route.name] ?? { tabs: [], title: '' })

provide('bcHubMode', true)

// tab 与地址栏双向绑定：?tab= 即页面状态，链接可复制、可分享
const activeKey = computed({
  get: () => resolveTab(hub.value, route.query),
  set: (key) => replaceTab(key),
})

function replaceTab(key) {
  // replace：tab 是同一页面的视图模式，不占浏览历史（返回键仍回到上一个页面）
  if (route.query.tab === key || !key) return
  router.replace({ name: route.name, query: { ...route.query, tab: key } })
}

// 归一化：缺失/非法的 ?tab=（含旧深链）写回实际生效项，避免地址栏与实际内容不一致
watch(
  () => [route.name, route.query.tab, route.query.kb_id],
  () => {
    if (route.query.tab !== activeKey.value) replaceTab(activeKey.value)
  },
  { immediate: true }
)
</script>

<style scoped>
.hub {
  max-width: var(--bc-content-max);
  margin: 0 auto;
}
/* tab 条与页面内容同一左基线：抵消 el-tabs 默认的左侧留白 */
.hub__tabs {
  margin-bottom: var(--bc-space-2);
}
.hub__tabs :deep(.el-tabs__header) {
  margin-bottom: var(--bc-space-6);
}
</style>
