import { createRouter, createWebHashHistory } from 'vue-router'

// 路由：hash 模式（FastAPI 托管时无需 SPA fallback 特殊处理）。
// 守卫：未登录 → 跳登录页（除登录页自身）。
const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
      { path: 'users', name: 'users', component: () => import('./views/Users.vue') },
      { path: 'roles', name: 'roles', component: () => import('./views/Roles.vue') },
      // 数字员工 Hub：实例 + 装配资产（提示词/模型/技能/MCP/知识）+ 渠道接入统一入口。
      // 子页 URL 即 tab，路由 name 沿用历史值，既有跳转引用无需改动。
      {
        path: 'agents',
        component: () => import('./views/Agents.vue'),
        children: [
          { path: '', name: 'agents', component: () => import('./views/AgentInstances.vue') },
          { path: 'channels', name: 'channels', component: () => import('./views/Channels.vue') },
          { path: 'prompts', name: 'prompts', component: () => import('./views/Prompts.vue') },
          { path: 'models', name: 'models', component: () => import('./views/Models.vue') },
          { path: 'skills', name: 'skills', component: () => import('./views/Skills.vue') },
          { path: 'mcp', name: 'mcp', component: () => import('./views/MCP.vue') },
          { path: 'knowledge-bases', name: 'knowledge-bases', component: () => import('./views/KnowledgeBases.vue') },
          { path: 'knowledge', name: 'knowledge', component: () => import('./views/Knowledge.vue') },
        ],
      },
      // :id 限定数字，避免吞掉 /agents/channels 等静态子路径
      { path: 'agents/:id(\\d+)', name: 'agent-detail', component: () => import('./views/AgentDetail.vue'), props: true },
      { path: 'conversations', name: 'conversations', component: () => import('./views/Conversations.vue') },
      { path: 'approvals', name: 'approvals', component: () => import('./views/Approvals.vue') },
      { path: 'tasks', name: 'tasks', component: () => import('./views/Tasks.vue') },
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
      { path: 'audit', name: 'audit', component: () => import('./views/Audit.vue') },
      { path: 'policy-simulator', name: 'policy-simulator', component: () => import('./views/PolicySimulator.vue') },
      { path: 'releases', name: 'releases', component: () => import('./views/Releases.vue') },
      { path: 'ops-metrics', name: 'ops-metrics', component: () => import('./views/OpsMetrics.vue') },
      // 对话测试：数据面体验页，保留独立全屏路由；侧边栏无入口，从数字员工实例行内进入
      { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue') },
      // 旧一级路径 → Hub 子页（保留 query，如 knowledge?kb_id=）
      { path: 'channels', redirect: (to) => ({ path: '/agents/channels', query: to.query }) },
      { path: 'prompts', redirect: (to) => ({ path: '/agents/prompts', query: to.query }) },
      { path: 'models', redirect: (to) => ({ path: '/agents/models', query: to.query }) },
      { path: 'skills', redirect: (to) => ({ path: '/agents/skills', query: to.query }) },
      { path: 'mcp', redirect: (to) => ({ path: '/agents/mcp', query: to.query }) },
      { path: 'knowledge-bases', redirect: (to) => ({ path: '/agents/knowledge-bases', query: to.query }) },
      { path: 'knowledge', redirect: (to) => ({ path: '/agents/knowledge', query: to.query }) },
    ],
  },
  // 404 兜底：未知路径回仪表盘（hash 模式下无此路由则整页空白且无导航可点）
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 登录保护由 axios 401 拦截器统一处理（见 api.js），无需路由守卫。

// 路由切换进度条：懒加载路由在慢网下有白屏间隙，用顶部细条提示加载中。
// 纯 CSS 实现（0 依赖），见 styles/base.css .route-progress。
let progressBar = null
function ensureProgressBar() {
  if (progressBar) return progressBar
  progressBar = document.createElement('div')
  progressBar.className = 'route-progress'
  document.body.appendChild(progressBar)
  return progressBar
}

router.beforeEach(() => {
  const bar = ensureProgressBar()
  bar.classList.remove('route-progress--done')
  bar.classList.add('route-progress--active')
})
router.afterEach(() => {
  const bar = ensureProgressBar()
  bar.classList.remove('route-progress--active')
  bar.classList.add('route-progress--done')
})
// 懒加载失败（如重新部署后旧 chunk 404）不会触发 afterEach：这里收掉进度条并提示刷新
router.onError(() => {
  const bar = ensureProgressBar()
  bar.classList.remove('route-progress--active')
  bar.classList.add('route-progress--done')
})

export default router
