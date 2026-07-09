<template>
  <div>
    <el-alert
      v-if="actAsInfo"
      type="warning"
      :closable="false"
      style="margin-bottom: 16px"
      show-icon
    >
      当前以 <strong>{{ actAsInfo }}</strong> 身份测试。所有权限判定按该用户的角色生效。
      <el-button size="small" @click="onClearActAs" style="margin-left: 12px">恢复 admin</el-button>
    </el-alert>

    <el-card>
      <template #header>对话测试（数据面 /chat）</template>
      <p class="muted">
        这里验证治理链路：身份 → 能力集 → ACL 预过滤 RAG → 控制面提示词 → 审计。
        在「用户管理」点「扮演」切换身份，可对比不同角色的可见范围。
      </p>

      <el-input
        v-model="question"
        placeholder="问一个业务问题，如：上季度销售额是多少？"
        @keyup.enter="onAsk"
      >
        <template #append>
          <el-button :loading="loading" @click="onAsk">提问</el-button>
        </template>
      </el-input>

      <el-descriptions
        v-if="result"
        :column="2"
        border
        style="margin-top: 16px"
        title="治理决策（透明化）"
      >
        <el-descriptions-item label="当前用户">{{ result.user }}</el-descriptions-item>
        <el-descriptions-item label="角色">{{ result.role }}</el-descriptions-item>
        <el-descriptions-item label="知识域权限">
          <el-tag v-for="t in result.knowledge_scope" :key="t" size="small" type="success" style="margin-right: 4px">
            {{ t }}
          </el-tag>
          <span v-if="!result.knowledge_scope.length" class="muted">无（默认隔离）</span>
        </el-descriptions-item>
        <el-descriptions-item label="命中授权 chunk">{{ result.retrieved_chunks }}</el-descriptions-item>
        <el-descriptions-item label="决策">
          <el-tag :type="result.decision === 'answered' ? 'success' : 'danger'">{{ result.decision }}</el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <el-card v-if="result" shadow="never" style="margin-top: 16px">
        <div class="answer">{{ result.answer }}</div>
      </el-card>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { admin, chat } from '../api'

const question = ref('')
const loading = ref(false)
const result = ref(null)
const actAsInfo = ref('')

async function onAsk() {
  if (!question.value.trim()) return
  loading.value = true
  try {
    result.value = await chat.ask(question.value)
    if (result.value.decision === 'denied_no_access') {
      ElMessage.warning('该用户无权访问相关知识（ACL 预过滤拦截）')
    }
  } finally {
    loading.value = false
  }
}

async function onClearActAs() {
  await admin.clearActAs()
  actAsInfo.value = ''
  ElMessage.success('已恢复 admin 身份')
  await detectActAs()
}

// 检测当前是否在 act-as 状态（通过尝试一个普通用户视角的 chat 判断较复杂，
// 简化：admin 身份下 result.role 会是 admin；这里仅提示，真实状态由后端 cookie 决定）
async function detectActAs() {
  // 无显式"当前会话"端点；act-as 状态由后端 cookie 持有，前端展示用上次 chat 结果推断
}
</script>

<style scoped>
.muted {
  color: #909399;
  font-size: 13px;
}
.answer {
  white-space: pre-wrap;
  line-height: 1.7;
  color: #303133;
}
</style>
