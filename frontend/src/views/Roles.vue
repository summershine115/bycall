<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 新建角色
      </el-button>
      <span class="muted">角色配置：可用工具白名单 / 知识域标签 / 职级（ABAC）</span>
    </div>

    <el-table :data="roles" border>
      <el-table-column prop="name" label="角色名" width="140">
        <template #default="{ row }">
          <strong>{{ row.name }}</strong>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="160" />
      <el-table-column label="可用工具" min-width="160">
        <template #default="{ row }">
          <el-tag v-for="t in row.config.allowed_tools" :key="t" size="small" style="margin-right: 4px">{{ t }}</el-tag>
          <span v-if="!row.config.allowed_tools?.length" class="muted">无</span>
        </template>
      </el-table-column>
      <el-table-column label="知识域" min-width="160">
        <template #default="{ row }">
          <el-tag v-for="t in row.config.knowledge_scope_tags" :key="t" size="small" type="success" style="margin-right: 4px">{{ t }}</el-tag>
          <span v-if="!row.config.knowledge_scope_tags?.length" class="muted">无（默认隔离）</span>
        </template>
      </el-table-column>
      <el-table-column label="职级" width="80">
        <template #default="{ row }">{{ row.config.level }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑角色' : '新建角色'" width="540px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="角色名" v-if="!editing">
          <el-input v-model="form.name" placeholder="如 sales / hr（不能是 admin）" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item label="可用工具">
          <el-input
            v-model="toolsInput"
            placeholder="逗号分隔，如 kb_search,send_email"
          />
          <div class="muted small">LLM 的工具列表 = 这个集合；不在此列的工具 LLM 看不到</div>
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-input
            v-model="scopeInput"
            placeholder="逗号分隔，如 public,sales,hr"
          />
          <div class="muted small">RAG 预过滤：只召回这些标签的知识；空 = 无可见知识（默认隔离）</div>
        </el-form-item>
        <el-form-item label="职级 level">
          <el-input-number v-model="form.level" :min="0" :max="999" />
          <div class="muted small">ABAC 用：机密资源按 resource_level ≤ user_level 放行</div>
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
import { ref, reactive, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const loading = ref(true)
const roles = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ name: '', description: '', level: 0 })
const toolsInput = ref('')
const scopeInput = ref('')

async function load() {
  loading.value = true
  try {
    roles.value = await admin.listRoles()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = false
  Object.assign(form, { name: '', description: '', level: 0 })
  toolsInput.value = ''
  scopeInput.value = ''
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, {
    name: row.name,
    description: row.description,
    level: row.config.level,
  })
  form._original_name = row.name
  toolsInput.value = (row.config.allowed_tools || []).join(', ')
  scopeInput.value = (row.config.knowledge_scope_tags || []).join(', ')
  dialogVisible.value = true
}

function parseList(str) {
  return str
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

async function onSave() {
  saving.value = true
  const config = {
    allowed_tools: parseList(toolsInput.value),
    knowledge_scope_tags: parseList(scopeInput.value),
    level: form.level,
  }
  try {
    if (editing.value) {
      await admin.updateRole(form._original_name, { description: form.description, config })
    } else {
      await admin.createRole({ name: form.name, description: form.description, config })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确定删除角色「${row.name}」？`, '确认', { type: 'warning' })
  await admin.deleteRole(row.name)
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
</style>
