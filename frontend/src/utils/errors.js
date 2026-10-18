// 面向用户解释失败，详细接口路径留给开发工具。
export function errorMessage(error, fallback = '操作失败，请重试') {
  if (!error?.response && error?.isAxiosError) return '无法连接服务，请检查网络后重试'
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；') || fallback
  return fallback
}
