# 周报生成 Agent

本项目使用 Python 3.11+，从多行表头 CSV 中选择指定周次或最新有效周次，经过确定性规范化与层级规划后，基于参考模板生成并校验 DOCX 周报。运行时仅使用 Python 标准库，不调用外部服务，也不会改写日期、版本、指标或业务结论。

## 项目结构

- `src/weekly_report_agent/csv_parser.py`：CSV 读取、多行表头识别与周次选择。
- `src/weekly_report_agent/normalizer.py`：术语、正式称呼和相对星期规范化。
- `src/weekly_report_agent/planner.py`：项目分组与 `9.` 起始的标题层级规划。
- `src/weekly_report_agent/docx_renderer.py`：基于模板 OOXML 渲染 DOCX。
- `src/weekly_report_agent/validator.py`：DOCX 包、标题层级和排版校验。
- `src/weekly_report_agent/cli.py`：命令行入口。
- `tests/`：单元测试和端到端回归测试。

`data/周报.csv` 和 `data/研发三部周报模板.docx` 是不可修改的参考夹具；生成文件默认写入 `output/`。

## 环境与安装

```bash
conda activate py31115
python -m pip install -r requirements.txt
```

依赖均已固定版本。应用运行时没有第三方依赖，`pytest` 与 Ruff 用于开发验证。

## 生成报告

使用默认路径并自动选择最后一个有有效进展的周次：

```bash
python -m weekly_report_agent
```

指定周次、报告日期和输出文件：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output output/周报-20260924.docx \
  --week "第1周" \
  --report-date 2026-09-24
```

安装后也可以使用 `weekly-report-agent` 命令。周次支持 `第1周` 或 `2026年9月第1周`。没有周进展的项目不会写入报告。

## 验证

```bash
python -m pytest
ruff check .
ruff format --check .
```

当前内容处理完全采用确定性规则。若后续接入 LLM，应在规范化与规划之间增加独立接口，并继续对所有数字、日期、模型版本和结论执行事实保真校验。
