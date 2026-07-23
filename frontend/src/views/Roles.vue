<template>
  <div class="bc-page">
    <PageHeader
      title="角色与权限"
      description="角色的权限边界：工具白名单、知识域标签、Skill 分发与职级（ABAC）。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建角色</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="4" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!roles.length"
        icon="Key"
        title="还没有自定义角色"
        description="角色是权限治理的基本单元：它决定员工能看到什么工具与知识。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个角色</el-button>
      </EmptyState>

      <el-table v-else :data="roles">
        <el-table-column label="角色" min-width="160">
          <template #default="{ row }">
            <div class="role-cell">
              <span class="bc-strong">{{ row.name }}</span>
              <span class="bc-muted bc-text-xs">{{ row.description || '—' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="可用工具" min-width="170">
          <template #default="{ row }">
            <el-tag v-for="t in row.config.allowed_tools" :key="t" size="small" class="list-tag">{{ t }}</el-tag>
            <span v-if="!row.config.allowed_tools?.length" class="bc-muted bc-text-sm">无</span>
          </template>
        </el-table-column>
        <el-table-column label="知识域" min-width="150">
          <template #default="{ row }">
            <el-tag v-for="t in row.config.knowledge_scope_tags" :key="t" size="small" type="success" class="list-tag">
              {{ t }}
            </el-tag>
            <span v-if="!row.config.knowledge_scope_tags?.length" class="bc-muted bc-text-sm">无（默认隔离）</span>
          </template>
        </el-table-column>
        <el-table-column label="Skills" min-width="130">
          <template #default="{ row }">
            <el-tag v-for="s in row.config.skills" :key="s" size="small" type="info" class="list-tag">{{ s }}</el-tag>
            <span v-if="!row.config.skills?.length" class="bc-muted bc-text-sm">无</span>
          </template>
        </el-table-column>
        <el-table-column label="职级" width="76" align="center">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.config.level }}</span>
          </template>
        </el-table-column>
        <el-table-column label="提示词" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.config.system_prompt_version" size="small" type="warning">
              v{{ row.config.system_prompt_version }}
            </el-tag>
            <span v-else class="bc-muted bc-text-sm">全局</span>
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

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑角色' : '新建角色'" width="560px">
      <el-form :model="form" label-position="top">
        <el-form-item v-if="!editing" label="角色名">
          <el-input v-model="form.name" placeholder="如 sales / hr（不能是 admin）" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明这个角色面向谁" />
        </el-form-item>
        <el-form-item label="可用工具">
          <el-select
            v-model="allowedTools"
            multiple
            filterable
            allow-create
            :reserve-keyword="false"
            placeholder="选择工具；可手动输入 mcp__服务名__* 通配整组"
            style="width: 100%"
          >
            <el-option-group v-for="grp in toolGroups" :key="grp.label" :label="grp.label">
              <el-option v-for="t in grp.options" :key="t.name" :label="t.name" :value="t.name" />
            </el-option-group>
          </el-select>
          <div class="bc-hint">LLM 的工具列表 = 这个集合；不在此列的工具 LLM 根本看不到</div>
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-input v-model="scopeInput" placeholder="逗号分隔，如 public,sales,hr" />
          <div class="bc-hint">RAG 预过滤：只召回这些标签的知识；留空 = 无可见知识（默认隔离）</div>
        </el-form-item>
        <el-form-item label="绑定 Skills">
          <el-select v-model="form.skills" multiple filterable placeholder="选择注入该角色 system prompt 的技能" style="width: 100%">
            <el-option v-for="s in skills" :key="s.name" :label="s.name" :value="s.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="职级 level">
          <el-input-number v-model="form.level" :min="0" :max="999" />
          <div class="bc-hint">ABAC：机密资源按 resource_level ≤ user_level 放行</div>
        </el-form-item>
        <el-form-item label="系统提示词">
          <el-select v-model="form.system_prompt_version" placeholder="使用全局激活版本" clearable style="width: 100%">
            <el-option
              v-for="p in prompts"
              :key="p.version"
              :label="`v${p.version}${p.label ? ' - ' + p.label : ''}${p.is_active ? '（激活中）' : ''}`"
              :value="p.version"
            />
          </el-select>
          <div class="bc-hint">绑定后该角色使用此版本提示词；留空 = 跟随全局激活版本</div>
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
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const roles = ref([])
const prompts = ref([])
const skills = ref([])
const tools = ref([])
const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = reactive({ name: '', description: '', level: 0, system_prompt_version: null, skills: [] })
const allowedTools = ref([]) // 可用工具白名单（多选 + 可手输通配）
const scopeInput = ref('')

// 工具按来源分组（内置 / MCP），供 el-select option-group 渲染
const toolGroups = computed(() => {
  const builtin = tools.value.filter((t) => t.source !== 'mcp')
  const mcp = tools.value.filter((t) => t.source === 'mcp')
  const groups = []
  if (builtin.length) groups.push({ label: '内置工具', options: builtin })
  if (mcp.length) groups.push({ label: 'MCP 工具', options: mcp })
  return groups
})

async function load() {
  loading.value = true
  try {
    ;[roles.value] = await Promise.all([admin.listRoles(), loadPrompts(), loadSkills(), loadTools()])
  } finally {
    loading.value = false
  }
}

// 以下三个加载失败时降级为空列表，不阻塞角色列表本身
async function loadPrompts() {
  try {
    prompts.value = await admin.listPrompts()
  } catch {
    prompts.value = []
  }
}
async function loadSkills() {
  try {
    skills.value = await admin.listSkills()
  } catch {
    skills.value = []
  }
}
async function loadTools() {
  try {
    tools.value = await admin.listTools()
  } catch {
    tools.value = []
  }
}

function openCreate() {
  editing.value = false
  Object.assign(form, { name: '', description: '', level: 0, system_prompt_version: null, skills: [] })
  allowedTools.value = []
  scopeInput.value = ''
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  Object.assign(form, {
    name: row.name,
    description: row.description,
    level: row.config.level,
    system_prompt_version: row.config.system_prompt_version ?? null,
    skills: [...(row.config.skills || [])],
  })
  form._original_name = row.name
  allowedTools.value = [...(row.config.allowed_tools || [])]
  scopeInput.value = (row.config.knowledge_scope_tags || []).join(', ')
  dialogVisible.value = true
}

function parseList(str) {
  return str.split(',').map((s) => s.trim()).filter(Boolean)
}

async function onSave() {
  saving.value = true
  const config = {
    allowed_tools: allowedTools.value,
    knowledge_scope_tags: parseList(scopeInput.value),
    level: form.level,
    system_prompt_version: form.system_prompt_version || null,
    skills: form.skills,
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
.role-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.list-tag {
  margin: 1px var(--bc-space-1) 1px 0;
}
</style>
