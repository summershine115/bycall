<template>
  <!-- 模型列表：单个类型（chat/embedding/rerank）的模型表格 + 空态 -->
  <div class="model-table">
    <EmptyState
      v-if="!models.length"
      icon="Cpu"
      title="该类型下还没有模型"
      description="添加模型后可测试连接、激活使用；激活的模型即当前生效配置。"
    >
      <el-button type="primary" :icon="Plus" @click="$emit('add')">添加模型</el-button>
    </EmptyState>

    <el-table v-else :data="models">
      <el-table-column label="名称" min-width="180">
        <template #default="{ row }">
          <div class="model-cell">
            <span class="bc-strong">{{ row.display_name || row.name }}</span>
            <span class="bc-muted bc-text-xs mono">{{ row.name }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="provider" label="服务商" width="130">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.provider }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="API 地址" min-width="220">
        <template #default="{ row }">
          <span class="bc-text-sm bc-muted mono">{{ row.base_url || '(空)' }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="modelType === 'embedding'" label="维度" width="80" align="center">
        <template #default="{ row }">{{ row.embed_dimension || '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <StatusBadge v-if="row.is_active" type="ok" text="激活中" />
          <span v-else class="bc-muted bc-text-sm">未激活</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="290" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            :type="row.is_active ? 'info' : 'success'"
            :plain="!row.is_active"
            :disabled="row.is_active"
            @click="$emit('activate', row)"
          >
            {{ row.is_active ? '已激活' : '激活' }}
          </el-button>
          <el-button size="small" @click="$emit('test', row)">测试</el-button>
          <el-button size="small" @click="$emit('edit', row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="$emit('delete', row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
// 模型列表表格：从 Models.vue 抽出的展示组件
import { Plus } from '@element-plus/icons-vue'
import EmptyState from './EmptyState.vue'
import StatusBadge from './StatusBadge.vue'

defineProps({
  modelType: { type: String, required: true },
  models: { type: Array, default: () => [] },
})
defineEmits(['add', 'edit', 'activate', 'test', 'delete'])
</script>

<style scoped>
.model-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
</style>
