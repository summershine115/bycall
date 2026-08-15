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
      <div class="stats-grid">
        <StatCard
          label="平均轮次延迟"
          :value="`${metrics.latency.avg_seconds.toFixed(2)}s`"
          :hint="`累计 ${metrics.latency.turn_count} 轮对话`"
          accent="primary"
        />
        <StatCard
          label="累计 LLM 成本"
          :value="`$${metrics.cost.total_usd.toFixed(4)}`"
          :hint="`输入 ${metrics.cost.input_tokens} / 输出 ${metrics.cost.output_tokens} token`"
          accent="success"
        />
        <StatCard
          label="人工接管率"
          :value="pct(metrics.human_takeover.rate)"
          :hint="`${metrics.human_takeover.requested} 次审批请求 / ${metrics.human_takeover.approved} 通过 / ${metrics.human_takeover.rejected} 拒绝`"
          accent="warning"
        />
        <StatCard
          label="失败率"
          :value="pct(metrics.failure.rate)"
          :hint="`${metrics.failure.failed_turns} 失败 / ${metrics.failure.total_turns} 轮`"
          accent="info"
        />
        <StatCard
          label="重复执行率"
          :value="pct(metrics.duplicate_execution.rate)"
          :hint="`${metrics.duplicate_execution.blocked_messages} 条重复消息被幂等去重拦截`"
          accent="primary"
        />
      </div>

      <DashSection title="指标口径">
        <p class="bc-muted">
          延迟 = 一轮完整治理链路（能力集 → RAG 预过滤 → LLM/工具回路 → 审计）耗时；
          成本 = 按 token 用量 × 单价（DB 设置 cost.*）估算，非计费系统；
          人工接管 = HITL 审批请求数 / 对话轮次；失败 = LLM/工具回路异常轮次 / 总轮次；
          重复执行 = 幂等去重拦截消息 /（拦截 + 已处理轮次）。
        </p>
      </DashSection>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import ContentLoader from '../components/ContentLoader.vue'
import DashSection from '../components/DashSection.vue'

const loading = ref(true)
const metrics = ref({
  latency: { avg_seconds: 0, turn_count: 0 },
  cost: { total_usd: 0, input_tokens: 0, output_tokens: 0 },
  human_takeover: { requested: 0, approved: 0, rejected: 0, rate: 0 },
  failure: { failed_turns: 0, total_turns: 0, rate: 0 },
  duplicate_execution: { blocked_messages: 0, total_turns: 0, rate: 0 },
})

function pct(rate) {
  return `${(rate * 100).toFixed(1)}%`
}

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
.stats-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: var(--bc-space-4);
}
@media (max-width: 1280px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
