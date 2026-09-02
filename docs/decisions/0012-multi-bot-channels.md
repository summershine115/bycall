# ADR-0012：多 Bot 渠道体系——per-Agent IM 凭证生效与按连接路由

- **状态**：✅ 已采纳（产品方向由用户 2026-09-02 拍板：做多 Bot 平台，每个数字员工可绑定专属 IM Bot）
- **日期**：2026-09-02
- **背景**：控制面存在两处 IM 配置但只有一处生效——`ChannelManager` 单槽位只读全局
  `SystemSetting im.*`，而 `Agent.im_config_encrypted` 只写不读（网关层零消费方），
  管理员在数字员工表单配置企微凭证后**不会产生任何运行时效果**，属于误导性 UI；
  同时幂等去重键与主动推送缺 Bot 维度，多连接下会串扰。
- **关联**：ADR-0004（单组织，`agent_id` 曾恒 None）、`architecture.md` 红线 #2、
  `product-design.md` §2.4、execution-plan T3.5

## 决策

1. **槽位模型**：`ChannelManager` 由单槽位改为多槽位 `dict[key, slot]`，
   `key = "wecom:global"` / `f"wecom:agent:{agent_id}"`；每槽位独立持有
   channel / daemon 线程 / 事件循环 / 生命周期状态。跨线程断开必须经
   `run_coroutine_threadsafe` marshal 到该槽位事件循环线程（沿用既有约定，
   见 `gateway/manager.py` `_stop_locked` 注释）。
2. **全局默认 Bot 保留为兜底**：`SystemSetting im.*` 语义不变——无专属 Bot 的
   入站流量路由到默认实例（`agent_id=None`，历史会话键完全兼容）。
3. **per-Agent 凭证真正生效**：`Agent.im_config`（Fernet 加密存库）由网关消费。
   启用且有 `im_config` 的实例在应用启动与热重载时各建一条企微长连接；
   `wecom_bot_id` **全局唯一**（Agent 之间、以及与全局默认 Bot 之间冲突即 400）。
4. **入站按连接路由**：`InboundMessage` 携带 `agent_id`（由收到消息的那条连接注入）；
   `im_handler` 据此 `get_or_create_conversation(..., agent_id=...)`，会话键
   `{channel}:{user_id}:agent:{id}`——红线 #2 的实例维度就此落地。实例失效
   （禁用/删除后仍有滞留消息）时礼貌拒绝，**不得回落默认实例**（防跨实例串权）。
5. **幂等与推送补 Bot 维度**：幂等去重键加入 Bot 标识（`agent_id`，全局槽位用
   `global`），消除跨 Bot `msg_id` 碰撞；主动推送 `send_proactive` 按
   `conversation.agent_id` 路由到对应连接，无专属连接时走全局槽位。
6. **控制面单一体系两个作用域**：渠道凭证收敛为"全局默认（渠道接入页）+
   per-Agent 专属（数字员工详情 IM 区）"；agent 的 `im_config` 变更 / 启停 / 删除
   自动 reload / stop 对应槽位；`GET /im/status` 返回全部连接列表；
   `POST /im/reload` 支持指定 agent 范围。
7. **边界**：企微群聊仍一律拒绝（冻结项不变）；飞书仅 UI 占位（无网关实现）；
   tenant 维度不变（ADR-0004 触发条件不受影响）。

## 备选方案

| 方案 | 结论 |
|------|------|
| 独立 `Channel` 表 + agent 外键绑定 | 拒绝：`Agent.im_config` 字段已预留、加密存储与脱敏回显管线已就绪；再加一张表徒增 join 与迁移成本，单组织规模下无收益 |
| 保持全局唯一 Bot，多 Agent 靠提示词区分 | 拒绝：会话入口无法按实例隔离，员工无法选择"找哪个数字员工"；与"数字员工 = 独立协作者"的产品形态冲突 |
| 引入 tenant 维度做多 Bot | 后置：ADR-0004 单组织优先的触发条件不变，本决策不触碰 tenant |

## 后果

- **正**：控制面两处 IM 配置从"一真一假"归一为"一套体系、两个作用域"，误导消除；
  产品可按 Bot 交付多个数字员工，会话/能力/模型三层按实例隔离（数据面本就支持）。
- **负/边界**：进程内线程数随 Bot 数线性增长（每 Bot 一条 daemon 线程 + 事件循环），
  单组织规模（个位数~十几个 Bot）可接受；企微侧每个 Bot 需独立应用凭证（运营侧准备）。
- **风险**：多线程生命周期管理复杂度上升——严格复用既有 marshal 断开约定，
  以单测（槽位生命周期）+ 集测（路由隔离/幂等/失效守卫）锁定。

## 修订记录

- **2026-09-02（IA 修订，不改决策）**：管理面板二次重组（数字员工 Hub 化）后，
  「渠道接入」不再是「系统」组下的独立页面，改为数字员工页 `/agents` 下的
  「渠道接入」标签页；全局默认凭证、连接总览、飞书占位整体迁入，旧链接重定向。
  决策第 6 条中「渠道接入页」的表述相应理解为该标签页；两个作用域
  （全局默认兜底 + per-Agent 专属）的配置语义、后端 API 与数据面行为均不变。
