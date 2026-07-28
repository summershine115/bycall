<template>
  <div class="bc-page">
    <PageHeader
      title="用户管理"
      description="组织成员的账号、角色与 IM 身份绑定；角色决定其可见的工具与知识边界。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">邀请用户</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="5" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!users.length"
        icon="User"
        title="还没有用户"
        description="邀请组织成员加入，并为其分配角色与企微身份。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">邀请第一个用户</el-button>
      </EmptyState>

      <el-table v-else :data="users">
        <el-table-column prop="id" label="ID" width="64" />
        <el-table-column label="账号" min-width="140">
          <template #default="{ row }">
            <div class="user-cell">
              <span class="user-cell__avatar" aria-hidden="true">
                {{ (row.display_name || row.name).slice(0, 1).toUpperCase() }}
              </span>
              <div class="user-cell__names">
                <span class="bc-strong">{{ row.display_name || row.name }}</span>
                <span class="bc-muted bc-text-xs">@{{ row.name }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'warning' : 'primary'">{{ row.role }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusBadge :type="row.is_active ? 'ok' : 'err'" :text="row.is_active ? '启用' : '禁用'" />
          </template>
        </el-table-column>
        <el-table-column label="IM 绑定" min-width="180">
          <template #default="{ row }">
            <span v-if="!row.im_bindings || !Object.keys(row.im_bindings).length" class="bc-muted bc-text-sm">
              未绑定
            </span>
            <el-tag
              v-for="(uid, ch) in row.im_bindings"
              :key="ch"
              size="small"
              type="info"
              class="im-tag"
            >
              {{ ch }}: {{ uid }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="row.role !== 'admin'" size="small" type="warning" plain @click="onActAs(row)">
              扮演(dev)
            </el-button>
            <el-button v-if="row.role !== 'admin'" size="small" type="danger" plain @click="onDelete(row)">
              禁用
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建/编辑用户对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '邀请用户'" width="480px">
      <el-form :model="form" label-position="top">
        <el-form-item v-if="!editing" label="账号">
          <el-input v-model="form.name" placeholder="如 alice（登录账号，创建后不可改）" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.display_name" placeholder="真实姓名，用于展示" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
          </el-select>
          <div class="bc-hint">角色决定该用户可用的工具、知识与职级边界</div>
        </el-form-item>
        <el-form-item v-if="editing" label="重置密码">
          <el-input v-model="form.password" placeholder="留空则不修改密码" show-password />
        </el-form-item>
        <el-form-item v-if="editing" label="账号状态">
          <el-switch v-model="form.is_active" active-text="启用" inactive-text="禁用" />
        </el-form-item>
        <el-form-item label="企微 UserID">
          <el-input v-model="form.im_bindings.wecom" placeholder="企业微信 UserID（如 ZhangSan）" />
          <div class="bc-hint">员工在企微里 @机器人 时，系统据此识别其组织身份</div>
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
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const users = ref([])
const roles = ref([])
const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
  emptyForm: () => ({ name: '', display_name: '', role: 'member', password: '', is_active: true, im_bindings: {} }),
  toForm: (row) => ({
    name: row.name,
    display_name: row.display_name,
    role: row.role,
    password: '',
    is_active: row.is_active,
    im_bindings: row.im_bindings ? { ...row.im_bindings } : {},
    _id: row.id,
  }),
})

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


async function onSave() {
  saving.value = true
  try {
    const im = Object.fromEntries(
      Object.entries(form.im_bindings || {}).filter(([, v]) => v && v.trim()),
    )
    if (editing.value) {
      const data = { display_name: form.display_name, role: form.role, is_active: form.is_active, im_bindings: im }
      if (form.password) data.password = form.password
      await admin.updateUser(form._id, data)
    } else {
      await admin.createUser({
        name: form.name,
        display_name: form.display_name,
        role: form.role,
        is_active: form.is_active,
        im_bindings: im,
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
  await ElMessageBox.confirm(`确定禁用用户 ${row.name}？（软删除，保留审计记录）`, '确认', {
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
.user-cell {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
}
.user-cell__avatar {
  width: 30px;
  height: 30px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-primary-2);
  color: var(--bc-primary-8);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-semibold);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.user-cell__names {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.im-tag {
  margin-right: var(--bc-space-1);
}
</style>
