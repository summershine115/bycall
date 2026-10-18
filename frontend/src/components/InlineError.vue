<template>
  <div v-if="message" class="inline-error" role="alert">
    <span>{{ message }}</span>
    <el-button v-if="retryable" size="small" :loading="busy" @click="$emit('retry')">重试</el-button>
  </div>
</template>
<script setup>
defineProps({ message: String, busy: Boolean, retryable: { type: Boolean, default: true } })
defineEmits(['retry'])
</script>
<style scoped>
.inline-error { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; padding:12px 16px; margin:12px 0; border:1px solid var(--bc-border); border-left:3px solid var(--bc-danger); border-radius:var(--bc-radius-md); background:var(--bc-bg-surface); font-size:14px; line-height:1.6; overflow-wrap:anywhere; }
</style>
