<template>
  <div v-loading="loading">
    <el-alert
      title="IM 渠道配置"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
      description="企业微信智能机器人凭证在此管理。保存后可热重连（不重启服务）。LLM/向量模型请在「模型管理」页配置。"
    />

    <el-card>
      <template #header>
        <div class="card-title"><el-icon><ChatDotRound /></el-icon> 企业微信</div>
      </template>
      <el-form label-width="140px">
        <el-form-item label="启用 IM 渠道">
          <el-switch v-model="imEnable" />
          <div class="hint">开启后启动企微长连接（需重启服务生效首次）；凭证从下方读取</div>
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
          <div class="hint" v-if="masks['im.wecom_bot_secret']">
            当前: {{ masks['im.wecom_bot_secret'] }} —— 不改则留空保存
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <div class="actions">
      <el-button type="primary" :loading="saving" @click="onSave">保存设置</el-button>
      <el-button @click="load">重置</el-button>
      <el-button type="success" :loading="reloading" @click="onReconnect">立即重连</el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

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
        await ElMessageBox.confirm(
          'IM 凭证已更新，是否立即重连？',
          'IM 渠道',
          { confirmButtonText: '立即重连', cancelButtonText: '稍后', type: 'success' },
        )
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
.card-title {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.actions {
  margin-top: 16px;
}
</style>
