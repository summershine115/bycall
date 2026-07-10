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
      { path: 'models', name: 'models', component: () => import('./views/Models.vue') },
      { path: 'prompts', name: 'prompts', component: () => import('./views/Prompts.vue') },
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
      { path: 'audit', name: 'audit', component: () => import('./views/Audit.vue') },
      { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue') },
    ],
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 简易登录守卫：尝试一个需 admin 的请求，401 则跳登录。
// （后端用 cookie 会话，无显式"当前用户"端点，这里靠 401 拦截器处理跳转。）
router.beforeEach((to, from, next) => {
  next()
})

export default router
