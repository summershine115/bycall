<template>
  <div class="layout">
    <!-- ============ 侧边栏 ============ -->
    <aside class="sidebar" :class="{ 'sidebar--collapsed': collapsed }">
      <div class="sidebar__brand">
        <div class="sidebar__logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
            <rect x="2" y="2" width="20" height="20" rx="5" fill="currentColor" opacity="0.14" />
            <path
              d="M7 15.5V8.5h2.2v2.6l3.4-2.6h2.9l-3.9 3 4.1 4h-2.9l-2.8-2.9-1 0.8v2.1H7z"
              fill="currentColor"
            />
          </svg>
        </div>
        <transition name="fade">
          <div v-if="!collapsed" class="sidebar__brand-text">
            <span class="sidebar__name">Bycall</span>
            <span class="sidebar__sub">数字员工治理控制台</span>
          </div>
        </transition>
      </div>

      <nav class="sidebar__nav" aria-label="主导航">
        <div v-for="group in navGroups" :key="group.key" class="nav-group">
          <div v-if="!collapsed" class="nav-group__label">{{ group.label }}</div>
          <div v-else class="nav-group__divider" aria-hidden="true" />
          <router-link
            v-for="item in group.items"
            :key="item.name"
            :to="{ name: item.name }"
            class="nav-item"
            :class="{ 'nav-item--active': isItemActive(item) }"
            :title="collapsed ? item.label : ''"
            :aria-current="route.name === item.name ? 'page' : undefined"
          >
            <el-icon :size="17" class="nav-item__icon" aria-hidden="true">
              <component :is="item.icon" />
            </el-icon>
            <span v-if="!collapsed" class="nav-item__label">{{ item.label }}</span>
          </router-link>
        </div>
      </nav>

      <div class="sidebar__footer">
        <button
          class="sidebar__collapse"
          type="button"
          :aria-label="collapsed ? '展开侧边栏' : '收起侧边栏'"
          @click="collapsed = !collapsed"
        >
          <el-icon :size="16" aria-hidden="true">
            <Fold v-if="!collapsed" />
            <Expand v-else />
          </el-icon>
          <span v-if="!collapsed">收起导航</span>
        </button>
      </div>
    </aside>

    <!-- ============ 主区 ============ -->
    <div class="layout__main">
      <header class="topbar">
        <div class="topbar__crumb">
          <span class="topbar__group">{{ currentGroupLabel }}</span>
          <el-icon :size="12" class="topbar__sep" aria-hidden="true"><ArrowRight /></el-icon>
          <span class="topbar__page">{{ pageTitle }}</span>
        </div>
        <div class="topbar__actions">
          <!-- 明暗切换：light ⇄ dark（手动切换覆盖 auto 并持久化） -->
          <ModeToggle />
          <span class="topbar__env">{{ envLabel }}</span>
          <el-dropdown trigger="click" @command="onUserCommand">
            <button class="topbar__user" type="button" aria-label="账户菜单">
              <span class="topbar__avatar" aria-hidden="true">A</span>
              <span class="topbar__username">管理员</span>
              <el-icon :size="12" aria-hidden="true"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">
                  <el-icon aria-hidden="true"><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="content">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <!-- key 用一级路由记录：Hub 内切子 tab 时外层壳不重建，过渡交给内层 router-view -->
            <component :is="Component" :key="route.matched[1]?.path ?? route.fullPath" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { auth } from '../api'
import ModeToggle from '../components/ModeToggle.vue'
import {
  ArrowDown, ArrowRight, Avatar, ChatLineRound,
  DataLine, DocumentChecked, Expand, Fold, Key, List,
  RefreshLeft, Setting, SwitchButton, TrendCharts, User,
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

// 导航分组（数字员工 Hub 重组）：实例、渠道接入与装配资产（提示词/模型/技能/MCP/知识）
// 统一收进「数字员工」一个入口（/agents 嵌套子页），侧边栏只留 Hub 一项；
// 渠道接入不再是独立系统页（ADR-0012 修订），对话测试从实例行内进入。
const navGroups = [
  {
    key: 'overview',
    label: '总览',
    items: [
      { name: 'dashboard', label: '仪表盘', icon: DataLine },
      { name: 'settings', label: '运行时设置', icon: Setting },
    ],
  },
  {
    key: 'identity',
    label: '组织与身份',
    items: [
      { name: 'users', label: '用户管理', icon: User },
      { name: 'roles', label: '角色与权限', icon: Key },
      { name: 'policy-simulator', label: '策略模拟', icon: List },
    ],
  },
  {
    key: 'agent',
    label: '数字员工',
    items: [
      // Hub 及其全部子页（/agents/*）都归属这一项：active 用路径前缀匹配
      { name: 'agents', label: '数字员工', icon: Avatar, matchPrefix: '/agents' },
    ],
  },
  {
    key: 'runtime',
    label: '运行与监督',
    items: [
      { name: 'conversations', label: '会话管理', icon: ChatLineRound },
      { name: 'approvals', label: '审批队列', icon: DocumentChecked },
      { name: 'tasks', label: '任务执行', icon: List },
      { name: 'audit', label: '审计日志', icon: List },
      { name: 'releases', label: '发布与回滚', icon: RefreshLeft },
      { name: 'ops-metrics', label: '运营指标', icon: TrendCharts },
    ],
  },
]

// 路由 name → 中文标题（与分组解耦，路由不变）
const titles = {
  dashboard: '仪表盘',
  users: '用户管理',
  roles: '角色与权限',
  agents: '实例',
  'agent-detail': '数字员工详情',
  models: '模型管理',
  knowledge: '文档管理',
  'knowledge-bases': '知识库',
  conversations: '会话管理',
  prompts: '提示词版本',
  skills: 'Skill 管理',
  mcp: 'MCP 工具',
  approvals: '审批队列',
  tasks: '任务执行',
  channels: '渠道接入',
  settings: '运行时设置',
  audit: '审计日志',
  'policy-simulator': '策略模拟',
  releases: '发布与回滚',
  'ops-metrics': '运营指标',
  chat: '对话测试',
}

const pageTitle = computed(() => titles[route.name] || 'Bycall')

// Hub 子页（/agents/channels 等）路由 name 不在 navGroups 里，
// 用路径前缀把整组子路由归到「数字员工」一项上
function isItemActive(item) {
  return item.matchPrefix ? route.path.startsWith(item.matchPrefix) : route.name === item.name
}
const currentGroupLabel = computed(
  () => navGroups.find((g) => g.items.some(isItemActive))?.label || ''
)
const envLabel = import.meta.env.DEV ? '开发环境' : '生产环境'

async function onUserCommand(cmd) {
  if (cmd === 'logout') {
    // 注销失败（如会话已过期）也要回登录页，避免卡在无凭证的控制台里
    await auth.logout().catch(() => {})
    router.push('/login')
  }
}
</script>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
  background: var(--bc-bg-page);
}

/* ============ 侧边栏（mono 纯黑锚点） ============ */
.sidebar {
  width: var(--bc-sidebar-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bc-sidebar-bg);
  border-right: 1px solid var(--bc-sidebar-border);
  position: sticky;
  top: 0;
  height: 100vh;
  transition: width var(--bc-duration-normal) var(--bc-ease-out);
  z-index: 20;
}
.sidebar--collapsed {
  width: var(--bc-sidebar-collapsed-width);
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: 0 var(--bc-space-5);
  height: var(--bc-header-height);
  border-bottom: 1px solid var(--bc-sidebar-border);
  flex-shrink: 0;
  overflow: hidden;
}
.sidebar__logo {
  width: 32px;
  height: 32px;
  border-radius: var(--bc-radius-md);
  background: var(--bc-sidebar-text-strong);
  color: var(--bc-sidebar-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sidebar__brand-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  white-space: nowrap;
}
.sidebar__name {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-bold);
  color: var(--bc-sidebar-text-strong);
  letter-spacing: 0.01em;
  line-height: 1.2;
}
.sidebar__sub {
  font-size: var(--bc-text-xs);
  color: var(--bc-sidebar-text);
  line-height: 1.2;
  margin-top: 1px;
}

.sidebar__nav {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--bc-space-4) var(--bc-space-3);
}
.nav-group {
  margin-bottom: var(--bc-space-6);
}
.nav-group:last-child {
  margin-bottom: var(--bc-space-2);
}
.nav-group__label {
  font-size: 11px;
  color: var(--bc-sidebar-text);
  opacity: 0.55;
  font-weight: var(--bc-weight-semibold);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: var(--bc-space-2) var(--bc-space-3) var(--bc-space-1);
  user-select: none;
}
.nav-group__divider {
  height: 1px;
  background: var(--bc-sidebar-border);
  margin: var(--bc-space-2) var(--bc-space-2);
}
.nav-item {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-3);
  border-radius: var(--bc-radius-md);
  color: var(--bc-sidebar-text);
  text-decoration: none;
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  line-height: 20px;
  margin-bottom: 2px;
  transition:
    background var(--bc-duration-fast) var(--bc-ease-out),
    color var(--bc-duration-fast) var(--bc-ease-out);
  white-space: nowrap;
}
.nav-item:hover {
  background: var(--bc-sidebar-hover);
  color: var(--bc-sidebar-text-strong);
}
.nav-item--active {
  background: var(--bc-sidebar-active);
  color: var(--bc-sidebar-text-strong);
}
.nav-item--active .nav-item__icon {
  color: var(--bc-sidebar-text-strong);
}
.nav-item__icon {
  flex-shrink: 0;
  color: var(--bc-sidebar-text);
  transition: color var(--bc-duration-fast) var(--bc-ease-out);
}
.nav-item:hover .nav-item__icon {
  color: var(--bc-sidebar-text-strong);
}
.sidebar--collapsed .nav-item {
  justify-content: center;
  padding: var(--bc-space-2) 0;
}

.sidebar__footer {
  border-top: 1px solid var(--bc-sidebar-border);
  padding: var(--bc-space-2) var(--bc-space-3);
  flex-shrink: 0;
}
.sidebar__collapse {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  width: 100%;
  padding: var(--bc-space-2) var(--bc-space-3);
  border: none;
  background: transparent;
  border-radius: var(--bc-radius-md);
  color: var(--bc-sidebar-text);
  font-size: var(--bc-text-sm);
  cursor: pointer;
  font-family: inherit;
  transition:
    background var(--bc-duration-fast) var(--bc-ease-out),
    color var(--bc-duration-fast) var(--bc-ease-out);
}
.sidebar__collapse:hover {
  background: var(--bc-sidebar-hover);
  color: var(--bc-sidebar-text-strong);
}
.sidebar--collapsed .sidebar__collapse {
  justify-content: center;
}

/* ============ 顶栏 ============ */
.layout__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  height: var(--bc-header-height);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--bc-space-6);
  background: var(--bc-topbar-bg);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  border-bottom: 1px solid var(--bc-border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.topbar__crumb {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-sm);
}
.topbar__group {
  color: var(--bc-text-3);
}
.topbar__sep {
  color: var(--bc-text-4);
}
.topbar__page {
  color: var(--bc-text-1);
  font-weight: var(--bc-weight-semibold);
}
.topbar__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-4);
}
.topbar__env {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  padding: 2px var(--bc-space-2);
  background: var(--bc-gray-2);
  border-radius: var(--bc-radius-sm);
}
.topbar__user {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  border: none;
  background: transparent;
  cursor: pointer;
  font-family: inherit;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  padding: var(--bc-space-1) var(--bc-space-2);
  border-radius: var(--bc-radius-md);
  transition: background var(--bc-duration-fast) var(--bc-ease-out);
}
.topbar__user:hover {
  background: var(--bc-gray-2);
}
.topbar__avatar {
  width: 26px;
  height: 26px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-text-1);
  color: var(--bc-bg-surface);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-semibold);
  display: flex;
  align-items: center;
  justify-content: center;
}

/* ============ 内容区 ============ */
.content {
  flex: 1;
  /* 顶部充足呼吸撑起 hero 头部；左右大留白收束阅读宽度 */
  padding: var(--bc-space-10) var(--bc-space-12) var(--bc-space-16);
  min-width: 0;
}
@media (max-width: 1280px) {
  .content {
    padding: var(--bc-space-8) var(--bc-space-6) var(--bc-space-12);
  }
}
@media (max-width: 768px) {
  .content {
    padding: var(--bc-space-6) var(--bc-space-5) var(--bc-space-10);
  }
}

/* 折叠文案淡入淡出 */
.fade-enter-active {
  transition: opacity var(--bc-duration-normal) var(--bc-ease-out);
}
.fade-enter-from {
  opacity: 0;
}

/* 路由切换：新页面从下方 8px 淡入上移 */
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
