<h1 align="center"><img src="src/static/sheetweave-mark.svg" alt="" width="40" height="40" align="absmiddle"> SheetWeaveAgent</h1>

<h3 align="center">Turn business spreadsheets into reviewable Word documents.</h3>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong> · <a href="docs/operations.en.md">User guide</a> · <a href="docs/project-design.en.md">Architecture</a></p>

SheetWeaveAgent turns Excel and CSV records into editable Word documents that follow your team's content and formatting rules. Set up a profile once, reuse it with each new spreadsheet, and review the generated text against its sources before downloading the DOCX.

| Use case | Inputs | Deliverable |
|---|---|---|
| Team progress | Project records, status, and periodic updates | Progress report grouped by project |
| Project delivery | Implementation notes, acceptance checks, and open issues | Milestone delivery report |
| Model evaluation | Experiment settings, metrics, and limitations | Evaluation report with traceable evidence |
| Remediation tracking | Issues, corrective actions, and verification records | Report organized by issue and status |

> In one department's workflow, preparing and manually checking a rules-compliant document used to take about 20 minutes. Experienced users now complete the same workflow within 2 minutes, saving roughly 90% of that time. First-time setup and model latency vary by task.

## How it works

1. **Define a profile.** Map spreadsheet fields, configure row filters and grouping, and set Word styles. Model-assisted setup can propose rules from a sample spreadsheet and reference DOCX for you to confirm.
2. **Generate from a new file.** Select a worksheet and, where applicable, a reporting period. Keep the original wording with local normalization, or use an LLM to polish and reorganize the selected text.
3. **Review and deliver.** Inspect source rows, current text, and the document preview side by side. Compare model candidates using inline diffs, confirm edits, and export an editable DOCX with native Word headings and lists.

The generation harness checks key facts such as dates, metrics, and versions. Significant conflicts preserve the candidate for review and default to the original-text path. Deterministic code renders and checks Word formatting. SQLite-backed jobs move long model calls out of the Web request that submitted them.

**Stack** · Python 3.11+ · Flask · Gunicorn · SQLite · openpyxl · OpenAI-compatible Chat Completions · OOXML

## Quick start

Run from the repository root in an activated Python 3.11+ environment:

```bash
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
python -m sheetweave_agent.server --host 127.0.0.1 --port 18989
```

Open `http://127.0.0.1:18989`. The [English user guide](docs/operations.en.md) includes the Chinese UI labels. Create or migrate a profile before generating a document. Model-assisted setup and editing require an authorized API in `.env`. With a saved profile, original-text generation runs locally.

## Explore the project

- [User guide](docs/operations.en.md) · Setup, profile management, deployment, and diagnostics.
- [Architecture and design](docs/project-design.en.md) · Intermediate representation, LLM harness, and document rendering.
- [Project showcase (Chinese)](assets/showcase/SheetWeaveAgent-项目展示.pdf) · [Review workspace and model diff](assets/showcase/pdfs/04a-model-result-and-diff.pdf).

## Development checks

```bash
python -m pytest
ruff check .
ruff format --check .
```
