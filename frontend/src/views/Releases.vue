<template>
  <div class="bc-page">
    <PageHeader
      title="发布与回滚"
      description="提示词与 chat 模型的灰度发布状态：发布前过评测门，灰度可转全量，一键回滚到上一版本（复用 version + is_active）。"
    >
      <el-button :icon="Refresh" circle aria-label="刷新" @click="load" />
    </PageHeader>

    <el-table v-loading="loading" :data="releases" class="bc-table" empty-text="暂无发布记录">
      <el-table-column label="类型" width="120">
        <template #default="{ row }">
          <el-tag :type="row.kind === 'prompt' ? 'primary' : 'success'">
            {{ row.kind === 'prompt' ? '系统提示词' : 'chat 模型' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="目标版本" width="140">
        <template #default="{ row }">
          <span v-if="row.kind === 'prompt'">v{{ row.target_prompt_version }}</span>
          <span v-else>model #{{ row.target_model_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="上一版本" width="140">
        <template #default="{ row }">
          <span v-if="row.kind === 'prompt'">
            {{ row.previous_prompt_version ? `v${row.previous_prompt_version}` : '（无）' }}
          </span>
          <span v-else>{{ row.previous_model_id ? `model #${row.previous_model_id}` : '（无）' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="灰度比例" width="120">
        <template #default="{ row }">
          <el-progress
            :percentage="row.canary_percent"
            :stroke-width="8"
            :show-text="row.canary_percent > 0 && row.canary_percent < 100"
          />
          <span v-if="row.canary_percent === 0 || row.canary_percent === 100" class="bc-muted">全量</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="发布门" min-width="180">
        <template #default="{ row }">
          <template v-if="gateOf(row).passed === true || gateOf(row).passed === false">
            <el-tag :type="gateOf(row).passed === false ? 'danger' : 'success'" size="small">
              {{ gateOf(row).passed === false ? '未通过' : '已通过' }}
            </el-tag>
            <span class="bc-muted">安全用例失败 {{ gateOf(row).security_failed ?? 0 }} 条</span>
          </template>
          <span v-else class="bc-muted">未记录（跳过门）</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'canary'"
            size="small"
            type="primary"
            @click="promote(row.kind)"
          >
            转全量
          </el-button>
          <el-button
            v-if="row.status === 'canary' || row.status === 'active'"
            size="small"
            type="danger"
            plain
            @click="rollback(row.kind)"
          >
            回滚
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <p class="bc-muted" style="margin-top: var(--bc-space-4)">
      灰度（1–99%）期间保留旧版 is_active，运行时按用户/会话稳定哈希分流；转全量或回滚都是
      原子切换 is_active——任何一步发布都先过评测门，一个失败用例即阻断。
    </p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'

const loading = ref(false)
const releases = ref([])

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
  await ElMessageBox.confirm(`确认将「${kind}」灰度发布转为全量？`, '转全量')
  await admin.promoteRelease(kind)
  ElMessage.success('已转全量')
  load()
}

async function rollback(kind) {
  await ElMessageBox.confirm(
    `确认回滚「${kind}」到上一版本？is_active 将立即切回。`,
    '一键回滚',
    { type: 'warning' },
  )
  await admin.rollbackRelease(kind)
  ElMessage.success('已回滚')
  load()
}

onMounted(load)
</script>
