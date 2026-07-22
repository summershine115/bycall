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
          accept=".txt,.md,.markdown,.pdf,.docx,.doc,.html,.htm,.csv,.json,.xml,.yaml,.yml,.py,.js,.ts,.go,.java,.sql"
        >
          <el-button type="success">
            <el-icon><Upload /></el-icon> 上传文件
          </el-button>
        </el-upload>
        <el-button @click="openPreview">
          <el-icon><Grid /></el-icon> 切块预览
        </el-button>
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
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.status === 'failed' && row.error_message"
              :content="row.error_message"
              placement="top"
              effect="dark"
            >
              <el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }} ⚠</el-tag>
            </el-tooltip>
            <el-tag v-else :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
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
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="viewContent(row)">查看正文</el-button>
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

    <!-- 上传文件对话框（后端解析，支持 PDF/Word/HTML 等）-->
    <el-dialog v-model="uploadDialogVisible" title="上传文件入库" width="560px">
      <el-form :model="uploadForm" label-width="100px">
        <el-form-item label="文件">
          <div>
            <el-tag v-if="uploadFile" type="success" closable @close="uploadFile = null">
              {{ uploadFile.name }}（{{ (uploadFile.size / 1024).toFixed(1) }}KB）
            </el-tag>
            <el-upload
              v-else
              :show-file-list="false"
              :before-upload="(f) => { uploadFile = f; return false }"
              accept=".txt,.md,.markdown,.pdf,.docx,.doc,.html,.htm,.csv,.json,.xml,.yaml,.yml,.py,.js,.ts,.go,.java,.sql"
            >
              <el-button type="primary" :icon="Upload">选择文件</el-button>
            </el-upload>
            <div class="muted small">
              支持后端解析：PDF / Word / Markdown / HTML / TXT / 代码等（≤10MB），同步解析入库
            </div>
          </div>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="uploadForm.title" placeholder="默认用文件名" />
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-select
            v-model="uploadForm.source"
            placeholder="选择或输入新的知识域标签"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
          >
            <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" :disabled="!uploadFile" @click="doUpload">
          上传并入库
        </el-button>
      </template>
    </el-dialog>

    <!-- 切块预览对话框（参考 WeKnora chunker/preview，不落库）-->
    <el-dialog v-model="previewVisible" title="切块预览" width="780px">
      <el-form :model="previewForm" label-width="100px">
        <el-form-item label="样例正文">
          <el-input
            v-model="previewForm.text"
            type="textarea"
            :rows="6"
            placeholder="粘贴一段文本，预览入库时的切块效果（不写入知识库）"
          />
        </el-form-item>
        <el-form-item label="单块字符数">
          <el-input-number v-model="previewForm.chunkSize" :min="100" :max="4000" :step="50" />
        </el-form-item>
        <el-form-item label="父子块">
          <el-switch v-model="previewForm.enableParentChild" />
          <span class="muted small" style="margin-left: 8px">child=384 精确匹配 / parent=4096 上下文</span>
        </el-form-item>
      </el-form>
      <div v-if="previewResult" style="margin-top: 8px">
        <div class="muted small" style="margin-bottom: 6px">
          共 {{ previewResult.total }} 块（child {{ previewResult.child_count }} / parent
          {{ previewResult.parent_count }}）
        </div>
        <el-scrollbar max-height="320px">
          <div
            v-for="(c, i) in previewResult.items"
            :key="i"
            class="chunk-item"
          >
            <div class="chunk-meta">
              #{{ c.chunk_index }}
              <el-tag size="small" :type="c.chunk_type === 'parent' ? 'warning' : ''">
                {{ c.chunk_type }}
              </el-tag>
              {{ c.char_count }} 字
            </div>
            <div class="chunk-text">{{ c.text }}</div>
          </div>
        </el-scrollbar>
      </div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
        <el-button type="primary" :loading="previewLoading" @click="runPreview">预览切块</el-button>
      </template>
    </el-dialog>

    <!-- 查看正文对话框 -->
    <el-dialog v-model="contentVisible" :title="`正文：${contentData.title}`" width="760px">
      <div v-loading="contentLoading">
        <div class="muted small" style="margin-bottom: 8px">共 {{ contentData.char_count }} 字</div>
        <el-input
          :model-value="contentData.content"
          type="textarea"
          :rows="18"
          readonly
          resize="none"
        />
      </div>
      <template #footer>
        <el-button @click="contentVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'
import { Upload } from '@element-plus/icons-vue'

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
const form = reactive({ title: '', source: '', content: '' })

// 上传文件（后端解析）
const uploadDialogVisible = ref(false)
const uploadFile = ref(null)
const uploadForm = reactive({ title: '', source: '' })
const uploading = ref(false)

// 切块预览
const previewVisible = ref(false)
const previewForm = reactive({ text: '', chunkSize: 500, enableParentChild: false })
const previewResult = ref(null)
const previewLoading = ref(false)

// 查看正文
const contentVisible = ref(false)
const contentLoading = ref(false)
const contentData = reactive({ title: '', content: '', char_count: 0 })

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

function onFileSelect(file) {
  // 选择文件 → 打开上传对话框收集 source/title，实际上传走后端 /upload（支持 PDF/Word 等）
  uploadFile.value = file
  Object.assign(uploadForm, {
    title: file.name.replace(/\.[^.]+$/, ''),
    source: '',
  })
  uploadDialogVisible.value = true
  return false // 阻止 el-upload 自动上传
}

async function doUpload() {
  if (!uploadForm.source.trim()) {
    ElMessage.warning('知识域标签不能为空')
    return
  }
  uploading.value = true
  try {
    const res = await admin.uploadDocument(
      kbId.value,
      uploadFile.value,
      uploadForm.source.trim(),
      uploadForm.title.trim() || null,
    )
    if (res.ok) {
      ElMessage.success(`入库成功，切块 ${res.chunk_count} 块`)
    } else {
      ElMessage.error('入库失败：' + (res.errors?.join('; ') || '未知错误'))
    }
    uploadDialogVisible.value = false
    uploadFile.value = null
    await loadDocuments()
  } catch (e) {
    // 解析阶段 4xx（非 UTF-8 / 格式不支持 / 文件过大）
    ElMessage.error(e.response?.data?.detail || e.message || '上传失败')
  } finally {
    uploading.value = false
  }
}

function openPreview() {
  Object.assign(previewForm, { text: '', chunkSize: 500, enableParentChild: false })
  previewResult.value = null
  previewVisible.value = true
}

async function runPreview() {
  if (!previewForm.text.trim()) {
    ElMessage.warning('请输入样例正文')
    return
  }
  previewLoading.value = true
  try {
    previewResult.value = await admin.previewChunks(kbId.value, {
      text: previewForm.text,
      title: '预览',
      chunk_size: previewForm.chunkSize,
      enable_parent_child: previewForm.enableParentChild,
    })
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message || '预览失败')
  } finally {
    previewLoading.value = false
  }
}

async function viewContent(row) {
  contentVisible.value = true
  contentLoading.value = true
  Object.assign(contentData, { title: row.title, content: '', char_count: 0 })
  try {
    const data = await admin.getDocumentContent(kbId.value, row.id)
    Object.assign(contentData, data)
  } catch (e) {
    ElMessage.error('读取正文失败')
  } finally {
    contentLoading.value = false
  }
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
.chunk-item {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.chunk-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}
.chunk-text {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
