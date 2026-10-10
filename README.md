# 周报生成 Agent

这是一个 Python 3.11+ 的周报生成工具。它支持读取 CSV/XLSX、选择规则预设和工作表、映射字段、预览来源记录及内容块，并基于 DOCX 模板生成和校验周报。排版与编号始终由确定性规则完成；内容可保留原文，也可通过 DeepSeek 进行润色、精简、详细整理或按自定义要求整理。

## 当前功能

- `rd3`（研发三部周报）：兼容原有两行表头周报，自动选择指定周次或最新有效周次，一级章节默认从 `9` 开始。
- `general`（通用周报）：读取单行表头 CSV/XLSX，按“本周工作 / 问题与风险 / 下周计划”组织内容，一级章节默认从 `1` 开始。
- 支持 XLSX 工作表选择、常见列名自动识别和手动字段映射。
- 保留 CSV 物理行号或 XLSX Sheet/行号，为来源记录和内容块生成稳定 ID。
- Web 页面可预览完整原始行、规范字段、内容块及来源映射，再生成 DOCX。
- 支持原文保留、规范润色、精简总结、详细整理和自定义要求五种内容模式。
- DeepSeek API Key 仅从服务端环境读取，不在页面显示、保存或回传。
- CLI 和 Web 均保留原有研发三部生成方式。

原文保留模式不需要 API Key，也不会发起模型网络请求。其他模式只发送内容块、所属章节和项目名称，不发送完整上传文件；使用前需要明确确认允许外发。当前 Web 预览不持久化上传文件，预览后生成时需要重新选择同一文件；模型模式的预览和下载也会分别调用模型。跨请求审核与编辑属于后续阶段。

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

## 项目结构

- `src/weekly_report_agent/input_parser.py`：CSV/XLSX 读取、Sheet 选择、字段映射和物理行定位。
- `src/weekly_report_agent/rules/`：`rd3`、`general` 规则预设及 JSON 校验。
- `src/weekly_report_agent/models.py`：来源记录、内容块和报告计划模型。
- `src/weekly_report_agent/content_builder.py`：内容组织及双向来源关系。
- `src/weekly_report_agent/llm/`：DeepSeek 配置、OpenAI 兼容客户端、内容模式及事实保护。
- `src/weekly_report_agent/normalizer.py`：术语、正式称呼和相对星期规范化。
- `src/weekly_report_agent/planner.py`：规则化正文层级和报告编号规划。
- `src/weekly_report_agent/docx_renderer.py`：基于模板 OOXML 渲染 DOCX。
- `src/weekly_report_agent/validator.py`：编号、措辞和 DOCX 排版校验。
- `src/weekly_report_agent/cli.py`：命令行入口。
- `src/weekly_report_agent/web.py`：Web 预览、生成与下载入口。
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
2. XLSX 可填写 Sheet 名；自动识别失败时展开“手动字段映射”。
3. 点击“预览来源与结构”检查物理行、字段映射和内容块来源。
4. 重新选择同一文件，点击“生成并下载 DOCX”。

选择非原文模式时还需选择服务器允许的模型，并勾选数据外发确认。页面不会显示或保存 API Key。上传文件只存在于本次请求的临时目录，请求完成后删除；请求总大小限制为 16 MB。如需维护 WSL 局域网访问，请参阅 [WSL 局域网访问与端口转发维护](docs/wsl-lan-access.md)。

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

其他模式的 `--content-mode` 可设为 `concise`、`expand` 或 `custom`；自定义模式还需传入 `--custom-instruction`。模型返回结果会按稳定内容块 ID 对齐，并检查数字、日期、模型/技术名和任务状态；结构不完整或疑似改变事实时不会采用结果。

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```
