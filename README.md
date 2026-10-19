# docAgent · 周报生成 Agent

Python 3.11+ 周报整理工具：CSV/XLSX → 规则与字段映射 → IR → 可选 LLM → 事实校验与回退 → DOCX。编号、术语规范化和文档格式由确定性规则处理，LLM 只整理允许改写的内容。

## 功能

- 所有周报均先适配并保存方案，再在主页选择方案生成。已取消“研发三部周报”内置规则及隐式默认；`general` 仅为内部适配基线，不是生成入口。
- 工作表/周次选择、字段映射、来源追溯；按状态列及历史周记录标记已完成事项。
- 原文、润色、精简、详细整理、自定义五种模式；原文模式无需 API Key，不调用外部模型。
- 三栏审核、模型候选与原文 Diff、人工编辑确认、单块重生成、独立缩放与专注模式。
- DOCX 格式验收、可选 PDF、匿名使用统计；独立任务 Worker 与增量进度显示。
- M6 方案适配：分析陌生表头、确认字段与状态保留策略、配置段落格式、试生成后长期保存并复用。

方案库长期保留；审核会话默认保留两小时。适配支持记录表、两行周次矩阵和段落型 DOCX，复杂版式会拒绝保存；不承诺任意 Excel/DOCX 自动转换。

## 安装与配置

先检查 `conda env list`。不存在 `py31115` 时创建；若同名环境用于其他项目，使用独立环境，不覆盖它。

```bash
conda create -n py31115 python=3.11 pip -y  # 仅环境不存在时执行
conda activate py31115
cd ~/docAgent                           # 改为实际项目路径
python -m pip install -r requirements.txt

test -f .env || cp .env.example .env
chmod 600 .env
```

安装方式为 editable；源码在 `src/`，逻辑包名仍是 `weekly_report_agent`，不要直接执行 `python src/web.py`。运行/开发依赖以 `pyproject.toml` 和 `requirements.txt` 为准。

编辑 `.env`，模型模式需要 `LLM_API_KEY`；Base URL、模型允许列表和其他选项参考 `.env.example`。密钥只在服务端读取，不写入 Git。部署方需确认内容外发授权；CLI 模型模式要求 `--allow-external`，Web 单块重生成保留外发确认。

通用 `LLM_*` 配置优先，未设置时兼容旧 `DEEPSEEK_*`，显式空值不会回退旧值。`LLM_REASONING_EFFORT` 默认留空、不发送；仅在接口/模型支持时设置对应值，代码不保证平台接受该参数。修改配置后重启 Web 和任务 Worker。

## 部署：单终端启动

先停止原来单独启动的 Gunicorn 和任务 Worker，再在一个终端执行：

```bash
conda activate py31115
cd ~/docAgent
python -m weekly_report_agent.server --host 127.0.0.1 --port 18988
```

此入口同时管理 **Web 服务和独立任务 Worker**，并不是把任务放回 Web 线程。默认 Web 为 2 个 gthread 进程、每进程最多 2 个请求线程；后台任务并发仍由 `TASK_MAX_CONCURRENCY` 控制。两端使用相同 Python、项目目录及配置，日志直接显示在当前终端。

- 默认读取项目 `.env`，不需要另行 `source`。支持普通 `KEY=value`、引号、注释和 `export` 前缀，不执行 shell 命令或展开变量。
- 不指定 `--host` 时监听 `127.0.0.1`，用于本机或 SSH 转发；`0.0.0.0` 用于其他机器直接访问服务器 IP。
- Ctrl+C / SIGTERM 同时停止两服务，等待当前任务结束，最长等待约 190 秒；再次 Ctrl+C 可强制停止，中断任务按租约规则失败。
- 端口已占用时拒绝启动，不会停止已有服务；一方意外退出时停止另一方并返回失败，避免只剩 Web 导致一直排队。
- 更新代码或配置后重启整个统一入口。关闭终端可能中断服务，长期部署可将统一入口交给已有进程管理器，本项目不自动配置开机自启。

其他启动选项：

```bash
python -m weekly_report_agent.server --env-file /绝对路径/配置.env
python -m weekly_report_agent.server --no-env  # 仅使用终端已有环境变量
python -m weekly_report_agent.server --workers 2 --threads 2 --port 18989
```

重新安装项目后也可使用 `weekly-report-server`。若配置需要 shell 变量展开，先在当前终端加载配置，再用 `--no-env`。

### 分开管理（可选）

已有进程管理器可继续分别管理两服务，各自加载同一份环境变量：

```bash
# Web
gunicorn --config gunicorn.conf.py --workers 2 --worker-class gthread --threads 2 \
  --bind 127.0.0.1:18989 'weekly_report_agent.web:create_app()'
# 独立任务 Worker（另一个终端或进程管理器单独运行）
python -m weekly_report_agent.task_worker
```

单独启动 Gunicorn 不会执行排队任务，原文模式也需要任务 Worker。CLI 不依赖任务 Worker。

### 常用配置

| 变量 | 默认值 / 用途 |
|---|---|
| `LLM_API_KEY`、`LLM_API_BASE`、`LLM_MODEL`、`LLM_ALLOWED_MODELS` | 模型凭据、接口、默认模型及允许列表 |
| `LLM_TIMEOUT`、`LLM_MAX_RETRIES` | 请求超时 60 秒、最多重试 1 次 |
| `LLM_MAX_CONCURRENCY`、`LLM_AUTO_CORRECT` | 单任务最多 2 批并发；默认不自动定向修正 |
| `TASK_MAX_CONCURRENCY`、`TASK_QUEUE_CAPACITY` | 同配置后台 Worker 共享任务并发 2、接纳容量 100 |
| `TASK_DB_PATH`、`TASK_FILES_DIR` | 默认项目 `var/tasks.sqlite3`、`var/tasks/` |
| `WEEKLY_REPORT_SNAPSHOT_DIR` | 默认系统临时目录下 `weekly-report-agent-snapshots/` |
| `STATS_DB_PATH` | 默认项目 `var/stats.sqlite3` |
| `REPORT_PROFILES_DIR` | 默认项目 `var/profiles/`；元数据库位于同级 `profiles.sqlite3`，不自动过期 |
| `LIBREOFFICE_PATH`、`PDF_PREVIEW_TIMEOUT` | 可选 PDF 转换器与超时 45 秒 |

Web 与后台 Worker 必须连接同一任务数据库，使用同一任务文件和快照目录；路径配置建议使用实际绝对路径。详细排查、数据管理及网络访问见 [运维说明](docs/operations.md)。

## 使用

网页上传文件 → 选择规则、日期和整理模式 → 预览审核 → 下载 DOCX。示例下载在“使用帮助”页面，首次体验示例先进入“方案适配”，分析、试生成并保存后回主页选用。

主页只选择已保存方案；字段映射、自定义模板和格式调整统一在“方案适配”中确认、试生成并保存。

“当前内容”和 HTML 正文显示用于导出的规范化、标点与编号结果。模型候选未通过校验时默认回退原文，核对后可谨慎采用；人工编辑以当前采用内容为基准，通过独立修改 Diff 确认后生效。新建周报等待完整模型响应后校验，再进入审核；生成期间仅显示进度，不逐片展示正文。

提交返回任务 ID，浏览器短轮询获取进度；同页刷新可恢复任务。关闭页面不自动取消后台任务，主动取消是协作式的，发布阶段不接受取消。任务队列隔离 Web 容量，不会直接缩短模型本身的计算时间。

CLI 不依赖后台 Worker，默认原文模式，必须使用已保存方案（模板随方案读取）：

```bash
python -m weekly_report_agent \
  --input data/周报-v1.csv \
  --profile p_实际方案ID \
  --output outputs/周报.docx
```

模型模式增加 `--content-mode polish --model <允许的模型> --allow-external`；自定义模式使用 `--content-mode custom --custom-instruction '整理要求'`。其他参数用 `python -m weekly_report_agent --help` 查看。

## M6：适配并复用报告方案

导航进入 **方案适配**（`/profiles`）：

1. 上传已脱敏 CSV/XLSX 样例与参考 DOCX。工作表自动读取为下拉选项；可填写希望的报告目录与补充要求。
2. 确认允许模型读取样例后，后台自动识别内容含义、字段用途、报告目录和段落格式，直接填入待核对方案。
3. 检查“数据如何组成报告”：模型自动提出标题组织，人工核对；字段含义中的“主要正文”可切换到其他列，并可勾选多列补充内容。同一正文所属标题下的行统一合并为一个审核区块。可添加任意已映射字段的筛选条件，多选值或删除条件。仅识别到状态字段时显示保留选项，未知状态保留并提醒。
4. 检查报告标题、起始章节、标题/正文格式与六级编号样式；点击“验证样例”，下载核对 DOCX。配置改变后须重新验证样例，再明确确认保存。
5. 首页直接选择已保存方案，后续只上传新表格。更新产生新版本；队列及审核会话冻结实际规则和模板，已有报告不受后续更新影响。

自动识别需要已配置的 LLM API，经明确确认后才发送截取的表格行/列值、参考文档片段、解析样式和补充要求；不会直接上传原始文件包。识别走独立 Worker，最多两轮提出/修正完整方案，并在本地检查字段、目录覆盖和离线 DOCX。失败明确提示，可人工修正后试生成；不会自动发布方案。试生成采用原文，不再次调用模型。首次自动识别与后续报告整理是独立步骤。

周次矩阵中，即使首行表头为空（例如“未命名列7”），也能根据第二行识别正文候选；主要正文用途可修改，不锁定列。若主要正文映射到周进展列，生成时仍按所选周读取；映射到普通列则使用该列。其余列可以作为补充正文添加或移除。

“字段含义”只是数据角色映射；“写入此目录正文的字段”才决定正文来源。勾选的字段在模型模式下作为可整理内容；未勾选的项目、事项、状态、日期等仍可能作为只读上下文传给模型，不代表禁止外发。正文勾选项显示映射后的语义名称；筛选条件显示原 Excel 列名，规则仍通过字段映射关联，可从任意已映射字段中添加，支持复选框多选；选择“不使用”的字段不显示，其已有筛选条件移除，同一字段内为 OR，不同字段间为 AND；“不限制”包含以后新增的值，选择具体值只包含所选值。

固定目录适合每周沿用“百擎平台 / LDM”等指定标题；动态目录适合从新表格的项目、事项列产生标题，也支持固定父标题与动态子标题混合。动态标题不会把本周项目名单固化到方案，新项目自动进入对应层级；缺少标题值会明确报错。不同最终标题的来源行不会合并，历史只读事项与可编辑事项也保持分开。

目录支持 1～3 级语义标题，例如把不同来源项目归入“LDM / 模型研发”，按给定目录顺序生成。提供明确编号的期望目录时，检查其层级和相对顺序；未被目录覆盖的有效来源行会报错，不能静默丢弃。多个字段合并后仍保留原始行、字段及事实约束。字段表不再逐列设置章节名或 LLM 开关。

支持记录表、两行周次矩阵和段落型 DOCX。编号可以在受支持的数字、括号和带圈样式中选择，不允许任意格式代码。表格、文本框、正文图片、公式、多节等复杂版式不会静默转成段落；HTML 与 DOCX 使用同一目录和正文编号，精确版式以 DOCX/可选 PDF 为准。方案由服务器共享，不提供账户隔离。

CLI 可复用已保存方案（不需要再次传模板）：

```bash
python -m weekly_report_agent --profile p_实际方案ID --input 新周报.xlsx --output outputs/周报.docx
```

管理员查询最近**实际提交时间**、成功时间和使用次数，按需确认删除：

```bash
python scripts/manage_profiles.py       # 循环菜单：查询、搜索、筛选、详情、删除、重命名、退出

# 也保留一次性子命令，便于脚本调用：
python scripts/manage_profiles.py list
python scripts/manage_profiles.py list --unused-days 90  # 只筛选，不自动删除
python scripts/manage_profiles.py show p_实际方案ID
python scripts/manage_profiles.py rename p_实际方案ID "新的方案名称"
python scripts/manage_profiles.py delete p_实际方案ID    # 输入完整 ID 确认，删除全部版本
```

交互菜单选 `6` 重命名，可按列表序号或完整 ID 选择方案，回车取消。新名称为 1～100 字，重命名生成新版本，保留 ID、历史配置、模板与使用统计；已提交任务及审核快照不变。

交互菜单支持按列表序号或完整 ID 选择方案；删除仍需输入完整 ID 确认。操作完成、取消或失败后均返回菜单，输入 `0`/`q`、Ctrl+C 或 Ctrl+D 退出。

管理脚本默认安全读取项目 `.env`；支持 `--env-file 路径` 或 `--no-env`（放在子命令前）。排队、运行、发布中或尚未释放执行权的任务引用方案时拒绝删除。内置规则不能由此脚本删除；已生成的独立审核快照保留至其正常过期。

## 开发与验证

```bash
python -m pytest
ruff check .
ruff format --check .
```

当前回归基线：**197 passed**，Ruff lint/format 通过。测试使用 Mock；真实 API 测速只允许使用获准的合成数据。默认离线测速：

```bash
python scripts/benchmark_timing.py
```

## 文件与文档

- `src/`：应用代码、规则、页面、合成示例；`tests/`：按功能组织的当前回归测试。
- `data/`：原 CSV 与 DOCX 参考夹具，保持不变；`scripts/`：合成测速与方案管理脚本。
- `var/`：私有运行数据；`outputs/`：按需创建的报告输出，二者不提交 Git。
- [项目架构](docs/project-overview.md)：模块、Harness、审核与队列实现。
- [运维说明](docs/operations.md)：排队排查、运行数据、进程与网络访问。
- [ROADMAP](ROADMAP.md)：当前进度、边界和后续计划。
