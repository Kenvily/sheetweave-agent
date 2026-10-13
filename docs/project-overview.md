# docAgent 项目架构

安装、两个服务的启动命令和使用流程见 [README](../README.md)，部署排查见 [运维说明](operations.md)，计划见 [ROADMAP](../ROADMAP.md)。本文只描述当前代码。

## 目录与模块

源码直接位于 `src/`，setuptools 将其映射为逻辑包 `weekly_report_agent`；不是 `src.weekly_report_agent`。`weekly_report_agent.egg-info/` 为安装元数据，不是业务代码。

| 文件/目录 | 职责 |
|---|---|
| `src/__main__.py`、`cli.py` | CLI 入口与参数，调用同步生成流程 |
| `web.py` | Flask 工厂、上传、任务接口、审核、帮助与下载 |
| `task_queue.py` | SQLite 队列、原子领取、共享并发上限、租约、事件、取消与清理 |
| `task_worker.py` | 独立后台进程，执行生成、局部重生成和 PDF |
| `generation.py` | 与 Flask 请求上下文无关的完整生成和快照发布 |
| `service.py` | 解析/处理/渲染编排、人工编辑、版本采用及规范化计划 |
| `input_parser.py`、`csv_parser.py` | CSV/XLSX、表头、Sheet/周次、字段映射、物理行号；原 CSV 接口仍受支持 |
| `rules/` | `rd3`、`general` JSON 预设与配置校验 |
| `models.py`、`content_builder.py` | 来源记录、内容块、报告 IR、分组与双向来源关系 |
| `normalizer.py`、`planner.py` | 术语/称呼/相对日期、标点、段落层级与编号 |
| `llm/config.py`、`client.py` | 服务端配置、模型允许列表、阻塞 HTTP 与上游流式读取 |
| `llm/service.py` | 模式契约、批次路由、JSON 对齐、验证、可选修正、单块重生成 |
| `llm/facts.py`、`validation.py` | 事实提取/别名归一化与分级冲突检查 |
| `docx_renderer.py`、`docx_security.py`、`validator.py` | ZIP/XML 渲染、包安全、编号/字体/段落/页面验收 |
| `web_sessions.py`、`pdf_preview.py` | 随机令牌快照、文件锁、revision 与可选 LibreOffice 转换 |
| `stats.py`、`timing.py` | 匿名统计及可选无正文阶段计时 |
| `templates/`、`static/` | Jinja2 页面、样式、短轮询客户端、三栏定位/缩放；旧 SSE 客户端兼容资源 |
| `web_stream.py` | 旧 SSE 路径的进程内事件队列与协作式取消 |
| `examples/` | 非敏感 CSV/XLSX 示例 |
| `tests/`、`scripts/` | 功能回归与合成测速工具 |
| `data/` | 原 CSV/DOCX 参考夹具，保持不变 |
| `var/`、`outputs/` | 私有运行状态、按需生成的报告；都忽略 Git |

## 生成流水线

```text
CSV/XLSX
  → 预设、工作表、周次和字段映射
  → ParsedInput：原始值、物理位置、字段和告警
  → PreparedReport / ContentBlock：可追溯 IR
  → 原文保留 或 LLM 候选生成
  → 结构与事实校验、候选采用或回退
  → 规范化、标点和编号 → ReportPlan
  → 标准库 OOXML 渲染 → 文档校验 → 审核快照
```

来源原文、模型候选、人工编辑分别保存；规范化不会覆盖原始事实。中栏当前内容、HTML 正文与 DOCX 使用同一正文规划。术语替换在 `normalizer.py`，事实别名在 `llm/facts.py`，目前没有独立统一词表。

`rd3` 使用多行表头与周次矩阵，章节默认 9；`general` 使用单行记录，章节默认 1。历史完成规则只依据状态列和此前周记录，保留事项但正文标记“已完成”并跳过 LLM；正文出现“完成”不等同于状态已完成。

## Harness 如何约束模型

1. 挑选可编辑块，项目、任务、日期、结构状态作为只读上下文；历史完成块跳过模型。
2. 按模式契约和长度软目标构造批次，发送稳定块 ID、可编辑文本及受保护事实。
3. 解析完整 JSON，检查 ID 是否重复、遗漏或未知；无效结构使任务失败，不交付半成品。
4. 对数字、指标、百分比/百分点、模型版本、日期、技术名和任务状态做归一化检查，分为通过、待确认、谨慎使用。
5. 通过候选自动采用；其余保留候选并默认回退原文。可选一轮定向修正默认关闭，用户可在核对告警后明确采用。

事实检查依赖规则与启发式，不保证完整语义正确。人工编辑不执行模型事实检查，经独立修改前后 Diff 确认后生效；页面常驻 Diff 比较来源原文与当前有效内容。模型候选另有原文/候选行内差异，未采用候选不会替换编辑基准。

## Web 与后台进程

```text
浏览器 POST /tasks → Web 保存私有输入并返回 HTTP 202 → 释放请求线程
                            |
                    SQLite 持久队列与状态
                            |
                   独立 task_worker 进程
                   默认最多同时执行 2 任务
                            |
                   生成 / 重生成 / PDF
                            |
                 更新结果 → 审核 / 下载地址

浏览器短轮询 GET /tasks/<id>?after=<cursor>，每次响应后释放请求线程
```

Gunicorn 的 Web Worker 与任务 Worker 是不同进程。默认 Web 配置为两个 gthread 子进程，每进程请求线程池上限二，线程按需创建。后台任务线程及单任务 LLM 批次线程属于独立后台进程。

首页、局部重生成和 PDF 按钮走队列；CLI、旧同步与 SSE 路由保留兼容，直接调用仍可能占用 Web 请求容量。匿名统计数据库不承担任务状态存储。

## 审核、文档与界面

来源、内容块和 HTML 条目支持三向定位；缩放和专注模式只影响显示，阅读偏好留在浏览器，不改变 DOCX。共享导航、可见焦点、减少动态效果和移动端布局使用原生 HTML/CSS/JavaScript，无前端框架或字体 CDN。

审核会话使用随机令牌、私有文件、文件锁与 revision。旧任务/旧标签页不能覆盖新版本；编辑后旧 PDF 失效。PDF 按需生成，不可用时仍可下载 DOCX。Linux 字体和 Word/PDF 分页一致性需单独验收。

渲染器使用 `zipfile` 与 `ElementTree`，复制模板部件并重建正文，确定性设置字体、字号、段间距、行距及缩进；没有引入 `python-docx`，不保证任意复杂模板兼容。

## 测试组织

测试按功能命名，不再使用阶段编号。`test_input_parser.py` 合并原 CSV 与通用 CSV/XLSX 输入测试；其他测试覆盖规则/来源、历史完成、规划、LLM、Web、审核会话、文档/PDF、统计、队列、流式兼容和计时。

`test_historical_completion.py` 的“历史”指多周输入中的已完成事项，是当前功能回归，不是迁移历史数据。参考夹具受保护；新输入多数由测试临时生成。当前 152 项回归覆盖保持不变。
