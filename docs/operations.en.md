<h1><img src="../src/static/sheetweave-mark.svg" alt="" width="40" height="40" align="absmiddle"> SheetWeaveAgent User Guide </h1>

[简体中文](operations.md) · **English** · [Project overview](../README.en.md) · [Architecture](project-design.en.md)

## Install and start

Run from the project root in an activated Python 3.11+ environment:

```bash
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
python -m sheetweave_agent.server --host 127.0.0.1 --port 18989
```

The launcher loads the project's `.env` and starts the Web server and a separate task worker. Open `http://127.0.0.1:18989`. Press Ctrl+C to stop both. After moving the checkout, reinstall with `python -m pip install -e .`.

For remote access over SSH, use `ssh -N -L 18989:127.0.0.1:18989 user@server`. For direct access within an approved network, use `--host 0.0.0.0` and handle authentication, TLS, and network access at the deployment layer. Profiles are shared on the server.

### Start Web and worker separately

Use the same Python environment, configuration, and storage paths in both terminals. If using a trusted shell-compatible `.env`, load and export it in each terminal with `set -a`, `source .env`, and `set +a`.

```bash
# Web terminal
gunicorn --config gunicorn.conf.py --bind 127.0.0.1:18989 'sheetweave_agent.web:create_app()'

# Worker terminal
python -m sheetweave_agent.task_worker
```

For an existing systemd service, use `sudo systemctl restart sheetweaveagent.service`. Reload systemd with `sudo systemctl daemon-reload` after changing a unit or drop-in.

## Model settings

Set `LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL`, and `LLM_ALLOWED_MODELS` for your authorized OpenAI-compatible Chat Completions provider. The base URL ends before `/chat/completions`. Include the configured model in the allowed list. Keep the key local and send only approved data. Original-text generation with a saved profile can run offline.

| Setting | Code default | Effect |
|---|---|---|
| `LLM_API_BASE` | `https://api.deepseek.com` | Base URL of your provider |
| `LLM_MODEL` | `deepseek-chat` | Default model |
| `LLM_ALLOWED_MODELS` | `deepseek-chat,deepseek-reasoner` | Models offered in the Web UI |
| `LLM_TIMEOUT` | 60 seconds | Connection and blocking-read timeout per API attempt |
| `LLM_MAX_RETRIES` | 1 | Extra attempts for eligible HTTP and connection failures. Timeouts, interrupted bodies, and parse failures are excluded |
| `LLM_MAX_BATCH_CHARS` | 12000 | Full-message character budget for each initial editing batch. An individual block is kept intact and can exceed the budget |
| `LLM_MAX_CONCURRENCY` | 2 | Parallel batches per report, from 1 to 4 |
| `LLM_AUTO_CORRECT` | `false` | Allows extra calls to repair fact conflicts |
| `LLM_REASONING_EFFORT` | Unset | Sent only when configured. Use a provider-supported value |

The code defaults apply to unset variables. Legacy `DEEPSEEK_*` values apply only where the matching `LLM_*` variable is unset. See `.env.example` for example settings.

### Timeouts and concurrency

An API attempt starts after the background job is running and the worker has prepared its batch. Queue time, data preparation, and DOCX rendering sit outside `LLM_TIMEOUT`. Measure representative reports before increasing the timeout for slow reasoning models. For example, `LLM_TIMEOUT=300` allows a longer blocking wait. Restart Web and worker to load changes.

Gunicorn's `timeout=180` monitors Web worker liveness. With `gthread`, it does not impose a deadline on individual background model calls. `graceful_timeout=180` controls Web worker shutdown. The unified launcher waits up to 190 seconds during shutdown, and systemd can impose a shorter wait. Let active jobs finish before a planned restart.

Gunicorn is configured for two Web processes and two request threads per process. The unified launcher passes its own `--threads` value, also two by default, and overrides the process count only when passed `--workers N`. Background capacity is separate. With `TASK_MAX_CONCURRENCY=2` and `LLM_MAX_CONCURRENCY=2`, two multi-batch reports can make up to four concurrent initial editing requests. Waiting for a model still occupies a background job slot.

## Create and reuse a profile

1. Open **Profile adaptation (方案适配)**. Upload a permitted CSV/XLSX sample and an optional reference DOCX.
2. Select the worksheet. Add fixed headings or requirements as needed, then confirm authorization before running model-assisted recognition.
3. Check the inferred field meanings, especially **Primary body (主要正文)**. Review the fields that populate each section and the row filters. Other mapped fields may drive headings, filtering, or model context.
4. Review headings, starting section, fonts, spacing, and list styles. Use **Validate sample (验证样例)** and inspect the DOCX before saving.
5. Choose the saved profile in **New document (新建文档)** for subsequent files.

New files may contain more rows than the sample. Dynamic headings can use new field values. For fixed headings, **Accept new projects (接纳新增项目)** can route unmatched rows to one existing section or a separate unclassified section. Review flags these assignments. Required fields and status filters still apply.

| Item | Behavior on a new upload |
|---|---|
| Worksheet | Select it manually. If the saved default name is absent, the first worksheet becomes the default |
| Weekly matrix | Month labels come from row one and week labels from row two. Select a week or use the latest valid week |
| Ordinary table | Uses the saved header row count |
| Body mapping | Period-based mappings follow the chosen week. Fixed mappings keep reading their configured columns |
| Saved `expected_headers` | Ordinary tables require matching normalized headers and order. Recognized period columns can change in weekly matrices, while fixed fields still undergo checks |

An explicitly selected missing worksheet or week produces an error. Changes to fixed fields or layout may require adapting the profile again. Exported rule JSON serves as reference. Set up a deployment by creating profiles or migrating the saved profile database and version directory.

## Generate, review, and export

1. In **New document (新建文档)**, select the file, saved profile, worksheet, and reporting period where applicable.
2. Choose **Keep original wording (原文保留)** for local normalization or **Polish and restructure (规范润色)** for model editing with optional extra instructions.
3. Submit and follow status updates. To stop the job, use the cancel control.
4. Select a block to review its source, current text, and preview. Inspect any model candidate and inline diff before adopting it. Manual edits take effect after confirmation.
5. Download the current DOCX. If another edit changed the revision, reload before submitting. Optional PDF preview requires LibreOffice and suitable fonts. `LIBREOFFICE_PATH` is auto-detected by default and `PDF_PREVIEW_TIMEOUT` defaults to 45 seconds. Check final pagination in Word or PDF.

Embedded PNG/JPEG images follow their source rows and are retained in DOCX and PDF. The upper-left anchor determines the row. Weekly matrices read images from the selected period and non-period columns. Original-text mode keeps images with their respective rows inside grouped blocks. Model or manual edits retain them at the end of the block. Images stay local. Limits are 8 MiB per image, 24 MiB per selected worksheet, and 100 images. Use ordinary embedded PNG/JPEG for workbooks with unsupported external links or special in-cell images.

### CLI generation

The CLI requires a saved profile. Export settings in the shell if needed.

```bash
python -m sheetweave_agent --profile p_PROFILE_ID \
  --input records.xlsx --sheet Records --report-date 2026-10-29 \
  --content-mode original --output outputs/report.docx
```

For a matrix add a week selector such as `--week '第4周'`. For model editing use `--content-mode polish --allow-external` in place of original mode.

## Storage and administration

| Setting | Default | Purpose |
|---|---|---|
| `TASK_MAX_CONCURRENCY` | 2 | Concurrent background tasks in the shared database, from 1 to 16 |
| `TASK_QUEUE_CAPACITY` | 100 | Admission capacity for queued and running jobs |
| `REPORT_SESSION_TTL` | `2h` | New review and download sessions. Supports inputs such as `24h`, `3d`, and `1w` |
| `WEEKLY_REPORT_SNAPSHOT_DIR` | System temp directory under `weekly-report-agent-snapshots/` | Shared review snapshot directory |
| `REPORT_PROFILES_DIR` | `var/profiles/` | Persistent profile versions, with the profile database in the parent directory |
| `TASK_DB_PATH` / `TASK_FILES_DIR` | `var/tasks.sqlite3` / `var/tasks/` | Task state and input attachments |
| `STATS_DB_PATH` | `var/stats.sqlite3` | Aggregate usage statistics |
| `STATS_HMAC_KEY` | Auto-generated `.key` file | Shared stable value, at least 16 bytes when set explicitly |
| `REPORT_TIMEZONE` | `Asia/Shanghai` | Display time zone for statistics and session expiration |

Review sessions expire from creation time. Editing and refreshing leave the expiry time unchanged. New TTL values apply to new sessions after restart, with `REPORT_SESSION_TTL_SECONDS` supported when the new setting is unset. Later application activity cleans up expired files. The OS can also clear temporary storage. Profiles persist until removed. Adaptation drafts, queued jobs, and terminal records have separate two-hour rules.

```bash
python scripts/manage_profiles.py
python scripts/manage_profiles.py list --unused-days 90
python scripts/manage_profiles.py show p_PROFILE_ID
python scripts/manage_profiles.py rename p_PROFILE_ID "New name"
python scripts/manage_profiles.py delete p_PROFILE_ID
```

The menu supports lookup, search, rename, and deletion. Deletion requires the full ID and is blocked when active tasks reference the profile. The script reads the project `.env` by default. Specify `--env-file /path/to/config.env` or `--no-env` before the subcommand if needed.

Back up both `var/profiles.sqlite3` and the full `var/profiles/` tree. Use SQLite's backup API or stop services for a consistent filesystem copy. Preserve the statistics key with its database and leave live WAL and SHM sidecars in place.

## Troubleshooting

| Symptom | Check |
|---|---|
| Stuck in queue | Run `pgrep -af 'sheetweave_agent.task_worker'` and verify shared paths for Web and worker |
| `await_headers` timeout | Check connectivity, gateway queueing, and time to first response |
| `read_body` timeout | Check for stalled reads after response headers |
| `response_parse` failure | Check whether model output conforms to the response contract |
| No usable rows | Check worksheet, period, mapped fields, filters, and status rules |
| Stale revision or expired session | Reload the latest revision, or regenerate if expired |

```bash
python scripts/diagnose_llm.py --worker-pid WORKER_PID --expect-model MODEL
python scripts/diagnose_llm.py --worker-pid WORKER_PID --expect-model MODEL \
  --cases basic --max-requests 1 --execute
```

The first command previews the request plan without sending it. To compare prompt formats use `--cases report compact`. Use `--transport stream` or `both` to compare transport behavior. Each run targets one selected batch. All three cases with both transports require `--max-requests 6`. Defaults are a 15-second socket timeout, a 20-second deadline per case, and a 60-second run budget. Each is capped at 180 seconds. Testing a real file requires `--input`, `--profile`, and `--allow-input-upload`. A client deadline can leave an upstream request running, so avoid immediate repeats.

To collect an original-mode baseline, run `python scripts/benchmark_timing.py --mode original --rows 7 --output /tmp/report-timing.json`. With systemd, use `journalctl -u sheetweaveagent.service --since "10 minutes ago" -o cat` and follow the same task ID. Share timings and error types without credentials, prompts, or source content.

Use spreadsheets with clear headers and reference DOCX files built primarily from headings, paragraphs, and lists. Check complex layouts such as tables or text boxes separately.
