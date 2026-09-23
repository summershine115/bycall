<!--
  发起发布（T2.4）：发布门 + 灰度 + 全量切换的统一入口。

  两个使用场景共用同一个组件，避免"发布"在控制台出现两套口径：
  - 指定目标（提示词/模型 行内操作）：kind + 目标已确定，只选发布方式；
  - 未指定目标（「发布与回滚」页）：连类型与目标一起选。

  灰度（1–99%）不改 is_active，运行时按用户/会话稳定哈希分流；全量（100）原子切换。
  发布门默认开启：任一失败用例即 409，且 is_active 保持原状（服务端已保证无副作用）。
-->
<template>
  <el-dialog
    v-model="visible"
    title="发起发布"
    width="580px"
    :close-on-click-modal="false"
    @opened="onOpened"
  >
    <el-form label-position="top">
      <el-form-item v-if="!targetId" label="发布类型">
        <el-radio-group v-model="kind" @change="onKindChange">
          <el-radio value="prompt">系统提示词</el-radio>
          <el-radio value="model">chat 模型</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="!targetId" label="发布目标">
        <el-select
          v-model="pickedTarget"
          placeholder="选择要发布的版本/模型"
          style="width: 100%"
          :loading="targetsLoading"
        >
          <el-option v-for="t in targets" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
        <div class="bc-hint">
          {{ kind === 'prompt' ? '当前生效版本不在列表中（已生效无需重复发布）' : '仅 chat 模型走发布门（向量/重排模型用「激活」）' }}
        </div>
      </el-form-item>

      <el-form-item v-else label="发布目标">
        <span class="bc-strong mono">{{ targetLabel }}</span>
      </el-form-item>

      <el-form-item label="发布方式">
        <div class="publish-mode">
          <el-radio-group v-model="mode">
            <el-radio value="canary">灰度</el-radio>
            <el-radio value="full">全量切换</el-radio>
          </el-radio-group>
          <template v-if="mode === 'canary'">
            <el-input-number v-model="percent" :min="1" :max="99" size="small" />
            <span class="bc-muted bc-text-sm">% 流量</span>
          </template>
        </div>
        <div class="bc-hint">
          {{ mode === 'canary'
            ? '灰度期间旧版本保持激活，按用户/会话稳定哈希分流（比例不变则不跳变）；确认后再转全量'
            : '立即原子切换 is_active，全部会话生效' }}
        </div>
      </el-form-item>

      <el-form-item label="运行发布门">
        <div class="publish-mode">
          <el-switch v-model="runGate" />
          <span class="bc-muted bc-text-sm">{{ runGate ? '先跑评测再发布（推荐）' : '跳过评测门（仅限已在别处验证过的同版本）' }}</span>
        </div>
      </el-form-item>

      <el-alert
        v-if="gateError"
        type="error"
        :closable="false"
        show-icon
        class="gate-alert"
      >
        <template #title>发布门未通过：本次发布已被阻断，当前配置未发生任何变化</template>
        <div class="gate-alert__body">
          <div>失败用例 {{ gateError.failed }} 条{{ gateError.securityFailed ? `（其中权限安全 ${gateError.securityFailed} 条）` : '' }}</div>
          <ul class="gate-alert__list">
            <li v-for="(c, i) in gateError.cases" :key="i" class="mono">{{ c }}</li>
          </ul>
        </div>
      </el-alert>

      <el-alert v-else-if="errorText" type="error" :closable="false" show-icon :title="errorText" />
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!resolvedTarget" @click="onSubmit">
        {{ mode === 'canary' ? '开始灰度发布' : '全量发布' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { admin } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 'prompt' | 'model'
  kind: { type: String, default: 'prompt' },
  // 指定目标（行内操作）：非空则不显示类型/目标选择器
  targetId: { type: [Number, String], default: null },
  targetLabel: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'published'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const kind = ref(props.kind)
const pickedTarget = ref(null)
const targets = ref([])
const targetsLoading = ref(false)
const mode = ref('full')
const percent = ref(10)
const runGate = ref(true)
const submitting = ref(false)
const gateError = ref(null)
const errorText = ref('')

// 行内模式下目标由调用方给定；页面模式下由选择器决定
const resolvedTarget = computed(() => props.targetId || pickedTarget.value)
const canaryPercent = computed(() => (mode.value === 'canary' ? percent.value : 100))

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      kind.value = props.kind
      pickedTarget.value = null
      mode.value = 'full'
      percent.value = 10
      runGate.value = true
      gateError.value = null
      errorText.value = ''
      targets.value = []
    }
  },
)

async function onOpened() {
  if (!props.targetId) await loadTargets()
}

async function onKindChange() {
  pickedTarget.value = null
  await loadTargets()
}

async function loadTargets() {
  targetsLoading.value = true
  try {
    if (kind.value === 'prompt') {
      const prompts = await admin.listPrompts()
      // 已生效版本无需重复发布（服务端也会 409）
      targets.value = prompts
        .filter((p) => !p.is_active)
        .map((p) => ({ value: p.version, label: `v${p.version} · ${p.label || '未命名版本'}` }))
    } else {
      const models = await admin.listModels()
      targets.value = models
        .filter((m) => m.model_type === 'chat' && !m.is_active)
        .map((m) => ({ value: m.id, label: `${m.display_name || m.name} (${m.name})` }))
    }
  } finally {
    targetsLoading.value = false
  }
}

function gateErrorFrom(error) {
  const detail = error?.response?.data?.detail
  if (!detail || typeof detail !== 'object' || !detail.gate) return null
  const results = detail.gate?.report?.results || []
  const failed = results.filter((r) => !r.passed)
  return {
    failed: failed.length,
    securityFailed: detail.gate.security_failed ?? 0,
    cases: failed.slice(0, 5).map((r) => `${r.id || r.name || '未命名用例'}: ${r.error || '失败'}`),
  }
}

async function onSubmit() {
  submitting.value = true
  gateError.value = null
  errorText.value = ''
  const payload = { canary_percent: canaryPercent.value, skip_gate: !runGate.value }
  try {
    if (kind.value === 'prompt') {
      await admin.publishPrompt(resolvedTarget.value, payload)
    } else {
      await admin.publishModel(resolvedTarget.value, payload)
    }
    ElMessage.success(
      mode.value === 'canary'
        ? `已开始灰度发布（${percent.value}% 流量）`
        : '已全量发布并切换生效版本',
    )
    visible.value = false
    emit('published')
  } catch (error) {
    const gate = gateErrorFrom(error)
    if (gate) {
      gateError.value = gate
    } else {
      errorText.value = error?.response?.data?.detail
        ? String(error.response.data.detail)
        : '发布失败，请查看提示信息'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.publish-mode {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
}
.gate-alert {
  margin-bottom: var(--bc-space-3);
}
.gate-alert__body {
  margin-top: 4px;
  font-size: var(--bc-text-xs);
  line-height: 1.6;
}
.gate-alert__list {
  margin: 4px 0 0;
  padding-left: 18px;
  max-height: 140px;
  overflow: auto;
}
</style>
