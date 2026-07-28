<template>
  <div class="bc-page">
    <PageHeader
      title="知识库"
      description="每个知识库绑定一个 embedding 模型（决定向量维度）与独立向量集合；库间检索完全隔离。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建知识库</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="3" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!kbs.length"
        icon="Files"
        title="还没有知识库"
        description="先创建知识库，再到库内上传文档；检索按知识库隔离，配合角色知识域标签做预过滤。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个知识库</el-button>
      </EmptyState>

      <el-table v-else :data="kbs" @row-dblclick="goDocuments">
        <el-table-column label="名称" min-width="200">
          <template #default="{ row }">
            <div class="kb-cell">
              <el-link type="primary" class="kb-cell__name" @click="goDocuments(row)">
                {{ row.name }}
              </el-link>
              <span class="bc-muted bc-text-xs">{{ row.description || '—' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="embedding 模型" min-width="170">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ embedName(row.embedding_model_id) || `#${row.embedding_model_id}` }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="维度" width="80" align="center">
          <template #default="{ row }">{{ row.dimension || '—' }}</template>
        </el-table-column>
        <el-table-column label="文档数" width="90" align="center">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.document_count ?? 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="切块数" width="90" align="center">
          <template #default="{ row }">{{ row.chunk_count ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">
            <span class="bc-text-sm bc-muted">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="goDocuments(row)">管理文档</el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建/编辑知识库对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑知识库' : '新建知识库'" width="580px">
      <el-form :model="form" label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：产品知识库" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="这个知识库装什么、给谁用" />
        </el-form-item>
        <el-form-item v-if="!editing" label="embedding 模型">
          <el-select v-model="form.embedding_model_id" placeholder="选择一个 embedding 模型" style="width: 100%">
            <el-option
              v-for="m in embedModels"
              :key="m.id"
              :label="`${m.name}（${m.embed_dimension}维）`"
              :value="m.id"
            />
          </el-select>
          <div class="bc-hint">
            模型决定向量维度，创建后不可更改。没有可选？先在「模型管理」创建并激活一个 embedding 模型。
          </div>
        </el-form-item>
        <template v-if="editing">
          <el-form-item label="embedding 模型">
            <div class="bc-hint" style="margin-top: 0">embedding 模型在创建时锁定，不可更改（维度已固定）。</div>
          </el-form-item>
          <el-collapse class="chunk-collapse">
            <el-collapse-item title="切块参数设置">
              <el-form-item label="切块策略">
                <el-select v-model="form.strategy" style="width: 100%">
                  <el-option label="auto（自动）" value="auto" />
                  <el-option label="recursive（递归）" value="recursive" />
                  <el-option label="heading（标题）" value="heading" />
                </el-select>
              </el-form-item>
              <el-form-item label="单块字符数">
                <el-input-number v-model="form.chunk_size" :min="100" :max="4000" :step="50" />
              </el-form-item>
              <el-form-item label="重叠字符数">
                <el-input-number v-model="form.chunk_overlap" :min="0" :max="500" :step="10" />
              </el-form-item>
              <el-form-item label="启用父子块">
                <el-switch v-model="form.enable_parent_child" />
              </el-form-item>
              <el-form-item label="父块字符数">
                <el-input-number
                  v-model="form.parent_chunk_size"
                  :min="512"
                  :max="8192"
                  :step="128"
                  :disabled="!form.enable_parent_child"
                />
              </el-form-item>
              <el-form-item label="子块字符数">
                <el-input-number
                  v-model="form.child_chunk_size"
                  :min="64"
                  :max="2048"
                  :step="32"
                  :disabled="!form.enable_parent_child"
                />
              </el-form-item>
            </el-collapse-item>
          </el-collapse>
          <div class="bc-hint">这些设置也可在知识库文档管理页面中调整</div>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime } from '../utils/format'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const router = useRouter()
const kbs = ref([])
const embedModels = ref([])
const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
  emptyForm: () => ({
    name: '', description: '', embedding_model_id: null,
    chunk_size: 500, chunk_overlap: 50, strategy: 'auto',
    enable_parent_child: false, parent_chunk_size: 4096, child_chunk_size: 384,
  }),
  toForm: (row) => ({
    name: row.name,
    description: row.description,
    embedding_model_id: row.embedding_model_id,
    chunk_size: row.chunk_size ?? 500,
    chunk_overlap: row.chunk_overlap ?? 50,
    strategy: row.strategy || 'auto',
    enable_parent_child: row.enable_parent_child ?? false,
    parent_chunk_size: row.parent_chunk_size ?? 4096,
    child_chunk_size: row.child_chunk_size ?? 384,
    _id: row.id,
  }),
})

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
        chunk_size: form.chunk_size,
        chunk_overlap: form.chunk_overlap,
        strategy: form.strategy,
        enable_parent_child: form.enable_parent_child,
        parent_chunk_size: form.enable_parent_child ? form.parent_chunk_size : null,
        child_chunk_size: form.enable_parent_child ? form.child_chunk_size : null,
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

onMounted(load)
</script>

<style scoped>
.kb-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.kb-cell__name {
  font-weight: var(--bc-weight-semibold);
}
.chunk-collapse {
  margin-bottom: var(--bc-space-3);
  border-top: none;
}
</style>
