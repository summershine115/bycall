<template>
  <div class="bc-page">
    <PageHeader
      title="提示词版本"
      description="数字员工人格的版本化资产：创建即激活，历史版本可随时回滚。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建版本</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="3" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!prompts.length"
        icon="Document"
        title="还没有提示词版本"
        description="系统提示词定义数字员工的人格与行为边界，用户消息永远只进 user message。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">创建第一个版本</el-button>
      </EmptyState>

      <el-table v-else :data="prompts">
        <el-table-column label="版本" width="90">
          <template #default="{ row }">
            <span class="bc-strong mono">v{{ row.version }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="label" label="标签" width="200">
          <template #default="{ row }">{{ row.label || '—' }}</template>
        </el-table-column>
        <el-table-column label="内容" min-width="320">
          <template #default="{ row }">
            <div class="bc-cell-clamp">{{ row.content }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusBadge v-if="row.is_active" type="ok" text="激活中" />
            <span v-else class="bc-muted bc-text-sm">历史版本</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="onView(row)">查看</el-button>
            <el-button v-if="!row.is_active" size="small" type="success" plain @click="onActivate(row)">
              回滚
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" title="新建提示词版本" width="680px">
      <el-form :model="form" label-position="top">
        <el-form-item label="标签">
          <el-input v-model="form.label" placeholder="如：销售场景专用 / 全公司统一" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="数字员工的系统提示词..."
          />
          <div class="bc-hint">创建后将立即激活为当前版本，旧版本自动转为历史版本</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">创建并激活</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="viewVisible" width="760px">
      <template #header>
        <div class="prompt-viewer__title">
          <span class="bc-strong mono">v{{ viewing.version }}</span>
          <span class="bc-muted">{{ viewing.label || '未命名版本' }}</span>
          <StatusBadge v-if="viewing.is_active" type="ok" text="激活中" />
        </div>
      </template>
      <pre class="prompt-viewer__body">{{ viewing.content }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const prompts = ref([])
const { loading, dialogVisible, saving, form, openCreate } = useCrud({
  emptyForm: () => ({ label: '', content: '' }),
})
const viewVisible = ref(false)
const viewing = ref({})

async function load() {
  loading.value = true
  try {
    prompts.value = await admin.listPrompts()
  } finally {
    loading.value = false
  }
}


async function onSave() {
  if (!form.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  saving.value = true
  try {
    // 版本号 = 当前最大 + 1
    const nextVersion = (prompts.value[0]?.version || 0) + 1
    const created = await admin.createPrompt({
      version: nextVersion,
      label: form.label || `v${nextVersion}`,
      content: form.content,
      is_active: false,
    })
    // 创建后立即激活
    await admin.activatePrompt(created.version)
    ElMessage.success('已创建并激活')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onActivate(row) {
  await ElMessageBox.confirm(`确定回滚到 v${row.version}？当前激活版本将停用`, '确认回滚', {
    type: 'warning',
  })
  await admin.activatePrompt(row.version)
  ElMessage.success('已激活')
  await load()
}

function onView(row) {
  viewing.value = row
  viewVisible.value = true
}

onMounted(load)
</script>

<style scoped>

/* 全文查看器 */
.prompt-viewer__title {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.prompt-viewer__body {
  max-height: 60vh;
  overflow: auto;
  margin: 0;
  padding: var(--bc-space-4);
  background: var(--bc-bg-sunken);
  border-radius: var(--bc-radius-md);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-normal);
  color: var(--bc-text-2);
}
</style>
