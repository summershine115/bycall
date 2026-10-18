import { computed, reactive, toRaw } from 'vue'

// 基线与表单必须独立持有数组；保存接口直接使用真实字段名。
export function copyDraft(value) {
  // 响应对象可能已被 ref 转成嵌套 Proxy，structuredClone 不能直接复制其中的数组。
  if (Array.isArray(value)) return value.map(copyDraft)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(toRaw(value)).map(([key, item]) => [key, copyDraft(item)]))
  }
  return value
}

export function useSectionDraft(initial) {
  const form = reactive(copyDraft(initial))
  const baseline = reactive(copyDraft(initial))
  const dirty = computed(() => JSON.stringify(form) !== JSON.stringify(baseline))
  function accept(value) {
    Object.assign(form, copyDraft(value))
    Object.assign(baseline, copyDraft(value))
  }
  function reset() { Object.assign(form, copyDraft(baseline)) }
  function payload() { return copyDraft(form) }
  return { form, dirty, accept, reset, payload }
}
