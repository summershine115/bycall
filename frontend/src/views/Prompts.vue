<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 新建版本
      </el-button>
      <span class="muted">系统提示词是 admin 管理的版本化资产；用户消息永远只进 user message</span>
    </div>

    <el-table :data="prompts" border>
      <el-table-column prop="version" label="版本" width="80">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column prop="label" label="标签" width="200" />
      <el-table-column label="内容" min-width="300">
        <template #default="{ row }">
          <div class="content-cell">{{ row.content }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.is_active" type="success">激活中</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="!row.is_active"
            size="small"
            type="success"
            @click="onActivate(row)"
          >
            激活
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新建提示词版本" width="640px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="标签">
          <el-input v-model="form.label" placeholder="如：销售场景专用 / 全公司统一" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="10"
            placeholder="数字员工的系统提示词..."
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">创建并激活</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const loading = ref(true)
const prompts = ref([])
const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({ label: '', content: '' })

async function load() {
  loading.value = true
  try {
    prompts.value = await admin.listPrompts()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { label: '', content: '' })
  dialogVisible.value = true
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
  await ElMessageBox.confirm(`确定激活 v${row.version}？其他版本将停用`, '确认', { type: 'warning' })
  await admin.activatePrompt(row.version)
  ElMessage.success('已激活')
  await load()
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 16px;
}
.muted {
  color: #c0c4cc;
}
.content-cell {
  white-space: pre-wrap;
  max-height: 80px;
  overflow: hidden;
  color: #606266;
  font-size: 13px;
}
</style>
