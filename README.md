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
- 中栏只展示当前生效内容；每个内容块最多提供一个以原文为基准的 Git 风格单栏 Unified Diff；上下文行保持原样，仅用 `-` 红色删除行和 `+` 绿色新增行突出变化，并支持版本恢复、单块重新生成和事实冲突回退。
- 支持原文保留、规范润色、精简总结、详细整理和自定义要求五种内容模式。
- LLM 仅能改写所选周次的工作内容；项目、任务、结构状态和日期字段只作为只读上下文。
- 对模型版本、指标、百分比、百分点、数量、日期和任务状态执行归一化分级校验。
- DeepSeek API Key 仅从服务端环境读取，不在页面显示、保存或回传。
- CLI 和 Web 均保留原有研发三部生成方式。

原文保留模式不需要 API Key，也不会发起模型网络请求。其他模式只发送可编辑工作内容、必要的只读结构上下文和归一化事实，不发送完整上传文件；使用前需要明确确认允许外发。Web 预览会建立一个有效期为 2 小时的临时审核会话，可直接下载，无需重新选择文件；刷新页面后人工编辑仍保留。保存编辑、切换有效版本或单块重新生成时会生成新 revision，并同步更新 HTML 预览和 DOCX。模型模式不会在刷新、切换版本或下载时再次调用模型。过期会话自动清理，不提供历史报告检索。

四种模型模式采用不同的写作契约，而不只是切换名称：规范润色以原文 85%～115% 的长度等长改写并保持事项顺序；精简总结以 50%～75% 为目标压缩重复内容；详细整理以 120%～160% 为目标补全句子和事实间衔接；自定义要求按用户输入控制表达。上述长度均为软目标，数字、指标、模型版本、日期和状态等受保护事实必须优先完整保留，因此事实密度很高的短文本可能无法达到目标比例。

## 当前开发阶段

ROADMAP 的 M0～M3 已完成。M4 已完成第一阶段增强：DOCX 模板和输出会检查必要部件、ZIP 安全边界、核心 XML、页面尺寸、页边距、编号及段落样式；三栏工作区可在 LibreOffice 可用时按当前 revision 生成 PDF 真实版式预览。多模板目前保留为高级适配设置中的简单上传与字段映射，完整的多模板配置体验仍待完善；匿名统计与完整网页帮助属于 M5。

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

规则预设位于 `src/weekly_report_agent/rules/`。JSON 分别定义输入映射、内容章节和文档参数，加载时会校验字段引用、章节、编号范围和模板相对路径。DOCX 仍由标准库 OOXML 引擎生成，不使用 `python-docx`。

历史完成标记只读取“状态”列，不解析周报正文中的“完成”字样。对包含多周列的规则，如果状态为完成、已完成、结项或关闭等配置值，并且所选周次之前至少有一周记录，该事项仍保留在本次报告中，但正文统一写为“已完成”并跳过 LLM。首周完成或此前没有周记录时保留本周实际内容。该能力属于通用 `weekly_matrix` 规则配置，不依赖研发三部预设；单周记录格式没有历史周列，无法据此判断。

## 项目结构

- `src/weekly_report_agent/input_parser.py`：CSV/XLSX 读取、Sheet 选择、字段映射和物理行定位。
- `src/weekly_report_agent/rules/`：`rd3`、`general` 规则预设及 JSON 校验。
- `src/weekly_report_agent/models.py`：来源记录、内容块和报告计划模型。
- `src/weekly_report_agent/content_builder.py`：内容组织及双向来源关系。
- `src/weekly_report_agent/llm/service.py`：内容模式、最小化 Prompt、批处理、有限修正和安全回退。
- `src/weekly_report_agent/llm/facts.py`：模型版本、指标、数量、日期和状态等带类型事实提取与归一化。
- `src/weekly_report_agent/llm/validation.py`：模式相关的通过、待人工确认和严重冲突分级校验。
- `src/weekly_report_agent/normalizer.py`：术语、正式称呼和相对星期规范化。
- `src/weekly_report_agent/planner.py`：规则化正文层级和报告编号规划。
- `src/weekly_report_agent/docx_renderer.py`：基于模板 OOXML 渲染 DOCX。
- `src/weekly_report_agent/docx_security.py`：DOCX 包路径、体积、必要部件和核心 XML 安全检查。
- `src/weekly_report_agent/validator.py`：编号、措辞、页面设置和 DOCX 排版校验。
- `src/weekly_report_agent/pdf_preview.py`：可选 LibreOffice headless PDF 版式转换。
- `src/weekly_report_agent/cli.py`：命令行入口。
- `src/weekly_report_agent/web.py`：Web 上传、三栏审核、编辑、重生成与下载入口。
- `src/weekly_report_agent/web_sessions.py`：两小时临时会话、revision 并发保护及当前 DOCX 快照。
- `src/weekly_report_agent/templates/workspace.html`：三栏审核工作区与原生 JavaScript 双向高亮。
- `tests/`：单元测试、Web 测试和端到端回归测试。

`data/周报.csv` 与 `data/研发三部周报模板.docx` 是不可修改的参考夹具。生成文件默认写入 `output/`。

## 安装

```bash
conda activate py31115
python -m pip install -r requirements.txt
```

项目运行依赖固定为 Flask `3.1.3` 和 openpyxl `3.1.5`；openpyxl 仅用于读取 XLSX。

### DeepSeek 配置

只有非原文模式需要配置。不要把真实 Key 写入 `.env.example`：该文件是可提交到 Git 的配置模板。请在项目根目录复制一份名为 `.env` 的本地配置文件：

```bash
cp .env.example .env
```

编辑 `.env`，将第一行替换为自己的 Key：

```dotenv
DEEPSEEK_API_KEY=sk-请替换为你的真实Key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_MODELS=deepseek-chat,deepseek-reasoner
LLM_TIMEOUT=60
LLM_MAX_RETRIES=1
LLM_MAX_BATCH_CHARS=12000
```

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

项目不会自动读取 `.env`，每次新开终端后都需要先执行 `source .env`，或者把加载命令配置到仅自己可访问的启动脚本或进程管理器中。不要把 `.env`、真实 Key 或包含 Key 的终端输出提交到 Git。

## Web 页面

```bash
python -m weekly_report_agent.web --host 127.0.0.1 --port 18989
```

浏览器访问 <http://127.0.0.1:18989>。需要监听 WSL 所有网卡时使用：

```bash
python -m weekly_report_agent.web --host 0.0.0.0 --port 18989
```

页面操作：

1. 上传 CSV/XLSX，并选择 `研发三部周报` 或 `通用周报`。
2. 选择文件后，页面自动读取可选工作表和周次；起始章节号留空时使用规则默认值，也可直接输入。
3. 点击“预览来源与内容”进入三栏工作区；点击左侧来源行或中间内容块可双向定位。
4. 在中栏审核当前生效内容；发生模型改写或人工编辑时，可展开唯一的 Git 风格单栏 Unified Diff。上下文行保持原样，删除行以红色 `-` 标记，新增行以绿色 `+` 标记；也可保存编辑、恢复原文或只重新生成当前块。
5. 从右栏下载当前 revision 的 DOCX；若服务器安装了 LibreOffice，也可手动生成同一 revision 的 PDF 版式预览。会话保留 2 小时，刷新页面无需重新选择文件。

人工编辑会再次执行事实校验：校验通过时自动采用；待确认版本需显式选择；严重冲突版本保留供核对，但不能直接导出。若多个标签页同时修改，旧 revision 会收到 `409` 提示，不会静默覆盖新内容。单块重生成不会修改其他块；已有人工编辑时必须明确勾选覆盖。

“高级适配设置”当前提供自定义 DOCX 模板上传和字段映射。上传模板会先执行受限 ZIP/DOCX 检查；现阶段仍由已有 OOXML 渲染器替换正文，不承诺兼容任意复杂模板布局。

PDF 预览是可选能力，不影响 DOCX 生成和下载。安装 LibreOffice 后通常可自动识别 `soffice`；也可在 `.env` 中配置：

```dotenv
LIBREOFFICE_PATH=/usr/bin/soffice
PDF_PREVIEW_TIMEOUT=45
```

生成 PDF 后可在右栏通过 HTML/PDF 页签随时切换，两种预览基于同一 revision。未安装 LibreOffice 时页面继续显示 HTML 快速预览。人工编辑、采用版本或单块重生成后，旧 PDF 会自动失效，需针对新的 revision 手动重新生成。

选择非原文模式时还需选择服务器允许的模型。页面不会显示或保存 API Key；是否允许通过外部模型处理周报数据，应由部署方按公司数据管理要求统一控制。上传原文件在请求完成后删除；预览生成的 DOCX 快照在本机受限临时目录保留 2 小时并自动清理；请求总大小限制为 16 MB。如需维护 WSL 局域网访问，请参阅 [WSL 局域网访问与端口转发维护](docs/wsl-lan-access.md)。

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
  --output output/研发三部周报.docx \
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
  --output output/通用周报.docx \
  --report-date 2026-10-09
```

通用规则至少需要可映射到“项目”和“本周工作”的列；可选列为“问题与风险”和“下周计划”。CLI 当前使用预设中的列别名自动映射，复杂手动映射请使用 Web 页面。

原文模式完全离线，是 CLI 默认行为。使用 DeepSeek 润色时必须显式确认允许外发：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output output/研发三部周报-润色.docx \
  --content-mode polish \
  --model deepseek-chat \
  --allow-external
```

其他模式的 `--content-mode` 可设为 `concise`、`expand` 或 `custom`；自定义模式还需传入 `--custom-instruction`。模型返回结果会按稳定内容块 ID 对齐。校验分为“通过、待人工确认、严重冲突”：通过的候选进入 DOCX；待确认或严重冲突的候选保留在 Web 预览中并展示原文、生成内容和原因，但 DOCX 自动回退原文。严重冲突只进行一次针对该块的修正，不会反复重跑整个批次；结构不完整或 JSON 无效仍会终止本次模型处理。

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```
