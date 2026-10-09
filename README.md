# 周报生成 Agent

这是一个 Python 3.11+ 的**纯规则周报生成工具**。它从多行表头 CSV 中选择指定周次或最新有效周次，规范正文层级、术语、日期和称呼，再基于参考模板生成并校验 DOCX。项目提供命令行和简洁 Web 页面，不使用 LLM、不需要 API Key，也不会上传周报数据。

## 规则与格式

生成结果遵循以下固定规则：

- 标题编号依次为 `1.`、`1.1`、`1.1.1`，正文编号依次为 `1、`、`（1）`、`①`。
- 一级标题：微软雅黑、加粗、16 磅，段前段后 6 磅。
- 二级标题：微软雅黑、加粗、15 磅，段前段后 6 磅。
- 三级标题：微软雅黑、加粗、14 磅，段前段后 6 磅。
- 正文：宋体、10.5 磅，首行缩进 2 字符。
- 全篇使用 1.5 倍行距，正文每次换行均补齐标点。
- `周一`、`本周二`、`下周三` 等相对星期在有报告日期时转为具体日期。
- `XX哥`、`XX姐` 等非正式称呼替换为“相关负责人”。
- 起始章节号默认是 `9`，可通过 CLI 或 Web 页面调整。
- 规则只调整结构和规范用语，不改写日期、模型版本、数字指标或业务结论。

正文会按原始编号推断层级并重新连续编号。没有显式结构的内容统一归入“进展内容”，不会调用模型进行推测或摘要。

## 项目结构

- `src/weekly_report_agent/csv_parser.py`：CSV 读取、多行表头识别与周次选择。
- `src/weekly_report_agent/normalizer.py`：术语、正式称呼和相对星期规范化。
- `src/weekly_report_agent/planner.py`：规则化正文层级和报告编号规划。
- `src/weekly_report_agent/docx_renderer.py`：基于模板 OOXML 渲染 DOCX。
- `src/weekly_report_agent/validator.py`：编号、措辞和 DOCX 排版校验。
- `src/weekly_report_agent/cli.py`：命令行入口。
- `src/weekly_report_agent/web.py`：Web 上传、生成与下载入口。
- `tests/`：单元测试、Web 测试和端到端测试。

`data/周报.csv` 与 `data/研发三部周报模板.docx` 是不可修改的参考夹具。生成文件默认写入 `output/`。

## 安装

```bash
conda activate py31115
python -m pip install -r requirements.txt
```

如果当前环境尚未安装项目，也可以在仓库根目录临时使用 `PYTHONPATH=src` 运行模块。

## Web 页面

```bash
weekly-report-web --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>。远程服务器或 Jupyter 环境可监听所有网卡：

```bash
weekly-report-web --host 0.0.0.0 --port 8000
```

页面支持上传 CSV、选择报告日期、指定周次、设置起始章节号和临时上传模板。上传文件仅存在于本次请求的临时目录，请求完成后删除；请求总大小限制为 16 MB。

如需让同一局域网设备访问 WSL 中的服务，以及维护 WSL 重启后的端口转发，请参阅 [WSL 局域网访问与端口转发维护](docs/wsl-lan-access.md)。

## 命令行

使用默认路径、最新有效周次和起始章节号 `9`：

```bash
python -m weekly_report_agent
```

指定全部参数：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output output/周报-20261009.docx \
  --week "第1周" \
  --report-date 2026-10-09 \
  --section-number 9
```

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```
