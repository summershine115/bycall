<template>
  <div class="bc-page">
    <ContentLoader v-if="loading" :rows="5" />

    <template v-else>
      <!-- ============ KPI 行 ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">实例总数</div>
          <div class="kpi__value mono">{{ agents.length }}</div>
          <div class="kpi__meta">{{ enabledCount }} 启用 · {{ disabledCount }} 停用</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">默认实例</div>
          <div class="kpi__value mono" style="font-size: var(--bc-text-lg)">{{ defaultAgent?.display_name || defaultAgent?.name || '—' }}</div>
          <div class="kpi__meta">{{ defaultAgent ? `slug: ${defaultAgent.name}` : '未设置默认' }}</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">专属 Bot</div>
          <div class="kpi__value mono">{{ botCount }}</div>
          <div class="kpi__meta">{{ connectedBotCount }}/{{ botCount }} 已连接 · 企微凭证在详情页「IM 渠道」区维护</div>
        </div>
      </div>

      <div class="bc-panel">
        <!-- ============ 筛选栏 ============ -->
        <div class="filterbar">
          <el-input
            v-model="keyword"
            class="filterbar__search"
            placeholder="按 slug / 名称 / 描述搜索"
            clearable
            :prefix-icon="Search"
          />
          <span class="filterbar__spacer"></span>
          <button
            v-for="c in statusChips"
            :key="c.value"
            type="button"
            class="fchip"
            :class="{ 'is-on': statusFilter === c.value }"
            @click="statusFilter = c.value"
          >
            {{ c.label }} <span class="n">{{ c.count }}</span>
          </button>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建数字员工</el-button>
        </div>

        <EmptyState
          v-if="!agents.length"
          icon="Avatar"
          title="还没有数字员工"
          description="新建一个数字员工实例，然后在详情页绑定人格、模型、能力边界与专属 Bot。"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个数字员工</el-button>
        </EmptyState>

        <template v-else>
          <div class="table-scroll">
            <el-table v-if="filtered.length" :data="filtered">
              <el-table-column label="实例" min-width="220">
                <template #default="{ row }">
                  <div class="agent-cell">
                    <span class="agent-cell__avatar" aria-hidden="true">
                      {{ (row.display_name || row.name).slice(0, 1).toUpperCase() }}
                    </span>
                    <div class="agent-cell__names">
                      <span class="bc-strong mono">{{ row.name }}</span>
                      <span class="bc-muted bc-text-xs">{{ row.display_name || '—' }}</span>
                    </div>
                    <el-tag v-if="row.is_default" size="small" type="success" effect="plain" class="agent-cell__default">默认</el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="IM 接入" min-width="190">
                <template #default="{ row }">
                  <!-- 专属 Bot：凭证 + 连接状态就地可见，重连不进详情页 -->
                  <div v-if="row.has_im_config" class="im-cell">
                    <el-tag size="small" type="success">专属 Bot</el-tag>
                    <el-tooltip
                      :content="botConnTooltip(row)"
                      :disabled="!connOf(row.id)?.last_error"
                      placement="top"
                    >
                      <span><StatusBadge :type="botConnType(row.id)" :text="botConnText(row.id)" /></span>
                    </el-tooltip>
                    <el-button
                      link
                      :icon="Refresh"
                      aria-label="重连专属 Bot"
                      :loading="reloadingId === row.id"
                      @click="onReconnectBot(row)"
                    />
                  </div>
                  <el-tag v-else-if="row.channel === 'wecom'" size="small" type="info" effect="plain">默认 Bot</el-tag>
                  <span v-else class="bc-muted bc-text-sm">—</span>
                </template>
              </el-table-column>
              <el-table-column label="主渠道" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="channelType(row.channel)" effect="plain">{{ channelLabel(row.channel) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="人格" width="100">
                <template #default="{ row }">
                  <span class="bc-text-sm mono">{{ row.system_prompt_version != null ? `v${row.system_prompt_version}` : '角色回退' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="模型" width="150">
                <template #default="{ row }">
                  <span class="bc-text-sm">{{ modelName(row.model_id) || '全局' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="能力边界" min-width="220">
                <template #default="{ row }">
                  <div class="bc-text-xs bc-muted" style="line-height:1.5">
                    <div>技能: {{ row.skills?.length ? row.skills.join(', ') : '— (沿用角色)' }}</div>
                    <div>工具: {{ row.allowed_tools?.length ? row.allowed_tools.slice(0,3).join(', ') + (row.allowed_tools.length>3?' …':'') : '— (沿用角色)' }}</div>
                    <div>知识: {{ row.knowledge_scope_tags?.length ? row.knowledge_scope_tags.join(', ') : '— (沿用角色)' }}</div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="可见角色" width="130">
                <template #default="{ row }">
                  <span class="bc-text-sm">{{ row.visible_roles?.length ? row.visible_roles.join(', ') : '全部' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="启用" width="80" align="center">
                <template #default="{ row }">
                  <el-switch :model-value="row.is_enabled" @change="(val) => onToggleEnabled(row, val)" />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="250" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" type="primary" @click="goDetail(row)">配置</el-button>
                  <el-button size="small" @click="goChat(row)">对话</el-button>
                  <el-button v-if="!row.is_default" size="small" type="warning" plain @click="onSetDefault(row)">设为默认</el-button>
                  <el-button size="small" type="danger" plain :disabled="row.is_default" @click="onDelete(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <EmptyState
              v-if="!filtered.length"
              icon="Search"
              title="没有匹配的实例"
              description="换个关键词，或清除筛选。"
            >
              <el-button @click="keyword=''; statusFilter='all'">清除筛选</el-button>
            </EmptyState>
          </div>

          <div class="table-foot">
            <span>会话按 实例×用户×渠道 独立隔离 · 禁用后新会话拒绝，历史只读</span>
            <span class="mono table-foot__num">共 {{ filtered.length }} / {{ agents.length }}</span>
          </div>
        </template>
      </div>
    </template>

    <!-- 新建对话框（精简：创建后进详情页完成其余配置） -->
    <el-dialog v-model="dialogVisible" title="新建数字员工" width="520px">
      <el-form :model="form" label-position="top">
        <el-form-item label="Slug (唯一标识)">
          <el-input v-model="form.name" placeholder="小写字母/数字/连字符，如 sales-assistant" />
          <div class="bc-hint">作 API 寻址键，创建后不可改</div>
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" placeholder="如 销售助手" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明该数字员工的职责与边界" />
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="主渠道">
              <el-select v-model="form.channel" style="width:100%">
                <el-option label="对话测试 (api)" value="api" />
                <el-option label="企业微信 (wecom)" value="wecom" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="设为默认">
              <el-switch v-model="form.is_default" />
              <div class="bc-hint">全局唯一默认；无归属会话的兜底入口</div>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onCreate">创建并配置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { admin } from '../api'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const router = useRouter()
const agents = ref([])
const chatModels = ref([])
const connections = ref([])
const loading = ref(true)

// 新建弹窗（精简表单）
const dialogVisible = ref(false)
const saving = ref(false)
const form = ref({ name: '', display_name: '', description: '', channel: 'api', is_default: false })

const keyword = ref('')
const statusFilter = ref('all')
const reloadingId = ref(null)

const enabledCount = computed(() => agents.value.filter(a => a.is_enabled).length)
const disabledCount = computed(() => agents.value.length - enabledCount.value)
const defaultAgent = computed(() => agents.value.find(a => a.is_default) || null)
const botCount = computed(() => agents.value.filter(a => a.has_im_config).length)
const connectedBotCount = computed(() =>
  agents.value.filter((a) => a.has_im_config && connOf(a.id)?.is_connected).length,
)

const statusChips = computed(() => [
  { value: 'all', label: '全部', count: agents.value.length },
  { value: 'enabled', label: '启用', count: enabledCount.value },
  { value: 'disabled', label: '停用', count: disabledCount.value },
])

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return agents.value.filter(a => {
    if (statusFilter.value === 'enabled' && !a.is_enabled) return false
    if (statusFilter.value === 'disabled' && a.is_enabled) return false
    if (kw && !`${a.name} ${a.display_name || ''} ${a.description || ''}`.toLowerCase().includes(kw)) return false
    return true
  })
})

// 该实例的专属 Bot 槽位连接（未建槽位 = 凭证未生效或实例已停用）
function connOf(agentId) {
  return connections.value.find((c) => c.agent_id === agentId) || null
}
function botConnType(agentId) {
  const c = connOf(agentId)
  if (!c) return 'idle'
  return c.is_connected ? 'ok' : 'err'
}
function botConnText(agentId) {
  const c = connOf(agentId)
  if (!c) return '未运行'
  return c.is_connected ? '已连接' : '未连接'
}
function botConnTooltip(row) {
  const c = connOf(row.id)
  return c?.last_error || botConnText(row.id)
}

function channelType(ch) { return { api: 'info', wecom: 'success' }[ch] || 'info' }
function channelLabel(ch) { return { api: '对话测试', wecom: '企微' }[ch] || ch }
function modelName(id) {
  if (id == null) return ''
  const m = chatModels.value.find(x => x.id === id)
  return m ? (m.display_name || m.name) : `#${id}`
}

async function load() {
  loading.value = true
  try {
    const [ag, mods, status] = await Promise.all([
      admin.listAgents(),
      admin.listModels('chat').catch(() => []),
      admin.imStatus().catch(() => ({ channels: [] })),
    ])
    agents.value = ag
    chatModels.value = mods
    connections.value = status.channels || []
  } finally {
    loading.value = false
  }
}

async function refreshConnections() {
  const status = await admin.imStatus().catch(() => ({ channels: [] }))
  connections.value = status.channels || []
}

function openCreate() {
  form.value = { name: '', display_name: '', description: '', channel: 'api', is_default: false }
  dialogVisible.value = true
}

async function onCreate() {
  const name = form.value.name.trim()
  if (!name) { ElMessage.warning('Slug 不能为空'); return }
  if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) { ElMessage.warning('Slug 需小写字母/数字/连字符，首字符非连字符'); return }
  saving.value = true
  try {
    const created = await admin.createAgent({ ...form.value, name })
    ElMessage.success('已创建，继续完成详情配置')
    dialogVisible.value = false
    router.push({ name: 'agent-detail', params: { id: String(created.id) } })
  } finally { saving.value = false }
}

function goDetail(row) {
  router.push({ name: 'agent-detail', params: { id: String(row.id) } })
}

// 对话测试：预选该实例开新对话（会话按实例隔离）
function goChat(row) {
  router.push({ name: 'chat', query: { agent_id: String(row.id) } })
}

async function onToggleEnabled(row, val) {
  try {
    await admin.updateAgent(row.id, { is_enabled: val })
    row.is_enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
    await refreshConnections()
  } catch {
    row.is_enabled = !val
  }
}

async function onReconnectBot(row) {
  reloadingId.value = row.id
  try {
    const r = await admin.reloadIm(row.id)
    ElMessage.success(r.message || '专属 Bot 已重连')
    // 后端重连是异步的，稍候刷新连接状态
    setTimeout(refreshConnections, 1500)
  } catch {
    // 失败已由拦截器 toast
  } finally {
    reloadingId.value = null
  }
}

async function onSetDefault(row) {
  const ok = await ElMessageBox.confirm(`确定将「${row.display_name || row.name}」设为默认实例？`, '设为默认', { type: 'info' }).catch(() => null)
  if (!ok) return
  await admin.updateAgent(row.id, { is_default: true })
  ElMessage.success('已设为默认')
  await load()
}

async function onDelete(row) {
  if (row.is_default) { ElMessage.warning('默认实例不可直接删除，请先将默认切到其他实例'); return }
  const ok = await ElMessageBox.confirm(`确定删除数字员工「${row.name}」？\n会话引用该实例时将拒绝删除（需先归档会话）。`, '确认删除', { type: 'error' }).catch(() => null)
  if (!ok) return
  try {
    await admin.deleteAgent(row.id)
    ElMessage.success('已删除')
    await load()
  } catch {
    // 409 细节由拦截器 toast
  }
}

onMounted(load)
</script>

<style scoped>
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--bc-space-4); margin-bottom: var(--bc-space-4); }
.kpi { padding: var(--bc-space-4); transition: box-shadow .15s var(--bc-ease, ease-out); }
.kpi:hover { box-shadow: var(--bc-shadow-sm); }
.kpi__label { font-size: var(--bc-text-xs); font-weight: var(--bc-weight-medium); color: var(--bc-text-3); letter-spacing: .02em; }
.kpi__value { margin-top: var(--bc-space-2); font-size: var(--bc-text-3xl); font-weight: var(--bc-weight-semibold); line-height: 1.1; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.kpi__meta { margin-top: var(--bc-space-2); font-size: var(--bc-text-xs); color: var(--bc-text-3); }
.filterbar { display: flex; align-items: center; gap: var(--bc-space-2); flex-wrap: wrap; padding: var(--bc-space-3) var(--bc-space-4) 0; }
.filterbar__search { width: 240px; }
.filterbar__spacer { flex: 1; }
.fchip { display:inline-flex; align-items:center; gap:6px; height:26px; padding:0 10px; border:1px solid var(--bc-border); border-radius:var(--bc-radius-full); background:transparent; color:var(--bc-text-2); font:500 var(--bc-text-xs)/1 inherit; font-family:inherit; cursor:pointer; transition: all .15s var(--bc-ease, ease-out); }
.fchip .n { font-family:var(--bc-font-mono); font-variant-numeric:tabular-nums; color:var(--bc-text-3); }
.fchip:hover { border-color:var(--bc-border-strong); color:var(--bc-text-1); }
.fchip.is-on { background:var(--bc-primary-6); border-color:var(--bc-primary-6); color:var(--bc-on-primary,#fff); }
.fchip.is-on .n { color:var(--bc-on-primary,#fff); opacity:.72; }
.table-scroll { overflow-x:auto; }
.table-foot { display:flex; align-items:center; justify-content:space-between; gap:var(--bc-space-3); border-top:1px solid var(--bc-border); padding:var(--bc-space-2) var(--bc-space-4); font-size:var(--bc-text-xs); color:var(--bc-text-3); }
.table-foot__num { font-variant-numeric:tabular-nums; }
.agent-cell { display:flex; align-items:center; gap:var(--bc-space-3); }
.agent-cell__avatar { width:30px; height:30px; border-radius:var(--bc-radius-full); background:var(--bc-primary-2); color:var(--bc-primary-8); font-size:var(--bc-text-xs); font-weight:var(--bc-weight-semibold); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.agent-cell__names { display:flex; flex-direction:column; line-height:1.3; }
.agent-cell__default { margin-left:4px; }
.im-cell { display:flex; align-items:center; gap:var(--bc-space-2); }
.im-cell :deep(.el-button.is-link) { padding: 2px; }
@media (max-width:768px){ .filterbar__search{width:100%; order:5} .filterbar__spacer{display:none} }
</style>
