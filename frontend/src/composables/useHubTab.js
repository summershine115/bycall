// Hub 内 tab 的「重新进入」钩子。
//
// 背景（ADR-0014）：Hub 的 el-tab-pane 是 lazy 挂载后**常驻**的（切走只是隐藏，
// 不卸载）。于是 onMounted 里拉的数据在切走期间不会更新——同源 tab 之间互相改数据时
// （典型：在「发布与回滚」回滚提示词/模型，而「提示词版本」「模型管理」还显示旧状态）
// 会看到与后端不一致的陈旧页面。
//
// 用法：页面声明自己所属的 tab key，切回该 tab 时重新拉取（同一次激活只触发一次）：
//   useHubTabRefresh('prompts', () => load().catch(() => {}))
import { inject, watch } from 'vue'

/**
 * @param {string} tabKey - 本页在 hubs[].tabs 里的 key（见 src/nav.js）
 * @param {() => void} reload - 重新进入该 tab 时的刷新动作
 */
export function useHubTabRefresh(tabKey, reload) {
  // 不在 Hub 内（独立页）时无此注入，直接跳过
  const activeKey = inject('bcHubActiveTab', null)
  if (!activeKey) return
  watch(activeKey, (key, prev) => {
    // 首次挂载由 onMounted 负责；这里只管「隐藏后再切回」
    if (key === tabKey && prev !== key) reload()
  })
}
