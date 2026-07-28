<template>
  <div class="bc-page">
    <PageHeader
      title="审计日志"
      description="全链路留痕：谁在何时问了什么，被放行还是拒绝，以及原因。"
    >
      <el-select v-model="decisionFilter" placeholder="按决策筛选" clearable style="width: 220px">
        <el-option label="全部决策" value="" />
        <el-option label="已回答 answered" value="answered" />
        <el-option label="拒绝(无权限) denied_no_access" value="denied_no_access" />
        <el-option label="拒绝 denied" value="denied" />
        <el-option label="错误 error" value="error" />
      </el-select>
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="8" />

    <div v-else class="bc-panel">
      <EmptyState
        v-if="!filtered.length"
        icon="List"
        :title="decisionFilter ? '该决策类型下暂无记录' : '暂无审计记录'"
        description="数字员工的每一次问答都会在此留痕，包括被策略拒绝的请求。"
      />

      <el-table v-else :data="filtered">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">
            <span class="bc-text-sm">{{ fmtTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="user_name" label="用户" width="110">
          <template #default="{ row }">
            <span class="bc-strong">{{ row.user_name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="决策" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.decision" :type="decisionType(row.decision)" size="small">{{ row.decision }}</el-tag>
            <span v-else class="bc-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="question" label="问题" min-width="200">
          <template #default="{ row }">
            <span class="bc-text-sm">{{ row.question || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="回答 / 详情" min-width="260">
          <template #default="{ row }">
            <div v-if="row.answer || row.detail" class="bc-cell-clamp">{{ row.answer || row.detail }}</div>
            <span v-else class="bc-empty">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime as fmtTime } from '../utils/format'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const logs = ref([])
const decisionFilter = ref('')

const filtered = computed(() =>
  decisionFilter.value ? logs.value.filter((l) => l.decision === decisionFilter.value) : logs.value,
)

async function load() {
  loading.value = true
  try {
    logs.value = await admin.listAudit(200)
  } finally {
    loading.value = false
  }
}

function decisionType(d) {
  if (d === 'answered') return 'success'
  if (d?.startsWith('denied')) return 'danger'
  if (d === 'error') return 'warning'
  return 'info'
}

onMounted(load)
</script>
