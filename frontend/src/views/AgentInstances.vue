<template>
  <div class="bc-page">
    <PageHeader
      title="数字员工"
      description="按需创建的数字员工实例：人格 / 模型 / 能力边界 / 专属渠道围绕实例装配，会话按 实例×用户×渠道 独立隔离。平台不预置任何员工。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建数字员工</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="5" />

    <template v-else>
      <!-- ============ KPI 行（默认实例概念已移除：不再有"兜底员工"，见 ADR-0013） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">实例总数</div>
          <div class="kpi__value mono">{{ agents.length }}</div>
          <div class="kpi__meta">{{ enabledCount }} 启用 · {{ disabledCount }} 停用</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">专属渠道连接</div>
          <div class="kpi__value mono">{{ connCount }}</div>
          <div class="kpi__meta">
            {{ connectedConnCount }}/{{ connCount }} 已连接 · 一个实例可接多个渠道
          </div>
        </div>
      </div>

      <div class="bc-panel">
        <!-- ============ 筛选栏（无实例时不显示，避免空表头噪声） ============ -->
        <div v-if="agents.length" class="filterbar">
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
        </div>

        <EmptyState
          v-if="!agents.length"
          icon="Avatar"
          title="还没有数字员工"
          description="数字员工由你显式创建：起个名字、绑人格与模型、划定能力边界，需要接 IM 时再配专属渠道。平台不预置「默认员工」——空组织就该是空的。"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个数字员工</el-button>
        </EmptyState>

        <template v-else>
          <div class="table-scroll">
            <el-table v-if="filtered.length" :data="filtered" row-key="id">
              <!-- 展开行：能力边界明细留在二级层，主行只给可扫读的摘要 -->
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="agent-detail">
                    <div class="agent-detail__grid">
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">绑定技能</span>
                        <span class="agent-detail__v">{{ listText(row.skills, '沿用角色') }}</span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">工具白名单</span>
                        <span class="agent-detail__v">{{ listText(row.allowed_tools, '沿用角色') }}</span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">知识域标签</span>
                        <span class="agent-detail__v">{{ listText(row.knowledge_scope_tags, '沿用角色') }}</span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">可见角色</span>
                        <span class="agent-detail__v">
                          {{ row.visible_roles?.length ? row.visible_roles.join('、') : '全部角色' }}
                        </span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">内部标识</span>
                        <span class="agent-detail__v mono">{{ row.name }}</span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">主渠道</span>
                        <span class="agent-detail__v">{{ channelLabel(row.channel) }}</span>
                      </div>
                      <div class="agent-detail__item">
                        <span class="agent-detail__k">最近更新</span>
                        <span class="agent-detail__v">{{ formatTime(row.updated_at) || '—' }}</span>
                      </div>
                    </div>
                    <p class="bc-hint">
                      「沿用角色」= 该实例未覆盖此项，直接用用户角色的配置；一旦填写，工具与知识域只会在角色范围内收窄。
                      想看某个角色实际生效的能力，进详情页「生效预览」。
                    </p>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="实例" min-width="230">
                <template #default="{ row }">
                  <div class="agent-cell">
                    <span class="agent-cell__avatar" aria-hidden="true">
                      <img v-if="row.avatar_url" :src="row.avatar_url" alt="" />
                      <template v-else>{{ (row.display_name || row.name).slice(0, 1).toUpperCase() }}</template>
                    </span>
                    <div class="agent-cell__names">
                      <span class="bc-strong">{{ row.display_name || row.name }}</span>
                      <span v-if="row.description" class="bc-text-xs bc-muted agent-cell__desc">{{ row.description }}</span>
                    </div>
                    <el-tag v-if="!row.is_enabled" size="small" type="info" effect="plain">停用</el-tag>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="装配" min-width="235">
                <template #default="{ row }">
                  <div class="assembly">
                    <span>人格 {{ row.system_prompt_version != null ? `v${row.system_prompt_version}` : '沿用角色' }}</span>
                    <span>模型 {{ row.model_id != null ? modelLabel(row.model_id) : '全局激活' }}</span>
                    <span>工具 {{ row.allowed_tools?.length ? `${row.allowed_tools.length} 项覆盖` : '沿用角色' }}</span>
                    <span>技能 {{ row.skills?.length ? `${row.skills.length} 项` : '沿用角色' }}</span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="接入" min-width="210">
                <template #default="{ row }">
                  <!-- 专属渠道：渠道标签 + 聚合连接状态就地可见，重连不进详情页 -->
                  <div v-if="imChannelsOf(row).length" class="im-cell">
                    <el-tag
                      v-for="ch in imChannelsOf(row)"
                      :key="ch"
                      size="small"
                      type="success"
                    >{{ channelLabel(ch) }}专属</el-tag>
                    <el-tooltip
                      :content="connTooltip(row)"
                      :disabled="!connsOf(row.id).some((c) => c.last_error)"
                      placement="top"
                    >
                      <span><StatusBadge :type="connType(row.id)" :text="connText(row.id)" /></span>
                    </el-tooltip>
                    <el-button
                      link
                      :icon="Refresh"
                      aria-label="重连专属连接"
                      :loading="reloadingId === row.id"
                      @click="onReconnectBot(row)"
                    />
                  </div>
                  <el-tooltip
                    v-else
                    content="未配置专属 Bot：IM 里的消息不会进入该员工（走全局 Bot 的会话不归属任何实例）"
                    placement="top"
                  >
                    <span class="bc-muted bc-text-sm">未接入专属渠道</span>
                  </el-tooltip>
                </template>
              </el-table-column>

              <el-table-column label="启用" width="76" align="center">
                <template #default="{ row }">
                  <el-switch :model-value="row.is_enabled" @change="(val) => onToggleEnabled(row, val)" />
                </template>
              </el-table-column>

              <el-table-column label="操作" width="168" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" type="primary" @click="goDetail(row)">配置</el-button>
                  <el-button size="small" @click="goChat(row)">对话</el-button>
                  <el-dropdown trigger="click" @command="(cmd) => onRowCommand(cmd, row)">
                    <el-button size="small" text :icon="MoreFilled" aria-label="更多操作" />
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="delete" :icon="Delete">删除</el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
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
        <el-form-item label="名称">
          <el-input v-model="form.display_name" placeholder="如 销售助手" />
          <div class="bc-hint">员工在列表与对话里显示的名字；内部标识由系统自动生成，无需你操心</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明该数字员工的职责与边界" />
        </el-form-item>
        <el-form-item label="主渠道">
          <el-select v-model="form.channel" style="width:100%">
            <el-option label="对话测试 (api)" value="api" />
            <el-option label="企业微信 (wecom)" value="wecom" />
          </el-select>
          <div class="bc-hint">
            仅作标签与筛选；真正接 IM 需在详情页「渠道」区配置专属 Bot
          </div>
        </el-form-item>
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
import { Delete, MoreFilled, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatTime } from '../utils/format'

const router = useRouter()
const agents = ref([])
const chatModels = ref([])
const connections = ref([])
const loading = ref(true)

// 新建弹窗（精简表单）
const dialogVisible = ref(false)
const saving = ref(false)
const form = ref({ display_name: '', description: '', channel: 'api' })

const keyword = ref('')
const statusFilter = ref('all')
const reloadingId = ref(null)

const enabledCount = computed(() => agents.value.filter(a => a.is_enabled).length)
const disabledCount = computed(() => agents.value.length - enabledCount.value)
// 专属连接数：一个实例接入多个渠道时按连接（渠道）计，而非按实例计
const connCount = computed(() =>
  agents.value.reduce((n, a) => n + imChannelsOf(a).length, 0),
)
const connectedConnCount = computed(() =>
  connections.value.filter((c) => c.agent_id != null && c.is_connected).length,
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

// 该实例已配置专属凭证的渠道列表（im_channels 缺失时回退 has_im_config 单渠道形态）
function imChannelsOf(agent) {
  if (agent.im_channels?.length) return agent.im_channels
  return agent.has_im_config ? ['wecom'] : []
}
// 该实例的全部专属连接槽位（一个实例多渠道 = 多条；未建槽位 = 凭证未生效或实例已停用）
function connsOf(agentId) {
  return connections.value.filter((c) => c.agent_id === agentId)
}
function connType(agentId) {
  const conns = connsOf(agentId)
  if (!conns.length) return 'idle'
  return conns.some((c) => c.is_connected) ? 'ok' : 'err'
}
function connText(agentId) {
  const conns = connsOf(agentId)
  if (!conns.length) return '未运行'
  const ok = conns.filter((c) => c.is_connected).length
  // 多渠道时显示聚合（如 已连接 1/2）；单渠道保持原展示
  return conns.length > 1 ? `已连接 ${ok}/${conns.length}` : ok ? '已连接' : '未连接'
}
function connTooltip(row) {
  const conns = connsOf(row.id)
  const errs = conns.filter((c) => c.last_error).map((c) => `${c.key}: ${c.last_error}`)
  return errs.join('\n') || connText(row.id)
}

function channelLabel(ch) { return { api: '对话测试', wecom: '企微', feishu: '飞书' }[ch] || ch }
function listText(list, fallback) {
  return list?.length ? list.join('、') : `— (${fallback})`
}
function modelLabel(id) {
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
  form.value = { display_name: '', description: '', channel: 'api' }
  dialogVisible.value = true
}

async function onCreate() {
  const displayName = form.value.display_name.trim()
  if (!displayName) { ElMessage.warning('请填写名称'); return }
  saving.value = true
  try {
    const created = await admin.createAgent({ ...form.value, display_name: displayName })
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

function onRowCommand(cmd, row) {
  if (cmd === 'delete') onDelete(row)
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
    ElMessage.success(r.message || '专属连接已重连')
    // 后端重连是异步的，稍候刷新连接状态
    setTimeout(refreshConnections, 1500)
  } catch {
    // 失败已由拦截器 toast
  } finally {
    reloadingId.value = null
  }
}

async function onDelete(row) {
  const ok = await ElMessageBox.confirm(
    `确定删除数字员工「${row.name}」？\n其专属渠道连接会立即断开；若仍有会话引用该实例，删除会被拒绝（需先归档会话）。`,
    '确认删除',
    { type: 'error' },
  ).catch(() => null)
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
.kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--bc-space-4); margin-bottom: var(--bc-space-4); }
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
.agent-cell__avatar { width:30px; height:30px; border-radius:var(--bc-radius-full); background:var(--bc-primary-2); color:var(--bc-primary-8); font-size:var(--bc-text-xs); font-weight:var(--bc-weight-semibold); display:flex; align-items:center; justify-content:center; flex-shrink:0; overflow:hidden; }
.agent-cell__avatar img { width:100%; height:100%; object-fit:cover; display:block; }
.agent-cell__names { display:flex; flex-direction:column; line-height:1.3; min-width:0; }
.agent-cell__desc { max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.assembly { display:flex; flex-wrap:wrap; gap:4px var(--bc-space-3); font-size:var(--bc-text-xs); color:var(--bc-text-2); }
.im-cell { display:flex; align-items:center; gap:var(--bc-space-2); flex-wrap:wrap; }
.im-cell :deep(.el-button.is-link) { padding: 2px; }
.agent-detail { padding: var(--bc-space-2) var(--bc-space-4) var(--bc-space-4); }
.agent-detail__grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:var(--bc-space-3) var(--bc-space-5); }
.agent-detail__item { display:flex; flex-direction:column; gap:2px; }
.agent-detail__k { font-size:var(--bc-text-xs); color:var(--bc-text-3); }
.agent-detail__v { font-size:var(--bc-text-sm); color:var(--bc-text-1); word-break:break-word; }
@media (max-width:768px){ .filterbar__search{width:100%; order:5} .filterbar__spacer{display:none} }
</style>
