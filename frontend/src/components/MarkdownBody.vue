<template>
  <!-- v-html 渲染 marked 输出；内容来自自有 LLM，页面仅 admin 可访问，
       威胁模型不支持存储型 XSS，故不引入 DOMPurify。代码块内 HTML 由 marked 自动转义。 -->
  <div class="md-body" v-html="html" />
</template>

<script setup>
import { computed } from 'vue'
import { marked } from 'marked'

// GFM 表格/删除线 + 单换行转 <br>（对话场景更自然）。
marked.use({ gfm: true, breaks: true })

const props = defineProps({
  content: { type: String, default: '' },
})

const html = computed(() => marked.parse(props.content || ''))
</script>

<style scoped>
.md-body {
  font-size: var(--bc-text-md);
  line-height: var(--bc-leading-relaxed);
  color: var(--bc-text-1);
  word-break: break-word;
}
.md-body :deep(p) {
  margin: 0 0 var(--bc-space-3);
}
.md-body :deep(p:last-child) {
  margin-bottom: 0;
}
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4),
.md-body :deep(h5) {
  margin: var(--bc-space-5) 0 var(--bc-space-2);
  font-weight: var(--bc-weight-semibold);
  line-height: var(--bc-leading-tight);
}
.md-body :deep(h1:first-child),
.md-body :deep(h2:first-child),
.md-body :deep(h3:first-child) {
  margin-top: 0;
}
.md-body :deep(h1) {
  font-size: var(--bc-text-xl);
}
.md-body :deep(h2) {
  font-size: var(--bc-text-lg);
}
.md-body :deep(h3),
.md-body :deep(h4),
.md-body :deep(h5) {
  font-size: var(--bc-text-md);
}
.md-body :deep(ul),
.md-body :deep(ol) {
  margin: 0 0 var(--bc-space-3);
  padding-left: var(--bc-space-5);
}
.md-body :deep(li) {
  margin: var(--bc-space-1) 0;
}
.md-body :deep(li > p) {
  margin: 0;
}
.md-body :deep(code) {
  font-family: var(--bc-font-mono);
  font-size: 0.9em;
  background: var(--bc-gray-2);
  padding: 2px 5px;
  border-radius: var(--bc-radius-sm);
}
.md-body :deep(pre) {
  margin: 0 0 var(--bc-space-3);
  padding: var(--bc-space-3) var(--bc-space-4);
  background: var(--bc-gray-1);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  overflow-x: auto;
}
.md-body :deep(pre code) {
  background: none;
  padding: 0;
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-normal);
}
.md-body :deep(blockquote) {
  margin: 0 0 var(--bc-space-3);
  padding: var(--bc-space-1) var(--bc-space-4);
  border-left: 3px solid var(--bc-border);
  color: var(--bc-text-3);
}
.md-body :deep(blockquote p:last-child) {
  margin-bottom: 0;
}
.md-body :deep(table) {
  border-collapse: collapse;
  margin: 0 0 var(--bc-space-3);
  font-size: var(--bc-text-sm);
  display: table;
  width: auto;
  max-width: 100%;
}
.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid var(--bc-border);
  padding: var(--bc-space-2) var(--bc-space-3);
  text-align: left;
}
.md-body :deep(th) {
  background: var(--bc-gray-1);
  font-weight: var(--bc-weight-medium);
}
.md-body :deep(a) {
  color: var(--bc-primary-6);
  text-decoration: none;
}
.md-body :deep(a:hover) {
  text-decoration: underline;
}
.md-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--bc-border);
  margin: var(--bc-space-4) 0;
}
.md-body :deep(strong) {
  font-weight: var(--bc-weight-semibold);
}
</style>
