<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-select v-model="decisionFilter" placeholder="按决策筛选" clearable style="width: 180px">
        <el-option label="全部" value="" />
        <el-option label="已回答 answered" value="answered" />
        <el-option label="拒绝(无权限) denied_no_access" value="denied_no_access" />
        <el-option label="拒绝 denied" value="denied" />
        <el-option label="错误 error" value="error" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table :data="filtered" border>
      <el-table-column prop="created_at" label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="user_name" label="用户" width="100" />
      <el-table-column label="决策" width="170">
        <template #default="{ row }">
          <el-tag :type="decisionType(row.decision)">{{ row.decision }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="question" label="问题" min-width="200" />
      <el-table-column prop="detail" label="详情" min-width="200" />
      <el-table-column prop="answer" label="回答/结果" min-width="240">
        <template #default="{ row }">
          <div class="answer-cell">{{ row.answer || row.detail }}</div>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { admin } from '../api'

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

function fmtTime(s) {
  if (!s) return ''
  return new Date(s).toLocaleString('zh-CN')
}

function decisionType(d) {
  if (d === 'answered') return 'success'
  if (d?.startsWith('denied')) return 'danger'
  if (d === 'error') return 'warning'
  return 'info'
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
}
.answer-cell {
  white-space: pre-wrap;
  max-height: 100px;
  overflow: hidden;
  font-size: 13px;
  color: #606266;
}
</style>
