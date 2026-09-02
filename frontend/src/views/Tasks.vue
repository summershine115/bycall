<template>
  <div class="bc-page">
    <PageHeader
      title="任务执行"
      description="持久化多步任务：对话 / 定时 / 事件触发，审批暂停可恢复，SSE 实时进度。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="loadTasks" />
    </PageHeader>

    <!-- ============ KPI 行（设计稿：label + mono 大数 + 真实脚注） ============ -->
    <div class="kpis">
      <div class="kpi bc-panel">
        <div class="kpi__label">启用调度</div>
        <div class="kpi__value mono">{{ kpiData.enabledSchedules }}</div>
        <div class="kpi__meta">cron × {{ kpiData.cronCount }} · 事件 × {{ kpiData.eventCount }}</div>
      </div>
      <div class="kpi bc-panel">
        <div class="kpi__label">今日任务</div>
        <div class="kpi__value mono">{{ kpiData.todayTasks }}</div>
        <div class="kpi__meta">含对话 / 定时 / 事件触发</div>
      </div>
      <div class="kpi bc-panel">
        <div class="kpi__label">进行中</div>
        <div class="kpi__value mono">{{ kpiData.activeTasks }}</div>
        <div class="kpi__meta">规划 + 执行 + 暂停待审批</div>
      </div>
      <div class="kpi bc-panel">
        <div class="kpi__label">近期失败</div>
        <div class="kpi__value mono">{{ kpiData.failedTasks }}</div>
        <div class="kpi__meta">
          <span v-if="kpiData.failedTasks" class="kpi__pill kpi__pill--warn">需处理</span>
          <span v-else class="kpi__pill kpi__pill--ok">无积压</span>
        </div>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="tasks-tabs">
      <!-- ==================== 任务列表 ==================== -->
      <el-tab-pane label="任务列表" name="tasks">
        <ContentLoader v-if="tasksLoading" :rows="6" />
        <div v-else class="bc-panel">
          <EmptyState
            v-if="!tasks.length"
            icon="List"
            title="暂无任务"
            description="员工在对话中输入「启动任务：目标」、或调度/事件触发后，任务会出现在这里。"
          />
          <el-table v-else :data="tasks">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column label="用户" width="110">
              <template #default="{ row }">
                <span class="bc-strong">{{ row.user_name || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="目标" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="bc-text-sm">{{ row.goal }}</span>
              </template>
            </el-table-column>
            <el-table-column label="触发" width="100">
              <template #default="{ row }">
                <span class="bc-text-sm bc-muted">{{ triggerLabel(row.trigger) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="taskStatusType(row.status)" size="small">
                  {{ taskStatusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }">
                <span class="bc-text-sm">{{ fmtTime(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="openProgress(row)">进度</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ==================== 定时调度 ==================== -->
      <el-tab-pane label="定时调度" name="schedules">
        <div class="bc-panel">
          <div class="toolbar">
            <span class="bc-text-sm bc-muted">
              定时（cron）与事件（webhook）触发由管理员显式安排，任务以指定用户身份执行。
            </span>
            <el-button type="primary" :icon="Plus" @click="openScheduleDialog()">
              新建调度
            </el-button>
          </div>
          <!-- 触发类型 chips（设计稿：filterbar 带计数胶囊） -->
          <div class="filterbar">
            <button
              v-for="c in scheduleChips"
              :key="c.value"
              type="button"
              class="fchip"
              :class="{ 'is-on': scheduleFilter === c.value }"
              @click="scheduleFilter = c.value"
            >
              {{ c.label }} <span class="n">{{ c.count }}</span>
            </button>
          </div>
          <ContentLoader v-if="schedulesLoading" :rows="4" />
          <EmptyState
            v-else-if="!schedules.length"
            icon="Timer"
            title="暂无调度"
            description="新建 cron 定时任务或事件入口（外部系统 POST /api/events/{event_source} 触发）。"
          />
          <el-table v-else :data="filteredSchedules">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
            <el-table-column label="类型" width="90">
              <template #default="{ row }">
                <el-tag :type="row.trigger_type === 'cron' ? 'primary' : 'success'" size="small">
                  {{ row.trigger_type === 'cron' ? '定时' : '事件' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="触发配置" min-width="200">
              <template #default="{ row }">
                <span class="mono bc-text-xs">
                  {{ row.trigger_type === 'cron' ? row.cron_expr : row.event_source }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="执行用户" width="110">
              <template #default="{ row }">
                <span class="bc-text-sm">{{ userName(row.user_id) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="80">
              <template #default="{ row }">
                <el-switch
                  :model-value="row.is_enabled"
                  @change="(v) => toggleSchedule(row, v)"
                />
              </template>
            </el-table-column>
            <el-table-column label="上次触发" width="170">
              <template #default="{ row }">
                <span class="bc-text-sm">{{ fmtTime(row.last_fired_at) || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="fireSchedule(row)">触发一次</el-button>
                <el-button size="small" @click="openScheduleDialog(row)">编辑</el-button>
                <el-button size="small" type="danger" plain @click="removeSchedule(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ==================== 任务进度对话框（SSE 实时） ==================== -->
    <el-dialog v-model="progressVisible" title="任务进度" width="760px" destroy-on-close>
      <template v-if="progressTask">
        <div class="task-head">
          <div class="task-goal">{{ progressTask.goal }}</div>
          <div class="task-meta">
            <el-tag :type="taskStatusType(progressTask.status)" size="small">
              {{ taskStatusLabel(progressTask.status) }}
            </el-tag>
            <span class="bc-muted bc-text-sm">
              #{{ progressTask.id }} · {{ triggerLabel(progressTask.trigger) }} ·
              {{ progressTask.user_name || '—' }} · {{ fmtTime(progressTask.created_at) }}
            </span>
          </div>
        </div>

        <el-timeline v-if="progressSteps.length" class="task-timeline">
          <el-timeline-item
            v-for="(step, i) in progressSteps"
            :key="step.id"
            :type="stepType(step.status)"
            :hollow="step.status !== 'running'"
            :timestamp="`步骤 ${i + 1} · ${stepKindLabel(step.kind)}`"
          >
            <div class="step-row">
              <span class="step-goal">{{ stepGoal(step) }}</span>
              <el-tag :type="stepStatusType(step.status)" size="small">
                {{ stepStatusLabel(step.status) }}
              </el-tag>
            </div>
            <div v-if="stepRuns(step.id).length" class="step-runs">
              <div v-for="run in stepRuns(step.id)" :key="run.id" class="run-block">
                <div class="run-head bc-muted bc-text-xs">
                  尝试 #{{ run.id }} · {{ runDuration(run) }} · {{ runStatusLabel(run.status) }}
                </div>
                <pre v-if="runOutput(run)" class="run-output">{{ runOutput(run) }}</pre>
                <div v-if="runToolCalls(run).length" class="run-tools">
                  <el-tag
                    v-for="(tc, ti) in runToolCalls(run)"
                    :key="`${tc.name}-${ti}`"
                    size="small"
                    type="warning"
                    effect="plain"
                    class="tool-tag"
                  >
                    {{ tc.name }}
                  </el-tag>
                </div>
              </div>
            </div>
          </el-timeline-item>
        </el-timeline>
        <EmptyState v-else icon="List" title="暂无步骤" description="任务尚未拆解出执行步骤。" />
        <div v-if="sseConnected" class="sse-hint">● 实时推送已连接</div>
      </template>
    </el-dialog>

    <!-- ==================== 调度编辑对话框 ==================== -->
    <el-dialog
      v-model="scheduleVisible"
      :title="scheduleForm.id ? '编辑调度' : '新建调度'"
      width="560px"
      destroy-on-close
    >
      <el-form label-position="top">
        <el-form-item label="调度名称" required>
          <el-input v-model="scheduleForm.name" placeholder="如：每日晨间巡检" maxlength="100" />
        </el-form-item>
        <el-form-item label="触发类型" required>
          <el-radio-group v-model="scheduleForm.trigger_type">
            <el-radio-button value="cron">定时（cron）</el-radio-button>
            <el-radio-button value="event">事件（webhook）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="scheduleForm.trigger_type === 'cron'"
          label="cron 表达式（分 时 日 月 周）"
          required
        >
          <el-input v-model="scheduleForm.cron_expr" placeholder="如：0 9 * * *（每天 9:00）" />
        </el-form-item>
        <el-form-item
          v-else
          label="事件来源标识（外部系统 POST /api/events/{source} 触发）"
          required
        >
          <el-input v-model="scheduleForm.event_source" placeholder="如：webhook-orders" />
        </el-form-item>
        <el-form-item label="执行用户" required>
          <el-select v-model="scheduleForm.user_id" placeholder="选择执行身份" style="width: 100%">
            <el-option
              v-for="u in users"
              :key="u.id"
              :label="`${u.display_name || u.name}（${u.role}）`"
              :value="u.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="任务模板（goal 支持 {payload.xxx} 占位符）" required>
          <el-input
            v-model="scheduleForm.goal"
            type="textarea"
            :rows="2"
            placeholder="如：处理订单 {payload.order_id}"
          />
        </el-form-item>
        <el-form-item label="初始计划（可选 JSON：[{&quot;goal&quot;: &quot;…&quot;, &quot;kind&quot;: &quot;tool_loop&quot;}]，留空由 LLM 拆解）">
          <el-input
            v-model="scheduleForm.plan_json"
            type="textarea"
            :rows="3"
            placeholder='[{"goal": "第一步", "kind": "tool_loop"}]'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="scheduleVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingSchedule" @click="saveSchedule">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime as fmtTime } from '../utils/format'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const activeTab = ref('tasks')

// ==================== KPI（真实数据派生） ====================
const isToday = (iso) => {
  if (!iso) return false
  const d = new Date(iso)
  const now = new Date()
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  )
}
const kpiData = computed(() => {
  const enabled = schedules.value.filter((s) => s.is_enabled)
  return {
    enabledSchedules: enabled.length,
    cronCount: enabled.filter((s) => s.trigger_type === 'cron').length,
    eventCount: enabled.filter((s) => s.trigger_type !== 'cron').length,
    todayTasks: tasks.value.filter((t) => isToday(t.created_at)).length,
    activeTasks: tasks.value.filter((t) => ['planning', 'running', 'paused'].includes(t.status)).length,
    failedTasks: tasks.value.filter((t) => t.status === 'failed').length,
  }
})

// ==================== 调度类型筛选（chips） ====================
const scheduleFilter = ref('all')
const filteredSchedules = computed(() =>
  scheduleFilter.value === 'all'
    ? schedules.value
    : schedules.value.filter((s) => s.trigger_type === scheduleFilter.value),
)
const scheduleChips = computed(() => [
  { label: '全部', value: 'all', count: schedules.value.length },
  {
    label: '定时 cron',
    value: 'cron',
    count: schedules.value.filter((s) => s.trigger_type === 'cron').length,
  },
  {
    label: '事件 webhook',
    value: 'event',
    count: schedules.value.filter((s) => s.trigger_type !== 'cron').length,
  },
])

// ==================== 任务列表 ====================
const tasks = ref([])
const tasksLoading = ref(false)

async function loadTasks() {
  tasksLoading.value = true
  try {
    tasks.value = await admin.listTasks({ limit: 200 })
  } finally {
    tasksLoading.value = false
  }
}

// ==================== 任务进度（SSE） ====================
const progressVisible = ref(false)
const progressTask = ref(null)
const progressSteps = ref([])
const progressRuns = ref([])
const sseConnected = ref(false)
let eventSource = null

function triggerLabel(t) {
  return { conversation: '对话', cron: '定时', event: '事件' }[t] || t
}

// Run 状态（task_engine 写入 ok/error，超时归入 error）
function runStatusLabel(s) {
  return { ok: '成功', error: '失败', timeout: '超时' }[s] || s
}

function taskStatusLabel(s) {
  return {
    draft: '草稿',
    planning: '规划中',
    running: '执行中',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }[s] || s
}

function taskStatusType(s) {
  return {
    running: 'primary',
    paused: 'warning',
    completed: 'success',
    failed: 'danger',
    cancelled: 'info',
    planning: 'primary',
  }[s] || 'info'
}

function stepKindLabel(k) {
  return { tool_loop: '工具回路', approval_wait: '等待审批', clarify: '等待澄清' }[k] || k
}

function stepStatusLabel(s) {
  return {
    pending: '待执行',
    running: '执行中',
    done: '已完成',
    failed: '失败',
    skipped: '已跳过',
    awaiting_approval: '待审批',
  }[s] || s
}

function stepStatusType(s) {
  return {
    running: 'primary',
    done: 'success',
    failed: 'danger',
    awaiting_approval: 'warning',
    pending: 'info',
    skipped: 'info',
  }[s] || 'info'
}

function stepType(s) {
  // el-timeline-item 类型：primary/success/warning/danger/info
  return { running: 'primary', done: 'success', failed: 'danger', awaiting_approval: 'warning' }[s] || 'info'
}

function stepGoal(step) {
  try {
    const spec = JSON.parse(step.spec_json)
    return spec.goal || step.spec_json
  } catch {
    return step.spec_json
  }
}

function stepRuns(stepId) {
  return progressRuns.value.filter((r) => r.step_id === stepId)
}

function runDuration(run) {
  return run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '—'
}

function runOutput(run) {
  try {
    const out = JSON.parse(run.output_json)
    if (typeof out === 'object' && out.answer) return String(out.answer)
    return JSON.stringify(out)
  } catch {
    return ''
  }
}

function runToolCalls(run) {
  try {
    const calls = JSON.parse(run.tool_calls_json)
    return Array.isArray(calls) ? calls : []
  } catch {
    return []
  }
}

async function refreshProgressDetail() {
  if (!progressTask.value) return
  const detail = await admin.getTask(progressTask.value.id)
  progressTask.value = detail.task
  progressSteps.value = detail.steps
  progressRuns.value = detail.runs
}

function openProgress(row) {
  progressTask.value = row
  progressSteps.value = []
  progressRuns.value = []
  sseConnected.value = false
  progressVisible.value = true
  refreshProgressDetail().catch(() => {})
  connectEvents(row.id)
}

function connectEvents(taskId) {
  if (eventSource) eventSource.close()
  eventSource = new EventSource(admin.taskEventsUrl(taskId))
  eventSource.onopen = () => {
    sseConnected.value = true
  }
  eventSource.onerror = () => {
    // 断线时浏览器自动重连；对话框关闭由下方 watch 显式 close。
    sseConnected.value = false
  }
  eventSource.onmessage = (msg) => {
    // 事件即"有变化"的信号：重拉详情拿权威状态（任务执行低频，开销可忽略）。
    refreshProgressDetail().catch(() => {})
  }
}

// 对话框关闭即断开 SSE：否则后台持续收事件重拉详情，会话过期时还会无限重连
watch(progressVisible, (open) => {
  if (!open && eventSource) {
    eventSource.close()
    eventSource = null
    sseConnected.value = false
  }
})

// ==================== 调度管理 ====================
const schedules = ref([])
const schedulesLoading = ref(false)
const users = ref([])
const scheduleVisible = ref(false)
const savingSchedule = ref(false)
const scheduleForm = ref({})

function userName(uid) {
  const u = users.value.find((x) => x.id === uid)
  return u ? u.display_name || u.name : `#${uid}`
}

async function loadSchedules() {
  schedulesLoading.value = true
  try {
    schedules.value = await admin.listTaskSchedules()
  } finally {
    schedulesLoading.value = false
  }
}

function openScheduleDialog(row) {
  scheduleForm.value = row
    ? {
        id: row.id,
        name: row.name,
        trigger_type: row.trigger_type,
        cron_expr: row.cron_expr,
        event_source: row.event_source,
        user_id: row.user_id,
        // 对话框没有启用开关：编辑时保留原启用状态，避免 PATCH 语义下静默翻转
        is_enabled: row.is_enabled,
        goal: row.task_template?.goal || '',
        plan_json: row.task_template?.plan ? JSON.stringify(row.task_template.plan) : '',
      }
    : {
        id: null,
        is_enabled: true,
        name: '',
        trigger_type: 'cron',
        cron_expr: '0 9 * * *',
        event_source: '',
        user_id: null,
        goal: '',
        plan_json: '',
      }
  scheduleVisible.value = true
}

function parsePlan(jsonText) {
  const text = (jsonText || '').trim()
  if (!text) return []
  try {
    const plan = JSON.parse(text)
    if (!Array.isArray(plan)) throw new Error('必须是数组')
    return plan
  } catch (e) {
    ElMessage.error(`初始计划 JSON 非法：${e.message}`)
    return null
  }
}

async function saveSchedule() {
  const f = scheduleForm.value
  if (!f.name || !f.user_id || !f.goal) {
    ElMessage.warning('请填写名称、执行用户与任务模板 goal')
    return
  }
  const plan = parsePlan(f.plan_json)
  if (plan === null) return
  const payload = {
    name: f.name,
    trigger_type: f.trigger_type,
    cron_expr: f.cron_expr,
    event_source: f.event_source,
    user_id: f.user_id,
    task_template: { goal: f.goal, ...(plan.length ? { plan } : {}) },
    is_enabled: f.is_enabled ?? true,
  }
  savingSchedule.value = true
  try {
    if (f.id) {
      await admin.updateTaskSchedule(f.id, payload)
      ElMessage.success('调度已更新')
    } else {
      await admin.createTaskSchedule(payload)
      ElMessage.success('调度已创建')
    }
    scheduleVisible.value = false
    await loadSchedules()
  } catch {
    // 错误 toast 由 axios 拦截器统一处理
  } finally {
    savingSchedule.value = false
  }
}

async function toggleSchedule(row, enabled) {
  try {
    await admin.updateTaskSchedule(row.id, { is_enabled: enabled })
    row.is_enabled = enabled
    ElMessage.success(enabled ? '已启用' : '已停用')
  } catch {
    // 失败时刷新列表恢复开关状态
    loadSchedules()
  }
}

async function fireSchedule(row) {
  try {
    const task = await admin.fireTaskSchedule(row.id)
    ElMessage.success(`已触发任务 #${task.id}`)
    await loadSchedules()
    loadTasks()
  } catch {
    // 错误 toast 统一处理
  }
}

async function removeSchedule(row) {
  const ok = await ElMessageBox.confirm(
    `删除调度「${row.name}」？已触发的任务不受影响。`,
    '确认删除',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  ).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.deleteTaskSchedule(row.id)
  ElMessage.success('已删除')
  await loadSchedules()
}

onMounted(async () => {
  loadTasks()
  loadSchedules()
  try {
    users.value = await admin.listUsers()
  } catch {
    users.value = []
  }
})

onBeforeUnmount(() => {
  if (eventSource) eventSource.close()
})
</script>

<style scoped>
/* ============ KPI 行（设计稿语言：label + mono 大数 + 脚注） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
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
}
.kpi__pill {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 var(--bc-space-2);
  border-radius: var(--bc-radius-full);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
}
.kpi__pill--warn {
  background: var(--bc-warning-bg);
  color: var(--bc-warning-text);
}
.kpi__pill--ok {
  background: var(--bc-success-bg);
  color: var(--bc-success-text);
}

/* ============ 触发类型 chips ============ */
.filterbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-3);
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
  font-family: inherit;
  font-size: var(--bc-text-xs);
  font-weight: 500;
  line-height: 1;
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
}
.fchip .n {
  font-family: var(--bc-font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
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

.tasks-tabs {
  margin-top: var(--bc-space-2);
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  margin-bottom: var(--bc-space-3);
}
.task-head {
  margin-bottom: var(--bc-space-4);
}
.task-goal {
  font-weight: 600;
  margin-bottom: var(--bc-space-2);
}
.task-meta {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.task-timeline {
  padding-left: var(--bc-space-2);
}
.step-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-2);
}
.step-goal {
  font-size: var(--bc-text-sm);
}
.step-runs {
  margin-top: var(--bc-space-2);
}
.run-block {
  border: 1px solid var(--bc-gray-2);
  border-radius: var(--bc-radius-sm);
  padding: var(--bc-space-2) var(--bc-space-3);
  margin-top: var(--bc-space-2);
  background: var(--bc-gray-1);
}
.run-head {
  margin-bottom: var(--bc-space-1);
}
.run-output {
  margin: 0;
  font-size: var(--bc-text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
  color: var(--bc-text-2);
}
.run-tools {
  margin-top: var(--bc-space-1);
  display: flex;
  gap: var(--bc-space-1);
  flex-wrap: wrap;
}
.sse-hint {
  margin-top: var(--bc-space-3);
  font-size: var(--bc-text-xs);
  color: var(--bc-success);
}
</style>
