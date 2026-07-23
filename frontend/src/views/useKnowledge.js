// 知识库文档管理页的全部状态与行为（从 Knowledge.vue 抽出的 composable）。
// 覆盖：KB 选择、文档 CRUD、上传、切块预览、正文/切块查看、库设置、入库进度轮询与抽屉。
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { admin } from '../api'

export function useKnowledge() {
  const route = useRoute()
  const router = useRouter()

  const loading = ref(true)
  const saving = ref(false)
  const editing = ref(false)
  const reindexing = ref(null)

  const kbs = ref([])
  const kbId = ref(null)
  const documents = ref([])
  const sources = ref([])
  const filterSource = ref('')
  const dialogVisible = ref(false)
  const form = reactive({ title: '', source: '', content: '' })

  // 上传文件（后端解析）
  const uploadDialogVisible = ref(false)
  const uploadFile = ref(null)
  const uploadForm = reactive({ title: '', source: '' })
  const uploading = ref(false)

  // 切块预览
  const previewVisible = ref(false)
  const previewForm = reactive({ text: '', chunkSize: 500, enableParentChild: false })
  const previewResult = ref(null)
  const previewLoading = ref(false)

  // 文档切块
  const chunksVisible = ref(false)
  const chunksLoading = ref(false)
  const chunksData = ref([])
  const chunksDocTitle = ref('')

  // 知识库设置
  const settingsTab = ref('basic')
  const settingsVisible = ref(false)
  const settingsLoading = ref(false)
  const settingsForm = reactive({
    name: '',
    description: '',
    visible_to_roles: [],
    chunk_size: 500,
    chunk_overlap: 50,
    strategy: 'auto',
    enable_parent_child: false,
    parent_chunk_size: 4096,
    child_chunk_size: 384,
    separators: [],
  })
  const roles = ref([])
  const embedModels = ref([])
  const parsers = ref([])
  const parserLoading = ref(false)

  // 查看正文
  const contentVisible = ref(false)
  const contentLoading = ref(false)
  const contentData = reactive({ title: '', content: '', char_count: 0 })

  const currentKb = computed(() => kbs.value.find((k) => k.id === kbId.value))
  const embedModelName = computed(() => {
    const model = embedModels.value.find((m) => m.id === currentKb.value?.embedding_model_id)
    return model ? model.name : ''
  })

  // ============ 异步入库进度可视化（轮询 + 抽屉） ============
  // progressMap：doc_id → IngestProgressOut（本会话内累积，含已完成的，供抽屉展示）
  // ingestOrder：doc_id 首次出现顺序（整数 key 在普通对象里按数值排序，故单独维护顺序）
  const progressMap = ref({})
  const ingestOrder = ref([])
  const lastStepMap = ref({}) // 失败时回退到"最后已知阶段"用
  let pollTimer = null
  const ingestDrawerVisible = ref(false)

  // 进行中判定：status=processing 且不是"重启丢失进度"的停滞态（stage 空且有重启提示）。
  // 这样刚提交（stage 空、无 error）仍算 active 继续轮询；服务重启的停滞文档不算 active，
  // 避免无限轮询——它仍展示在表格/抽屉里并提供「重建」入口。
  function isActiveEntry(p) {
    if (!p) return false
    if (p.status !== 'processing') return false
    if (p.stage === '' && p.error_message) return false
    return true
  }

  const hasActive = computed(() => Object.values(progressMap.value).some(isActiveEntry))
  const activeCount = computed(() => Object.values(progressMap.value).filter(isActiveEntry).length)

  // 抽屉条目：进行中的置顶，其余按首次出现顺序
  const drawerIngests = computed(() => {
    const list = ingestOrder.value.map((id) => progressMap.value[id]).filter(Boolean)
    return list.sort((a, b) => Number(isActiveEntry(b)) - Number(isActiveEntry(a)))
  })

  const STAGE_STEP = { chunking: 0, embedding: 1, indexing: 2, done: 3 }
  const STAGE_LABEL = {
    '': '入库中…',
    chunking: '切块中',
    embedding: '向量化中',
    indexing: '写库中',
    done: '已完成',
    failed: '失败',
  }

  function rememberProgress(p) {
    // 存入 progressMap（带 _kbId 便于抽屉里重建），维护顺序，记录最后已知阶段
    if (!progressMap.value[p.document_id]) ingestOrder.value.push(p.document_id)
    progressMap.value = {
      ...progressMap.value,
      [p.document_id]: {
        ...p,
        _kbId: p._kbId ?? progressMap.value[p.document_id]?._kbId ?? kbId.value,
      },
    }
    if (p.stage in STAGE_STEP) lastStepMap.value[p.document_id] = STAGE_STEP[p.stage]
  }

  function ingestPercent(docId) {
    return progressMap.value[docId]?.percent ?? 0
  }
  function ingestProgressStatus(docId) {
    const p = progressMap.value[docId]
    if (!p) return undefined
    if (p.stage === 'done' || p.status === 'completed') return 'success'
    if (p.stage === 'failed' || p.status === 'failed') return 'exception'
    return undefined
  }
  function ingestStageText(docId) {
    const p = progressMap.value[docId]
    if (!p || p.stage === '') return '入库中…'
    if (p.stage === 'embedding' && p.total_chunks > 0) {
      return `向量化中 ${p.processed_chunks}/${p.total_chunks}`
    }
    return STAGE_LABEL[p.stage] || '入库中…'
  }
  function stageStep(stage) {
    if (stage === 'failed') return null // 用 lastStepMap 兜底（见 stepsActive）
    return stage in STAGE_STEP ? STAGE_STEP[stage] : 0
  }
  function stepsActive(p) {
    if (p.stage === 'failed') return lastStepMap.value[p.document_id] ?? 0
    return stageStep(p.stage)
  }
  function isRestartHint(msg) {
    return !!(msg && (msg.includes('进度不可用') || msg.includes('重启')))
  }

  // 把单个进度合并进当前 KB 的 documents 行（status/stage 衍生字段），让表格实时反映
  function syncDocRow(p) {
    const idx = documents.value.findIndex((d) => d.id === p.document_id)
    if (idx === -1) return
    const row = { ...documents.value[idx] }
    row.status = p.status
    if (typeof p.chunk_count === 'number') row.chunk_count = p.chunk_count
    if (p.status === 'failed' && p.error_message) row.error_message = p.error_message
    documents.value = [...documents.value.slice(0, idx), row, ...documents.value.slice(idx + 1)]
  }

  let _reloadingAfterIngest = false
  async function pollOnce() {
    let active = []
    try {
      active = await admin.listActiveIngests()
    } catch {
      return // 网络抖动：本轮跳过，下轮再试
    }
    const activeById = new Map(active.map((p) => [p.document_id, p]))
    // 本轮开始前处于 processing 的 doc_id（用于检测本轮是否有人转为终态）
    const beforeIds = Object.values(progressMap.value)
      .filter((p) => p.status === 'processing')
      .map((p) => p.document_id)

    // 合并 live 进度
    for (const p of active) rememberProgress(p)

    // 跟踪中的 processing 文档但已不在 live 列表（worker 刚结束/已落终态/服务重启）
    // → 单独拉一次权威状态，确保跟踪条目能转入终态（否则 badge/轮询永不收尾）。
    const trackedIds = Object.values(progressMap.value)
      .filter((p) => p.status === 'processing' && !activeById.has(p.document_id))
      .map((p) => p.document_id)
    const docIds = documents.value
      .filter((d) => d.status === 'processing' && !activeById.has(d.id))
      .map((d) => d.id)
    const staleIds = [...new Set([...trackedIds, ...docIds])]
    if (staleIds.length) {
      const fetched = await Promise.all(
        staleIds.map((id) => {
          const kb = progressMap.value[id]?._kbId || kbId.value
          return admin.getIngestProgress(kb, id).catch(() => null)
        }),
      )
      for (const p of fetched) if (p) rememberProgress(p)
    }

    // 实时同步当前 KB 的表格行
    for (const p of active) syncDocRow(p)

    // 检测终态转换
    const justFinished = beforeIds.filter((id) => {
      const cur = progressMap.value[id]
      return cur && (cur.status === 'completed' || cur.status === 'failed')
    })

    if (justFinished.length && !_reloadingAfterIngest) {
      _reloadingAfterIngest = true
      try {
        await Promise.all([loadDocuments(), loadKbs()])
      } finally {
        _reloadingAfterIngest = false
      }
    }
  }

  function ensurePolling() {
    if (pollTimer) return
    pollOnce() // 立即跑一轮，避免首个 tick 延迟
    pollTimer = setInterval(pollOnce, 900)
  }
  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // hasActive 驱动轮询启停：有进行中即轮询，全部终态即停
  watch(hasActive, (v) => {
    if (v) ensurePolling()
    else stopPolling()
  })

  // 触发后立即种子进度（row 立刻显示进度）+ 保活轮询
  async function seedProgress(docId) {
    try {
      const p = await admin.getIngestProgress(kbId.value, docId)
      rememberProgress(p)
    } catch {
      // 拉不到也不阻塞：进度会在下一轮 pollOnce 补上
    }
    ensurePolling()
  }

  // 路由 query 变化 → 同步 kbId（从知识库列表点过来时带 kb_id）
  watch(
    () => route.query.kb_id,
    (val) => {
      if (val && Number(val) !== kbId.value) {
        kbId.value = Number(val)
        loadDocuments()
      }
    },
  )

  async function loadKbs() {
    kbs.value = await admin.listKnowledgeBases()
    // 初始：优先用 query 参数，否则选第一个
    const qid = route.query.kb_id ? Number(route.query.kb_id) : null
    if (qid && kbs.value.some((k) => k.id === qid)) {
      kbId.value = qid
    } else if (kbs.value.length > 0 && !kbId.value) {
      kbId.value = kbs.value[0].id
    }
  }

  async function loadAll() {
    loading.value = true
    try {
      await loadKbs()
      await loadDocuments()
      await loadSources()
    } finally {
      loading.value = false
    }
  }

  async function loadDocuments() {
    if (!kbId.value) {
      documents.value = []
      return
    }
    documents.value = await admin.listDocuments(kbId.value, filterSource.value || undefined)
    // 页面加载/切换途中若有 processing 文档：种子进度并保活轮询
    const processing = documents.value.filter((d) => d.status === 'processing')
    if (processing.length) {
      await Promise.all(processing.map((d) => seedProgress(d.id)))
    }
  }

  async function loadSources() {
    sources.value = await admin.listSources().catch(() => [])
  }

  function onKbChange(val) {
    // 切换 KB：停掉旧轮询（避免跨 KB 误刷新），同步路由 query，重新加载文档
    stopPolling()
    router.replace({ query: { kb_id: val } })
    filterSource.value = ''
    loadDocuments()
  }

  function openCreate() {
    editing.value = false
    Object.assign(form, { title: '', source: '', content: '' })
    dialogVisible.value = true
  }

  function openEdit(row) {
    editing.value = true
    Object.assign(form, { title: row.title, source: row.source, content: '' })
    form._id = row.id
    dialogVisible.value = true
  }

  async function onSave() {
    if (!form.title.trim()) {
      ElMessage.warning('标题不能为空')
      return
    }
    if (!form.source.trim()) {
      ElMessage.warning('知识域标签不能为空')
      return
    }

    saving.value = true
    try {
      if (editing.value) {
        await admin.updateDocument(kbId.value, form._id, {
          title: form.title,
          source: form.source,
        })
      } else {
        if (!form.content.trim()) {
          ElMessage.warning('正文不能为空')
          saving.value = false
          return
        }
        const res = await admin.createDocument(kbId.value, {
          knowledge_base_id: kbId.value,
          title: form.title,
          source: form.source,
          content: form.content,
        })
        // 异步入库：种子进度（抽屉即时反馈）+ 刷新表格让新行出现并保活轮询
        ElMessage.success('已提交入库，正在处理…')
        dialogVisible.value = false
        await seedProgress(res.document_id)
        await loadDocuments()
        return
      }
      // 编辑分支：仅改元数据，不重入库
      ElMessage.success('已保存')
      dialogVisible.value = false
      await loadDocuments()
    } finally {
      saving.value = false
    }
  }

  async function onDelete(row) {
    await ElMessageBox.confirm(
      `确定删除文档「${row.title}」？将同时清理向量库中的所有切块。`,
      '确认删除',
      { type: 'warning' },
    )
    await admin.deleteDocument(kbId.value, row.id)
    ElMessage.success('已删除')
    await loadDocuments()
  }

  async function onReindex(row) {
    // reindexing 仅作为提交瞬间的按钮 loading；受理后刷新表格行 + 内联进度接管
    reindexing.value = row.id
    try {
      const res = await admin.reindexDocument(kbId.value, row.id)
      ElMessage.success('重建已提交，入库中…')
      await seedProgress(res.document_id)
      await loadDocuments()
    } finally {
      reindexing.value = null
    }
  }

  // 抽屉里"重启丢失进度"文档的重建入口（progress 条目带 _kbId；缺失则禁用）
  async function reindexFromDrawer(p) {
    const targetKb = p._kbId || kbId.value
    if (!targetKb) return
    reindexing.value = p.document_id
    try {
      const res = await admin.reindexDocument(targetKb, p.document_id)
      ElMessage.success('重建已提交，入库中…')
      // 重建后 kbId 可能不同：切到目标 KB 再种子进度
      if (targetKb !== kbId.value) {
        kbId.value = targetKb
        await loadDocuments()
      }
      await seedProgress(res.document_id)
    } catch (e) {
      ElMessage.error(e.response?.data?.detail || e.message || '重建失败')
    } finally {
      reindexing.value = null
    }
  }

  function onFileSelect(file) {
    // 选择文件 → 打开上传对话框收集 source/title，实际上传走后端 /upload
    uploadFile.value = file
    Object.assign(uploadForm, {
      title: file.name.replace(/\.[^.]+$/, ''),
      source: '',
    })
    uploadDialogVisible.value = true
    return false // 阻止 el-upload 自动上传
  }

  async function doUpload() {
    if (!uploadForm.source.trim()) {
      ElMessage.warning('知识域标签不能为空')
      return
    }
    uploading.value = true
    try {
      const res = await admin.uploadDocument(
        kbId.value,
        uploadFile.value,
        uploadForm.source.trim(),
        uploadForm.title.trim() || null,
      )
      // 异步入库：受理后返回 {document_id, status}，种子进度 + 刷新表格让新行出现
      ElMessage.success('已提交入库，正在处理…')
      uploadDialogVisible.value = false
      uploadFile.value = null
      await seedProgress(res.document_id)
      await loadDocuments()
    } catch (e) {
      // 解析阶段 4xx（非 UTF-8 / 格式不支持 / 文件过大）
      ElMessage.error(e.response?.data?.detail || e.message || '上传失败')
    } finally {
      uploading.value = false
    }
  }

  function openPreview() {
    Object.assign(previewForm, { text: '', chunkSize: 500, enableParentChild: false })
    previewResult.value = null
    previewVisible.value = true
  }

  async function runPreview() {
    if (!previewForm.text.trim()) {
      ElMessage.warning('请输入样例正文')
      return
    }
    previewLoading.value = true
    try {
      previewResult.value = await admin.previewChunks(kbId.value, {
        text: previewForm.text,
        title: '预览',
        chunk_size: previewForm.chunkSize,
        enable_parent_child: previewForm.enableParentChild,
      })
    } catch (e) {
      ElMessage.error(e.response?.data?.detail || e.message || '预览失败')
    } finally {
      previewLoading.value = false
    }
  }

  async function viewContent(row) {
    contentVisible.value = true
    contentLoading.value = true
    Object.assign(contentData, { title: row.title, content: '', char_count: 0 })
    try {
      const data = await admin.getDocumentContent(kbId.value, row.id)
      Object.assign(contentData, data)
    } catch {
      ElMessage.error('读取正文失败')
    } finally {
      contentLoading.value = false
    }
  }

  async function viewChunks(row) {
    chunksDocTitle.value = row.title
    chunksLoading.value = true
    chunksVisible.value = true
    try {
      const res = await admin.listDocumentChunks(kbId.value, row.id)
      chunksData.value = res.items || []
    } catch {
      ElMessage.error('获取切块列表失败')
    } finally {
      chunksLoading.value = false
    }
  }

  async function openSettings() {
    const kb = currentKb.value
    if (!kb) return
    settingsTab.value = 'basic'
    Object.assign(settingsForm, {
      name: kb.name || '',
      description: kb.description || '',
      visible_to_roles: kb.visible_to_roles || [],
      chunk_size: kb.chunk_size ?? 500,
      chunk_overlap: kb.chunk_overlap ?? 50,
      strategy: kb.strategy || 'auto',
      enable_parent_child: kb.enable_parent_child ?? false,
      parent_chunk_size: kb.parent_chunk_size ?? 4096,
      child_chunk_size: kb.child_chunk_size ?? 384,
      separators: kb.separators || [],
    })
    settingsVisible.value = true
    // 并行加载角色列表、embedding 模型列表和解析引擎
    const [roleList, models] = await Promise.all([
      admin.listRoles().catch(() => []),
      admin.listModels('embedding').catch(() => []),
    ])
    roles.value = roleList.map((r) => r.name)
    embedModels.value = models
    loadParsers()
  }

  async function loadParsers() {
    parserLoading.value = true
    try {
      parsers.value = await admin.listParsers()
    } catch {
      parsers.value = []
    } finally {
      parserLoading.value = false
    }
  }

  async function saveSettings() {
    settingsLoading.value = true
    try {
      await admin.updateKnowledgeBase(kbId.value, {
        name: settingsForm.name,
        description: settingsForm.description,
        visible_to_roles: settingsForm.visible_to_roles,
        chunk_size: settingsForm.chunk_size,
        chunk_overlap: settingsForm.chunk_overlap,
        strategy: settingsForm.strategy,
        enable_parent_child: settingsForm.enable_parent_child,
        parent_chunk_size: settingsForm.enable_parent_child ? settingsForm.parent_chunk_size : null,
        child_chunk_size: settingsForm.enable_parent_child ? settingsForm.child_chunk_size : null,
        separators: settingsForm.separators,
      })
      ElMessage.success('设置已保存')
      settingsVisible.value = false
      await loadKbs()
    } catch {
      ElMessage.error('保存设置失败')
    } finally {
      settingsLoading.value = false
    }
  }

  function statusType(s) {
    return { completed: 'success', processing: 'warning', pending: 'info', failed: 'danger' }[s] || 'info'
  }
  function statusLabel(s) {
    return { pending: '待入库', processing: '入库中', completed: '已完成', failed: '失败' }[s] || s
  }
  function formatTime(t) {
    if (!t) return ''
    return new Date(t).toLocaleString('zh-CN', { hour12: false })
  }

  onMounted(loadAll)
  onUnmounted(stopPolling)

  return {
    // 基础
    loading, saving, editing, reindexing,
    kbs, kbId, documents, sources, filterSource, dialogVisible, form, currentKb,
    // 上传
    uploadDialogVisible, uploadFile, uploadForm, uploading,
    // 预览
    previewVisible, previewForm, previewResult, previewLoading,
    // 切块
    chunksVisible, chunksLoading, chunksData, chunksDocTitle,
    // 设置
    settingsTab, settingsVisible, settingsLoading, settingsForm, roles, embedModels, parsers, parserLoading, embedModelName,
    // 正文
    contentVisible, contentLoading, contentData,
    // 入库进度
    progressMap, ingestDrawerVisible, activeCount, drawerIngests, STAGE_LABEL,
    ingestPercent, ingestProgressStatus, ingestStageText, stepsActive, isRestartHint,
    // 行为
    loadDocuments, onKbChange, openCreate, openEdit, onSave, onDelete, onReindex,
    reindexFromDrawer, onFileSelect, doUpload, openPreview, runPreview,
    viewContent, viewChunks, openSettings, saveSettings,
    statusType, statusLabel, formatTime,
  }
}
