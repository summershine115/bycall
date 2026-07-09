<template>
  <div v-loading="loading">
    <el-row :gutter="20">
      <el-col :span="6" v-for="card in cards" :key="card.label">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon :size="32" :color="card.color"><component :is="card.icon" /></el-icon>
            <div>
              <div class="stat-num">{{ card.value }}</div>
              <div class="stat-label">{{ card.label }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 20px" v-if="activePrompt">
      <template #header>当前激活的提示词 (v{{ activePrompt.version }})</template>
      <pre class="prompt-preview">{{ activePrompt.content }}</pre>
    </el-card>

    <el-alert
      title="治理脊柱说明"
      type="info"
      :closable="false"
      style="margin-top: 20px"
      description="策略活在代码层不在提示词层。LLM 拿不到它无权拿的东西——不是'拿到后被拦'，是'请求时那个东西根本不在它的世界里'。可在「对话测试」用 act-as 验证权限链路。"
    />
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { admin } from '../api'

const loading = ref(true)
const counts = ref({ users: 0, roles: 0, prompts: 0, audit: 0 })
const activePrompt = ref(null)

const cards = computed(() => [
  { label: '用户数', value: counts.value.users, icon: 'User', color: '#409eff' },
  { label: '角色数', value: counts.value.roles, icon: 'Key', color: '#67c23a' },
  { label: '提示词版本', value: counts.value.prompts, icon: 'Document', color: '#e6a23c' },
  { label: '审计记录', value: counts.value.audit, icon: 'List', color: '#f56c6c' },
])

onMounted(async () => {
  try {
    const [users, roles, prompts, audit] = await Promise.all([
      admin.listUsers(),
      admin.listRoles(),
      admin.listPrompts(),
      admin.listAudit(500),
    ])
    counts.value = {
      users: users.length,
      roles: roles.length,
      prompts: prompts.length,
      audit: audit.length,
    }
    activePrompt.value = prompts.find((p) => p.is_active) || null
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.stat-card {
  display: flex;
  align-items: center;
  gap: 16px;
}
.stat-num {
  font-size: 28px;
  font-weight: 600;
  color: #303133;
}
.stat-label {
  font-size: 13px;
  color: #909399;
}
.prompt-preview {
  white-space: pre-wrap;
  font-family: inherit;
  margin: 0;
  color: #606266;
  line-height: 1.6;
}
</style>
