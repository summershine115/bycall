<template>
  <div class="bc-page">
    <PageHeader
      title="策略模拟"
      description="预演某用户对检索 / 工具调用的治理判定，验证治理脊柱实际会放行还是拒绝。"
    />

    <div class="bc-panel">
      <el-form label-position="top" inline>
        <el-form-item label="用户">
          <el-select v-model="userId" filterable placeholder="选择用户" style="width: 220px">
            <el-option v-for="u in users" :key="u.id" :label="`${u.name} (${u.role})`" :value="u.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="动作">
          <el-radio-group v-model="action">
            <el-radio-button value="retrieve">检索知识</el-radio-button>
            <el-radio-button value="tool_call">调用工具</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="action === 'tool_call'" label="工具名">
          <el-input v-model="toolName" placeholder="如 send_email" style="width: 200px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">模拟</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div v-if="result" class="bc-panel">
      <h3>判定结果</h3>
      <el-tag :type="result.decision === 'allowed' ? 'success' : 'danger'" size="large">
        {{ result.decision === 'allowed' ? '允许 (allowed)' : '拒绝 (denied)' }}
      </el-tag>
      <p class="bc-muted">{{ result.detail }}</p>

      <el-descriptions :column="2" border style="margin-top: 16px">
        <el-descriptions-item label="角色">{{ result.role }}</el-descriptions-item>
        <el-descriptions-item label="职级">{{ result.level }}</el-descriptions-item>
        <el-descriptions-item label="admin">{{ result.is_admin ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="可见知识域">{{ result.knowledge_scope_tags.join('、') || '（空）' }}</el-descriptions-item>
        <el-descriptions-item label="可用工具">{{ result.allowed_tools.join('、') || '（空）' }}</el-descriptions-item>
      </el-descriptions>

      <h4 style="margin: 16px 0 8px">召回前过滤条件</h4>
      <pre v-if="result.retrieval_filters.length" class="bc-code">{{ JSON.stringify(result.retrieval_filters, null, 2) }}</pre>
      <p v-else class="bc-muted">无过滤（admin / 通配）</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'

const users = ref([])
const userId = ref(null)
const action = ref('retrieve')
const toolName = ref('')
const loading = ref(false)
const result = ref(null)

onMounted(async () => {
  users.value = await admin.listUsers()
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
  } finally {
    loading.value = false
  }
}
</script>