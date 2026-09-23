# ADR-0015：飞书渠道接入——自研长连接协议层 + 卡片伪流式

- **状态**：✅ 已采纳（代码已落地）
- **日期**：2026-09-23
- **背景**：ADR-0012 的多 Bot 渠道体系定义了渠道抽象与注册表，但 `SUPPORTED_CHANNEL_TYPES`
  长期只有 `wecom`，飞书在控制台只是占位卡。阶段 3 的「参考场景与真实使用验证」需要第二个
  真实渠道来证明「渠道抽象 + 治理脊柱」可复用（而非企微一次性适配），且飞书是国内企业 IM 的
  第二常见选择。
- **关联**：[ADR-0012](0012-multi-bot-channels.md)（多 Bot 渠道）、
  [ADR-0004](0004-single-tenant-priority.md)（单组织私有部署）、`architecture.md` 红线 #2
  （Session 隔离）、`docs/execution-plan.md` T3.5。

## 背景

飞书开放平台的事件投递有两种模式：

1. **Webhook（回调 URL）**：需要公网可达的 HTTPS 端点，且需处理 URL 校验握手、
   加密/签名（Encrypt Key / Verification Token）。对「内网私有部署」是硬约束。
2. **WebSocket 长连接**：应用主动外连 `open.feishu.cn` 换取端点后长连接收事件，
   无需公网入口。官方仅提供 SDK 用法（Go/Java/Python/Node 四套官方 SDK + 官方
   OpenClaw 插件），**未公开 wire protocol 文档**。

参考项目调研（hermes-agent / openclaw-lark / 官方 lark-oapi 1.7.3 源码）结论：

- **hermes-agent**（NousResearch，Python）：默认长连接，但直接包官方 `lark_oapi.ws.Client`。
  该 SDK 在 import 期抓取**模块级全局事件循环**、`start()` 永久阻塞、**无公开停连 API**；
  hermes 被迫新建专用线程 + monkey-patch `ws_client_module.loop`，停连时置
  `_auto_reconnect=False` 再杀线程。其源码明确注明只支持**单 Bot**（一个进程一条飞书连接）。
- **openclaw-lark**（飞书官方团队出品）：同样基于官方 Node SDK 的长连接；流式回复用
  **CardKit 卡片实体 + 增量 PATCH + 节流**；`app_id` 是应用级身份，`open_id` 是 **app 维度**
  （同一人在不同 app 下不同），会话身份偏好 `union_id`。
- 协议要件可从官方 SDK 完整提取（端点换取、protobuf 帧、心跳、事件回执、合包、重连、握手错误头）。

## 决策

1. **传输层自研**（`gateway/feishu_wire.py`，对齐官方 lark-oapi 1.7.3 wire format），
   **不引入官方 Python SDK**。理由：
   - **与槽位模型根本冲突**：官方 SDK 的模块级全局 loop 与 `ChannelManager` 的
     「N 连接 × N 线程 × 各自事件循环」（ADR-0012）不兼容；bycall 需要**热重载干净停连**
     （飞书一个 app 允许多条并发连接且**全部收事件**，僵尸连接 = 消息重复处理，比企微
     单连接互踢更糟），而 SDK 无公开 stop。
   - **依赖代价**：官方 SDK 连带 `requests` / `requests_toolbelt` / `pycryptodome`
     （后者仅 webhook 加解密用）；bycall 已有的 `websockets`（企微 SDK 底层同款）+ `httpx`
     恰好就是 SDK 自身使用的栈。
   - **可控性**：生命周期事件可完全对齐 `WecomChannel` 契约（connected/reconnecting/
     disconnected/error → manager 槽位状态 + OTel span），协议编解码隔离在单文件，
     演进时只改一处。
2. **协议正确性锚定**：编解码用官方 SDK 生成的 golden bytes 做单测夹具
   （`tests/unit/test_feishu_gateway.py`），wire format 若与官方不一致立即红灯；
   ADR 记录参考版本（lark-oapi 1.7.3）以备演进。
3. **事件与回复**：
   - 入站订阅 `im.message.receive_v1`；文本直取、post 富文本拼接、其余类型降级占位文本
     （v1 不做附件下载），`system` 消息不投递。
   - 出站 **reply-first**（`/im/v1/messages/{id}/reply`），目标被撤回/不存在
     （230011/231003）回退 create 到会话；主动推送按 ID 前缀路由 receive_id_type。
   - **流式 = 卡片伪流式**：首帧发 interactive 卡片，后续 `PUT /im/v1/messages/{id}`
     更新卡片内容（飞书无企微式 WS 流式帧）；replace 语义与 `im_handler` 的累积全文
     完全对齐，建卡失败降级一次性文本。不依赖 CardKit 卡片实体 API（无需额外权限）。
4. **身份绑定用 `union_id` 优先**，缺失回退 `open_id`。`open_id` 是 app 维度——per-Agent
   多 Bot = 多飞书 app，同一员工在不同 app 下 `open_id` 不同，单值 `im_bindings.feishu`
   会绑不住；`union_id` 在同一开发者主体下跨 app 稳定。**前提：各飞书 app 注册在同一主体下**
   （部署文档注明，写入控制台提示）。
5. **`InboundMessage.chat_id` 仅在群聊填充**（飞书 p2p 也有 `oc_` 会话 ID，必须清空）；
   回复所需的真实 chat_id 经渠道内部上下文携带。群聊仍一律拒绝（红线不变，共同授权模型
   未落地前）。
6. **幂等复用既有 `IdempotencyRecord`**（长连接重连会重推事件，at-least-once；事件回执
   按官方语义：未齐分片与 card 帧不回执）。
7. **作用域与既有体系一致**：全局默认（`im.feishu_app_id/secret/domain`）+ per-Agent
   专属凭证（`im_config.feishu`），`app_id` 全局唯一（同一 app 建多条连接会重复收事件）。

## 备选方案

| 方案 | 为何不选 |
|------|---------|
| 包官方 `lark-oapi` SDK（hermes 路线） | 模块级全局 loop / 无公开停连 / 私有属性 monkey-patch，与多槽位热重载架构根本冲突；+3 传递依赖 |
| Webhook 回调模式 | 需公网 HTTPS 入口 + 加解密，与「单组织内网私有部署」（ADR-0004）冲突；官方长连接是默认推荐 |
| 只支持全局单 Bot（不做 per-Agent） | 放弃 ADR-0012 多 Bot 能力；`app_id` 唯一性与槽位模型已就绪，无必要收窄 |
| CardKit 真流式（openclaw-lark 路线） | 需卡片实体 API 与额外权限，节流/状态机复杂；v1 用消息更新接口已达成 replace 语义，列为后续增强 |

## 后果

**收益**：

- 无需公网入口即可接入飞书（与企微长连接形态一致，适配内网部署）。
- 渠道抽象被第二个真实渠道验证；数据面（`im_handler`/治理链路）零改动。
- 零新增运行时依赖；协议层隔离在 `feishu_wire.py`，可用 golden bytes 回归。

**代价与风险**：

- 自担协议维护：wire format 非公开文档，飞书演进需跟进。缓解：golden 夹具锚定 +
  参考版本记录 + 编解码单文件隔离（同 wire format 上跑着 4 套官方 SDK 与官方插件，
  破坏性变更必然协调发布）。
- v1 能力边界：不含附件下载/图片理解、卡片交互回调（button/表单）、@mention 门槛
  （群聊整体拒绝，门槛待共同授权模型落地）、CardKit 真流式与卡片模板。

**后续增强**（按真实使用反馈排期）：消息资源下载（`im/v1/messages/{id}/resources`）→
`Attachment`；卡片交互回调路由；群聊 @mention gating；CardKit 流式卡片。

## 落地文件

`src/bycall/gateway/feishu_wire.py`（协议层）、`src/bycall/gateway/feishu.py`（渠道）、
`src/bycall/gateway/channels.py`（注册表）、`src/bycall/gateway/manager.py`（全局双渠道 +
专属分派）、`src/bycall/db.py`（设置种子）、`src/bycall/tools/approval.py`（审批回推按渠道）、
`frontend/src/views/Channels.vue` / `AgentDetail.vue`（凭证表单）、
`tests/unit/test_feishu_gateway.py`（25 例，含 golden 锚定）。
