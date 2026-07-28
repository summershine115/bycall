# Implementation Plan: 前端优化 P0–P2

## Overview

基于探索报告，对 bycall-admin 前端做打包体积、代码去重、运行时开销、小项修复四个阶段的优化。
目标：主 JS chunk 从 1.2MB 降到 ~300KB 以下，CSS 从 372KB 降到 ~50KB 以下，消除重复模式，降低运行时开销。

## Architecture Decisions

- **EP 按需引入用 unplugin 方案**（unplugin-vue-components + unplugin-auto-import）：官方推荐，零运行时开销，自动 tree-shake。不用手动 import 每个组件。
- **图标迁移策略**：Layout/Login/EmptyState/StatCard 中的字符串图标名改为直接 import 组件引用，然后删除 main.js 全局注册。不引入 unplugin-icons（过度设计，EP 图标包本身支持按需 import）。
- **useResource composable**：抽取 CRUD 生命周期（loading/dialog/save/load），但不强制所有视图迁移——Knowledge 已有自己的 composable，Chat/Dashboard 不适用。只迁移标准 CRUD 视图。
- **Pinia 删除**：零 store、零 defineStore，纯死重。将来需要时 re-add 成本极低。
- **MutationObserver 改 CSS-only**：EP table 的 `el-table-column` 有 `label` 属性，窄屏堆叠用 CSS `::before { content: attr(data-label) }` 需要 data-label——但可以用 EP 的 `show-overflow-tooltip` + 纯 CSS media query 替代整个 JS 方案。

## Task List

### Phase 1: Bundle 瘦身 (P0)

- [ ] Task 1: Element Plus 按需引入
- [ ] Task 2: 图标去全局注册
- [ ] Task 3: Vendor 分包 (manualChunks)

### Checkpoint: Bundle
- [ ] `npm run build` 无 >500KB 警告
- [ ] 主 JS chunk < 350KB (gzip < 120KB)
- [ ] `npm run dev` 启动正常，所有页面可访问
- [ ] 侧边栏/登录页/各表格页图标正常渲染

### Phase 2: 代码去重 (P1)

- [ ] Task 4: 抽取 utils/format.js
- [ ] Task 5: 抽取 useResource composable
- [ ] Task 6: 删除 Pinia 死依赖

### Checkpoint: 去重
- [ ] `npm run build` 成功
- [ ] 无 formatTime 重复定义（grep 验证）
- [ ] 迁移后的视图功能不变（CRUD 流程手动验证）

### Phase 3: 运行时优化 (P1)

- [ ] Task 7: App.vue MutationObserver → CSS-only
- [ ] Task 8: 入库轮询降频 + 减少 N+1
- [ ] Task 9: rememberProgress 避免全量拷贝

### Checkpoint: 运行时
- [ ] 窄屏下表格堆叠标签正常
- [ ] 入库进度轮询功能正常（提交文档 → 进度更新 → 完成）
- [ ] DevTools Performance 无 body-wide observer 开销

### Phase 4: 小项修复 (P2)

- [ ] Task 10: 路由守卫清理
- [ ] Task 11: Login validate 写法修正
- [ ] Task 12: Dashboard audit 请求优化
- [ ] Task 13: index.html favicon + meta
- [ ] Task 14: 路由切换 loading 态

### Checkpoint: Complete
- [ ] `npm run build` 成功，无警告
- [ ] 全页面手动走查无回归
- [ ] commit 并合并回 main

---

## Task Details

### Task 1: Element Plus 按需引入

**Description:** 安装 unplugin-vue-components + unplugin-auto-import，配置 vite 插件自动按需引入 EP 组件和 API（ElMessage/ElMessageBox 等），删除 main.js 的全量 import。

**Acceptance criteria:**
- [ ] main.js 不再 `import ElementPlus from 'element-plus'` 和全量 CSS
- [ ] 只保留 `element-plus/theme-chalk` 中实际用到的基础样式（或 base.css 已覆盖）
- [ ] locale 配置保留（zh-cn）
- [ ] 所有页面的 el-* 组件正常渲染

**Verification:**
- `npm run build` 成功
- 主 CSS chunk < 60KB
- 手动检查：Login / Dashboard / Knowledge / Models 页面样式无异常

**Dependencies:** None

**Files likely touched:**
- `frontend/package.json` (devDeps)
- `frontend/vite.config.js`
- `frontend/src/main.js`

**Estimated scope:** S (3 files)

---

### Task 2: 图标去全局注册

**Description:** 将 Layout.vue navGroups、Login.vue points、EmptyState/StatCard 调用方的字符串图标名改为直接 import 的组件引用，然后删除 main.js 的 `import * as ElementPlusIconsVue` 循环注册。

**Acceptance criteria:**
- [ ] main.js 无 `ElementPlusIconsVue` 相关代码
- [ ] Layout 侧边栏 14 个导航图标正常
- [ ] Login 品牌区 3 个图标正常
- [ ] EmptyState / StatCard 传入的 icon prop 正常渲染
- [ ] 各视图已有的具名 import（Plus, Refresh 等）不受影响

**Verification:**
- `npm run build` 成功
- 手动检查侧边栏、登录页、Dashboard 卡片、各空状态

**Dependencies:** Task 1（main.js 同时改）

**Files likely touched:**
- `frontend/src/main.js`
- `frontend/src/views/Layout.vue`
- `frontend/src/views/Login.vue`
- `frontend/src/views/Dashboard.vue`（StatCard icon prop）
- 其他传 icon 字符串给 EmptyState 的视图（grep 确认）

**Estimated scope:** M (5-7 files)

---

### Task 3: Vendor 分包

**Description:** 在 vite.config.js 配置 `build.rollupOptions.output.manualChunks`，将 vue / vue-router / axios / element-plus 拆为独立 vendor chunk，利用长期缓存。

**Acceptance criteria:**
- [ ] 产物有独立 vendor chunk（vue-vendor / ep-vendor / axios-vendor 或合并）
- [ ] 业务代码改动不影响 vendor chunk hash
- [ ] 无循环依赖警告

**Verification:**
- `npm run build` 成功，无 >500KB 警告
- 检查 dist/assets/ 文件列表

**Dependencies:** Task 1（EP 按需引入后分包策略更清晰）

**Files likely touched:**
- `frontend/vite.config.js`

**Estimated scope:** XS (1 file)

---

### Task 4: 抽取 utils/format.js

**Description:** 将 6 处重复的 `formatTime` 函数抽到 `src/utils/format.js`，各视图改为 import。

**Acceptance criteria:**
- [ ] `src/utils/format.js` 导出 `formatTime`
- [ ] Approvals / Audit / Conversations / KnowledgeBases / Skills / useKnowledge 中无本地 formatTime 定义
- [ ] 时间显示格式不变（`zh-CN, hour12: false`）

**Verification:**
- `grep -r "function formatTime" frontend/src` 只命中 utils/format.js
- `npm run build` 成功

**Dependencies:** None

**Files likely touched:**
- `frontend/src/utils/format.js` (新建)
- 6 个视图/composable 文件

**Estimated scope:** M (7 files，但每个改动极小)

---

### Task 5: 抽取 useResource composable

**Description:** 抽取标准 CRUD 生命周期（loading / dialogVisible / editing / saving / form / load / openCreate / openEdit / onSave / onDelete）为 `src/composables/useResource.js`。迁移 Users / Roles / Prompts / Skills / MCP / Approvals / KnowledgeBases / Models 中适用的视图。

**Acceptance criteria:**
- [ ] `src/composables/useResource.js` 导出通用 composable
- [ ] 至少 5 个视图迁移完成（Knowledge / Chat / Dashboard 不适用，跳过）
- [ ] 迁移后各视图 CRUD 流程不变
- [ ] 视图文件行数平均减少 30%+

**Verification:**
- `npm run build` 成功
- 手动验证 2-3 个迁移视图的 增/改/删 流程

**Dependencies:** Task 4（formatTime 已抽出）

**Files likely touched:**
- `frontend/src/composables/useResource.js` (新建)
- 5-8 个视图文件

**Estimated scope:** L (8+ files) — 机械迁移，风险低

---

### Task 6: 删除 Pinia 死依赖

**Description:** 移除 package.json 中的 pinia 依赖、main.js 中的 createPinia/app.use，删除 node_modules 中对应包。

**Acceptance criteria:**
- [ ] package.json 无 pinia
- [ ] main.js 无 pinia 相关 import/use
- [ ] `npm run build` 成功

**Verification:**
- `npm install && npm run build`

**Dependencies:** None

**Files likely touched:**
- `frontend/package.json`
- `frontend/src/main.js`

**Estimated scope:** XS (2 files)

---

### Task 7: App.vue MutationObserver → CSS-only

**Description:** 删除 App.vue 中的全局 MutationObserver + labelTable 逻辑。窄屏表格堆叠标签改用纯 CSS：利用 EP table 的 `el-table-column` 已有 label，通过 CSS media query + `::before` 实现（需要确认 EP 是否在 DOM 上保留 th 文本可供 CSS 引用；若不行，改为在 element-overrides.css 中用 `td:nth-child(n)::before` 硬编码列名，或保留 observer 但收窄到 `.bc-panel` 并 debounce）。

**Acceptance criteria:**
- [ ] App.vue 无 MutationObserver
- [ ] 窄屏（<768px）下表格仍有列名标签
- [ ] 宽屏无视觉变化

**Verification:**
- DevTools 切换窄屏，检查各表格页
- Performance 面板无持续 observer 回调

**Dependencies:** None

**Files likely touched:**
- `frontend/src/App.vue`
- `frontend/src/styles/element-overrides.css`

**Estimated scope:** S (2 files)

---

### Task 8: 入库轮询降频 + 减少 N+1

**Description:** 将 `useKnowledge.js` 轮询间隔从 900ms 改为 2500ms（入库是分钟级操作）。stale doc 的逐个 `getIngestProgress` 改为：仅对当前 KB 的 stale docs 查询（已有），且加 500ms 合并窗口避免同一 tick 内重复请求。

**Acceptance criteria:**
- [ ] 轮询间隔 ≥ 2000ms
- [ ] 单次 pollOnce 最多 1 + N 请求（N = stale count），无重复
- [ ] 入库进度仍能正常更新（允许 2-3s 延迟）

**Verification:**
- 提交文档入库，观察进度条更新
- Network 面板确认请求频率

**Dependencies:** None

**Files likely touched:**
- `frontend/src/views/useKnowledge.js`

**Estimated scope:** XS (1 file)

---

### Task 9: rememberProgress 避免全量拷贝

**Description:** `rememberProgress` 中 `progressMap.value = { ...progressMap.value, [id]: ... }` 改为直接 mutate `progressMap.value[id] = ...`。Vue 3 ref 包裹的普通对象支持属性级追踪，无需替换整个引用。

**Acceptance criteria:**
- [ ] 无 `{ ...progressMap.value }` 展开
- [ ] 抽屉/表格进度显示不变
- [ ] `drawerIngests` computed 仍能正确触发（依赖 ingestOrder 数组 push 触发）

**Verification:**
- 入库进度功能正常
- Vue DevTools 确认响应式更新

**Dependencies:** None

**Files likely touched:**
- `frontend/src/views/useKnowledge.js`

**Estimated scope:** XS (1 file)

---

### Task 10: 路由守卫清理

**Description:** 删除 `router.js` 中空壳 `beforeEach`（只调 next()）。401 跳转已由 axios 拦截器处理，空守卫无意义。

**Acceptance criteria:**
- [ ] router.js 无空 beforeEach
- [ ] 未登录访问仍正确跳转登录页（axios 401 拦截器）

**Verification:**
- 清除 cookie 后访问任意页面 → 跳转 /login

**Dependencies:** None

**Files likely touched:**
- `frontend/src/router.js`

**Estimated scope:** XS (1 file)

---

### Task 11: Login validate 写法修正

**Description:** `Login.vue:120` 的 `.catch(() => Promise.reject())` 改为 `try { await formRef.value.validate() } catch { return }`，语义更清晰，不产生无意义 rejection。

**Acceptance criteria:**
- [ ] 表单验证失败时不触发登录请求
- [ ] 无 unhandled rejection 警告

**Verification:**
- 空表单点登录 → 显示验证错误，无 console 报错

**Dependencies:** None

**Files likely touched:**
- `frontend/src/views/Login.vue`

**Estimated scope:** XS (1 file)

---

### Task 12: Dashboard audit 请求优化

**Description:** Dashboard 拉 `listAudit(500)` 只为 `slice(0, 8)` + `length`。改为 `listAudit(8)` 获取最近 8 条；总数用单独轻量请求或从 audit 列表 API 的 total 字段获取（需确认后端是否返回 total；若不返回，保持 limit=500 但加注释说明原因）。

**Acceptance criteria:**
- [ ] 仪表盘审计卡片数字正确
- [ ] 最近审计列表显示 8 条
- [ ] 网络请求 payload 明显减小（若后端支持）

**Verification:**
- 打开 Dashboard，对比审计数字与审计页总数

**Dependencies:** None（可能需确认后端 API）

**Files likely touched:**
- `frontend/src/views/Dashboard.vue`
- 可能 `frontend/src/api.js`

**Estimated scope:** XS-S (1-2 files)

---

### Task 13: index.html favicon + meta

**Description:** 添加 favicon（用 Layout/Login 中已有的 SVG logo 转 .svg favicon）和 meta description。

**Acceptance criteria:**
- [ ] 浏览器 tab 显示 Bycall 图标
- [ ] `<meta name="description">` 存在

**Verification:**
- 打开页面检查 tab 图标

**Dependencies:** None

**Files likely touched:**
- `frontend/index.html`
- `frontend/public/favicon.svg` (新建)

**Estimated scope:** XS (2 files)

---

### Task 14: 路由切换 loading 态

**Description:** 利用空出的 router.beforeEach 位置（Task 10 删除后），加入轻量路由切换进度条（NProgress 或纯 CSS top-bar），解决懒加载路由白屏。

**Acceptance criteria:**
- [ ] 路由切换时顶部有进度条
- [ ] 加载完成后进度条消失
- [ ] 不引入重依赖（NProgress ~2KB 可接受，或纯 CSS 方案）

**Verification:**
- 慢网模拟下切换页面，观察进度条

**Dependencies:** Task 10

**Files likely touched:**
- `frontend/src/router.js`
- `frontend/src/styles/base.css` 或新建 progress 样式
- 可能 `frontend/package.json`（若用 NProgress）

**Estimated scope:** S (2-3 files)

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| EP 按需引入后某些隐式依赖组件丢失（如 ElMessage 的 CSS） | High | unplugin-auto-import 自动处理 API 导入；CSS 用 `element-plus/theme-chalk` 按需或保留 base import 兜底 |
| 图标字符串→组件迁移遗漏 | Med | grep 全量扫描 `component :is` + icon 字符串；构建后手动走查 |
| useResource 过度抽象导致特殊视图适配困难 | Med | 只迁移标准 CRUD；特殊逻辑留在视图层，composable 提供 escape hatch |
| CSS-only 表格堆叠在 EP 版本升级后失效 | Low | 保留 element-overrides.css 中的 fallback；EP 版本锁定 ^2.8 |
| 轮询降频后用户感知进度延迟 | Low | 2.5s 对分钟级操作可接受；首次提交仍立即 seed |

## Open Questions

- Task 12：后端 `/api/admin/audit` 是否返回 total count？若不返回，是否值得加？（当前保持 limit=500 也可接受，admin 控制台数据量小）
- Task 14：用 NProgress（2KB，成熟）还是纯 CSS top-bar（0 依赖，~20 行）？倾向纯 CSS。

## Parallelization Opportunities

- **Phase 1 内部**：Task 1 → Task 2 → Task 3 严格顺序（都改 main.js / vite.config）
- **Phase 2 内部**：Task 4 和 Task 6 可并行；Task 5 依赖 Task 4
- **Phase 3 内部**：Task 7 / 8 / 9 完全独立，可并行
- **Phase 4 内部**：Task 10-13 完全独立可并行；Task 14 依赖 Task 10
- **跨 Phase**：Phase 2 的 Task 6 和 Phase 3 全部可与 Phase 1 并行（不碰 main.js/vite.config）
