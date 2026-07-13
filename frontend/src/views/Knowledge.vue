<template>
  <div v-loading="loading">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    >
      <template #title>
        先选择一个<strong>知识库</strong>，再管理其文档。文档入库后按 <strong>source 标签</strong>做 ACL
        预过滤——只有角色<strong>知识域标签</strong>包含该 source 的用户才能检索到。
        <el-link type="primary" style="vertical-align: baseline" @click="$router.push({ name: 'knowledge-bases' })">
          去管理知识库
        </el-link>
      </template>
    </el-alert>

    <!-- 知识库选择 -->
    <div class="kb-selector">
      <span class="kb-label">当前知识库：</span>
      <el-select
        v-model="kbId"
        placeholder="选择一个知识库"
        style="width: 320px"
        @change="onKbChange"
      >
        <el-option
          v-for="kb in kbs"
          :key="kb.id"
          :label="`${kb.name}（${kb.dimension}维）`"
          :value="kb.id"
        />
      </el-select>
      <el-tag v-if="currentKb" type="info" style="margin-left: 8px">
        {{ currentKb.document_count }} 文档 / {{ currentKb.chunk_count }} 切块
      </el-tag>
    </div>

    <template v-if="kbId">
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon> 添加文档
        </el-button>
        <el-upload
          :show-file-list="false"
          :before-upload="onFileSelect"
          accept=".txt,.md,.markdown"
        >
          <el-button type="success">
            <el-icon><Upload /></el-icon> 上传文件
          </el-button>
        </el-upload>
        <el-select
          v-model="filterSource"
          placeholder="按知识域过滤"
          clearable
          style="width: 180px"
          @change="loadDocuments"
        >
          <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
        </el-select>
      </div>

      <el-table :data="documents" border>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="title" label="标题" min-width="180" />
        <el-table-column label="知识域" width="120">
          <template #default="{ row }">
            <el-tag type="success">{{ row.source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="切块数" width="90" />
        <el-table-column prop="char_count" label="字符数" width="90" />
        <el-table-column prop="file_type" label="类型" width="80">
          <template #default="{ row }">
            {{ row.file_type || '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              size="small"
              type="warning"
              :loading="reindexing === row.id"
              @click="onReindex(row)"
            >
              重建
            </el-button>
            <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-empty v-else description="请先选择一个知识库" />

    <!-- 添加/编辑文档对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑文档' : '添加文档'" width="680px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="如：销售流程手册" />
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-select
            v-model="form.source"
            placeholder="选择或输入新的知识域标签"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
          >
            <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
          </el-select>
          <div class="muted small">
            对应角色配置里的「知识域标签」；角色 knowledge_scope_tags 包含此值才能检索到
          </div>
        </el-form-item>
        <el-form-item label="正文" v-if="!editing">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="粘贴或输入文档正文（支持多段落，空行分段）"
          />
        </el-form-item>
        <el-form-item label="正文" v-if="editing">
          <div class="muted small">编辑正文请用「重建」功能（改 source 会自动重入库）</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存并入库</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const route = useRoute()
const router = useRouter()

const loading = ref(true)
const saving = ref(false)
const reindexing = ref(null)
const kbs = ref([])
const kbId = ref(null)
const documents = ref([])
const sources = ref([])
const filterSource = ref('')
const dialogVisible = ref(false)
const editing = ref(false)
const form = reactive({ title: '', source: '', content: '' })

const currentKb = computed(() => kbs.value.find((k) => k.id === kbId.value))

// 路由 query 变化 → 同步 kbId（从知识库列表点过来时带 kb_id）
watch(
  () => route.query.kb_id,
  (val) => {
    if (val && Number(val) !== kbId.value) {
      kbId.value = Number(val)
      loadDocuments()
    }
  },
)

async function loadKbs() {
  kbs.value = await admin.listKnowledgeBases()
  // 初始：优先用 query 参数，否则选第一个
  const qid = route.query.kb_id ? Number(route.query.kb_id) : null
  if (qid && kbs.value.some((k) => k.id === qid)) {
    kbId.value = qid
  } else if (kbs.value.length > 0 && !kbId.value) {
    kbId.value = kbs.value[0].id
  }
}

async function loadAll() {
  loading.value = true
  try {
    await loadKbs()
    await loadDocuments()
    await loadSources()
  } finally {
    loading.value = false
  }
}

async function loadDocuments() {
  if (!kbId.value) {
    documents.value = []
    return
  }
  documents.value = await admin.listDocuments(kbId.value, filterSource.value || undefined)
}

async function loadSources() {
  sources.value = await admin.listSources()
}

function onKbChange(val) {
  // 切换 KB 时同步路由 query，并重新加载文档
  router.replace({ query: { kb_id: val } })
  filterSource.value = ''
  loadDocuments()
}

function openCreate() {
  editing.value = false
  Object.assign(form, { title: '', source: '', content: '' })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, { title: row.title, source: row.source, content: '' })
  form._id = row.id
  dialogVisible.value = true
}

async function onSave() {
  if (!form.title.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  if (!form.source.trim()) {
    ElMessage.warning('知识域标签不能为空')
    return
  }

  saving.value = true
  try {
    if (editing.value) {
      await admin.updateDocument(kbId.value, form._id, {
        title: form.title,
        source: form.source,
      })
    } else {
      if (!form.content.trim()) {
        ElMessage.warning('正文不能为空')
        saving.value = false
        return
      }
      await admin.createDocument(kbId.value, {
        knowledge_base_id: kbId.value,
        title: form.title,
        source: form.source,
        content: form.content,
      })
    }
    ElMessage.success(editing.value ? '已保存' : '入库成功')
    dialogVisible.value = false
    await loadDocuments()
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(
    `确定删除文档「${row.title}」？将同时清理向量库中的所有切块。`,
    '确认删除',
    { type: 'warning' },
  )
  await admin.deleteDocument(kbId.value, row.id)
  ElMessage.success('已删除')
  await loadDocuments()
}

async function onReindex(row) {
  reindexing.value = row.id
  try {
    await admin.reindexDocument(kbId.value, row.id)
    ElMessage.success('重建成功')
    await loadDocuments()
  } finally {
    reindexing.value = null
  }
}

async function onFileSelect(file) {
  const ext = file.name.split('.').pop()?.toLowerCase()
  if (!['txt', 'md', 'markdown'].includes(ext)) {
    ElMessage.error('仅支持 txt/md/markdown 格式')
    return false
  }
  const text = await file.text()
  editing.value = false
  Object.assign(form, {
    title: file.name.replace(/\.[^.]+$/, ''),
    source: '',
    content: text,
    _fileName: file.name,
    _fileType: ext,
  })
  dialogVisible.value = true
  return false // 阻止自动上传
}

function statusType(s) {
  return { completed: 'success', processing: 'warning', pending: 'info', failed: 'danger' }[s] || 'info'
}

function statusLabel(s) {
  return { pending: '待入库', processing: '入库中', completed: '已完成', failed: '失败' }[s] || s
}

function formatTime(t) {
  if (!t) return ''
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}

onMounted(loadAll)
</script>

<style scoped>
.kb-selector {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.kb-label {
  font-weight: 500;
  color: #606266;
}
.toolbar {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.muted {
  color: #909399;
}
.small {
  font-size: 12px;
}
</style>
