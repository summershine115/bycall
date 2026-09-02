<template>
  <div class="bc-page">
    <div class="page-head">
      <p class="bc-muted bc-text-sm">对话 / 向量 / 重排模型是独立可管理资源，可配多个、各选服务商；激活的模型即当前生效配置。</p>
      <el-button type="primary" :icon="Plus" @click="openCreate(activeKind === 'all' ? 'chat' : activeKind)">
        添加模型
      </el-button>
    </div>

    <ContentLoader v-if="loading" :rows="4" />

    <template v-else>
      <!-- ============ KPI 行（设计稿：label + mono 大数 + 真实脚注） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">已接入</div>
          <div class="kpi__value mono">{{ allModels.length }}</div>
          <div class="kpi__meta">{{ providerCount }} 家服务商</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">激活中</div>
          <div class="kpi__value mono">{{ activeCount }}</div>
          <div class="kpi__meta">{{ activeSummary }}</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">向量维度</div>
          <div class="kpi__value mono">{{ activeEmbedDim || '—' }}</div>
          <div class="kpi__meta">{{ activeEmbedName || '未激活向量模型' }}</div>
        </div>
      </div>

      <!-- ============ 模型列表（chips 分类筛选 + 统一表格） ============ -->
      <div class="bc-panel models-panel">
        <div class="models-panel__head">
          <span class="models-panel__title">模型列表</span>
          <span class="models-panel__count mono">{{ filteredModels.length }} 个模型</span>
        </div>
        <div class="filterbar">
          <button
            v-for="c in kindChips"
            :key="c.value"
            type="button"
            class="fchip"
            :class="{ 'is-on': activeKind === c.value }"
            @click="activeKind = c.value"
          >
            {{ c.label }} <span class="n">{{ c.count }}</span>
          </button>
        </div>

        <EmptyState
          v-if="!filteredModels.length"
          icon="Cpu"
          title="该类型下还没有模型"
          description="添加模型后可测试连接、激活使用；激活的模型即当前生效配置。"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate(activeKind === 'all' ? 'chat' : activeKind)">
            添加模型
          </el-button>
        </EmptyState>

        <el-table v-else :data="filteredModels">
          <el-table-column label="模型名称" min-width="200">
            <template #default="{ row }">
              <div class="model-cell">
                <span class="bc-strong mono">{{ row.name }}</span>
                <span class="bc-muted bc-text-xs">{{ row.display_name || '—' }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.model_type === 'chat' ? 'primary' : 'info'">
                {{ kindLabel(row.model_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="provider" label="服务商" width="130">
            <template #default="{ row }">
              <span class="bc-text-sm">{{ row.provider }}</span>
            </template>
          </el-table-column>
          <el-table-column label="API 地址" min-width="220">
            <template #default="{ row }">
              <span class="bc-text-sm bc-muted mono addr">{{ row.base_url || '(空)' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="维度" width="80" align="center">
            <template #default="{ row }">
              <span v-if="row.model_type === 'embedding'" class="mono">{{ row.embed_dimension || '—' }}</span>
              <span v-else class="bc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <StatusBadge v-if="row.is_active" type="ok" text="激活中" />
              <span v-else class="bc-muted bc-text-sm">未激活</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="270" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                :type="row.is_active ? 'info' : 'success'"
                :plain="!row.is_active"
                :disabled="row.is_active"
                @click="onActivate(row)"
              >
                {{ row.is_active ? '已激活' : '激活' }}
              </el-button>
              <el-button size="small" @click="onTest(row)">测试</el-button>
              <el-button size="small" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="models-panel__foot">
          <span>API Key 仅在创建时可查看，此后以掩码存储；切换向量模型需重建索引</span>
        </div>
      </div>
    </template>

    <!-- 新建/编辑模型对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑模型' : '添加模型'" width="600px">
      <el-form :model="form" label-position="top">
        <div class="form-row">
          <el-form-item label="服务商" class="form-row__item">
            <el-select
              v-model="form.provider"
              filterable
              allow-create
              placeholder="选择服务商"
              style="width: 100%"
              @change="onProviderChange"
            >
              <el-option v-for="p in providerOptions" :key="p.value" :label="p.label" :value="p.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="模型名称" class="form-row__item">
            <el-input v-model="form.name" :placeholder="sampleModelHint" list="sample-models" />
            <datalist id="sample-models">
              <option v-for="m in sampleModels" :key="m" :value="m" />
            </datalist>
          </el-form-item>
        </div>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" placeholder="便于识别的名字" />
        </el-form-item>
        <el-form-item label="API 地址">
          <el-input v-model="form.base_url" placeholder="https://..." />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            :placeholder="editing && hasKey ? apiMaskHint : 'sk-...'"
            show-password
          />
          <div v-if="editing && hasKey" class="bc-hint">
            当前已设密钥 {{ apiMaskHint }}，不修改则留空
          </div>
        </el-form-item>
        <el-form-item v-if="form.model_type === 'embedding'" label="向量维度">
          <el-input-number v-model="form.embed_dimension" :min="1" :max="8192" />
          <div class="bc-hint">可先随意填，保存后点「测试连接」自动探测真实维度</div>
        </el-form-item>
        <el-form-item v-if="!editing" label="创建后激活">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button :loading="testing" @click="onTestForm">测试连接</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const loading = ref(true)
const activeKind = ref('all')
const allModels = ref([])
const providers = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const testing = ref(false)
const hasKey = ref(false)
const form = reactive({
  id: null,
  name: '',
  display_name: '',
  model_type: 'chat',
  provider: 'generic',
  base_url: '',
  api_key: '',
  embed_dimension: 1536,
  is_active: false,
})

const KIND_LABELS = { chat: '对话', embedding: '向量', rerank: '重排' }
const kindLabel = (t) => KIND_LABELS[t] || t

const kindChips = computed(() => [
  { label: '全部', value: 'all', count: allModels.value.length },
  ...['chat', 'embedding', 'rerank'].map((t) => ({
    label: `${kindLabel(t)}模型`,
    value: t,
    count: allModels.value.filter((m) => m.model_type === t).length,
  })),
])

const filteredModels = computed(() =>
  activeKind.value === 'all'
    ? allModels.value
    : allModels.value.filter((m) => m.model_type === activeKind.value),
)

const providerCount = computed(
  () => new Set(allModels.value.filter((m) => m.provider).map((m) => m.provider)).size,
)
const activeModels = computed(() => allModels.value.filter((m) => m.is_active))
const activeCount = computed(() => activeModels.value.length)
const activeSummary = computed(() =>
  activeModels.value.length
    ? activeModels.value.map((m) => kindLabel(m.model_type)).join(' × ')
    : '尚无激活模型',
)
const activeEmbed = computed(() => activeModels.value.find((m) => m.model_type === 'embedding'))
const activeEmbedDim = computed(() => activeEmbed.value?.embed_dimension || 0)
const activeEmbedName = computed(() => activeEmbed.value?.name || '')

const providerOptions = computed(() =>
  providers.value.filter((p) => p.model_types.includes(form.model_type)),
)
const sampleModels = computed(() => {
  const p = providers.value.find((x) => x.value === form.provider)
  return p?.sample_models?.[form.model_type] || []
})
const sampleModelHint = computed(() =>
  sampleModels.value.length ? `如 ${sampleModels.value.slice(0, 2).join(' / ')}` : '模型标识',
)
const apiMaskHint = computed(() => {
  const m = allModels.value.find((x) => x.id === form.id)
  return m?.api_key_masked || '(已设)'
})

async function load() {
  loading.value = true
  try {
    const [models, provs] = await Promise.all([admin.listModels(), admin.listModelProviders()])
    allModels.value = models
    providers.value = provs
  } finally {
    loading.value = false
  }
}

function openCreate(modelType) {
  editing.value = false
  Object.assign(form, {
    id: null,
    name: '',
    display_name: '',
    model_type: modelType,
    provider: 'generic',
    base_url: '',
    api_key: '',
    embed_dimension: 1536,
    is_active: false,
  })
  hasKey.value = false
  dialogVisible.value = true
}

function openEdit(model) {
  editing.value = true
  Object.assign(form, {
    id: model.id,
    name: model.name,
    display_name: model.display_name,
    model_type: model.model_type,
    provider: model.provider,
    base_url: model.base_url,
    api_key: '',
    embed_dimension: model.embed_dimension || 1536,
    is_active: model.is_active,
  })
  hasKey.value = model.has_api_key
  dialogVisible.value = true
}

function onProviderChange(provider) {
  // 选服务商后自动填默认 URL（用户仍可改）
  const p = providers.value.find((x) => x.value === provider)
  if (p && p.default_urls?.[form.model_type] && !form.base_url) {
    form.base_url = p.default_urls[form.model_type]
  }
}

async function onSave() {
  if (!form.name.trim()) {
    ElMessage.warning('请填模型名称')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      const data = {
        display_name: form.display_name,
        provider: form.provider,
        base_url: form.base_url,
        embed_dimension: form.embed_dimension,
      }
      if (form.api_key) data.api_key = form.api_key
      await admin.updateModel(form.id, data)
    } else {
      await admin.createModel({
        name: form.name,
        display_name: form.display_name,
        model_type: form.model_type,
        source: 'remote',
        provider: form.provider,
        base_url: form.base_url,
        api_key: form.api_key,
        embed_dimension: form.model_type === 'embedding' ? form.embed_dimension : 0,
        is_active: form.is_active,
      })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onTestForm() {
  // 后端 test 需要已落库的 model id，未保存的表单无法测
  testing.value = true
  try {
    if (!editing.value) {
      ElMessage.info('请先保存模型，再点列表里的「测试」')
    } else {
      await doTest(form.id)
    }
  } finally {
    testing.value = false
  }
}

async function doTest(id) {
  ElMessage.info('正在测试连接...')
  try {
    const result = await admin.testModel(id)
    if (result.ok) {
      if (result.embed_dimension != null) {
        ElMessage.success(
          `连接成功，检测到维度 ${result.embed_dimension}（${result.latency_ms}ms，已自动填入）`,
        )
        await load()
        if (editing.value && form.id === id) {
          form.embed_dimension = result.embed_dimension
        }
      } else {
        ElMessage.success(`连接成功（${result.latency_ms}ms）`)
      }
    } else {
      ElMessage.error(`连接失败：${result.error}`)
    }
  } catch {
    ElMessage.error('测试请求失败')
  }
}

async function onTest(model) {
  await doTest(model.id)
}

async function onActivate(model) {
  try {
    const result = await admin.activateModel(model.id)
    if (result.need_rebuild) {
      // 维度变化 → 确认重建
      const ok = await ElMessageBox.confirm(
        `向量维度从 ${result.old_dim} 变为 ${result.new_dim}，激活后需重建知识库索引（已有数据会重新向量化）。是否继续？`,
        '维度变化确认',
        { confirmButtonText: '确认重建并激活', cancelButtonText: '取消', type: 'warning' },
      ).catch(() => null)
      if (!ok) return  // 用户取消
      await admin.activateModel(model.id, true)
      ElMessage.success('已激活（需重新导入知识库文档以重建索引）')
    } else {
      ElMessage.success('已激活')
    }
    await load()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('激活失败')
  }
}

async function onDelete(model) {
  let msg = `确定删除模型「${model.display_name || model.name}」？`
  if (model.is_active) {
    msg = `模型「${model.display_name || model.name}」当前正在使用中，删除后该类型将无激活模型，确定删除？`
  }
  const ok = await ElMessageBox.confirm(msg, model.is_active ? '删除激活模型' : '确认', {
    type: model.is_active ? 'error' : 'warning',
  }).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.deleteModel(model.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<style scoped>
/* ============ 子页顶部操作条（Hub 子页：说明 + 按钮一行） ============ */
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
/* ============ KPI 行（设计稿语言：label + mono 大数 + 脚注） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
.kpi {
  padding: var(--bc-space-4);
  transition: box-shadow 0.15s var(--bc-ease, ease-out);
}
.kpi:hover {
  box-shadow: var(--bc-shadow-sm);
}
.kpi__label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  letter-spacing: 0.02em;
}
.kpi__value {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-3xl);
  font-weight: var(--bc-weight-semibold);
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.kpi__meta {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}

/* ============ 模型列表面板 ============ */
.models-panel {
  padding: 0 0 var(--bc-space-3);
}
.models-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-4) var(--bc-space-5) 0;
}
.models-panel__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.models-panel__count {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  font-variant-numeric: tabular-nums;
}
.models-panel__foot {
  padding: var(--bc-space-3) var(--bc-space-5) 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.models-panel .empty-state {
  margin: var(--bc-space-2) 0;
}

/* ============ 类型 chips ============ */
.filterbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  padding: var(--bc-space-3) var(--bc-space-5);
}
.fchip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-full);
  background: transparent;
  color: var(--bc-text-2);
  font-family: inherit;
  font-size: var(--bc-text-xs);
  font-weight: 500;
  line-height: 1;
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
}
.fchip .n {
  font-family: var(--bc-font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
  font-variant-numeric: tabular-nums;
  color: var(--bc-text-3);
}
.fchip:hover {
  border-color: var(--bc-border-strong);
  color: var(--bc-text-1);
}
.fchip.is-on {
  background: var(--bc-primary-6);
  border-color: var(--bc-primary-6);
  color: var(--bc-on-primary, #fff);
}
.fchip.is-on .n {
  color: var(--bc-on-primary, #fff);
  opacity: 0.72;
}

.model-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.addr {
  font-size: var(--bc-text-xs);
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--bc-space-4);
}
</style>
