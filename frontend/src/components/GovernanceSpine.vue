<template>
  <!-- 治理脊柱：四道防线的概念图，传达"策略在代码层"的核心设计哲学 -->
  <div class="spine">
    <div class="spine__track" role="list">
      <div v-for="(item, i) in items" :key="item.title" class="spine__step" role="listitem">
        <div class="spine__num" aria-hidden="true">{{ i + 1 }}</div>
        <div class="spine__body">
          <div class="spine__title">
            {{ item.title }}
            <StatusBadge type="ok" :text="item.status" />
          </div>
          <div class="spine__desc">{{ item.desc }}</div>
        </div>
        <el-icon v-if="i < items.length - 1" class="spine__arrow" :size="14" aria-hidden="true">
          <ArrowRight />
        </el-icon>
      </div>
    </div>
    <p class="spine__foot">
      策略绝不能活在提示词层 —— LLM 拿不到它无权拿的东西：不是「拿到后被拦」，而是请求时那个资源根本不在它的世界里。
    </p>
  </div>
</template>

<script setup>
import StatusBadge from './StatusBadge.vue'

// 治理脊柱四道防线（静态概念呈现；动态数值由仪表盘其他版块承担）
const items = [
  { title: '身份识别', status: '强制', desc: 'IM 消息经绑定关系解析为组织内用户' },
  { title: 'RBAC 预过滤', status: '前置', desc: '工具列表与知识域在请求前按角色裁剪' },
  { title: 'ABAC 职级', status: '生效', desc: '机密资源按 resource_level ≤ user_level 放行' },
  { title: '审计留痕', status: '全量', desc: '放行与拒绝皆落审计日志，可追溯' },
]
</script>

<style scoped>
.spine {
  background: var(--bc-bg-surface);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-lg);
  box-shadow: var(--bc-shadow-xs);
  padding: var(--bc-space-5) var(--bc-space-6);
}
.spine__track {
  display: flex;
  align-items: stretch;
  gap: var(--bc-space-3);
}
.spine__step {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  padding: var(--bc-space-4);
  background: var(--bc-gray-1);
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
}
.spine__num {
  width: 26px;
  height: 26px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-primary-6);
  color: var(--bc-bg-surface);
  font-size: var(--bc-text-xs);
  font-weight: var(--bc-weight-semibold);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.spine__body {
  min-width: 0;
}
.spine__title {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  font-size: var(--bc-text-sm);
  font-weight: var(--bc-weight-semibold);
  color: var(--bc-text-1);
  flex-wrap: wrap;
}
.spine__desc {
  margin-top: var(--bc-space-1);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  line-height: var(--bc-leading-normal);
}
.spine__arrow {
  color: var(--bc-text-4);
  flex-shrink: 0;
  align-self: center;
  margin: 0 calc(-1 * var(--bc-space-1));
}
.spine__foot {
  margin: var(--bc-space-4) 0 0;
  font-size: var(--bc-text-sm);
  color: var(--bc-text-3);
  line-height: var(--bc-leading-relaxed);
  padding-left: var(--bc-space-3);
  border-left: 2px solid var(--bc-primary-4);
}
@media (max-width: 1280px) {
  .spine__track {
    flex-wrap: wrap;
  }
  .spine__step {
    flex: 1 1 44%;
  }
  .spine__arrow {
    display: none;
  }
}
</style>
