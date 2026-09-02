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

      <template v-else>
        <!-- ============ 筛选栏（设计稿：搜索 + 角色 chips 计数，即时生效） ============ -->
        <div class="filterbar">
          <el-input
            v-model="keyword"
            class="filterbar__search"
            placeholder="按账号或姓名搜索"
            clearable
            :prefix-icon="Search"
          />
          <span class="filterbar__spacer"></span>
          <button
            v-for="opt in roleChips"
            :key="opt.value"
            type="button"
            :class="['fchip', { 'is-on': roleFilter === opt.value }]"
            @click="roleFilter = opt.value"
          >
            {{ opt.label }} <span class="n">{{ opt.count }}</span>
          </button>
        </div>

        <div class="table-scroll">
          <el-table v-if="filtered.length" :data="filtered">
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
          <EmptyState
            v-if="!filtered.length"
            icon="Search"
            title="没有匹配的用户"
            description="换个关键词，或清除角色筛选。"
          >
            <el-button @click="keyword = ''; roleFilter = ''">清除筛选</el-button>
          </EmptyState>
        </div>

        <div class="table-foot">
          <span>角色决定可见的工具与知识边界 · 禁用为软删除，保留审计记录</span>
          <span class="mono table-foot__num">共 {{ filtered.length }} 人</span>
        </div>
      </template>
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
import { Plus, Search } from '@element-plus/icons-vue'
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

// 内置角色 + 自建角色去重（seed 必建 member，否则下拉出现重复项/重复 key）
const roleOptions = computed(() => [...new Set(['admin', 'member', ...roles.value.map((r) => r.name)])])

/* ---------------- 组合筛选（关键词 × 角色 chips，前端即时生效） ---------------- */
const keyword = ref('')
const roleFilter = ref('')

const roleChips = computed(() => {
  const counts = new Map()
  for (const u of users.value) counts.set(u.role, (counts.get(u.role) || 0) + 1)
  return [
    { value: '', label: '全部', count: users.value.length },
    ...[...counts.entries()].map(([value, count]) => ({ value, label: value, count })),
  ]
})

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return users.value.filter((u) => {
    if (roleFilter.value && u.role !== roleFilter.value) return false
    if (kw && !`${u.name} ${u.display_name || ''}`.toLowerCase().includes(kw)) return false
    return true
  })
})

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
  const ok = await ElMessageBox.confirm(`确定禁用用户 ${row.name}？（软删除，保留审计记录）`, '确认', {
    type: 'warning',
  }).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.deleteUser(row.id)
  ElMessage.success('已禁用')
  await load()
}

async function onActAs(row) {
  await admin.actAs(row.id)
  ElMessage.success(`已切换为扮演「${row.display_name || row.name}」，去「对话测试」验证权限`)
}
onMounted(() => load().catch(() => {}))  // 失败已由拦截器 toast
</script>

<style scoped>
/* ============ 筛选栏（设计稿：搜索 + 角色 chips） ============ */
.filterbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  padding: var(--bc-space-3) var(--bc-space-4) 0;
}
.filterbar__search {
  width: 240px;
}
.filterbar__spacer {
  flex: 1;
}
.fchip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-full);
  background: transparent;
  color: var(--bc-text-2);
  font: 500 var(--bc-text-xs) / 1 inherit;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
}
.fchip .n {
  font-family: var(--bc-font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--bc-text-3);
}
.fchip:hover {
  border-color: var(--bc-border-strong);
  color: var(--bc-text-1);
}
.fchip.is-on {
  background: var(--bc-primary-6);
  border-color: var(--bc-primary-6);
  color: var(--bc-on-primary, #fff);
}
.fchip.is-on .n {
  color: var(--bc-on-primary, #fff);
  opacity: 0.72;
}

.table-scroll {
  overflow-x: auto;
}

.table-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  border-top: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-4);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.table-foot__num {
  font-variant-numeric: tabular-nums;
}

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

@media (max-width: 768px) {
  .filterbar__search {
    width: 100%;
    order: 5;
  }
  .filterbar__spacer {
    display: none;
  }
}
</style>
