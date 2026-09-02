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
      { path: 'agents', name: 'agents', component: () => import('./views/Agents.vue') },
      { path: 'models', name: 'models', component: () => import('./views/Models.vue') },
      { path: 'knowledge', name: 'knowledge', component: () => import('./views/Knowledge.vue') },
      { path: 'knowledge-bases', name: 'knowledge-bases', component: () => import('./views/KnowledgeBases.vue') },
      { path: 'conversations', name: 'conversations', component: () => import('./views/Conversations.vue') },
      { path: 'prompts', name: 'prompts', component: () => import('./views/Prompts.vue') },
      { path: 'skills', name: 'skills', component: () => import('./views/Skills.vue') },
      { path: 'mcp', name: 'mcp', component: () => import('./views/MCP.vue') },
      { path: 'approvals', name: 'approvals', component: () => import('./views/Approvals.vue') },
      { path: 'tasks', name: 'tasks', component: () => import('./views/Tasks.vue') },
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
      { path: 'audit', name: 'audit', component: () => import('./views/Audit.vue') },
      { path: 'policy-simulator', name: 'policy-simulator', component: () => import('./views/PolicySimulator.vue') },
      { path: 'releases', name: 'releases', component: () => import('./views/Releases.vue') },
      { path: 'ops-metrics', name: 'ops-metrics', component: () => import('./views/OpsMetrics.vue') },
      { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue') },
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
