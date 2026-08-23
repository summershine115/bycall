// API 层：封装所有后端调用。统一走 axios，带错误拦截。
// 后端端点：/api/login, /api/admin/* (用户/角色/提示词/审计/act-as), /chat

import axios from 'axios'
import { ElMessage } from 'element-plus'

const http = axios.create({
  baseURL: '/',
  withCredentials: true, // 带 cookie（会话）
  timeout: 30000,
})

// 响应拦截：统一处理 401/403/错误
http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail || error.message
    if (status === 401) {
      // 未登录 → 跳登录页（避免在登录页自身跳转造成循环）
      if (!window.location.hash.includes('/login')) {
        window.location.hash = '#/login'
      }
    } else if (status >= 400) {
      const method = (error.config?.method || 'get').toUpperCase()
      const url = error.config?.url || ''
      ElMessage.error(`请求失败 (${status}) ${method} ${url}: ${detail}`)
    }
    return Promise.reject(error)
  },
)

// ============ 会话 ============

export const auth = {
  login: (name, password) =>
    http.post('/api/login', new URLSearchParams({ name, password })),
  logout: () => http.post('/api/logout'),
}

// ============ Admin REST ============

export const admin = {
  // 用户
  listUsers: () => http.get('/api/admin/users').then((r) => r.data),
  simulatePolicy: (data) => http.post('/api/admin/policy/simulate', data).then((r) => r.data),
  createUser: (data) => http.post('/api/admin/users', data).then((r) => r.data),
  updateUser: (id, data) => http.patch(`/api/admin/users/${id}`, data).then((r) => r.data),
  deleteUser: (id) => http.delete(`/api/admin/users/${id}`),
  // 角色
  listRoles: () => http.get('/api/admin/roles').then((r) => r.data),
  createRole: (data) => http.post('/api/admin/roles', data).then((r) => r.data),
  updateRole: (name, data) => http.patch(`/api/admin/roles/${name}`, data).then((r) => r.data),
  deleteRole: (name) => http.delete(`/api/admin/roles/${name}`),
  // 提示词
  listPrompts: () => http.get('/api/admin/prompts').then((r) => r.data),
  getPrompt: (version) => http.get(`/api/admin/prompts/${version}`).then((r) => r.data),
  createPrompt: (data) => http.post('/api/admin/prompts', data).then((r) => r.data),
  updatePrompt: (version, data) => http.put(`/api/admin/prompts/${version}`, data).then((r) => r.data),
  deletePrompt: (version) => http.delete(`/api/admin/prompts/${version}`),
  activatePrompt: (version) =>
    http.post('/api/admin/prompts/activate', { version }).then((r) => r.data),
  // Skill（领域知识文档，name 主键；PUT 是 PATCH 语义）
  listSkills: () => http.get('/api/admin/skills').then((r) => r.data),
  getSkill: (name) => http.get(`/api/admin/skills/${name}`).then((r) => r.data),
  createSkill: (data) => http.post('/api/admin/skills', data).then((r) => r.data),
  updateSkill: (name, data) => http.put(`/api/admin/skills/${name}`, data).then((r) => r.data),
  deleteSkill: (name) => http.delete(`/api/admin/skills/${name}`),
  // Skill 导入导出
  exportSkills: (names) => {
    const params = names?.length ? { names: names.join(',') } : {}
    return http.get('/api/admin/skills/export', { params, responseType: 'blob' }).then((r) => r.data)
  },
  importSkills: (file, onConflict = 'skip') => {
    const formData = new FormData()
    formData.append('file', file)
    return http.post(`/api/admin/skills/import?on_conflict=${onConflict}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then((r) => r.data)
  },
  // 审计
  listAudit: (params = {}) =>
    http.get('/api/admin/audit', { params }).then((r) => r.data),
  // 运行时设置（IM 凭证，控制台管理）
  listSettings: () => http.get('/api/admin/settings').then((r) => r.data),
  updateSettings: (settings) =>
    http.put('/api/admin/settings', { settings }).then((r) => r.data),
  // IM 渠道热重载（改凭证后重连，不重启服务）
  reloadIm: () => http.post('/api/admin/im/reload').then((r) => r.data),
  imStatus: () => http.get('/api/admin/im/status').then((r) => r.data),
  // 模型管理（Model 实体 CRUD + 激活 + 测试 + provider 注册表）
  listModelProviders: (modelType) =>
    http
      .get('/api/admin/models/providers', { params: modelType ? { model_type: modelType } : {} })
      .then((r) => r.data),
  listModels: (modelType) =>
    http
      .get('/api/admin/models', { params: modelType ? { model_type: modelType } : {} })
      .then((r) => r.data),
  createModel: (data) => http.post('/api/admin/models', data).then((r) => r.data),
  updateModel: (id, data) => http.put(`/api/admin/models/${id}`, data).then((r) => r.data),
  deleteModel: (id) => http.delete(`/api/admin/models/${id}`),
  activateModel: (id, rebuild) =>
    http
      .post(`/api/admin/models/${id}/activate`, null, { params: rebuild ? { rebuild: true } : {} })
      .then((r) => r.data),
  testModel: (id) => http.post(`/api/admin/models/${id}/test`).then((r) => r.data),
  // 知识库管理（per-KB：每个 KB 绑定一个 embedding 模型 + 一个 zvec collection）
  listKnowledgeBases: () => http.get('/api/admin/knowledge-bases').then((r) => r.data),
  getKnowledgeBase: (kbId) => http.get(`/api/admin/knowledge-bases/${kbId}`).then((r) => r.data),
  createKnowledgeBase: (data) => http.post('/api/admin/knowledge-bases', data).then((r) => r.data),
  updateKnowledgeBase: (kbId, data) =>
    http.put(`/api/admin/knowledge-bases/${kbId}`, data).then((r) => r.data),
  deleteKnowledgeBase: (kbId) => http.delete(`/api/admin/knowledge-bases/${kbId}`),
  // 知识库文档管理（绑定到 KB）
  listDocuments: (kbId, source) =>
    http
      .get(`/api/admin/knowledge-bases/${kbId}/documents`, { params: source ? { source } : {} })
      .then((r) => r.data),
  createDocument: (kbId, data) =>
    http.post(`/api/admin/knowledge-bases/${kbId}/documents`, data).then((r) => r.data),
  getDocument: (kbId, id) =>
    http.get(`/api/admin/knowledge-bases/${kbId}/documents/${id}`).then((r) => r.data),
  updateDocument: (kbId, id, data) =>
    http.put(`/api/admin/knowledge-bases/${kbId}/documents/${id}`, data).then((r) => r.data),
  deleteDocument: (kbId, id) =>
    http.delete(`/api/admin/knowledge-bases/${kbId}/documents/${id}`),
  reindexDocument: (kbId, id) =>
    http.post(`/api/admin/knowledge-bases/${kbId}/documents/${id}/reindex`).then((r) => r.data),
  getIngestProgress: (kbId, docId) =>
    http.get(`/api/admin/knowledge-bases/${kbId}/documents/${docId}/progress`).then((r) => r.data),
  listActiveIngests: () => http.get('/api/admin/ingest/active').then((r) => r.data),
  listSources: () => http.get('/api/admin/knowledge/sources').then((r) => r.data),
  uploadDocument: (kbId, file, source, title) => {
    const formData = new FormData()
    formData.append('file', file)
    return http
      .post(`/api/admin/knowledge-bases/${kbId}/upload`, formData, {
        params: { source, ...(title ? { title } : {}) },
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
  previewChunks: (kbId, data) =>
    http.post(`/api/admin/knowledge-bases/${kbId}/documents/preview-chunks`, data).then((r) => r.data),
  getDocumentContent: (kbId, id) =>
    http.get(`/api/admin/knowledge-bases/${kbId}/documents/${id}/content`).then((r) => r.data),
  // 文档切块列表
  listDocumentChunks: (kbId, docId) =>
    http.get(`/api/admin/knowledge-bases/${kbId}/documents/${docId}/chunks`).then((r) => r.data),
  // 解析器列表（按扩展名路由的内置解析器）
  listParsers: () => http.get('/api/admin/parsers').then((r) => r.data),
  // 解析引擎列表（builtin / markitdown / mineru，含可用性与支持类型）
  listParseEngines: () => http.get('/api/admin/parse-engines').then((r) => r.data),
  // dev act-as
  actAs: (userId) =>
    http.post('/api/admin/act-as', null, { params: { target_user_id: userId } }),
  clearActAs: () => http.post('/api/admin/clear-act-as'),
  // 会话管理
  listConversations: (userId) =>
    http
      .get('/api/admin/conversations', { params: userId ? { user_id: userId } : {} })
      .then((r) => r.data),
  getConversation: (id) => http.get(`/api/admin/conversations/${id}`).then((r) => r.data),
  listMessages: (convId, limit = 50) =>
    http
      .get(`/api/admin/conversations/${convId}/messages`, { params: { limit } })
      .then((r) => r.data),
  deleteConversation: (id) => http.delete(`/api/admin/conversations/${id}`),
  resetConversation: (id) => http.post(`/api/admin/conversations/${id}/reset`).then((r) => r.data),
  // 用户长期记忆
  listUserMemory: (userId) => http.get(`/api/admin/users/${userId}/memory`).then((r) => r.data),
  createUserMemory: (userId, data) =>
    http.post(`/api/admin/users/${userId}/memory`, data).then((r) => r.data),
  updateUserMemory: (memId, data) => http.put(`/api/admin/memory/${memId}`, data).then((r) => r.data),
  deleteUserMemory: (memId) => http.delete(`/api/admin/memory/${memId}`),
  // MCP 工具服务器（M4）
  listMcpServers: () => http.get('/api/admin/mcp-servers').then((r) => r.data),
  createMcpServer: (data) => http.post('/api/admin/mcp-servers', data).then((r) => r.data),
  getMcpServer: (id) => http.get(`/api/admin/mcp-servers/${id}`).then((r) => r.data),
  updateMcpServer: (id, data) => http.put(`/api/admin/mcp-servers/${id}`, data).then((r) => r.data),
  deleteMcpServer: (id) => http.delete(`/api/admin/mcp-servers/${id}`),
  reloadMcpServer: (id) => http.post(`/api/admin/mcp-servers/${id}/reload`).then((r) => r.data),
  testMcpServer: (id) => http.post(`/api/admin/mcp-servers/${id}/test`).then((r) => r.data),
  listTools: () => http.get('/api/admin/tools').then((r) => r.data),
  // HITL 审批队列（M4）：敏感工具调用需人工批准后执行
  listApprovals: (status) =>
    http
      .get('/api/admin/approvals', { params: status ? { status } : {} })
      .then((r) => r.data),
  getApproval: (id) => http.get(`/api/admin/approvals/${id}`).then((r) => r.data),
  approveApproval: (id, note, grantScope = 'once') =>
    http
      .post(`/api/admin/approvals/${id}/approve`, { note, grant_scope: grantScope })
      .then((r) => r.data),
  rejectApproval: (id, note) =>
    http.post(`/api/admin/approvals/${id}/reject`, { note }).then((r) => r.data),
  // 审批授权（T3.3 三档 once/session/always）：台账列表 + 撤销
  listApprovalGrants: (params = {}) =>
    http.get('/api/admin/approvals/grants', { params }).then((r) => r.data),
  revokeApprovalGrant: (id) =>
    http.post(`/api/admin/approvals/grants/${id}/revoke`).then((r) => r.data),
  // 任务执行（S1）：多步任务列表 / 详情 / SSE 进度
  listTasks: (params = {}) => http.get('/api/admin/tasks', { params }).then((r) => r.data),
  getTask: (id) => http.get(`/api/admin/tasks/${id}`).then((r) => r.data),
  taskEventsUrl: (id) => `/api/admin/tasks/${id}/events`,
  // 任务调度（S1b）：定时/事件触发管理
  listTaskSchedules: () => http.get('/api/admin/task-schedules').then((r) => r.data),
  createTaskSchedule: (data) =>
    http.post('/api/admin/task-schedules', data).then((r) => r.data),
  updateTaskSchedule: (id, data) =>
    http.patch(`/api/admin/task-schedules/${id}`, data).then((r) => r.data),
  deleteTaskSchedule: (id) => http.delete(`/api/admin/task-schedules/${id}`),
  fireTaskSchedule: (id) =>
    http.post(`/api/admin/task-schedules/${id}/fire`).then((r) => r.data),
  // 发布门 + 灰度 + 一键回滚（T2.4）
  listReleases: () => http.get('/api/admin/releases').then((r) => r.data),
  publishPrompt: (version, data = { canary_percent: 0, skip_gate: false }) =>
    http.post(`/api/admin/prompts/${version}/publish`, data).then((r) => r.data),
  publishModel: (id, data = { canary_percent: 0, skip_gate: false }) =>
    http.post(`/api/admin/models/${id}/publish`, data).then((r) => r.data),
  promoteRelease: (kind) =>
    http.post(`/api/admin/releases/${kind}/promote`).then((r) => r.data),
  rollbackRelease: (kind) =>
    http.post(`/api/admin/releases/${kind}/rollback`).then((r) => r.data),
  // 五维运营指标（T2.5）
  opsMetrics: () => http.get('/api/admin/ops-metrics').then((r) => r.data),
}

// ============ 数据面 /chat ============

export const chat = {
  ask: (question, conversationId) =>
    http.post('/chat', { question, conversation_id: conversationId }).then((r) => r.data),
  // 流式对话（SSE）：POST 无法用 EventSource（仅 GET），改用 fetch + ReadableStream
  // 逐帧解析 event:/data:，分发到回调。不走 axios 拦截（401 单独处理跳登录）。
  stream: async (question, conversationId, { onToken, onFlush, onDone, onError } = {}) => {
    const resp = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ question, conversation_id: conversationId }),
    })
    if (resp.status === 401) {
      if (!window.location.hash.includes('/login')) window.location.hash = '#/login'
      return
    }
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      onError?.(new Error(`请求失败 (${resp.status}): ${detail}`))
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const evt = _parseSse(buffer.slice(0, idx))
          buffer = buffer.slice(idx + 2)
          if (evt.type === 'token') onToken?.(evt.data.text)
          else if (evt.type === 'flush') onFlush?.()
          else if (evt.type === 'done') onDone?.(evt.data)
          else if (evt.type === 'error') onError?.(new Error(evt.data.message))
        }
      }
    } catch (e) {
      onError?.(e)
    }
  },
  // 当前用户的全部 api 会话（侧栏列表）
  conversations: () => http.get('/chat/conversations').then((r) => r.data),
  // 某会话的消息历史（切换会话时加载）
  conversationMessages: (convId) =>
    http.get(`/chat/conversations/${convId}/messages`).then((r) => r.data),
  // 删除某会话（消息 + 会话记录）
  deleteConversation: (convId) => http.delete(`/chat/conversations/${convId}`),
}

// SSE 帧解析：event:/data: 行 → { type, data }
function _parseSse(raw) {
  let type = 'message'
  let dataStr = ''
  for (const line of raw.split('\n')) {
    if (line.startsWith('event: ')) type = line.slice(7).trim()
    else if (line.startsWith('data: ')) dataStr += line.slice(6)
  }
  try {
    return { type, data: dataStr ? JSON.parse(dataStr) : {} }
  } catch {
    return { type, data: {} }
  }
}
