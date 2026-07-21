<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 新建技能
      </el-button>
      <span class="muted">Skill = admin 管控的领域知识文档（Markdown），按角色绑定注入 system prompt</span>
    </div>

    <el-table :data="skills" border>
      <el-table-column prop="name" label="技能名" width="180">
        <template #default="{ row }">
          <strong>{{ row.name }}</strong>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="160" />
      <el-table-column label="内容" min-width="300">
        <template #default="{ row }">
          <div class="content-cell">{{ row.content }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.is_enabled" @change="(val) => onToggle(row, val)" />
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="180">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑技能' : '新建技能'" width="640px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="技能名" v-if="!editing">
          <el-input
            v-model="form.name"
            placeholder="小写字母/数字/连字符，如 sales-playbook"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明这个技能的用途" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="领域知识 Markdown，会注入 system prompt"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_enabled" />
          <span class="muted small" style="margin-left: 8px">停用后该技能不会被注入</span>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const loading = ref(true)
const skills = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ name: '', description: '', content: '', is_enabled: true })

function formatTime(t) {
  if (!t) return ''
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  try {
    skills.value = await admin.listSkills()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = false
  Object.assign(form, { name: '', description: '', content: '', is_enabled: true })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, {
    name: row.name,
    description: row.description,
    content: row.content,
    is_enabled: row.is_enabled,
  })
  form._original_name = row.name
  dialogVisible.value = true
}

async function onSave() {
  if (!form.name.trim()) {
    ElMessage.warning('技能名不能为空')
    return
  }
  if (!form.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await admin.updateSkill(form._original_name, {
        description: form.description,
        content: form.content,
        is_enabled: form.is_enabled,
      })
    } else {
      await admin.createSkill({
        name: form.name,
        description: form.description,
        content: form.content,
      })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onToggle(row, val) {
  try {
    await admin.updateSkill(row.name, { is_enabled: val })
    ElMessage.success(val ? '已启用' : '已停用')
    await load()
  } catch {
    // 拦截器已统一 toast；失败也要重载，把开关状态同步回真实值
    await load()
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确定删除技能「${row.name}」？`, '确认', { type: 'warning' })
  await admin.deleteSkill(row.name)
  ElMessage.success('已删除')
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
  color: #909399;
}
.small {
  font-size: 12px;
}
.content-cell {
  white-space: pre-wrap;
  max-height: 80px;
  overflow: hidden;
  color: #606266;
  font-size: 13px;
}
</style>
