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
      ElMessage.error(`请求失败 (${status}): ${detail}`)
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
  createPrompt: (data) => http.post('/api/admin/prompts', data).then((r) => r.data),
  activatePrompt: (version) =>
    http.post('/api/admin/prompts/activate', { version }).then((r) => r.data),
  // Skill（领域知识文档，name 主键；PUT 是 PATCH 语义）
  listSkills: () => http.get('/api/admin/skills').then((r) => r.data),
  getSkill: (name) => http.get(`/api/admin/skills/${name}`).then((r) => r.data),
  createSkill: (data) => http.post('/api/admin/skills', data).then((r) => r.data),
  updateSkill: (name, data) => http.put(`/api/admin/skills/${name}`, data).then((r) => r.data),
  deleteSkill: (name) => http.delete(`/api/admin/skills/${name}`),
  // 审计
  listAudit: (limit = 100) =>
    http.get('/api/admin/audit', { params: { limit } }).then((r) => r.data),
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
}

// ============ 数据面 /chat ============

export const chat = {
  ask: (question) => http.post('/chat', { question }).then((r) => r.data),
}
