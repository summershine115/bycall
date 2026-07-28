<template>
  <div class="bc-page">
    <PageHeader
      title="审批队列"
      description="人在环路审批：敏感工具调用须人工批准后执行，结果与备注全程留痕。"
    >
      <el-select
        v-model="statusFilter"
        placeholder="按状态筛选"
        style="width: 200px"
        @change="load"
      >
        <el-option label="全部状态" value="" />
        <el-option label="待审批" value="pending" />
        <el-option label="已执行" value="executed" />
        <el-option label="已拒绝" value="rejected" />
        <el-option label="失败" value="failed" />
      </el-select>
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="8" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!approvals.length"
        icon="DocumentChecked"
        :title="statusFilter ? '该状态下暂无审批请求' : '暂无审批请求'"
        description="数字员工调用敏感工具时会产生审批请求，在此批准或拒绝后才会真正执行。"
      />

      <el-table v-else :data="approvals">
        <el-table-column prop="id" label="ID" width="70" align="center" />
        <el-table-column label="用户" width="120">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.user_name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="工具" min-width="200">
          <template #default="{ row }">
            <span class="mono bc-text-xs">{{ row.tool_name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="参数" min-width="200">
          <template #default="{ row }">
            <span v-if="hasArgs(row.tool_args)" class="mono bc-text-xs bc-muted">
              {{ truncateArgs(row.tool_args) }}
            </span>
            <span v-else class="bc-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="审批人" width="120">
          <template #default="{ row }">
            <span class="bc-text-sm">{{ row.reviewed_by || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="bc-text-sm">{{ fmtTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <template v-if="row.status === 'pending'">
              <el-button size="small" type="primary" @click="openReview(row, 'approve')">批准</el-button>
              <el-button size="small" type="danger" plain @click="openReview(row, 'reject')">拒绝</el-button>
            </template>
            <el-button v-else size="small" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
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
        <el-descriptions-item label="ID">{{ detailRow.id }}</el-descriptions-item>
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
          <span v-else class="bc-empty">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="审批人">{{ detailRow.reviewed_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="审批时间">{{ fmtTime(detailRow.reviewed_at) || '—' }}</el-descriptions-item>
        <el-descriptions-item label="执行时间">{{ fmtTime(detailRow.executed_at) || '—' }}</el-descriptions-item>
        <el-descriptions-item label="审批备注">{{ detailRow.review_note || '—' }}</el-descriptions-item>
        <el-descriptions-item label="执行结果">
          <pre v-if="detailRow.result_text" class="json-block">{{ detailRow.result_text }}</pre>
          <span v-else class="bc-empty">—</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime as fmtTime } from '../utils/format'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const approvals = ref([])
const loading = ref(false)
const statusFilter = ref('')

// 审批对话框状态
const reviewVisible = ref(false)
const reviewAction = ref('approve') // approve | reject
const reviewRow = ref(null)
const reviewNote = ref('')
const reviewing = ref(false)

// 详情对话框状态
const detailVisible = ref(false)
const detailRow = ref(null)

async function load() {
  loading.value = true
  try {
    approvals.value = await admin.listApprovals(statusFilter.value)
  } finally {
    loading.value = false
  }
}

onMounted(load)

// —— 状态展示映射：pending=待审批 ... failed=失败 ——
function statusLabel(s) {
  return { pending: '待审批', executed: '已执行', rejected: '已拒绝', failed: '失败' }[s] || s
}
// 状态标签颜色：pending=warning executed=success rejected=danger failed=info
function statusType(s) {
  return { pending: 'warning', executed: 'success', rejected: 'danger', failed: 'info' }[s] || 'info'
}

// —— 工具参数展示 ——
function hasArgs(args) {
  return args != null && typeof args === 'object' && Object.keys(args).length > 0
}
function truncateArgs(args) {
  const s = JSON.stringify(args)
  return s.length > 40 ? s.slice(0, 40) + '…' : s
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
  reviewVisible.value = true
}

async function onReview() {
  reviewing.value = true
  try {
    const row = reviewRow.value
    if (reviewAction.value === 'approve') {
      // 批准会触发工具执行；后端可能返回 executed 或 failed（工具不存在时）
      const result = await admin.approveApproval(row.id, reviewNote.value)
      if (result.status === 'failed') {
        ElMessage.warning('已批准，但工具执行失败')
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

// —— 详情 ——
function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}
</script>

<style scoped>
.review-target {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
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
</style>
