<template>
  <div class="bc-page">
    <PageHeader
      title="Skill 管理"
      description="Skill 是 admin 管控的领域知识文档（Markdown），按角色绑定注入 system prompt。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建技能</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="4" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!skills.length"
        icon="MagicStick"
        title="还没有 Skill"
        description="把领域知识（如销售话术、人事政策）沉淀为 Skill，按角色分发给数字员工。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个技能</el-button>
      </EmptyState>

      <el-table v-else :data="skills">
        <el-table-column label="技能名" width="180">
          <template #default="{ row }">
            <span class="bc-strong mono">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="180">
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="内容" min-width="300">
          <template #default="{ row }">
            <div class="bc-cell-clamp">{{ row.content }}</div>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90" align="center">
          <template #default="{ row }">
            <el-switch :model-value="row.is_enabled" @change="(val) => onToggle(row, val)" />
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">
            <span class="bc-text-sm bc-muted">{{ formatTime(row.updated_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑技能' : '新建技能'" width="680px">
      <el-form :model="form" label-position="top">
        <el-form-item v-if="!editing" label="技能名">
          <el-input v-model="form.name" placeholder="小写字母/数字/连字符，如 sales-playbook" />
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
          <el-switch v-model="form.is_enabled" active-text="启用" inactive-text="停用" />
          <div class="bc-hint">停用后该技能不会被注入任何角色的 system prompt</div>
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
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime } from '../utils/format'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const skills = ref([])
const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
  emptyForm: () => ({ name: '', description: '', content: '', is_enabled: true }),
  toForm: (row) => ({
    name: row.name,
    description: row.description,
    content: row.content,
    is_enabled: row.is_enabled,
    _original_name: row.name,
  }),
})

async function load() {
  loading.value = true
  try {
    skills.value = await admin.listSkills()
  } finally {
    loading.value = false
  }
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
    await admin.updateSkill(row.name, {
      description: row.description,
      content: row.content,
      is_enabled: val,
    })
    row.is_enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
  } catch {
    // 失败已由拦截器提示；开关回滚
    row.is_enabled = !val
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确定删除技能「${row.name}」？已绑定角色将不再注入它`, '确认', {
    type: 'warning',
  })
  await admin.deleteSkill(row.name)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>
