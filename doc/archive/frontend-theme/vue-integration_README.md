# Bycall 双主题接入包 —— 设计稿 → Vue 工程

> 状态：历史实施记录（归档，2026-10-13）。原任务勾选与设计描述保留历史，不代表当前待办；当前执行范围以 [执行计划](../../execution-plan.md) 为准。

设计稿（本工作区 18 屏 + `assets/bc-theme.css`）定义的两套主题：

| 主题 | 设计意图 | 对应工程状态 |
|---|---|---|
| **纯白** | 侧栏与画布同白，发丝线分层；主操作近黑 | `data-theme="mono" data-mode="light"` 换值 |
| **纯黑** | 全画布近黑（避开 `#000` 振动）；主操作反转为白 | `data-theme="mono" data-mode="dark"` 换值 |

**结论先行**：你们的令牌架构已经完整（双轴矩阵、持久化、防闪烁、侧栏全令牌化），
接入面只有——**2 个 CSS 块换值 + 3 处一行级小改 + 1 个新组件**。业务页面零改动。

---

## 接入步骤（按序，半天内可完成）

### 步骤 1 — 换 mono 两个块的值

打开 `src/styles/tokens.css`：

1. 用本包 `tokens-mono-dual.css` 里的 **light 块** 整体替换
   `:root[data-theme='mono'][data-mode='light'] { … }`（原 95–169 行）
2. 用 **dark 块** 整体替换
   `:root[data-theme='mono'][data-mode='dark'] { … }`（原 170–238 行）
3. **删除原 464 行的追加块**（`--bc-on-primary: #111111` 已并入新 dark 块）

改动的行都有 `←` 标注和理由注释，其余行与现值完全一致。

### 步骤 2 — 接入 Element Plus 暗色基座

**`main.js`**：在程序化组件样式之后、`tokens.css` 之前加一行：

```js
import 'element-plus/theme-chalk/base.css'
// …现有的 el-overlay / el-message-box 等…
import 'element-plus/theme-chalk/dark/css-vars.css'   // ← 新增：EP 官方暗色基座
import './styles/tokens.css'
import './styles/element-overrides.css'
import './styles/element-dual-theme.css'              // ← 新增：本包映射层（放最后）
import './styles/base.css'
```

**`src/composables/useTheme.js`** 的 `apply()` 加一行（同步 `html.dark` 类）：

```js
function apply() {
  const el = document.documentElement
  el.dataset.theme = theme.value
  el.dataset.mode = resolvedMode()
  el.classList.toggle('dark', resolvedMode() === 'dark')  // ← 新增
}
```

**`index.html`** 防闪烁脚本同样加一行（在写入 dataset 之后）：

```js
document.documentElement.dataset.theme = theme
document.documentElement.dataset.mode = mode
document.documentElement.classList.toggle('dark', mode === 'dark')  // ← 新增
```

说明：EP 的暗色组件样式通过 `html.dark` 作用域激活，而你们的切换走
`data-mode` 属性——加这一行把两套开关联动。值本身的精修由
`element-dual-theme.css` 的映射层完成（EP 官方暗色默认 `#141414` 系
会被覆盖成我们的 `#0a0a0a / #111` 体系）。

### 步骤 3 — 顶栏装明暗切换钮

你们 `useTheme` 已暴露 `mode / setMode`，但顶栏目前只有 mono/evergreen
主题选择器。把本包 `ModeToggle.vue` 拷到 `src/components/`，在
`Layout.vue` 顶栏右侧（主题选择器旁）挂载：

```vue
import ModeToggle from '../components/ModeToggle.vue'

<!-- 顶栏 right 区 -->
<ModeToggle />
```

手动切换会覆盖 `auto` 并持久化（沿用你们现有的 `bycall.mode` 存储）。

### 步骤 4 — 视觉核对（已预检的风险点）

接入前已扫描过工程，以下都是安全的，仅列出备查：

- ✅ 侧栏样式（`.nav-item`、激活态、折叠钮）全部走 `--bc-sidebar-*`，
  白侧栏/黑侧栏由令牌翻转自动生效，无硬编码残留
- ✅ `--bc-on-dark-1` 只有 `Login.vue` 用到，且在深色底上，不受影响
- ✅ evergreen 两个块有独立的侧栏分支，完全不动
- ✅ 表格 hover（`--bc-gray-1`）在新表面值下行为保持：深色模式行 hover
  仍是微降一阶
- ⚠️ 唯一建议自查：全局 `grep -rn 'rgba(255' src/views src/components`，
  确认没有为"黑侧栏"手写的白色高光残留在侧栏相关组件上

### 步骤 5 — 验证清单

- [ ] 浅色：侧栏纯白、发丝线右缘、品牌标黑底白字
- [ ] 深色：画布 `#0a0a0a`、面板 `#111`、主按钮白底黑字
- [ ] 深色下 EP 程序化组件（MessageBox / Message / 下拉 / 日期选择 /
  Loading）无白底残留
- [ ] 状态 pill（success/warning/danger）深色下为半透明色渍，不发灰
- [ ] 刷新不闪（防闪烁脚本）、跨页保持、`auto` 模式跟随系统
- [ ] 对比度抽检：深色正文 ≥ 4.5:1、侧栏导航 ≥ 4.5:1

---

## 设计值变更对照（mono）

### light（纯白主题）

| 变量 | 原值 | 新值 | 理由 |
|---|---|---|---|
| `--bc-sidebar-bg` | `#000000` | `#ffffff` | 纯白锚点：侧栏与画布同色 |
| `--bc-sidebar-text` | `#a3a3a3` | `#525252` | 白底需更深（对比 7.2:1） |
| `--bc-sidebar-text-strong` | `#fafafa` | `#111111` | 激活/品牌文字反转 |
| `--bc-sidebar-hover` | `rgba(255,255,255,.08)` | `rgba(0,0,0,.05)` | 白底黑系高光 |
| `--bc-sidebar-active` | `rgba(255,255,255,.14)` | `rgba(0,0,0,.08)` | 同上 |
| `--bc-sidebar-border` | `rgba(255,255,255,.1)` | `#e5e5e5` | 发丝线分隔 |

### dark（纯黑主题）

| 变量 | 原值 | 新值 | 理由 |
|---|---|---|---|
| `--bc-bg-page` | `#000000` | `#0a0a0a` | 避开大面积纯黑振动 |
| `--bc-bg-surface` | `#0f0f0f` | `#111111` | 与沉底拉开一阶层次 |
| `--bc-bg-sunken` | `#141414` | `#1a1a1a` | 同上 |
| `--bc-border-strong` | `#404040` | `#383838` | 收敛一档，暗面更安静 |
| `--bc-success/warning/danger-bg` | 实底深色 tint | `rgba(...,0.14)` 色渍 | 近黑面板上不发灰 |
| `--bc-info-bg` | `#262626` | `rgba(255,255,255,.07)` | 同上 |
| `--bc-sidebar-text` | `#737373` | `#a3a3a3` | 导航文字提亮（4.6→7.6:1） |
| `--bc-on-primary` | 464 行追加 `#111111` | `#0a0a0a`（并入主块） | 白底按钮上的近黑文字 |

---

## 回滚

全部改动集中在 `tokens.css` 两个块 + 两个新 CSS 引入 + `useTheme`/`index.html`
各一行 + 一个新组件，`git revert` 即完整回滚，不触及任何业务组件。

## 可选增强（不阻塞接入）

- **`?theme=dark` 深链**：设计稿支持 URL 直开指定主题，工程里可在
  `useTheme` 初始化时读一次 `location.search`，用于演示分享
- **evergreen 白侧栏对照版**：如 evergreen 也要切到"纯白"骨架，把其
  light 块的 sidebar 六值按上表同步即可
