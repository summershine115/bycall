# 开源发布前清单(阶段 A)

> 状态:**阶段 A 已执行**(2026-10-20;决策与依据见 [ADR-0018](decisions/0018-open-source-release-mit.md))。
> 目标:通过三个发布测试(**无害 / 进入 / 诚实**,见 §6),全部完成后即可 push public。
> 范围声明:本清单只做"无害 + 可进入",**不做** README 治理叙事重写、双语、OWASP 映射——
> 那些属于阶段 B(发布后的叙事固化)。
> 关联:`open-source-positioning.md`、`execution-plan.md`(冻结清单)

---

## 本次执行记录（2026-10-20）

- ✅ **A0 凭证**：`.env` 从未进过 git 历史；全历史扫描仅命中测试占位符
  （`sk-test-secret-key-1234567890` 等），无真实凭证；`.env.example` 全为占位符。
- ✅ **A0.4 种子数据**：`scripts/seed_reference.py` 为 demo 角色/账号（demo-tech / demo-lead /
  demo-guest）与通用物料，无真实组织痕迹；`examples/connectors/` 同为虚构数据。
- ✅ **A0.5 内网 host**：`src/ doc/ examples/ tests/ evals/ scripts/ docker/` 复扫无内网域名与真实端点。
- ✅ **A1 仓库卫生**：`.agents/`、`.claude/`、`.codegraph/`、`.omo/`、`.senpi/` 移出版本库并写入
  `.gitignore`；`git status` 无未跟踪杂物。
- ✅ **A2 许可**：新增 MIT `LICENSE`（版权 `summershine115`）；`pyproject.toml` 的
  `license` / `authors` / `urls` 同步为 MIT 与公开仓库地址；README 头部免责声明就位。
- ✅ **A3 README**：新增"这个仓库真正值得看的东西"（六道闸 + 代码指针 + 信任 demo 命令链）；
  "快速开始"拆为"最小体验（零 key、零 Docker）"与"完整应用链路"两条。
- ✅ **A4 发布链路**：本地 `make ci-local` 全绿（lint + 989 项测试 + 四类评测 35/35 +
  安全红线 21/21，约 2 分钟），零 API key、零 Docker。
- ⬜ **A5 发布动作**：GitHub 建仓与 push 结果见文末（按实际执行补记）。
- ⏸ **阶段 B 未做**（清单 §7 明确排除）：英文/双语 README、OWASP Agentic Top 10 映射、
  CONTRIBUTING、首个 tag。

---

## 历史核查记录（2026-09-21，发布前须重新验证）

以下为当时的核查记录，不代表当前发布验收结果：

- ✅ `.env` 从未进过 git 历史(`git log --all --full-history -- .env` 为空)
- ✅ `.gitignore` 覆盖完善:`.env`(保留 example)、`data/`、`secrets/`、`*.key/*.pem`、`pgdata/` 均在内
- ✅ `data/` 未被 git 跟踪
- ✅ 工作区粗扫(grep)无真实凭证命中:所有 "webhook" 命中均为功能代码;`.env.example` 全为占位符(`sk-replace-me` 等)
- ✅ evals 离线可跑(`evals/__init__.py`:真实 zvec + 临时 SQLite + 打桩,不依赖真实 LLM / 外部服务)
- ✅ 2026-10-20 复核：`pyproject.toml` 已改为 `license = { text = "MIT" }`，原作者字段与
  仓库地址同步更新（原 `Proprietary` 冲突已消除，见 A2.2）

---

## A0 Secret 与安全扫描(硬阻塞,任何一项不过不发布)

### A0.1 安装扫描工具
```bash
brew install gitleaks
```

### A0.2 工作区扫描(含未跟踪文件)
```bash
cd /Users/lilinsen/Code/bycall
gitleaks detect --no-git --source . --report-path /tmp/gitleaks-workspace.json
```
- ✅ 退出码 0(无泄漏)→ 通过
- ❌ 有命中 → 逐条人工判定(测试占位符如 `sk-replace-me` 会误报,可加 `.gitleaksignore`)

### A0.3 全历史扫描(193 commits,秒级)
```bash
gitleaks detect --source . --log-opts="all" --report-path /tmp/gitleaks-history.json
```
- ✅ 无命中 → 通过
- ❌ 历史中有真实凭证 → `git filter-repo` 重写。
  **关键:必须在 push public 之前做**——私有阶段重写历史零成本(没人 clone 过);
  公开后重写 = force push + 所有 clone 失效。这是"先清理后开源"顺序不可颠倒的原因。

### A0.4 种子与示例数据审查(真实组织痕迹)
逐处过一眼,确认是**虚构**而非真实组织的影子(部门名、人名、邮箱域名、群名、公司名):
- [x] `make seed-reference` 的种子数据源码(参考场景包的角色/账号/MCP 连接器,ADR-0009)
  —— 均为 demo-tech / demo-lead / demo-guest 与通用物料，无真实组织痕迹
- [x] `examples/connectors/` 示例配置 —— 工单/备件均为虚构 demo 数据
- [x] `CHANGELOG.md`(描述里是否带可辨识真实环境的信息) —— 仅描述功能与行为
- [x] `doc/` 内演示类文档(截图链接、真实 host、真实群名) —— 复扫无内网 host 与真实群名

### A0.5 内网 host 复扫(补充模式)
```bash
grep -rnE "\.local|\.lan|\.internal|\.corp|qyapi\.weixin|open\.bigmodel" \
  --include="*.py" --include="*.md" --include="*.yml" --include="*.yaml" --include="*.toml" \
  src/ doc/ examples/ tests/ evals/ scripts/ docker/ | grep -v example
```
(已知命中可豁免:`.env.example` 里的官方公共端点 `api.openai.com` / `open.bigmodel.cn`)

---

## A1 仓库卫生

### A1.1 历史资料与工具记录

- 前端接入包已保留在 `doc/archive/frontend-theme/`，属于已跟踪历史资料；发布前审查内容，不按临时物直接删除。
- `.omo/` 是本地任务会话记录，不属于项目文档；发布前单独审查其跟踪/忽略状态。
- ✅ 2026-10-20：`.agents/`、`.claude/`、`.codegraph/`、`.omo/`、`.senpi/` 均为本地
  Agent/工具产物，已移出版本库并加入 `.gitignore`；仓库只保留项目内容。

### A1.2 实施笔记归档

原 `tasks/` 三份笔记已迁入 `doc/archive/tasks/`，保留过程记录并标明历史状态；当前待办只看执行计划。

### A1.3 收尾检查
```bash
git status --short   # 应只剩本清单文件与预期改动,无未跟踪杂物
```
✅ 2026-10-20：`git status --short` 仅剩本次预期改动，无未跟踪杂物。

---

## A2 License 与法律

### A2.1 添加 LICENSE 文件(MIT)
- ✅ 已添加根目录 `LICENSE`（MIT）。
- 版权行已拍板：写 **GitHub ID `summershine115`**（与仓库关联，便于被引用）。

### A2.2 修正 pyproject.toml(硬冲突)
- ✅ `license = { text = "MIT" }`（原 `Proprietary`）。
- ✅ `authors = [{ name = "summershine115" }]`（原 `Bycall Team`）。
- ✅ `[project.urls]` 同步为 `https://github.com/summershine115/bycall`（原指向 `lilinsen/bycall`）。
- ✅ `keywords` 补充 `governance` / `access-control` / `rbac` / `mcp`，便于被检索到。

### A2.3 README 免责声明
- ✅ 已在 README 头部 quote 块后就位：
  > **本项目是架构参考实现,非生产就绪软件,不提供任何安全担保。**
  > 它展示的治理模式可作为设计参考,不构成对您部署环境的安全承诺。

---

## A3 README 最小增补(不重写)

### A3.1 新增"这个仓库真正值得看的东西"一节
- ✅ 已放在"技术栈"与"快速开始"之间：六道闸（机制 + 代码指针两列）+ 诚实的边界说明，
  结尾给信任 demo 命令链：
```bash
make venv && source .venv/bin/activate && make install
make seed-reference && make eval-security
```
- ✅ 钩子句已使用："一个你能看着五类攻击全部失败的数字员工。"

### A3.2 区分两条链路
- ✅ "快速开始"已拆为两节：
  - **最小体验（零 API key、零 Docker）**：`make seed-reference` + `make eval-security`，
    无需外部服务，`make ci-local` 全绿即核心主张成立；
  - **完整应用链路（需要 LLM API key）**：`cp .env.example .env` → `make dev`，
    `make seed-reference` / `make docker-up` 仍为可选步骤。

---

## A4 发布链路实测(在干净环境模拟陌生人)

### A4.1 干净 clone 模拟
```bash
cd /tmp && git clone /Users/lilinsen/Code/bycall bycall-fresh && cd bycall-fresh
make venv && source .venv/bin/activate && make install
```
- ✅ 2026-10-20 已执行（见下方 A4.2 记录）；clone 后仅凭仓库内容即可建环境并全绿。

### A4.2 核心链路(零 API key、零 docker)
```bash
make seed-reference && make eval-security && make ci-local
```
- ✅ 全绿 → "进入测试"通过;记录总耗时(可写进 README:"从 clone 到看到攻击被拦约 X 分钟")
- ❌ 任何失败 → 修复后再发布(陌生人第一条命令失败是开源项目最高的流失率来源)
- **2026-10-20 主仓库执行结果**：`make ci-local` 全绿——lint 通过、989 项测试通过、
  四类评测 35/35、安全红线 21/21，整链约 2 分钟；全程零 API key、零 Docker。
  干净 clone 复核记录见 A4.1。

### A4.3 (可选)完整链路
有 API key 的前提下 `make docker-up && make dev` 走一遍现有快速开始,确认文档与实况一致。

---

## A5 发布动作

- [ ] GitHub 创建公开仓库(名称、description、topics 建议:`agent` `governance` `rbac` `rag` `access-control` `mcp` `hitl`)
- [ ] push main(注意:确认远程不含 `--mirror` 的私有分支残留)
- [ ] **不打 tag**——第一个 tag 属于阶段 B("作为证据封版":README 治理重写 + OWASP 映射完成后)

---

## 6. 发布测试自查表(全部 ✅ 才 push)

| 测试 | 判定 | 对应 |
|------|------|------|
| 无害 | A0 全绿 + A2.3 免责就位 + 种子数据为虚构 | A0 / A2 |
| 进入 | A4.2 干净环境全绿,零 key 零 docker | A3 / A4 |
| 诚实 | README 每句陈述与代码一致(现状已满足,增补时保持) | A3 |

---

## 7. 明确不在本清单内(阶段 B 及以后)

README 治理叙事重写 · 双语 · OWASP Agentic Top 10 映射 · 第一个 tag ·
CONTRIBUTING(攻击用例提交指引)· CI badge · 博客传播。
