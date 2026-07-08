-- Bycall PostgreSQL 初始化。
-- pgvector/pgvector:pg16 镜像已内置 pgvector 扩展，这里只启用。
-- M0 只起服务 + 建库（库由 POSTGRES_DB 环境变量建好），扩展留到 M5 持久化记忆时用。

-- 启用 pgvector（向量类型 + ivfflat/hnsw 索引），M5 长期记忆需要。
CREATE EXTENSION IF NOT EXISTS vector;

-- 预留：M5 记忆/状态表将建在 bycall schema 下，避免与未来其他服务混用 public。
CREATE SCHEMA IF NOT EXISTS bycall;
