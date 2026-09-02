<template>
  <div class="bc-page">
    <ContentLoader v-if="loading" :rows="8" />

    <template v-else-if="agent">
      <PageHeader :title="agent.display_name || agent.name" :description="agent.description || `数字员工实例 · slug: ${agent.name}`">
        <el-button :icon="ArrowLeft" @click="goBack">返回列表</el-button>
      </PageHeader>

      <!-- 状态条：默认/启用/专属 Bot -->
      <div class="statusbar">
        <el-tag :type="agent.is_enabled ? 'success' : 'info'" effect="plain">
          {{ agent.is_enabled ? '已启用' : '已停用' }}
        </el-tag>
        <el-tag v-if="agent.is_default" type="success">默认实例</el-tag>
        <el-tag v-if="agent.has_im_config" type="success">企微专属 Bot</el-tag>
        <el-tag type="info" effect="plain">主渠道：{{ channelLabel(agent.channel) }}</el-tag>
        <span class="mono bc-muted bc-text-xs statusbar__id">#{{ agent.id }}</span>
      </div>

      <!-- ============ ① 基础信息 ============ -->
      <div class="bc-panel section">
        <h3 class="section__title">基础信息</h3>
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
            <el-col :span="8">
              <el-form-item label="主渠道">
                <el-select v-model="basic.channel" style="width:100%">
                  <el-option label="对话测试 (api)" value="api" />
                  <el-option label="企业微信 (wecom)" value="wecom" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="启用">
                <el-switch v-model="basic.is_enabled" active-text="启用" inactive-text="停用" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="设为默认">
                <el-switch v-model="basic.is_default" />
                <div class="bc-hint">全局唯一默认；无归属会话的兜底入口</div>
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
        <div class="section__foot">
          <el-button type="primary" :loading="savingKey === 'basic'" @click="saveBasic">保存基础信息</el-button>
        </div>
      </div>

      <!-- ============ ② 人格与模型 ============ -->
      <div class="bc-panel section">
        <h3 class="section__title">人格与模型</h3>
        <p class="bc-muted bc-text-sm section__desc">
          留空 = 沿用角色/全局链路（提示词走 角色 &gt; 全局激活；模型走 会话 &gt; 实例 &gt; 灰度 &gt; 全局）。
        </p>
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
        <div class="section__foot">
          <el-button type="primary" :loading="savingKey === 'persona'" @click="savePersona">保存人格与模型</el-button>
        </div>
      </div>

      <!-- ============ ③ 能力边界 ============ -->
      <div class="bc-panel section">
        <h3 class="section__title">能力边界</h3>
        <p class="bc-muted bc-text-sm section__desc">
          角色是天花板：技能与角色取<strong>并集</strong>（实例补充能力）；工具与知识域取<strong>交集</strong>（实例只能收窄）。留空 = 沿用角色。
        </p>
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
        <div class="section__foot">
          <el-button type="primary" :loading="savingKey === 'capability'" @click="saveCapability">保存能力边界</el-button>
        </div>
      </div>

      <!-- ============ ④ 可见性 ============ -->
      <div class="bc-panel section">
        <h3 class="section__title">可见性</h3>
        <el-form label-position="top">
          <el-form-item label="可见角色">
            <el-select v-model="visibility.visible_roles" multiple clearable placeholder="为空=全部角色可见" style="width:100%">
              <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
            </el-select>
            <div class="bc-hint">限制哪些角色可在对话中选用该实例；admin 恒可见</div>
          </el-form-item>
        </el-form>
        <div class="section__foot">
          <el-button type="primary" :loading="savingKey === 'visibility'" @click="saveVisibility">保存可见性</el-button>
        </div>
      </div>

      <!-- ============ ⑤ IM 渠道（专属 Bot，ADR-0012 真实生效） ============ -->
      <div class="bc-panel section">
        <div class="section__head">
          <h3 class="section__title">IM 渠道 · 企微专属 Bot</h3>
          <div v-if="botConn" class="section__conn">
            <StatusBadge :type="botConn.is_connected ? 'ok' : 'err'" :text="botConn.is_connected ? '已连接' : '未连接'" />
            <span v-if="botConn.last_error" class="bc-text-xs bc-muted">{{ botConn.last_error }}</span>
            <el-button size="small" :loading="reconnecting" @click="onReconnectBot">重连</el-button>
          </div>
        </div>
        <p class="bc-muted bc-text-sm section__desc">
          为该实例绑定独立的企微机器人：员工私聊这个 Bot 即进入该数字员工的独立会话。
          bot_id 全局唯一（与其他实例及全局默认 Bot 不得重复）。
        </p>
        <el-form label-position="top">
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="企微 bot_id">
                <el-input v-model="im.bot_id" :placeholder="im.has ? '已配置（***），修改需完整填写' : '企业微信后台「智能机器人」获取'" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="企微 bot_secret">
                <el-input v-model="im.secret" :placeholder="im.has ? '已配置（***）—— 不修改则留空' : '企微后台获取的 secret'" show-password />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="bc-hint">
            保存将整体替换该实例的专属凭证并自动重连；修改时需同时填写 bot_id 与 secret。全局默认 Bot 在数字员工页「渠道接入」标签页维护。
          </div>
        </el-form>
        <div class="section__foot">
          <el-button
            v-if="im.has"
            type="danger"
            plain
            :loading="savingKey === 'im-clear'"
            @click="onClearIm"
          >
            清除专属 Bot
          </el-button>
          <el-button type="primary" :loading="savingKey === 'im'" @click="saveIm">保存并重连</el-button>
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
import { ArrowLeft } from '@element-plus/icons-vue'
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

// 分区表单：各 section 独立保存（后端 PATCH 语义，只传本区字段）
const basic = reactive({ display_name: '', description: '', avatar_url: '', channel: 'api', is_enabled: true, is_default: false })
const persona = reactive({ system_prompt_version: null, model_id: null })
const capability = reactive({ skills: null, knowledge_scope_tags: null, allowed_tools: null })
const visibility = reactive({ visible_roles: null })
const im = reactive({ bot_id: '', secret: '', has: false })

// 选项
const prompts = ref([])
const chatModels = ref([])
const allSkills = ref([])
const toolOptions = ref([])
const roleOptions = ref([])
const sourceOptions = ref([])

const botConn = computed(() =>
  (imStatusData.value.channels || []).find((c) => c.agent_id === agentId) || null,
)

function channelLabel(ch) { return { api: '对话测试', wecom: '企微' }[ch] || ch }

async function load() {
  loading.value = true
  try {
    const [a, pr, mods, sks, tools, roles, sources, status] = await Promise.all([
      admin.getAgent(agentId).catch(() => null),
      admin.listPrompts().catch(() => []),
      admin.listModels('chat').catch(() => []),
      admin.listSkills().catch(() => []),
      admin.listTools().catch(() => []),
      admin.listRoles().catch(() => []),
      admin.listSources().catch(() => []),
      admin.imStatus().catch(() => ({ channels: [] })),
    ])
    agent.value = a
    if (!a) return
    prompts.value = pr
    chatModels.value = mods
    allSkills.value = sks
    toolOptions.value = tools
    roleOptions.value = ['admin', ...roles.map((r) => r.name)]
    sourceOptions.value = sources || []
    imStatusData.value = status

    Object.assign(basic, {
      display_name: a.display_name || '',
      description: a.description || '',
      avatar_url: a.avatar_url || '',
      channel: a.channel || 'api',
      is_enabled: a.is_enabled,
      is_default: a.is_default,
    })
    persona.system_prompt_version = a.system_prompt_version ?? null
    persona.model_id = a.model_id ?? null
    capability.skills = a.skills ?? null
    capability.knowledge_scope_tags = a.knowledge_scope_tags ?? null
    capability.allowed_tools = a.allowed_tools ?? null
    visibility.visible_roles = a.visible_roles ?? null
    // 脱敏预览：值打码为 ***，不回填输入框（修改需完整填写）
    im.has = !!a.has_im_config
    im.bot_id = ''
    im.secret = ''
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
  } finally {
    savingKey.value = ''
  }
}

function saveBasic() {
  return patch('basic', { ...basic }, '基础信息已保存')
}

function savePersona() {
  return patch('persona', { ...persona }, '人格与模型已保存')
}

function saveCapability() {
  return patch('capability', { ...capability }, '能力边界已保存')
}

function saveVisibility() {
  return patch('visibility', { ...visibility }, '可见性已保存')
}

async function saveIm() {
  const botId = im.bot_id.trim()
  const secret = im.secret.trim()
  if (!botId && !secret) {
    ElMessage.warning(im.has ? '未填写任何修改；如需移除请点「清除专属 Bot」' : '请填写 bot_id 与 secret')
    return
  }
  if (!botId || !secret) {
    ElMessage.warning('请同时填写 bot_id 与 secret（保存将整体替换凭证）')
    return
  }
  await patch('im', { im_config: { wecom_bot_id: botId, wecom_secret: secret } }, '专属 Bot 凭证已保存，正在自动重连')
  im.has = true
  im.bot_id = ''
  im.secret = ''
  // 后端保存后自动重连该槽位；稍候刷新连接状态
  setTimeout(refreshStatus, 1500)
}

async function onClearIm() {
  const ok = await ElMessageBox.confirm(
    '确定清除该实例的专属企微 Bot？清除后其连接立即断开，员工消息不再进入该实例。',
    '清除专属 Bot',
    { type: 'warning' },
  ).catch(() => null)
  if (!ok) return
  await patch('im-clear', { im_config: {} }, '专属 Bot 已清除')
  im.has = false
  im.bot_id = ''
  im.secret = ''
  await refreshStatus()
}

async function onReconnectBot() {
  reconnecting.value = true
  try {
    const r = await admin.reloadIm(agentId)
    ElMessage.success(r.message || '专属 Bot 已重连')
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
.section {
  padding: var(--bc-space-5) var(--bc-space-6);
  margin-bottom: var(--bc-space-4);
}
.section__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-4);
}
.section__title {
  margin: 0 0 var(--bc-space-2);
  font-size: var(--bc-text-md);
}
.section__desc {
  margin: 0 0 var(--bc-space-4);
  line-height: var(--bc-leading-relaxed);
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
</style>
