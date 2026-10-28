# SheetWeaveAgent

**将结构化业务数据，按照企业模板和内容规则，生成可追溯、可审核的正式文档。**

SheetWeaveAgent 面向 Excel / CSV 到 Word 的企业文档生产：通过可复用方案确认字段含义、内容目录和文档样式，由确定性规则约束事实与格式，LLM 可选整理语义，最终由人工核对与确认。

支持字段与模板辅助适配、跨行内容整合、原文保留或规范润色、来源追溯、候选差异审核、人工编辑、Word 原生多级编号及可选 PDF 预览。可用于项目进展、研发记录、评估记录和交付台账等数据整理为报告初稿；业务结论来自输入，不自动计算复杂指标或作专业判断。

## 安装与启动

Python 3.11+，已有 Conda 环境可直接使用，不覆盖其他项目的环境。

```bash
conda activate py31115
cd ~/SheetWeaveAgent                  # 改为实际项目路径
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
python -m sheetweave_agent.server --host 127.0.0.1 --port 18989
```

访问 `http://127.0.0.1:18989`。统一入口读取 `.env`，同时启动 Web 和独立任务 Worker；Ctrl+C 协调关闭两者。远程访问、分开启动及完整配置见 [项目与运维说明](docs/operations.md)。

Web 进程数在 `gunicorn.conf.py` 的 `workers` 中设置，当前为 `2`，修改后重启服务生效。统一入口仅在显式传入 `--workers N` 时覆盖该值；后台任务并发仍由 `.env` 的 `TASK_MAX_CONCURRENCY` 控制。

## 部署时检查超时

`LLM_TIMEOUT` 在 `.env` 中设置，单位为秒，默认 `60`。公司 API 或推理模型处理真实报告可能超过一分钟：短测试成功、正式报告超时时，应结合实际生成耗时调整，例如 `LLM_TIMEOUT=300`，并重启 Web 和后台 Worker。这是等待阈值，不会让模型生成更快。

同时检查 `gunicorn.conf.py`：当前 `timeout=180` 是 Web Worker 无响应/心跳超时，`graceful_timeout=180` 是优雅关闭等待时间。当前使用 `gthread`，生成任务由独立 Worker 执行，**Gunicorn 的 `timeout` 不限制后台模型调用，也不要求机械地大于 `LLM_TIMEOUT`**。同步入口、改用 `sync` Worker 或重启服务时需另外核对相关时限；统一启动器关闭等待上限为 190 秒。

两处配置的作用、调整与重启注意事项见[超时配置说明](docs/operations.md#模型等待与-web-worker-超时)。

## 使用

1. 在“方案适配”上传脱敏样例及可选 DOCX，核对识别结果，验证并保存方案。
2. 在“新建文档”选择方案、数据文件和内容模式，按当前文件选择工作表及周次。旧默认工作表不存在时可重新选择，周次矩阵允许月份/周次列变化；固定业务表头仍需符合方案。原文保留无需模型 Key；规范润色支持补充要求。
3. 核对来源与当前内容，按需编辑，下载 DOCX。PDF 预览需要服务器已有 LibreOffice。

方案长期保存；审核会话默认保留两小时，可在 `.env` 设置：

```dotenv
REPORT_SESSION_TTL=24h
```

支持小时、天、周，例如 `24h`、`3d`、`1w`，也支持 `24小时`、`3天`、`1周`。修改后重启 Web 和 Worker，对新会话生效；已创建会话沿用原到期时间。

CLI 必须使用已保存方案：

```bash
python -m sheetweave_agent --profile p_实际方案ID --input 数据.xlsx --output outputs/报告.docx
```

## 方案管理

在项目根目录、激活项目环境后运行：

```bash
python scripts/manage_profiles.py
```

循环菜单支持查询全部、名称/ID 搜索、筛选长期未使用、查看详情、删除及重命名。输入序号选择操作；查看结果后按回车，自动清屏返回菜单，输入 `0` 退出。删除需输入完整方案 ID 确认，正在被任务使用的方案不能删除。

也可直接执行子命令：

```bash
python scripts/manage_profiles.py list
python scripts/manage_profiles.py list --unused-days 90
python scripts/manage_profiles.py show p_方案ID
python scripts/manage_profiles.py rename p_方案ID "新名称"
python scripts/manage_profiles.py delete p_方案ID
```

默认读取项目 `.env`，与服务使用相同方案目录。指定配置可用 `python scripts/manage_profiles.py --env-file /路径/配置.env`；仅使用已有环境变量可加 `--no-env`。方案不会自动过期，筛选操作不会删除数据。

公司 API 短请求成功但报告超时：参见[网关诊断、合成测速与部署验证](docs/operations.md#公司网关超时诊断)。

## 开发检查

```bash
python -m pytest
ruff check .
ruff format --check .
```

核心测试覆盖解析与状态策略、语义分段与事实回退、方案复用、队列并发、审核版本、会话过期、DOCX/PDF 安全与格式。测试只使用仓库夹具和合成数据，不依赖本地业务 Excel，不实际调用外部 LLM。

项目结构、架构原理、配置默认值、数据保留与备份、部署排查统一见 [项目与运维说明](docs/operations.md)。
