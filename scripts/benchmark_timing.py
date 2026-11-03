"""Benchmark synthetic weekly reports; external calls require explicit consent."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shlex
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

from sheetweave_agent.llm import LLMConfig, LLMService, OpenAICompatibleClient, load_llm_config
from sheetweave_agent.service import prepare_report, process_report, render_prepared_report
from sheetweave_agent.timing import TimingRecorder

ROOT = Path(__file__).resolve().parents[1]


class MockClient:
    """Emulate provider wait time without network requests."""

    def __init__(self, recorder: TimingRecorder, delay: float) -> None:
        self.recorder = recorder
        self.delay = delay

    def complete(self, messages, model):
        with self.recorder.measure("http_attempt", attempt=1):
            time.sleep(self.delay)
            blocks = json.loads(messages[-1]["content"])["blocks"]
            return json.dumps(
                {
                    "blocks": [
                        {"block_id": b["block_id"], "text": b["editable_text"]} for b in blocks
                    ]
                }
            )


def load_environment(path: Path | None) -> dict[str, str]:
    """Read simple local dotenv assignments without executing shell code."""
    values = dict(os.environ)
    if path is not None:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.removeprefix("export ").partition("=")
            if not separator:
                raise ValueError("配置文件包含非赋值行。")
            parts = shlex.split(value, comments=True)
            if len(parts) > 1:
                raise ValueError("配置值包含未引用的空格。")
            values[key.strip()] = parts[0] if parts else ""
    return values


def summarize(recorder: TimingRecorder) -> dict:
    """Report wall times separately from summed concurrent request durations."""
    events = recorder.snapshot()
    totals: dict[str, float] = {}
    for event in events:
        name = str(event["stage"])
        totals[name] = round(totals.get(name, 0) + float(event["seconds"]), 6)
    attempts = [event for event in events if event["stage"] == "http_attempt"]
    return {
        "seconds": totals,
        "llm_batches": sum(event["stage"] == "llm_batch" for event in events),
        "correction_calls": sum(event["stage"] == "llm_correction" for event in events),
        "http_attempts": len(attempts),
        "http_retries": sum(int(event.get("attempt", 1)) > 1 for event in attempts),
        "failed_http_attempts": sum(not event["success"] for event in attempts),
        "events": events,
    }


def main() -> int:
    """Run at most three synthetic reports and optionally one simpler request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--rows", type=int, default=7)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--mock-delay", type=float, default=0.2)
    parser.add_argument("--max-batch-chars", type=int)
    parser.add_argument("--plain-comparison", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/timing-benchmark.json")
    parser.add_argument("--max-concurrency", type=int, choices=range(1, 5))
    parser.add_argument("--mode", choices=("original", "polish"), default="polish")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not 1 <= args.rows <= 50 or not 1 <= args.runs <= 3 or args.mock_delay < 0:
        parser.error("rows 必须为 1～50，runs 为 1～3，mock-delay 不得为负。")
    if args.real and not args.allow_external:
        parser.error("真实测速仅发送合成数据，但仍需 --allow-external 明确授权。")
    config = (
        load_llm_config(load_environment(args.env_file))
        if args.real
        else LLMConfig(api_key="mock-only")
    )
    if args.max_batch_chars is not None:
        from dataclasses import replace

        if args.max_batch_chars <= 0:
            parser.error("max-batch-chars 必须大于 0。")
        config = replace(config, max_batch_chars=args.max_batch_chars)
    if args.max_concurrency is not None:
        from dataclasses import replace

        config = replace(config, max_concurrency=args.max_concurrency)
    results = []
    with tempfile.TemporaryDirectory(prefix="sheetweave-timing-") as directory:
        work = Path(directory)
        source = work / "synthetic.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["项目", "本周工作"])
            for index in range(args.rows):
                writer.writerow(
                    [
                        f"合成项目{index + 1}",
                        "完成接口验证，检查数据导入、字段映射和结果导出。"
                        "已完成 TabPFN 3.0 模型评估，使用 3000 条训练样本，"
                        "在 1 万条验证样本上得到 AUC 0.81、KS 62.5%。"
                        "整理测试记录并复核指标计算，完成异常输入与兼容性检查。",
                    ]
                )
        for index in range(args.runs):
            recorder = TimingRecorder()
            report = {"run": index + 1, "success": False}
            try:
                with recorder.measure("total"):
                    with recorder.measure("prepare_ir"):
                        prepared, preset = prepare_report(source, preset_id="general")
                    client = None if args.real else MockClient(recorder, args.mock_delay)
                    service = LLMService(config, client, timings=recorder)
                    with recorder.measure("llm_process"):
                        prepared = process_report(prepared, args.mode, llm_service=service)
                    with recorder.measure("docx_render_validate"):
                        render_prepared_report(
                            prepared,
                            preset,
                            ROOT / "data/周报/研发三部/模板/研发三部周报模板_测试.docx",
                            work / f"report-{index}.docx",
                            date(2026, 10, 12),
                        )
                report["validation_counts"] = dict(
                    Counter(b.validation_level for b in prepared.blocks)
                )
                report["success"] = True
            except (OSError, ValueError, RuntimeError) as exc:
                report["error_type"] = type(exc).__name__
            report.update(summarize(recorder))
            results.append(report)
        comparison = None
        if args.real and args.plain_comparison:
            recorder = TimingRecorder()
            comparison = {"success": False}
            try:
                with recorder.measure("plain_llm"):
                    OpenAICompatibleClient(config, timings=recorder).complete(
                        (
                            {
                                "role": "system",
                                "content": "将合成周报规范润色，保留全部事实，"
                                '返回 JSON 对象 {"report":"整理后的完整周报"}。',
                            },
                            {"role": "user", "content": source.read_text()},
                        ),
                        config.model,
                    )
                comparison["success"] = True
            except (OSError, ValueError, RuntimeError) as exc:
                comparison["error_type"] = type(exc).__name__
            comparison.update(summarize(recorder))
    summary = {
        "backend": "real" if args.real else "mock",
        "model": config.model if args.real else "mock",
        "synthetic_rows": args.rows,
        "mode": args.mode,
        "batch_char_limit": config.max_batch_chars,
        "max_concurrency": config.max_concurrency,
        "auto_correct": config.auto_correct,
        "note": "请求耗时包含网络及服务等待，非首 token 耗时；并发请求耗时之和不等于总等待。",
        "runs": results,
        "plain_comparison": comparison,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return (
        0
        if all(run["success"] for run in results) and (comparison is None or comparison["success"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
