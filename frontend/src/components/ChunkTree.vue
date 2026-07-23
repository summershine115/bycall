<template>
  <!--
    切块层级树（父子块一一对应展示，参考 WeKnora 切块调试面板）。
    - 父子模式：parent 为可折叠组头卡片，其下经连接线/左侧色条缩进挂 child。
    - 非父子模式（无 parent）：child 平铺，分组头提示「未启用父子块」。
    - 孤儿 child（parent_id 指向不存在的 parent）：归入「未归组」区平铺。
    两种数据源兼容：预览 items 用 `id`、查看切块 items 用 `chunk_id` 作锚点。
  -->
  <div class="chunk-tree">
    <!-- 汇总统计 + 全部展开/收起（有多组时才有意义） -->
    <div class="chunk-tree__bar">
      <div class="chunk-tree__stats">
        <span class="chunk-tree__stat">
          <span class="chunk-tree__stat-num">{{ stats.parents }}</span> 父块
        </span>
        <span class="chunk-tree__divider" aria-hidden="true" />
        <span class="chunk-tree__stat">
          <span class="chunk-tree__stat-num">{{ stats.children }}</span> 子块
        </span>
        <span v-if="stats.orphans" class="chunk-tree__stat chunk-tree__stat--warn">
          · {{ stats.orphans }} 未归组
        </span>
        <span v-if="!isParentChild" class="chunk-tree__hint">未启用父子块</span>
      </div>
      <div v-if="groups.length > 1" class="chunk-tree__bar-actions">
        <el-button text size="small" @click="setAll(true)">全部展开</el-button>
        <el-button text size="small" @click="setAll(false)">全部收起</el-button>
      </div>
    </div>

    <!-- 层级分组（父子模式 / 或非父子模式的平铺区） -->
    <div class="chunk-tree__groups">
      <section
        v-for="group in groups"
        :key="group.key"
        class="chunk-group"
        :class="{ 'chunk-group--flat': group.flat }"
      >
        <!-- 组头：可折叠（平铺区不可折叠，仅作说明性标签） -->
        <button
          v-if="!group.flat"
          type="button"
          class="chunk-group__head"
          :aria-expanded="String(isOpen(group.key))"
          :aria-controls="`chunk-panel-${group.key}`"
          @click="toggle(group.key)"
        >
          <el-icon class="chunk-group__caret" :class="{ 'is-open': isOpen(group.key) }" aria-hidden="true">
            <ArrowRight />
          </el-icon>

          <span class="chunk-group__index mono">#{{ group.parent.chunk_index }}</span>
          <el-tag size="small" type="warning" class="chunk-group__tag">parent</el-tag>
          <span class="chunk-group__meta">
            {{ charCountOf(group.parent) }} 字 · {{ group.children.length }} 子块
          </span>

          <!-- parent 正文预览（截断一行；组头本体仍是整颗按钮，键盘可达） -->
          <span class="chunk-group__preview">{{ textOf(group.parent) }}</span>

          <el-tooltip
            :content="isExpandedText(group.key) ? '收起 parent 全文' : '展开 parent 全文'"
            placement="top"
            :show-after="300"
          >
            <span
              class="chunk-group__expand-text"
              role="button"
              tabindex="0"
              :aria-label="isExpandedText(group.key) ? '收起 parent 全文' : '展开 parent 全文'"
              @click.stop="toggleText(group.key)"
              @keydown.enter.stop.prevent="toggleText(group.key)"
              @keydown.space.stop.prevent="toggleText(group.key)"
            >
              <el-icon :size="13" aria-hidden="true">
                <ArrowRight v-if="isExpandedText(group.key)" style="transform: rotate(-90deg)" />
                <ArrowRight v-else style="transform: rotate(90deg)" />
              </el-icon>
            </span>
          </el-tooltip>
        </button>

        <!-- 平铺区组头（非父子模式 / 未归组）：仅说明，不可折叠 -->
        <div v-else class="chunk-group__flat-head">
          <span class="chunk-group__flat-label">{{ group.label }}</span>
          <span class="chunk-group__flat-meta">{{ group.children.length }} 块</span>
        </div>

        <!-- parent 全文（独立于 child 折叠，默认收起避免长 parent 刷屏） -->
        <div
          v-if="!group.flat && isExpandedText(group.key)"
          class="chunk-group__parent-text"
        >
          {{ textOf(group.parent) }}
        </div>

        <!-- child 列表：折叠过渡 -->
        <el-collapse-transition>
          <div
            v-show="group.flat || isOpen(group.key)"
            :id="`chunk-panel-${group.key}`"
            class="chunk-group__body"
            role="region"
            :aria-label="group.flat ? group.label : `父块 #${group.parent.chunk_index} 的子块`"
          >
            <article
              v-for="child in group.children"
              :key="childKey(child)"
              class="chunk-child"
              :class="{ 'chunk-child--flat': group.flat }"
            >
              <div class="chunk-child__meta">
                <span class="mono">#{{ child.chunk_index }}</span>
                <el-tag size="small">{{ child.chunk_type || 'child' }}</el-tag>
                <span class="chunk-child__chars">{{ charCountOf(child) }} 字</span>
              </div>
              <p class="chunk-child__text">{{ textOf(child) }}</p>
            </article>
          </div>
        </el-collapse-transition>
      </section>
    </div>

    <!-- 完全无数据 -->
    <EmptyState
      v-if="!items.length"
      icon="Grid"
      title="无切块数据"
      description="该文档尚未入库或切块为空。"
    />
  </div>
</template>

<script setup>
// 切块层级树：把平铺 chunk 列表按 parent_id（或 id 命名规律）组织成 parent→children 分组。
// 设计要点：
//  1) 归属判定双通道——优先用 child.parent_id === parent[idKey]（显式契约）；
//     落空时回退到入库命名规律 `…_child_{pi}_{ci}` → `…_parent_{pi}`（持久化块 parent_id 未写库时的兜底）。
//  2) parent 组头默认收起 parent 全文（长上下文不刷屏）、默认展开 child 列表（管理员主要看 child 粒度）。
//  3) 折叠用 el-collapse-transition；组头是原生 <button>，键盘可达 + aria-expanded/controls。
//  4) parent 重样式（teal 左边条 + 暖灰底 + 粗字号），child 轻量缩进经连接线/色条体现从属。
import { ref, computed } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import EmptyState from './EmptyState.vue'

const props = defineProps({
  // chunk 列表（预览或已入库）；每项含 chunk_type/chunk_index/char_count/text/parent_id + id 或 chunk_id
  items: { type: Array, default: () => [] },
  // 锚点 id 字段名：预览用 'id'、查看切块用 'chunk_id'
  idKey: { type: String, default: 'id' },
})

// 取锚点 id：优先 idKey，兼容两种数据源互用
function idOf(item) {
  return item?.[props.idKey] ?? item?.id ?? item?.chunk_id ?? ''
}
function childKey(item) {
  return idOf(item) || `idx-${item?.chunk_index}`
}
function textOf(item) {
  return item?.text ?? item?.content ?? ''
}
function charCountOf(item) {
  return item?.char_count ?? (item?.content?.length || 0)
}

// 命名规律兜底：从 child id `…_child_{pi}_{ci}` 推出 parent id `…_parent_{pi}`
function parentIdFromPattern(childId) {
  if (typeof childId !== 'string') return ''
  const m = childId.match(/^(.*)_child_(\d+)_\d+$/)
  return m ? `${m[1]}_parent_${m[2]}` : ''
}

// 分组：parents 按 chunk_index 排序挂 children；无 parent 时整体平铺
const groups = computed(() => {
  const items = props.items || []
  const parents = items
    .filter((c) => c.chunk_type === 'parent')
    .sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0))
  const children = items.filter((c) => c.chunk_type !== 'parent')

  // 非父子模式：单组平铺
  if (!parents.length) {
    if (!children.length) return []
    return [
      {
        key: '__flat__',
        flat: true,
        label: '全部切块',
        parent: null,
        children: [...children].sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0)),
      },
    ]
  }

  const parentById = new Map(parents.map((p) => [String(idOf(p)), p]))
  const buckets = new Map(parents.map((p) => [String(idOf(p)), []]))
  const orphans = []

  for (const child of children) {
    // 通道 1：显式 parent_id
    let pid = child.parent_id ? String(child.parent_id) : ''
    if (pid && !parentById.has(pid)) pid = '' // parent_id 指向不存在的 parent → 视为未归组
    // 通道 2：命名规律兜底
    if (!pid) {
      const guess = parentIdFromPattern(idOf(child))
      if (guess && parentById.has(guess)) pid = guess
    }
    if (pid && buckets.has(pid)) buckets.get(pid).push(child)
    else orphans.push(child)
  }

  const result = parents.map((p) => {
    const key = String(idOf(p))
    const kids = (buckets.get(key) || []).sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0))
    return { key, flat: false, parent: p, children: kids }
  })

  if (orphans.length) {
    result.push({
      key: '__orphan__',
      flat: true,
      label: '未归组切块',
      parent: null,
      children: orphans.sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0)),
    })
  }
  return result
})

const isParentChild = computed(() => (props.items || []).some((c) => c.chunk_type === 'parent'))

const stats = computed(() => ({
  parents: groups.value.filter((g) => !g.flat).length,
  children: (props.items || []).filter((c) => c.chunk_type !== 'parent').length,
  orphans: groups.value.find((g) => g.key === '__orphan__')?.children.length || 0,
}))

// 折叠状态：child 列表默认展开（admin 调试主场景）；parent 全文默认收起。
// 用 Set 存「被显式收起」的组 key，默认展开 → 新组出现无需初始化。
const collapsedKeys = ref(new Set())
const expandedTextKeys = ref(new Set())

function isOpen(key) {
  return !collapsedKeys.value.has(key)
}
function toggle(key) {
  const next = new Set(collapsedKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedKeys.value = next
}
function setAll(open) {
  if (open) {
    collapsedKeys.value = new Set()
  } else {
    collapsedKeys.value = new Set(groups.value.filter((g) => !g.flat).map((g) => g.key))
  }
}
function isExpandedText(key) {
  return expandedTextKeys.value.has(key)
}
function toggleText(key) {
  const next = new Set(expandedTextKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedTextKeys.value = next
}
</script>

<style scoped>
/* ===== 汇总条 ===== */
.chunk-tree__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  margin-bottom: var(--bc-space-3);
}
.chunk-tree__stats {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.chunk-tree__stat-num {
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
}
.chunk-tree__stat--warn {
  color: var(--bc-warning-text);
}
.chunk-tree__divider {
  width: 1px;
  height: 12px;
  background: var(--bc-border-strong);
}
.chunk-tree__hint {
  margin-left: var(--bc-space-1);
  padding: 1px var(--bc-space-2);
  border-radius: var(--bc-radius-sm);
  background: var(--bc-info-bg);
  color: var(--bc-text-2);
}
.chunk-tree__bar-actions {
  display: flex;
  gap: var(--bc-space-1);
}

/* ===== 分组容器 ===== */
.chunk-tree__groups {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-3);
}
.chunk-group {
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  background: var(--bc-bg-surface);
  overflow: hidden;
}

/* ===== parent 组头（整颗为原生 button，键盘可达） ===== */
.chunk-group__head {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  width: 100%;
  padding: var(--bc-space-2) var(--bc-space-3);
  border: none;
  /* parent 重样式：teal 左边条 + 暖灰底 */
  border-left: 3px solid var(--bc-primary-6);
  background: var(--bc-gray-2);
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: var(--bc-text-1);
  transition: background var(--bc-duration-fast) var(--bc-ease-out);
}
.chunk-group__head:hover {
  background: var(--bc-gray-3);
}
.chunk-group__head:focus-visible {
  outline: 2px solid var(--bc-primary-6);
  outline-offset: -2px;
}
.chunk-group__caret {
  flex-shrink: 0;
  color: var(--bc-text-3);
  transition: transform var(--bc-duration-normal) var(--bc-ease-out);
}
.chunk-group__caret.is-open {
  transform: rotate(90deg);
}
.chunk-group__index {
  flex-shrink: 0;
  font-weight: var(--bc-weight-semibold);
  font-size: var(--bc-text-sm);
}
.chunk-group__tag {
  flex-shrink: 0;
}
.chunk-group__meta {
  flex-shrink: 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  white-space: nowrap;
}
.chunk-group__preview {
  flex: 1;
  min-width: 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chunk-group__expand-text {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--bc-radius-sm);
  color: var(--bc-text-3);
  cursor: pointer;
  transition: background var(--bc-duration-fast) var(--bc-ease-out), color var(--bc-duration-fast) var(--bc-ease-out);
}
.chunk-group__expand-text:hover {
  background: var(--bc-primary-1);
  color: var(--bc-primary-7);
}

/* parent 全文（独立于 child 折叠） */
.chunk-group__parent-text {
  padding: var(--bc-space-3);
  border-top: 1px dashed var(--bc-border);
  background: var(--bc-gray-1);
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-relaxed);
  color: var(--bc-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ===== child 列表：缩进 + 连接线/色条体现从属 ===== */
.chunk-group__body {
  padding: var(--bc-space-2) var(--bc-space-3) var(--bc-space-3);
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-2);
}
.chunk-child {
  position: relative;
  margin-left: var(--bc-space-6);
  padding: var(--bc-space-2) var(--bc-space-3);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  background: var(--bc-gray-1);
  transition: border-color var(--bc-duration-fast) var(--bc-ease-out);
}
/* 左侧色条：从属 parent 的视觉锚 */
.chunk-child::before {
  content: '';
  position: absolute;
  left: -1px;
  top: var(--bc-space-2);
  bottom: var(--bc-space-2);
  width: 2px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-primary-3);
}
/* 连接线：从组头连到每条 child 的横向引导线 */
.chunk-child::after {
  content: '';
  position: absolute;
  left: calc(-1 * var(--bc-space-6));
  top: var(--bc-space-4);
  width: calc(var(--bc-space-6) - var(--bc-space-2));
  height: 1px;
  background: var(--bc-border-strong);
}
.chunk-child:hover {
  border-color: var(--bc-border-strong);
}
.chunk-child__meta {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  margin-bottom: var(--bc-space-1);
}
.chunk-child__chars {
  color: var(--bc-text-4);
}
.chunk-child__text {
  margin: 0;
  font-size: var(--bc-text-sm);
  line-height: var(--bc-leading-relaxed);
  color: var(--bc-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ===== 平铺区（非父子 / 未归组）：去层级化，与层级 child 区分 ===== */
.chunk-group--flat {
  border-style: dashed;
  background: var(--bc-bg-sunken);
}
.chunk-group__flat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--bc-space-2) var(--bc-space-3);
  background: var(--bc-gray-2);
  border-bottom: 1px solid var(--bc-border);
}
.chunk-group--flat .chunk-group__body {
  padding: var(--bc-space-3);
}
.chunk-group__flat-label {
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-2);
  letter-spacing: 0.02em;
}
.chunk-group__flat-meta {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.chunk-child--flat {
  margin-left: 0;
  background: var(--bc-bg-surface);
}
.chunk-child--flat::before,
.chunk-child--flat::after {
  display: none;
}
</style>
