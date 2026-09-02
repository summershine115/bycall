<template>
  <div class="bc-page">
    <ContentLoader v-if="loading" :rows="5" />

    <template v-else>
      <!-- ============ 连接总览：全局 + 各数字员工专属 Bot ============ -->
      <div class="bc-panel">
        <div class="conn-head">
          <div>
            <h3>连接总览</h3>
            <p class="bc-muted bc-text-sm">
              IM Bot 连接的集中视图：全局默认 Bot 兜底 + 各数字员工专属 Bot，保存即热重连、无需重启服务。
              每个数字员工可绑定一个专属企微 Bot（在数字员工详情「IM 渠道」区配置）；
              员工私聊哪个 Bot，就进入哪个数字员工的独立会话。
            </p>
          </div>
          <el-button :icon="Refresh" circle aria-label="刷新" @click="loadAll" />
        </div>
        <el-table :data="connections" style="width: 100%">
          <el-table-column label="作用域" width="130">
            <template #default="{ row }">
              <el-tag v-if="row.scope === 'global'" effect="plain">全局默认</el-tag>
              <el-tag v-else type="primary" effect="plain">专属 Bot</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="归属" min-width="160">
            <template #default="{ row }">
              <template v-if="row.scope === 'global'">
                默认数字员工（未绑定专属 Bot 的入口）
              </template>
              <template v-else>
                <span>{{ agentName(row.agent_id) }}</span>
                <span class="mono bc-muted bc-text-xs"> #{{ row.agent_id }}</span>
              </template>
            </template>
          </el-table-column>
          <el-table-column label="bot_id" min-width="140">
            <template #default="{ row }">
              <span class="mono">{{ row.bot_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="连接状态" width="110">
            <template #default="{ row }">
              <StatusBadge :type="row.is_connected ? 'ok' : 'err'" :text="row.is_connected ? '已连接' : '未连接'" />
            </template>
          </el-table-column>
          <el-table-column label="重连" width="80">
            <template #default="{ row }">{{ row.reconnect_attempts }}</template>
          </el-table-column>
          <el-table-column label="最近错误" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.last_error || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :loading="reloadingKey === row.key" @click="onReconnect(row)">
                重连
              </el-button>
              <el-button v-if="row.scope === 'agent'" link type="primary" @click="goAgent(row.agent_id)">
                配置凭证
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <EmptyState
          v-if="!connections.length"
          icon="Connection"
          title="暂无运行中的 IM 连接"
          :description="imEnable ? '已启用但尚无可用凭证，请在下方配置全局默认 Bot。' : 'IM 未启用：打开下方开关并填入企微凭证后保存。'"
        />
      </div>

      <!-- ============ 全局默认渠道凭证（自运行时设置迁入，唯一编辑入口） ============ -->
      <div class="bc-panel im-card">
        <div class="im-card__head">
          <div>
            <h3>全局默认渠道 · 企业微信</h3>
            <p class="bc-muted bc-text-sm">
              未绑定专属 Bot 的企微入口走这套凭证，进入默认数字员工的会话；
              数字员工的专属凭证在其详情页「IM 渠道」区维护。
            </p>
          </div>
          <div class="im-card__status">
            <StatusBadge :type="imEnable ? 'ok' : 'idle'" :text="imEnable ? '已启用' : '未启用'" />
            <el-switch v-model="imEnable" active-text="启用" inactive-text="停用" />
          </div>
        </div>
        <el-form label-position="top" class="im-card__form">
          <div class="im-row">
            <el-form-item label="企微 bot_id">
              <el-input v-model="form['im.wecom_bot_id']" placeholder="企业微信后台「智能机器人」获取" />
            </el-form-item>
            <el-form-item label="企微 bot_secret">
              <el-input
                v-model="form['im.wecom_bot_secret']"
                :placeholder="masks['im.wecom_bot_secret'] || '企微后台获取的 secret'"
                show-password
              />
              <div v-if="masks['im.wecom_bot_secret']" class="bc-hint">
                当前已设置：{{ masks['im.wecom_bot_secret'] }} —— 不修改则留空保存
              </div>
            </el-form-item>
          </div>
        </el-form>
        <div class="im-card__foot">
          <el-button :loading="reloading" :icon="Refresh" @click="onReconnect()">立即重连</el-button>
          <el-button type="primary" :loading="saving" @click="onSave">保存设置</el-button>
        </div>
      </div>

      <!-- ============ 飞书（占位，未实现网关） ============ -->
      <div class="bc-panel feishu-card">
        <div class="im-card__head">
          <div>
            <h3>飞书</h3>
            <p class="bc-muted bc-text-sm">渠道抽象已预留（FeishuChannel），网关实现排期中。</p>
          </div>
          <el-tag type="info" effect="plain">规划中</el-tag>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const router = useRouter()
const loading = ref(true)
const saving = ref(false)
const reloading = ref(false)
const reloadingKey = ref('')
const form = reactive({})
const masks = reactive({})
const imEnable = ref(false)
const connections = ref([])
const agents = ref([])
// 影响长连接的 IM 字段：任一变化才需要重连（避免未改凭证也每次弹重连）。
const IM_KEYS = ['im.enable', 'im.wecom_bot_id', 'im.wecom_bot_secret']
const imSnapshot = reactive({})

function agentName(agentId) {
  const a = agents.value.find((x) => x.id === agentId)
  return a ? (a.display_name || a.name) : `数字员工 #${agentId}`
}

function goAgent(agentId) {
  router.push({ name: 'agent-detail', params: { id: String(agentId) } })
}

async function loadAll() {
  loading.value = true
  try {
    const [settings, statusRes, agentList] = await Promise.all([
      admin.listSettings(),
      admin.imStatus().catch(() => ({ channels: [] })),
      admin.listAgents().catch(() => []),
    ])
    Object.keys(form).forEach((k) => delete form[k])
    Object.keys(masks).forEach((k) => delete masks[k])
    for (const s of settings) {
      if (s.is_secret) {
        form[s.key] = ''
        masks[s.key] = s.is_set ? s.value : ''
      } else {
        form[s.key] = s.value
        masks[s.key] = ''
      }
    }
    imEnable.value = form['im.enable'] === 'true'
    IM_KEYS.forEach((k) => { imSnapshot[k] = form[k] ?? '' })
    connections.value = statusRes.channels || []
    agents.value = agentList
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    form['im.enable'] = imEnable.value ? 'true' : 'false'
    await admin.updateSettings(form)
    // 仅当 IM 连接字段真变化（对比加载快照）才自动重连，不再每次弹窗询问。
    const imChanged = IM_KEYS.some((k) => (form[k] ?? '') !== (imSnapshot[k] ?? ''))
    if (imChanged) {
      const r = await admin.reloadIm()
      if (r.ok) {
        ElMessage.success(r.message || 'IM 凭证已更新，已自动重连')
      } else {
        ElMessage.warning(r.message || 'IM 重连未成功，请检查凭证或点「立即重连」')
      }
    } else {
      ElMessage.success('已保存')
    }
    await loadAll()
  } finally {
    saving.value = false
  }
}

async function onReconnect(row = null) {
  if (row) {
    // 专属 Bot 槽位：带 agent_id 只重连那一条连接。
    reloadingKey.value = row.key
    try {
      const r = await admin.reloadIm(row.agent_id)
      ElMessage.success(r.message || '专属 Bot 已重连')
      await loadAll()
    } catch {
      // 失败已由拦截器 toast
    } finally {
      reloadingKey.value = ''
    }
    return
  }
  reloading.value = true
  try {
    const r = await admin.reloadIm()
    ElMessage.success(r.message || 'IM 渠道已重连')
    await loadAll()
  } catch {
    // 失败已由拦截器 toast
  } finally {
    reloading.value = false
  }
}

onMounted(() => loadAll().catch(() => {}))  // 失败已由拦截器 toast
</script>

<style scoped>
.conn-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--bc-space-4);
  padding: var(--bc-space-5) var(--bc-space-6) var(--bc-space-4);
}
.conn-head h3 {
  margin: 0;
}
.conn-head p {
  margin: var(--bc-space-1) 0 0;
  line-height: var(--bc-leading-relaxed);
}
.im-card {
  margin-top: var(--bc-space-4);
  padding: var(--bc-space-5) var(--bc-space-6);
}
.im-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-4);
  padding-bottom: var(--bc-space-4);
  border-bottom: 1px solid var(--bc-border);
}
.im-card__head h3 {
  margin: 0;
}
.im-card__head p {
  margin: var(--bc-space-1) 0 0;
  line-height: var(--bc-leading-relaxed);
}
.im-card__status {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-shrink: 0;
}
.im-card__form {
  margin-top: var(--bc-space-4);
}
.im-card__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--bc-space-3);
  margin-top: var(--bc-space-2);
}
.im-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 var(--bc-space-5);
  align-items: start;
}
.feishu-card {
  padding: var(--bc-space-4) var(--bc-space-6);
}
@media (max-width: 960px) {
  .im-row {
    grid-template-columns: 1fr;
  }
  .im-card__head {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
