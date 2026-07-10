<template>
  <div v-loading="loading">
    <el-alert
      title="运行时配置"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
      description="LLM 与 IM 凭证在此管理，改动即时生效（下一条消息即用新值），无需重启。Secret 字段脱敏显示，留空保存即清除该设置。"
    />

    <!-- LLM 配置 -->
    <el-card style="margin-bottom: 20px">
      <template #header>
        <div class="card-title"><el-icon><Cpu /></el-icon> LLM 配置（模型提供商）</div>
      </template>
      <el-form label-width="140px">
        <el-form-item label="提供商 provider">
          <el-select v-model="form['llm.provider']" style="width: 200px">
            <el-option label="OpenAI 兼容" value="openai" />
            <el-option label="智谱 GLM" value="glm" />
          </el-select>
          <div class="hint">选择后下方 base_url / model 自动建议对应值</div>
        </el-form-item>
        <el-form-item label="API base_url">
          <el-input v-model="form['llm.base_url']" placeholder="https://api.openai.com/v1" />
        </el-form-item>
        <el-form-item label="API key">
          <el-input
            v-model="form['llm.api_key']"
            :placeholder="masks['llm.api_key'] || 'sk-...（脱敏显示，输入新值即更新）'"
            show-password
          />
          <div class="hint" v-if="masks['llm.api_key']">
            当前: {{ masks['llm.api_key'] }} —— 不改则留空保存
          </div>
        </el-form-item>
        <el-form-item label="对话模型 model">
          <el-input v-model="form['llm.model']" placeholder="gpt-4o-mini / glm-4-flash" />
        </el-form-item>
        <el-form-item label="向量模型 embed">
          <el-input v-model="form['llm.embed_model']" placeholder="text-embedding-3-small" />
        </el-form-item>
      </el-form>
    </el-card>

    <!-- IM 配置 -->
    <el-card style="margin-bottom: 20px">
      <template #header>
        <div class="card-title"><el-icon><ChatDotRound /></el-icon> IM 渠道（企业微信）</div>
      </template>
      <el-form label-width="140px">
        <el-form-item label="启用 IM 渠道">
          <el-switch v-model="imEnable" />
          <div class="hint">开启后启动企微长连接（需重启服务生效）；凭证从下方读取</div>
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
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { admin } from '../api'

const loading = ref(true)
const saving = ref(false)
// form: 用户可编辑的明文值（secret 字段初始为空，只在用户输入时才提交）
const form = reactive({})
// masks: 后端返回的脱敏值，仅用于占位提示
const masks = reactive({})
const imEnable = ref(false)

async function load() {
  loading.value = true
  try {
    const settings = await admin.listSettings()
    // 清空旧的
    Object.keys(form).forEach((k) => delete form[k])
    Object.keys(masks).forEach((k) => delete masks[k])
    for (const s of settings) {
      if (s.is_secret) {
        // secret：form 留空（不改即不改），mask 记下脱敏值显示
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
    // 构造提交：非 secret 直接提交；secret 仅在用户输入了非空值时提交
    // im.enable 从 switch 同步
    form['im.enable'] = imEnable.value ? 'true' : 'false'
    const payload = {}
    for (const [k, v] of Object.entries(form)) {
      // secret 字段：空值=不改（后端会跳过），非空=更新
      payload[k] = v
    }
    const result = await admin.updateSettings(payload)
    ElMessage.success(`已更新 ${result.updated.length} 项${result.skipped.length ? '，跳过 ' + result.skipped.length + ' 项未改' : ''}`)
    await load()
  } finally {
    saving.value = false
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
  margin-top: 8px;
}
</style>
