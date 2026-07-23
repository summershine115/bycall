<template>
  <div class="bc-page">
    <PageHeader
      title="文档管理"
      description="文档按知识库隔离入库；source 标签即 ACL 预过滤边界 —— 只有知识域包含它的角色才能检索到。"
    >
      <template v-if="kbId">
        <el-button :icon="Grid" @click="openPreview">切块预览</el-button>
        <el-button :icon="Setting" @click="openSettings">库设置</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">添加文档</el-button>
      </template>
    </PageHeader>

    <ContentLoader v-if="loading" :rows="5" />

    <template v-else>
      <!-- 知识库上下文头：当前库 + 关键指标 + 切换 -->
      <div class="kb-context bc-panel">
        <div class="kb-context__main">
          <div class="kb-context__picker">
            <el-icon :size="18" class="kb-context__icon" aria-hidden="true"><Collection /></el-icon>
            <el-select
              v-model="kbId"
              placeholder="选择一个知识库"
              class="kb-context__select"
              @change="onKbChange"
            >
              <el-option
                v-for="kb in kbs"
                :key="kb.id"
                :label="`${kb.name}（${kb.dimension}维）`"
                :value="kb.id"
              />
            </el-select>
          </div>
          <p v-if="currentKb?.description" class="kb-context__desc">{{ currentKb.description }}</p>
        </div>
        <template v-if="currentKb">
          <div class="kb-context__stats">
            <div class="bc-kv">
              <span class="bc-kv__label">文档</span>
              <span class="bc-kv__value">{{ currentKb.document_count ?? 0 }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">切块</span>
              <span class="bc-kv__value">{{ currentKb.chunk_count ?? 0 }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">向量维度</span>
              <span class="bc-kv__value">{{ currentKb.dimension || '—' }}</span>
            </div>
          </div>
          <div class="kb-context__actions">
            <el-upload
              :show-file-list="false"
              :before-upload="onFileSelect"
              accept=".txt,.md,.markdown,.pdf,.docx,.doc,.html,.htm,.csv,.json,.xml,.yaml,.yml,.py,.js,.ts,.go,.java,.sql"
            >
              <el-button type="success" plain :icon="Upload">上传文件</el-button>
            </el-upload>
            <el-badge :value="activeCount" :hidden="activeCount === 0" :max="99">
              <el-button :icon="DataLine" @click="ingestDrawerVisible = true">入库进度</el-button>
            </el-badge>
          </div>
        </template>
      </div>

      <template v-if="kbId">
        <!-- 筛选行 -->
        <div class="bc-toolbar">
          <el-select
            v-model="filterSource"
            placeholder="按知识域过滤"
            clearable
            style="width: 200px"
            @change="loadDocuments"
          >
            <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
          </el-select>
          <span class="bc-muted bc-text-sm">共 {{ documents.length }} 篇文档</span>
        </div>

        <div class="bc-panel">
          <EmptyState
            v-if="!documents.length"
            icon="Document"
            :title="filterSource ? '该知识域下暂无文档' : '库内还没有文档'"
            description="添加文档或上传文件入库；入库后可预览切块效果。"
          >
            <el-button type="primary" :icon="Plus" @click="openCreate">添加文档</el-button>
          </EmptyState>

          <el-table v-else :data="documents">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column label="标题" min-width="180">
              <template #default="{ row }">
                <span class="bc-strong">{{ row.title }}</span>
              </template>
            </el-table-column>
            <el-table-column label="知识域" width="120">
              <template #default="{ row }">
                <el-tag type="success" size="small">{{ row.source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="180">
              <template #default="{ row }">
                <div v-if="row.status === 'processing'" class="ingest-inline">
                  <el-progress
                    :percentage="ingestPercent(row.id)"
                    :status="ingestProgressStatus(row.id)"
                    :stroke-width="12"
                    text-inside
                  />
                  <div class="ingest-inline__meta">{{ ingestStageText(row.id) }}</div>
                </div>
                <el-tooltip
                  v-else-if="row.status === 'failed' && row.error_message"
                  :content="row.error_message"
                  placement="top"
                  effect="dark"
                >
                  <el-tag :type="statusType(row.status)" size="small">
                    {{ statusLabel(row.status) }} ⚠
                  </el-tag>
                </el-tooltip>
                <el-tag v-else :type="statusType(row.status)" size="small">
                  {{ statusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="切块数" width="86" align="center">
              <template #default="{ row }">{{ row.chunk_count ?? 0 }}</template>
            </el-table-column>
            <el-table-column label="字符数" width="86" align="center">
              <template #default="{ row }">{{ row.char_count ?? 0 }}</template>
            </el-table-column>
            <el-table-column label="类型" width="76">
              <template #default="{ row }">
                <span class="bc-text-sm bc-muted">{{ row.file_type || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="160">
              <template #default="{ row }">
                <span class="bc-text-sm bc-muted">{{ formatTime(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="310" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="viewContent(row)">正文</el-button>
                <el-button size="small" @click="viewChunks(row)">切块</el-button>
                <el-button size="small" @click="openEdit(row)">编辑</el-button>
                <el-button
                  size="small"
                  type="warning"
                  plain
                  :loading="reindexing === row.id"
                  @click="onReindex(row)"
                >
                  重建
                </el-button>
                <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </template>

      <div v-else class="bc-panel">
        <EmptyState
          icon="Collection"
          title="先选择一个知识库"
          description="文档按知识库隔离管理；还没有知识库？先去「知识库」页创建。"
        >
          <el-button type="primary" @click="$router.push({ name: 'knowledge-bases' })">
            去管理知识库
          </el-button>
        </EmptyState>
      </div>
    </template>

    <!-- 添加/编辑文档对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑文档' : '添加文档'" width="680px">
      <el-form :model="form" label-position="top">
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="如：销售流程手册" />
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-select
            v-model="form.source"
            placeholder="选择或输入新的知识域标签"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
          >
            <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
          </el-select>
          <div class="bc-hint">对应角色配置里的「知识域标签」；角色 knowledge_scope_tags 包含此值才能检索到</div>
        </el-form-item>
        <el-form-item v-if="!editing" label="正文">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="粘贴或输入文档正文（支持多段落，空行分段）"
          />
        </el-form-item>
        <el-form-item v-else label="正文">
          <div class="bc-hint" style="margin-top: 0">编辑正文请用「重建」功能（改 source 会自动重入库）</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存并入库</el-button>
      </template>
    </el-dialog>

    <!-- 上传文件对话框（后端解析，支持 PDF/Word/HTML 等）-->
    <el-dialog v-model="uploadDialogVisible" title="上传文件入库" width="560px">
      <el-form :model="uploadForm" label-position="top">
        <el-form-item label="文件">
          <div>
            <el-tag v-if="uploadFile" type="success" closable @close="uploadFile = null">
              {{ uploadFile.name }}（{{ (uploadFile.size / 1024).toFixed(1) }}KB）
            </el-tag>
            <el-upload
              v-else
              :show-file-list="false"
              :before-upload="(f) => { uploadFile = f; return false }"
              accept=".txt,.md,.markdown,.pdf,.docx,.doc,.html,.htm,.csv,.json,.xml,.yaml,.yml,.py,.js,.ts,.go,.java,.sql"
            >
              <el-button type="primary" plain :icon="Upload">选择文件</el-button>
            </el-upload>
            <div class="bc-hint">支持 PDF / Word / Markdown / HTML / TXT / 代码等（≤10MB）</div>
          </div>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="uploadForm.title" placeholder="默认用文件名" />
        </el-form-item>
        <el-form-item label="知识域标签">
          <el-select
            v-model="uploadForm.source"
            placeholder="选择或输入新的知识域标签"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
          >
            <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" :disabled="!uploadFile" @click="doUpload">
          上传并入库
        </el-button>
      </template>
    </el-dialog>

    <!-- 切块预览对话框（不落库）-->
    <el-dialog v-model="previewVisible" title="切块预览" width="780px">
      <el-form :model="previewForm" label-position="top">
        <el-form-item label="样例正文">
          <el-input
            v-model="previewForm.text"
            type="textarea"
            :rows="6"
            placeholder="粘贴一段文本，预览入库时的切块效果（不写入知识库）"
          />
        </el-form-item>
        <div class="preview-row">
          <el-form-item label="单块字符数">
            <el-input-number v-model="previewForm.chunkSize" :min="100" :max="4000" :step="50" />
          </el-form-item>
          <el-form-item label="父子块">
            <el-switch v-model="previewForm.enableParentChild" />
            <span class="bc-hint" style="margin-left: 8px; margin-top: 0">child=384 精确匹配 / parent=4096 上下文</span>
          </el-form-item>
        </div>
      </el-form>
      <div v-if="previewResult" class="preview-result">
        <div class="bc-muted bc-text-sm preview-result__meta">
          共 {{ previewResult.total }} 块（child {{ previewResult.child_count }} / parent {{ previewResult.parent_count }}）
        </div>
        <el-scrollbar max-height="420px">
          <ChunkTree :items="previewResult.items" id-key="id" />
        </el-scrollbar>
      </div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
        <el-button type="primary" :loading="previewLoading" @click="runPreview">预览切块</el-button>
      </template>
    </el-dialog>

    <!-- 查看正文对话框 -->
    <el-dialog v-model="contentVisible" :title="`正文：${contentData.title}`" width="760px">
      <div v-loading="contentLoading">
        <div class="bc-muted bc-text-sm" style="margin-bottom: 8px">共 {{ contentData.char_count }} 字</div>
        <el-input :model-value="contentData.content" type="textarea" :rows="18" readonly resize="none" />
      </div>
      <template #footer>
        <el-button @click="contentVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 查看文档切块对话框 -->
    <el-dialog v-model="chunksVisible" :title="`切块：${chunksDocTitle}`" width="820px">
      <div v-loading="chunksLoading">
        <template v-if="chunksData.length > 0">
          <div class="bc-muted bc-text-sm preview-result__meta">
            共 {{ chunksData.length }} 块
          </div>
          <el-scrollbar max-height="500px">
            <ChunkTree :items="chunksData" id-key="chunk_id" />
          </el-scrollbar>
        </template>
        <EmptyState v-else-if="!chunksLoading" icon="Grid" title="无切块数据" />
      </div>
      <template #footer>
        <el-button @click="chunksVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 知识库设置对话框 -->
    <el-dialog v-model="settingsVisible" title="知识库设置" width="720px">
      <el-tabs v-model="settingsTab">
        <el-tab-pane label="基本信息" name="basic">
          <el-form :model="settingsForm" label-position="top">
            <el-form-item label="名称">
              <el-input v-model="settingsForm.name" />
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="settingsForm.description" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="可见角色">
              <el-select
                v-model="settingsForm.visible_to_roles"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入角色名"
                style="width: 100%"
              >
                <el-option v-for="r in roles" :key="r" :label="r" :value="r" />
              </el-select>
              <div class="bc-hint">留空表示所有角色可见</div>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="切块设置" name="chunking">
          <el-form :model="settingsForm" label-position="top">
            <el-form-item label="切块策略">
              <el-select v-model="settingsForm.strategy" style="width: 100%">
                <el-option label="auto（自动）" value="auto" />
                <el-option label="recursive（递归）" value="recursive" />
                <el-option label="heading（标题）" value="heading" />
              </el-select>
            </el-form-item>
            <div class="form-grid">
              <el-form-item label="单块字符数">
                <el-input-number v-model="settingsForm.chunk_size" :min="100" :max="4000" :step="50" />
              </el-form-item>
              <el-form-item label="重叠字符数">
                <el-input-number v-model="settingsForm.chunk_overlap" :min="0" :max="500" :step="10" />
              </el-form-item>
            </div>
            <el-form-item label="分隔符">
              <el-select
                v-model="settingsForm.separators"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="添加分隔符"
                style="width: 100%"
              >
                <el-option label="换行 \n" value="\n" />
                <el-option label="双换行 \n\n" value="\n\n" />
                <el-option label="句号 。" value="。" />
                <el-option label="逗号 ，" value="，" />
                <el-option label="空格" value=" " />
              </el-select>
            </el-form-item>
            <el-form-item label="启用父子块">
              <el-switch v-model="settingsForm.enable_parent_child" />
            </el-form-item>
            <div class="form-grid">
              <el-form-item label="父块字符数">
                <el-input-number
                  v-model="settingsForm.parent_chunk_size"
                  :min="512"
                  :max="8192"
                  :step="128"
                  :disabled="!settingsForm.enable_parent_child"
                />
              </el-form-item>
              <el-form-item label="子块字符数">
                <el-input-number
                  v-model="settingsForm.child_chunk_size"
                  :min="64"
                  :max="2048"
                  :step="32"
                  :disabled="!settingsForm.enable_parent_child"
                />
              </el-form-item>
            </div>
          </el-form>
          <div class="bc-hint">调整切块参数仅影响后续入库文档，已有文档不重建。</div>
        </el-tab-pane>
        <el-tab-pane label="索引设置" name="index">
          <div class="index-info">
            <div class="bc-kv">
              <span class="bc-kv__label">embedding 模型</span>
              <span class="bc-kv__value">#{{ currentKb?.embedding_model_id }} {{ embedModelName }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">向量维度</span>
              <span class="bc-kv__value">{{ currentKb?.dimension || '—' }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">collection 名称</span>
              <span class="bc-kv__value mono">{{ currentKb?.collection_name || '—' }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">文档数</span>
              <span class="bc-kv__value">{{ currentKb?.document_count || 0 }}</span>
            </div>
            <div class="bc-kv">
              <span class="bc-kv__label">切块数</span>
              <span class="bc-kv__value">{{ currentKb?.chunk_count || 0 }}</span>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="解析引擎" name="parsers">
          <div v-loading="parserLoading">
            <el-table v-if="parsers.length > 0" :data="parsers">
              <el-table-column prop="name" label="名称" width="160" />
              <el-table-column prop="description" label="描述" min-width="180" />
              <el-table-column label="支持的文件类型" min-width="200">
                <template #default="{ row }">
                  <el-tag
                    v-for="ft in row.file_types || []"
                    :key="ft"
                    size="small"
                    class="parser-tag"
                  >
                    {{ ft }}
                  </el-tag>
                  <span v-if="!row.file_types?.length" class="bc-muted">—</span>
                </template>
              </el-table-column>
            </el-table>
            <EmptyState v-else-if="!parserLoading" icon="Document" title="无可用解析引擎" />
          </div>
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="settingsVisible = false">取消</el-button>
        <el-button type="primary" :loading="settingsLoading" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>

    <!-- 入库进度抽屉：切块→向量化→写库 管道可视化 -->
    <el-drawer v-model="ingestDrawerVisible" title="入库进度" size="520px" direction="rtl">
      <EmptyState v-if="drawerIngests.length === 0" icon="DataLine" title="暂无入库任务" />
      <div v-else class="ingest-list">
        <div v-for="p in drawerIngests" :key="p.document_id" class="ingest-card">
          <div class="ingest-card__head">
            <span class="ingest-card__title" :title="p.title">{{ p.title || `#${p.document_id}` }}</span>
            <el-tag size="small" :type="statusType(p.status)">{{ statusLabel(p.status) }}</el-tag>
          </div>
          <el-steps
            :active="stepsActive(p)"
            align-center
            finish-status="success"
            :process-status="p.stage === 'failed' ? 'error' : 'process'"
          >
            <el-step title="切块" />
            <el-step title="向量化" />
            <el-step title="写库" />
          </el-steps>
          <el-progress
            :percentage="p.percent"
            :status="ingestProgressStatus(p.document_id)"
            :stroke-width="10"
          />
          <div class="ingest-card__detail">
            <span v-if="p.stage === 'embedding' && p.total_chunks > 0">
              已向量化 {{ p.processed_chunks }}/{{ p.total_chunks }} 块
            </span>
            <span v-else-if="p.stage === 'done' || p.status === 'completed'">共 {{ p.chunk_count }} 块</span>
            <span v-else-if="p.stage">{{ STAGE_LABEL[p.stage] }}</span>
            <span v-else class="bc-muted">等待开始…</span>
          </div>
          <div v-if="isRestartHint(p.error_message)" class="ingest-card__restart">
            <span class="bc-muted bc-text-xs">{{ p.error_message }}</span>
            <el-button
              size="small"
              type="warning"
              :loading="reindexing === p.document_id"
              :disabled="!p._kbId"
              @click="reindexFromDrawer(p)"
            >
              重建
            </el-button>
          </div>
          <div v-else-if="p.error_message" class="ingest-card__error">{{ p.error_message }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
// 知识库文档管理：视图层；全部状态与行为在 useKnowledge composable
import { Plus, Upload, Grid, Setting, Collection, DataLine } from '@element-plus/icons-vue'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import ChunkTree from '../components/ChunkTree.vue'
import { useKnowledge } from './useKnowledge'

const {
  loading, saving, editing, reindexing,
  kbs, kbId, documents, sources, filterSource, dialogVisible, form, currentKb,
  uploadDialogVisible, uploadFile, uploadForm, uploading,
  previewVisible, previewForm, previewResult, previewLoading,
  chunksVisible, chunksLoading, chunksData, chunksDocTitle,
  settingsTab, settingsVisible, settingsLoading, settingsForm, roles, embedModels, parsers, parserLoading, embedModelName,
  contentVisible, contentLoading, contentData,
  ingestDrawerVisible, activeCount, drawerIngests, STAGE_LABEL,
  ingestPercent, ingestProgressStatus, ingestStageText, stepsActive, isRestartHint,
  loadDocuments, onKbChange, openCreate, openEdit, onSave, onDelete, onReindex,
  reindexFromDrawer, onFileSelect, doUpload, openPreview, runPreview,
  viewContent, viewChunks, openSettings, saveSettings,
  statusType, statusLabel, formatTime,
} = useKnowledge()
</script>

<style scoped>
/* 知识库上下文头 */
.kb-context {
  display: flex;
  align-items: center;
  gap: var(--bc-space-6);
  padding: var(--bc-space-4) var(--bc-space-5);
  margin-bottom: var(--bc-space-4);
}
.kb-context__main {
  flex: 1;
  min-width: 0;
}
.kb-context__picker {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
}
.kb-context__icon {
  color: var(--bc-primary-6);
  flex-shrink: 0;
}
.kb-context__select {
  width: 280px;
}
.kb-context__desc {
  margin: var(--bc-space-1) 0 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-context__stats {
  display: flex;
  gap: var(--bc-space-6);
  padding: 0 var(--bc-space-6);
  border-left: 1px solid var(--bc-border);
  /* 与主区/操作区形成稳定三段式 */
  align-self: stretch;
  align-items: center;
}
.kb-context__actions {
  display: flex;
  gap: var(--bc-space-3);
  align-items: center;
}

/* 预览/切块 */
.preview-row {
  display: flex;
  gap: var(--bc-space-6);
}
.preview-result {
  margin-top: var(--bc-space-2);
}
.preview-result__meta {
  margin-bottom: var(--bc-space-2);
}

/* 设置 */
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--bc-space-4);
}
.index-info {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--bc-space-4);
  padding: var(--bc-space-2) 0;
}
.parser-tag {
  margin: 1px var(--bc-space-1) 1px 0;
}

/* 表格内联进度 */
.ingest-inline {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ingest-inline__meta {
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
  line-height: 1.2;
}

/* 入库进度抽屉 */
.ingest-list {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-4);
}
.ingest-card {
  border: 1px solid var(--bc-border);
  border-radius: var(--bc-radius-md);
  padding: var(--bc-space-3) var(--bc-space-4);
  background: var(--bc-bg-surface);
}
.ingest-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-2);
  margin-bottom: var(--bc-space-3);
}
.ingest-card__title {
  font-weight: var(--bc-weight-medium);
  font-size: var(--bc-text-sm);
  color: var(--bc-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ingest-card__detail {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-text-2);
}
.ingest-card__restart {
  margin-top: var(--bc-space-2);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-2);
}
.ingest-card__error {
  margin-top: var(--bc-space-2);
  font-size: var(--bc-text-xs);
  color: var(--bc-danger);
  word-break: break-word;
}
@media (max-width: 1280px) {
  .kb-context {
    flex-wrap: wrap;
  }
  .kb-context__stats {
    border-left: none;
    padding: 0;
  }
}
</style>
