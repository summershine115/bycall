<template>
  <div class="bc-page">
    <PageHeader
      title="策略模拟"
      description="以任意用户身份试运行检索 / 工具调用，预览治理判定 · 与数据面同源组件，不产生真实调用。"
    />

    <div class="row-1-2">
      <!-- ============ 左：模拟身份 + 动作（设计稿 stack） ============ -->
      <div class="stack">
        <div class="bc-panel sim-card">
          <div class="card-head">
            <span class="card-head__title">模拟身份</span>
          </div>
          <div class="sim-body">
            <div class="field">
              <span class="field__label">选择用户</span>
              <el-select
                v-model="userId"
                filterable
                placeholder="选择要模拟的用户"
                style="width: 100%"
              >
                <el-option
                  v-for="u in users"
                  :key="u.id"
                  :label="`${u.display_name || u.name}（${u.role}）`"
                  :value="u.id"
                />
              </el-select>
            </div>
            <dl v-if="profile" class="detail-list">
              <div class="detail-row">
                <dt>角色</dt>
                <dd>{{ profile.role }}</dd>
              </div>
              <div class="detail-row">
                <dt>职级</dt>
                <dd>{{ profile.level || '—' }}</dd>
              </div>
            </dl>
            <p v-else class="field__hint">选择用户后显示其角色与职级</p>
          </div>
        </div>

        <div class="bc-panel sim-card">
          <div class="card-head">
            <span class="card-head__title">动作</span>
          </div>
          <div class="sim-body">
            <div class="field">
              <span class="field__label">动作类型</span>
              <div class="segmented" role="tablist" aria-label="动作类型">
                <button
                  v-for="a in ACTIONS"
                  :key="a.value"
                  type="button"
                  :class="['segmented__btn', { 'is-on': action === a.value }]"
                  role="tab"
                  :aria-selected="action === a.value"
                  @click="action = a.value"
                >
                  {{ a.label }}
                </button>
              </div>
            </div>
            <div v-if="action === 'tool_call'" class="field">
              <span class="field__label">工具名</span>
              <el-select
                v-model="toolName"
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入工具名"
                style="width: 100%"
              >
                <el-option
                  v-for="t in tools"
                  :key="t.name"
                  :label="t.description ? `${t.name} · ${t.description}` : t.name"
                  :value="t.name"
                />
              </el-select>
              <span class="field__hint">可选范围 = 全量已注册工具；模拟不受该用户授权限制</span>
            </div>
            <el-button
              type="primary"
              :loading="loading"
              :disabled="!userId || (action === 'tool_call' && !toolName)"
              style="width: 100%"
              @click="run"
            >
              运行模拟
            </el-button>
          </div>
        </div>
      </div>

      <!-- ============ 右：决策结果（verdict 卡 + 能力集详情） ============ -->
      <div class="bc-panel sim-card">
        <div class="card-head">
          <span class="card-head__title">决策结果</span>
          <span v-if="ranAt" class="mono card-head__hint">{{ ranAt }}</span>
        </div>

        <div v-if="!result" :class="['verdict', 'verdict--idle']">
          <div class="verdict__ic">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M10 3v4M14 3v4M9.5 3h5M8 21l1.5-6.5h5L16 21M12 14.5V11" />
              <path d="M9 9c0-1 .8-2 3-2s3 1 3 2-.8 2-3 2-3-1-3-2z" />
            </svg>
          </div>
          <div class="verdict__big">尚未运行</div>
          <p class="verdict__why">
            选择身份与动作后运行模拟，这里会显示治理引擎的实时判定：允许或拒绝，并附能力集与过滤条件。
          </p>
        </div>

        <div v-else :class="['verdict', result.decision === 'allowed' ? 'verdict--allow' : 'verdict--deny']">
          <div class="verdict__ic">
            <svg v-if="result.decision === 'allowed'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4.5 12.5l5 5 10-11" />
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </div>
          <div class="verdict__big">
            {{ result.decision === 'allowed' ? '允许' : '拒绝' }}
            <span class="mono verdict__enum">{{ result.decision }}</span>
          </div>
          <p class="verdict__why">{{ result.detail }}</p>
          <span class="verdict__pid mono">
            {{ result.user_name }} · {{ action === 'tool_call' ? toolName : 'retrieve' }}
          </span>
        </div>

        <template v-if="result">
          <dl class="detail-list detail-list--bordered">
            <div class="detail-row">
              <dt>角色</dt>
              <dd>{{ result.role }}<template v-if="result.is_admin"> · admin</template></dd>
            </div>
            <div class="detail-row">
              <dt>职级</dt>
              <dd>{{ result.level || '—' }}</dd>
            </div>
            <div class="detail-row">
              <dt>可见知识域</dt>
              <dd class="chips">
                <span v-for="t in result.knowledge_scope_tags" :key="t" class="chip">{{ t }}</span>
                <span v-if="!result.knowledge_scope_tags.length" class="bc-muted">（空）</span>
              </dd>
            </div>
            <div class="detail-row">
              <dt>可用工具</dt>
              <dd class="chips">
                <span v-for="t in result.allowed_tools.slice(0, 8)" :key="t" class="chip mono">{{ t }}</span>
                <span v-if="result.allowed_tools.length > 8" class="chip chip--more">
                  +{{ result.allowed_tools.length - 8 }}
                </span>
                <span v-if="!result.allowed_tools.length" class="bc-muted">（空）</span>
              </dd>
            </div>
            <div class="detail-row detail-row--col">
              <dt>召回前过滤条件（retrieval_filters）</dt>
              <dd>
                <pre v-if="result.retrieval_filters.length" class="json-block">{{ formatJson(result.retrieval_filters) }}</pre>
                <span v-else class="bc-muted bc-text-sm">无过滤（admin / 通配）</span>
              </dd>
            </div>
          </dl>
        </template>

        <div class="table-foot">
          <span>复用治理脊柱真实组件（与数据面判定同源）· 不产生审计条目</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'

const users = ref([])
const tools = ref([])
const userId = ref(null)
const action = ref('retrieve')
const toolName = ref('')
const loading = ref(false)
const result = ref(null)
const ranAt = ref('')

const ACTIONS = [
  { value: 'retrieve', label: '检索知识' },
  { value: 'tool_call', label: '调用工具' },
]

const profile = computed(() => users.value.find((u) => u.id === userId.value) || null)

function formatJson(v) {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

onMounted(async () => {
  const [u, t] = await Promise.allSettled([admin.listUsers(), admin.listTools()])
  if (u.status === 'fulfilled') users.value = u.value
  if (t.status === 'fulfilled') tools.value = t.value
})

async function run() {
  if (!userId.value) return
  loading.value = true
  try {
    result.value = await admin.simulatePolicy({
      user_id: userId.value,
      action: action.value,
      tool_name: action.value === 'tool_call' ? toolName.value || null : null,
    })
    ranAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* ============ 主行布局：1 : 2（设计稿 row-1-2） ============ */
.row-1-2 {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: var(--bc-space-4);
  align-items: start;
}
.stack {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-4);
  min-width: 0;
}
.sim-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* ============ 卡头 ============ */
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

/* ============ 表单体 ============ */
.sim-body {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-4);
  padding: var(--bc-space-4);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field__label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  letter-spacing: 0.02em;
}
.field__hint {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  line-height: 1.5;
  margin: 0;
}

/* ============ 分段控件（与仪表盘/会话同语言） ============ */
.segmented {
  display: inline-flex;
  background: var(--bc-bg-sunken);
  border-radius: var(--bc-radius-md);
  padding: 2px;
  width: fit-content;
}
.segmented__btn {
  border: 0;
  background: transparent;
  font: 500 var(--bc-text-xs) / 1 inherit;
  font-family: inherit;
  color: var(--bc-text-2);
  padding: 6px 12px;
  border-radius: var(--bc-radius-sm);
  cursor: pointer;
  transition: all 0.15s var(--bc-ease, ease-out);
}
.segmented__btn.is-on {
  background: var(--bc-bg-surface);
  color: var(--bc-text-1);
  box-shadow: var(--bc-shadow-xs);
}

/* ============ 决策 verdict（设计稿：图标圆 + 判定 + 依据 + 身份 pill） ============ */
.verdict {
  display: grid;
  gap: 10px;
  place-items: center;
  text-align: center;
  padding: var(--bc-space-6) var(--bc-space-4);
}
.verdict__ic {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  display: grid;
  place-items: center;
}
.verdict__ic svg {
  width: 20px;
  height: 20px;
}
.verdict--idle .verdict__ic {
  background: var(--bc-bg-sunken);
  color: var(--bc-text-3);
}
.verdict--allow .verdict__ic {
  background: var(--bc-success-bg);
  color: var(--bc-success-text);
}
.verdict--deny .verdict__ic {
  background: var(--bc-danger-bg);
  color: var(--bc-danger-text);
}
.verdict__big {
  font-size: 17px;
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.verdict--idle .verdict__big {
  color: var(--bc-text-3);
}
.verdict__enum {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-3);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-full);
  padding: 2px 8px;
}
.verdict__why {
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  max-width: 44ch;
  line-height: 1.6;
  margin: 0;
}
.verdict__pid {
  font-size: 11px;
  font-weight: var(--bc-weight-medium);
  letter-spacing: 0.06em;
  color: var(--bc-text-3);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-full);
  padding: 4px 8px;
}

/* ============ 详情列表 ============ */
.detail-list {
  margin: 0;
  padding: var(--bc-space-2) var(--bc-space-4);
}
.detail-list--bordered {
  border-top: 1px solid var(--bc-border);
}
.detail-row {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: var(--bc-space-3);
  padding: var(--bc-space-2) 0;
  border-bottom: 1px solid var(--bc-border);
  align-items: baseline;
}
.detail-row:last-child {
  border-bottom: 0;
}
.detail-row--col {
  grid-template-columns: 1fr;
}
.detail-row dt {
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-medium);
  color: var(--bc-text-1);
}
.detail-row dd {
  margin: 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-2);
  line-height: 1.6;
}

/* ============ 能力集 chips（与角色页同语言） ============ */
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.chip {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--bc-border);
  border-radius: 5px;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  white-space: nowrap;
}
.chip--more {
  border-style: dashed;
}

.json-block {
  margin: 0;
  padding: var(--bc-space-2) var(--bc-space-3);
  background: var(--bc-gray-2);
  border-radius: var(--bc-radius-sm);
  font-family: var(--bc-font-mono);
  font-size: var(--bc-text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow: auto;
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

@media (max-width: 1000px) {
  .row-1-2 {
    grid-template-columns: 1fr;
  }
}
</style>
