<template>
  <el-container class="layout">
    <el-aside width="210px" class="sidebar">
      <div class="logo">Bycall</div>
      <el-menu :default-active="route.name" router>
        <el-menu-item index="dashboard" :route="{ name: 'dashboard' }">
          <el-icon><DataLine /></el-icon><span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="users" :route="{ name: 'users' }">
          <el-icon><User /></el-icon><span>用户管理</span>
        </el-menu-item>
        <el-menu-item index="roles" :route="{ name: 'roles' }">
          <el-icon><Key /></el-icon><span>角色与权限</span>
        </el-menu-item>
        <el-menu-item index="prompts" :route="{ name: 'prompts' }">
          <el-icon><Document /></el-icon><span>提示词</span>
        </el-menu-item>
        <el-menu-item index="settings" :route="{ name: 'settings' }">
          <el-icon><Setting /></el-icon><span>运行时设置</span>
        </el-menu-item>
        <el-menu-item index="audit" :route="{ name: 'audit' }">
          <el-icon><List /></el-icon><span>审计日志</span>
        </el-menu-item>
        <el-menu-item index="chat" :route="{ name: 'chat' }">
          <el-icon><ChatDotRound /></el-icon><span>对话测试</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <span class="page-title">{{ pageTitle }}</span>
        <el-button type="text" @click="onLogout">
          <el-icon><SwitchButton /></el-icon> 退出
        </el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { auth } from '../api'

const route = useRoute()
const router = useRouter()

const titles = {
  dashboard: '仪表盘',
  users: '用户管理',
  roles: '角色与权限',
  prompts: '提示词管理',
  settings: '运行时设置',
  audit: '审计日志',
  chat: '对话测试',
}
const pageTitle = computed(() => titles[route.name] || 'Bycall')

async function onLogout() {
  await auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.layout {
  min-height: 100vh;
}
.sidebar {
  background: #304156;
}
.logo {
  height: 60px;
  line-height: 60px;
  text-align: center;
  color: #fff;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 2px;
}
.el-menu {
  border-right: none;
  background: #304156;
}
:deep(.el-menu-item) {
  color: #bfcbd9;
}
:deep(.el-menu-item.is-active) {
  background: #1f2d3d;
  color: #409eff;
}
.header {
  background: #fff;
  border-bottom: 1px solid #e6e6e6;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.page-title {
  font-size: 18px;
  font-weight: 500;
}
</style>
