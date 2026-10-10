# 周报生成 Agent

这是一个 Python 3.11+ 的确定性周报生成工具。它支持读取 CSV/XLSX、选择规则预设和工作表、映射字段、预览来源记录及内容块，并基于 DOCX 模板生成和校验周报。当前内容处理为纯规则模式，不使用 LLM、不需要 API Key，也不会把周报数据发送到外部服务。

## 当前功能

- `rd3`（研发三部周报）：兼容原有两行表头周报，自动选择指定周次或最新有效周次，一级章节默认从 `9` 开始。
- `general`（通用周报）：读取单行表头 CSV/XLSX，按“本周工作 / 问题与风险 / 下周计划”组织内容，一级章节默认从 `1` 开始。
- 支持 XLSX 工作表选择、常见列名自动识别和手动字段映射。
- 保留 CSV 物理行号或 XLSX Sheet/行号，为来源记录和内容块生成稳定 ID。
- Web 页面可预览完整原始行、规范字段、内容块及来源映射，再生成 DOCX。
- CLI 和 Web 均保留原有研发三部生成方式。

当前 Web 预览不持久化上传文件；预览后生成时需要重新选择同一文件。跨请求审核、编辑和 LLM 内容模式属于后续阶段。

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

## Web 页面

```bash
python -m weekly_report_agent.web --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>。需要监听 WSL 所有网卡时使用：

```bash
python -m weekly_report_agent.web --host 0.0.0.0 --port 18989
```

页面操作：

1. 上传 CSV/XLSX，并选择 `研发三部周报` 或 `通用周报`。
2. XLSX 可填写 Sheet 名；自动识别失败时展开“手动字段映射”。
3. 点击“预览来源与结构”检查物理行、字段映射和内容块来源。
4. 重新选择同一文件，点击“生成并下载 DOCX”。

上传文件只存在于本次请求的临时目录，请求完成后删除；请求总大小限制为 16 MB。如需维护 WSL 局域网访问，请参阅 [WSL 局域网访问与端口转发维护](docs/wsl-lan-access.md)。

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

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```
