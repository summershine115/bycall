<script setup>
/**
 * 顶栏明暗切换钮：light ⇄ dark。
 * 沿用 useTheme 的模块级单例状态（mode / setMode），
 * 手动切换会覆盖 auto 并持久化到 bycall.mode。
 */
import { computed } from 'vue'
import { Moon, Sunny } from '@element-plus/icons-vue'
import { mode, setMode } from '../composables/useTheme'

const isDark = computed(() => mode.value === 'dark')

function toggle() {
  setMode(isDark.value ? 'light' : 'dark')
}
</script>

<template>
  <button
    type="button"
    class="mode-toggle"
    :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'"
    :title="isDark ? '切换到浅色模式' : '切换到深色模式'"
    @click="toggle"
  >
    <el-icon :size="15" aria-hidden="true">
      <Sunny v-if="isDark" />
      <Moon v-else />
    </el-icon>
  </button>
</template>

<style scoped>
.mode-toggle {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  background: var(--bc-bg-surface);
  color: var(--bc-text-2);
  cursor: pointer;
  transition:
    color var(--bc-duration-fast) var(--bc-ease-out),
    border-color var(--bc-duration-fast) var(--bc-ease-out);
}
.mode-toggle:hover {
  color: var(--bc-text-1);
  border-color: var(--bc-border-strong);
}
.mode-toggle:focus-visible {
  outline: 2px solid var(--bc-primary-6);
  outline-offset: 2px;
}
.mode-toggle:active {
  transform: scale(0.96);
}
</style>
