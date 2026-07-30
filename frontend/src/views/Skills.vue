<template>
  <div class="bc-page">
    <PageHeader
      title="Skill 管理"
      description="Skill 是 admin 管控的领域知识文档（Markdown），按角色绑定注入 system prompt。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建技能</el-button>
      <el-button @click="onExport" :loading="exporting">导出</el-button>
      <el-button @click="onImportClick" :loading="importing">
        <input ref="importInput" type="file" accept=".zip" style="display:none" @change="onImportFile" />
        导入
      </el-button>
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
        <el-form-item label="披露方式">
          <el-select v-model="form.disclosure" style="width: 100%">
            <el-option label="始终注入 (always) — 全文常驻 system prompt" value="always" />
            <el-option label="按需加载 (on_demand) — 只注入索引，全文经工具按需取" value="on_demand" />
          </el-select>
          <div class="bc-hint">always：核心约束类技能；on_demand：省 token，适合大量参考文档</div>
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
const importInput = ref(null)
const exporting = ref(false)
const importing = ref(false)
const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
  emptyForm: () => ({ name: '', description: '', content: '', is_enabled: true, disclosure: 'always' }),
  toForm: (row) => ({
    name: row.name,
    description: row.description,
    content: row.content,
    is_enabled: row.is_enabled,
    disclosure: row.disclosure || 'always',
    _original_name: row.name,
  }),
})

// 导出
async function onExport() {
  exporting.value = true
  try {
    const blob = await admin.exportSkills()
    // 触发浏览器下载
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'skills-export.zip'
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch {
    // 错误已由拦截器处理
  } finally {
    exporting.value = false
  }
}

// 导入：触发文件选择
function onImportClick() {
  importInput.value?.value && (importInput.value.value = '')  // 重置以支持重复选同一文件
  importInput.value?.click()
}

// 导入文件选中后上传
async function onImportFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  importing.value = true
  try {
    const result = await admin.importSkills(file, 'skip')
    ElMessage.success(
      `导入完成：新建 ${result.total_created}、更新 ${result.total_updated}、跳过 ${result.total_skipped}`
    )
    if (result.errors?.length) {
      ElMessage.warning(`${result.errors.length} 项解析失败`)
    }
    await load()
  } catch {
    // 错误已由拦截器处理
  } finally {
    importing.value = false
  }
}

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
        disclosure: form.disclosure,
      })
    } else {
      await admin.createSkill({
        name: form.name,
        description: form.description,
        content: form.content,
        disclosure: form.disclosure,
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
