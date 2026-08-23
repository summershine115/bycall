<template>
  <div class="bc-page">
    <PageHeader
      title="审批队列"
      description="人在环路审批：敏感工具调用须人工批准后执行，结果与备注全程留痕。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="6" />

    <div v-else class="row-2-1">
      <!-- ============ 左：待办与历史（设计稿：chips 状态筛选 + 表格 + 表脚） ============ -->
      <div class="bc-panel appr-card">
        <div class="card-head">
          <span class="card-head__title">待办与历史</span>
          <span class="mono card-head__hint">共 {{ approvals.length }} 条</span>
        </div>

        <div class="filterbar">
          <button
            v-for="st in STATUS"
            :key="st.value"
            type="button"
            :class="['fchip', { 'is-on': statusFilter === st.value }]"
            @click="statusFilter = st.value"
          >
            {{ st.label }} <span class="n">{{ statusCount(st.value) }}</span>
          </button>
        </div>

        <EmptyState
          v-if="!filtered.length"
          icon="DocumentChecked"
          :title="statusFilter ? '该状态下暂无审批请求' : '暂无审批请求'"
          description="数字员工调用敏感工具时会产生审批请求，在此批准或拒绝后才会真正执行。"
        />

        <template v-else>
          <div class="table-scroll">
            <el-table :data="filtered">
              <el-table-column label="ID" width="84">
                <template #default="{ row }">
                  <span class="mono bc-text-sm bc-muted">#A-{{ row.id }}</span>
                </template>
              </el-table-column>
              <el-table-column label="用户" width="100">
                <template #default="{ row }">
                  <span class="bc-strong">{{ row.user_name || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="工具" min-width="150">
                <template #default="{ row }">
                  <span class="mono bc-text-sm">{{ row.tool_name }}</span>
                </template>
              </el-table-column>
              <el-table-column label="工具参数" min-width="170">
                <template #default="{ row }">
                  <span v-if="hasArgs(row.tool_args)" class="mono bc-text-xs bc-muted arg-cell" :title="formatJson(row.tool_args)">
                    {{ truncateArgs(row.tool_args) }}
                  </span>
                  <span v-else class="bc-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="审批人 / 时间" width="160">
                <template #default="{ row }">
                  <span v-if="row.reviewed_by" class="mono bc-text-xs bc-text-2 reviewer-cell">
                    {{ row.reviewed_by }} · {{ fmtTime(row.reviewed_at) }}
                  </span>
                  <span v-else class="bc-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="170" fixed="right" align="right">
                <template #default="{ row }">
                  <template v-if="row.status === 'pending'">
                    <el-button size="small" type="primary" @click="openReview(row, 'approve')">通过</el-button>
                    <el-button size="small" type="danger" plain @click="openReview(row, 'reject')">拒绝</el-button>
                  </template>
                  <el-button v-else size="small" text @click="openDetail(row)">
                    {{ row.status === 'rejected' ? '审批备注' : '执行结果' }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="table-foot">
            <span>批准时可选授权档位：仅本次 / 本会话 / 永久</span>
            <span class="mono table-foot__num">{{ filtered.length }} 条记录</span>
          </div>
        </template>
      </div>

      <!-- ============ 右：授权管理（三档台账，紧凑列表 + 即时撤销） ============ -->
      <div class="bc-panel appr-card">
        <div class="card-head">
          <span class="card-head__title">授权管理</span>
          <span class="mono card-head__hint">{{ activeGrants.length }} 项生效中</span>
        </div>

        <EmptyState
          v-if="!grants.length"
          icon="Key"
          title="暂无审批授权"
          description="批准审批时选择「本会话免审」或「永久免审」会在此登记，可随时撤销以恢复人审。"
        />

        <div v-else class="grant-list">
          <div
            v-for="g in grants"
            :key="g.id"
            :class="['grant-row', { 'is-revoked': g.status !== 'active' }]"
          >
            <div class="grant-row__top">
              <span class="bc-strong">{{ g.user_name || `#${g.user_id}` }}</span>
              <el-tag :type="g.scope === 'always' ? 'danger' : 'warning'" size="small">
                {{ scopeLabel(g.scope) }}
              </el-tag>
            </div>
            <div class="grant-row__tool mono">{{ g.tool_name }}</div>
            <div class="grant-row__meta">
              <span>{{ g.scope === 'session' ? `会话 #${g.conversation_id}` : '全部会话' }}</span>
              <span class="grant-row__sep">·</span>
              <span>{{ g.granted_by || '—' }} 授予</span>
              <span class="grant-row__sep">·</span>
              <span class="mono">{{ fmtTime(g.granted_at) }}</span>
            </div>
            <div class="grant-row__ops">
              <el-button
                v-if="g.status === 'active'"
                size="small"
                type="danger"
                text
                @click="onRevokeGrant(g)"
              >
                撤销授权
              </el-button>
              <span v-else class="mono bc-text-xs bc-muted">已撤销 {{ fmtTime(g.revoked_at) }}</span>
            </div>
          </div>
        </div>

        <div v-if="grants.length" class="table-foot">
          <span>撤销后立即恢复人工审批</span>
        </div>
      </div>
    </div>

    <!-- 审批对话框（批准 / 拒绝共用） -->
    <el-dialog
      v-model="reviewVisible"
      :title="reviewAction === 'approve' ? '批准审批' : '拒绝审批'"
      width="520px"
    >
      <div class="review-target">
        <span class="bc-muted bc-text-sm">目标工具：</span>
        <span class="mono bc-text-sm bc-strong">{{ reviewRow?.tool_name }}</span>
      </div>
      <el-form label-position="top" style="margin-top: var(--bc-space-3)">
        <el-form-item v-if="reviewAction === 'approve'" label="授权档位">
          <el-radio-group v-model="reviewGrantScope">
            <el-radio value="once">仅本次</el-radio>
            <el-radio value="session" :disabled="reviewRow?.conversation_id == null">
              本会话免审
            </el-radio>
            <el-radio value="always">永久免审</el-radio>
          </el-radio-group>
          <div class="grant-hint bc-text-xs bc-muted">{{ grantHint }}</div>
        </el-form-item>
        <el-form-item label="审批备注">
          <el-input
            v-model="reviewNote"
            type="textarea"
            :rows="3"
            placeholder="可选，记录本次审批的理由"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reviewVisible = false">取消</el-button>
        <el-button
          :type="reviewAction === 'approve' ? 'primary' : 'danger'"
          :loading="reviewing"
          @click="onReview"
        >
          {{ reviewAction === 'approve' ? '确认批准' : '确认拒绝' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 详情对话框 -->
    <el-dialog v-model="detailVisible" title="审批详情" width="620px">
      <el-descriptions v-if="detailRow" :column="1" border>
        <el-descriptions-item label="ID">#A-{{ detailRow.id }}</el-descriptions-item>
        <el-descriptions-item label="用户">{{ detailRow.user_name || '—' }}</el-descriptions-item>
        <el-descriptions-item label="渠道">{{ detailRow.channel || '—' }}</el-descriptions-item>
        <el-descriptions-item label="工具">
          <span class="mono">{{ detailRow.tool_name }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusType(detailRow.status)" size="small">{{ statusLabel(detailRow.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="工具参数">
          <pre v-if="hasArgs(detailRow.tool_args)" class="json-block">{{ formatJson(detailRow.tool_args) }}</pre>
          <span v-else class="bc-muted">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="审批人">{{ detailRow.reviewed_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="审批时间">{{ fmtTime(detailRow.reviewed_at) || '—' }}</el-descriptions-item>
        <el-descriptions-item label="执行时间">{{ fmtTime(detailRow.executed_at) || '—' }}</el-descriptions-item>
        <el-descriptions-item label="审批备注">{{ detailRow.review_note || '—' }}</el-descriptions-item>
        <el-descriptions-item label="执行结果">
          <pre v-if="detailRow.result_text" class="json-block">{{ detailRow.result_text }}</pre>
          <span v-else class="bc-muted">—</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime as fmtTime } from '../utils/format'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const approvals = ref([])
const loading = ref(false)
const statusFilter = ref('') // '' = 全部（前端 chips 即时过滤）

// 授权管理（三档台账）
const grants = ref([])

// 审批对话框状态
const reviewVisible = ref(false)
const reviewAction = ref('approve') // approve | reject
const reviewRow = ref(null)
const reviewNote = ref('')
const reviewGrantScope = ref('once') // once | session | always
const reviewing = ref(false)

// 详情对话框状态
const detailVisible = ref(false)
const detailRow = ref(null)

const STATUS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待审批' },
  { value: 'executed', label: '已执行' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'failed', label: '失败' },
]

const statusCount = (v) => (v ? approvals.value.filter((a) => a.status === v).length : approvals.value.length)
const filtered = computed(() => (statusFilter.value ? approvals.value.filter((a) => a.status === statusFilter.value) : approvals.value))
const activeGrants = computed(() => grants.value.filter((g) => g.status === 'active'))

async function load() {
  loading.value = true
  try {
    await Promise.all([loadApprovals(), loadGrants()])
  } finally {
    loading.value = false
  }
}

async function loadApprovals() {
  approvals.value = await admin.listApprovals('')
}

async function loadGrants() {
  grants.value = await admin.listApprovalGrants()
}

onMounted(load)

// —— 状态展示映射：pending=待审批 ... failed=失败 ——
function statusLabel(s) {
  return { pending: '待审批', executed: '已执行', rejected: '已拒绝', failed: '失败' }[s] || s
}
function statusType(s) {
  return { pending: 'warning', executed: 'success', rejected: 'danger', failed: 'info' }[s] || 'info'
}

// —— 工具参数展示 ——
function hasArgs(args) {
  return args != null && typeof args === 'object' && Object.keys(args).length > 0
}
function truncateArgs(args) {
  const s = JSON.stringify(args)
  return s.length > 36 ? s.slice(0, 36) + '…' : s
}
function formatJson(v) {
  try {
    return typeof v === 'string' ? JSON.stringify(JSON.parse(v), null, 2) : JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

// —— 审批动作 ——
function openReview(row, action) {
  reviewRow.value = row
  reviewAction.value = action
  reviewNote.value = ''
  reviewGrantScope.value = 'once'
  reviewVisible.value = true
}

const grantHints = {
  once: '仅本次执行需要人审（默认）；后续同类调用仍会进审批队列。',
  session: `本会话（#${reviewRow.value?.conversation_id}）内该工具免审直执行；跨会话仍需人审。`,
  always: '该用户此后调用该工具一律免审直执行（仍受权限矩阵约束）；可随时在「授权管理」撤销。',
}
const grantHint = computed(() => grantHints[reviewGrantScope.value] || '')

async function onReview() {
  reviewing.value = true
  try {
    const row = reviewRow.value
    if (reviewAction.value === 'approve') {
      const result = await admin.approveApproval(row.id, reviewNote.value, reviewGrantScope.value)
      if (result.status === 'failed') {
        ElMessage.warning('已批准，但工具执行失败')
      } else if (reviewGrantScope.value !== 'once') {
        ElMessage.success(`已批准并执行；已登记${scopeLabel(reviewGrantScope.value)}授权`)
      } else {
        ElMessage.success('已批准并执行')
      }
    } else {
      await admin.rejectApproval(row.id, reviewNote.value)
      ElMessage.success('已拒绝')
    }
    reviewVisible.value = false
    await load()
  } finally {
    reviewing.value = false
  }
}

// —— 授权档位 ——
function scopeLabel(scope) {
  return { session: '会话级', always: '永久' }[scope] || scope
}

async function onRevokeGrant(row) {
  try {
    await ElMessageBox.confirm(
      `撤销后 ${row.user_name || '该用户'} 调用 ${row.tool_name} 将立即恢复人审。`,
      '撤销授权',
      { type: 'warning', confirmButtonText: '撤销', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  await admin.revokeApprovalGrant(row.id)
  ElMessage.success('授权已撤销，恢复人审')
  await loadGrants()
}

// —— 详情 ——
function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}
</script>

<style scoped>
/* ============ 主行布局：2 : 1（设计稿 row-2-1） ============ */
.row-2-1 {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--bc-space-4);
  align-items: start;
}
.appr-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* ============ 卡头 ============ */
.card-head {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  padding: var(--bc-space-3) var(--bc-space-4);
  border-bottom: 1px solid var(--bc-border);
}
.card-head__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.card-head__hint {
  margin-left: auto;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  font-variant-numeric: tabular-nums;
}

/* ============ 状态 chips（设计稿：filterbar 内带计数的胶囊按钮） ============ */
.filterbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  padding: var(--bc-space-3) var(--bc-space-4) 0;
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

/* ============ 表格细节 ============ */
.table-scroll {
  overflow-x: auto;
}
.arg-cell {
  display: inline-block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
.reviewer-cell {
  color: var(--bc-text-2);
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
  margin-top: auto;
}
.table-foot__num {
  font-variant-numeric: tabular-nums;
}

/* ============ 授权管理侧卡：紧凑台账列表 ============ */
.grant-list {
  display: flex;
  flex-direction: column;
  max-height: 520px;
  overflow-y: auto;
}
.grant-row {
  padding: var(--bc-space-3) var(--bc-space-4);
  border-bottom: 1px solid var(--bc-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
  transition: background 0.15s var(--bc-ease, ease-out);
}
.grant-row:last-child {
  border-bottom: 0;
}
.grant-row:hover {
  background: var(--bc-bg-sunken);
}
.grant-row__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-2);
}
.grant-row__tool {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
}
.grant-row__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.grant-row__ops {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 24px;
}
.grant-row.is-revoked {
  opacity: 0.55;
}

/* ============ 对话框细节（沿用） ============ */
.review-target {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.grant-hint {
  margin-top: var(--bc-space-1);
  line-height: 1.5;
}
.json-block {
  margin: 0;
  padding: var(--bc-space-2) var(--bc-space-3);
  background: var(--bc-gray-2);
  border-radius: var(--bc-radius-sm);
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: var(--bc-text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow: auto;
}

@media (max-width: 1100px) {
  .row-2-1 {
    grid-template-columns: 1fr;
  }
}
</style>
