# 评测集（阶段 2 · T2.1–T2.4）

本目录是 execution-plan 约定的评测入口；引擎与内置用例在 `src/bycall/evals/`。

## 常用命令

```bash
make eval                 # 跑四类评测集全部用例
make eval-security        # 只跑权限安全红线用例（五类攻击 × ≥3 条）
python -m evals.run --list
python -m evals.run --snapshot evals/snapshots/baseline.json   # 抓当前配置快照
python -m evals.run --compare evals/snapshots/baseline.json evals/snapshots/candidate.json
```

回放命令输出：配置 diff → 按变更类型选择的评测子集 → 旧/新两份报告 → 判定
（是否出现越权或质量回退）。任一新配置用例失败，进程退出码为 1——CI 与发布门都据此红灯。

快照只存内容指纹（SHA-256）与配置结构，**不存提示词正文 / 密钥**，可安全提交进仓库。
