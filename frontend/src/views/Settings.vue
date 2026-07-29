<template>
  <div class="bc-page">
    <PageHeader
      title="运行时设置"
      description="IM 渠道凭证与文档解析引擎配置；保存即时生效，无需重启服务。"
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

      <!-- 解析引擎配置（与模型管理同思路：可选引擎 + 参数，DB 优先于 .env） -->
      <div class="bc-panel parse-panel">
        <div class="parse-panel__head">
          <h3>解析引擎</h3>
          <p class="bc-muted bc-text-sm">
            知识库文档上传时使用的解析引擎。非默认引擎不支持的文件类型自动退回
            builtin；per-KB 的引擎映射在知识库设置里配置。
          </p>
        </div>

        <el-form label-position="top">
          <el-form-item label="默认解析引擎">
            <el-radio-group v-model="form['parse.engine']" class="engine-radios">
              <label
                v-for="eng in engines"
                :key="eng.name"
                class="engine-radio"
                :class="{
                  'is-active': form['parse.engine'] === eng.name,
                  'is-unavailable': !eng.is_available,
                }"
              >
                <el-radio :value="eng.name">
                  <span class="engine-radio__main">
                    <span class="engine-radio__name">{{ eng.name }}</span>
                    <el-tag v-if="eng.is_default" size="small" type="success" effect="plain">
                      当前生效
                    </el-tag>
                    <StatusBadge
                      :type="eng.is_available ? 'ok' : 'warn'"
                      :text="eng.is_available ? '可用' : '不可用'"
                    />
                  </span>
                </el-radio>
                <div class="engine-radio__desc bc-muted bc-text-sm">{{ eng.description }}</div>
                <div
                  v-if="!eng.is_available && eng.reason_unavailable"
                  class="engine-radio__reason bc-text-xs"
                >
                  {{ eng.reason_unavailable }}
                </div>
              </label>
            </el-radio-group>
          </el-form-item>
        </el-form>

        <div class="parse-cols">
          <!-- MarkItDown 参数 -->
          <div class="bc-panel parse-subpanel">
            <h4 class="parse-subpanel__title">MarkItDown 选项</h4>
            <el-form label-position="top">
              <el-form-item label="启用第三方插件">
                <el-switch v-model="markitdownPlugins" active-text="启用" inactive-text="关闭" />
                <div class="bc-hint">默认只使用内置 converter；开启后加载 pip 安装的 markitdown 插件</div>
              </el-form-item>
            </el-form>
          </div>

          <!-- MinerU 参数 -->
          <div class="bc-panel parse-subpanel">
            <h4 class="parse-subpanel__title">MinerU 远程 API</h4>
            <el-form label-position="top">
              <el-form-item label="部署模式">
                <el-select v-model="form['parse.mineru_mode']" style="width: 100%">
                  <el-option label="自建（mineru-api /tasks 协议）" value="selfhost" />
                  <el-option label="云端（mineru.net /api/v4 协议）" value="cloud" />
                </el-select>
                <div class="bc-hint">
                  自建：本地/内网部署的 MinerU 服务；云端：mineru.net 精准解析 API（两套协议不同）
                </div>
              </el-form-item>
              <el-form-item label="服务基地址（base_url）">
                <el-input
                  v-model="form['parse.mineru_base_url']"
                  :placeholder="
                    mineruIsCloud
                      ? '如 https://mineru.net（空 → 默认 mineru.net）'
                      : '如 http://127.0.0.1:8000；空 → 引擎不可用'
                  "
                />
              </el-form-item>
              <el-form-item label="鉴权 token（Bearer）">
                <el-input
                  v-model="form['parse.mineru_api_token']"
                  :placeholder="masks['parse.mineru_api_token'] || (mineruIsCloud ? '云端必填' : '自建多数可留空')"
                  show-password
                />
                <div v-if="masks['parse.mineru_api_token']" class="bc-hint">
                  当前已设置：{{ masks['parse.mineru_api_token'] }} —— 不修改则留空保存
                </div>
              </el-form-item>

              <!-- selfhost 专用参数 -->
              <template v-if="!mineruIsCloud">
                <div class="parse-row">
                  <el-form-item label="解析后端（--backend）">
                    <el-select v-model="form['parse.mineru_backend']" style="width: 100%">
                      <el-option label="hybrid-auto-engine" value="hybrid-auto-engine" />
                      <el-option label="hybrid-http-client" value="hybrid-http-client" />
                      <el-option label="pipeline" value="pipeline" />
                      <el-option label="vlm-auto-engine" value="vlm-auto-engine" />
                      <el-option label="vlm-http-client" value="vlm-http-client" />
                    </el-select>
                  </el-form-item>
                  <el-form-item label="解析方法（--parse_method）">
                    <el-select v-model="form['parse.mineru_parse_method']" style="width: 100%">
                      <el-option label="auto" value="auto" />
                      <el-option label="txt" value="txt" />
                      <el-option label="ocr" value="ocr" />
                    </el-select>
                    <div class="bc-hint">仅 pipeline / hybrid 后端生效</div>
                  </el-form-item>
                </div>
              </template>

              <!-- cloud 专用参数（mineru.net 精准解析） -->
              <template v-if="mineruIsCloud">
                <div class="parse-row">
                  <el-form-item label="模型版本（model_version）">
                    <el-select v-model="form['parse.mineru_model_version']" style="width: 100%">
                      <el-option label="vlm（推荐）" value="vlm" />
                      <el-option label="pipeline" value="pipeline" />
                      <el-option label="MinerU-HTML（HTML 专用）" value="MinerU-HTML" />
                    </el-select>
                  </el-form-item>
                  <el-form-item label="强制 OCR（is_ocr）">
                    <el-switch v-model="mineruIsOcr" active-text="强制" inactive-text="自动" />
                    <div class="bc-hint">默认自动识别扫描件；强制 = 对数字文档也跑 OCR</div>
                  </el-form-item>
                </div>
              </template>

              <div class="parse-row">
                <el-form-item label="OCR 语言组">
                  <el-input v-model="form['parse.mineru_lang']" placeholder="ch / en / korean / japan" />
                </el-form-item>
                <el-form-item label="单任务超时（秒）">
                  <el-input-number
                    :model-value="Number(form['parse.mineru_timeout']) || 600"
                    :min="1"
                    :step="60"
                    controls-position="right"
                    style="width: 100%"
                    @update:model-value="(v) => (form['parse.mineru_timeout'] = String(v))"
                  />
                </el-form-item>
              </div>
              <el-form-item label="轮询间隔（秒）">
                <el-input-number
                  :model-value="Number(form['parse.mineru_poll_interval']) || 2"
                  :min="0.1"
                  :step="0.5"
                  :precision="1"
                  controls-position="right"
                  style="width: 180px"
                  @update:model-value="(v) => (form['parse.mineru_poll_interval'] = String(v))"
                />
              </el-form-item>
            </el-form>
          </div>
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
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
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
const engines = ref([])
const markitdownPlugins = ref(false)
const mineruIsOcr = ref(false)
const mineruIsCloud = computed(() => form['parse.mineru_mode'] === 'cloud')
// 影响长连接的 IM 字段：任一变化才需要重连（避免未改凭证也每次弹重连）。
const IM_KEYS = ['im.enable', 'im.wecom_bot_id', 'im.wecom_bot_secret']
const imSnapshot = reactive({})

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
    markitdownPlugins.value = form['parse.markitdown_enable_plugins'] === 'true'
    // MinerU 模式/OCR 派生状态；旧库无 key 时给默认值（selfhost / 不强制 OCR）。
    if (!form['parse.mineru_mode']) form['parse.mineru_mode'] = 'selfhost'
    if (!form['parse.mineru_model_version']) form['parse.mineru_model_version'] = 'vlm'
    mineruIsOcr.value = form['parse.mineru_is_ocr'] === 'true'
    // 快照 IM 连接字段：secret 未改时 form 里是空串，与下次空串相等 → 判为未变。
    IM_KEYS.forEach((k) => { imSnapshot[k] = form[k] ?? '' })
    engines.value = await admin.listParseEngines()
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    form['im.enable'] = imEnable.value ? 'true' : 'false'
    form['parse.markitdown_enable_plugins'] = markitdownPlugins.value ? 'true' : 'false'
    form['parse.mineru_is_ocr'] = mineruIsOcr.value ? 'true' : 'false'
    const result = await admin.updateSettings(form)
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
/* ---- 解析引擎面板 ---- */
.parse-panel {
  margin-top: var(--bc-space-4);
  padding: var(--bc-space-5) var(--bc-space-6);
}
.parse-panel__head {
  margin-bottom: var(--bc-space-5);
}
.parse-panel__head p {
  margin: var(--bc-space-2) 0 0;
  line-height: var(--bc-leading-relaxed);
}
.engine-radios {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--bc-space-3);
  width: 100%;
}
.engine-radio {
  display: block;
  padding: var(--bc-space-3) var(--bc-space-4);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  cursor: pointer;
  transition:
    border-color var(--bc-duration-fast) var(--bc-ease-out),
    background var(--bc-duration-fast) var(--bc-ease-out);
}
.engine-radio:hover {
  border-color: var(--bc-border-strong);
}
.engine-radio.is-active {
  border-color: var(--bc-primary-6);
  background: var(--bc-primary-1);
}
.engine-radio.is-unavailable {
  opacity: 0.75;
}
.engine-radio__main {
  display: inline-flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.engine-radio__name {
  font-weight: var(--bc-weight-semibold);
  font-family: var(--bc-font-mono);
}
.engine-radio__desc {
  margin-top: var(--bc-space-2);
  padding-left: var(--bc-space-6);
  line-height: var(--bc-leading-normal);
}
.engine-radio__reason {
  margin-top: var(--bc-space-1);
  padding-left: var(--bc-space-6);
  color: var(--bc-warning-text);
}
.parse-cols {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: var(--bc-space-4);
  align-items: start;
}
.parse-subpanel {
  padding: var(--bc-space-4) var(--bc-space-5);
  background: var(--bc-bg-sunken);
}
.parse-subpanel__title {
  margin: 0 0 var(--bc-space-4);
  font-size: var(--bc-text-md);
}
.parse-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--bc-space-4);
}
@media (max-width: 1280px) {
  .engine-radios {
    grid-template-columns: 1fr;
  }
  .parse-cols {
    grid-template-columns: 1fr;
  }
  .parse-row {
    grid-template-columns: 1fr;
  }
}
</style>
