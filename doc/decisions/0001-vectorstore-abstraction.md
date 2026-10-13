# ADR-0001：抽象 VectorStore 接口

- **状态**：已采纳
- **日期**：2026-07-08（M0）
- **背景**：数字员工 RAG 检索后端选型

## 背景

zvec 已通过完整 PoC（CRUD / 混合检索 / WAL 崩溃恢复 / 性能基准 / RAG 端到端 / 并发读，
全 PASS），作为 RAG 知识库的起步向量库。但 zvec 项目较新，长期维护存在不确定性，
且不同里程碑的规模/权限/并发需求可能超出 zvec 适用范围。

## 决策

定义后端无关的 `VectorStore` 抽象接口（`src/bycall/vector_store/base.py`），
zvec 是第一个实现（`ZvecStore`），业务层只依赖抽象，不直接 import zvec。

接口对齐迁移目标（Qdrant / pgvector / Milvus）的概念：
`create_collection / open / insert / upsert / delete / search / flush / close`，
一次 `search` 可同时给 vector + full_text + filter（混合检索），
`filters` 用结构化 `FilterCondition`（不透传后端 DSL 字符串）。

## 迁移触发条件

任一满足即迁：

| 条件 | 目标后端 |
|------|---------|
| 规模超千万级 chunk | Milvus（分布式、GPU 索引） |
| 需原生 RBAC + 复杂权限模型 | pgvector（Postgres RLS）或 Qdrant |
| 多 Agent 实例共享知识库 + 高并发 | Qdrant / Milvus（服务端） |

## 后果

**优点**：
- 业务代码与具体向量库解耦，迁移时只换实现 + factory 注册，不改调用方。
- 权限预过滤（HANDOVER §4.1 红线）在抽象层就有位置承载（`SearchRequest.filters`）。
- 测试可针对抽象接口写，zvec 实现的真实集成测试单独跑。

**代价**：
- 多一层抽象，zvec 的部分高级能力（如特定 index_param）需在实现层暴露配置。
- 抽象需保持克制，避免过早设计一堆用不到的方法（YAGNI）。

## 约束

- zvec 实现必须封装好其特有的坑（单批 ≤1024、单等号 filter、无 close()、多进程只读），
  对外只暴露干净接口——见 `ZvecStore` 类文档与注释。
- zvec 只做 **RAG 知识库**（读密集、大规模），**不**做 agent 长期记忆
  （记忆是写密集、小规模，用 Postgres+pgvector，见 HANDOVER §4.4）。

## 相关

- 接口定义：`src/bycall/vector_store/base.py`
- zvec 实现：`src/bycall/vector_store/zvec_store.py`
- 迁移目标对比：`digital-employee-plan.md` §1.2 决策 4
