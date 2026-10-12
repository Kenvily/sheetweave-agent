# 周报生成 Agent

这是一个 Python 3.11+ 的周报生成工具。它支持读取 CSV/XLSX、选择规则预设和工作表、映射字段、预览来源记录及内容块，并基于 DOCX 模板生成和校验周报。排版与编号始终由确定性规则完成；内容可保留原文，也可通过 DeepSeek 进行润色、精简、详细整理或按自定义要求整理。

## 当前功能

- `rd3`（研发三部周报）：兼容原有两行表头周报，自动选择指定周次或最新有效周次，一级章节默认从 `9` 开始。
- 对包含周次列的周报，可按“状态”列识别历史已完成事项：若状态为完成类值且所选周次之前已有记录，本次正文固定标记为“已完成”，不再用 LLM 整理旧内容。
- `general`（通用周报）：读取单行表头 CSV/XLSX，按“本周工作 / 问题与风险 / 下周计划”组织内容，一级章节默认从 `1` 开始。
- 支持 XLSX 工作表选择、常见列名自动识别和手动字段映射。
- 保留 CSV 物理行号或 XLSX Sheet/行号，为来源记录和内容块生成稳定 ID。
- Web 提供三栏审核工作区：左栏完整原始行、中栏内容版本与审核操作、右栏当前有效内容预览。
- 来源行与内容块支持双向定位和高亮，兼容一行关联多块、多行合并一块。
- 中栏主卡展示当前生效内容，独立候选区完整展示模型结果与行内差异；每个内容块最多提供一个以原文为基准的 Git 风格单栏 Unified Diff；上下文行保持原样，仅用 `-` 红色删除行和 `+` 绿色新增行突出变化，并支持版本恢复、单块重新生成和事实告警确认。人工编辑框始终显示当前区块已采用、规范化后的最新结果；未采用的模型候选不会替换编辑基准。
- 支持原文保留、规范润色、精简总结、详细整理和自定义要求五种内容模式。
- LLM 仅能改写所选周次的工作内容；项目、任务、结构状态和日期字段只作为只读上下文。
- 对模型版本、指标、百分比、百分点、数量、日期和任务状态执行归一化分级校验。
- DeepSeek API Key 仅从服务端环境读取，不在页面显示、保存或回传。
- CLI 和 Web 均保留原有研发三部生成方式。

原文保留模式不需要 API Key，也不会发起模型网络请求。其他模式只发送可编辑工作内容、必要的只读结构上下文和归一化事实，不发送完整上传文件；使用前应由部署方确认允许外发；CLI 要求 `--allow-external`，Web 首次生成由服务端部署策略控制，单块重生成仍要求勾选外发确认。Web 预览会建立一个有效期为 2 小时的临时审核会话，可直接下载，无需重新选择文件；刷新页面后人工编辑仍保留。保存编辑、切换有效版本或单块重新生成时会生成新 revision，并同步更新 HTML 预览和 DOCX。模型模式不会在刷新、切换版本或下载时再次调用模型。过期会话在应用请求中触发清理，不提供历史报告检索。

四种模型模式采用不同的写作契约，而不只是切换名称：规范润色以原文 85%～115% 的长度等长改写并保持事项顺序；精简总结以 50%～75% 为目标压缩重复内容；详细整理以 120%～160% 为目标补全句子和事实间衔接；自定义要求按用户输入控制表达。上述长度均为软目标，数字、指标、模型版本、日期和状态等受保护事实必须优先完整保留，因此事实密度很高的短文本可能无法达到目标比例。

## 当前开发阶段

M0～M3 与 M5 的当前实现已通过回归，M4 第一阶段已完成。M4 已增强 DOCX 包安全、页面设置验收和按 revision 生成的可选 PDF 版式预览；多模板目前保留为高级适配设置中的简单上传与字段映射。M5 已提供匿名聚合统计、网页帮助、通用 CSV/XLSX 示例和 Gunicorn + WSL2 共享部署指引。M6 的未知结构辅助适配尚未实现，属于后续受控探索。最近一次本地回归为 **137 passed**，Ruff 检查与格式检查通过；测试结果对应当前工作区，不代表这些修改已经提交或部署到正在运行的服务。

## 最近更新（2026-10-12）

- 源码直接位于 `src/`，保留逻辑包名与启动入口；报告输出统一为 `outputs/`，统计持久保存在 `var/`。
- 当前内容和 HTML 正文复用 DOCX 正文规划，原始来源独立保留。
- 人工编辑基于当前有效结果，独立 Diff 弹窗确认后直接生效；模型候选独立展示行内差异，告警摘要与详情分开展示。
- 修复“KS 是 / KS 为”和“1万 / 1 万”的事实识别误报，真实数值变更继续提示冲突。旧会话保存的校验结果不会自动重算。
- 项目根目录 `gunicorn.conf.py` 默认配置线程 Worker，避免同步 Worker 的默认超时中断模型等待；部署配置与应用源码分开管理。

## 规则与格式

- 标题编号依次为 `1.`、`1.1.`、`1.1.1.`，正文编号依次为 `1、`、`（1）`、`①`。
- 一级标题：微软雅黑、加粗、16 磅，段前段后 6 磅。
- 二级标题：微软雅黑、加粗、15 磅，段前段后 6 磅。
- 三级标题：微软雅黑、加粗、14 磅，段前段后 6 磅。
- 正文：宋体、10.5 磅，首行缩进 2 字符。
- 全篇使用 1.5 倍行距，正文换行补齐标点。
- `周一`、`本周二`、`下周三` 等相对星期在有报告日期时转换为具体日期。
- `XX哥`、`XX姐` 等非正式称呼替换为“相关负责人”。
- 规则只调整结构和规范用语，不改写日期、模型版本、数字指标或业务结论。

规则预设位于 `src/rules/`。JSON 分别定义输入映射、内容章节和文档参数，加载时会校验字段引用、章节、编号范围和模板相对路径。DOCX 仍由标准库 OOXML 引擎生成，不使用 `python-docx`。

术语规范化在内容选定后、生成 ReportPlan 时执行，原文模式也会规范专业术语、称呼和相对星期；不会覆盖 IR 中保存的原文、模型候选或人工编辑。内容审核的当前内容和右栏 HTML 正文复用 DOCX 的规范化、标点与编号规划，左栏保留原始来源。人工编辑框显示当前规范化结果；提交时单独弹出本次修改前后 Diff，确认后直接生效。术语替换和事实识别别名目前分别定义在 `normalizer.py`、`llm/facts.py`，没有独立统一词表。

历史完成标记只读取“状态”列，不解析周报正文中的“完成”字样。对包含多周列的规则，如果状态为完成、已完成、结项或关闭等配置值，并且所选周次之前至少有一周记录，该事项仍保留在本次报告中，但正文统一写为“已完成”并跳过 LLM。首周完成或此前没有周记录时保留本周实际内容。该能力属于通用 `weekly_matrix` 规则配置，不依赖研发三部预设；单周记录格式没有历史周列，无法据此判断。

## 项目结构

```text
docAgent/
├── src/                 # Python 模块、LLM、规则、页面与合成示例
├── tests/               # 单元、Web、Mock LLM 和端到端回归
├── data/                # 原始 CSV 与 DOCX 参考夹具，保持不变
├── docs/                # 项目介绍与 WSL 网络维护说明
├── var/                 # 统计数据库与密钥，不提交 Git
├── outputs/              # 运行时创建的报告输出目录，不提交 Git
├── gunicorn.conf.py     # 线程 Worker 与模型等待超时配置
├── pyproject.toml       # 包映射、依赖、命令入口和质量工具配置
├── requirements.txt     # editable 安装及固定开发工具版本
├── .env.example         # 可提交的配置示例
├── AGENTS.md            # 仓库协作约定
├── README.md            # 安装、使用、部署与验证
└── ROADMAP.md           # 设计边界、里程碑与后续计划
```

源码直接位于 `src/`。setuptools 将 Python 包名 `weekly_report_agent` 映射到该目录，因此 `python -m weekly_report_agent`、`python -m weekly_report_agent.web` 和已有导入保持兼容；不要直接执行 `python src/web.py`。包安装生成的 `weekly_report_agent.egg-info/` 位于项目根目录，由 Git 忽略，不是源码。

模块职责、逐项文件说明和实现原理见 [项目介绍](docs/project-overview.md)。`data/周报.csv` 与 `data/研发三部周报模板.docx` 是不可修改的参考夹具。`outputs/` 按需创建，统计数据库与密钥保存在 `var/`；构建目录、安装元数据及缓存均可重新生成。

## 安装

检查 `conda env list`；若不存在 `py31115`，先执行 `conda create -n py31115 python=3.11 pip -y`。若同名环境用于其他项目，应使用独立环境。随后在项目根目录执行（源码布局变更后必须重新安装，避免环境仍引用旧路径）：

```bash
conda activate py31115
python -m pip install -r requirements.txt
```

`requirements.txt` 包含 `-e .`，以 editable 方式安装项目，并安装 pytest `8.3.5`、Ruff `0.11.0`。项目运行依赖固定为 Flask `3.1.3`、Gunicorn `26.2.0` 和 openpyxl `3.1.5`；openpyxl 仅用于读取 XLSX。LLM 客户端使用标准库 `urllib`，无需额外 SDK。

### DeepSeek 配置

只有非原文模式需要配置。不要把真实 Key 写入 `.env.example`：该文件是可提交到 Git 的配置模板。若尚无 `.env`，在项目根目录复制配置示例；已有配置请保留：

```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

编辑 `.env`，设置 `DEEPSEEK_API_KEY`：

```dotenv
DEEPSEEK_API_KEY=sk-请替换为你的真实Key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_MODELS=deepseek-chat,deepseek-reasoner,deepseek-flash
LLM_TIMEOUT=60
LLM_MAX_RETRIES=1
LLM_MAX_BATCH_CHARS=12000
LLM_MAX_CONCURRENCY=2
LLM_AUTO_CORRECT=false
```

`.env.example` 当前选择 `deepseek-flash`；未加载环境变量时代码默认仍为 `deepseek-chat`。模型必须在服务端允许列表中，并由部署方核实服务可用性。`LLM_MAX_CONCURRENCY` 限制单次生成的批次并发（1～4），不是全站并发限制；`LLM_AUTO_CORRECT=true` 才开启一轮定向修正。短报告只有一个批次时不会因并发而提速，已有合成数据单次测速，不能代表真实周报的稳定性能。

`.env` 已被 `.gitignore` 排除，不会被正常提交。启动 Web 或运行 CLI 前，在同一个终端加载配置：

```bash
set -a
source .env
set +a

python -m weekly_report_agent.web --host 0.0.0.0 --port 18989
```

可用下面的命令确认 Key 已加载；该命令只显示是否存在，不打印 Key 内容：

```bash
python -c 'import os; print("DeepSeek Key 已加载" if os.getenv("DEEPSEEK_API_KEY") else "DeepSeek Key 未加载")'
```

更新代码或服务环境变量后需要重启 Web/Gunicorn；刷新浏览器不会重新加载服务端代码。项目不会自动读取 `.env`，每次新开终端后都需要先执行 `source .env`，或者把加载命令配置到仅自己可访问的启动脚本或进程管理器中。不要把 `.env`、真实 Key 或包含 Key 的终端输出提交到 Git。

## Web 页面

```bash
python -m weekly_report_agent.web --host 127.0.0.1 --port 18989
```

在启动服务的机器上访问 <http://127.0.0.1:18989>；远程 Linux 服务器可通过 SSH 端口转发访问，浏览器中的 localhost 指浏览器所在机器。直接打开 `/preview` 或 `/generate` 会跳转首页，实际预览由上传表单的 POST 请求创建。Web 默认端口为 8000，本例显式指定 18989。首页“使用帮助”提供四步操作说明，并可下载不含真实业务数据的通用 CSV/XLSX 示例。需要监听 WSL 所有网卡时使用：

```bash
python -m weekly_report_agent.web --host 0.0.0.0 --port 18989
```

页面操作：

1. 上传 CSV/XLSX，并选择 `研发三部周报` 或 `通用周报`。
2. 选择文件后，页面自动读取可选工作表和周次；起始章节号留空时使用规则默认值，也可直接输入。
3. 点击“预览来源与内容”进入三栏工作区；点击左侧来源行或中间内容块可双向定位。
4. 在中栏审核当前生效内容；发生模型改写或人工编辑时，可展开唯一的 Git 风格单栏 Unified Diff。上下文行保持原样，删除行以红色 `-` 标记，新增行以绿色 `+` 标记；也可保存编辑、恢复原文或只重新生成当前块。
5. 从右栏下载当前 revision 的 DOCX；若服务器安装了 LibreOffice，可点击右上角 PDF 页签生成同一 revision 的 PDF 版式预览。会话保留 2 小时，刷新页面无需重新选择文件。

模型生成结果始终有独立展示区，无论是否采用都会完整显示。候选与原文用行内 Diff 对比，红色删除线表示删除，绿色高亮表示新增；也可展开完整模型文本。告警默认只显示最多三条简短摘要，完整原因和原文/生成片段折叠在“校验详情”中。候选展示不改变当前内容、编辑基准或导出。只有模型候选执行事实校验，有告警时显示“谨慎使用”，核对并勾选确认后可采用。人工编辑以当前有效结果为基准，点击“确认修改”先展示独立的修改前后 Diff；点击“确认并保存”后直接生效，不执行模型事实校验，也不需要再次“采用人工编辑”。取消或关闭确认框不会保存。页面“查看变更对比”始终比较来源原文与当前有效内容，独立于本次编辑确认。若多个标签页同时修改，旧 revision 会收到 `409` 提示，不会静默覆盖新内容。单块重生成不会修改其他块；已有人工编辑时必须明确勾选覆盖。

“高级适配设置”当前提供自定义 DOCX 模板上传和字段映射。上传模板会先执行受限 ZIP/DOCX 检查；现阶段仍由已有 OOXML 渲染器替换正文，不承诺兼容任意复杂模板布局。

PDF 预览是可选能力，不影响 DOCX 生成和下载。安装 LibreOffice 后通常可自动识别 `soffice`；也可在 `.env` 中配置：

```dotenv
LIBREOFFICE_PATH=/usr/bin/soffice
PDF_PREVIEW_TIMEOUT=45
```

点击右上角 PDF 页签即可生成，页签内显示生成中状态；生成 PDF 后可通过 HTML/PDF 页签随时切换，两种预览基于同一 revision。未安装 LibreOffice 时页面继续显示 HTML 快速预览。人工编辑、采用版本或单块重生成后，旧 PDF 会自动失效，需针对新的 revision 手动重新生成。

选择非原文模式时还需选择服务器允许的模型。页面不会显示或保存 API Key；是否允许通过外部模型处理周报数据，应由部署方按公司数据管理要求统一控制。上传原文件在请求完成后删除；预览生成的 DOCX 快照在本机受限临时目录保留 2 小时并自动清理；请求总大小限制为 16 MB。

首页显示累计成功生成、本周成功生成、独立访客（估算）和访问次数。统计仅保存按日聚合计数、匿名浏览器标识的 HMAC 摘要和短期任务摘要，不保存姓名、IP、周报正文或历史文档。一次预览生成或直接生成成功计一次；刷新工作区、生成 PDF 和重复下载不重复计数。清除 Cookie 或更换浏览器会被视为新的估算访客。统计使用持久化 SQLite，默认位于项目内 `var/stats.sqlite3`（不受启动工作目录影响），也可通过 `STATS_DB_PATH` 固定路径；重启不清零。首次使用新默认路径时，会复制旧用户目录数据库和匿名标识密钥，旧文件保留作备份；显式配置 `STATS_DB_PATH` 时沿用配置。访问次数统计首页、帮助页和工作区成功的 GET 请求，包含刷新；文件下载和静态资源不计入。统计故障不会阻断 DOCX，并显示不可用提示。

统计配置示例（空路径使用项目默认值）：

```dotenv
STATS_DB_PATH=
REPORT_TIMEZONE=Asia/Shanghai
STATS_HMAC_KEY=
```

密钥为空时会生成并持久保存 `var/stats.sqlite3.key`；也可设置至少 16 字节的稳定 `STATS_HMAC_KEY`。多 Worker 使用同一数据库和密钥，变更密钥会影响访客去重。数据库与密钥应一起备份；运行时可能存在 WAL/SHM 文件，不手动删除。`var/` 各文件含义见[项目介绍](docs/project-overview.md#var-文件说明)。仅在新默认数据库不存在时迁移旧默认路径；旧文件保留，不合并后续写入。迁移时应停止旧服务写入，再启动使用新路径的服务；显式配置旧路径会继续覆盖默认值。新增访问次数无法从旧数据库补算。

多人共享建议使用 Gunicorn，见下方[共享部署](#共享部署)。

## 共享部署

在项目根目录运行，确保当前 Conda 环境已安装本项目：

```bash
conda activate py31115
# 仅需要模型模式时加载本地配置：
set -a
source .env
set +a

gunicorn --workers 2 --bind 127.0.0.1:18989 \
  --access-logfile - --error-logfile - \
  'weekly_report_agent.web:create_app()'
```

应用使用 `create_app()` 工厂，不提供全局 `app` 实例。在项目根目录启动时自动加载 `gunicorn.conf.py`：`gthread` Worker、每 Worker 两个线程、`timeout=180`、`graceful_timeout=180`。线程 Worker 在 LLM/PDF 等待期间持续发送心跳，避免默认同步 Worker 的 30 秒超时先于 `LLM_TIMEOUT=60` 终止请求。从其他目录启动时显式指定 `--config /绝对路径/docAgent/gunicorn.conf.py`。命令行参数仍可覆盖配置。多 Worker 默认共享项目 `var/stats.sqlite3` 和临时会话目录，也可在 `.env` 显式配置：

```dotenv
WEEKLY_REPORT_SNAPSHOT_DIR=/绝对路径/docAgent/var/snapshots
STATS_DB_PATH=/绝对路径/docAgent/var/stats.sqlite3
REPORT_TIMEZONE=Asia/Shanghai
STATS_HMAC_KEY=
```

所有 Worker 使用同一 Linux 用户、同一数据库和持久密钥；目录需有写入权限。空 HMAC 配置会使用数据库旁持久生成的密钥，备份时与数据库一起保存。更新代码、路径或环境变量后重启服务。默认模板和 CLI 数据路径依赖项目工作目录；从其他目录启动时通过参数或 `WEEKLY_REPORT_TEMPLATE` 提供绝对模板路径。

Linux 可直接运行上述命令；WSL2 会受 Windows 休眠和关闭影响。局域网访问与转发维护见 [WSL 网络说明](docs/wsl-lan-access.md)。

## 命令行

使用原研发三部规则、默认路径、最新有效周次和章节号 `9`：

```bash
python -m weekly_report_agent
```

指定研发三部参数：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output outputs/研发三部周报.docx \
  --preset rd3 \
  --week "第1周" \
  --report-date 2026-10-09 \
  --section-number 9
```

通用 XLSX 示例（省略 `--section-number` 时从 `1` 开始）：

```bash
python -m weekly_report_agent \
  --input /path/to/通用周报.xlsx \
  --sheet "周报数据" \
  --preset general \
  --template data/研发三部周报模板.docx \
  --output outputs/通用周报.docx \
  --report-date 2026-10-09
```

通用规则至少需要可映射到“项目”和“本周工作”的列；可选列为“问题与风险”和“下周计划”。CLI 当前使用预设中的列别名自动映射，复杂手动映射请使用 Web 页面。

原文模式完全离线，是 CLI 默认行为。使用 DeepSeek 润色时必须显式确认允许外发：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output outputs/研发三部周报-润色.docx \
  --content-mode polish \
  --model deepseek-chat \
  --allow-external
```

其他模式的 `--content-mode` 可设为 `concise`、`expand` 或 `custom`；自定义模式还需传入 `--custom-instruction`。模型返回结果会按稳定内容块 ID 对齐。校验分为“通过、待人工确认、谨慎使用”：通过的候选进入 DOCX；待确认或有事实告警的候选保留在 Web 预览中并展示原文、生成内容和原因，默认 DOCX 回退原文。编辑框跟随当前已采用内容；未采用的候选只有显式采用后才进入编辑基准和 DOCX。Web 可在核对告警后显式采用候选。为减少等待，默认关闭自动定向修正；设置 `LLM_AUTO_CORRECT=true` 可开启一轮修正。多批次默认最多两个并行请求，`LLM_MAX_CONCURRENCY` 可设为 1～4。首次模型请求失败、结构不完整或 JSON 无效会中止本次生成并显示错误，不会自动输出原文 DOCX；可切换原文模式重新生成。

## Web 流式生成

首页预览和使用 LLM 的 DOCX 生成通过 `POST /preview-stream` 返回 SSE；CLI 与原 `POST /preview` 保留同步接口。生成弹层按批次逐步显示候选正文，不显示 JSON、块 ID 或模型思考内容。可取消等待；断流或响应结构无效时显示错误，不交付不完整报告。

流式文本尚未经过校验，也可能被后续修正。模型完成后仍执行结构与事实校验、规则规范化、DOCX 渲染和 OOXML 验证，再进入审核页或下载最终快照。有冲突的候选仍默认回退原文，并在审核页保留候选供确认。流式不改变采用规则，也不缩短模型计算本身的时间。

服务每 5 秒发送心跳，设置 `Cache-Control: no-store` 和 `X-Accel-Buffering: no`。若另有反向代理，需确保 SSE 不被缓存或缓冲。连接取消会在下一次发送内容/进度时停止处理；等待上游读取时仍受 API 超时限制，取消不保证撤销已产生的 API 用量。首片段前的瞬时网络错误可重试；已输出内容后发生断流不会自动重试拼接。

2026-10-12 七条合成记录的单次流式 API 实测：首个内容片段 12.479 秒，模型与校验 13.860 秒，含 DOCX 验证共 13.893 秒，661 个内容分片。当前服务的首片段仍较晚，该样本不代表稳定性能或官网对照。

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```

最近一次验证（2026-10-12）：137 项测试通过，Ruff lint/format 通过；Flask 和 Gunicorn 双 Worker 的真实 HTTP 上传、预览、下载通过。PDF 转换已验证，但 Linux 字体替代和 Word 分页一致性仍需单独验收。自动化测试使用 Mock；另外已获准使用七条合成记录调用当前 API 测速，未外发真实周报。

## 耗时测试

仅使用脚本内生成的合成周报，不接受真实输入文件。默认 Mock 模式，无网络请求：

```bash
python scripts/benchmark_timing.py --output outputs/timing-mock.json
```

真实测试会产生 API 用量，必须显式授权；可加一次简化整批请求对照：

```bash
python scripts/benchmark_timing.py --real --allow-external --env-file .env \
  --plain-comparison --output outputs/timing-real.json
```

记录 IR 准备、LLM 批次、HTTP 尝试/重试、事实校验、DOCX 渲染与校验、总耗时；不保存请求正文、模型输出或密钥。计时器为可选接口，当前由测试脚本启用，不会自动收集所有线上请求。并发批次耗时之和不等于用户等待时间；非流式请求时间也不是首 token 时间。

2026-10-12 单次合成实测（deepseek-flash、7 个项目）：Harness 共 14.897 秒，1 批/1 次 HTTP/0 重试；API 14.845 秒，事实校验 0.013 秒，DOCX 0.034 秒。简化整批对照 11.682 秒。两者输出契约不同，该单次对照不是官网测速或稳定性能基准。
