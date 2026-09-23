<template>
  <div class="bc-page">
    <PageHeader title="仪表盘" :description="`平台治理与运行概览 · 更新于 ${loadedAt}`">
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :cards="4" />

    <template v-else>
      <!-- ============ 第一层级：KPI 行（设计稿：标签 + mono 大数 + 真实脚注） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">用户总数</div>
          <div class="kpi__value mono">{{ fmtNum(usersDisplay) }}</div>
          <div class="kpi__meta">{{ kpi.activeUsers }} 个启用</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">数字员工</div>
          <div class="kpi__value mono">{{ fmtNum(agentsDisplay) }}</div>
          <div class="kpi__meta">{{ agentBotCount }} 个专属连接 · 已授权 {{ kpi.distinctTools }} 项工具</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">今日会话</div>
          <div class="kpi__value mono">{{ fmtNum(todayConvosDisplay) }}</div>
          <div class="kpi__meta">覆盖 {{ kpi.todayUsers }} 个用户</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">待审批</div>
          <div class="kpi__value mono">{{ fmtNum(pendingDisplay) }}</div>
          <div class="kpi__meta">
            <span v-if="kpi.pending" class="kpi__pill kpi__pill--warn">需处理</span>
            <span v-else class="kpi__pill kpi__pill--ok">无积压</span>
            <span>敏感操作人工批准</span>
          </div>
        </div>
      </div>

      <!-- ============ 第二层级：会话趋势 + 治理概览 ============ -->
      <div class="row-2-1">
        <div class="bc-panel dash-card">
          <div class="dash-card__head">
            <span class="dash-card__title">会话趋势</span>
            <div class="segmented" role="tablist" aria-label="时间范围">
              <button
                v-for="r in RANGES"
                :key="r.value"
                type="button"
                :class="['segmented__btn', { 'is-on': range === r.value }]"
                role="tab"
                :aria-selected="range === r.value"
                @click="range = r.value"
              >
                {{ r.label }}
              </button>
            </div>
          </div>
          <div class="dash-card__body chart-wrap">
            <template v-if="trend.values.length">
              <svg
                class="chart"
                viewBox="0 0 640 200"
                preserveAspectRatio="none"
                role="img"
                aria-label="会话趋势图"
              >
                <line
                  v-for="y in gridY"
                  :key="y"
                  x1="8"
                  :y1="y"
                  x2="632"
                  :y2="y"
                  class="chart__grid"
                />
                <path :d="areaPath" class="chart__area" />
                <path :d="linePath" class="chart__line" />
                <circle :cx="lastPoint.x" :cy="lastPoint.y" r="3.2" class="chart__dot" />
              </svg>
              <div class="chart-axis mono">
                <span
                  v-for="(t, i) in axisTicks"
                  :key="i"
                  :style="{ left: t.pct + '%' }"
                  :class="{ 'is-edge-first': i === 0, 'is-edge-last': i === axisTicks.length - 1 }"
                >{{ t.text }}</span>
              </div>
            </template>
            <div v-else class="chart-empty bc-muted">暂无会话数据</div>
          </div>
        </div>

        <div class="bc-panel dash-card">
          <div class="dash-card__head">
            <span class="dash-card__title">治理概览</span>
            <span class="dash-card__hint">近 {{ auditSampleSize }} 条样本</span>
          </div>
          <div class="gov-list">
            <div class="gov-row">
              审计记录 · 近 24h
              <span class="gov-row__v mono">{{ fmtNum(kpi.audit24h) }}</span>
            </div>
            <div class="gov-row">
              已拒绝访问
              <span class="gov-row__v mono">{{ fmtNum(kpi.auditDenied) }}</span>
            </div>
            <div class="gov-row">
              执行错误
              <span class="gov-row__v mono">{{ fmtNum(kpi.auditError) }}</span>
            </div>
            <div class="gov-row">
              全量留痕
              <el-tag type="success" size="small" class="gov-row__tag">已开启</el-tag>
            </div>
          </div>
          <div class="audit-feed">
            <div class="audit-feed__h">最近审计</div>
            <div v-for="log in auditFeed" :key="log.id ?? log.created_at" class="audit-feed__item">
              <time class="mono">{{ fmtClock(log.created_at) }}</time>
              <span class="audit-feed__txt">
                {{ log.user_name || '未知用户' }} · {{ decisionLabel(log.decision) }} ·
                {{ log.question || log.detail || '—' }}
              </span>
            </div>
          </div>
          <div class="dash-card__foot">
            <span class="bc-muted bc-text-xs">全量留痕可回溯</span>
            <el-button text size="small" @click="$router.push({ name: 'supervision', query: { tab: 'audit' } })">
              查看全部<el-icon class="el-icon--right"><ArrowRight /></el-icon>
            </el-button>
          </div>
        </div>
      </div>

      <!-- ============ 第三层级：最近会话 + 知识库 ============ -->
      <div class="row-2-1">
        <div class="bc-panel dash-card">
          <div class="dash-card__head">
            <span class="dash-card__title">最近会话</span>
            <el-button text size="small" @click="$router.push({ name: 'runtime', query: { tab: 'conversations' } })">
              查看全部会话<el-icon class="el-icon--right"><ArrowRight /></el-icon>
            </el-button>
          </div>
          <el-table v-if="recentConvos.length" :data="recentConvos" class="conv-table">
            <el-table-column label="会话标识" min-width="170">
              <template #default="{ row }">
                <span class="mono bc-text-sm">{{ shortKey(row.session_key) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="渠道" width="104">
              <template #default="{ row }">
                <el-tag :type="channelType(row.channel)" size="small">
                  {{ channelLabel(row.channel) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用户" min-width="110">
              <template #default="{ row }">
                <span class="bc-strong">{{ userName(row.user_id) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="消息数" width="84" align="right">
              <template #default="{ row }">
                <span class="mono bc-strong">{{ row.message_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag :type="isLive(row) ? 'success' : 'info'" size="small">
                  {{ isLive(row) ? '活跃' : '已结束' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近活跃" width="96" align="right">
              <template #default="{ row }">
                <span class="mono bc-muted bc-text-sm">{{ fmtClock(row.last_message_at) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="chart-empty bc-muted">暂无会话</div>
        </div>

        <div class="bc-panel dash-card">
          <div class="dash-card__head">
            <span class="dash-card__title">知识库</span>
            <el-button text size="small" @click="$router.push({ name: 'knowledge-hub', query: { tab: 'bases' } })">
              管理知识库<el-icon class="el-icon--right"><ArrowRight /></el-icon>
            </el-button>
          </div>
          <div v-if="kbRows.length" class="kb-list">
            <div v-for="kb in kbRows" :key="kb.name" class="kb-row">
              <div class="kb-row__top">
                <span class="bc-strong">{{ kb.name }}</span>
                <span class="mono bc-muted bc-text-xs">{{ fmtNum(kb.docs) }} 篇</span>
              </div>
              <div class="kb-row__bar">
                <i :style="{ width: kb.percent + '%' }" />
              </div>
            </div>
          </div>
          <div v-else class="chart-empty bc-muted">暂无知识库</div>
          <div class="dash-card__foot">
            <span class="bc-muted bc-text-xs">共 {{ fmtNum(totalDocs) }} 篇文档已索引</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Refresh, ArrowRight } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import { useCountUp } from '../composables/useCountUp'

/* ---------------- 状态 ---------------- */
const loading = ref(true)
const loadedAt = ref('—')
const users = ref([])
const roles = ref([])
const agents = ref([]) // 数字员工实例（KPI「数字员工」取此数，而非角色数）
const conversations = ref([])
const auditItems = ref([])
const pendingApprovals = ref([])
const kbStats = ref([]) // [{ name, docs }]
const range = ref('today')

const RANGES = [
  { value: 'today', label: '今日' },
  { value: 'week', label: '近 7 天' },
  { value: 'month', label: '近 30 天' },
]
const AUDIT_SAMPLE = 100
const auditSampleSize = ref(AUDIT_SAMPLE)

/* ---------------- KPI ---------------- */
const isToday = (t) => t && new Date(t).toDateString() === new Date().toDateString()

const kpi = computed(() => {
  const tools = new Set()
  for (const r of roles.value) for (const t of r.config?.allowed_tools || []) tools.add(t)
  const todayConvs = conversations.value.filter((c) => isToday(c.last_message_at))
  const dayAgo = Date.now() - 24 * 3600 * 1000
  const sample = auditItems.value
  return {
    activeUsers: users.value.filter((u) => u.is_active).length,
    distinctTools: tools.size,
    todayConvos: todayConvs.length,
    todayUsers: new Set(todayConvs.map((c) => c.user_id)).size,
    pending: pendingApprovals.value.filter((a) => a.status === 'pending').length,
    audit24h: sample.filter((l) => new Date(l.created_at) >= dayAgo).length,
    auditDenied: sample.filter((l) => l.decision?.startsWith('denied')).length,
    auditError: sample.filter((l) => l.decision === 'error').length,
  }
})

// 数字滚动（尊重 prefers-reduced-motion，组合式函数内置）
const usersDisplay = useCountUp(computed(() => users.value.length))
const agentsDisplay = useCountUp(computed(() => agents.value.length))
// 专属连接数：多渠道实例按连接（渠道）计
const agentBotCount = computed(() =>
  agents.value.reduce((n, a) => n + (a.im_channels?.length || (a.has_im_config ? 1 : 0)), 0),
)
const todayConvosDisplay = useCountUp(computed(() => kpi.value.todayConvos))
const pendingDisplay = useCountUp(computed(() => kpi.value.pending))

/* ---------------- 会话趋势（真实数据分桶） ---------------- */
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())

const trend = computed(() => {
  const convs = conversations.value.filter((c) => c.last_message_at)
  const labels = []
  const values = []
  const now = new Date()
  if (range.value === 'today') {
    // 每 2 小时一桶（00–24），值 = 该时段内最后活跃的会话数
    for (let h = 0; h < 24; h += 2) {
      labels.push(String(h).padStart(2, '0'))
      values.push(0)
    }
    for (const c of convs) {
      const t = new Date(c.last_message_at)
      if (t.toDateString() === now.toDateString()) {
        values[Math.min(Math.floor(t.getHours() / 2), 11)]++
      }
    }
  } else {
    const days = range.value === 'week' ? 7 : 30
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(now)
      d.setDate(d.getDate() - i)
      labels.push(`${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`)
      values.push(0)
    }
    const todayStart = startOfDay(now).getTime()
    for (const c of convs) {
      const diff = Math.floor((todayStart - startOfDay(new Date(c.last_message_at)).getTime()) / 86400000)
      if (diff >= 0 && diff < days) values[days - 1 - diff]++
    }
  }
  return { labels, values }
})

// SVG 路径（640×200 画布，8px 内边距，底部留 14px 给横轴）
const CHART_W = 640
const CHART_H = 200
const CHART_PAD = 8

const chartGeom = computed(() => {
  const { values } = trend.value
  if (!values.length) return { line: '', area: '', grid: [], last: { x: 0, y: 0 } }
  const max = Math.max(...values, 1)
  const stepX = (CHART_W - CHART_PAD * 2) / (values.length - 1)
  const X = (i) => CHART_PAD + i * stepX
  const Y = (v) => CHART_H - 14 - (v / max) * (CHART_H - 34)
  const line = values.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join('')
  const area = `${line} L${X(values.length - 1).toFixed(1)} ${(CHART_H - 14).toFixed(1)} L${CHART_PAD} ${(CHART_H - 14).toFixed(1)} Z`
  const grid = [0.25, 0.5, 0.75].map((g) => (CHART_H - 14 - g * (CHART_H - 34)).toFixed(1))
  return { line, area, grid, last: { x: X(values.length - 1), y: Y(values[values.length - 1]) } }
})
const gridY = computed(() => chartGeom.value.grid)
const linePath = computed(() => chartGeom.value.line)
const areaPath = computed(() => chartGeom.value.area)
const lastPoint = computed(() => chartGeom.value.last)

// 横轴标签抽稀：今日取 6 个、7 天全显、30 天每 5 个 + 今天。
// 标签的 left 与折线数据点同源计算（同一 X 坐标公式），避免 space-between
// 均匀铺开导致的标签-数据桶错位。
const axisTicks = computed(() => {
  const { labels } = trend.value
  const n = labels.length
  if (!n) return []
  let idxs
  if (range.value === 'today') idxs = labels.map((_, i) => i).filter((i) => i % 2 === 0)
  else if (range.value === 'week') idxs = labels.map((_, i) => i)
  else idxs = labels.map((_, i) => i).filter((i) => i % 5 === 0 || i === n - 1)
  return idxs.map((i) => {
    const x = CHART_PAD + (i * (CHART_W - CHART_PAD * 2)) / (n - 1 || 1)
    const text = range.value !== 'today' && i === n - 1 ? '今天' : labels[i]
    return { text, pct: (x / CHART_W) * 100 }
  })
})

/* ---------------- 列表派生 ---------------- */
const recentConvos = computed(() =>
  [...conversations.value]
    .filter((c) => c.last_message_at)
    .sort((a, b) => new Date(b.last_message_at) - new Date(a.last_message_at))
    .slice(0, 6),
)
const auditFeed = computed(() => auditItems.value.slice(0, 3))

const kbRows = computed(() => {
  const max = Math.max(...kbStats.value.map((k) => k.docs), 1)
  return [...kbStats.value].sort((a, b) => b.docs - a.docs).map((k) => ({ ...k, percent: Math.round((k.docs / max) * 100) }))
})
const totalDocs = computed(() => kbStats.value.reduce((a, k) => a + k.docs, 0))

/* ---------------- 展示辅助 ---------------- */
const userName = (id) => {
  const u = users.value.find((u) => u.id === id)
  return (u && (u.display_name || u.name)) || `#${id}`
}
const isLive = (row) => Date.now() - new Date(row.last_message_at).getTime() < 15 * 60 * 1000
const shortKey = (key) => (key?.length > 12 ? `${key.slice(0, 10)}…` : key || '—')
const channelType = (ch) => ({ api: 'info', wecom: 'success', feishu: 'primary' }[ch] || 'info')
const channelLabel = (ch) => ({ api: '对话测试', wecom: '企业微信', feishu: '飞书' }[ch] || ch)
const decisionLabel = (d) =>
  ({ answered: '已回答', denied_no_access: '无权限', error: '错误' }[d] || (d?.startsWith('denied') ? '已拒绝' : d || '未知'))
const fmtNum = (n) => (typeof n === 'number' ? Math.round(n).toLocaleString('zh-CN') : '—')

function fmtClock(s) {
  if (!s) return '—'
  const t = new Date(s)
  const sameDay = t.toDateString() === new Date().toDateString()
  const hm = t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return sameDay ? hm : `${t.getMonth() + 1}-${t.getDate()} ${hm}`
}

/* ---------------- 加载（并行，单项失败不阻塞） ---------------- */
async function load() {
  loading.value = true
  try {
    const safe = (p, fallback) => p.catch(() => fallback)
    const [userList, roleList, agentList, convList, audit, approvals, kbs] = await Promise.all([
      safe(admin.listUsers(), []),
      safe(admin.listRoles(), []),
      safe(admin.listAgents(), []),
      safe(admin.listConversations(), []),
      safe(admin.listAudit({ page: 1, page_size: AUDIT_SAMPLE }), { items: [] }),
      safe(admin.listApprovals('pending'), []),
      safe(admin.listKnowledgeBases(), []),
    ])
    users.value = userList
    roles.value = roleList
    agents.value = agentList
    conversations.value = convList
    auditItems.value = audit.items || []
    auditSampleSize.value = Math.min(auditItems.value.length, AUDIT_SAMPLE)
    pendingApprovals.value = approvals

    // 每个 KB 的文档量（失败按 0 计，不阻塞看板）
    kbStats.value = await safe(
      Promise.all(kbs.map((kb) => admin.listDocuments(kb.id).then((d) => ({ name: kb.name, docs: d.length })))),
      kbs.map((kb) => ({ name: kb.name, docs: 0 })),
    )

    loadedAt.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* ============ KPI 行（设计稿：无强调竖线，label + mono 大数 + 脚注） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--bc-space-4);
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

/* ============ 主行布局：2 : 1 ============ */
.row-2-1 {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--bc-space-4);
  margin-top: var(--bc-space-4);
  align-items: stretch;
}

/* ============ 卡片骨架（bc-panel 之上补头部/底部） ============ */
.dash-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.dash-card__head {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  padding: var(--bc-space-3) var(--bc-space-4);
  border-bottom: 1px solid var(--bc-border);
}
.dash-card__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  letter-spacing: 0.01em;
}
.dash-card__hint {
  margin-left: auto;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.dash-card__head :deep(.el-button + .el-button) {
  margin-left: 0;
}
.dash-card__head :deep(.el-button) {
  margin-left: auto;
}
.dash-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-top: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-4);
  margin-top: auto;
}

/* ============ 分段控件（设计稿：sunken 容器 + 浮起激活态） ============ */
.segmented {
  margin-left: auto;
  display: inline-flex;
  background: var(--bc-bg-sunken);
  border-radius: var(--bc-radius-md);
  padding: 2px;
}
.segmented__btn {
  border: 0;
  background: transparent;
  font: 500 var(--bc-text-xs) / 1 inherit;
  color: var(--bc-text-2);
  padding: 6px 10px;
  border-radius: var(--bc-radius-sm);
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
  font-family: inherit;
}
.segmented__btn.is-on {
  background: var(--bc-bg-surface);
  color: var(--bc-text-1);
  box-shadow: var(--bc-shadow-xs);
}

/* ============ 趋势图 ============ */
.chart-wrap {
  padding: var(--bc-space-4);
}
.chart {
  width: 100%;
  height: auto;
  display: block;
}
.chart__grid {
  stroke: var(--bc-bg-sunken);
  stroke-width: 1;
}
.chart__area {
  fill: color-mix(in srgb, var(--bc-primary-6) 5%, transparent);
}
.chart__line {
  fill: none;
  stroke: var(--bc-primary-6);
  stroke-width: 1.6;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.chart__dot {
  fill: var(--bc-primary-6);
}
.chart-axis {
  position: relative;
  height: 20px;
  padding: var(--bc-space-2) 0 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  letter-spacing: 0.02em;
}
.chart-axis span {
  position: absolute;
  transform: translateX(-50%);
  white-space: nowrap;
}
/* 首尾标签对齐画布边缘，避免 translate 后溢出容器 */
.chart-axis span.is-edge-first {
  transform: none;
}
.chart-axis span.is-edge-last {
  transform: translateX(-100%);
}
.chart-empty {
  padding: var(--bc-space-12) var(--bc-space-4);
  text-align: center;
  font-size: var(--bc-text-sm);
}

/* ============ 治理概览 ============ */
.gov-list {
  padding: var(--bc-space-1) var(--bc-space-4) var(--bc-space-2);
}
.gov-row {
  display: flex;
  align-items: center;
  padding: 9px 0;
  border-bottom: 1px solid var(--bc-bg-sunken);
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
}
.gov-row:last-child {
  border-bottom: 0;
}
.gov-row__v {
  margin-left: auto;
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  font-variant-numeric: tabular-nums;
}
.gov-row__tag {
  margin-left: auto;
}

/* ============ 最近审计 feed ============ */
.audit-feed {
  border-top: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-4) var(--bc-space-3);
}
.audit-feed__h {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  padding: 4px 0 6px;
  letter-spacing: 0.02em;
}
.audit-feed__item {
  display: flex;
  gap: var(--bc-space-2);
  padding: 5px 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-2);
  line-height: 1.5;
}
.audit-feed__item time {
  font-family: var(--bc-font-mono);
  color: var(--bc-text-3);
  flex: none;
}
.audit-feed__txt {
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* ============ 知识库 ============ */
.kb-list {
  padding: var(--bc-space-2) var(--bc-space-4) var(--bc-space-3);
}
.kb-row {
  padding: var(--bc-space-2) 0;
  border-bottom: 1px solid var(--bc-bg-sunken);
}
.kb-row:last-child {
  border-bottom: 0;
}
.kb-row__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-sm);
}
.kb-row__bar {
  margin-top: var(--bc-space-2);
  height: 3px;
  border-radius: 2px;
  background: var(--bc-bg-sunken);
  overflow: hidden;
}
.kb-row__bar i {
  display: block;
  height: 100%;
  background: var(--bc-primary-6);
  border-radius: 2px;
  opacity: 0.85;
}

/* ============ 响应式 ============ */
@media (max-width: 1280px) {
  .kpis {
    grid-template-columns: repeat(2, 1fr);
  }
  .row-2-1 {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 720px) {
  .kpis {
    grid-template-columns: 1fr;
  }
}
</style>
