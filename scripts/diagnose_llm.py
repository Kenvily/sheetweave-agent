r"""Bounded LLM diagnostics (default: no network, synthetic data, no content logs).

Preview:
  python scripts/diagnose_llm.py --worker-pid PID --expect-model MODEL
Compare all six requests, using exactly the Worker's environment:
  python scripts/diagnose_llm.py --worker-pid PID --expect-model MODEL \
      --transport both --max-requests 6 --execute
Configuration-file alternative:
  python scripts/diagnose_llm.py --env-file .env --expect-model MODEL --execute
Real input requires --input FILE --profile p_ID --allow-input-upload --execute.

Cases: basic = short JSON ping; report = production Prompt for ONE selected batch;
compact = same block IDs/editable_text, short instruction, no duplicated context.
All requests use the native client, same model and provider parameters, no retries.
The compact variant never changes production behavior. Streaming records first
event line, first content and reasoning activity separately. Partial content is never
saved. This diagnoses LLM requests, not DOCX rendering or full-report validation.

To observe the Worker's full 60s wait, select fewer cases and explicitly use
--timeout 60 --deadline 65 --total-timeout 140. Default deadlines are shorter for
cost control. Timings start at case entry; first_stream_line_seconds is a complete
SSE line, not the first TCP byte. Nonstream body_complete_seconds is a full read.
Exit codes: 0 = all selected cases parsed (or preview); 1 = failure/budget skip;
2 = configuration/setup error; 130 = interrupted. Successful structure parsing
is not a facts/format validation and does not certify the candidate for export.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import logging
import math
import os
import signal
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sheetweave_agent.llm import OpenAICompatibleClient, load_llm_config
from sheetweave_agent.llm.service import ContentMode, _batches, _decode_response, _messages
from sheetweave_agent.profiles import ProfileStore
from sheetweave_agent.server import load_environment
from sheetweave_agent.service import prepare_report
from sheetweave_agent.timing import TimingRecorder


class DiagnosticError(ValueError):
    """Only explicitly authored, content-free setup messages may be displayed."""


class DiagnosticDeadline(TimeoutError):
    """Wall deadline including streaming heartbeats and reasoning."""


def bounded_number(value):
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 180:
        raise argparse.ArgumentTypeError("时间须为大于 0、不超过 180 的有限秒数。")
    return number


def configuration(args):
    if args.worker_pid is not None:
        proc = Path("/proc") / str(args.worker_pid)
        if b"sheetweave_agent.task_worker" not in (proc / "cmdline").read_bytes().split(b"\0"):
            raise DiagnosticError("所选 PID 不是独立 task_worker。")
        values = dict(
            item.decode().split("=", 1)
            for item in (proc / "environ").read_bytes().split(b"\0")
            if b"=" in item
        )
    else:
        values = load_environment(args.env_file) if args.env_file else dict(os.environ)
    for primary, legacy in [
        ("LLM_API_BASE", "DEEPSEEK_API_BASE"),
        ("LLM_MODEL", "DEEPSEEK_MODEL"),
        ("LLM_API_KEY", "DEEPSEEK_API_KEY"),
    ]:
        if not values.get(primary, values.get(legacy, "")).strip():
            raise DiagnosticError(f"必须显式配置 {primary}，禁止回退默认服务或模型。")
    config = load_llm_config(values)
    config.require_available()
    if config.model != args.expect_model:
        raise DiagnosticError("实际模型与 --expect-model 不符，未发送请求。")
    if not math.isfinite(config.timeout):
        raise DiagnosticError("Worker 的 LLM_TIMEOUT 必须为有限数值。")
    return values, config


def prepare_messages(args, config, work):
    started = time.perf_counter()
    if args.input:
        preset, _, _ = ProfileStore.from_environment().load(args.profile, args.profile_version)
        source = args.input
    else:
        preset, source = None, work / "synthetic.csv"
        with source.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["项目", "本周工作"])
            for index in range(args.rows):
                writer.writerow(
                    [f"合成项目{index + 1}", "完成接口测试，检查导入和导出。AUC 为 0.81。"]
                )
    prepared, _ = prepare_report(
        source,
        preset=preset,
        preset_id=None if preset else "general",
        requested_week=args.week,
        sheet=args.sheet,
    )
    blocks = tuple(b for b in prepared.blocks if b.llm_editable)
    if args.max_blocks:
        blocks = blocks[: args.max_blocks]
    if not blocks:
        raise DiagnosticError("所选输入/方案没有可编辑区块。")
    instruction = args.instruction_file.read_text() if args.instruction_file else None

    def messages(items):
        return _messages(prepared, items, ContentMode.POLISH, instruction)

    overhead = sum(len(m["content"]) for m in messages(()))
    sizes = [sum(len(m["content"]) for m in messages((b,))) - overhead + 2 for b in blocks]
    batches = _batches(blocks, config.max_batch_chars, block_sizes=sizes, overhead=overhead)
    if not 1 <= args.batch_index <= len(batches):
        raise DiagnosticError("--batch-index 超出生产批次范围。")
    batch = batches[args.batch_index - 1]
    compact = (
        {
            "role": "system",
            "content": "润色正文保留事实，只返回 JSON："
            '{"blocks":[{"block_id":"原ID","paragraphs":[{"level":1,"text":"正文"}]}]}。'
            + (instruction or ""),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "blocks": [
                        {"block_id": b.block_id, "editable_text": b.original_text} for b in batch
                    ]
                },
                ensure_ascii=False,
            ),
        },
    )
    basic = (
        {"role": "system", "content": "只返回一个 JSON 对象。"},
        {"role": "user", "content": '返回 {"ok":true}。'},
    )
    return {"basic": basic, "report": messages(batch), "compact": compact}, {
        "prepare_seconds": round(time.perf_counter() - started, 6),
        "input_kind": "provided" if args.input else "synthetic",
        "editable_blocks": len(blocks),
        "production_batches": len(batches),
        "selected_batch": args.batch_index,
        "batch_blocks": len(batch),
        "batch_char_budget": config.max_batch_chars,
        "block_ids": tuple(b.block_id for b in batch),
    }


@contextmanager
def wall_deadline(seconds):
    def expired(_signum, _frame):
        raise DiagnosticDeadline()

    previous = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.perf_counter()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, old_timer[0] - (time.perf_counter() - started)),
                old_timer[1],
            )


class Probe:
    """Wrap the native HTTP call, collecting counts without retaining wire content."""

    def __init__(self):
        self.started = time.perf_counter()
        self.result = {
            "http_calls": 0,
            "response_bytes": 0,
            "content_chars": 0,
            "reasoning_chars": 0,
            "stage": "await_headers",
        }
        self.opener = urllib.request.urlopen

    def elapsed(self):
        return round(time.perf_counter() - self.started, 6)

    def open(self, request, *, timeout):
        self.result["http_calls"] += 1
        if self.result["http_calls"] > 1:
            raise DiagnosticError("诊断禁止重试。")
        self.result["request_bytes"] = len(request.data or b"")
        response = self.opener(request, timeout=timeout)
        self.result.update(
            headers_seconds=self.elapsed(), http_status=getattr(response, "status", None)
        )
        probe = self

        class ObservedResponse:
            def __enter__(self):
                response.__enter__()
                return self

            def __exit__(self, *exc):
                return response.__exit__(*exc)

            def read(self, *args):
                return self.receive("read", *args)

            def readline(self, *args):
                return self.receive("readline", *args)

            def receive(self, method, *args):
                probe.result["stage"] = "read_body"
                data = getattr(response, method)(*args)
                probe.result["response_bytes"] += len(data)
                if data:
                    timestamp_key = (
                        "first_stream_line_seconds"
                        if method == "readline"
                        else "body_complete_seconds"
                    )
                    probe.result.setdefault(timestamp_key, probe.elapsed())
                    try:
                        raw = data[5:].strip() if data.startswith(b"data:") else data
                        choice = json.loads(raw).get("choices", [{}])[0]
                        delta = choice.get("delta", choice.get("message", {}))
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            probe.result["reasoning_chars"] += len(reasoning)
                            probe.result.setdefault("first_reasoning_seconds", probe.elapsed())
                    except (ValueError, TypeError, IndexError, AttributeError):
                        pass
                if method == "read":
                    probe.result["stage"] = "decode_response"
                return data

        return ObservedResponse()


def error_metadata(exc):
    chain, current = [], exc
    while isinstance(current, BaseException) and len(chain) < 8:
        chain.append(current)
        current = (
            current.reason if isinstance(current, urllib.error.URLError) else current.__cause__
        )
    status = next((e.code for e in chain if isinstance(e, urllib.error.HTTPError)), None)
    if any(isinstance(e, DiagnosticDeadline) for e in chain):
        kind = "wall_deadline"
    elif any(isinstance(e, TimeoutError) for e in chain):
        kind = "socket_timeout"
    elif status:
        kind = "http_error"
    elif any(isinstance(e, OSError | http.client.HTTPException) for e in chain):
        kind = "network_error"
    else:
        kind = "response_error"
    return {
        "error_kind": kind,
        "exception_type": type(exc).__name__,
        "cause_type": type(chain[-1]).__name__,
        "http_status": status,
    }


def run_case(name, transport, messages, config, block_ids, seconds):
    recorder, probe = TimingRecorder(), Probe()
    result = {
        "case": name,
        "transport": transport,
        "success": False,
        "input_chars": sum(len(m["content"]) for m in messages),
        "batch_blocks": 0 if name == "basic" else len(block_ids),
    }
    client = OpenAICompatibleClient(config, timings=recorder)
    try:
        with wall_deadline(seconds), patch("urllib.request.urlopen", probe.open):
            with recorder.measure("llm_request"):
                if transport == "stream":
                    chunks = []
                    for chunk in client.stream(messages, config.model):
                        probe.result.setdefault("first_content_seconds", probe.elapsed())
                        probe.result["content_chars"] += len(chunk)
                        chunks.append(chunk)
                    content = "".join(chunks)
                else:
                    content = client.complete(messages, config.model)
                    probe.result["content_chars"] = len(content)
                    probe.result["content_available_seconds"] = probe.elapsed()
            probe.result["stage"] = "response_parse"
            with recorder.measure("response_parse"):
                if name == "basic":
                    if not isinstance(json.loads(content), dict):
                        raise ValueError()
                else:
                    _decode_response(content, block_ids)
            probe.result["stage"] = "complete"
            result["success"] = True
    except (ValueError, RuntimeError, OSError, http.client.HTTPException) as exc:
        result.update(error_metadata(exc))
    result.update(probe.result)
    result["total_seconds"] = probe.elapsed()
    result["timings"] = recorder.snapshot()
    return result


def analyse(results):
    notes = []
    lookup = {(r["case"], r["transport"]): r for r in results if "success" in r}
    for transport in ("nonstream", "stream"):
        basic, report, compact = (
            lookup.get((name, transport), {}) for name in ("basic", "report", "compact")
        )
        if basic.get("success") and report and not report.get("success"):
            notes.append(f"{transport}: 基础请求成功、报告失败；比较输入规模及网关处理行为。")
        if report and not report.get("success") and compact.get("success"):
            notes.append(
                f"{transport}: 精简版成功、真实 Prompt 失败；复杂度/规模值得排查，尚非根因证明。"
            )
    for name in ("basic", "report", "compact"):
        normal, stream = (lookup.get((name, kind), {}) for kind in ("nonstream", "stream"))
        if normal and not normal.get("success") and stream.get("success"):
            notes.append(
                f"{name}: 流式成功、非流式失败；排查缓冲及网关等待，不能推断生产应直接改流式。"
            )
    if any(r.get("reasoning_chars", 0) and not r.get("content_chars", 0) for r in results):
        notes.append("观察到推理输出但无正文，需排查推理耗时。")
    if any(r.get("error_kind") == "wall_deadline" for r in results):
        notes.append("达到工具硬截止时间；不等同于生产 60 秒 socket 超时。")
    if any(r.get("stage") == "skipped_total_budget" for r in results):
        notes.append("总时间预算已用尽，部分测试未发送，不能将跳过视为网关失败。")
    if any(r.get("error_kind") in {"wall_deadline", "socket_timeout"} for r in results):
        notes.append("客户端中止不保证上游已停止生成；后续用例可能受其占用影响，必要时分次执行。")
    return notes or ["按阶段时间比较；单次成功不能证明生产负载下稳定。"]


def parser():
    cli = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--worker-pid", type=int, help="读取独立 Worker 的实际 /proc/PID/environ")
    source.add_argument("--env-file", type=Path, help="按统一入口规则加载指定配置")
    source.add_argument("--from-env", action="store_true", help="明确使用当前终端环境")
    cli.add_argument("--expect-model", required=True, help="必须与实际模型完全一致")
    cli.add_argument(
        "--cases",
        nargs="+",
        choices=("basic", "report", "compact"),
        default=["basic", "report", "compact"],
    )
    cli.add_argument("--transport", choices=("nonstream", "stream", "both"), default="nonstream")
    cli.add_argument("--max-requests", type=int, choices=range(1, 7), default=3)
    cli.add_argument(
        "--timeout",
        type=bounded_number,
        default=15,
        help="socket 上限，取其与 Worker 配置较小值，默认15秒",
    )
    cli.add_argument("--deadline", type=bounded_number, default=20, help="单测试硬截止，默认20秒")
    cli.add_argument(
        "--total-timeout", type=bounded_number, default=60, help="整个测试组预算，默认60秒"
    )
    cli.add_argument("--execute", action="store_true", help="允许 API 请求；否则只预览计划")
    cli.add_argument("--input", type=Path, help="可选CSV/XLSX；默认合成数据")
    cli.add_argument("--profile", help="真实输入使用的已保存方案ID")
    cli.add_argument("--profile-version", type=int)
    cli.add_argument("--allow-input-upload", action="store_true", help="明确允许发送所选输入")
    cli.add_argument("--rows", type=int, choices=range(1, 51), default=7)
    cli.add_argument("--max-blocks", type=int)
    cli.add_argument("--batch-index", type=int, default=1, help="只请求此生产批次，默认1")
    cli.add_argument("--week")
    cli.add_argument("--sheet")
    cli.add_argument("--instruction-file", type=Path, help="可选生成时自定义要求，不输出正文")
    cli.add_argument("--output", type=Path, help="可选，保存0600权限脱敏JSON")
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    transports = ["nonstream", "stream"] if args.transport == "both" else [args.transport]
    cases = list(dict.fromkeys(args.cases))
    if len(cases) * len(transports) > args.max_requests:
        cli.error("计划请求数超过 --max-requests，未发送请求。")
    if args.input and not args.profile:
        cli.error("--input 必须同时提供 --profile。")
    if args.input and args.execute and not args.allow_input_upload:
        cli.error("真实文件调用必须提供 --allow-input-upload。")
    if args.max_blocks is not None and args.max_blocks <= 0:
        cli.error("--max-blocks 必须为正整数。")
    if args.profile and not args.input:
        cli.error("--profile 必须与 --input 一起使用。")
    try:
        values, original = configuration(args)
        effective = replace(original, timeout=min(original.timeout, args.timeout), max_retries=0)
        with (
            patch.dict(os.environ, values, clear=True),
            tempfile.TemporaryDirectory(prefix="sheetweave-diagnose-") as directory,
        ):
            messages, metadata = prepare_messages(args, original, Path(directory))
            block_ids = metadata.pop("block_ids")
            report = {
                "model": original.model,
                "config_source": (
                    "worker_process"
                    if args.worker_pid is not None
                    else "env_file"
                    if args.env_file
                    else "explicit_environment"
                ),
                "worker_timeout": original.timeout,
                "effective_timeout": effective.timeout,
                "effective_retries": 0,
                "sequential": True,
                "deadline_seconds": args.deadline,
                "total_budget_seconds": args.total_timeout,
                "planned_requests": len(cases) * len(transports),
                "executed": args.execute,
                **metadata,
                "results": [],
            }
            logger = logging.getLogger("sheetweave_agent.timing")
            with (
                patch.object(logger, "disabled", True),
                patch.object(urllib.request, "_opener", urllib.request.build_opener()),
            ):
                started = time.perf_counter()
                for name in cases:
                    for transport in transports:
                        if not args.execute:
                            report["results"].append(
                                {
                                    "case": name,
                                    "transport": transport,
                                    "input_chars": sum(len(m["content"]) for m in messages[name]),
                                    "stage": "dry_run",
                                }
                            )
                            continue
                        remaining = args.total_timeout - (time.perf_counter() - started)
                        if remaining <= 0:
                            report["results"].append(
                                {
                                    "case": name,
                                    "transport": transport,
                                    "stage": "skipped_total_budget",
                                }
                            )
                            continue
                        report["results"].append(
                            run_case(
                                name,
                                transport,
                                messages[name],
                                effective,
                                block_ids,
                                min(args.deadline, remaining),
                            )
                        )
            report["analysis"] = (
                analyse(report["results"])
                if args.execute
                else ["仅预览计划，HTTP请求数为0；加 --execute 才发送，只测试所选批次。"]
            )
            rendered = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                descriptor = os.open(
                    args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(rendered + "\n")
            print(rendered)
            return int(args.execute and any(not r.get("success") for r in report["results"]))
    except (OSError, ValueError, RuntimeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, DiagnosticError)
            else "检查配置来源、输入方案与权限；异常正文已隐藏。"
        )
        print(json.dumps({"error_type": type(exc).__name__, "error": message}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print('{"error":"interrupted"}')
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
