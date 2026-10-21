# SheetWeaveAgent

将 Excel / CSV 整理成符合方案要求的 Word 文档。支持字段与模板适配、原文保留或 LLM 润色、来源核对、人工编辑、原生多级编号及可选 PDF 预览。

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

## 使用

1. 在“方案适配”上传脱敏样例及可选 DOCX，核对识别结果，验证并保存方案。
2. 在“新建周报”选择方案、数据文件和内容模式。原文保留无需模型 Key；规范润色支持补充要求。
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
