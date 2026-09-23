import { createRouter, createWebHashHistory } from 'vue-router'
import { hubs } from './nav'

// 路由：hash 模式（FastAPI 托管时无需 SPA fallback 特殊处理）。
// 守卫：未登录 → 跳登录页（除登录页自身）。
//
// 一级页面分三类（ADR-0014）：
//   ① Hub 页（同源页面合并的 tab 容器）——由导航模型 nav.js 生成，避免路径与 tab 漂移；
//   ② 独立页（数字员工详情 / 审批队列 / 运行时设置 / 对话测试）；
//   ③ 旧路径重定向——合并前的每个入口都保留可达（深链不破）。
const hubRoutes = Object.entries(hubs).map(([name, hub]) => ({
  path: hub.path.replace(/^\//, ''),
  name,
  component: () => import('./views/Hub.vue'),
}))

// 旧路径 →（新一级页面, tab）：三列即"合并前 → 合并后"的完整对照表
const legacyRoutes = [
  ['users', 'org', 'users'],
  ['roles', 'org', 'roles'],
  ['policy-simulator', 'org', 'policy'],
  ['prompts', 'assets', 'prompts'],
  ['models', 'assets', 'models'],
  ['skills', 'assets', 'skills'],
  ['mcp', 'assets', 'mcp'],
  ['releases', 'assets', 'releases'],
  ['knowledge-bases', 'knowledge-hub', 'bases'],
  ['conversations', 'runtime', 'conversations'],
  ['tasks', 'runtime', 'tasks'],
  ['ops-metrics', 'supervision', 'metrics'],
  ['channels', 'agents', 'channels'],
  // ADR-0012 时代的「数字员工 Hub」子路径（二次重组后仍保留可达）
  ['agents/prompts', 'assets', 'prompts'],
  ['agents/models', 'assets', 'models'],
  ['agents/skills', 'assets', 'skills'],
  ['agents/mcp', 'assets', 'mcp'],
  ['agents/knowledge-bases', 'knowledge-hub', 'bases'],
  ['agents/knowledge', 'knowledge-hub', 'docs'],
  ['agents/channels', 'agents', 'channels'],
].map(([path, name, tab]) => ({
  path,
  // 保留原 query（如 knowledge?kb_id=）再补 tab
  redirect: (to) => ({ name, query: { ...to.query, tab } }),
}))

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
      ...hubRoutes,
      // :id 限定数字，避免吞掉 /agents/channels 等旧静态子路径
      { path: 'agents/:id(\\d+)', name: 'agent-detail', component: () => import('./views/AgentDetail.vue'), props: true },
      { path: 'approvals', name: 'approvals', component: () => import('./views/Approvals.vue') },
      // 运行时设置：已从侧边栏移入顶栏齿轮（低频、系统级，不占导航位）
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
      // 对话测试：数据面体验页，保留独立全屏路由；侧边栏无入口，从数字员工实例行内进入
      { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue') },
      ...legacyRoutes,
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
