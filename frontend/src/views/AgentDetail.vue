<template>
  <div class="bc-page">
    <ContentLoader v-if="loading" :rows="8" />

    <InlineError v-if="loadError" :message="loadError" :busy="loading" @retry="load" />
    <template v-if="!loading && agent">
      <PageHeader :title="agent.display_name || agent.name" :description="agent.description || `数字员工实例 · 标识 ${agent.name}`">
        <el-button :icon="ArrowLeft" @click="goBack">返回列表</el-button>
        <el-button :icon="ChatDotRound" @click="goChat">对话测试</el-button>
      </PageHeader>

      <!-- 状态条：启用/停用 · 专属渠道 · 渠道标签 -->
      <div class="statusbar">
        <el-tag :type="agent.is_enabled ? 'success' : 'info'" effect="plain">
          {{ agent.is_enabled ? '已启用' : '已停用' }}
        </el-tag>
        <el-tag v-for="ch in imChannels" :key="ch" :type="statusError ? 'info' : connectionFor(ch)?.is_connected ? 'success' : 'warning'">{{ channelLabel(ch) }}：{{ statusError ? '状态未知' : connectionFor(ch)?.is_connected ? '已连接' : connectionFor(ch)?.last_error ? '连接失败' : '未连接' }}</el-tag>
        <el-tag type="info" effect="plain">渠道标签：{{ channelLabel(agent.channel) }}</el-tag>
        <span class="mono bc-muted bc-text-xs statusbar__id">#{{ agent.id }} · {{ agent.name }}</span>
      </div>

      <InlineError :message="optionError" :busy="loading" @retry="retryOptions" />
      <InlineError :message="statusError" @retry="refreshStatus" />
      <InlineError :message="saveError" :retryable="false" />
      <div class="config-check bc-panel bc-panel--padded">
        <div class="bc-toolbar"><strong>配置检查</strong><span class="bc-muted">按需装配与验证</span><span class="bc-toolbar__spacer" /><el-button @click="checkOpen = !checkOpen" :aria-expanded="checkOpen">{{ checkOpen ? '收起检查' : '展开检查' }}</el-button></div>
        <div v-if="checkOpen" class="check-grid">
          <div><strong>人格与模型</strong><p>{{ effectiveModel }}</p><el-button text @click="activeSection = 'persona'">查看配置</el-button></div>
          <div><strong>用户权限</strong><p>{{ preview.result ? '已有用户检查结果' : '尚未检查' }}</p><el-button text @click="activeSection = 'preview'">检查生效结果</el-button></div>
          <div><strong>知识与工具</strong><p>按用户角色与实例限制共同判定</p><el-button text @click="activeSection = 'capability'">查看能力边界</el-button></div>
          <div><strong>渠道与验证</strong><p>{{ imChannels.length ? '已配置专属渠道，请检查连接状态' : '尚未接入 IM，可先进行对话验证' }}</p><el-button text @click="activeSection = 'channel'">查看渠道</el-button><el-button text @click="goChat">对话验证</el-button></div>
        </div>
      </div>
      <div class="detail-layout">
        <!-- ============ 左：分段导航（只渲染当前段，不再一条长滚动） ============ -->
        <nav class="sections" aria-label="配置分区">
          <button
            v-for="s in sections"
            :key="s.key"
            type="button"
            class="sections__item"
            :class="{ 'is-on': activeSection === s.key }"
            :disabled="Boolean(savingKey)"
            :aria-label="s.label + (dirty[s.key] ? '，有未保存修改' : '')"
            :aria-current="activeSection === s.key ? 'true' : undefined"
            @click="activeSection = s.key"
          >
            <el-icon :size="15" aria-hidden="true"><component :is="s.icon" /></el-icon>
            <span class="sections__label">{{ s.label }}</span>
            <span
              v-if="dirty[s.key]"
              class="sections__dot"
              :title="'「' + s.label + '」有未保存的修改'"
              aria-hidden="true"
            />
          </button>
        </nav>

        <!-- ============ 右：当前分段 ============ -->
        <div class="section-body">
          <!-- ① 基础信息 -->
          <div v-show="activeSection === 'basic'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">基础信息</h3><span class="save-state" role="status">{{ dirty.basic ? '有未保存的修改' : savedAt.basic ? '已保存 · ' + savedAt.basic : '与已保存配置一致' }}</span>
                <p class="bc-muted bc-text-sm section__desc">展示名、描述与启停。内部标识（{{ agent.name }}）由系统生成，仅供审计排障，不可改。</p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.basic" :disabled="Boolean(savingKey)" text @click="resetBasic">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.basic || Boolean(savingKey) || Boolean(optionError)" :loading="savingKey === 'basic'" @click="saveBasic">保存本区</el-button>
              </div>
            </div>
            <el-form label-position="top" :disabled="Boolean(savingKey) || Boolean(optionError)">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="显示名">
                    <el-input v-model="basic.display_name" placeholder="如 销售助手" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="头像">
                    <div class="avatar-picker">
                      <span class="avatar-picker__preview">
                        <img v-if="avatarUrl" :src="avatarUrl" alt="头像预览" />
                        <span v-else aria-hidden="true">{{ avatarInitial }}</span>
                      </span>
                      <div class="avatar-picker__body">
                        <input
                          ref="avatarInput"
                          class="avatar-picker__file"
                          type="file"
                          accept="image/png,image/jpeg,image/gif,image/webp"
                          @change="onAvatarFile"
                        />
                        <div class="avatar-picker__actions">
                          <el-button size="small" :loading="uploadingAvatar" @click="pickAvatar">
                            {{ avatarUrl ? '重新上传' : '上传图片' }}
                          </el-button>
                          <el-button
                            v-if="avatarUrl"
                            size="small"
                            text
                            :disabled="uploadingAvatar"
                            @click="onRemoveAvatar"
                          >移除</el-button>
                        </div>
                        <div class="bc-hint">本地图片：PNG / JPEG / GIF / WebP，不超过 2MB；上传后立即生效，无需保存</div>
                      </div>
                    </div>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-form-item label="描述">
                <el-input v-model="basic.description" placeholder="一句话说明该数字员工的职责与边界" />
              </el-form-item>
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="渠道标签">
                    <el-select v-model="basic.channel" style="width:100%">
                      <el-option label="对话测试 (api)" value="api" />
                      <el-option label="企业微信 (wecom)" value="wecom" />
                      <el-option label="飞书 (feishu)" value="feishu" />
                    </el-select>
                    <div class="bc-hint">仅作标签与筛选；真正接 IM 见「渠道接入」区</div>
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="启用">
                    <el-switch v-model="basic.is_enabled" active-text="启用" inactive-text="停用" />
                    <div class="bc-hint">停用后新会话拒绝、历史会话只读，专属连接停止</div>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ② 人格与模型 -->
          <div v-show="activeSection === 'persona'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">人格与模型</h3><span class="save-state" role="status">{{ dirty.persona ? '有未保存的修改' : savedAt.persona ? '已保存 · ' + savedAt.persona : '与已保存配置一致' }}</span>
                <p class="bc-muted bc-text-sm section__desc">
                  未指定时沿用组织配置。模型的会话绑定优先于实例绑定；最终结果以真实会话为准。
                </p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.persona" :disabled="Boolean(savingKey)" text @click="resetPersona">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.persona || Boolean(savingKey) || Boolean(optionError)" :loading="savingKey === 'persona'" @click="savePersona">保存本区</el-button>
              </div>
            </div>
            <el-form label-position="top" :disabled="Boolean(savingKey) || Boolean(optionError)">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="绑定人格版本">
                    <el-select v-model="promptSelection" clearable placeholder="沿用角色/全局" style="width:100%">
                      <el-option label="— 沿用角色/全局" :value="''" />
                      <el-option v-for="p in prompts" :key="p.version" :label="`v${p.version} ${p.label || ''} ${p.is_active ? '(激活)' : ''}`" :value="p.version" />
                    </el-select>
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="绑定对话模型">
                    <el-select v-model="modelSelection" clearable placeholder="沿用会话/全局" style="width:100%">
                      <el-option label="— 沿用会话/全局" :value="''" />
                      <el-option v-for="m in chatModels" :key="m.id" :label="`${m.display_name || m.name} (${m.name})`" :value="m.id" />
                    </el-select>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ③ 能力边界 -->
          <div v-show="activeSection === 'capability'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">能力边界</h3><span class="save-state" role="status">{{ dirty.capability ? '有未保存的修改' : savedAt.capability ? '已保存 · ' + savedAt.capability : '与已保存配置一致' }}</span>
                <p class="bc-muted bc-text-sm section__desc">
                  角色是天花板：技能与角色取<strong>并集</strong>（实例补充能力）；工具与知识域取<strong>交集</strong>（实例只能收窄）。留空 = 沿用角色。
                </p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.capability" :disabled="Boolean(savingKey)" text @click="resetCapability">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.capability || Boolean(savingKey) || Boolean(optionError)" :loading="savingKey === 'capability'" @click="saveCapability">保存本区</el-button>
              </div>
            </div>
            <el-form label-position="top" :disabled="Boolean(savingKey) || Boolean(optionError)">
              <el-form-item label="绑定技能">
                <el-radio-group v-model="skillMode" class="config-mode"><el-radio value="inherit">沿用角色技能</el-radio><el-radio value="custom">额外绑定技能</el-radio></el-radio-group>
                <el-select v-if="skillMode === 'custom'" v-model="capability.skills" multiple filterable clearable placeholder="选择要额外绑定的技能" style="width:100%">
                  <el-option v-for="s in allSkills" :key="s.name" :label="`${s.name} — ${s.description || ''}`" :value="s.name" />
                </el-select>
              </el-form-item>
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="知识域标签覆盖">
                    <el-radio-group v-model="knowledgeMode" class="config-mode"><el-radio value="inherit">沿用角色范围</el-radio><el-radio value="custom">在角色范围内限制</el-radio></el-radio-group>
                <el-select v-if="knowledgeMode === 'custom'" v-model="capability.knowledge_scope_tags" multiple filterable allow-create clearable placeholder="至少选择一项" style="width:100%">
                      <el-option v-for="tag in sourceOptions" :key="tag" :label="tag" :value="tag" />
                    </el-select>
                    <div class="bc-hint">只允许角色范围内的知识域，实例不能扩大访问权限。</div>
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="工具白名单覆盖">
                    <el-radio-group v-model="toolMode" class="config-mode"><el-radio value="inherit">沿用角色工具</el-radio><el-radio value="custom">在角色范围内限制</el-radio></el-radio-group>
                <el-select v-if="toolMode === 'custom'" v-model="capability.allowed_tools" multiple filterable allow-create clearable placeholder="至少选择一项" style="width:100%">
                      <el-option v-for="t in toolOptions" :key="t.name" :label="`${t.name} — ${t.description?.slice(0, 40) || ''}`" :value="t.name" />
                    </el-select>
                    <div class="bc-hint">只允许角色范围内的工具，实际权限可在生效预览中检查。</div>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </div>

          <!-- ④ 可见性 -->
          <div v-show="activeSection === 'visibility'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">可见性</h3><span class="save-state" role="status">{{ dirty.visibility ? '有未保存的修改' : savedAt.visibility ? '已保存 · ' + savedAt.visibility : '与已保存配置一致' }}</span>
                <p class="bc-muted bc-text-sm section__desc">限制哪些角色可在对话中选用该实例；admin 恒可见。</p>
              </div>
              <div class="section__actions">
                <el-button v-if="dirty.visibility" :disabled="Boolean(savingKey)" text @click="resetVisibility">放弃修改</el-button>
                <el-button type="primary" :disabled="!dirty.visibility || Boolean(savingKey) || Boolean(optionError)" :loading="savingKey === 'visibility'" @click="saveVisibility">保存本区</el-button>
              </div>
            </div>
            <el-form label-position="top" :disabled="Boolean(savingKey) || Boolean(optionError)">
              <el-form-item label="可见角色">
                <el-radio-group v-model="visibilityMode" class="config-mode"><el-radio value="inherit">全部角色可见</el-radio><el-radio value="custom">指定可见角色</el-radio></el-radio-group>
                <el-select v-if="visibilityMode === 'custom'" v-model="visibility.visible_roles" multiple clearable placeholder="至少选择一个角色" style="width:100%">
                  <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
                </el-select>
              </el-form-item>
            </el-form>
          </div>

          <!-- ⑤ 渠道接入（专属连接，ADR-0012：同实例可接入多个渠道） -->
          <div v-show="activeSection === 'channel'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">渠道接入 · 专属连接</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  一个数字员工可同时接入多个不同渠道（每种渠道一条专属连接，会话按 实例×渠道 独立隔离）；
                  同一凭证（如企微 bot_id）全局只能绑定一个实例。全局默认 Bot 在「渠道接入」页维护。
                </p>
              </div>
              <div v-if="wecomConn" class="section__conn">
                <StatusBadge :type="wecomConn.is_connected ? 'ok' : 'err'" :text="wecomConn.is_connected ? '已连接' : '未连接'" />
                <span v-if="wecomConn.last_error" class="bc-text-xs bc-muted">{{ wecomConn.last_error }}</span>
                <el-button size="small" :loading="reconnecting" @click="onReconnectBot">重连企微</el-button>
              </div>
            </div>
            <el-form label-position="top" :disabled="Boolean(savingKey) || Boolean(optionError)">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="企微 bot_id">
                    <el-input :disabled="Boolean(savingKey)" v-model="im.bot_id" :placeholder="hasWecom ? '已配置（***），修改需完整填写' : '企业微信后台「智能机器人」获取'" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="企微 bot_secret">
                    <el-input :disabled="Boolean(savingKey)" v-model="im.secret" :placeholder="hasWecom ? '已配置（***）—— 不修改则留空' : '企微后台获取的 secret'" show-password />
                  </el-form-item>
                </el-col>
              </el-row>
              <div class="bc-hint">
                保存仅替换企微渠道的凭证（不影响该实例其他渠道）并自动重连；修改时需同时填写 bot_id 与 secret。
              </div>
            </el-form>
            <div class="section__foot">
              <el-button v-if="hasWecom" type="danger" plain :disabled="Boolean(savingKey)" :loading="savingKey === 'im-clear'" @click="onClearIm">
                清除企微专属 Bot
              </el-button>
              <el-button type="primary" :disabled="!dirty.channel || Boolean(savingKey)" :loading="savingKey === 'im'" @click="saveIm">保存并重连</el-button>
            </div>

            <!-- 飞书专属连接（FeishuChannel，ADR-0015）：与企微并列的独立凭证 -->
            <div class="subchannel">
              <div class="subchannel__head">
                <div>
                  <span class="bc-strong bc-text-sm">飞书专属连接</span>
                  <span class="bc-muted bc-text-xs">
                    开放平台「企业自建应用」App ID/Secret；应用需开启长连接事件订阅（im.message.receive_v1）
                  </span>
                </div>
                <div v-if="feishuConn" class="section__conn">
                  <StatusBadge :type="feishuConn.is_connected ? 'ok' : 'err'" :text="feishuConn.is_connected ? '已连接' : '未连接'" />
                  <span v-if="feishuConn.last_error" class="bc-text-xs bc-muted">{{ feishuConn.last_error }}</span>
                </div>
              </div>
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="飞书 App ID">
                    <el-input :disabled="Boolean(savingKey)" v-model="fs.app_id" :placeholder="hasFeishu ? '已配置（***），修改需完整填写' : '开放平台应用的 App ID（cli_…）'" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="飞书 App Secret">
                    <el-input :disabled="Boolean(savingKey)" v-model="fs.app_secret" :placeholder="hasFeishu ? '已配置（***）—— 不修改则留空' : '开放平台获取的 App Secret'" show-password />
                  </el-form-item>
                </el-col>
              </el-row>
              <div class="section__foot">
                <el-button v-if="hasFeishu" type="danger" plain :disabled="Boolean(savingKey)" :loading="savingKey === 'fs-clear'" @click="onClearFeishu">
                  清除飞书专属连接
                </el-button>
                <el-button type="primary" :disabled="!(fs.app_id.trim() || fs.app_secret.trim()) || Boolean(savingKey)" :loading="savingKey === 'fs'" @click="saveFeishu">
                  保存飞书凭证并重连
                </el-button>
              </div>
            </div>
          </div>

          <!-- ⑥ 生效预览：把「留空=沿用角色」变成可验证的结果 -->
          <div v-show="activeSection === 'preview'" class="bc-panel section">
            <div class="section__head">
              <div>
                <h3 class="section__title">生效预览</h3>
                <p class="bc-muted bc-text-sm section__desc">
                  选一个用户，看治理脊柱对该用户的真实判定——实例只能在角色天花板下收窄，这里显示收窄后的结果。
                  基于已保存配置，不执行工具或调用模型。人格与模型下方仅为绑定说明，不含具体会话和灰度解析。
                </p>
              </div>
            </div>
            <el-alert v-if="dirtyCount" title="当前有未保存修改，检查结果不包含草稿。" type="warning" :closable="false" />
            <div class="preview__picker">
              <el-select
                v-model="preview.userId"
                filterable
                clearable
                placeholder="选择用户（按该用户的角色计算）"
                style="width:100%;max-width:320px"
                @change="runPreview"
              >
                <el-option v-for="u in users" :key="u.id" :label="`${u.display_name || u.name}（${u.role}）`" :value="u.id" />
              </el-select>
              <span class="bc-muted bc-text-xs">切换用户自动试算</span>
            </div>

            <InlineError :message="preview.error" @retry="runPreview" />
            <p v-if="preview.checkedAt" class="bc-muted">检查时间：{{ preview.checkedAt }} · 当前用户 · 已保存配置</p>
            <div v-if="preview.loading" class="bc-muted bc-text-sm">试算中…</div>
            <div v-else-if="preview.result" class="preview">
              <div class="preview__row">
                <span class="preview__k">判定</span>
                <el-tag :type="preview.result.decision === 'allowed' ? 'success' : 'danger'" effect="plain">
                  {{ preview.result.decision === 'allowed' ? '允许' : '拒绝' }}
                </el-tag>
                <span class="bc-text-sm bc-muted">{{ preview.result.detail }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">人格绑定说明</span>
                <span class="bc-text-sm">{{ effectivePrompt }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">模型绑定说明</span>
                <span class="bc-text-sm">{{ effectiveModel }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可用工具</span>
                <span class="bc-text-sm">{{ toolsText }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可见知识域</span>
                <span class="bc-text-sm">{{ listText(preview.result.knowledge_scope_tags, '无') }}</span>
              </div>
              <div class="preview__row">
                <span class="preview__k">可见知识库</span>
                <span class="bc-text-sm">
                  {{ preview.result.is_admin ? '全部（admin）' : `${preview.result.visible_kb_ids.length} 个` }}
                </span>
              </div>
            </div>
            <p v-else class="bc-hint">选择一个用户后自动试算；未选用户时该实例按角色基线生效。</p>
          </div>
        </div>
      </div>
      <div v-if="dirtyCount" class="draft-summary" role="status">{{ dirtyCount }} 个分区尚未保存 · 请在对应分区保存</div>
    </template>

    <EmptyState
      v-if="!loading && !agent && !loadError"
      icon="Avatar"
      title="数字员工不存在"
      description="可能已被删除，返回列表查看现有实例。"
    >
      <el-button type="primary" @click="goBack">返回列表</el-button>
    </EmptyState>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, ChatDotRound, Connection, DataAnalysis, InfoFilled, Lock, MagicStick, View,
} from '@element-plus/icons-vue'
import { admin } from '../api'
import PageHeader from '../components/PageHeader.vue'
import ContentLoader from '../components/ContentLoader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import InlineError from '../components/InlineError.vue'
import { useSectionDraft } from '../composables/useSectionDraft'
import { errorMessage } from '../utils/errors'

const route = useRoute()
const router = useRouter()
const agentId = computed(() => Number(route.params.id))

const loading = ref(true)
const loadError = ref('')
const optionError = ref('')
const saveError = ref('')
const statusError = ref('')
const savedAt = reactive({})
const checkOpen = ref(false)
let statusTimer
let previewSeq = 0
const agent = ref(null)
const savingKey = ref('')
const reconnecting = ref(false)
const uploadingAvatar = ref(false)
const avatarInput = ref(null)
const avatarUrl = ref('')
const imStatusData = ref({ channels: [] })

// 左导航分段：一次只渲染一段，改一项不必滚到底（ADR-0013）
const activeSection = ref('basic')
const sections = [
  { key: 'basic', label: '基础信息', icon: InfoFilled },
  { key: 'persona', label: '人格与模型', icon: MagicStick },
  { key: 'capability', label: '能力边界', icon: Lock },
  { key: 'visibility', label: '可见性', icon: View },
  { key: 'channel', label: '渠道接入', icon: Connection },
  { key: 'preview', label: '生效预览', icon: DataAnalysis },
]
const basicDraft = useSectionDraft({ display_name: '', description: '', channel: 'api', is_enabled: true })
const basic = basicDraft.form
const personaDraft = useSectionDraft({ system_prompt_version: null, model_id: null })
const persona = personaDraft.form
const promptSelection = computed({ get: () => persona.system_prompt_version ?? '', set: (v) => { persona.system_prompt_version = v === '' ? null : v } })
const modelSelection = computed({ get: () => persona.model_id ?? '', set: (v) => { persona.model_id = v === '' ? null : v } })
const capabilityDraft = useSectionDraft({ skills: null, knowledge_scope_tags: null, allowed_tools: null })
const capability = capabilityDraft.form
const visibilityDraft = useSectionDraft({ visible_roles: null })
const visibility = visibilityDraft.form
const feishuConn = computed(() =>
  (imStatusData.value.channels || []).find(
    (c) => c.agent_id === agentId.value && (c.channel === 'feishu' || c.key?.startsWith('feishu:')),
  ) || null,
)
const fs = reactive({ app_id: '', app_secret: '' })
const hasFeishu = computed(() => imChannels.value.includes('feishu'))

async function saveFeishu() {
  const appId = fs.app_id.trim()
  const appSecret = fs.app_secret.trim()
  if (!appId || !appSecret) {
    ElMessage.warning('请同时填写 App ID 与 App Secret（保存将整体替换飞书凭证）')
    return
  }
  if (!await patch('fs', { im_config: { feishu: { app_id: appId, app_secret: appSecret } } }, '飞书凭证已保存，正在连接')) return
  fs.app_id = ''
  fs.app_secret = ''
  clearTimeout(statusTimer)
  statusTimer = setTimeout(refreshStatus, 1500)
}

async function onClearFeishu() {
  const ok = await ElMessageBox.confirm(
    '确定清除该实例的飞书专属连接？清除后其连接立即断开（其他渠道不受影响）。',
    '清除飞书专属连接',
    { type: 'warning' },
  ).catch(() => null)
  if (!ok) return
  if (!await patch('fs-clear', { im_config: { feishu: {} } }, '飞书专属连接已清除')) return
  fs.app_id = ''
  fs.app_secret = ''
  await refreshStatus()
}

const im = reactive({ bot_id: '', secret: '' })
// 各段"上次保存后"的基线：用于未保存提示与保存按钮可用态

// 已配置专属凭证的渠道列表（AgentOut.im_channels，随保存响应自动更新）
const imChannels = computed(() => agent.value?.im_channels || [])
const hasWecom = computed(() => imChannels.value.includes('wecom'))

// 选项
const prompts = ref([])
const chatModels = ref([])
const allSkills = ref([])
const toolOptions = ref([])
const roles = ref([])
const users = ref([])
const roleOptions = ref([])
const sourceOptions = ref([])

// 生效预览（策略模拟器同源端点：agent_id 让能力集按实例收窄）
const preview = reactive({ userId: null, loading: false, result: null, error: '', checkedAt: '' })

const wecomConn = computed(() =>
  (imStatusData.value.channels || []).find(
    (c) => c.agent_id === agentId.value && (c.channel === 'wecom' || c.key?.startsWith('wecom:')),
  ) || null,
)

function channelLabel(ch) { return { api: '对话测试', wecom: '企微', feishu: '飞书' }[ch] || ch }
function listText(list, fallback) {
  return list?.length ? list.join('、') : fallback
}
function modelLabel(id) {
  const m = chatModels.value.find((x) => x.id === id)
  return m ? (m.display_name || m.name) : `#${id}`
}

// ============ 未保存改动 ============
// 头像不参与本区保存：它是独立的即时动作（选图即上传），因此不进快照——
// 「放弃修改」不会回滚已上传的头像，脏标记也不会被它带偏。
const drafts = { basic: basicDraft, persona: personaDraft, capability: capabilityDraft, visibility: visibilityDraft }
const basicSnap = basicDraft.payload
const personaSnap = personaDraft.payload
const capabilitySnap = capabilityDraft.payload
const visibilitySnap = visibilityDraft.payload
const dirty = computed(() => ({
  ...Object.fromEntries(Object.entries(drafts).map(([key, draft]) => [key, draft.dirty.value])),
  channel: Boolean(im.bot_id.trim() || im.secret.trim() || fs.app_id.trim() || fs.app_secret.trim()),
}))
const dirtyCount = computed(() => Object.values(dirty.value).filter(Boolean).length)
const resetBasic = basicDraft.reset
function markBaseline() {
  for (const key of Object.keys(drafts)) acceptSection(key, agent.value)
}
function acceptSection(key, value) {
  const draft = drafts[key]
  const fields = Object.keys(draft.form)
  draft.accept(Object.fromEntries(fields.map((field) => [field, Array.isArray(value[field]) && !value[field].length ? null : value[field] ?? null])))
}
function modeFor(form, field) {
  return computed({
    get: () => form[field] == null ? 'inherit' : 'custom',
    set: (mode) => { form[field] = mode === 'inherit' ? null : [] },
  })
}
const skillMode = modeFor(capability, 'skills')
const toolMode = modeFor(capability, 'allowed_tools')
const knowledgeMode = modeFor(capability, 'knowledge_scope_tags')
const visibilityMode = modeFor(visibility, 'visible_roles')
async function confirmLeave() {
  if (savingKey.value || uploadingAvatar.value) {
    ElMessage.warning('正在保存，请等待完成后离开')
    return false
  }
  if (!dirtyCount.value) return true
  return ElMessageBox.confirm(`有 ${dirtyCount.value} 个分区尚未保存，离开将丢失这些修改。`, '未保存的修改', {
    confirmButtonText: '放弃并离开', cancelButtonText: '继续编辑', type: 'warning',
  }).then(() => true).catch(() => false)
}
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate(async (to, from) => to.params.id === from.params.id || await confirmLeave())
function beforeUnload(event) {
  if (dirtyCount.value || savingKey.value) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => { window.removeEventListener('beforeunload', beforeUnload); clearTimeout(statusTimer); previewSeq++ })
watch(() => route.params.id, () => { previewSeq++; preview.userId = null; preview.result = null; load() })

// ============ 头像（本地图片上传，后端落盘 + 站内路径回吐）============
const avatarInitial = computed(() =>
  (basic.display_name || agent.value?.name || '?').slice(0, 1).toUpperCase(),
)

function pickAvatar() {
  avatarInput.value?.click()
}

function applyAvatar(url) {
  // 头像不进 basicSnap/基线，故这里只回填展示值——不会把本区其他字段的脏标记带偏
  avatarUrl.value = url
  if (agent.value) agent.value = { ...agent.value, avatar_url: url }
}

async function onAvatarFile(e) {
  const file = e.target.files?.[0]
  e.target.value = '' // 允许连续选择同一个文件
  if (!file) return
  uploadingAvatar.value = true
  try {
    const updated = await admin.uploadAgentAvatar(agentId.value, file)
    applyAvatar(updated.avatar_url)
    ElMessage.success('头像已更新')
  } catch {
    // 400/413 细节已由 api 拦截器 toast
  } finally {
    uploadingAvatar.value = false
  }
}

async function onRemoveAvatar() {
  uploadingAvatar.value = true
  try {
    await admin.deleteAgentAvatar(agentId.value)
    applyAvatar('')
    ElMessage.success('头像已移除')
  } catch {
    // 失败已由拦截器 toast
  } finally {
    uploadingAvatar.value = false
  }
}
const resetPersona = personaDraft.reset
const resetCapability = capabilityDraft.reset
const resetVisibility = visibilityDraft.reset

// ============ 生效预览 ============
const effectivePrompt = computed(() => agent.value?.system_prompt_version != null
  ? `v${agent.value.system_prompt_version}（实例绑定配置）` : '沿用角色 / 全局发布链路')
const effectiveModel = computed(() => agent.value?.model_id != null
  ? `${modelLabel(agent.value.model_id)}（实例绑定配置，会话绑定优先）` : '沿用运行时选择（会话 / 灰度 / 全局）')
const toolsText = computed(() => {
  const tools = preview.result?.allowed_tools || []
  if (tools.includes('*')) return '全部（admin 通配）'
  return listText(tools, '无')
})

async function runPreview() {
  const seq = ++previewSeq
  preview.result = null
  preview.error = ''
  preview.checkedAt = ''
  preview.loading = preview.userId != null
  if (preview.userId == null) return
  try {
    const result = await admin.simulatePolicy({ user_id: preview.userId, action: 'retrieve', agent_id: agentId.value })
    if (seq !== previewSeq) return
    preview.result = result
    preview.checkedAt = new Date().toLocaleTimeString('zh-CN')
  } catch (error) {
    if (seq === previewSeq) preview.error = errorMessage(error, '检查失败，请重试')
  } finally {
    if (seq === previewSeq) preview.loading = false
  }
}
function invalidatePreview() {
  previewSeq++
  preview.loading = false
  preview.result = null
  preview.checkedAt = ''
  preview.error = ''
}

// ============ 加载与保存 ============
let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  loading.value = true
  loadError.value = ''
  optionError.value = ''
  statusError.value = ''
  const optional = (request) => request.catch(() => { optionError.value = '部分配置选项加载失败，请重试后再编辑'; return [] })
  try {
    const [a, pr, mods, sks, tools, rs, us, sources, status] = await Promise.all([
      admin.getAgent(agentId.value),
      optional(admin.listPrompts()),
      optional(admin.listModels('chat')),
      optional(admin.listSkills()),
      optional(admin.listTools()),
      optional(admin.listRoles()),
      optional(admin.listUsers()),
      optional(admin.listSources()),
      admin.imStatus().catch(() => { statusError.value = '连接状态加载失败'; return { channels: [] } }),
    ])
    if (seq !== loadSeq) return
    agent.value = a
    if (!a) return
    prompts.value = pr
    chatModels.value = mods
    allSkills.value = sks
    toolOptions.value = tools
    roles.value = rs
    users.value = (us || []).filter((u) => u.is_active)
    roleOptions.value = ['admin', ...rs.map((r) => r.name)]
    sourceOptions.value = sources || []
    imStatusData.value = status

    avatarUrl.value = a.avatar_url || ''
    Object.assign(basic, {
      display_name: a.display_name || '',
      description: a.description || '',
      channel: a.channel || 'api',
      is_enabled: a.is_enabled,
    })
    persona.system_prompt_version = a.system_prompt_version ?? null
    persona.model_id = a.model_id ?? null
    capability.skills = a.skills ?? null
    capability.knowledge_scope_tags = a.knowledge_scope_tags ?? null
    capability.allowed_tools = a.allowed_tools ?? null
    visibility.visible_roles = a.visible_roles ?? null
    // 脱敏预览：值打码为 ***，不回填输入框（修改需完整填写）；渠道列表经 im_channels 计算
    im.bot_id = ''
    im.secret = ''
    fs.app_id = ''
    fs.app_secret = ''
    markBaseline()
    invalidatePreview()
  } catch (error) {
    if (seq === loadSeq) loadError.value = errorMessage(error, '无法加载数字员工')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function connectionFor(channel) { return (imStatusData.value.channels || []).find((c) => c.agent_id === agentId.value && (c.channel === channel || c.key?.startsWith(channel + ':'))) }
async function retryOptions() {
  optionError.value = ''
  try {
    const values = await Promise.all([admin.listPrompts(), admin.listModels('chat'), admin.listSkills(), admin.listTools(), admin.listRoles(), admin.listUsers(), admin.listSources()])
    ;[prompts.value, chatModels.value, allSkills.value, toolOptions.value, roles.value, users.value, sourceOptions.value] = values
    roleOptions.value = ['admin', ...roles.value.map((r) => r.name)]
  } catch { optionError.value = '部分配置选项加载失败，请重试后再编辑' }
}
async function refreshStatus() {
  try { imStatusData.value = await admin.imStatus(); statusError.value = '' }
  catch (error) { statusError.value = errorMessage(error, '连接状态刷新失败，当前可能为旧状态') }
}
async function patch(key, payload, okMsg) {
  if (savingKey.value) return false
  savingKey.value = key
  saveError.value = ''
  try {
    const updated = await admin.updateAgent(agentId.value, payload)
    agent.value = updated
    if (drafts[key]) acceptSection(key, updated)
    savedAt[key] = new Date().toLocaleTimeString('zh-CN')
    invalidatePreview()
    ElMessage.success(okMsg || '已保存')
    if (key === 'basic') await refreshStatus()
    return true
  } catch (error) {
    saveError.value = errorMessage(error, '保存失败，修改已保留，请重试')
    return false
  } finally { savingKey.value = '' }
}

function saveBasic() {
  return patch('basic', { ...basicSnap() }, '基础信息已保存')
}

function savePersona() {
  return patch('persona', { ...personaSnap() }, '人格与模型已保存')
}

function saveCapability() {
  if (Object.values(capability).some((value) => Array.isArray(value) && !value.length)) {
    saveError.value = '请至少选择一项，或切换为沿用角色'; return
  }
  return patch('capability', { ...capabilitySnap() }, '能力边界已保存')
}

function saveVisibility() {
  if (Array.isArray(visibility.visible_roles) && !visibility.visible_roles.length) { saveError.value = '请选择角色，或切换为全部角色'; return }
  return patch('visibility', { ...visibilitySnap() }, '可见性已保存')
}

async function saveIm() {
  const botId = im.bot_id.trim()
  const secret = im.secret.trim()
  if (!botId && !secret) {
    ElMessage.warning(hasWecom.value ? '未填写任何修改；如需移除请点「清除企微专属 Bot」' : '请填写 bot_id 与 secret')
    return
  }
  if (!botId || !secret) {
    ElMessage.warning('请同时填写 bot_id 与 secret（保存将整体替换企微凭证）')
    return
  }
  // 按渠道合并语义：只替换 wecom 渠道，不影响该实例其他渠道
  if (!await patch('im', { im_config: { wecom: { bot_id: botId, secret } } }, '企微凭证已保存，正在连接')) return
  im.bot_id = ''
  im.secret = ''
  // 后端保存后自动重连该槽位；稍候刷新连接状态
  clearTimeout(statusTimer)
  statusTimer = setTimeout(refreshStatus, 1500)
}

async function onClearIm() {
  const ok = await ElMessageBox.confirm(
    '确定清除该实例的企微专属 Bot？清除后其连接立即断开（其他渠道不受影响）。',
    '清除企微专属 Bot',
    { type: 'warning' },
  ).catch(() => null)
  if (!ok) return
  // 按渠道移除：wecom 置空分组即移除该渠道，不动其他渠道
  if (!await patch('im-clear', { im_config: { wecom: {} } }, '企微专属 Bot 已清除')) return
  im.bot_id = ''
  im.secret = ''
  await refreshStatus()
}

async function onReconnectBot() {
  reconnecting.value = true
  try {
    const r = await admin.reloadIm(agentId.value)
    ElMessage.success(r.message || '专属连接已重连')
    await refreshStatus()
  } catch {
    // 失败已由拦截器 toast
  } finally {
    reconnecting.value = false
  }
}

function goBack() {
  router.push({ name: 'agents' })
}

function goChat() {
  router.push({ name: 'chat', query: { agent_id: String(agentId.value) } })
}

onMounted(load)
</script>

<style scoped>
.save-state { display:block; font-size:12px; color:var(--bc-text-3); margin-top:6px; }
.config-mode { display:flex; width:100%; flex-wrap:wrap; margin-bottom:8px; }
.check-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; }
.check-grid p { font-size:14px; color:var(--bc-text-3); margin-top:8px; }
.draft-summary { position:sticky; bottom:12px; z-index:5; padding:12px 20px; background:var(--bc-bg-surface); border:1px solid var(--bc-border); border-radius:var(--bc-radius-md); box-shadow:var(--bc-shadow-sm); font-size:14px; }
@media(max-width:600px) { .check-grid { grid-template-columns:1fr; } .section__head { flex-wrap:wrap; } .section { padding:16px; } }

.statusbar {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.statusbar__id {
  margin-left: auto;
}

/* 左导航 + 右内容：改一项不必在长页面里翻找 */
.detail-layout {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  gap: var(--bc-space-5);
  align-items: start;
}
.sections {
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: sticky;
  top: calc(var(--bc-header-height) + var(--bc-space-4));
}
.sections__item {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  width: 100%;
  padding: var(--bc-space-2) var(--bc-space-3);
  border: none;
  border-radius: var(--bc-radius-md);
  background: transparent;
  color: var(--bc-text-2);
  font: var(--bc-weight-medium) var(--bc-text-sm) / 20px inherit;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--bc-duration-fast) var(--bc-ease-out), color var(--bc-duration-fast) var(--bc-ease-out);
}
.sections__item:hover {
  background: var(--bc-gray-2);
  color: var(--bc-text-1);
}
.sections__item.is-on {
  background: var(--bc-primary-1, var(--bc-primary-2));
  color: var(--bc-primary-8);
}
.sections__label {
  flex: 1;
  min-width: 0;
}
.sections__dot {
  width: 6px;
  height: 6px;
  border-radius: var(--bc-radius-full);
  background: var(--bc-warning);
  flex-shrink: 0;
}
@media (max-width: 900px) {
  .detail-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .sections {
    position: static;
    flex-direction: row;
    flex-wrap: wrap;
  }
  .sections__item { width: auto; flex: 1 1 120px; }
}

.section {
  padding: var(--bc-space-5) var(--bc-space-6);
}
.section + .section {
  margin-top: var(--bc-space-4);
}
.section__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--bc-space-4);
  margin-bottom: var(--bc-space-4);
}
.section__title {
  margin: 0 0 var(--bc-space-2);
  font-size: var(--bc-text-md);
}
.section__desc {
  margin: 0;
  line-height: var(--bc-leading-relaxed);
  max-width: 68ch;
}
.section__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-2);
  flex-shrink: 0;
}
.section__conn {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-shrink: 0;
}
.section__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--bc-space-3);
  margin-top: var(--bc-space-2);
}
/* 头像选择：左预览 + 右操作，上传即时生效（与分区保存解耦） */
.avatar-picker {
  display: flex;
  align-items: flex-start;
  gap: var(--bc-space-3);
}
.avatar-picker__preview {
  width: 64px;
  height: 64px;
  flex-shrink: 0;
  border-radius: var(--bc-radius-full);
  background: var(--bc-primary-2);
  color: var(--bc-primary-8);
  font-size: var(--bc-text-lg);
  font-weight: var(--bc-weight-semibold);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.avatar-picker__preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.avatar-picker__body {
  min-width: 0;
}
.avatar-picker__file {
  display: none;
}
.avatar-picker__actions {
  display: flex;
  align-items: center;
  gap: var(--bc-space-1, 4px);
}
.preview__picker {
  display: flex;
  align-items: center;
  gap: var(--bc-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--bc-space-4);
}
.preview {
  display: flex;
  flex-direction: column;
  gap: var(--bc-space-2);
}
.preview__row {
  display: flex;
  align-items: baseline;
  gap: var(--bc-space-3);
  flex-wrap: wrap;
}
.preview__k {
  width: 76px;
  flex-shrink: 0;
  font-size: var(--bc-text-xs);
  color: var(--bc-text-3);
}
.subchannel {
  margin-top: var(--bc-space-5);
  padding-top: var(--bc-space-4);
  border-top: 1px solid var(--bc-border);
}
.subchannel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--bc-space-3);
  margin-bottom: var(--bc-space-3);
}
.subchannel__head > div:first-child {
  display: flex;
  align-items: baseline;
  gap: var(--bc-space-2);
  flex-wrap: wrap;
}
</style>

