<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 邀请用户
      </el-button>
    </div>
    <el-table :data="users" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="账号" width="120" />
      <el-table-column prop="display_name" label="姓名" width="120" />
      <el-table-column prop="role" label="角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'danger' : 'info'">{{ row.role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'danger'">
            {{ row.is_active ? '启用' : '禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="IM 绑定" min-width="160">
        <template #default="{ row }">
          <span v-if="!row.im_bindings || !Object.keys(row.im_bindings).length" class="muted">未绑定</span>
          <el-tag v-for="(uid, ch) in row.im_bindings" :key="ch" size="small" style="margin-right: 4px">
            {{ ch }}: {{ uid }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button
            v-if="row.role !== 'admin'"
            size="small"
            type="warning"
            @click="onActAs(row)"
          >
            扮演(dev)
          </el-button>
          <el-button
            v-if="row.role !== 'admin'"
            size="small"
            type="danger"
            @click="onDelete(row)"
          >
            禁用
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建/编辑用户对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '邀请用户'" width="460px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="账号" v-if="!editing">
          <el-input v-model="form.name" placeholder="如 alice" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="密码" v-if="editing">
          <el-input v-model="form.password" placeholder="留空不改密码" show-password />
        </el-form-item>
        <el-form-item label="状态" v-if="editing">
          <el-switch v-model="form.is_active" />
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
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const loading = ref(true)
const users = ref([])
const roles = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ name: '', display_name: '', role: 'member', password: '', is_active: true })

const roleOptions = computed(() => ['admin', 'member', ...roles.value.map((r) => r.name)])

async function load() {
  loading.value = true
  try {
    const [u, r] = await Promise.all([admin.listUsers(), admin.listRoles()])
    users.value = u
    roles.value = r
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = false
  Object.assign(form, { name: '', display_name: '', role: 'member', password: '', is_active: true })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, {
    name: row.name,
    display_name: row.display_name,
    role: row.role,
    password: '',
    is_active: row.is_active,
  })
  form._id = row.id
  dialogVisible.value = true
}

async function onSave() {
  saving.value = true
  try {
    if (editing.value) {
      const data = { display_name: form.display_name, role: form.role, is_active: form.is_active }
      if (form.password) data.password = form.password
      await admin.updateUser(form._id, data)
    } else {
      await admin.createUser({
        name: form.name,
        display_name: form.display_name,
        role: form.role,
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

async function onDelete(row) {
  await ElMessageBox.confirm(`确定禁用用户 ${row.name}？(软删除，保留审计记录)`, '确认', {
    type: 'warning',
  })
  await admin.deleteUser(row.id)
  ElMessage.success('已禁用')
  await load()
}

async function onActAs(row) {
  await admin.actAs(row.id)
  ElMessage.success(`已切换为扮演「${row.display_name || row.name}」，去「对话测试」验证权限`)
}
onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
}
.muted {
  color: #c0c4cc;
}
</style>
