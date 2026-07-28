// 时间格式化公共工具。
// 全站统一用 zh-CN 24 小时制，避免各视图重复定义。

/** 格式化为本地化日期时间字符串（如 2026/7/28 14:30:00） */
export function formatTime(t) {
  if (!t) return ''
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}
