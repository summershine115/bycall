# 评测集（阶段 2 · T2.1–T2.4）

本指南说明评测入口。所有命令在仓库根目录执行；CLI 在 `evals/`，引擎与内置用例在 `src/bycall/evals/`。

## 常用命令

```bash
make eval                 # 跑四类评测集全部用例（当前 34 条）
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
