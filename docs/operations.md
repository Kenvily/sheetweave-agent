# SheetWeaveAgent 项目与运维说明

## 产品定位

将结构化业务数据，按照企业模板和内容规则，生成可追溯、可审核的正式文档。输入为 Excel / CSV，输出为符合已确认方案的 DOCX；模型负责允许范围内的语义整理，规则与 Harness 负责路由、约束、校验和回退，人工负责最终内容确认。

方案适配定义字段、目录、正文来源、筛选和格式，新建文档负责使用方案处理本次数据，审核工作区负责核对来源、候选及最终内容。项目进展、研发与评估记录、交付台账均可作为适配方向；周报是已有场景，周期矩阵只是输入布局之一，不是产品的唯一用途。现有能力不等于自动完成经营分析、专业合规判断或任意复杂模板排版。

已有方案名称和报告标题属于用户内容，不因产品重新定位自动改名；历史文件名、`weekly_matrix` 布局、`WEEKLY_REPORT_*` 配置键和存储路径继续兼容。

## 架构与目录

```text
浏览器 → Flask / Gunicorn → SQLite 任务队列 → 独立 Worker
   ↑             ↑              ↑                  |
   └─ 状态短轮询 ┘              └─ 更新进度/结果 ──┘

Worker：CSV/XLSX → 字段映射/方案 → IR → 可选 LLM
                                     ↓
                              事实校验与失败回退
                                     ↓
                              规范化 → DOCX 格式校验
                                     ↓
浏览器 ← 审核/编辑/下载 ← 带有效期的文件快照
```

方案适配负责识别并确认字段、目录、正文来源和格式；实际报告生成复用已保存方案。多行内容可按最终标题合并成一个审核区块。规则与 Harness 控制结构、事实、格式及失败回退；LLM 在允许的正文范围内整理语义，不能替代格式验收。

| 位置 | 职责 |
|---|---|
| `src/` | 映射为 Python 包 `sheetweave_agent`，不使用 `src.sheetweave_agent` |
| `input_parser.py`、`csv_parser.py` | CSV/XLSX、表头、工作表/周次、字段映射及来源位置 |
| `models.py`、`content_builder.py`、`planner.py` | IR、分组、段落层级、正文与目录规划 |
| `normalizer.py`、`numbering.py` | 确定性术语、称呼、日期、标点和编号；术语表为 `TERM_REPLACEMENTS` |
| `adaptation.py`、`web_profiles.py`、`profiles.py` | 辅助识别、用户确认、试生成、永久方案与不可变版本 |
| `llm/`、`service.py` | 服务端模型配置、批次处理、事实校验与生成编排 |
| `docx_renderer.py`、`word_lists.py`、`document_style.py`、`validator.py` | 标准库 ZIP/XML 渲染、Word 样式/列表及格式校验 |
| `web.py`、`web_sessions.py`、`pdf_preview.py` | Web、带有效期的审核快照、revision 和 PDF 转换 |
| `task_queue.py`、`task_worker.py`、`server.py` | 持久队列、独立执行与单终端进程管理 |
| `stats.py`、`timing.py` | 匿名聚合统计和阶段计时 |
| `templates/`、`static/` | 页面、样式、SVG 品牌资源、短轮询和三栏审核交互 |
| `src/rules/general.json` | 内部适配基线，不是公开生成入口或默认业务规则 |
| `tests/` | 核心回归；`fixtures/rules/rd3.json` 仅供周次矩阵等测试使用 |
| `scripts/` | 方案管理和合成数据测速 |
| `data/`、`src/examples/` | 参考夹具与可分发示例，不自动清理业务输入 |
| `var/`、`outputs/` | 私有运行数据及按需生成的报告，不提交 Git |

原文保留不调用模型，保留事项顺序与显式层级；规范润色允许重分条、重组结构和可选补充要求。日期、版本、指标、明确数量和状态冲突会回退，术语及无单位数字的未配对差异作为提示保留。人工编辑以当前内容为基础，确认 Diff 后生效。来源、模型候选和当前版本分别保存，可追溯对应输入行。

DOCX 使用原生三级正文列表及关联样式，每个目录隔离编号定义、实例和样式，支持 Word 中续号和级别调整。默认三级正文比二级多缩进两字符。标题具有大纲语义；HTML 是语义预览，最终分页以 Word/PDF 为准。复杂表格、文本框等模板不保证兼容。

## 方案约束与每次生成范围

方案定义字段语义、输入布局、目录和文档格式；工作表及报告周次由每次上传的文件决定。已保存方案无需为工作表改名重新保存：`sheet_default` 仅作为优先选择，旧名称不存在时回退到当前文件第一个工作表，用户可在首页下拉框手动切换；显式选择了不存在的工作表仍会报错，避免静默读错页。说明页或空工作表不会阻断工作表列表加载，生成时仍拒绝空数据。

周次矩阵从当前工作表第二行识别“第N周”，可手选，未选择时采用最新有有效进展的周次。切换文件/方案会重新识别默认周次；切换工作表时只保留该表仍存在的周次。月份变化和已识别的周次列增减不要求重建方案；单行明细表（`records`）不会凭空增加周次选择器。

| 已保存约束 | 当前行为 |
|---|---|
| 输入布局、表头行数 | 仍按方案解析，不自动猜测另一种布局；实际结构变化需适配方案 |
| `expected_headers` | 有保存时，普通明细表要求完整规范化表头及顺序一致，包含未写入正文的列；空白列转为“未命名列N”、重名列加后缀。周次矩阵允许识别到的周期区域中月份、周次及空白周期列变化，固定字段仍检查 |
| 必填字段与映射 | 必填字段必须能通过别名或显式映射定位，工作表可选不代表任何表都适用；字段名/固定列结构变化仍可能要求重新适配 |
| 正文来源、目录、分组及行筛选 | 按保存规则执行；指定项目或其他字段值的筛选不会自动放开，新项目可能被排除或需要新增目录规则 |
| 状态策略 | 已知但未保留的状态会被排除；未知/空状态保留并提示，历史完成标记依方案执行 |
| 文档格式 | 标题组织、样式和编号仍依方案及格式校验执行，不受工作表/周次选择影响 |

这些检查保护字段和内容归属，不要求 Excel 文件名、字体、列宽或正文值与样例一致。若普通明细表只改了一个未使用列名，也可能触发完整表头检查；这是当前保留的兼容性边界，不能把工作表/周次放宽理解为取消全部结构校验。

## 服务启动

推荐使用 [README](../README.md) 的统一入口：默认 2 个 gthread Web 进程、每进程 2 个请求线程，另有独立后台任务进程。日志输出当前终端；未安装开机自启或外部进程守护。端口被占用时启动会失败。

Web 进程数统一在 `gunicorn.conf.py` 的 `workers` 中配置，当前为 `2`。统一入口默认沿用该值，只有显式使用 `python -m sheetweave_agent.server --workers N` 才通过命令行覆盖；直接启动 Gunicorn 时的 `--workers N` 也会覆盖文件值。它控制 Web 进程，不改变后台 `TASK_MAX_CONCURRENCY`。

需要分开管理时，**两个终端必须加载相同配置**：

```bash
conda activate py31115
cd ~/SheetWeaveAgent
set -a; source .env; set +a            # 仅加载自己信任的本地配置
# 终端一：
gunicorn --config gunicorn.conf.py --worker-class gthread --threads 2 \
  --bind 127.0.0.1:18989 'sheetweave_agent.web:create_app()'
# 终端二：执行相同环境配置后
python -m sheetweave_agent.task_worker
```

同机浏览器访问 `http://127.0.0.1:18989`。远程服务器可用 `ssh -N -L 18989:127.0.0.1:18989 用户@服务器` 转发；局域网直连需绑定 `0.0.0.0` 并按现有网络规则放行。程序不修改代理、防火墙或系统源。迁移目录后重新 `python -m pip install -e .`，旧包升级可先卸载 `weekly-report-agent`；新包/命令统一使用 SheetWeave 名称。

## 配置

统一入口默认读取项目 `.env`，也支持 `--env-file /绝对路径/配置.env` 或 `--no-env`。修改后重启 Web 和 Worker。下表默认值以未设置环境变量时的代码为准，`.env.example` 是可调整的示例；除明确说明外，数值配置不要填空字符串。

### 模型与 PDF

| 变量 | 默认值 / 用途 |
|---|---|
| `LLM_API_KEY` | 空；仅保存在服务端，原文模式不需要 |
| `LLM_API_BASE` | `https://api.deepseek.com`；OpenAI-compatible Base，不含 `/chat/completions` |
| `LLM_MODEL` | `deepseek-chat`；当前默认模型 |
| `LLM_ALLOWED_MODELS` | `deepseek-chat,deepseek-reasoner`；默认模型必须在列表内 |
| `LLM_TIMEOUT` | `60` 秒，连接/单次阻塞读取等待阈值；不是整个任务的总时限 |
| `LLM_MAX_RETRIES` | `1`，限流、服务端错误及部分连接错误的最多重试次数；超时与解析失败不自动重试 |
| `LLM_MAX_BATCH_CHARS` | `12000`，首次规范润色批次的完整消息字符预算，含固定 Prompt、来源和事实上下文；单个审核区块不可拆分，超限会记录 `oversized=true` |
| `LLM_MAX_CONCURRENCY` | `2`，单份报告的并行批次数，范围 1～4 |
| `LLM_AUTO_CORRECT` | `false`；开启后可能为冲突追加修正调用 |
| `LLM_REASONING_EFFORT` | 空，不发送；仅在所选服务/模型支持时配置 |
| `LIBREOFFICE_PATH` | 空，自动检测 `soffice` / `libreoffice`；仅 PDF 需要 |
| `PDF_PREVIEW_TIMEOUT` | `45` 秒，PDF 转换超时 |

通用 `LLM_*` 未设置时兼容旧 `DEEPSEEK_*`；显式空值不会回退旧值。Key 不会显示在网页中。不要未经授权发送公司内容；方案识别需确认脱敏和外发权限，CLI 模型模式需 `--allow-external`。

### 模型等待与 Web Worker 超时

**部署时分别检查 `.env` 的 `LLM_TIMEOUT` 与 `gunicorn.conf.py` 的超时设置，它们作用于不同进程和阶段。** 公司内部推理模型可能在开始返回正文前思考较久；真实内容的耗时不能用一个简单 JSON 测试替代。若已确认模型仍在正常推理，可按实际耗时留出余量，例如：

```dotenv
# 示例：允许较慢的内部模型等待 5 分钟；不是默认值或通用推荐值。
LLM_TIMEOUT=300
```

| 配置位置 | 当前默认 / 设置值 | 作用与调整依据 |
|---|---|---|
| `.env`：`LLM_TIMEOUT` | 代码默认 `60` 秒 | 上游 HTTP 连接和阻塞读取的等待阈值。非流式可能等推理及正文生成完成才返回响应头；根据真实请求耗时和网关能力设置余量。多个批次、重试或修正可能使整份任务耗时更长；持续有数据的流式请求也可能超过该数值。 |
| `gunicorn.conf.py`：`timeout` | 文件当前为 `180` 秒 | Web Worker 长时间未向主进程报告存活时被杀死并重启。当前 `gthread` 有独立心跳，不是单个 HTTP 请求的固定时长上限。若改用 `sync` Worker，长同步请求可能触发该限制，应为完整同步处理时间留出余量。 |
| `gunicorn.conf.py`：`graceful_timeout` | 文件当前为 `180` 秒 | Gunicorn 优雅关闭/重启时等待 Web 请求完成的时间，不是正常运行期间的模型等待上限；也不管理独立任务 Worker。 |
| `src/server.py`：统一入口关闭等待 | 当前 `190` 秒 | 收到停止信号后同时通知 Web 和任务 Worker，超过该时间仍未退出会被强制终止。它不随以上两项配置自动增加，目前也没有对应环境变量或 CLI 参数。 |

当前队列路径是浏览器提交任务后短轮询，LLM 请求由独立任务 Worker 执行。仅提高 Gunicorn `timeout` **不能修复后台 LLM 超时**；只提高 `LLM_TIMEOUT` 也不能防止 Web Worker 失去心跳、上游网关主动断开连接或部署平台强制停止服务。对于此路径，`LLM_TIMEOUT=300` 与 Gunicorn `timeout=180` 并不矛盾，无需强制将后者调得更大。

若使用 `/preview`、`/generate` 等同步入口，应同时核对 Web Worker 类型、完整请求耗时及实际反向代理/客户端的超时；`gthread` 的长请求仍会占用请求线程。不要把 Worker 心跳超时当成接口调用的硬截止时间，也不要仅为等待模型而关闭所有超时保护。

修改 `.env` 后重启 **Web 与独立任务 Worker**；修改 `gunicorn.conf.py` 后重启 Gunicorn。统一入口会加载该配置文件，Gunicorn 命令行参数可覆盖文件值，分开启动时应显式使用 `--config gunicorn.conf.py`。沿用现有 systemd 服务的部署可执行 `sudo systemctl restart sheetweaveagent.service`，单纯修改上述两份文件不需要 `daemon-reload`；仅修改 unit/override 时才需要重新加载 systemd 配置。

**提高模型等待时间后，还要考虑停止/重启时的任务。** 例如模型允许等待 300 秒，但统一入口最多等待关闭 190 秒，运行中的任务仍可能在重启时被打断；systemd 实际设置的 `TimeoutStopSec` 也可能更早结束服务。尽量在无运行任务时重启，如需保证更长的优雅关闭，应另行核对并调整部署关闭策略，不能只修改 Gunicorn `graceful_timeout`。队列租约负责识别中断，不会无条件重放可能计费的调用。

诊断工具 `scripts/diagnose_llm.py` 有独立的 `--timeout`、`--deadline` 和 `--total-timeout`：默认分别为 15、20、60 秒，socket 阈值还会取其与 Worker `LLM_TIMEOUT` 的较小值。生产环境调大 `LLM_TIMEOUT` 后，诊断脚本仍可能提前截止，应显式调整这些参数并区别 `wall_deadline` 与 `socket_timeout`。当前脚本每个时间参数上限为 180 秒，不能直接照抄大于 180 秒的生产值；更长调用应结合实际 Worker 日志观察。

### 会话、队列与存储

| 变量 | 默认值 / 用途 |
|---|---|
| `REPORT_SESSION_TTL` | **`2h`**；新审核会话及下载快照有效期，空值也使用默认；正整数加 `h/d/w` 或“小时/天/周” |
| `WEEKLY_REPORT_SNAPSHOT_DIR` | 空，系统临时目录下 `weekly-report-agent-snapshots/`；沿用旧名兼容快照，可改为持久路径 |
| `TASK_DB_PATH` | 空，项目 `var/tasks.sqlite3` |
| `TASK_FILES_DIR` | 空，项目 `var/tasks/` |
| `TASK_MAX_CONCURRENCY` | `2`，同配置后台 Worker 共享的任务并发上限，范围 1～16 |
| `TASK_QUEUE_CAPACITY` | `100`，队列接纳容量 |
| `REPORT_PROFILES_DIR` | 空，项目 `var/profiles/`，元数据为其同级 `profiles.sqlite3` |
| `STATS_DB_PATH` | 空，项目 `var/stats.sqlite3` |
| `REPORT_TIMEZONE` | `Asia/Shanghai`；统计及审核到期时间的显示时区 |
| `STATS_HMAC_KEY` | 空，自动生成并保存 `.key`；显式配置至少 16 字节且所有进程一致 |

`TASK_MAX_CONCURRENCY` 控制同时执行几份任务，`LLM_MAX_CONCURRENCY` 控制一份报告内几批模型请求并行；提高并发不能缩短单次上游响应时间。Web 与 Worker 必须使用同一路径和配置。

将新审核会话延长至 24 小时：

```dotenv
REPORT_SESSION_TTL=24h
```

可设置 `24h`、`3d`、`1w` 或 `24小时`、`3天`、`1周`。未设置新变量时兼容旧的 `REPORT_SESSION_TTL_SECONDS` 正整数秒值；两者同时存在时新变量优先。

有效期从创建时计算，写入快照的 `expires_at`；刷新、编辑、下载或关闭页面均不会延长或暂停。配置变更只影响新快照，旧快照仍按原到期时间读取和清理。首页显示当前配置，审核页显示该会话实际到期时间。过期访问立即拒绝，物理文件在后续访问或创建会话时按需清理；停服时不会定时执行删除。临时目录也可能由操作系统清理，如需稳定保存到配置期限请使用持久路径。

以下周期**互相独立**：审核会话使用上面的可配值；适配草稿固定两小时，新分析时清理；排队超过两小时不再执行，终态任务结束两小时后由 Worker 清理；永久方案与累计统计不自动过期。任务状态链接过期不等于仍有效的审核地址失效，应保存审核地址并及时下载。

## 维护与备份

| 数据 | 管理方式 |
|---|---|
| `var/profiles.sqlite3` 和 `var/profiles/<id>/<version>/` | 索引、规则和模板共同构成永久方案，必须一起备份 |
| `var/tasks.sqlite3` 与 `var/tasks/` | 短期任务状态、候选事件、输入和模板；不要运行中清空 |
| 快照及同级 `*-adaptations/` | 审核的 DOCX/PDF/来源副本及短期适配草稿，按上述周期清理 |
| `var/stats.sqlite3`、`.key` | 匿名统计和去重密钥一起保留；删除会丢失统计或去重一致性 |
| `*-wal`、`*-shm` | SQLite 自身管理，运行中不要手动删除 |
| `var/logs/`、`var/run/` | 部署方式按需使用，不保证一定存在或持续更新 |
| `outputs/` | CLI 或测速生成结果，由使用者决定保留 |

`python scripts/manage_profiles.py` 提供查询、搜索、最近使用、详情、重命名和确认删除菜单。方案无账户隔离，服务器共享；删除需完整 ID，活跃任务引用会阻止删除。更新或删除方案不会改写已生成会话的冻结规则。备份方案时停止方案写入或持有文件锁；SQLite 使用 backup API，不仅复制可能未落盘的主库。不要把 Key、业务正文或运行数据加入 Git。

## 排查

- **一直排队**：Gunicorn 的 worker 是 Web 子进程；用 `pgrep -af 'sheetweave_agent.task_worker'` 确认独立任务进程，核对两端 Python、`.env`、路径、权限和并发容量。HTTP 202 仅表示已入队。
- **模型慢**：比较排队耗时与执行耗时；检查批次大小、上游响应、重试和自动修正。可用 `scripts/benchmark_timing.py` 对合成数据计时，不发送真实业务数据。
- **任务中断**：队列用租约识别故障，不自动重放可能计费的外部调用；刷新状态后核对结果。发布时可能跨快照/队列存储部分成功，先确认审核版本。
- **版本冲突**：编辑、重生成、PDF 绑定 revision，过时请求返回 409；刷新后再操作，不覆盖新版本。
- **PDF 不可用**：确认 LibreOffice 路径、转换权限、超时及字体；不会自动安装软件，不影响 DOCX 下载。
- **会话过期**：查看审核页到期时间。修改 TTL 后需重新生成，不能恢复已过期文件；重新部署后保持存储路径一致。
- **模块不存在**：确认环境、路径和 editable 安装指向新目录。实际入口为 `sheetweave_agent`，不要直接执行 `python src/web.py`。

## 回归范围

`tests/` 保留核心解析、规范化/语义分段、LLM 合约与事实保护、方案生命周期、队列、审核、Word 列表、文档安全/PDF 和服务/统计测试。删除旧流式页面、重复入口断言、历史菜单变体和依赖本地业务文件的测试；状态列完成规则等仍在使用的行为保留代表用例。所有模型调用使用 Mock，写入临时目录。运行 `python -m pytest`、`ruff check .`、`ruff format --check .`，实际结果以当前运行记录为准。


## 公司网关超时诊断

短请求成功只证明基础链路可用，推理型模型处理真实业务数据可能超过默认 60 秒。已在内部部署观察到模型持续返回推理而迟迟没有正文，提高 `LLM_TIMEOUT` 后能够完成生成；遇到类似情况，应根据实际模型耗时配置等待时间，参见上面的超时说明。其他部署仍需通过阶段日志区分网关排队、长生成和响应停滞，不能把所有超时都归因于同一原因。当前版本不自动重发超时或已收到响应头后中断的请求，避免上游仍在生成时重复计费/排队；HTTP 429/5xx 和部分连接错误仍按配置有限重试。`LLM_TIMEOUT` 是 urllib 的阻塞等待阈值，不是硬性的完整请求/任务总时限。

Worker 终端或现有 systemd journal 会输出 JSON 诊断行。沿同一 `task_id`，查看：

| 事件 / stage | 含义 |
|---|---|
| `worker_enter` | 任务被后台接手，区别于排队 |
| `prepare_ir` | 表格、映射和 IR 准备耗时 |
| `llm_plan` / `llm_batch_input` | 批次数、并发数、区块数、实际消息字符数及是否单块超限 |
| `http_start` | 模型、实际请求体字节数、等待阈值、尝试次数与上限 |
| `http_headers` | 已收到响应头及其耗时，随后开始读取完整非流式响应 |
| `http_end` | 成败、HTTP 状态、响应字节数、耗时、异常类型及是否重试 |
| `response_parse` / `fact_validation` | 模型内容结构解析与本地事实校验 |
| `docx_render_validate` | DOCX 渲染和格式验证 |
| `task_failure` / `task_result` | 失败类型及最终队列状态；`stage_end(task)` 只表示执行器已返回，成败以 `task_result.state` 为准 |

方案辅助识别与可选自动修正有各自的请求组织方式，不受首次规范润色批次预算限制；排查时保持 `LLM_AUTO_CORRECT=false`。

每条事件包含 UTC 时间；批次线程继承任务 ID，HTTP 事件关联 `batch`、`batches` 和 `blocks`。字符数用于比较输入规模，不冒充精确 token 数。日志不包含 Key、URL、请求头、Prompt、原始数据、模型输出或底层异常消息。

- `http_end.stage=await_headers`：等待响应头期间失败，包括 DNS、连接、TLS、网关等待及模型首响应；不能仅凭它断言模型已经开始计算。
- `stage=read_body`：已经收到响应头，读取正文失败或超时。
- `stage=decode_response`：完整 HTTP 响应已读完，解析或响应封装有问题。
- `cause_type=TimeoutError`：超时不自动重试；`status=401/403` 优先检查模型授权；`429/503` 优先检查网关容量。
- `oversized=true`：单个合并审核区块或固定提示词已超过预算。调整方案分组/输入范围，或结合网关能力评估等待阈值，继续减小预算不会拆开该区块。

`LLM_TIMEOUT` 应按真实模型耗时设置，不要求固定为 60 秒。需要进一步隔离问题时，可暂设 `LLM_MAX_RETRIES=0`、`LLM_MAX_BATCH_CHARS=6000`、`LLM_MAX_CONCURRENCY=1`，比较长请求、重复请求和并发竞争；这些仅是诊断起点，已稳定的部署无需照抄，也不保证适用于所有网关。每份报告可并发 1 批不代表全站只有 1 个上游请求：`TASK_MAX_CONCURRENCY` 仍限制并行任务数量。观察成功请求耗时和网关限制后，再逐项调整并发或等待阈值；不要同时修改全部参数后直接宣称根因已修复。

现有测速脚本仅生成合成数据，可复用真实解析、LLM、DOCX 渲染/校验流程。先进行无需 API 的原文基线：

```bash
python scripts/benchmark_timing.py --mode original --rows 7 --output /tmp/original-timing.json
```

在配置公司 API 的机器上，依次执行下列对比；每条命令都可能产生费用，上一项失败时先看日志，避免重复整组执行。`--allow-external` 明确允许将**合成数据**交给所配置的 API，脚本不会读取真实业务数据：

```bash
# 少量区块
python scripts/benchmark_timing.py --real --allow-external --env-file .env \
  --rows 1 --max-concurrency 1 --output /tmp/llm-small.json
# 同一份 12 行合成输入：较大批次、单并发
python scripts/benchmark_timing.py --real --allow-external --env-file .env \
  --rows 12 --max-batch-chars 12000 --max-concurrency 1 --output /tmp/llm-large.json
# 同样输入：缩小批次、单并发；以输出的 llm_batches 为准
python scripts/benchmark_timing.py --real --allow-external --env-file .env \
  --rows 12 --max-batch-chars 4000 --max-concurrency 1 --output /tmp/llm-split.json
# 仅前项稳定后比较双并发
python scripts/benchmark_timing.py --real --allow-external --env-file .env \
  --rows 12 --max-batch-chars 4000 --max-concurrency 2 --output /tmp/llm-parallel.json
```

以上 JSON 只保存计时、数量和错误类型，不保存输入输出正文。模型首次生成可能被事实校验回退；`success=true` 表示链路和 DOCX 校验完成，`validation_counts` 还需判断候选是否被采用。

systemd 部署更新代码及 `.env` 后，使用现有服务重启，不修改服务架构：

```bash
sudo systemctl restart sheetweaveagent.service
journalctl -u sheetweaveagent.service --since "10 minutes ago" -o cat
```

提交一份获准处理的报告，确认 `worker_enter → prepare_ir → llm_plan → http_start/http_end → response_parse → fact_validation → docx_render_validate → task_result(state=succeeded)`；失败时保留同一任务的脱敏诊断行。上线验收应以部署机真实请求的实际结果为准，不能用短请求或本地模拟网关的成功代替。
