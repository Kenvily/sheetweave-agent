<h1><img src="../src/static/sheetweave-mark.svg" alt="" width="40" height="40" align="absmiddle"> SheetWeaveAgent 使用指南</h1>

从首次启动到日常交付，按这份指南完成配置与使用。

**首次使用：安装启动 → 配置模型 → 创建方案 → 生成与审核。** 已有方案时，直接上传本期数据即可开始。

[产品介绍](../README.md) · [架构与设计](project-design.md)

## 1. 安装与启动

需要 Python 3.11+，在项目根目录运行。已有环境可直接使用；没有环境时可自行创建独立 Python 环境，不覆盖其他项目。

```bash
conda activate py31115
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
python -m sheetweave_agent.server --host 127.0.0.1 --port 18989
```

统一入口读取项目 `.env`，启动 Web 和独立任务 Worker；访问 `http://127.0.0.1:18989`。Ctrl+C 关闭两者。迁移项目目录后重新安装 `python -m pip install -e .`，不要直接执行 `python src/web.py`。

远程服务器可通过 SSH 转发访问：

```bash
ssh -N -L 18989:127.0.0.1:18989 用户@服务器
```

如需按现有内网策略直连，将启动参数改为 `--host 0.0.0.0`。程序不会配置防火墙、TLS 或访问认证；当前方案库是服务器共享的，不提供账户隔离。

已有 systemd 服务可执行 `sudo systemctl restart sheetweaveagent.service`。只修改 `.env` 或 Gunicorn 配置不需要 `daemon-reload`；修改 unit/override 后才需要。

需要分开启动时，两个终端使用相同环境、配置和数据路径：

```bash
# 每个终端先加载自己信任的配置文件
conda activate py31115
set -a
source .env
set +a

# 终端一
gunicorn --config gunicorn.conf.py --bind 127.0.0.1:18989 \
  'sheetweave_agent.web:create_app()'
# 终端二
python -m sheetweave_agent.task_worker
```

## 2. 配置模型与生成时限

在 `.env` 中指定获得授权的 OpenAI-compatible Chat Completions 服务：

```dotenv
LLM_API_BASE=https://模型服务地址/v1
LLM_API_KEY=填写本地密钥
LLM_MODEL=服务提供的模型名
LLM_ALLOWED_MODELS=服务提供的模型名
```

Base 不含 `/chat/completions`。模型名必须在允许列表中；示例文件中的 DeepSeek 值不是公司 API 的通用默认值。密钥只保留在本地，分享诊断结果前不要附上 `.env`。原文生成不调用模型；首次自动适配和规范润色需要模型配置与数据发送授权。

以下为**未设置环境变量时的代码默认值**，不等于 `.env.example` 中的示例选择。通用 `LLM_*` 未设置时兼容旧的 `DEEPSEEK_*` 对应配置；显式空值不会回退旧值。

| 参数 | 默认值 | 使用说明 |
|---|---|---|
| `LLM_API_BASE` | `https://api.deepseek.com` | 替换为实际服务 Base |
| `LLM_MODEL` | `deepseek-chat` | 改用内部服务时必须显式指定 |
| `LLM_ALLOWED_MODELS` | `deepseek-chat,deepseek-reasoner` | 网页可选模型，须含默认模型 |
| `LLM_TIMEOUT` | `60` 秒 | 连接/阻塞读取等待阈值，不是整份任务的总时限 |
| `LLM_MAX_RETRIES` | `1` | 可重试 HTTP/部分连接错误的额外次数；超时、正文中断、解析失败不自动重发 |
| `LLM_MAX_BATCH_CHARS` | `12000` | 首次润色批次的完整消息字符预算，不是 token 限额；单个审核区块不会强制拆开 |
| `LLM_MAX_CONCURRENCY` | `2` | 每份报告并行批次数，范围 1～4 |
| `LLM_AUTO_CORRECT` | `false` | 开启后可能为事实冲突追加修正调用 |
| `LLM_REASONING_EFFORT` | 空 | 空值不发送，仅按服务/模型实际支持设置 |

### 慢推理模型：分别检查两类超时

内部模型可能短请求很快、真实材料推理超过一分钟。已确认模型正常推理时，可按实测耗时留余量，例如 `LLM_TIMEOUT=300`；这只是配置示例，不保证所有模型在五分钟内完成。修改后重启 Web **和后台 Worker**。

| 设置 | 当前值 | 调整时注意 |
|---|---|---|
| `.env`：`LLM_TIMEOUT` | 默认 60 秒 | 控制模型连接和读取等待；批次执行时间还会累加 |
| `gunicorn.conf.py`：`timeout` | 180 秒 | Web Worker 心跳超时；当前 `gthread` 下不等于单个请求时限，也不管理独立后台模型调用 |
| `gunicorn.conf.py`：`graceful_timeout` | 180 秒 | Web Worker 优雅关闭等待，不是模型生成时限 |
| 统一入口关闭等待 | 190 秒 | 当前固定值，无对应 CLI 参数；超时仍未退出会被强制终止 |

后台模型等待 300 秒与 Gunicorn `timeout=180` 可以同时成立，无需机械地把后者改得更大。但运行任务超过 190 秒时，重启仍可能将其打断；既有 systemd 的 `TimeoutStopSec` 也可能更短。尽量在任务结束后重启，长期运行场景另行核对关闭策略。

Web 进程数在 `gunicorn.conf.py` 的 `workers` 中设置，当前为 2；统一入口仅在显式传入 `--workers N` 时覆盖。文件中的 `threads=2` 用于直接启动 Gunicorn；统一入口会传入自己的 `--threads` 值，默认也是 2。

## 3. 创建和复用方案

1. 打开“方案适配”，上传已获准处理的 CSV/XLSX 和可选参考 DOCX。
2. 选择数据工作表，填写必要的固定目录或补充要求，确认数据发送授权后执行识别。
3. 核对字段含义，尤其是“主要正文”；按目录选择写入正文的字段及行筛选条件。
4. 核对报告标题、起始章节、字体字号、行距和编号；执行“验证样例”，检查生成的 DOCX。
5. 确认后保存。以后在“新建文档”选择此方案，不必每次上传模板。

多条来源行可按同一目录合并为一个审核区块。字段“被识别”不等于“写入正文”：正文来源由目录中的字段选择决定；其他映射字段仍可能用于标题、筛选或模型上下文。状态策略只在有关字段及规则存在时生效。

### 工作表、周期与表头

| 项目 | 本次上传时如何处理 |
|---|---|
| 工作表 | 可手动选择；旧默认名称不存在时回退到当前文件首个工作表，说明页/空页仍允许切换 |
| 周次矩阵 | 从第二行识别“第N周”，从第一行识别月份；可手选周次，不选则使用最新有效周次 |
| 普通明细表 | 使用方案确认的表头行数，不额外显示周次 |
| 正文周期 | 正文映射指向周期列时，跟随所选周次；明确映射到非周期列时按其配置读取 |
| 结构检查 | 保存了 `expected_headers` 时，普通明细表要求完整规范化表头与顺序一致；矩阵允许已识别周期列变化，固定业务字段仍检查 |
| 其他规则 | 必填字段、目录筛选和状态策略继续执行，新项目不一定自动进入已有目录 |

月份或周次列变化不必重建矩阵方案；固定字段名、布局等变化时应重新适配。显式选择不存在的工作表或周次会报错，不会静默读取另一列。

仓库 `data/` 中的方案 JSON 是规则导出，不是网页直接导入接口；Git 不包含 `var/` 方案库。新部署需按上述步骤创建方案，或迁移已有方案数据库及完整版本目录。

## 4. 生成、审核与导出

1. 在“新建文档”上传数据，选择已保存方案、工作表及适用的周次。
2. 选择 **原文保留** 或 **规范润色**。前者执行本地规范化；后者允许模型重分条和调整正文层级，可填写额外要求。
3. 提交后等待任务完成。页面查询进度，关闭页面不等于取消任务；需要中止时使用取消按钮。
4. 在审核工作区点击区块，联动核对来源行、当前内容和右侧文档；查看模型候选及 Diff。
5. 关键事实冲突默认不自动采用候选，可核对告警后谨慎采用。人工编辑基于当前内容，确认修改前后 Diff 后生效，不走模型事实校验。
6. 下载“当前 DOCX”。修改会产生新 revision，旧页面提交可能返回版本冲突，刷新后再操作。

PDF 预览需服务器已有 LibreOffice 和相应字体；`LIBREOFFICE_PATH` 默认自动检测，`PDF_PREVIEW_TIMEOUT` 默认 45 秒。缺少它不影响 HTML 和 DOCX。HTML 不承诺与 Word 分页完全一致；最终排版请核对 Word 或 PDF。

CLI 也必须选择已保存方案。CLI 不自动读取项目 `.env`，需要时先加载可信配置或设置环境变量：

```bash
python -m sheetweave_agent --profile p_方案ID \
  --input 数据.xlsx --sheet 数据页 --report-date 2026-10-29 \
  --content-mode original --output outputs/报告.docx
# 周次矩阵按需追加：--week 第4周
# 模型模式需追加：--content-mode polish --allow-external
```

## 5. 并发、有效期与存储

| 参数 | 默认值 | 用法 |
|---|---|---|
| `TASK_MAX_CONCURRENCY` | `2` | 共享任务库下同时执行的任务上限，范围 1～16 |
| `TASK_QUEUE_CAPACITY` | `100` | 待处理/执行任务接纳容量 |
| `REPORT_SESSION_TTL` | `2h` | 新审核会话及下载快照有效期；支持 `24h`、`3d`、`1w` 及中文单位 |
| `WEEKLY_REPORT_SNAPSHOT_DIR` | 系统临时目录下 `weekly-report-agent-snapshots/` | 多进程共享路径；需稳定保留时设为持久目录 |
| `REPORT_PROFILES_DIR` | `var/profiles/` | 永久方案目录，数据库为同级 `profiles.sqlite3` |
| `TASK_DB_PATH` / `TASK_FILES_DIR` | `var/tasks.sqlite3` / `var/tasks/` | 任务状态及输入附件 |
| `STATS_DB_PATH` | `var/stats.sqlite3` | 聚合统计 |
| `STATS_HMAC_KEY` | 自动生成 `.key` 文件 | 可显式设置至少16字节的稳定值，各进程保持一致 |
| `REPORT_TIMEZONE` | `Asia/Shanghai` | 统计与会话到期时间显示时区 |

`TASK_MAX_CONCURRENCY=2`、`LLM_MAX_CONCURRENCY=2` 时，两份多批次报告最多可同时产生4个首次润色请求；提高并发前确认网关容量。等待模型仍占后台任务名额，不占提交时的 Web 请求线程。

会话从创建时计时，编辑和刷新不续期；修改 TTL 后重启，只影响新会话。未设置新变量时兼容旧 `REPORT_SESSION_TTL_SECONDS`。过期访问被拒绝，物理文件在后续访问/创建时按需清理；停服不会定时删除，但操作系统可能提前清理临时目录。

方案永久保存。适配草稿、队列等待及终态任务记录有独立的两小时规则，不随审核 TTL 延长；任务状态过期不代表审核快照同时过期，生成后保存审核地址并及时下载。

## 6. 管理与迁移

```bash
python scripts/manage_profiles.py
```

菜单支持查询、搜索、按未使用天数筛选、详情、重命名和删除；查看结果后按回车清屏返回，`0` 退出。删除需完整 ID 确认，活跃任务引用会阻止删除。

```bash
python scripts/manage_profiles.py list --unused-days 90
python scripts/manage_profiles.py show p_方案ID
python scripts/manage_profiles.py rename p_方案ID "新名称"
python scripts/manage_profiles.py delete p_方案ID
```

脚本默认读取项目 `.env`；可在子命令前指定 `--env-file /路径/配置.env` 或 `--no-env`。筛选不自动删除，重命名保留 ID 和历史版本。

迁移方案需同时备份 `var/profiles.sqlite3` 和完整 `var/profiles/`；更新方案不改变已有会话冻结的规则。SQLite 用 backup API 或停服后完整备份，不只拷贝可能尚未落盘的主库。统计库和 `.key` 一起保留，运行中不要删除 `*-wal`、`*-shm`。`var/verification/` 为验证记录，`outputs/` 为按需输出，均不提交 Git。

## 7. 常见问题与诊断

| 问题 | 操作 |
|---|---|
| 一直排队 | 用 `pgrep -af 'sheetweave_agent.task_worker'` 确认后台进程；核对 Web/Worker 的环境与共享路径。Gunicorn Worker 不等于任务 Worker |
| 模型生成慢 | 看等待阶段与实际模型推理耗时，按需调整 `LLM_TIMEOUT`，不要只改 Gunicorn 超时 |
| `await_headers` 超时 | 尚未收到响应头，可能是网络、网关等待或非流式模型生成；不能仅凭它认定 token 超限 |
| `read_body` 超时 | 已收到响应头，正文读取停滞；流式心跳不等于已经生成正文 |
| `response_parse` 失败 | 已收到模型内容但结构不合约，区别于网络超时 |
| 模块不存在 | 检查 Python 环境并在当前项目重新 `pip install -e .` |
| 找不到表头/有效内容 | 核对工作表、周次、固定字段、目录筛选及状态策略 |
| 版本冲突/会话过期 | 刷新获取最新版本；已过期快照需重新生成，改TTL不会恢复文件 |

独立诊断工具默认不发请求；从实际 Worker 读取配置并核对模型名：

```bash
python scripts/diagnose_llm.py --worker-pid 实际PID --expect-model 实际模型名
# 确认计划后，一次基础请求
python scripts/diagnose_llm.py --worker-pid 实际PID --expect-model 实际模型名 \
  --cases basic --max-requests 1 --execute
```

支持 `--cases report compact`、`--transport stream` 或 `both`。`both` 与三类测试一起使用时需显式 `--max-requests 6`。默认 socket 上限15秒、单次硬截止20秒、测试组预算60秒；生产等待调大后应对应调整 `--timeout`、`--deadline`、`--total-timeout`，当前每项上限180秒。真实文件还需 `--input`、`--profile` 和 `--allow-input-upload`。每次只测试所选生产批次，不代表整份报告已通过校验。

测试可能产生API用量。工具不保存密钥、Prompt或响应正文；`reasoning_chars` 增长而 `content_chars=0` 表示观察到推理但无正文。客户端截止不保证上游停止生成，避免连续重复发起慢请求。更多参数见 `python scripts/diagnose_llm.py --help`。

需要原文生成基线时：

```bash
python scripts/benchmark_timing.py --mode original --rows 7 --output /tmp/report-timing.json
```

使用既有 systemd 服务时，日志可用 `journalctl -u sheetweaveagent.service --since "10 minutes ago" -o cat` 查看，沿同一任务 ID 核对模型请求、校验和最终状态。不要分享 `.env`、请求正文或业务内容。

## 使用范围与资料说明

优先使用结构清晰的 CSV/XLSX，以及以标题、段落和列表为主的参考 DOCX。复杂表格、文本框等版式需要单独确认适配效果。HTML 用于审核内容结构，正式分页以 Word / PDF 为准。

模型处理前确认数据发送权限；原文模式可在已有方案下本地运行。仓库新增跨场景资料用于功能验证，展示统计包含生成验证任务。
