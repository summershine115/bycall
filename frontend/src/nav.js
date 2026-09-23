// 控制台导航模型（ADR-0014：导航精简合并）。
// 唯一真相源：Layout（侧边栏 + 面包屑）、Hub（页内 tab 容器）、router（路由与旧路径重定向）
// 都读这里——避免"侧边栏改了、路由忘了"的漂移。
import { defineAsyncComponent } from 'vue'
import {
  Avatar,
  ChatLineRound,
  Collection,
  Cpu,
  DataLine,
  DocumentChecked,
  Key,
  TrendCharts,
} from '@element-plus/icons-vue'

// 首页位：不归任何分组，固定排在品牌之下（仪表盘是"回首页"，不是一个功能区）
export const homeItem = { name: 'dashboard', label: '仪表盘', icon: DataLine }

// 侧边栏分组：item.name 即路由 name；matchPrefix 用于"详情页也算本项激活"（如 /agents/:id）
export const navGroups = [
  {
    key: 'identity',
    label: '组织与身份',
    items: [{ name: 'org', label: '组织与权限', icon: Key }],
  },
  {
    key: 'agents',
    label: '数字员工',
    items: [{ name: 'agents', label: '数字员工', icon: Avatar, matchPrefix: '/agents' }],
  },
  {
    key: 'assets',
    label: '能力资产',
    items: [
      { name: 'assets', label: '能力资产', icon: Cpu },
      { name: 'knowledge-hub', label: '知识库', icon: Collection },
    ],
  },
  {
    key: 'runtime',
    label: '运行与监督',
    items: [
      { name: 'runtime', label: '运行记录', icon: ChatLineRound },
      { name: 'approvals', label: '审批队列', icon: DocumentChecked },
      { name: 'supervision', label: '审计与指标', icon: TrendCharts },
    ],
  },
]

// Hub：一级页面内的 tab 容器（同源页面合并）。
// component 一律懒加载：切到该 tab 才拉对应 chunk，首个 tab 之外不产生多余请求。
export const hubs = {
  org: {
    path: '/org',
    title: '组织与权限',
    tabs: [
      { key: 'users', label: '用户管理', component: defineAsyncComponent(() => import('./views/Users.vue')) },
      { key: 'roles', label: '角色与权限', component: defineAsyncComponent(() => import('./views/Roles.vue')) },
      { key: 'policy', label: '策略模拟', component: defineAsyncComponent(() => import('./views/PolicySimulator.vue')) },
    ],
  },
  agents: {
    path: '/agents',
    title: '数字员工',
    tabs: [
      { key: 'instances', label: '实例列表', component: defineAsyncComponent(() => import('./views/AgentInstances.vue')) },
      { key: 'channels', label: '渠道接入', component: defineAsyncComponent(() => import('./views/Channels.vue')) },
    ],
  },
  assets: {
    path: '/assets',
    title: '能力资产',
    tabs: [
      { key: 'prompts', label: '提示词版本', component: defineAsyncComponent(() => import('./views/Prompts.vue')) },
      { key: 'models', label: '模型管理', component: defineAsyncComponent(() => import('./views/Models.vue')) },
      { key: 'skills', label: 'Skill 管理', component: defineAsyncComponent(() => import('./views/Skills.vue')) },
      { key: 'mcp', label: 'MCP 工具', component: defineAsyncComponent(() => import('./views/MCP.vue')) },
      { key: 'releases', label: '发布与回滚', component: defineAsyncComponent(() => import('./views/Releases.vue')) },
    ],
  },
  'knowledge-hub': {
    path: '/knowledge',
    title: '知识库',
    tabs: [
      { key: 'bases', label: '知识库', component: defineAsyncComponent(() => import('./views/KnowledgeBases.vue')) },
      { key: 'docs', label: '文档管理', component: defineAsyncComponent(() => import('./views/Knowledge.vue')) },
    ],
    // 旧深链 /knowledge?kb_id=N（合并前的"文档管理"页）落到文档 tab，不丢上下文
    defaultTab: (query) => (query.kb_id ? 'docs' : 'bases'),
  },
  runtime: {
    path: '/runtime',
    title: '运行记录',
    tabs: [
      { key: 'conversations', label: '会话管理', component: defineAsyncComponent(() => import('./views/Conversations.vue')) },
      { key: 'tasks', label: '任务执行', component: defineAsyncComponent(() => import('./views/Tasks.vue')) },
    ],
  },
  supervision: {
    path: '/audit',
    title: '审计与指标',
    tabs: [
      { key: 'audit', label: '审计日志', component: defineAsyncComponent(() => import('./views/Audit.vue')) },
      { key: 'metrics', label: '运营指标', component: defineAsyncComponent(() => import('./views/OpsMetrics.vue')) },
    ],
  },
}

// 非导航页的分组归属：仅供面包屑（侧边栏已无入口，如顶栏齿轮进入的运行时设置）
export const crumbGroups = {
  settings: '系统',
  chat: '数字员工',
  'agent-detail': '数字员工',
}

// 当前生效 tab：显式 ?tab= 优先（且必须是已声明的 key），否则用 Hub 的 defaultTab 兜底
export function resolveTab(hub, query = {}) {
  if (hub.tabs.some((t) => t.key === query.tab)) return query.tab
  return hub.defaultTab ? hub.defaultTab(query) : (hub.tabs[0]?.key ?? '')
}
