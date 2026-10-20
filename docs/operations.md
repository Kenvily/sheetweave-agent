# SheetWeaveAgent 运维说明

单终端统一启动及可选分开管理方式见 [README](../README.md#部署单终端启动)。本文说明排查和数据边界，架构见 [项目介绍](project-overview.md)。

## 任务一直排队

`POST /tasks → 202` 只表示入队；`GET /tasks/... → 200` 只表示状态接口正常。Gunicorn 的 `Booting worker` 是 Web 子进程，不代表独立任务进程已启动。

先检查后台进程并查看其终端/进程管理器日志：

```bash
pgrep -af 'sheetweave_agent.task_worker'
```

没有进程时，若使用统一入口，检查其退出错误后重新启动；若分开管理，启动 `python -m sheetweave_agent.task_worker`，已有任务会自动领取。已有进程却不处理时，确认两端使用同一 Python、项目安装和 `.env`。分开管理时，在两个终端各执行以下检查；统一入口自动为两端传入相同环境。检查不输出密钥、不创建数据库：

```bash
which python
pwd
python - <<'PY'
import os
import tempfile
from pathlib import Path
import sheetweave_agent.task_queue as module

root = Path(module.__file__).resolve().parents[1]
print("项目:", root)
for name, default in {
    "TASK_DB_PATH": root / "var/tasks.sqlite3",
    "TASK_FILES_DIR": root / "var/tasks",
    "WEEKLY_REPORT_SNAPSHOT_DIR": Path(tempfile.gettempdir()) / "weekly-report-agent-snapshots",
}.items():
    print(name, Path(os.environ.get(name, "").strip() or default).resolve())
PY
```

路径必须一致且可写；连接不同数据库的 Worker 会认为队列为空。也应确认后台并发是否已被运行任务占满。模块不存在时先同步当前代码，再 `python -m pip install -e .`；重装旧代码不会获得新模块。

## 任务状态与恢复

`queued → running → finalizing → succeeded`，也可进入 `failed` 或 `cancelled`。接口区分 `queue_seconds` 与 `run_seconds`，后台执行时间不全是 API 时间。

- 原子领取和短事务限制同配置 Worker 的全局任务并发，不在 LLM 等待期间持有 SQLite 写锁。
- 默认 60 秒租约、约 5 秒续租；状态查询及 Worker 周期检测中断，不自动重放可能计费的调用。
- 取消排队任务可阻止执行；运行任务在后续回调停止。上游读取/PDF 子进程未结束时仍保留并发容量，发布阶段不接受取消。
- 重生成与 PDF 在执行前及发布时校验 revision。快照与任务数据库没有跨存储事务；发布时崩溃可能出现状态失败但会话已更新，需刷新核对。
- 短轮询网络重试不重复提交；同页刷新可恢复。关闭标签页不取消任务，但没有账户或历史任务列表，不能保证重新找回入口。
- 排队超过两小时不再执行；终态任务在结束两小时后由 Worker 清理。Web 与 Worker 都需保持运行；统一入口协调启动/退出，未配置自动重启/开机自启。

队列事件最多保存 16 MiB，轮询按游标每次读取最多 100 条。超过事件上限仅停止保存更多临时候选分片，不影响完整校验与导出。未成功入队前进程崩溃可能留下无记录上传目录，需核对后处理，不能清空全部 `var/`。

## 运行数据

| 路径 | 用途与保留方式 |
|---|---|
| `var/stats.sqlite3`、`.key` | 匿名聚合统计及去重密钥；一起保留/备份，删除会丢失统计 |
| `var/tasks.sqlite3` | 持久队列、状态、短期候选事件与结果引用；包含短期业务内容 |
| `var/profiles.sqlite3` | 长期方案索引、版本、最近实际提交/成功时间与使用次数，不自动过期 |
| `var/profiles/<id>/<version>/` | 方案规则、确认依据与模板的不可变版本，需连同索引备份 |
| 快照目录同级 `*-adaptations/` | 初次适配用的私有表格/模板样例与草稿，两小时有效，新分析时清理过期草稿 |
| `var/tasks/<id>/` | 私有输入和模板，按任务保留策略清理 |
| 快照目录 | 默认系统临时目录，也可设为 `var/snapshots/`；两小时审核会话及 DOCX/PDF |
| `var/logs/`、`var/run/` | 部署方式按需创建的日志/PID；PID 文件不保证进程仍存在 |
| `*-wal`、`*-shm` | SQLite 自身管理的日志/共享索引，运行中不手动删除 |
| `.stats-migration.lock` | 旧统计库迁移的文件锁，文件存在不代表仍在迁移 |
| `outputs/` | 按需生成的报告或临时测速产物，不是任务数据库 |

运行数据、密钥、候选内容和输出不提交 Git。在线备份 SQLite 使用 backup API；直接复制主库可能遗漏 WAL。旧统计库只在新默认库不存在时复制，旧文件不删除、不持续同步。

## 方案维护

运行 `python scripts/manage_profiles.py` 进入循环菜单，可查询、搜索、按未使用天数筛选、查看详情、重命名和确认删除；操作后返回菜单，输入 `0` 退出。原有一次性子命令仍可使用。查询与确认删除命令见 [README](../README.md#m6适配并复用报告方案)。`--unused-days` 仅筛选；没有按时间自动删除方案的机制，服务停止也不改变保留策略。浏览器进入页面不更新使用时间，任务成功入队/CLI 实际提交才记录最近提交时间，成功完成另记成功时间。

Web 与 Worker 使用相同的 `REPORT_PROFILES_DIR`（默认 `var/profiles/`），索引为该目录同级同名 `.sqlite3`。脚本默认安全加载项目 `.env`；可指定 `--env-file`，或以 `--no-env` 使用当前终端环境。无账户隔离，方案供服务器共享。

删除作用于一个自定义方案的全部版本与模板，需要输入完整 ID；存在 queued/running/finalizing 或仍有执行权的任务引用时拒绝操作。任务/会话已有独立副本，不依赖方案目录的后续存续。内置规则不属于此库。

备份应在停止方案写入后同时保留索引与版本目录，或由管理员在持有方案文件锁期间协调 SQLite backup API 和目录备份。不要仅复制数据库而漏掉模板。崩溃可能留下未完成版本目录或 `.deleted-*` 临时目录，先检查数据库与任务引用再人工恢复/清理；程序不会覆盖未知文件。

## 网络访问与 PDF

同机或 SSH 转发使用 `127.0.0.1:18989`。远程 Linux 可在浏览器机器执行：

```bash
ssh -N -L 18989:127.0.0.1:18989 用户@服务器
```

然后访问本机 `http://127.0.0.1:18989`。局域网直连使用 Web 绑定 `0.0.0.0:18989`，网络放行遵循服务器已有规则，不自动改防火墙。

WSL2 的 localhost 访问与局域网访问不同。若使用 Windows portproxy，转发目标为当前 WSL IP，监听地址为当前 Windows LAN IP；重启或切换网络后需更新。用 `wsl.exe hostname -I`、`ipconfig` 和 `netsh interface portproxy show v4tov4` 核对实际地址。调整时只修改本项目 18989 规则，不用清空全部转发规则的 reset 命令。Web 与任务 Worker 两端都要运行，Windows 休眠可能中断服务。

PDF 是可选功能，依赖已有 LibreOffice。缺少转换器不影响 HTML 或 DOCX；需另行验收字体与分页，不自动安装系统软件。
