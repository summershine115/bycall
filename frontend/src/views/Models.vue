<template>
  <div class="bc-page">
    <PageHeader
      title="模型管理"
      description="对话 / 向量 / 重排模型是独立可管理资源，可配多个、各选服务商；激活的模型即当前生效配置。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate(activeTab)">添加模型</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="4" />

    <div v-else class="bc-panel models-panel">
      <el-tabs v-model="activeTab" class="models-tabs">
        <el-tab-pane v-for="t in typeTabs" :key="t.name" :name="t.name">
          <template #label>
            {{ t.label }}
            <span class="models-tabs__count">{{ modelsByType(t.name).length }}</span>
            <span v-if="activeOfType(t.name)" class="models-tabs__active" title="已有激活模型" />
          </template>
          <ModelTable
            :model-type="t.name"
            :models="modelsByType(t.name)"
            @add="openCreate(t.name)"
            @edit="openEdit"
            @activate="onActivate"
            @test="onTest"
            @delete="onDelete"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

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
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import ModelTable from '../components/ModelTable.vue'

const loading = ref(true)
const activeTab = ref('chat')
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

const typeTabs = [
  { name: 'chat', label: '对话模型' },
  { name: 'embedding', label: '向量模型' },
  { name: 'rerank', label: '重排模型' },
]

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

function modelsByType(type) {
  return allModels.value.filter((m) => m.model_type === type)
}
function activeOfType(type) {
  return allModels.value.some((m) => m.model_type === type && m.is_active)
}

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
      await ElMessageBox.confirm(
        `向量维度从 ${result.old_dim} 变为 ${result.new_dim}，激活后需重建知识库索引（已有数据会重新向量化）。是否继续？`,
        '维度变化确认',
        { confirmButtonText: '确认重建并激活', cancelButtonText: '取消', type: 'warning' },
      )
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
  await ElMessageBox.confirm(msg, model.is_active ? '删除激活模型' : '确认', {
    type: model.is_active ? 'error' : 'warning',
  })
  await admin.deleteModel(model.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<style scoped>
.models-panel {
  padding: 0 var(--bc-space-5) var(--bc-space-5);
}
.models-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--bc-space-4);
}
.models-tabs__count {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
  margin-left: var(--bc-space-1);
}
.models-tabs__active {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-success);
  margin-left: var(--bc-space-1);
  vertical-align: 2px;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--bc-space-4);
}
</style>
