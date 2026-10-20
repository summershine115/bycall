# ADR-0018：开源发布——MIT 许可 + 公开 GitHub 仓库（阶段 A）

- **状态**：✅ 已采纳（2026-10-20 用户拍板）
- **日期**：2026-10-20
- **背景**：项目已完成阶段 2 评测与发布门，本地 `make ci-local` 可复现（lint + 全部测试 +
  四类评测 + 安全红线）。需要把仓库公开发布到 GitHub，作为"组织授权下的可靠执行"的架构
  参考实现对外交付。发布前必须解决许可、仓库归属、凭证与工具残留三类问题；
  `open-source-checklist.md` 只覆盖"无害 + 可进入"，不涉及叙事重写。
- **关联**：`doc/open-source-positioning.md`、`doc/open-source-checklist.md`、ADR-0009、ADR-0010

## 背景

仓库自 M0 起即按"策略不活在提示词层"演进，治理脊柱有评测证明；但 `pyproject.toml`
仍写 `license = { text = "Proprietary" }`，且仓库内混入本地 Agent 工具目录（`.agents/`、
`.claude/`、`.codegraph/`、`.omo/`、`.senpi/`），不具备公开发布条件。
`open-source-positioning.md` 的治理叙事重写属于阶段 B，本次不做。

## 决策

1. **许可**：MIT，版权署名 `summershine115`（与 GitHub 账号一致）。
2. **仓库**：`https://github.com/summershine115/bycall`，**public**；
   `pyproject.toml` 的 license / authors / urls 同步为该仓库。
3. **README 只做阶段 A 最小增补**：免责声明、六道闸代码指针表、零依赖最小体验链路、
   License 一节；不重写现有功能叙事，不做双语，不做 OWASP 映射。
4. **开源定位仍未采纳**：`open-source-positioning.md` 保持"建议稿"，
   README 不按治理叙事整体重写。
5. **仓库卫生**：`.agents/`、`.claude/`、`.codegraph/`、`.omo/`、`.senpi/` 属于本地
   Agent / 工具产物，移出版本库并加入 `.gitignore`；项目内容只保留代码、文档与参考场景包。

## 备选方案

- **Apache-2.0**：专利授权条款更完整，但本项目无专利诉求；MIT 更贴合赛道惯例
  （Microsoft Agent Governance Toolkit、QM 均为 MIT）。
- **维持 Proprietary**：与公开仓库目标直接冲突，排除。
- **直接做阶段 B**（双语 README + OWASP Agentic Top 10 映射）：工作量与返工风险更大，
  推迟到发布后按真实反馈决定。

## 后果

- **历史不可回退**：仓库公开后，任何误提交凭证都需 `git filter-repo` + force push，
  且所有 clone 失效。因此 secret 扫描必须在首次 push **之前**完成（清单 A0）。
- **MIT 允许商业使用与再分发**，但不提供担保；README 已声明"非生产就绪、不提供安全担保"。
- **测试门槛不变**：`make ci-local` 仍是发布门，陌生人从 clone 到全绿是该项目的"进入测试"。
- 阶段 B（双语 README、OWASP 映射、首个 tag、CONTRIBUTING）继续留在
  `open-source-checklist.md` §7 的"不在本清单内"。
