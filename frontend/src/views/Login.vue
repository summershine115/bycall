<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <template #header>
        <div class="login-title">
          <h2>Bycall 控制台</h2>
          <p class="subtitle">企业级数字员工 · 管理员登录</p>
        </div>
      </template>
      <el-form :model="form" @submit.prevent="onLogin" label-position="top">
        <el-form-item label="账号">
          <el-input v-model="form.name" placeholder="admin" :prefix-icon="User" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="admin123"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="onLogin"
          />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width: 100%" @click="onLogin">
          登录
        </el-button>
      </el-form>
      <p class="hint">默认账号 admin / admin123（首次部署后请改密码）</p>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { auth } from '../api'

const router = useRouter()
const loading = ref(false)
const form = reactive({ name: 'admin', password: '' })

async function onLogin() {
  loading.value = true
  try {
    await auth.login(form.name, form.password)
    ElMessage.success('登录成功')
    router.push('/')
  } catch {
    // 错误信息由 axios 拦截器统一提示
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  width: 380px;
}
.login-title {
  text-align: center;
}
.login-title h2 {
  margin: 0 0 8px;
  color: #303133;
}
.subtitle {
  margin: 0;
  color: #909399;
  font-size: 13px;
}
.hint {
  text-align: center;
  color: #c0c4cc;
  font-size: 12px;
  margin: 12px 0 0;
}
</style>
