<template>
  <div class="bc-page">
    <PageHeader
      title="数字员工"
      description="全托管实例：人格（提示词）/ 模型 / 技能 / 知识域 / 可见角色 / 渠道与审批策略 — 会话按实例×用户×渠道独立隔离。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建数字员工</el-button>
    </PageHeader>

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
          <div class="kpi__label">渠道分布</div>
          <div class="kpi__value mono" style="font-size: var(--bc-text-lg)">{{ channelSummary || '—' }}</div>
          <div class="kpi__meta">{{ agents.length ? 'api / wecom / feishu' : '暂无实例' }}</div>
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
            v-for="c in channelChips"
            :key="c.value"
            type="button"
            class="fchip"
            :class="{ 'is-on': channelFilter === c.value }"
            @click="channelFilter = c.value"
          >
            {{ c.label }} <span class="n">{{ c.count }}</span>
          </button>
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
          description="新建一个数字员工实例，绑定人格、模型与能力边界，员工即可在 IM 或对话测试中与其协作。"
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
              <el-table-column label="渠道" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="channelType(row.channel)">{{ channelLabel(row.channel) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="人格" width="120">
                <template #default="{ row }">
                  <span class="bc-text-sm mono">{{ row.system_prompt_version != null ? `v${row.system_prompt_version}` : '—' }}</span>
                  <span v-if="row.system_prompt_version != null" class="bc-muted bc-text-xs"> / 角色回退</span>
                </template>
              </el-table-column>
              <el-table-column label="模型" width="150">
                <template #default="{ row }">
                  <span class="bc-text-sm">{{ modelName(row.model_id) || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="技能 / 工具 / 知识域" min-width="220">
                <template #default="{ row }">
                  <div class="bc-text-xs bc-muted" style="line-height:1.5">
                    <div>技能: {{ row.skills?.length ? row.skills.join(', ') : '— (沿用角色)' }}</div>
                    <div>工具: {{ row.allowed_tools?.length ? row.allowed_tools.slice(0,3).join(', ') + (row.allowed_tools.length>3?' …':'') : '—' }}</div>
                    <div>知识: {{ row.knowledge_scope_tags?.length ? row.knowledge_scope_tags.join(', ') : '—' }}</div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="可见角色" width="140">
                <template #default="{ row }">
                  <span class="bc-text-sm">{{ row.visible_roles?.length ? row.visible_roles.join(', ') : '全部' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="启用" width="90" align="center">
                <template #default="{ row }">
                  <el-switch :model-value="row.is_enabled" @change="(val) => onToggleEnabled(row, val)" />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="220" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" @click="openEdit(row)">编辑</el-button>
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
              <el-button @click="keyword=''; channelFilter='all'; statusFilter='all'">清除筛选</el-button>
            </EmptyState>
          </div>

          <div class="table-foot">
            <span>会话按 实例×用户×渠道 独立隔离 · 禁用后新会话拒绝，历史只读</span>
            <span class="mono table-foot__num">共 {{ filtered.length }} / {{ agents.length }}</span>
          </div>
        </template>
      </div>
    </template>

    <!-- 新建/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑数字员工' : '新建数字员工'" width="760px" top="5vh">
      <el-form :model="form" label-position="top">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item v-if="!editing" label="Slug (唯一标识)">
              <el-input v-model="form.name" placeholder="小写字母/数字/连字符，如 sales-assistant" />
              <div class="bc-hint">作 API 寻址键，创建后不可改</div>
            </el-form-item>
            <el-form-item v-else label="Slug">
              <el-input :model-value="form.name" disabled />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="显示名">
              <el-input v-model="form.display_name" placeholder="如 销售助手" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明该数字员工的职责与边界" />
        </el-form-item>

        <el-form-item label="头像 URL">
          <el-input v-model="form.avatar_url" placeholder="https://...（可选，前端展示用）" />
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="主渠道">
              <el-select v-model="form.channel" style="width:100%">
                <el-option label="对话测试 (api)" value="api" />
                <el-option label="企业微信 (wecom)" value="wecom" />
                <el-option label="飞书 (feishu)" value="feishu" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="绑定人格版本">
              <el-select v-model="form.system_prompt_version" clearable placeholder="沿用角色/全局" style="width:100%">
                <el-option :label="'— 沿用角色/全局'" :value="null" />
                <el-option v-for="p in prompts" :key="p.version" :label="`v${p.version} ${p.label || ''} ${p.is_active?'(激活)':''}`" :value="p.version" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="绑定对话模型">
              <el-select v-model="form.model_id" clearable placeholder="沿用会话/全局" style="width:100%">
                <el-option :label="'— 沿用会话/全局'" :value="null" />
                <el-option v-for="m in chatModels" :key="m.id" :label="`${m.display_name || m.name} (${m.name})`" :value="m.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="绑定技能">
          <el-select v-model="form.skills" multiple filterable allow-create clearable placeholder="独立技能（为空=沿用角色，可多选）" style="width:100%">
            <el-option v-for="s in allSkills" :key="s.name" :label="`${s.name} — ${s.description || ''}`" :value="s.name" />
          </el-select>
          <div class="bc-hint">实例技能与角色技能取并集（实例补充能力，角色仍是天花板）</div>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="知识域标签覆盖">
              <el-select v-model="form.knowledge_scope_tags" multiple filterable allow-create clearable placeholder="为空=沿用角色，与角色取交集" style="width:100%">
                <el-option v-for="tag in sourceOptions" :key="tag" :label="tag" :value="tag" />
              </el-select>
              <div class="bc-hint">与角色 knowledge_scope_tags 取交集（收紧）</div>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="工具白名单覆盖">
              <el-select v-model="form.allowed_tools" multiple filterable allow-create clearable placeholder="为空=沿用角色，与角色取交集" style="width:100%">
                <el-option v-for="t in toolOptions" :key="t.name" :label="`${t.name} — ${t.description?.slice(0,40) || ''}`" :value="t.name" />
              </el-select>
              <div class="bc-hint">与角色 allowed_tools 取交集（实例不能突破角色）</div>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="可见角色">
          <el-select v-model="form.visible_roles" multiple clearable placeholder="为空=全部角色可见" style="width:100%">
            <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
          </el-select>
          <div class="bc-hint">限制哪些角色可在对话中选用该实例；admin 恒可见</div>
        </el-form-item>

        <el-divider content-position="left">全托管：渠道与审批策略</el-divider>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="企微 Bot ID（per-Agent 覆盖，可选）">
              <el-input v-model="form.im_wecom_bot_id" placeholder="留空=沿用全局 SystemSetting" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="企微 Secret（per-Agent 覆盖）">
              <el-input v-model="form.im_wecom_secret" placeholder="留空=沿用全局，不修改则留空" show-password />
              <div class="bc-hint">加密存库，API 仅回脱敏预览</div>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="审批策略覆盖 (JSON)">
          <el-input v-model="form.approval_policy_json" type="textarea" :rows="3" placeholder='如 {"require_approval_tools": ["mcp__xxx__delete"]}' />
          <div class="bc-hint">与全局工具 destructiveHint 取并集（实例可追加需审批工具）</div>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="启用">
              <el-switch v-model="form.is_enabled" active-text="启用" inactive-text="停用" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="设为默认">
              <el-switch v-model="form.is_default" />
              <div class="bc-hint">全局唯一默认，新会话 fallback 用</div>
            </el-form-item>
          </el-col>
          <el-col :span="8" v-if="editing && currentHasIm">
            <div class="bc-hint" style="margin-top: 28px">已配置 per-Agent IM 凭证</div>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible=false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { admin } from '../api'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const agents = ref([])
const prompts = ref([])
const chatModels = ref([])
const allSkills = ref([])
const toolOptions = ref([])
const roleOptions = ref([])
const sourceOptions = ref([])

const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
  emptyForm: () => ({
    name: '',
    display_name: '',
    description: '',
    avatar_url: '',
    channel: 'api',
    system_prompt_version: null,
    model_id: null,
    skills: null,
    knowledge_scope_tags: null,
    allowed_tools: null,
    visible_roles: null,
    im_wecom_bot_id: '',
    im_wecom_secret: '',
    approval_policy_json: '',
    is_enabled: true,
    is_default: false,
  }),
  toForm: (row) => {
    let imBot = ''
    let imSecret = ''
    if (row.im_config_preview) {
      if (row.im_config_preview.wecom_bot_id) imBot = row.im_config_preview.wecom_bot_id === '***' ? '' : row.im_config_preview.wecom_bot_id
    }
    let policyJson = ''
    if (row.approval_policy) {
      try { policyJson = JSON.stringify(row.approval_policy, null, 2) } catch { policyJson = '' }
    }
    return {
      name: row.name,
      display_name: row.display_name,
      description: row.description,
      avatar_url: row.avatar_url,
      channel: row.channel || 'api',
      system_prompt_version: row.system_prompt_version ?? null,
      model_id: row.model_id ?? null,
      skills: row.skills ?? null,
      knowledge_scope_tags: row.knowledge_scope_tags ?? null,
      allowed_tools: row.allowed_tools ?? null,
      visible_roles: row.visible_roles ?? null,
      im_wecom_bot_id: imBot,
      im_wecom_secret: '',
      approval_policy_json: policyJson,
      is_enabled: row.is_enabled,
      is_default: row.is_default,
      _id: row.id,
      _has_im: row.has_im_config,
    }
  },
})

const keyword = ref('')
const channelFilter = ref('all')
const statusFilter = ref('all')

const enabledCount = computed(() => agents.value.filter(a => a.is_enabled).length)
const disabledCount = computed(() => agents.value.length - enabledCount.value)
const defaultAgent = computed(() => agents.value.find(a => a.is_default) || null)
const currentHasIm = computed(() => !!form._has_im)

const channelSummary = computed(() => {
  const m = new Map()
  for (const a of agents.value) m.set(a.channel, (m.get(a.channel) || 0) + 1)
  if (!m.size) return ''
  return [...m.entries()].map(([k,c]) => `${k}:${c}`).join(' · ')
})

const channelChips = computed(() => {
  const counts = new Map()
  for (const a of agents.value) counts.set(a.channel, (counts.get(a.channel) || 0) + 1)
  return [
    { value: 'all', label: '全部渠道', count: agents.value.length },
    ...['api','wecom','feishu'].map(v => ({ value: v, label: v, count: counts.get(v) || 0 })),
  ]
})
const statusChips = computed(() => [
  { value: 'all', label: '全部', count: agents.value.length },
  { value: 'enabled', label: '启用', count: enabledCount.value },
  { value: 'disabled', label: '停用', count: disabledCount.value },
])

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return agents.value.filter(a => {
    if (channelFilter.value !== 'all' && a.channel !== channelFilter.value) return false
    if (statusFilter.value === 'enabled' && !a.is_enabled) return false
    if (statusFilter.value === 'disabled' && a.is_enabled) return false
    if (kw && !`${a.name} ${a.display_name || ''} ${a.description || ''}`.toLowerCase().includes(kw)) return false
    return true
  })
})

function channelType(ch) { return { api:'info', wecom:'success', feishu:'primary'}[ch] || 'info' }
function channelLabel(ch) { return { api:'对话测试', wecom:'企微', feishu:'飞书'}[ch] || ch }
function modelName(id) {
  if (id == null) return ''
  const m = chatModels.value.find(x => x.id === id)
  return m ? (m.display_name || m.name) : `#${id}`
}

async function load() {
  loading.value = true
  try {
    const [ag, pr, mods, sks, tools, roles, sources] = await Promise.all([
      admin.listAgents(),
      admin.listPrompts().catch(() => []),
      admin.listModels('chat').catch(() => []),
      admin.listSkills().catch(() => []),
      admin.listTools().catch(() => []),
      admin.listRoles().catch(() => []),
      admin.listSources().catch(() => []),
    ])
    agents.value = ag
    prompts.value = pr
    chatModels.value = mods
    allSkills.value = sks
    toolOptions.value = tools
    roleOptions.value = ['admin', ...roles.map(r => r.name)]
    sourceOptions.value = sources || []
  } finally {
    loading.value = false
  }
}

function buildPayload() {
  // skills / tags / tools：空数组→传 []（清空覆盖）；null→未覆盖
  // visible_roles 同理
  const payload = {
    display_name: form.display_name,
    description: form.description,
    avatar_url: form.avatar_url,
    channel: form.channel,
    system_prompt_version: form.system_prompt_version,
    model_id: form.model_id,
    skills: form.skills,
    knowledge_scope_tags: form.knowledge_scope_tags,
    allowed_tools: form.allowed_tools,
    visible_roles: form.visible_roles,
    is_enabled: form.is_enabled,
    is_default: form.is_default,
  }
  // im_config：仅 wecom 两个字段示例，后续可扩展为通用 JSON
  const im = {}
  if (form.im_wecom_bot_id) im.wecom_bot_id = form.im_wecom_bot_id.trim()
  if (form.im_wecom_secret) im.wecom_secret = form.im_wecom_secret.trim()
  // 若用户清空了 bot_id 但原来有值，需传 {} 清空；这里用 model_fields_set 思路在前端用显式标记：
  // 当编辑且用户把 bot_id 清空且原来有配置，则传 {} 清空
  if (editing.value) {
    if (!form.im_wecom_bot_id && !form.im_wecom_secret) {
      // 若原来有配置且现在都空，是否要清空？保守：不传（不改），除非用户显式想清空——暂不自动清空
      // 只有当用户在输入框里有改动且都空时才认为清空，需用一个额外 flag；简化：有任一非空才传
      if (im.wecom_bot_id || im.wecom_secret) payload.im_config = im
    } else if (Object.keys(im).length) {
      payload.im_config = im
    }
  } else {
    if (Object.keys(im).length) payload.im_config = im
  }
  if (form.approval_policy_json && form.approval_policy_json.trim()) {
    try {
      payload.approval_policy = JSON.parse(form.approval_policy_json)
    } catch {
      ElMessage.warning('审批策略 JSON 解析失败，已忽略')
    }
  } else if (editing.value && form.approval_policy_json === '') {
    // 允许清空：传 null
    payload.approval_policy = null
  }
  return payload
}

async function onSave() {
  if (!editing.value && !form.name.trim()) { ElMessage.warning('Slug 不能为空'); return }
  if (!editing.value && !/^[a-z0-9][a-z0-9-]*$/.test(form.name.trim())) { ElMessage.warning('Slug 需小写字母/数字/连字符，首字符非连字符'); return }
  saving.value = true
  try {
    if (editing.value) {
      await admin.updateAgent(form._id, buildPayload())
    } else {
      await admin.createAgent({ name: form.name.trim(), ...buildPayload() })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } finally { saving.value = false }
}

async function onToggleEnabled(row, val) {
  try {
    await admin.updateAgent(row.id, { is_enabled: val })
    row.is_enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
  } catch {
    row.is_enabled = !val
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
  } catch (e) {
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
@media (max-width:768px){ .filterbar__search{width:100%; order:5} .filterbar__spacer{display:none} }
</style>
