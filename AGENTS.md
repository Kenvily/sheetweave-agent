# Repository Guidelines

## Project Structure & Module Organization

This repository is the starting point for a weekly-report generation agent. Current reference assets live in `data/`:

- `data/周报.csv`: raw project and weekly progress records with multi-row headers.
- `data/研发三部周报模板.docx`: formatting rules and an example target document.

Keep these files unchanged as reproducible fixtures. Place application code in `src/weekly_report_agent/`, tests in `tests/`, generated reports in `output/`, and developer scripts in `scripts/`. Do not commit generated `.docx` files unless they are intentional test fixtures.

## Architecture and Data Rules

Separate CSV parsing, content normalization, report planning, DOCX rendering, and validation into distinct modules. Preserve source facts—especially dates, model versions, metrics, and conclusions. The renderer must follow the template's heading hierarchy beginning at section `9.` and its font, spacing, and indentation rules. Replace informal names such as “XX哥/XX姐” with formal role descriptions, and convert relative weekdays to concrete dates when enough context exists.

## Build, Test, and Development Commands

The project targets Python 3.11+ and uses pinned development dependencies. Use these commands:

```bash
conda activate py31115
python -m pip install -r requirements.txt
python -m pytest
ruff check .
ruff format --check .
python -m weekly_report_agent --input data/周报.csv --template data/研发三部周报模板.docx
```

## Coding Style & Naming Conventions

Target Python 3.11+. Use four-space indentation, UTF-8, type hints, and concise docstrings for public APIs. Use `snake_case` for modules and functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Prefer `pathlib.Path` over string-built paths. Configure Ruff for linting/formatting and keep parsing logic deterministic; isolate LLM calls behind an interface.

## Testing Guidelines

Use `pytest`; name files `test_*.py` and tests `test_<behavior>()`. Cover multi-row CSV headers, empty rows, week selection, terminology normalization, hierarchy numbering, and DOCX styles. Add regression fixtures for malformed input and verify that numeric metrics are never rewritten. Run the full suite before opening a pull request.

## Commit & Pull Request Guidelines

There is no Git history from which to infer conventions. Use short imperative Conventional Commit messages, for example `feat: parse multi-row weekly headers`. Pull requests should describe the input/output behavior, list validation commands, link relevant issues, and include a sample or screenshot when DOCX formatting changes. Never include credentials, customer-sensitive data, or unredacted production reports.
