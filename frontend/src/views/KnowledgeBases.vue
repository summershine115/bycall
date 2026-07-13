<template>
  <div v-loading="loading">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    >
      <template #title>
        每个知识库绑定一个 <strong>embedding 模型</strong>（决定向量维度）和一个独立的向量集合。
        不同知识库可使用不同嵌入模型。文档入库、检索都按知识库隔离。先在这里建库，再到库内管理文档。
      </template>
    </el-alert>

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 新建知识库
      </el-button>
    </div>

    <el-table :data="kbs" border @row-dblclick="goDocuments">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" min-width="180">
        <template #default="{ row }">
          <el-link type="primary" @click="goDocuments(row)">{{ row.name }}</el-link>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="160" />
      <el-table-column label="embedding 模型" width="140">
        <template #default="{ row }">
          <el-tag type="info">#{{ row.embedding_model_id }}</el-tag>
          {{ embedName(row.embedding_model_id) }}
        </template>
      </el-table-column>
      <el-table-column prop="dimension" label="维度" width="90" />
      <el-table-column prop="document_count" label="文档数" width="90" />
      <el-table-column prop="chunk_count" label="切块数" width="90" />
      <el-table-column prop="created_at" label="创建时间" width="170">
        <template #default="{ row }">
          {{ formatTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="primary" @click="goDocuments(row)">文档</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建/编辑知识库对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑知识库' : '新建知识库'" width="560px">
      <el-form :model="form" label-width="130px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：产品知识库" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="embedding 模型" v-if="!editing">
          <el-select
            v-model="form.embedding_model_id"
            placeholder="选择一个 embedding 模型"
            style="width: 100%"
          >
            <el-option
              v-for="m in embedModels"
              :key="m.id"
              :label="`${m.name}（${m.embed_dimension}维）`"
              :value="m.id"
            />
          </el-select>
          <div class="muted small">
            模型决定向量维度，创建后不可更改。没有可选？先在「模型管理」创建并激活一个 embedding 模型。
          </div>
        </el-form-item>
        <el-form-item label="embedding 模型" v-if="editing">
          <div class="muted small">embedding 模型在创建时锁定，不可更改（维度已固定）。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const router = useRouter()
const loading = ref(true)
const saving = ref(false)
const kbs = ref([])
const embedModels = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const form = reactive({ name: '', description: '', embedding_model_id: null })

async function load() {
  loading.value = true
  try {
    const [kbList, models] = await Promise.all([
      admin.listKnowledgeBases(),
      admin.listModels('embedding'),
    ])
    kbs.value = kbList
    embedModels.value = models
  } finally {
    loading.value = false
  }
}

function embedName(id) {
  const m = embedModels.value.find((x) => x.id === id)
  return m ? m.name : ''
}

function openCreate() {
  editing.value = false
  Object.assign(form, { name: '', description: '', embedding_model_id: null })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, { name: row.name, description: row.description, embedding_model_id: row.embedding_model_id })
  form._id = row.id
  dialogVisible.value = true
}

async function onSave() {
  if (!form.name.trim()) {
    ElMessage.warning('名称不能为空')
    return
  }
  if (!editing.value && !form.embedding_model_id) {
    ElMessage.warning('请选择 embedding 模型')
    return
  }

  saving.value = true
  try {
    if (editing.value) {
      await admin.updateKnowledgeBase(form._id, {
        name: form.name,
        description: form.description,
      })
    } else {
      await admin.createKnowledgeBase({
        name: form.name,
        description: form.description,
        embedding_model_id: form.embedding_model_id,
      })
    }
    ElMessage.success(editing.value ? '已保存' : '创建成功')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(
    `确定删除知识库「${row.name}」？将级联删除其下全部文档和向量集合，不可恢复。`,
    '确认删除',
    { type: 'error' },
  )
  await admin.deleteKnowledgeBase(row.id)
  ElMessage.success('已删除')
  await load()
}

function goDocuments(row) {
  router.push({ name: 'knowledge', query: { kb_id: row.id } })
}

function formatTime(t) {
  if (!t) return ''
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
}
.muted {
  color: #909399;
}
.small {
  font-size: 12px;
}
</style>
