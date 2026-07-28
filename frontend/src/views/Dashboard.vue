<template>
  <div class="bc-page">
    <PageHeader title="仪表盘" description="组织数字员工的治理概览：规模、配置状态与全链路审计动态。">
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :cards="4" />

    <template v-else>
      <!-- 第一层级：关键规模指标 -->
      <div class="stats-grid">
        <StatCard
          label="组织用户"
          :value="counts.users"
          hint-icon="User"
          :hint="`${counts.activeUsers} 个启用`"
          accent="primary"
        />
        <StatCard
          label="角色"
          :value="counts.roles"
          hint="RBAC 权限边界"
          accent="success"
        />
        <StatCard
          label="知识库 / 文档"
          :value="counts.kbs"
          :hint="`${counts.docs} 篇文档已索引`"
          accent="warning"
        />
        <StatCard
          label="审计记录"
          :value="counts.audit"
          hint="全量留痕可追溯"
          accent="info"
        />
      </div>

      <!-- 第二层级：治理脊柱（核心设计哲学） -->
      <DashSection title="治理脊柱">
        <GovernanceSpine />
      </DashSection>

      <!-- 第三层级：动态 + 状态 -->
      <DashSection title="运行状态">
        <div class="status-grid">
          <AuditChart :logs="auditLogs" />
          <ActiveConfig :prompt="activePrompt" :models="models" :im-status="imStatus" />
        </div>
      </DashSection>

      <DashSection title="近期审计动态">
        <template #extra>
          <el-button text size="small" @click="$router.push({ name: 'audit' })">
            查看全部<el-icon class="el-icon--right"><ArrowRight /></el-icon>
          </el-button>
        </template>
        <RecentAudit :logs="recentLogs" />
      </DashSection>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import ContentLoader from '../components/ContentLoader.vue'
import DashSection from '../components/DashSection.vue'
import GovernanceSpine from '../components/GovernanceSpine.vue'
import AuditChart from '../components/AuditChart.vue'
import ActiveConfig from '../components/ActiveConfig.vue'
import RecentAudit from '../components/RecentAudit.vue'

const loading = ref(true)
const counts = ref({ users: 0, activeUsers: 0, roles: 0, kbs: 0, docs: 0, audit: 0 })
const activePrompt = ref(null)
const auditLogs = ref([])
const models = ref([])
const imStatus = ref(null)

const recentLogs = computed(() => auditLogs.value.slice(0, 8))

async function load() {
  loading.value = true
  try {
    // 并行拉取仪表盘所需的全部数据；单项失败不阻塞整体展示
    const safe = (p, fallback) => p.catch(() => fallback)
    const [users, roles, prompts, audit, kbs, modelList, im] = await Promise.all([
      safe(admin.listUsers(), []),
      safe(admin.listRoles(), []),
      safe(admin.listPrompts(), []),
      // 后端 audit API 返回扁平列表（无 total 字段），limit 上限 500。
      // 此处拉满 500 用于计数 + 近期列表；若审计量超 500 需后端加 count 端点。
      safe(admin.listAudit(500), []),
      safe(admin.listKnowledgeBases(), []),
      safe(admin.listModels(), []),
      safe(admin.imStatus(), null),
    ])
    const docs = await safe(
      Promise.all(kbs.map((kb) => admin.listDocuments(kb.id).then((d) => d.length))).then((ns) =>
        ns.reduce((a, b) => a + b, 0),
      ),
      0,
    )
    counts.value = {
      users: users.length,
      activeUsers: users.filter((u) => u.is_active).length,
      roles: roles.length,
      kbs: kbs.length,
      docs,
      audit: audit.length,
    }
    activePrompt.value = prompts.find((p) => p.is_active) || null
    auditLogs.value = audit
    models.value = modelList
    imStatus.value = im
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--bc-space-4);
}
.status-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: var(--bc-space-4);
  align-items: stretch;
}
@media (max-width: 1280px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .status-grid {
    grid-template-columns: 1fr;
  }
}
</style>
