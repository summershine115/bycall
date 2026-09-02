<template>
  <div class="login">
    <!-- 左侧品牌区：传达产品定位（治理/可控/可审计） -->
    <section class="login__brand" aria-hidden="true">
      <div class="brand">
        <div class="brand__mark">
          <div class="brand__logo">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
              <path
                d="M7 15.5V8.5h2.2v2.6l3.4-2.6h2.9l-3.9 3 4.1 4h-2.9l-2.8-2.9-1 0.8v2.1H7z"
                fill="currentColor"
              />
            </svg>
          </div>
          <span class="brand__name">Bycall</span>
        </div>

        <h1 class="brand__headline">
          数字员工的<br />治理控制台
        </h1>
        <p class="brand__lede">
          人格、能力、知识、权限、审计 —— 策略活在代码层，不在提示词层。
        </p>

        <ul class="brand__points">
          <li v-for="p in points" :key="p.title" class="brand__point">
            <el-icon :size="16" class="brand__point-icon"><component :is="p.icon" /></el-icon>
            <div>
              <div class="brand__point-title">{{ p.title }}</div>
              <div class="brand__point-desc">{{ p.desc }}</div>
            </div>
          </li>
        </ul>
      </div>
      <footer class="brand__foot">企业级数字员工系统 · 管理控制面</footer>
    </section>

    <!-- 右侧登录表单 -->
    <section class="login__panel">
      <div class="panel">
        <header class="panel__head">
          <h2 class="panel__title">管理员登录</h2>
          <p class="panel__sub">使用控制台账号登录，管理组织内的数字员工</p>
        </header>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          size="large"
          @submit.prevent="onLogin"
        >
          <el-form-item label="账号" prop="name">
            <el-input
              v-model="form.name"
              placeholder="请输入管理员账号"
              :prefix-icon="User"
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              :prefix-icon="Lock"
              show-password
              autocomplete="current-password"
              @keyup.enter="onLogin"
            />
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            class="panel__submit"
            :loading="loading"
            native-type="submit"
          >
            登录控制台
          </el-button>
        </el-form>

        <p class="panel__hint">
          <el-icon :size="13" aria-hidden="true"><Lock /></el-icon>
          首次部署请联系管理员获取初始密码，并及时修改
        </p>
      </div>
      <footer class="panel__foot">Bycall Admin Console</footer>
    </section>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { User, Lock, Document, RefreshLeft } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { auth } from '../api'

const router = useRouter()
const formRef = ref()
const loading = ref(false)
const form = reactive({ name: '', password: '' })

const rules = {
  name: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 品牌区卖点：与治理脊柱设计哲学一致
const points = [
  { icon: Lock, title: '权限前置隔离', desc: 'LLM 看不到无权访问的资源，而非拿到后被拦' },
  { icon: Document, title: '全链路可审计', desc: '谁在何时问了什么、被放行或拒绝，皆有记录' },
  { icon: RefreshLeft, title: '人格版本化', desc: '系统提示词版本管理，随时回滚' },
]

async function onLogin() {
  // 回车会同时触发原生 submit 和 keyup.enter：loading 守卫防止双重提交
  if (loading.value) return
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return // 表单校验未通过，不发起请求
  }
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
.login {
  display: flex;
  min-height: 100vh;
  background: var(--bc-bg-page);
}

/* ============ 左侧品牌区（mono 主题下为纯黑；evergreen 保留深青绿） ============ */
.login__brand {
  flex: 1.1;
  background: var(--bc-primary-9);
  color: var(--bc-primary-1);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: var(--bc-space-12) var(--bc-space-16);
  position: relative;
  overflow: hidden;
}
/* 细网格纹理，增加质感而不花哨 */
.login__brand::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
}
.brand {
  position: relative;
  max-width: 480px;
  margin-top: var(--bc-space-8);
}
/* 品牌区内容逐行淡入上移（入场动画） */
.brand__mark,
.brand__headline,
.brand__lede,
.brand__points,
.brand__foot {
  animation: login-rise var(--bc-duration-slow) var(--bc-ease-out) both;
}
.brand__headline { animation-delay: 80ms; }
.brand__lede { animation-delay: 160ms; }
.brand__points { animation-delay: 240ms; }
.brand__foot { animation-delay: 320ms; }
@keyframes login-rise {
  from {
    opacity: 0;
    transform: translateY(16px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.brand__mark {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
}
.brand__logo {
  width: 40px;
  height: 40px;
  border-radius: var(--bc-radius-md);
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.16);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--bc-primary-1);
}
.brand__name {
  font-size: var(--bc-text-xl);
  font-weight: var(--bc-weight-bold);
  letter-spacing: 0.02em;
}
.brand__headline {
  margin: var(--bc-space-12) 0 0;
  font-size: 40px;
  font-weight: var(--bc-weight-bold);
  line-height: 1.2;
  letter-spacing: -0.02em;
  color: var(--bc-on-dark-1);
}
.brand__lede {
  margin: var(--bc-space-5) 0 0;
  font-size: var(--bc-text-lg);
  line-height: var(--bc-leading-relaxed);
  color: var(--bc-primary-3);
}
.brand__points {
  list-style: none;
  margin: var(--bc-space-12) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-6);
}
.brand__point {
  display: flex;
  gap: var(--bc-space-3);
  align-items: flex-start;
}
.brand__point-icon {
  margin-top: 3px;
  color: var(--bc-primary-4);
  flex-shrink: 0;
}
.brand__point-title {
  font-size: var(--bc-text-md);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-primary-1);
}
.brand__point-desc {
  font-size: var(--bc-text-sm);
  color: var(--bc-primary-4);
  margin-top: 2px;
}
.brand__foot {
  position: relative;
  font-size: var(--bc-text-xs);
  color: var(--bc-primary-5);
}

/* mono 主题：品牌区文字层级用灰阶而非彩色 */
:root[data-theme='mono'] .brand__lede {
  color: rgba(255, 255, 255, 0.6);
}
:root[data-theme='mono'] .brand__point-icon {
  color: rgba(255, 255, 255, 0.5);
}
:root[data-theme='mono'] .brand__point-title {
  color: rgba(255, 255, 255, 0.92);
}
:root[data-theme='mono'] .brand__point-desc {
  color: rgba(255, 255, 255, 0.45);
}
:root[data-theme='mono'] .brand__foot {
  color: rgba(255, 255, 255, 0.35);
}
:root[data-theme='mono'] .brand__logo {
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.12);
  color: #fff;
}
:root[data-theme='mono'] .brand__name {
  color: #fff;
}

/* ============ 右侧表单区 ============ */
.login__panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--bc-space-8);
  background: var(--bc-bg-surface);
}
.panel {
  width: 100%;
  max-width: 380px;
  animation: login-rise var(--bc-duration-slow) var(--bc-ease-out) 120ms both;
}
.panel__head {
  margin-bottom: var(--bc-space-8);
}
.panel__title {
  font-size: var(--bc-text-2xl);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.panel__sub {
  margin: var(--bc-space-2) 0 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
}
.panel__submit {
  width: 100%;
  margin-top: var(--bc-space-2);
}
.panel__hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--bc-space-1);
  margin: var(--bc-space-6) 0 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
}
.panel__foot {
  margin-top: var(--bc-space-12);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-4);
}

/* 窄屏：隐藏品牌区，表单居中 */
@media (max-width: 720px) {
  .login__brand {
    display: none;
  }
}
</style>
