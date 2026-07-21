<template>
  <div v-loading="loading">
    <el-alert
      title="模型管理"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
      description="对话模型和向量模型是独立的可管理资源，可配置多个、各自选不同服务商。激活的模型即当前使用。切换向量模型若维度变化需重建索引。"
    />

    <el-tabs v-model="activeTab">
      <el-tab-pane label="对话模型 (Chat)" name="chat">
        <ModelList
          model-type="chat"
          :models="modelsByType('chat')"
          :providers="providers"
          @refresh="load"
          @add="openCreate('chat')"
          @edit="openEdit"
          @activate="onActivate"
          @test="onTest"
          @delete="onDelete"
        />
      </el-tab-pane>
      <el-tab-pane label="向量模型 (Embedding)" name="embedding">
        <ModelList
          model-type="embedding"
          :models="modelsByType('embedding')"
          :providers="providers"
          @refresh="load"
          @add="openCreate('embedding')"
          @edit="openEdit"
          @activate="onActivate"
          @test="onTest"
          @delete="onDelete"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 新建/编辑模型对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑模型' : '添加模型'" width="580px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="服务商">
          <el-select
            v-model="form.provider"
            filterable
            allow-create
            placeholder="选择服务商"
            style="width: 100%"
            @change="onProviderChange"
          >
            <el-option
              v-for="p in providerOptions"
              :key="p.value"
              :label="p.label"
              :value="p.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="模型名称">
          <el-input
            v-model="form.name"
            :placeholder="sampleModelHint"
            list="sample-models"
          />
          <datalist id="sample-models">
            <option v-for="m in sampleModels" :key="m" :value="m" />
          </datalist>
        </el-form-item>
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
          <div class="hint" v-if="editing && hasKey">
            当前已设密钥 {{ apiMaskHint }}，不改则留空
          </div>
        </el-form-item>
        <el-form-item label="向量维度" v-if="form.model_type === 'embedding'">
          <el-input-number v-model="form.embed_dimension" :min="1" :max="8192" />
          <div class="hint">可先随意填，保存后点"测试连接"自动探测真实维度</div>
        </el-form-item>
        <el-form-item label="创建后激活" v-if="!editing">
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
import { ref, reactive, computed, h, onMounted } from 'vue'
import { ElMessage, ElMessageBox, ElTag, ElButton, ElTable, ElTableColumn } from 'element-plus'
import { admin } from '../api'

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
  // 表单内测试：先临时构造（用表单值），若编辑中且 key 留空则用已存 key
  testing.value = true
  try {
    // 简化：保存后再测。未保存的无法测（后端 test 需要 model id）
    if (!editing.value) {
      ElMessage.info('请先保存模型，再点列表里的"测试连接"')
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
        ElMessage.success(`连接成功，检测到维度 ${result.embed_dimension}（${result.latency_ms}ms，已自动填入）`)
        await load()
        // 若正在编辑该模型，同步表单里的维度字段
        if (editing.value && form.id === id) {
          form.embed_dimension = result.embed_dimension
        }
      } else {
        ElMessage.success(`连接成功（${result.latency_ms}ms）`)
      }
    } else {
      ElMessage.error(`连接失败：${result.error}`)
    }
  } catch (e) {
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
  if (model.is_active) {
    ElMessage.warning('不能删除当前激活的模型，请先激活其他模型')
    return
  }
  await ElMessageBox.confirm(`确定删除模型「${model.display_name || model.name}」？`, '确认', {
    type: 'warning',
  })
  await admin.deleteModel(model.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)

// 内联子组件：模型列表（避免单独文件）
const ModelList = {
  props: ['modelType', 'models', 'providers'],
  emits: ['refresh', 'add', 'edit', 'activate', 'test', 'delete'],
  setup(props, { emit }) {
    return () =>
      h('div', [
        h(
          'div',
          { style: 'margin-bottom: 12px' },
          [
            h(
              ElButton,
              { type: 'primary', onClick: () => emit('add') },
              () => '添加模型',
            ),
            h(
              'span',
              { style: 'margin-left: 12px; color: #909399; font-size: 13px' },
              `共 ${props.models.length} 个模型`,
            ),
          ],
        ),
        h(
          ElTable,
          { data: props.models, border: true },
          () => [
            h(ElTableColumn, { prop: 'display_name', label: '名称', minWidth: 140 }),
            h(ElTableColumn, { prop: 'name', label: '模型标识', minWidth: 160 }),
            h(ElTableColumn, { prop: 'provider', label: '服务商', width: 120 }),
            h(ElTableColumn, {
              prop: 'base_url',
              label: 'API 地址',
              minWidth: 220,
              formatter: (row) => row.base_url || '(空)',
            }),
            props.modelType === 'embedding'
              ? h(ElTableColumn, {
                  prop: 'embed_dimension',
                  label: '维度',
                  width: 80,
                })
              : null,
            h(ElTableColumn, { label: '状态', width: 90 }, {
              default: ({ row }) =>
                h(ElTag, { type: row.is_active ? 'success' : 'info' }, () =>
                  row.is_active ? '激活中' : '未激活',
                ),
            }),
            h(
              ElTableColumn,
              { label: '操作', width: 280, fixed: 'right' },
              {
                default: ({ row }) => [
                  h(
                    ElButton,
                    {
                      size: 'small',
                      type: row.is_active ? 'info' : 'success',
                      disabled: row.is_active,
                      onClick: () => emit('activate', row),
                    },
                    () => (row.is_active ? '已激活' : '激活'),
                  ),
                  h(
                    ElButton,
                    { size: 'small', onClick: () => emit('test', row) },
                    () => '测试',
                  ),
                  h(
                    ElButton,
                    { size: 'small', onClick: () => emit('edit', row) },
                    () => '编辑',
                  ),
                  h(
                    ElButton,
                    { size: 'small', type: 'danger', onClick: () => emit('delete', row) },
                    () => '删除',
                  ),
                ],
              },
            ),
          ],
        ),
      ])
  },
}
</script>

<style scoped>
.hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
