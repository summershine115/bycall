# ADR-0002：抽象文档解析引擎（可切换）

- **状态**：已采纳
- **日期**：2026-07-27
- **背景**：知识库文档入库的解析能力可切换
- **关联**：ADR-0001（同源的可切换后端模式）、`architecture.md` §后端可切换设计

## 背景

M2 的文档入库已有一套**按文件扩展名路由**的解析器（`rag/parsers/registry.py`）：
每类型一个实现（PDF→MuPDF、docx→python-docx、HTML→bs4、Markdown/Text）。
这是「一类格式一个解析器」的细粒度模型。

但两类新需求打破了这个模型——它们是**通用解析器**，一个引擎覆盖多种类型：

- **MarkItDown**（Microsoft）：一个库吃下 pdf/docx/pptx/xlsx/html/图片/音频/zip。
- **MinerU**（opendatalab）：远程布局识别 + OCR，专治扫描件 / 复杂版式 PDF，输出结构化 Markdown。支持自建部署（`/tasks`）与 mineru.net 云端（`/api/v4`）两套协议。

强行把它们塞进「按扩展名路由」的注册表（让 .pdf 同时映射多个解析器）会丢掉
「整份文档走同一引擎」的语义，也无法表达「全局选某个引擎」的意图。

## 决策

在按扩展名路由的解析器**之上**引入「解析引擎」策略层（`rag/parsers/engines.py`），
参考 `VectorStore` 的可切换后端设计：

```
ParseEngine（策略）
├── BuiltinEngine    —— 委托 registry 的按扩展名解析器（默认，零外部依赖）
├── MarkItDownEngine —— MarkItDown 本地库（可选依赖）
└── MinerUEngine     —— MinerU 远程 API（selfhost `/tasks` 或 cloud `/api/v4`，按 mineru_mode 分派）
```

引擎接口：`name / description / supported_types / is_available / parse(content, file_name) -> ParseResult`。
引擎内部复用现有解析器产物（MarkItDown/MinerU 输出 Markdown，复用 `MarkdownParser`
抽 heading 结构，供 chunking 层 heading-aware 切分）。

### 路由规则（统一入口 `parse_document`）

1. 选引擎：显式 `engine` 参数 > 配置默认 `BYCALL_PARSE_ENGINE`。
2. 引擎不可用（依赖未装 / 远程未配置）且**被显式选中** → 抛错（暴露配置问题，不静默回退）。
3. 引擎**不支持该类型** → 自动退回 `builtin`（小众格式如 .py/.sql 不强制走 markitdown）。
4. 引擎解析抛错 → 上抛（真实解析错误）。**上传路径**把解析放在后台线程，这类错误落
   `status=failed` 经 `/progress` 暴露；**预览/同步路径**仍由调用方即时处理。

这套规则保证**零回归**：默认 `builtin` 行为与引入引擎层之前完全一致；选了非默认引擎
也不影响其不覆盖的格式。

## 后果

**优点**：
- 解析能力可按部署形态切换（本地无 GPU → builtin/markitdown；有 MinerU 服务 → mineru 做高质量 OCR）。
- 上传端点 `engine` 参数支持按文档覆盖（同一系统里扫描件走 mineru、普通 docx 走 builtin）。
- 引擎对调用方透明：业务/入库管道只调 `parse_document`，新增引擎只实现接口 + factory 注册。
- 非默认引擎是可选依赖 / 远程服务，不影响 builtin 路径的零依赖启动。

**代价**：
- 多一层抽象（引擎层 + 既有解析器注册表两层），需在文档/注释里讲清二者关系（本 ADR 即此）。
- MinerU 是远程异步任务，解析延迟从「秒级」变「分钟级」。为此**上传解析本身也异步化**
  （参考 WeKnora）：上传只做廉价同步校验（扩展名 / 引擎可用性 / 大小），解析+入库都在后台
  线程池并行，多文件互不阻塞；内容级错误（编码/格式/空）不再立即 4xx，而是后台落 failed。
  仅「引擎不可用」这类配置错误在请求线程即时 4xx（廉价预检）。

## 约束

- 非默认引擎的依赖（markitdown）/ 凭证（mineru token）走**可选依赖 + 配置**，
  `is_available()` 必须如实反映就绪状态，供 `/api/admin/parse-engines` 展示与路由兜底。
- 引擎输出统一收敛到 `ParseResult`（text + sections + metadata），
  不把引擎特有的中间结构（如 MinerU 的 middle_json）泄漏到下游 chunking/embedding 层。
- MinerU 客户端（`mineru_client.py`）的 HTTP 层可注入 fake client，保证不连真实服务即可测试。
- MinerU 双协议（`mineru_mode`）：selfhost 走 `/tasks` multipart；cloud 走 mineru.net
  `/api/v4` 预签名上传（**PUT 不带 Authorization / Content-Type**，OSS 签名要求）→ 轮询
  batch → 取 `full_zip_url`。cloud 单文件 `/api/v4/extract/task` 只收 URL、不支持直传，
  故本地字节流固定走 batch 流。状态字段两套：selfhost `status`∈{completed,failed}、
  cloud `state`∈{done,failed}；cloud 响应统一 `{code,msg,data}` 信封，`code!=0` 即失败。

## 相关

- 引擎抽象：`src/bycall/rag/parsers/engines.py`
- MinerU 客户端：`src/bycall/rag/parsers/mineru_client.py`
- 配置：`src/bycall/config.py` `ParseConfig`（前缀 `BYCALL_PARSE_`）
- 列表端点：`GET /api/admin/parse-engines`
