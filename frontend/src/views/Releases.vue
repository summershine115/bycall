<template>
  <div class="bc-page">
    <PageHeader
      title="发布与回滚"
      description="提示词与 chat 模型的灰度发布状态：发布前过评测门，灰度可转全量，一键回滚到上一版本。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <ContentLoader v-if="loading" :rows="3" />

    <template v-else>
      <!-- ============ KPI 行（真实数据派生） ============ -->
      <div class="kpis">
        <div class="kpi bc-panel">
          <div class="kpi__label">灰度中</div>
          <div class="kpi__value mono">{{ kpi.canary }}</div>
          <div class="kpi__meta">{{ kpi.canaryTargets || '当前无灰度发布' }}</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">已全量</div>
          <div class="kpi__value mono">{{ kpi.active }}</div>
          <div class="kpi__meta">当前生效中的发布</div>
        </div>
        <div class="kpi bc-panel">
          <div class="kpi__label">已回滚</div>
          <div class="kpi__value mono">{{ kpi.rolledBack }}</div>
          <div class="kpi__meta">记录保留，可追溯</div>
        </div>
      </div>

      <!-- ============ 发布记录（卡片 + 表格） ============ -->
      <div class="bc-panel rel-panel">
        <div class="rel-panel__head">
          <span class="rel-panel__title">发布记录</span>
          <span class="rel-panel__count mono">{{ releases.length }} 条记录</span>
        </div>

        <EmptyState
          v-if="!releases.length"
          icon="RefreshLeft"
          title="暂无发布记录"
          description="在「提示词版本」或「模型管理」发布新版本后，记录会出现在这里。"
        />

        <el-table v-else v-loading="loading" :data="releases">
          <el-table-column label="类型" width="120">
            <template #default="{ row }">
              <el-tag :type="row.kind === 'prompt' ? 'primary' : 'info'">
                {{ row.kind === 'prompt' ? '系统提示词' : 'chat 模型' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="目标版本" width="130">
            <template #default="{ row }">
              <span class="bc-strong mono">{{ targetLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="上一版本" width="130">
            <template #default="{ row }">
              <span class="mono bc-muted">{{ prevLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="灰度比例" width="150">
            <template #default="{ row }">
              <div class="canary-cell">
                <div class="canary-bar" aria-hidden="true">
                  <div class="canary-bar__fill" :style="{ width: `${row.canary_percent}%` }" />
                </div>
                <span class="mono canary-num">{{ row.canary_percent }}%</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="发布门" width="170">
            <template #default="{ row }">
              <template v-if="gateOf(row).passed === true">
                <el-tag type="success" size="small" effect="plain">已通过</el-tag>
              </template>
              <template v-else-if="gateOf(row).passed === false">
                <el-tag type="danger" size="small" effect="plain">未通过</el-tag>
                <span class="bc-muted bc-text-xs gate-fail">安全用例失败 {{ gateOf(row).security_failed ?? 0 }} 条</span>
              </template>
              <span v-else class="bc-muted bc-text-sm">未记录（跳过门）</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="light">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.status === 'canary'"
                size="small"
                type="primary"
                @click="promote(row.kind)"
              >
                提速至 100%
              </el-button>
              <el-button
                v-if="row.status === 'canary' || row.status === 'active'"
                size="small"
                type="danger"
                plain
                @click="rollback(row.kind)"
              >
                一键回滚
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="rel-panel__foot">
          <span>回滚立即生效、不经过灰度；灰度（1–99%）期间按用户/会话稳定哈希分流</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(false)
const releases = ref([])

const kpi = computed(() => {
  const list = releases.value
  const canaryList = list.filter((r) => r.status === 'canary')
  return {
    canary: canaryList.length,
    canaryTargets: canaryList.map(targetLabel).join(' · '),
    active: list.filter((r) => r.status === 'active').length,
    rolledBack: list.filter((r) => r.status === 'rolled_back').length,
  }
})

function targetLabel(row) {
  return row.kind === 'prompt' ? `v${row.target_prompt_version}` : `model #${row.target_model_id}`
}
function prevLabel(row) {
  if (row.kind === 'prompt') return row.previous_prompt_version ? `v${row.previous_prompt_version}` : '（无）'
  return row.previous_model_id ? `model #${row.previous_model_id}` : '（无）'
}

function gateOf(row) {
  try {
    return JSON.parse(row.gate_report_json || '{}')
  } catch {
    return {}
  }
}

function statusLabel(status) {
  return { canary: '灰度中', active: '已全量', rolled_back: '已回滚' }[status] || status
}

function statusType(status) {
  return { canary: 'warning', active: 'success', rolled_back: 'info' }[status] || 'info'
}

async function load() {
  loading.value = true
  try {
    releases.value = await admin.listReleases()
  } finally {
    loading.value = false
  }
}

async function promote(kind) {
  const ok = await ElMessageBox.confirm(`确认将「${kindLabel(kind)}」灰度发布转为全量？`, '转全量').catch(() => null)
  if (!ok) return  // 用户取消
  await admin.promoteRelease(kind)
  ElMessage.success('已转全量')
  await load().catch(() => {})
}

async function rollback(kind) {
  const ok = await ElMessageBox.confirm(
    `确认回滚「${kindLabel(kind)}」到上一版本？is_active 将立即切回。`,
    '一键回滚',
    { type: 'warning' },
  ).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.rollbackRelease(kind)
  ElMessage.success('已回滚')
  await load().catch(() => {})
}

function kindLabel(kind) {
  return kind === 'prompt' ? '系统提示词' : 'chat 模型'
}

onMounted(() => load().catch(() => {}))  // 失败已由拦截器 toast
</script>

<style scoped>
/* ============ KPI 行（设计稿语言：label + mono 大数 + 脚注） ============ */
.kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
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
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ============ 发布记录面板 ============ */
.rel-panel {
  padding: 0 0 var(--bc-space-3);
}
.rel-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-4) var(--bc-space-5) 0;
}
.rel-panel__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
}
.rel-panel__count {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.rel-panel__foot {
  padding: var(--bc-space-3) var(--bc-space-5) 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}

/* ============ 灰度比例条（设计稿：黑色填充比例条 + mono 数字） ============ */
.canary-cell {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.canary-bar {
  flex: 1;
  height: 6px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-gray-2);
  overflow: hidden;
}
.canary-bar__fill {
  height: 100%;
  border-radius: var(--bc-radius-full);
  background: var(--bc-primary-6);
  transition: width 0.3s var(--bc-ease, ease-out);
}
.canary-num {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-2);
  font-variant-numeric: tabular-nums;
  min-width: 38px;
  text-align: right;
}
.gate-fail {
  margin-left: 6px;
}
</style>
