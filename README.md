# docAgent · 周报生成 Agent

Python 3.11+ 周报整理工具：CSV/XLSX → 规则与字段映射 → IR → 可选 LLM → 事实校验与回退 → DOCX。编号、术语规范化和文档格式由确定性规则处理，LLM 只整理允许改写的内容。

## 功能

- `rd3`：研发三部多行表头、多周次周报，章节默认从 9 开始；`general`：通用单行表头，章节默认从 1 开始。
- 工作表/周次选择、字段映射、来源追溯；按状态列及历史周记录标记已完成事项。
- 原文、润色、精简、详细整理、自定义五种模式；原文模式无需 API Key，不调用外部模型。
- 三栏审核、模型候选与原文 Diff、人工编辑确认、单块重生成、独立缩放与专注模式。
- DOCX 格式验收、可选 PDF、匿名使用统计；独立任务 Worker 与增量进度显示。

未知输入/复杂模板自动适配尚未实现。审核会话默认保留两小时，不提供长期历史报告列表。

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

编辑 `.env`，模型模式需要 `DEEPSEEK_API_KEY`；Base URL、模型允许列表和其他选项参考 `.env.example`。密钥只在服务端读取，不写入 Git。部署方需确认内容外发授权；CLI 模型模式要求 `--allow-external`，Web 单块重生成保留外发确认。

## 部署：两个终端都要启动

**仅启动 Gunicorn 会接收任务，但不会执行任务。原文模式也需要独立后台 Worker。** 两端使用同一用户、项目安装、环境及 `.env`；应用不会自动加载 `.env`。

### 终端一：Web 服务

```bash
conda activate py31115
cd ~/docAgent
set -a
source .env
set +a

gunicorn --config gunicorn.conf.py --workers 2 --worker-class gthread --threads 2 \
  --bind 127.0.0.1:18989 \
  --access-logfile - --error-logfile - \
  'weekly_report_agent.web:create_app()'
```

访问 <http://127.0.0.1:18989>。其他机器直接访问服务器 IP 时，绑定地址改为 `0.0.0.0:18989`；SSH 转发使用 `127.0.0.1`。Gunicorn 的两个 Worker 是 Web 子进程，不是任务 Worker。

### 终端二：后台任务 Worker

```bash
conda activate py31115
cd ~/docAgent
set -a
source .env
set +a

python -m weekly_report_agent.task_worker
```

Worker 默认同时执行两个任务，空闲时可能没有日志输出，启动后会领取已有排队任务。两端都需保持运行；代码或配置更新后分别重启。长期运行交给已有进程管理器，本项目不自动配置开机自启。未使用 `.env` 时，两端都可省略加载配置的三行。

### 常用配置

| 变量 | 默认值 / 用途 |
|---|---|
| `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE`、`DEEPSEEK_MODEL`、`DEEPSEEK_MODELS` | 模型凭据、接口、默认模型及允许列表 |
| `LLM_TIMEOUT`、`LLM_MAX_RETRIES` | 请求超时 60 秒、最多重试 1 次 |
| `LLM_MAX_CONCURRENCY`、`LLM_AUTO_CORRECT` | 单任务最多 2 批并发；默认不自动定向修正 |
| `TASK_MAX_CONCURRENCY`、`TASK_QUEUE_CAPACITY` | 同配置后台 Worker 共享任务并发 2、接纳容量 100 |
| `TASK_DB_PATH`、`TASK_FILES_DIR` | 默认项目 `var/tasks.sqlite3`、`var/tasks/` |
| `WEEKLY_REPORT_SNAPSHOT_DIR` | 默认系统临时目录下 `weekly-report-agent-snapshots/` |
| `STATS_DB_PATH` | 默认项目 `var/stats.sqlite3` |
| `LIBREOFFICE_PATH`、`PDF_PREVIEW_TIMEOUT` | 可选 PDF 转换器与超时 45 秒 |

Web 与后台 Worker 必须连接同一任务数据库，使用同一任务文件和快照目录；路径配置建议使用实际绝对路径。详细排查、数据管理及网络访问见 [运维说明](docs/operations.md)。

## 使用

网页上传文件 → 选择规则、日期和整理模式 → 预览审核 → 下载 DOCX。示例下载在“使用帮助”页面，首次体验示例请选择“通用周报”。

“当前内容”和 HTML 正文显示用于导出的规范化、标点与编号结果。模型候选未通过校验时默认回退原文，核对后可谨慎采用；人工编辑以当前采用内容为基准，通过独立修改 Diff 确认后生效。流式候选尚未校验，最终导出始终使用完成校验的快照。

提交返回任务 ID，浏览器短轮询获取进度；同页刷新可恢复任务。关闭页面不自动取消后台任务，主动取消是协作式的，发布阶段不接受取消。任务队列隔离 Web 容量，不会直接缩短模型本身的计算时间。

CLI 不依赖后台 Worker，默认原文模式：

```bash
python -m weekly_report_agent \
  --input data/周报.csv \
  --template data/研发三部周报模板.docx \
  --output outputs/周报.docx
```

模型模式增加 `--content-mode polish --model <允许的模型> --allow-external`；自定义模式使用 `--content-mode custom --custom-instruction '整理要求'`。其他参数用 `python -m weekly_report_agent --help` 查看。

## 开发与验证

```bash
python -m pytest
ruff check .
ruff format --check .
```

当前回归基线：**152 passed**，Ruff lint/format 通过。测试使用 Mock；真实 API 测速只允许使用获准的合成数据。默认离线测速：

```bash
python scripts/benchmark_timing.py
```

## 文件与文档

- `src/`：应用代码、规则、页面、合成示例；`tests/`：按功能组织的当前回归测试。
- `data/`：原 CSV 与 DOCX 参考夹具，保持不变；`scripts/`：开发测速脚本。
- `var/`：私有运行数据；`outputs/`：按需创建的报告输出，二者不提交 Git。
- [项目架构](docs/project-overview.md)：模块、Harness、审核与队列实现。
- [运维说明](docs/operations.md)：排队排查、运行数据、进程与网络访问。
- [ROADMAP](ROADMAP.md)：当前进度、边界和后续计划。
