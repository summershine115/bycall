<template>
  <div class="bc-page">
    <PageHeader title="审计日志" description="全链路留痕：谁在何时问了什么，被放行还是拒绝，以及原因。">
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :cards="3" />

    <template v-else>
      <!-- ============ KPI 行（设计稿：标签 + mono 大数 + 真实脚注，基于近 N 条样本） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">审计条目</div>
          <div class="kpi__value mono">{{ fmtNum(sample.length) }}</div>
          <div class="kpi__meta">近 {{ sampleSize }} 条样本 · 全量留痕</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">已回答 answered</div>
          <div class="kpi__value mono">{{ fmtNum(sampleAnswered) }}</div>
          <div class="kpi__meta">占样本 {{ pct(sampleAnswered) }}</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">拒绝 denied</div>
          <div class="kpi__value mono">{{ fmtNum(sampleDenied) }}</div>
          <div class="kpi__meta">占样本 {{ pct(sampleDenied) }} · 含策略命中</div>
        </div>
      </div>

      <!-- ============ 决策明细（设计稿：筛选栏 + 表格 + 分页） ============ -->
      <div class="bc-panel">
        <div class="card-head">
          <span class="card-head__title">决策明细</span>
          <span class="mono card-head__hint">共 {{ total }} 条</span>
        </div>

        <div class="filterbar">
          <el-select
            v-model="userFilter"
            placeholder="按用户筛选"
            clearable
            filterable
            class="filterbar__select"
            @change="resetAndLoad"
          >
            <el-option label="全部用户" value="" />
            <el-option
              v-for="user in users"
              :key="user.id"
              :label="user.display_name || user.name"
              :value="user.id"
            />
          </el-select>
          <el-select
            v-model="decisionFilter"
            placeholder="按决策筛选"
            clearable
            class="filterbar__select"
            @change="resetAndLoad"
          >
            <el-option label="全部决策" value="" />
            <el-option label="已回答 answered" value="answered" />
            <el-option label="拒绝(无权限) denied_no_access" value="denied_no_access" />
            <el-option label="拒绝 denied" value="denied" />
            <el-option label="错误 error" value="error" />
          </el-select>
          <el-input
            v-model="keyword"
            class="filterbar__search"
            placeholder="按问题关键词搜索（当前页）"
            clearable
            :prefix-icon="Search"
          />
          <span class="filterbar__count">
            当前页匹配 <b>{{ filtered.length }}</b> / {{ logs.length }} 条
          </span>
        </div>

        <EmptyState
          v-if="!filtered.length"
          icon="List"
          :title="userFilter || decisionFilter || keyword ? '当前筛选条件下暂无记录' : '暂无审计记录'"
          description="数字员工的每一次问答都会在此留痕，包括被策略拒绝的请求。"
        >
          <el-button v-if="userFilter || decisionFilter || keyword" @click="clearFilter">清除筛选</el-button>
        </EmptyState>

        <el-table v-else :data="filtered">
          <el-table-column label="时间" width="92">
            <template #default="{ row }">
              <span class="mono bc-text-sm">{{ fmtClock(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="用户" width="104">
            <template #default="{ row }">
              <span class="bc-strong">{{ row.user_name || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="问题" min-width="220">
            <template #default="{ row }">
              <span class="q-cell" :title="row.question">{{ row.question || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="决策" width="128">
            <template #default="{ row }">
              <el-tag v-if="row.decision" :type="decisionType(row.decision)" size="small">
                {{ decisionLabel(row.decision) }}
              </el-tag>
              <span v-else class="bc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="回答 / 详情" min-width="260">
            <template #default="{ row }">
              <div v-if="row.answer || row.detail" class="a-cell" :title="row.answer || row.detail">
                {{ row.answer || row.detail }}
              </div>
              <span v-else class="bc-muted">—</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="table-foot">
          <span>全量留痕可回溯 · 按时间倒序</span>
          <el-pagination
            v-if="total"
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[20, 50, 100]"
            layout="total, sizes, prev, pager, next, jumper"
            class="table-foot__pager"
            @current-change="load"
            @size-change="resetAndLoad"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

/* ---------------- 状态 ---------------- */
const loading = ref(true)
const logs = ref([])
const users = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const userFilter = ref('')
const decisionFilter = ref('')
const keyword = ref('')

const SAMPLE_SIZE = 100
const sampleSize = ref(SAMPLE_SIZE)
const sample = ref([])
let loadGeneration = 0

/* ---------------- KPI（基于最近样本，诚实标注样本量） ---------------- */
const sampleAnswered = computed(() => sample.value.filter((l) => l.decision === 'answered').length)
const sampleDenied = computed(() => sample.value.filter((l) => l.decision?.startsWith('denied')).length)
const pct = (n) => (sample.value.length ? `${Math.round((n / sample.value.length) * 100)}%` : '—')
const fmtNum = (n) => (typeof n === 'number' ? n.toLocaleString('zh-CN') : '—')

/* ---------------- 关键词过滤（当前页内即时生效；用户/决策为服务端过滤） ---------------- */
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return logs.value
  return logs.value.filter((l) => `${l.question || ''} ${l.answer || ''} ${l.detail || ''} ${l.user_name || ''}`.toLowerCase().includes(kw))
})

function clearFilter() {
  userFilter.value = ''
  decisionFilter.value = ''
  keyword.value = ''
  resetAndLoad()
}

/* ---------------- 展示辅助 ---------------- */
function decisionType(d) {
  if (d === 'answered') return 'success'
  if (d?.startsWith('denied')) return 'danger'
  if (d === 'error') return 'warning'
  return 'info'
}
function decisionLabel(d) {
  if (d === 'answered') return '已回答'
  if (d === 'denied_no_access') return '拒绝 · 无权限'
  if (d === 'denied') return '拒绝'
  if (d === 'error') return '错误'
  return d || '—'
}

function fmtClock(s) {
  if (!s) return '—'
  const t = new Date(s)
  const sameDay = t.toDateString() === new Date().toDateString()
  const hm = t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  return sameDay ? hm : `${t.getMonth() + 1}-${String(t.getDate()).padStart(2, '0')} ${hm}`
}

/* ---------------- 数据加载 ---------------- */
async function load() {
  const generation = ++loadGeneration
  loading.value = true
  try {
    const result = await admin.listAudit({
      page: page.value,
      page_size: pageSize.value,
      ...(userFilter.value ? { user_id: userFilter.value } : {}),
      ...(decisionFilter.value ? { decision: decisionFilter.value } : {}),
    })
    if (generation !== loadGeneration) return
    logs.value = result.items ?? []
    total.value = result.total ?? 0
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function loadSample() {
  try {
    const arr = await admin.listAudit({ limit: SAMPLE_SIZE })
    sample.value = Array.isArray(arr) ? arr : []
    sampleSize.value = Math.min(sample.value.length, SAMPLE_SIZE)
  } catch {
    sample.value = []
  }
}

function resetAndLoad() {
  page.value = 1
  load()
}

onMounted(async () => {
  const [userResult] = await Promise.allSettled([admin.listUsers(), load(), loadSample()])
  if (userResult.status === 'fulfilled') users.value = userResult.value
})
</script>

<style scoped>
/* ============ KPI 行（三卡，与仪表盘同一视觉语言） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
.kpi {
  padding: var(--bc-space-4);
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
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}

/* ============ 卡头 + 筛选栏 ============ */
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
.filterbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  padding: var(--bc-space-3) var(--bc-space-4) 0;
}
.filterbar__select {
  width: 200px;
}
.filterbar__search {
  width: 240px;
}
.filterbar__count {
  margin-left: auto;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.filterbar__count b {
  color: var(--bc-text-2);
  font-weight: var(--bc-weight-medium);
  font-variant-numeric: tabular-nums;
}

/* ============ 问题 / 回答列：单行截断，悬停看全文 ============ */
.q-cell {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  display: block;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.a-cell {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ============ 表脚：左信息 + 右分页 ============ */
.table-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  flex-wrap: wrap;
  border-top: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-4);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.table-foot__pager {
  margin-left: auto;
}

@media (max-width: 900px) {
  .kpis {
    grid-template-columns: 1fr;
  }
  .filterbar__select,
  .filterbar__search {
    width: 100%;
  }
  .filterbar__count {
    margin-left: 0;
    width: 100%;
  }
}
</style>
