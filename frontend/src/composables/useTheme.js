/**
 * 主题管理：仅保留 mono（黑白）× light/dark 模式。
 *
 * - 模式默认跟随系统（prefers-color-scheme），也可手动锁定。
 * - 首帧防闪烁由 index.html 内联脚本完成；本模块负责运行期切换与持久化。
 * - data-theme 固定为 mono（保留兼容，evergreen 已下线）。
 */
import { ref, watch, onBeforeUnmount } from 'vue'

const MODE_KEY = 'bycall.mode'

// mode: 'light' | 'dark' | 'auto'（跟随系统）
export const mode = ref(localStorage.getItem(MODE_KEY) || 'light')

const media = window.matchMedia('(prefers-color-scheme: dark)')

function resolvedMode() {
  if (mode.value === 'auto') return media.matches ? 'dark' : 'light'
  return mode.value
}

function apply() {
  const el = document.documentElement
  // 切换主题时临时禁用过渡，避免整表/按钮变量渐变导致的闪烁
  el.classList.add('no-theme-transition')
  el.dataset.theme = 'mono'
  el.dataset.mode = resolvedMode()
  // 联动 EP 暗色基座（dark/css-vars.css 以 html.dark 作用域生效）
  el.classList.toggle('dark', resolvedMode() === 'dark')
  // 下两帧后恢复过渡（首帧已完成变量切换，次帧恢复动画）
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      el.classList.remove('no-theme-transition')
    })
  })
}

const onSystemChange = () => {
  if (mode.value === 'auto') apply()
}

export function setMode(next) {
  mode.value = next
}

/** 在 App 根组件调用一次：安装监听并应用初始值 */
export function useTheme() {
  apply()
  watch(mode, (v) => {
    localStorage.setItem(MODE_KEY, v)
    apply()
  })
  media.addEventListener('change', onSystemChange)
  onBeforeUnmount(() => media.removeEventListener('change', onSystemChange))
  return { mode, setMode }
}
