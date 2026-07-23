<template>
  <div class="bc-page">
    <PageHeader
      title="运行时设置"
      description="IM 渠道（企业微信）凭证与连接管理；保存后可热重连，无需重启服务。"
    >
      <el-button :loading="reloading" :icon="Refresh" @click="onReconnect">立即重连</el-button>
      <el-button type="primary" :loading="saving" @click="onSave">保存设置</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="4" />

    <template v-else>
      <div class="settings-grid">
        <!-- 连接状态卡 -->
        <div class="bc-panel status-card">
          <div class="status-card__head">
            <h3>连接状态</h3>
            <StatusBadge :type="imEnable ? 'ok' : 'idle'" :text="imEnable ? '已启用' : '未启用'" />
          </div>
          <p class="bc-muted bc-text-sm status-card__desc">
            企微智能机器人长连接状态。LLM / 向量 / 重排模型请在「模型管理」页配置。
          </p>
        </div>

        <!-- 凭证表单卡 -->
        <div class="bc-panel form-card">
          <h3 class="form-card__title">企业微信凭证</h3>
          <el-form label-position="top">
            <el-form-item label="启用 IM 渠道">
              <el-switch v-model="imEnable" active-text="启用" inactive-text="停用" />
              <div class="bc-hint">开启后建立企微长连接；凭证从下方字段读取</div>
            </el-form-item>
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
          </el-form>
        </div>
      </div>

      <div class="settings-actions">
        <el-button type="primary" :loading="saving" @click="onSave">保存设置</el-button>
        <el-button @click="load">重置</el-button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import StatusBadge from '../components/StatusBadge.vue'

const loading = ref(true)
const saving = ref(false)
const reloading = ref(false)
const form = reactive({})
const masks = reactive({})
const imEnable = ref(false)

async function load() {
  loading.value = true
  try {
    const settings = await admin.listSettings()
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
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    form['im.enable'] = imEnable.value ? 'true' : 'false'
    const result = await admin.updateSettings(form)
    const imChanged = result.updated.some((k) => k.startsWith('im.'))
    if (imChanged) {
      try {
        await ElMessageBox.confirm('IM 凭证已更新，是否立即重连？', 'IM 渠道', {
          confirmButtonText: '立即重连',
          cancelButtonText: '稍后',
          type: 'success',
        })
        const r = await admin.reloadIm()
        ElMessage.success(r.message || 'IM 渠道已重连')
      } catch {
        ElMessage.info('可稍后点「立即重连」')
      }
    } else {
      ElMessage.success('已保存')
    }
    await load()
  } finally {
    saving.value = false
  }
}

async function onReconnect() {
  reloading.value = true
  try {
    const r = await admin.reloadIm()
    ElMessage.success(r.message || 'IM 渠道已重连')
  } finally {
    reloading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: var(--bc-space-4);
  align-items: start;
}
.status-card {
  padding: var(--bc-space-5);
}
.status-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.status-card__desc {
  margin: var(--bc-space-3) 0 0;
  line-height: var(--bc-leading-relaxed);
}
.form-card {
  padding: var(--bc-space-5) var(--bc-space-6);
}
.form-card__title {
  margin: 0 0 var(--bc-space-5);
}
.settings-actions {
  margin-top: var(--bc-space-5);
  display: flex;
  gap: var(--bc-space-3);
}
@media (max-width: 1280px) {
  .settings-grid {
    grid-template-columns: 1fr;
  }
}
</style>
