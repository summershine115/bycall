<template>
  <div class="bc-page">
    <PageHeader
      title="提示词版本"
      description="数字员工人格的版本化资产：创建即激活，历史版本可随时回滚。"
    >
      <el-button type="primary" :icon="Plus" @click="openCreate">新建版本</el-button>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="3" />

    <template v-else>
      <!-- ============ 当前生效横幅（设计稿：生效版本一目了然） ============ -->
      <div v-if="activePrompt" class="bc-panel active-banner">
        <div class="active-banner__main">
          <div class="active-banner__label">当前生效</div>
          <div class="active-banner__version mono">v{{ activePrompt.version }}</div>
          <StatusBadge type="ok" text="激活中" />
        </div>
        <div class="active-banner__body">
          <div class="active-banner__meta">
            {{ activePrompt.label || '未命名版本' }} · {{ fmtTime(activePrompt.created_at) }}
          </div>
          <div class="active-banner__preview">{{ activePrompt.content }}</div>
        </div>
        <div class="active-banner__actions">
          <el-button size="small" @click="onView(activePrompt)">查看全文</el-button>
        </div>
      </div>

      <!-- ============ 版本列表 ============ -->
      <div class="bc-panel list-panel">
        <div class="list-panel__head">
          <span class="list-panel__title">版本列表</span>
          <span class="list-panel__count mono">{{ prompts.length }} 个版本</span>
        </div>
        <EmptyState
          v-if="!prompts.length"
          icon="Document"
          title="还没有提示词版本"
          description="系统提示词定义数字员工的人格与行为边界，用户消息永远只进 user message。"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate">创建第一个版本</el-button>
        </EmptyState>

        <el-table v-else :data="prompts">
          <el-table-column label="版本" width="90">
            <template #default="{ row }">
              <span class="bc-strong mono">v{{ row.version }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="label" label="标签" width="220">
            <template #default="{ row }">{{ row.label || '—' }}</template>
          </el-table-column>
          <el-table-column label="内容" min-width="320">
            <template #default="{ row }">
              <div class="bc-cell-clamp">{{ row.content }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <StatusBadge v-if="row.is_active" type="ok" text="激活中" />
              <span v-else class="bc-muted bc-text-sm">历史版本</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="onView(row)">查看</el-button>
              <el-button v-if="!row.is_active" size="small" type="success" plain @click="onActivate(row)">
                回滚到此
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="list-panel__foot">
          <span>新建版本将立即激活；回滚是原子切换 is_active，立即对全部会话生效</span>
        </div>
      </div>

      <!-- ============ 差异预览（真实 LCS 行级 diff） ============ -->
      <div v-if="prompts.length >= 2" class="bc-panel diff-panel">
        <div class="diff-panel__head">
          <span class="diff-panel__title">差异预览</span>
          <div class="diff-panel__picks">
            <el-select v-model="diffA" size="small" class="diff-pick" aria-label="版本 A">
              <el-option v-for="p in prompts" :key="p.version" :label="`v${p.version}`" :value="p.version" />
            </el-select>
            <span class="bc-muted bc-text-xs">对比</span>
            <el-select v-model="diffB" size="small" class="diff-pick" aria-label="版本 B">
              <el-option v-for="p in prompts" :key="p.version" :label="`v${p.version}`" :value="p.version" />
            </el-select>
          </div>
        </div>
        <div v-if="diffLines" class="diff-body">
          <div
            v-for="(line, i) in diffLines"
            :key="i"
            class="diff-line"
            :class="`diff-line--${line.type}`"
          >
            <span class="diff-line__sign">{{ line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' ' }}</span>
            <span class="diff-line__text">{{ line.text || ' ' }}</span>
          </div>
        </div>
        <div v-else class="diff-empty bc-muted bc-text-sm">
          {{ diffLines === null ? '版本行数过大，已跳过逐行对比' : '两个版本内容完全一致' }}
        </div>
        <div class="diff-panel__foot mono">
          <span class="diff-stat diff-stat--add">+{{ diffStats.add }}</span>
          <span class="diff-stat diff-stat--del">−{{ diffStats.del }}</span>
          <span class="bc-muted">v{{ diffA }} → v{{ diffB }} · 按行对比</span>
        </div>
      </div>
    </template>

    <el-dialog v-model="dialogVisible" title="新建提示词版本" width="680px">
      <el-form :model="form" label-position="top">
        <el-form-item label="标签">
          <el-input v-model="form.label" placeholder="如：销售场景专用 / 全公司统一" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="数字员工的系统提示词..."
          />
          <div class="bc-hint">创建后将立即激活为当前版本，旧版本自动转为历史版本</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">创建并激活</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="viewVisible" width="760px">
      <template #header>
        <div class="prompt-viewer__title">
          <span class="bc-strong mono">v{{ viewing.version }}</span>
          <span class="bc-muted">{{ viewing.label || '未命名版本' }}</span>
          <StatusBadge v-if="viewing.is_active" type="ok" text="激活中" />
        </div>
      </template>
      <pre class="prompt-viewer__body">{{ viewing.content }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { admin } from '../api'
import { formatTime as fmtTime } from '../utils/format'
import { useCrud } from '../composables/useCrud'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const prompts = ref([])
const { loading, dialogVisible, saving, form, openCreate } = useCrud({
  emptyForm: () => ({ label: '', content: '' }),
})
const viewVisible = ref(false)
const viewing = ref({})

const activePrompt = computed(() => prompts.value.find((p) => p.is_active) || null)

// ==================== 差异预览（LCS 行级 diff，真实数据） ====================
const diffA = ref(null)
const diffB = ref(null)

// 默认对比：最新版 ↔ 当前生效版
function initDiffPicks() {
  const latest = prompts.value[0]
  const active = activePrompt.value
  diffA.value = active?.version ?? latest?.version ?? null
  diffB.value = latest?.version ?? null
}

const diffLines = computed(() => {
  const a = prompts.value.find((p) => p.version === diffA.value)
  const b = prompts.value.find((p) => p.version === diffB.value)
  if (!a || !b) return []
  const la = (a.content || '').split('\n')
  const lb = (b.content || '').split('\n')
  // 超长保护：O(n·m) 的 dp 矩阵在行数过大时卡顿，超出阈值直接返回 null（面板显示提示）
  if (la.length * lb.length > 1_000_000) return null
  return lcsDiff(la, lb)
})

const diffStats = computed(() => {
  if (!diffLines.value) return { add: 0, del: 0 }
  const add = diffLines.value.filter((l) => l.type === 'add').length
  const del = diffLines.value.filter((l) => l.type === 'del').length
  return { add, del }
})

// 经典 LCS 行 diff：O(n·m)，提示词行数量级（百行）完全够用
function lcsDiff(a, b) {
  const n = a.length
  const m = b.length
  // dp[i][j] = a[i:] 与 b[j:] 的最长公共子序列长度
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) out.push({ type: 'del', text: a[i++] })
  while (j < m) out.push({ type: 'add', text: b[j++] })
  return out
}

async function load() {
  loading.value = true
  try {
    prompts.value = await admin.listPrompts()
    initDiffPicks()
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!form.content.trim()) {
    ElMessage.warning('内容不能为空')
    return
  }
  saving.value = true
  try {
    // 版本号 = 当前最大 + 1
    const nextVersion = (prompts.value[0]?.version || 0) + 1
    let created
    try {
      created = await admin.createPrompt({
        version: nextVersion,
        label: form.label || `v${nextVersion}`,
        content: form.content,
        is_active: false,
      })
    } catch {
      // 创建失败（如并发/重试导致 409 版本冲突）也要刷新列表，
      // 否则重试仍基于过期的最大版本号推算 → 永远 409 死锁
      await load().catch(() => {})
      return
    }
    // 创建后立即激活
    await admin.activatePrompt(created.version)
    ElMessage.success('已创建并激活')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function onActivate(row) {
  const ok = await ElMessageBox.confirm(`确定回滚到 v${row.version}？当前激活版本将停用`, '确认回滚', {
    type: 'warning',
  }).catch(() => null)
  if (!ok) return  // 用户取消
  await admin.activatePrompt(row.version)
  ElMessage.success('已激活')
  await load()
}

function onView(row) {
  viewing.value = row
  viewVisible.value = true
}

onMounted(() => load().catch(() => {}))  // 失败已由拦截器 toast
</script>

<style scoped>
/* ============ 当前生效横幅 ============ */
.active-banner {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: var(--bc-space-5);
  align-items: center;
  padding: var(--bc-space-4) var(--bc-space-5);
  margin-bottom: var(--bc-space-4);
}
.active-banner__main {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
}
.active-banner__label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  letter-spacing: 0.06em;
}
.active-banner__version {
  font-size: var(--bc-text-2xl);
  font-weight: var(--bc-weight-semibold);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
.active-banner__meta {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin-bottom: var(--bc-space-1);
}
.active-banner__preview {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  line-height: var(--bc-leading-normal);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ============ 版本列表面板 ============ */
.list-panel {
  padding: 0 0 var(--bc-space-3);
}
.list-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-4) var(--bc-space-5) 0;
}
.list-panel__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
}
.list-panel__count {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.list-panel__foot {
  padding: var(--bc-space-3) var(--bc-space-5) 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}

/* ============ 差异预览 ============ */
.diff-panel {
  margin-top: var(--bc-space-4);
  padding: 0 0 var(--bc-space-3);
}
.diff-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-4) var(--bc-space-5) var(--bc-space-3);
}
.diff-panel__title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
}
.diff-panel__picks {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.diff-pick {
  width: 110px;
}
.diff-body {
  margin: 0 var(--bc-space-5);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  background: var(--bc-bg-sunken);
  overflow: auto;
  max-height: 360px;
  font-family: var(--bc-font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
  font-size: var(--bc-text-xs);
  line-height: 1.7;
}
.diff-line {
  display: flex;
  gap: var(--bc-space-2);
  padding: 0 var(--bc-space-3);
  white-space: pre-wrap;
  word-break: break-all;
}
.diff-line__sign {
  flex-shrink: 0;
  width: 1em;
  text-align: center;
  user-select: none;
}
.diff-line--add {
  background: var(--bc-success-bg);
  color: var(--bc-success-text);
}
.diff-line--del {
  background: var(--bc-danger-bg);
  color: var(--bc-danger-text);
}
.diff-line--same .diff-line__text {
  color: var(--bc-text-3);
}
.diff-empty {
  margin: 0 var(--bc-space-5);
  padding: var(--bc-space-6);
  text-align: center;
}
.diff-panel__foot {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-5) 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.diff-stat--add {
  color: var(--bc-success-text);
}
.diff-stat--del {
  color: var(--bc-danger-text);
}

/* 全文查看器 */
.prompt-viewer__title {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.prompt-viewer__body {
  max-height: 60vh;
  overflow: auto;
  margin: 0;
  padding: var(--bc-space-4);
  background: var(--bc-bg-sunken);
  border-radius: var(--bc-radius-md);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-normal);
  color: var(--bc-text-2);
}
</style>
