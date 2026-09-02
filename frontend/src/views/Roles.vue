<template>
  <div class="bc-page">
    <PageHeader title="角色与权限" description="角色决定数字员工可用的工具白名单与知识域 · 默认隔离">
      <el-button type="primary" :icon="Plus" @click="openCreate">新建角色</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="4" />

    <template v-else>
      <!-- ============ 组合筛选（设计稿：搜索 + 维度筛选 + 计数） ============ -->
      <div class="toolbar">
        <el-input
          v-model="keyword"
          class="toolbar__search"
          placeholder="搜索角色名称或描述"
          clearable
          :prefix-icon="Search"
        />
        <el-select v-model="promptFilter" class="toolbar__select" aria-label="提示词筛选">
          <el-option label="全部提示词" value="all" />
          <el-option label="跟随全局" value="global" />
          <el-option label="已绑定版本" value="bound" />
        </el-select>
        <el-select v-model="scopeFilter" class="toolbar__select" aria-label="知识域筛选">
          <el-option label="全部知识域" value="all" />
          <el-option label="无可见知识（默认隔离）" value="none" />
          <el-option v-for="tag in scopeTags" :key="tag" :label="tag" :value="tag" />
        </el-select>
        <span class="toolbar__count">
          显示 <b>{{ filtered.length }}</b> / {{ roles.length }} 个
        </span>
      </div>

      <div class="bc-panel">
        <EmptyState
          v-if="!roles.length"
          icon="Key"
          title="还没有角色"
          description="角色是权限治理的基本单元：它决定员工能看到什么工具与知识。"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate">新建第一个角色</el-button>
        </EmptyState>

        <EmptyState
          v-else-if="!filtered.length"
          icon="Key"
          title="暂无匹配的角色"
          description="调整关键词或筛选条件后重试"
        >
          <el-button @click="clearFilter">清除筛选</el-button>
        </EmptyState>

        <template v-else>
          <el-table :data="filtered">
            <el-table-column label="角色" min-width="220">
              <template #default="{ row }">
                <div class="role-cell">
                  <span class="role-cell__glyph"><el-icon :size="15"><Avatar /></el-icon></span>
                  <div class="role-cell__body">
                    <span class="role-cell__name">{{ row.name }}</span>
                    <span class="role-cell__desc">{{ row.description || '—' }}</span>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="可用工具" width="96" align="right">
              <template #default="{ row }">
                <el-tooltip
                  v-if="row.config.allowed_tools?.length"
                  :content="row.config.allowed_tools.join(' · ')"
                  placement="top"
                  :show-after="300"
                >
                  <span class="mono bc-strong">{{ row.config.allowed_tools.length }} 项</span>
                </el-tooltip>
                <span v-else class="bc-muted">无</span>
              </template>
            </el-table-column>
            <el-table-column label="知识域" min-width="160">
              <template #default="{ row }">
                <template v-if="row.config.knowledge_scope_tags?.length">
                  <span v-for="t in row.config.knowledge_scope_tags.slice(0, 3)" :key="t" class="chip">{{ t }}</span>
                  <span v-if="row.config.knowledge_scope_tags.length > 3" class="chip chip-more">
                    +{{ row.config.knowledge_scope_tags.length - 3 }}
                  </span>
                </template>
                <span v-else class="bc-muted bc-text-sm">无（默认隔离）</span>
              </template>
            </el-table-column>
            <el-table-column label="Skills" min-width="130">
              <template #default="{ row }">
                <template v-if="row.config.skills?.length">
                  <span v-for="s in row.config.skills.slice(0, 2)" :key="s" class="chip">{{ s }}</span>
                  <span v-if="row.config.skills.length > 2" class="chip chip-more">
                    +{{ row.config.skills.length - 2 }}
                  </span>
                </template>
                <span v-else class="bc-muted bc-text-sm">无</span>
              </template>
            </el-table-column>
            <el-table-column label="职级" width="76" align="right">
              <template #default="{ row }">
                <span class="mono bc-strong">{{ row.config.level }}</span>
              </template>
            </el-table-column>
            <el-table-column label="提示词" width="104">
              <template #default="{ row }">
                <span v-if="row.config.system_prompt_version" class="pill-outline mono">
                  v{{ row.config.system_prompt_version }}
                </span>
                <span v-else class="bc-muted bc-text-sm">全局</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="128" fixed="right" align="right">
              <template #default="{ row }">
                <el-button size="small" text @click="openEdit(row)">编辑</el-button>
                <el-button size="small" text class="op-danger" @click="onDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="table-foot">
            <span>共 {{ roles.length }} 个角色 · 授权工具去重 {{ distinctTools }} 项 · 知识域 {{ scopeTags.length }} 个</span>
            <span class="mono table-foot__num">按名称排序</span>
          </div>
        </template>
      </div>
    </template>

    <!-- 新建 / 编辑对话框（逻辑保持不变） -->
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
import { Plus, Search, Avatar } from '@element-plus/icons-vue'
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

/* ---------------- 组合筛选 ---------------- */
const keyword = ref('')
const promptFilter = ref('all')
const scopeFilter = ref('all')

const scopeTags = computed(() => {
  const set = new Set()
  for (const r of roles.value) for (const t of r.config?.knowledge_scope_tags || []) set.add(t)
  return [...set].sort()
})

const distinctTools = computed(() => {
  const set = new Set()
  for (const r of roles.value) for (const t of r.config?.allowed_tools || []) set.add(t)
  return set.size
})

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return [...roles.value]
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
    .filter((r) => {
      if (kw && !`${r.name} ${r.description || ''}`.toLowerCase().includes(kw)) return false
      if (promptFilter.value === 'global' && r.config?.system_prompt_version) return false
      if (promptFilter.value === 'bound' && !r.config?.system_prompt_version) return false
      const tags = r.config?.knowledge_scope_tags || []
      if (scopeFilter.value === 'none' && tags.length) return false
      if (scopeFilter.value !== 'all' && scopeFilter.value !== 'none' && !tags.includes(scopeFilter.value)) return false
      return true
    })
})

function clearFilter() {
  keyword.value = ''
  promptFilter.value = 'all'
  scopeFilter.value = 'all'
}

/* ---------------- 工具按来源分组（内置 / MCP），供 el-select option-group 渲染 ---------------- */
const toolGroups = computed(() => {
  const builtin = tools.value.filter((t) => t.source !== 'mcp')
  const mcp = tools.value.filter((t) => t.source === 'mcp')
  const groups = []
  if (builtin.length) groups.push({ label: '内置工具', options: builtin })
  if (mcp.length) groups.push({ label: 'MCP 工具', options: mcp })
  return groups
})

/* ---------------- 数据加载 ---------------- */
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

/* ---------------- 新建 / 编辑 ---------------- */
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
  // 去重：重复值会产生重复 chip / 重复 v-for key，且原样写库无意义
  return [...new Set(str.split(',').map((s) => s.trim()).filter(Boolean))]
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
  const ok = await ElMessageBox.confirm(`确定删除角色「${row.name}」？`, '确认', { type: 'warning' }).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.deleteRole(row.name)
  ElMessage.success('已删除')
  await load()
}

onMounted(() => load().catch(() => {}))  // 失败已由拦截器 toast
</script>

<style scoped>
/* ============ 筛选栏 ============ */
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.toolbar__search {
  width: 260px;
}
.toolbar__select {
  width: 176px;
}
.toolbar__count {
  margin-left: auto;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.toolbar__count b {
  color: var(--bc-text-2);
  font-weight: var(--bc-weight-medium);
  font-variant-numeric: tabular-nums;
}

/* ============ 角色列（设计稿：glyph + 名称 + 描述两行） ============ */
.role-cell {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-1) 0;
}
.role-cell__glyph {
  width: 30px;
  height: 30px;
  border-radius: var(--bc-radius-md);
  background: var(--bc-bg-sunken);
  color: var(--bc-text-3);
  display: grid;
  place-items: center;
  flex: none;
}
.role-cell__body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.role-cell__name {
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.role-cell__desc {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
}

/* ============ 标签 chips（设计稿：hairline 描边小方块） ============ */
.chip {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--bc-border);
  border-radius: 5px;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin: 1px var(--bc-space-1) 1px 0;
  white-space: nowrap;
}
.chip-more {
  border-style: dashed;
}

/* ============ 提示词版本 pill ============ */
.pill-outline {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-full);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}

.op-danger:hover {
  color: var(--bc-danger-text, var(--el-color-danger));
}

/* ============ 表脚 ============ */
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

@media (max-width: 768px) {
  .toolbar__search {
    width: 100%;
    order: 5;
  }
  .toolbar__count {
    margin-left: 0;
    width: 100%;
  }
}
</style>
