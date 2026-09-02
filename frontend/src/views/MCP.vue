<template>
  <div class="bc-page">
    <PageHeader
      title="MCP 工具服务器"
      description="注册外部 MCP server，工具自动发现并纳入治理脊柱 —— LLM 只能经角色白名单调用，agent 永不直连。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
      <el-button type="primary" :icon="Plus" @click="openCreate">注册 MCP Server</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="3" />

    <template v-else>
      <!-- ============ KPI 行（设计稿：label + mono 大数 + 真实脚注） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">已连接</div>
          <div class="kpi__value mono">{{ kpi.connected }}</div>
          <div class="kpi__meta">stdio × {{ kpi.stdio }} · http × {{ kpi.http }}</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">暴露工具数</div>
          <div class="kpi__value mono">{{ kpi.tools }}</div>
          <div class="kpi__meta">经角色白名单二次过滤</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">未连接</div>
          <div class="kpi__value mono">{{ kpi.disconnected }}</div>
          <div class="kpi__meta">
            <span v-if="kpi.disconnectedNames" class="kpi__pill kpi__pill--warn">{{ kpi.disconnectedNames }}</span>
            <span v-else class="kpi__pill kpi__pill--ok">全部在线</span>
          </div>
        </div>
      </div>

      <div class="bc-panel">
      <EmptyState
        v-if="!servers.length"
        icon="Connection"
        title="还没有 MCP Server"
        description="注册后其工具以 mcp__服务名__工具 形式进入工具目录，可在角色白名单中放行。"
      >
        <el-button type="primary" :icon="Plus" @click="openCreate">注册第一个 MCP Server</el-button>
      </EmptyState>

      <el-table v-else :data="servers">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <div class="srv-cell">
              <span class="bc-strong mono">{{ row.name }}</span>
              <span class="bc-muted bc-text-xs">{{ row.description || '—' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="传输" width="100">
          <template #default="{ row }">
            <el-tag :type="row.transport === 'stdio' ? 'info' : 'primary'" size="small">
              {{ row.transport }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="220">
          <template #default="{ row }">
            <span v-if="row.transport === 'stdio'" class="mono bc-text-xs bc-muted">
              {{ row.command }} {{ row.args.join(' ') }}
            </span>
            <span v-else class="mono bc-text-xs bc-muted">{{ row.url }}</span>
          </template>
        </el-table-column>
        <el-table-column label="工具数" width="90" align="center">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.tool_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusBadge :type="row.connected ? 'ok' : 'idle'" :text="row.connected ? '已连接' : '未连接'" />
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch :model-value="row.is_enabled" @change="(v) => toggleEnabled(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :loading="row._testing" @click="onTest(row)">测试</el-button>
            <el-button size="small" @click="onReload(row)">重载</el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    </template>

    <!-- 注册/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑 MCP Server' : '注册 MCP Server'" width="620px">
      <el-form :model="form" label-position="top">
        <el-form-item v-if="!editing" label="名称">
          <el-input v-model="form.name" placeholder="如 sales-tools（工具前缀 mcp__名称__）" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="这个工具服务器提供什么能力" />
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
            <div v-if="editing && form._hasEnv" class="bc-hint">已配置环境变量（不改则留空）</div>
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
            <div v-if="editing && form._hasHeaders" class="bc-hint">已配置 header（不改则留空）</div>
          </el-form-item>
        </template>

        <el-form-item v-if="!editing" label="启用">
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
      <div v-if="testResult.ok && testResult.tools.length" class="test-tools">
        <div class="bc-muted bc-text-sm test-tools__label">发现的工具：</div>
        <el-tag v-for="t in testResult.tools" :key="t" class="test-tools__tag" size="small">{{ t }}</el-tag>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const servers = ref([])
const loading = ref(false)

// KPI（真实数据派生）
const kpi = computed(() => {
  const list = servers.value
  const connected = list.filter((s) => s.connected)
  const disconnected = list.filter((s) => !s.connected)
  return {
    connected: connected.length,
    stdio: connected.filter((s) => s.transport === 'stdio').length,
    http: connected.filter((s) => s.transport !== 'stdio').length,
    tools: list.reduce((sum, s) => sum + (s.tool_count || 0), 0),
    disconnected: disconnected.length,
    disconnectedNames: disconnected.map((s) => s.name).join(' · ').slice(0, 30) || '',
  }
})
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

onMounted(() => load().catch(() => {}))  // 失败已由拦截器 toast

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
  const ok = await ElMessageBox.confirm(`确认删除 MCP server「${row.name}」？其工具将从治理管道移除。`, '删除', {
    type: 'warning',
  }).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.deleteMcpServer(row.id)
  ElMessage.success('已删除')
  await load()
}

async function onTest(row) {
  row._testing = true
  try {
    testResult.value = await admin.testMcpServer(row.id)
    testVisible.value = true
  } catch {
    // 失败已由拦截器 toast；这里保证 loading 复位
  } finally {
    row._testing = false
  }
}

async function onReload(row) {
  const r = await admin.reloadMcpServer(row.id).catch(() => null)
  if (!r) return
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
/* ============ KPI 行（设计稿语言：label + mono 大数 + 脚注） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
.kpi {
  padding: var(--bc-space-4);
  transition: box-shadow 0.15s var(--bc-ease, ease-out);
}
.kpi:hover {
  box-shadow: var(--bc-shadow-sm);
}
.kpi__label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  letter-spacing: 0.02em;
}
.kpi__value {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-3xl);
  font-weight: var(--bc-weight-semibold);
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.kpi__meta {
  margin-top: var(--bc-space-2);
  display: flex;
  align-items: center;
  gap: var(--bc-space-1);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  min-width: 0;
}
.kpi__pill {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 var(--bc-space-2);
  border-radius: var(--bc-radius-full);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kpi__pill--warn {
  background: var(--bc-warning-bg);
  color: var(--bc-warning-text);
}
.kpi__pill--ok {
  background: var(--bc-success-bg);
  color: var(--bc-success-text);
}

.srv-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.test-tools {
  margin-top: var(--bc-space-3);
}
.test-tools__label {
  margin-bottom: var(--bc-space-2);
}
.test-tools__tag {
  margin: 2px;
}
</style>
