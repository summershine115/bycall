<template>
  <div class="bc-page">
    <ContentLoader v-if="loading" :rows="8" />

    <template v-else-if="agent">
      <PageHeader :title="agent.display_name || agent.name" :description="agent.description || `数字员工实例 · 标识 ${agent.name}`">
        <el-button :icon="ArrowLeft" @click="goBack">返回列表</el-button>
        <el-button :icon="ChatDotRound" @click="goChat">对话测试</el-button>
      </PageHeader>

      <!-- 状态条：启用/停用 · 专属渠道 · 主渠道 -->
      <div class="statusbar">
        <el-tag :type="agent.is_enabled ? 'success' : 'info'" effect="plain">
          {{ agent.is_enabled ? '已启用' : '已停用' }}
        </el-tag>
        <el-tag v-for="ch in imChannels" :key="ch" type="success">{{ channelLabel(ch) }}专属</el-tag>
        <el-tag type="info" effect="plain">主渠道：{{ channelLabel(agent.channel) }}</el-tag>
        <span class="mono bc-muted bc-text-xs statusbar__id">#{{ agent.id }} · {{ agent.name }}</span>
      </div>

      <div class="detail-layout">
        <!-- ============ 左：分段导航（只渲染当前段，不再一条长滚动） ============ -->
        <nav class="sections" aria-label="配置分区">
          <button
            v-for="s in sections"
            :key="s.key"
            type="button"
            class="sections__item"
            :class="{ 'is-on': activeSection === s.key }"
            :aria-current="activeSection === s.key ? 'true' : undefined"
            @click="activeSection = s.key"
          >
            <el-icon :size="15" aria-hidden="true"><component :is="s.icon" /></el-icon>
            <span class="sections__label">{{ s.label }}</span>
            <span
              v-if="dirty[s.key]"
              class="sections__dot"
              :title="'「' + s.label + '」有未保存的修改'"
              aria-hidden="true"
            />
          </button>
        </nav>

        <!-- ============ 右：当前分段 ============ -->
        <div class="section-body">
          <!-- ① 基础信息 -->
          <div v-show="activeSection === 'basic'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">基础信息</h3>
                <p class="bc-muted bc-text-sm section__desc">展示名、描述与启停。内部标识（{{ agent.name }}）由系统生成，仅供审计排障，不可改。</p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.basic" text @click="resetBasic">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.basic" :loading="savingKey === 'basic'" @click="saveBasic">保存</el-button>
              </div>
            </div>
            <el-form label-position="top">
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="显示名">
                    <el-input v-model="basic.display_name" placeholder="如 销售助手" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="头像 URL">
                    <el-input v-model="basic.avatar_url" placeholder="https://...（可选）" />
                  </el-form-item>
                </el-col>
              </el-row>
              <el-form-item label="描述">
                <el-input v-model="basic.description" placeholder="一句话说明该数字员工的职责与边界" />
              </el-form-item>
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="主渠道">
                    <el-select v-model="basic.channel" style="width:100%">
                      <el-option label="对话测试 (api)" value="api" />
                      <el-option label="企业微信 (wecom)" value="wecom" />
                    </el-select>
                    <div class="bc-hint">仅作标签与筛选；真正接 IM 见「渠道接入」区</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="启用">
                    <el-switch v-model="basic.is_enabled" active-text="启用" inactive-text="停用" />
                    <div class="bc-hint">停用后新会话拒绝、历史会话只读，专属连接停止</div>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ② 人格与模型 -->
          <div v-show="activeSection === 'persona'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">人格与模型</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  留空 = 沿用角色/全局链路（提示词：会话 &gt; 实例 &gt; 角色 &gt; 全局激活；模型：会话 &gt; 实例 &gt; 灰度 &gt; 全局）。
                </p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.persona" text @click="resetPersona">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.persona" :loading="savingKey === 'persona'" @click="savePersona">保存</el-button>
              </div>
            </div>
            <el-form label-position="top">
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="绑定人格版本">
                    <el-select v-model="persona.system_prompt_version" clearable placeholder="沿用角色/全局" style="width:100%">
                      <el-option label="— 沿用角色/全局" :value="null" />
                      <el-option v-for="p in prompts" :key="p.version" :label="`v${p.version} ${p.label || ''} ${p.is_active ? '(激活)' : ''}`" :value="p.version" />
                    </el-select>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="绑定对话模型">
                    <el-select v-model="persona.model_id" clearable placeholder="沿用会话/全局" style="width:100%">
                      <el-option label="— 沿用会话/全局" :value="null" />
                      <el-option v-for="m in chatModels" :key="m.id" :label="`${m.display_name || m.name} (${m.name})`" :value="m.id" />
                    </el-select>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ③ 能力边界 -->
          <div v-show="activeSection === 'capability'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">能力边界</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  角色是天花板：技能与角色取<strong>并集</strong>（实例补充能力）；工具与知识域取<strong>交集</strong>（实例只能收窄）。留空 = 沿用角色。
                </p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.capability" text @click="resetCapability">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.capability" :loading="savingKey === 'capability'" @click="saveCapability">保存</el-button>
              </div>
            </div>
            <el-form label-position="top">
              <el-form-item label="绑定技能">
                <el-select v-model="capability.skills" multiple filterable clearable placeholder="为空=沿用角色（可多选）" style="width:100%">
                  <el-option v-for="s in allSkills" :key="s.name" :label="`${s.name} — ${s.description || ''}`" :value="s.name" />
                </el-select>
              </el-form-item>
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="知识域标签覆盖">
                    <el-select v-model="capability.knowledge_scope_tags" multiple filterable allow-create clearable placeholder="为空=沿用角色" style="width:100%">
                      <el-option v-for="tag in sourceOptions" :key="tag" :label="tag" :value="tag" />
                    </el-select>
                    <div class="bc-hint">与角色 knowledge_scope_tags 取交集（收紧）</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="工具白名单覆盖">
                    <el-select v-model="capability.allowed_tools" multiple filterable allow-create clearable placeholder="为空=沿用角色" style="width:100%">
                      <el-option v-for="t in toolOptions" :key="t.name" :label="`${t.name} — ${t.description?.slice(0, 40) || ''}`" :value="t.name" />
                    </el-select>
                    <div class="bc-hint">与角色 allowed_tools 取交集（实例不能突破角色）</div>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ④ 可见性 -->
          <div v-show="activeSection === 'visibility'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">可见性</h3>
                <p class="bc-muted bc-text-sm section__desc">限制哪些角色可在对话中选用该实例；admin 恒可见。</p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.visibility" text @click="resetVisibility">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.visibility" :loading="savingKey === 'visibility'" @click="saveVisibility">保存</el-button>
              </div>
            </div>
            <el-form label-position="top">
              <el-form-item label="可见角色">
                <el-select v-model="visibility.visible_roles" multiple clearable placeholder="为空=全部角色可见" style="width:100%">
                  <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
                </el-select>
              </el-form-item>
            </el-form>
          </div>

          <!-- ⑤ 渠道接入（专属连接，ADR-0012：同实例可接入多个渠道） -->
          <div v-show="activeSection === 'channel'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">渠道接入 · 专属连接</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  一个数字员工可同时接入多个不同渠道（每种渠道一条专属连接，会话按 实例×渠道 独立隔离）；
                  同一凭证（如企微 bot_id）全局只能绑定一个实例。全局默认 Bot 在「渠道接入」页维护。
                </p>
              </div>
              <div v-if="wecomConn" class="section__conn">
                <StatusBadge :type="wecomConn.is_connected ? 'ok' : 'err'" :text="wecomConn.is_connected ? '已连接' : '未连接'" />
                <span v-if="wecomConn.last_error" class="bc-text-xs bc-muted">{{ wecomConn.last_error }}</span>
                <el-button size="small" :loading="reconnecting" @click="onReconnectBot">重连企微</el-button>
              </div>
            </div>
            <el-form label-position="top">
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="企微 bot_id">
                    <el-input v-model="im.bot_id" :placeholder="hasWecom ? '已配置（***），修改需完整填写' : '企业微信后台「智能机器人」获取'" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="企微 bot_secret">
                    <el-input v-model="im.secret" :placeholder="hasWecom ? '已配置（***）—— 不修改则留空' : '企微后台获取的 secret'" show-password />
                  </el-form-item>
                </el-col>
              </el-row>
              <div class="bc-hint">
                保存仅替换企微渠道的凭证（不影响该实例其他渠道）并自动重连；修改时需同时填写 bot_id 与 secret。
              </div>
            </el-form>
            <div class="section__foot">
              <el-button v-if="hasWecom" type="danger" plain :loading="savingKey === 'im-clear'" @click="onClearIm">
                清除企微专属 Bot
              </el-button>
              <el-button type="primary" :disabled="!dirty.channel" :loading="savingKey === 'im'" @click="saveIm">保存并重连</el-button>
            </div>

            <!-- 飞书占位：接入后与企微并列为一项专属连接（网关实现排期中） -->
            <div class="soon">
              <div>
                <span class="bc-strong bc-text-sm">飞书专属连接</span>
                <span class="bc-muted bc-text-xs soon__hint">渠道抽象已预留（FeishuChannel），网关实现排期中</span>
              </div>
              <el-tag type="info" effect="plain" size="small">规划中</el-tag>
            </div>
          </div>

          <!-- ⑥ 生效预览：把「留空=沿用角色」变成可验证的结果 -->
          <div v-show="activeSection === 'preview'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">生效预览</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  选一个用户，看治理脊柱对该用户的真实判定——实例只能在角色天花板下收窄，这里显示收窄后的结果。
                  与数据面同源（computed by policy simulate），不产生真实调用、不留痕。
                </p>
              </div>
            </div>
            <div class="preview__picker">
              <el-select
                v-model="preview.userId"
                filterable
                clearable
                placeholder="选择用户（按该用户的角色计算）"
                style="width:320px"
                @change="runPreview"
              >
                <el-option v-for="u in users" :key="u.id" :label="`${u.display_name || u.name}（${u.role}）`" :value="u.id" />
              </el-select>
              <span class="bc-muted bc-text-xs">切换用户自动试算</span>
            </div>

            <div v-if="preview.loading" class="bc-muted bc-text-sm">试算中…</div>
            <div v-else-if="preview.result" class="preview">
              <div class="preview__row">
                <span class="preview__k">判定</span>
                <el-tag :type="preview.result.decision === 'allowed' ? 'success' : 'danger'" effect="plain">
                  {{ preview.result.decision === 'allowed' ? '允许' : '拒绝' }}
                </el-tag>
                <span class="bc-text-sm bc-muted">{{ preview.result.detail }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">生效人格</span>
                <span class="bc-text-sm">{{ effectivePrompt }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">生效模型</span>
                <span class="bc-text-sm">{{ effectiveModel }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可用工具</span>
                <span class="bc-text-sm">{{ toolsText }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可见知识域</span>
                <span class="bc-text-sm">{{ listText(preview.result.knowledge_scope_tags, '无') }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可见知识库</span>
                <span class="bc-text-sm">
                  {{ preview.result.is_admin ? '全部（admin）' : `${preview.result.visible_kb_ids.length} 个` }}
                </span>
              </div>
            </div>
            <p v-else class="bc-hint">选择一个用户后自动试算；未选用户时该实例按角色基线生效。</p>
          </div>
        </div>
      </div>
    </template>

    <EmptyState
      v-else
      icon="Avatar"
      title="数字员工不存在"
      description="可能已被删除，返回列表查看现有实例。"
    >
      <el-button type="primary" @click="goBack">返回列表</el-button>
    </EmptyState>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, ChatDotRound, Connection, DataAnalysis, InfoFilled, Lock, MagicStick, View,
} from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const router = useRouter()
const agentId = Number(route.params.id)

const loading = ref(true)
const agent = ref(null)
const savingKey = ref('')
const reconnecting = ref(false)
const imStatusData = ref({ channels: [] })

// 左导航分段：一次只渲染一段，改一项不必滚到底（ADR-0013）
const activeSection = ref('basic')
const sections = [
  { key: 'basic', label: '基础信息', icon: InfoFilled },
  { key: 'persona', label: '人格与模型', icon: MagicStick },
  { key: 'capability', label: '能力边界', icon: Lock },
  { key: 'visibility', label: '可见性', icon: View },
  { key: 'channel', label: '渠道接入', icon: Connection },
  { key: 'preview', label: '生效预览', icon: DataAnalysis },
]

// 分区表单：各段独立保存（后端 PATCH 语义，只传本区字段）
const basic = reactive({ display_name: '', description: '', avatar_url: '', channel: 'api', is_enabled: true })
const persona = reactive({ system_prompt_version: null, model_id: null })
const capability = reactive({ skills: null, knowledge_scope_tags: null, allowed_tools: null })
const visibility = reactive({ visible_roles: null })
const im = reactive({ bot_id: '', secret: '' })
// 各段"上次保存后"的基线：用于未保存提示与保存按钮可用态
const baseline = reactive({ basic: null, persona: null, capability: null, visibility: null })
// 已配置专属凭证的渠道列表（AgentOut.im_channels，随保存响应自动更新）
const imChannels = computed(() => agent.value?.im_channels || [])
const hasWecom = computed(() => imChannels.value.includes('wecom'))

// 选项
const prompts = ref([])
const chatModels = ref([])
const allSkills = ref([])
const toolOptions = ref([])
const roles = ref([])
const users = ref([])
const roleOptions = ref([])
const sourceOptions = ref([])

// 生效预览（策略模拟器同源端点：agent_id 让能力集按实例收窄）
const preview = reactive({ userId: null, loading: false, result: null })

const wecomConn = computed(() =>
  (imStatusData.value.channels || []).find(
    (c) => c.agent_id === agentId && (c.channel === 'wecom' || c.key?.startsWith('wecom:')),
  ) || null,
)

function channelLabel(ch) { return { api: '对话测试', wecom: '企微', feishu: '飞书' }[ch] || ch }
function listText(list, fallback) {
  return list?.length ? list.join('、') : fallback
}
function modelLabel(id) {
  const m = chatModels.value.find((x) => x.id === id)
  return m ? (m.display_name || m.name) : `#${id}`
}

// ============ 未保存改动 ============
function basicSnap() {
  return {
    display_name: basic.display_name,
    description: basic.description,
    avatar_url: basic.avatar_url,
    channel: basic.channel,
    is_enabled: basic.is_enabled,
  }
}
function personaSnap() {
  return { v: persona.system_prompt_version ?? null, m: persona.model_id ?? null }
}
function capabilitySnap() {
  return {
    s: capability.skills ?? null,
    t: capability.knowledge_scope_tags ?? null,
    a: capability.allowed_tools ?? null,
  }
}
function visibilitySnap() {
  return { r: visibility.visible_roles ?? null }
}
const dirty = computed(() => ({
  basic: JSON.stringify(basicSnap()) !== JSON.stringify(baseline.basic),
  persona: JSON.stringify(personaSnap()) !== JSON.stringify(baseline.persona),
  capability: JSON.stringify(capabilitySnap()) !== JSON.stringify(baseline.capability),
  visibility: JSON.stringify(visibilitySnap()) !== JSON.stringify(baseline.visibility),
  // 渠道区是 write-only 凭证：填了内容即视为待保存
  channel: Boolean(im.bot_id.trim() || im.secret.trim()),
}))

function markBaseline() {
  baseline.basic = basicSnap()
  baseline.persona = personaSnap()
  baseline.capability = capabilitySnap()
  baseline.visibility = visibilitySnap()
}

function resetBasic() { Object.assign(basic, baseline.basic) }
function resetPersona() { Object.assign(persona, baseline.persona) }
function resetCapability() { Object.assign(capability, baseline.capability) }
function resetVisibility() { Object.assign(visibility, baseline.visibility) }

// ============ 生效预览 ============
const previewUser = computed(() => users.value.find((u) => u.id === preview.userId) || null)
const previewRole = computed(() => {
  const name = preview.result?.role || previewUser.value?.role
  return roles.value.find((r) => r.name === name) || null
})
const effectivePrompt = computed(() => {
  if (agent.value?.system_prompt_version != null) return `v${agent.value.system_prompt_version}（实例绑定）`
  const roleVer = previewRole.value?.config?.system_prompt_version
  if (roleVer != null) return `v${roleVer}（角色 ${previewRole.value.name} 绑定）`
  const active = prompts.value.find((p) => p.is_active)
  return active ? `v${active.version}（全局激活）` : '无（未配置提示词）'
})
const effectiveModel = computed(() => {
  if (agent.value?.model_id != null) return `${modelLabel(agent.value.model_id)}（实例绑定）`
  const active = chatModels.value.find((m) => m.is_active)
  return active ? `${active.display_name || active.name}（全局激活）` : '无（未配置模型）'
})
const toolsText = computed(() => {
  const tools = preview.result?.allowed_tools || []
  if (tools.includes('*')) return '全部（admin 通配）'
  return listText(tools, '无')
})

async function runPreview() {
  if (preview.userId == null) {
    preview.result = null
    return
  }
  preview.loading = true
  try {
    preview.result = await admin.simulatePolicy({
      user_id: preview.userId,
      action: 'retrieve',
      agent_id: agentId,
    })
  } catch {
    preview.result = null
  } finally {
    preview.loading = false
  }
}

// ============ 加载与保存 ============
async function load() {
  loading.value = true
  try {
    const [a, pr, mods, sks, tools, rs, us, sources, status] = await Promise.all([
      admin.getAgent(agentId).catch(() => null),
      admin.listPrompts().catch(() => []),
      admin.listModels('chat').catch(() => []),
      admin.listSkills().catch(() => []),
      admin.listTools().catch(() => []),
      admin.listRoles().catch(() => []),
      admin.listUsers().catch(() => []),
      admin.listSources().catch(() => []),
      admin.imStatus().catch(() => ({ channels: [] })),
    ])
    agent.value = a
    if (!a) return
    prompts.value = pr
    chatModels.value = mods
    allSkills.value = sks
    toolOptions.value = tools
    roles.value = rs
    users.value = (us || []).filter((u) => u.is_active)
    roleOptions.value = ['admin', ...rs.map((r) => r.name)]
    sourceOptions.value = sources || []
    imStatusData.value = status

    Object.assign(basic, {
      display_name: a.display_name || '',
      description: a.description || '',
      avatar_url: a.avatar_url || '',
      channel: a.channel || 'api',
      is_enabled: a.is_enabled,
    })
    persona.system_prompt_version = a.system_prompt_version ?? null
    persona.model_id = a.model_id ?? null
    capability.skills = a.skills ?? null
    capability.knowledge_scope_tags = a.knowledge_scope_tags ?? null
    capability.allowed_tools = a.allowed_tools ?? null
    visibility.visible_roles = a.visible_roles ?? null
    // 脱敏预览：值打码为 ***，不回填输入框（修改需完整填写）；渠道列表经 im_channels 计算
    im.bot_id = ''
    im.secret = ''
    markBaseline()
  } finally {
    loading.value = false
  }
}

async function refreshStatus() {
  imStatusData.value = await admin.imStatus().catch(() => ({ channels: [] }))
}

async function patch(key, payload, okMsg) {
  savingKey.value = key
  try {
    agent.value = await admin.updateAgent(agentId, payload)
    ElMessage.success(okMsg || '已保存')
    markBaseline()
    if (key === 'basic') await refreshStatus()
  } finally {
    savingKey.value = ''
  }
}

function saveBasic() {
  return patch('basic', { ...basicSnap() }, '基础信息已保存')
}

function savePersona() {
  return patch('persona', { ...personaSnap() }, '人格与模型已保存')
}

function saveCapability() {
  return patch('capability', { ...capabilitySnap() }, '能力边界已保存')
}

function saveVisibility() {
  return patch('visibility', { ...visibilitySnap() }, '可见性已保存')
}

async function saveIm() {
  const botId = im.bot_id.trim()
  const secret = im.secret.trim()
  if (!botId && !secret) {
    ElMessage.warning(hasWecom.value ? '未填写任何修改；如需移除请点「清除企微专属 Bot」' : '请填写 bot_id 与 secret')
    return
  }
  if (!botId || !secret) {
    ElMessage.warning('请同时填写 bot_id 与 secret（保存将整体替换企微凭证）')
    return
  }
  // 按渠道合并语义：只替换 wecom 渠道，不影响该实例其他渠道
  await patch('im', { im_config: { wecom: { bot_id: botId, secret } } }, '企微专属凭证已保存，正在自动重连')
  im.bot_id = ''
  im.secret = ''
  // 后端保存后自动重连该槽位；稍候刷新连接状态
  setTimeout(refreshStatus, 1500)
}

async function onClearIm() {
  const ok = await ElMessageBox.confirm(
    '确定清除该实例的企微专属 Bot？清除后其连接立即断开（其他渠道不受影响）。',
    '清除企微专属 Bot',
    { type: 'warning' },
  ).catch(() => null)
  if (!ok) return
  // 按渠道移除：wecom 置空分组即移除该渠道，不动其他渠道
  await patch('im-clear', { im_config: { wecom: {} } }, '企微专属 Bot 已清除')
  im.bot_id = ''
  im.secret = ''
  await refreshStatus()
}

async function onReconnectBot() {
  reconnecting.value = true
  try {
    const r = await admin.reloadIm(agentId)
    ElMessage.success(r.message || '专属连接已重连')
    await refreshStatus()
  } catch {
    // 失败已由拦截器 toast
  } finally {
    reconnecting.value = false
  }
}

function goBack() {
  router.push({ name: 'agents' })
}

function goChat() {
  router.push({ name: 'chat', query: { agent_id: String(agentId) } })
}

onMounted(load)
</script>

<style scoped>
.statusbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.statusbar__id {
  margin-left: auto;
}

/* 左导航 + 右内容：改一项不必在长页面里翻找 */
.detail-layout {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  gap: var(--bc-space-5);
  align-items: start;
}
.sections {
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: sticky;
  top: calc(var(--bc-header-height) + var(--bc-space-4));
}
.sections__item {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  width: 100%;
  padding: var(--bc-space-2) var(--bc-space-3);
  border: none;
  border-radius: var(--bc-radius-md);
  background: transparent;
  color: var(--bc-text-2);
  font: var(--bc-weight-medium) var(--bc-text-sm) / 20px inherit;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--bc-duration-fast) var(--bc-ease-out), color var(--bc-duration-fast) var(--bc-ease-out);
}
.sections__item:hover {
  background: var(--bc-gray-2);
  color: var(--bc-text-1);
}
.sections__item.is-on {
  background: var(--bc-primary-1, var(--bc-primary-2));
  color: var(--bc-primary-8);
}
.sections__label {
  flex: 1;
  min-width: 0;
}
.sections__dot {
  width: 6px;
  height: 6px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-warning);
  flex-shrink: 0;
}
@media (max-width: 900px) {
  .detail-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .sections {
    position: static;
    flex-direction: row;
    flex-wrap: wrap;
  }
}

.section {
  padding: var(--bc-space-5) var(--bc-space-6);
}
.section + .section {
  margin-top: var(--bc-space-4);
}
.section__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
.section__title {
  margin: 0 0 var(--bc-space-2);
  font-size: var(--bc-text-md);
}
.section__desc {
  margin: 0;
  line-height: var(--bc-leading-relaxed);
  max-width: 68ch;
}
.section__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-shrink: 0;
}
.section__conn {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-shrink: 0;
}
.section__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--bc-space-3);
  margin-top: var(--bc-space-2);
}
.preview__picker {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.preview {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-2);
}
.preview__row {
  display: flex;
  align-items: baseline;
  gap: var(--bc-space-3);
  flex-wrap: wrap;
}
.preview__k {
  width: 76px;
  flex-shrink: 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.soon {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  margin-top: var(--bc-space-4);
  padding: var(--bc-space-3) var(--bc-space-4);
  border: 1px dashed var(--bc-border);
  border-radius: var(--bc-radius-md);
}
.soon__hint {
  margin-left: var(--bc-space-2);
}
</style>
