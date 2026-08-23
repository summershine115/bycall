<template>
  <div class="bc-page">
    <PageHeader
      title="运营指标"
      description="五维运营指标：延迟 / 成本 / 人工接管率 / 失败率 / 重复执行率。数据与 /metrics 同源（Prometheus），是试点阶段业务效果看板（T3.4）的底子。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :cards="5" />

    <template v-else>
      <!-- ============ KPI 行（设计稿：label + mono 大数 + 单位小字 + 真实脚注） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">平均轮次延迟</div>
          <div class="kpi__value mono">
            {{ metrics.latency.avg_seconds.toFixed(2) }}<small>s</small>
          </div>
          <div class="kpi__meta">累计 {{ num(metrics.latency.turn_count) }} 轮对话</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">累计 LLM 成本</div>
          <div class="kpi__value mono">
            <small>$</small>{{ metrics.cost.total_usd.toFixed(4) }}
          </div>
          <div class="kpi__meta">
            输入 {{ num(metrics.cost.input_tokens) }} / 输出 {{ num(metrics.cost.output_tokens) }} token
          </div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">人工接管率</div>
          <div class="kpi__value mono">{{ pct(metrics.human_takeover.rate) }}<small>%</small></div>
          <div class="kpi__meta">
            {{ num(metrics.human_takeover.requested) }} 请求 · {{ metrics.human_takeover.approved }} 通过 ·
            {{ metrics.human_takeover.rejected }} 拒绝
          </div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">失败率</div>
          <div class="kpi__value mono">{{ pct(metrics.failure.rate) }}<small>%</small></div>
          <div class="kpi__meta">{{ num(metrics.failure.failed_turns) }} 失败 / {{ num(metrics.failure.total_turns) }} 轮</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">重复执行率</div>
          <div class="kpi__value mono">{{ pct(metrics.duplicate_execution.rate) }}<small>%</small></div>
          <div class="kpi__meta">{{ num(metrics.duplicate_execution.blocked_messages) }} 条被幂等去重拦截</div>
        </div>
      </div>

      <!-- ============ 指标口径（设计稿：detail-list 定义卡） ============ -->
      <div class="bc-panel metric-card">
        <div class="card-head">
          <span class="card-head__title">指标口径</span>
          <span class="mono card-head__hint">与 /metrics 同源</span>
        </div>
        <dl class="detail-list">
          <div class="detail-row">
            <dt>平均轮次延迟</dt>
            <dd>一轮完整治理链路（能力集 → RAG 预过滤 → LLM/工具回路 → 审计）耗时</dd>
          </div>
          <div class="detail-row">
            <dt>累计 LLM 成本</dt>
            <dd>按 token 用量 × 单价（DB 设置 cost.*）估算，非计费系统</dd>
          </div>
          <div class="detail-row">
            <dt>人工接管率</dt>
            <dd>HITL 审批请求数 ÷ 对话轮次</dd>
          </div>
          <div class="detail-row">
            <dt>失败率</dt>
            <dd>LLM/工具回路异常轮次 ÷ 总轮次</dd>
          </div>
          <div class="detail-row">
            <dt>重复执行率</dt>
            <dd>幂等去重拦截消息 ÷（拦截 + 已处理轮次）</dd>
          </div>
        </dl>
        <div class="table-foot">
          <span>试点阶段业务效果看板（T3.4）· 每次刷新实时计算</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'

const loading = ref(true)
const metrics = ref({
  latency: { avg_seconds: 0, turn_count: 0 },
  cost: { total_usd: 0, input_tokens: 0, output_tokens: 0 },
  human_takeover: { requested: 0, approved: 0, rejected: 0, rate: 0 },
  failure: { failed_turns: 0, total_turns: 0, rate: 0 },
  duplicate_execution: { blocked_messages: 0, total_turns: 0, rate: 0 },
})

function pct(rate) {
  return (rate * 100).toFixed(1)
}
const num = (n) => (typeof n === 'number' ? n.toLocaleString('zh-CN') : '—')

async function load() {
  loading.value = true
  try {
    const data = await admin.opsMetrics()
    if (data?.latency) metrics.value = data
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* ============ KPI 行（设计稿：五卡，label + mono 大数 + 单位小字） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
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
.kpi__value small {
  font-size: 15px;
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  letter-spacing: 0;
  margin-left: 2px;
}
.kpi__meta {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  line-height: 1.5;
}

/* ============ 口径定义卡 ============ */
.metric-card {
  display: flex;
  flex-direction: column;
}
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
}
.detail-list {
  margin: 0;
  padding: var(--bc-space-2) var(--bc-space-4);
}
.detail-row {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: var(--bc-space-3);
  padding: var(--bc-space-2) 0;
  border-bottom: 1px solid var(--bc-border);
}
.detail-row:last-child {
  border-bottom: 0;
}
.detail-row dt {
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-1);
}
.detail-row dd {
  margin: 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  line-height: 1.6;
}

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

@media (max-width: 1280px) {
  .kpis {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 640px) {
  .kpis {
    grid-template-columns: 1fr;
  }
  .detail-row {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
</style>
