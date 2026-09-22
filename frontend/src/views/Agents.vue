<template>
  <div class="bc-page">
    <PageHeader
      title="数字员工"
      description="实例、装配资产与渠道接入的统一工作台：人格 / 模型 / 技能 / 知识 / 专属连接均围绕实例装配，会话按 实例×用户×渠道 独立隔离。"
    />

    <!-- 子页即 tab：URL 可深链，切 tab = 路由跳转 -->
    <el-tabs :model-value="activeTab" class="hub-tabs" @tab-change="onTabChange">
      <el-tab-pane v-for="t in tabs" :key="t.name" :label="t.label" :name="t.name" />
    </el-tabs>

    <router-view v-slot="{ Component }">
      <transition name="page" mode="out-in">
        <component :is="Component" :key="route.fullPath" />
      </transition>
    </router-view>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'

const route = useRoute()
const router = useRouter()

const tabs = [
  { name: 'agents', label: '实例' },
  { name: 'channels', label: '渠道接入' },
  { name: 'prompts', label: '提示词版本' },
  { name: 'models', label: '模型管理' },
  { name: 'skills', label: 'Skill 管理' },
  { name: 'mcp', label: 'MCP 工具' },
  { name: 'knowledge-bases', label: '知识库' },
  { name: 'knowledge', label: '文档管理' },
]

const activeTab = computed(() => (tabs.some((t) => t.name === route.name) ? route.name : 'agents'))

function onTabChange(name) {
  if (name !== route.name) router.push({ name })
}
</script>

<style scoped>
.hub-tabs {
  margin-bottom: var(--bc-space-4);
}
.hub-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

/* 内层子页切换动画：与 Layout 的页面过渡同款（scoped 可命中子页根元素） */
.page-enter-active {
  transition:
    opacity var(--bc-duration-normal) var(--bc-ease-out),
    transform var(--bc-duration-normal) var(--bc-ease-out);
}
.page-leave-active {
  transition: opacity calc(var(--bc-duration-normal) / 2) var(--bc-ease-out);
}
.page-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.page-leave-to {
  opacity: 0;
}
</style>
