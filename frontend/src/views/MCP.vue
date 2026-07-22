<template>
  <div v-loading="loading">
    <el-alert
      title="MCP 工具服务器"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
      description="注册外部 MCP server，其工具自动发现并纳入治理脊柱——LLM 只能通过角色白名单调用（mcp__{服务名}__{工具}）。agent 永不直连 MCP server。"
    />

    <div class="toolbar">
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon> 注册 MCP Server
      </el-button>
      <el-button @click="load" :loading="loading">刷新</el-button>
    </div>

    <el-table :data="servers" border>
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column prop="transport" label="传输" width="90">
        <template #default="{ row }">
          <el-tag :type="row.transport === 'stdio' ? '' : 'success'" size="small">
            {{ row.transport }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="地址" min-width="200">
        <template #default="{ row }">
          <span v-if="row.transport === 'stdio'" class="mono">
            {{ row.command }} {{ row.args.join(' ') }}
          </span>
          <span v-else class="mono">{{ row.url }}</span>
        </template>
      </el-table-column>
      <el-table-column label="工具" width="80" align="center">
        <template #default="{ row }">
          <el-badge :value="row.tool_count" :type="row.connected ? 'primary' : 'info'" />
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.connected ? 'success' : 'info'" size="small">
            {{ row.connected ? '已连接' : '未连接' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.is_enabled" @change="(v) => toggleEnabled(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="onTest(row)" :loading="row._testing">测试</el-button>
          <el-button size="small" @click="onReload(row)">重载</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 注册/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑 MCP Server' : '注册 MCP Server'" width="600px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="名称" v-if="!editing">
          <el-input v-model="form.name" placeholder="如 sales-tools（工具前缀 mcp__名称__）" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item label="传输方式">
          <el-radio-group v-model="form.transport">
            <el-radio value="stdio">stdio（本地子进程）</el-radio>
            <el-radio value="http">streamable HTTP（远程）</el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.transport === 'stdio'">
          <el-form-item label="命令">
            <el-input v-model="form.command" placeholder="如 uvx / npx / python" />
          </el-form-item>
          <el-form-item label="参数">
            <el-input
              v-model="argsInput"
              placeholder="空格分隔，如 mcp-server-time --local-timezone Asia/Shanghai"
            />
          </el-form-item>
          <el-form-item label="环境变量">
            <el-input
              v-model="envInput"
              type="textarea"
              :rows="3"
              placeholder='JSON，如 {"API_KEY": "xxx"}；留空 = 不改（编辑时）'
            />
            <div class="hint" v-if="editing && form._hasEnv">已配置环境变量（不改则留空）</div>
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="URL">
            <el-input v-model="form.url" placeholder="https://host/mcp" />
          </el-form-item>
          <el-form-item label="自定义 Header">
            <el-input
              v-model="headersInput"
              type="textarea"
              :rows="3"
              placeholder='JSON，如 {"Authorization": "Bearer xxx"}；留空 = 不改（编辑时）'
            />
            <div class="hint" v-if="editing && form._hasHeaders">已配置 header（不改则留空）</div>
          </el-form-item>
        </template>

        <el-form-item label="启用" v-if="!editing">
          <el-switch v-model="form.is_enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 测试结果 -->
    <el-dialog v-model="testVisible" title="连通性测试" width="520px">
      <el-result
        :icon="testResult.ok ? 'success' : 'error'"
        :title="testResult.ok ? '连接成功' : '连接失败'"
        :sub-title="testResult.message"
      />
      <div v-if="testResult.ok && testResult.tools.length" style="margin-top: 12px">
        <div class="muted" style="margin-bottom: 8px">发现的工具：</div>
        <el-tag
          v-for="t in testResult.tools"
          :key="t"
          style="margin: 2px"
          size="small"
        >{{ t }}</el-tag>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

const servers = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(false)
const testVisible = ref(false)
const testResult = ref({ ok: false, message: '', tools: [] })

const argsInput = ref('')
const envInput = ref('')
const headersInput = ref('')

const emptyForm = () => ({
  name: '',
  description: '',
  transport: 'stdio',
  command: '',
  url: '',
  is_enabled: true,
  _hasEnv: false,
  _hasHeaders: false,
})
const form = ref(emptyForm())

async function load() {
  loading.value = true
  try {
    servers.value = await admin.listMcpServers()
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openCreate() {
  editing.value = false
  form.value = emptyForm()
  argsInput.value = ''
  envInput.value = ''
  headersInput.value = ''
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = true
  form.value = { ...row, _hasEnv: row.has_env, _hasHeaders: row.has_headers }
  argsInput.value = row.args.join(' ')
  // 编辑时 env/headers 留空 = 不改
  envInput.value = ''
  headersInput.value = ''
  dialogVisible.value = true
}

function parseJsonOrEmpty(text, field) {
  const trimmed = text.trim()
  if (trimmed === '') return null // 不改
  try {
    return JSON.parse(trimmed)
  } catch {
    throw new Error(`${field} 不是合法 JSON`)
  }
}

async function onSave() {
  saving.value = true
  try {
    const f = form.value
    const args = argsInput.value.trim() ? argsInput.value.trim().split(/\s+/) : []
    const payload = {
      description: f.description,
      transport: f.transport,
    }
    if (f.transport === 'stdio') {
      payload.command = f.command
      payload.args = args
      const env = parseJsonOrEmpty(envInput.value, '环境变量')
      if (env !== null) payload.env = env
    } else {
      payload.url = f.url
      const headers = parseJsonOrEmpty(headersInput.value, 'Header')
      if (headers !== null) payload.headers = headers
    }
    if (!editing.value) {
      payload.name = f.name
      payload.is_enabled = f.is_enabled
      await admin.createMcpServer(payload)
      ElMessage.success('已注册')
    } else {
      await admin.updateMcpServer(f.id, payload)
      ElMessage.success('已保存')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    if (e instanceof Error && e.message.includes('JSON')) {
      ElMessage.error(e.message)
    }
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(`确认删除 MCP server「${row.name}」？其工具将从治理管道移除。`, '删除', {
    type: 'warning',
  })
  await admin.deleteMcpServer(row.id)
  ElMessage.success('已删除')
  await load()
}

async function onTest(row) {
  row._testing = true
  try {
    testResult.value = await admin.testMcpServer(row.id)
    testVisible.value = true
  } finally {
    row._testing = false
  }
}

async function onReload(row) {
  const r = await admin.reloadMcpServer(row.id)
  ElMessage[r.ok ? 'success' : 'warning'](r.message)
  await load()
}

async function toggleEnabled(row, enabled) {
  await admin.updateMcpServer(row.id, { is_enabled: enabled })
  ElMessage.success(enabled ? '已启用' : '已禁用')
  await load()
}
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
}
.mono {
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.muted {
  color: #909399;
}
.hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
