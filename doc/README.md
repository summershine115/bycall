# Bycall 文档入口

> 更新：2026-10-15。项目文档统一放在 `doc/`；文中的 `src/`、`tests/`、`evals/` 等代码路径以及 shell 命令均以仓库根目录为基准。

## 阅读顺序与权威来源

| 文档 | 用途 | 状态 |
|---|---|---|
| [执行计划](execution-plan.md) | 当前任务方向、阶段与验收范围 | 阶段 3 进行中 |
| [治理交付与后置边界](gaps.md) | 逐项说明已实现什么、尚未实现什么 | 当前代码核对 |
| [产品设计](product-design.md) | 产品形态、信任模型与历史路线图 | 随实现演进 |
| [架构](architecture.md) | 当前模块与数据流、不可违背的红线 | 随实现演进 |
| [决策登记册](decisions/README.md) | ADR 索引与开放决策的唯一入口 | 已采纳决策与状态 |
| [策略建议](product-strategy.md) | 方向判断与历史建议 | 建议稿；不覆盖执行计划 |

## 指南

- [RAG 运行与验证](guides/rag.md)：索引发布、权限、降级排序、故障状态与引用边界。
- [评测与回放](guides/evaluation.md)：本地验证、快照、配置对比和发布门。
- [参考连接器](guides/reference-connectors.md)：示例 MCP 连接器接入和角色治理。
- [T3.2 真机演示](guides/demo-t3.2.md)：三条平台流程的人工演示动线。

## 规格与调研

- [Agent 框架规格](specs/agent-framework-upgrade.md)：S1/S2 已落地，S3/S4 冻结。
- [T3.2 平台流程](specs/t3.2-platform-flows.md)：自动化验收已完成，真机验证待完成。
- [QM 对标](qm-benchmark.md)、[TencentDB Agent Memory 对标](tencent-agent-memory-benchmark.md)：历史调研，不代表全部机制已实现。

## 提案与归档

- [开源定位](open-source-positioning.md) 为尚未采纳的建议；[开源清单](open-source-checklist.md) 为待执行清单，不代表已经通过发布检查。
- `archive/tasks/` 保存原实施计划与任务勾选；`archive/frontend-theme/` 保存已跟踪的前端接入包。归档中的步骤、路径和勾选保留历史，不作为当前实施指令。

## 维护约定

1. 新文档放入 `doc/` 对应子目录，并登记在本入口；根目录只保留标准入口 [README](../README.md)、[CHANGELOG](../CHANGELOG.md)、[AGENTS](../AGENTS.md)。工具技能的 `SKILL.md` 保持工具要求的位置。
2. 相对 Markdown 链接按当前文档所在目录解析；代码路径和命令按仓库根目录解释。
3. 当前事实、设计目标、后置项、历史记录须明确区分；以代码和可运行验证支撑“已完成”。
4. 新决策先写 ADR 并登记，编号递增；当前任务只由执行计划维护，历史笔记不产生第二份待办。
5. 移动文档时同步 README、AGENTS、正文引用及代码注释；外部文档必须标明在仓库外。
