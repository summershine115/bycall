/**
 * 主题管理：主题（mono/evergreen）× 模式（light/dark）二维切换。
 *
 * - mono（黑白）为默认主题；evergreen 为备选（原品牌绿色完整保留）。
 * - 模式默认跟随系统（prefers-color-scheme），也可手动锁定。
 * - 首帧防闪烁由 index.html 内联脚本完成；本模块负责运行期切换与持久化。
 */
import { ref, watch, onBeforeUnmount } from 'vue'

const THEME_KEY = 'bycall.theme'
const MODE_KEY = 'bycall.mode'

export const THEMES = [
  { key: 'mono', label: '墨 · 黑白', desc: '纯黑锚点，单色灰阶' },
  { key: 'evergreen', label: '青 · 常绿', desc: '深青绿品牌色' },
]

export const theme = ref(localStorage.getItem(THEME_KEY) || 'mono')
// mode: 'light' | 'dark' | 'auto'（跟随系统）
export const mode = ref(localStorage.getItem(MODE_KEY) || 'light')

const media = window.matchMedia('(prefers-color-scheme: dark)')

function resolvedMode() {
  if (mode.value === 'auto') return media.matches ? 'dark' : 'light'
  return mode.value
}

function apply() {
  const el = document.documentElement
  el.dataset.theme = theme.value
  el.dataset.mode = resolvedMode()
  // 联动 EP 暗色基座（dark/css-vars.css 以 html.dark 作用域生效）
  el.classList.toggle('dark', resolvedMode() === 'dark')
}

const onSystemChange = () => {
  if (mode.value === 'auto') apply()
}

export function setTheme(next) {
  theme.value = next
}

export function setMode(next) {
  mode.value = next
}

/** 在 App 根组件调用一次：安装监听并应用初始值 */
export function useTheme() {
  apply()
  watch(theme, (v) => {
    localStorage.setItem(THEME_KEY, v)
    apply()
  })
  watch(mode, (v) => {
    localStorage.setItem(MODE_KEY, v)
    apply()
  })
  media.addEventListener('change', onSystemChange)
  onBeforeUnmount(() => media.removeEventListener('change', onSystemChange))
  return { theme, mode, themes: THEMES, setTheme, setMode }
}
