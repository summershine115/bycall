# 评测集（阶段 2 · T2.1–T2.4）

本指南说明评测入口。所有命令在仓库根目录执行；CLI 在 `evals/`，引擎与内置用例在 `src/bycall/evals/`。

## 常用命令

```bash
make eval                 # 跑四类评测集全部用例（当前 35 条）
make eval-security        # 只跑权限安全红线用例（五类攻击 × ≥3 条，当前 21 条）
python -m evals.run --list
python -m evals.run --snapshot evals/snapshots/baseline.json   # 抓当前配置快照
python -m evals.run --compare evals/snapshots/baseline.json evals/snapshots/candidate.json
```

五类攻击中「审批绕过」覆盖审批门拦截、执行前重算能力集、工具失效拒绝执行，以及
T3.3 审批授权三档的越权边界（会话隔离 / 用户隔离 / 撤销即失效）。

回放命令输出：配置 diff → 按变更类型选择的评测子集 → 旧/新两份报告 → 判定
（是否出现越权或质量回退）。任一新配置用例失败，进程退出码为 1——CI 与发布门都据此红灯。

快照只存内容指纹（SHA-256）与配置结构，**不存提示词正文 / 密钥**，可安全提交进仓库。

## RAG 回归覆盖（ADR-0016，2026-10-15）

`knowledge_recall_publication` 使用临时 SQLite + 真实 zvec 验证已知答案、无关知识库
竞争、无答案问题，以及已写入但未发布的索引代不可召回。embedding 使用离线桩，
此用例验证授权、发布与召回链路，不代表真实 embedding 模型的语义质量。

单元/API 回归同时覆盖：

- `tests/unit/test_knowledge_api.py`：部分写入后降密失败回滚、未发布代隔离、旧代清理故障、
  重建统计、父子块实际召回、多库统一重排、同会话改写与最终证据链。
- `tests/unit/test_rag_quality.py`：多库候选竞争、关键词降级的无答案、非法重排结果、
  无命中/部分故障/全故障状态、实际注入引用、改写回退、同步/流式故障披露。
- `tests/unit/test_rag.py` 与 `test_vector_store_zvec.py`：embedding 后续批失败不写入、
  维度/数量/非有限值检查、父块 ACL 与文档归属、child-only 召回、未知 filter 拒绝。

真实模型验收仍需业务语料与人工标注：测量 Recall@K、排序与无答案准确率，校准激活
reranker 的阈值，再验证真实企微/飞书回答。离线回归通过不能替代这一步。
