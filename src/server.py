"""Run Web and the independent task worker together in one foreground terminal."""

from __future__ import annotations

import argparse
import http.client
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]


def load_environment(path: Path | None) -> dict[str, str]:
    """Read simple dotenv assignments without executing code or expanding variables."""
    values = dict(os.environ)
    if path is None:
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.removeprefix("export ").partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            raise ValueError(f"环境文件第 {number} 行不是有效赋值。")
        try:
            parts = shlex.split(value, comments=True)
        except ValueError as exc:
            raise ValueError(f"环境文件第 {number} 行引号无效。") from exc
        if len(parts) > 1:
            raise ValueError(f"环境文件第 {number} 行包含未引用的空格。")
        values[name.strip()] = parts[0] if parts else ""
    return values


def web_ready(host: str, port: int) -> bool:
    """Probe a lightweight redirect without incrementing homepage visitor counts."""
    connection = http.client.HTTPConnection(host, port, timeout=0.5)
    try:
        connection.request("GET", "/preview")
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 400
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def run_services(
    web_command: list[str],
    worker_command: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    ready: Callable[[], bool],
    shutdown_timeout: float = 190,
) -> int:
    """Supervise both process groups; a stopped child stops its peer as well."""
    children: list[tuple[str, subprocess.Popen]] = []
    stopped, force = Event(), Event()
    previous = {}

    def stop(_signum, _frame):
        if stopped.is_set():
            force.set()
        stopped.set()

    def send(process, signum):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        web = subprocess.Popen(web_command, cwd=cwd, env=environ, start_new_session=True)
        children.append(("Web", web))
        deadline = time.monotonic() + 20
        while not stopped.is_set():
            if web.poll() is not None:
                print("Web 启动失败，未启动任务 Worker。", file=sys.stderr, flush=True)
                return 1
            if ready():
                break
            if time.monotonic() >= deadline:
                print("Web 启动检查超时。", file=sys.stderr, flush=True)
                return 1
            stopped.wait(0.1)
        if stopped.is_set():
            return 0
        worker = subprocess.Popen(worker_command, cwd=cwd, env=environ, start_new_session=True)
        children.append(("任务 Worker", worker))
        print(f"Web PID={web.pid}，任务 Worker PID={worker.pid}。Ctrl+C 同时停止。", flush=True)
        while not stopped.wait(0.2):
            for name, process in children:
                if process.poll() is not None:
                    print(f"{name} 意外退出，将停止另一服务。", file=sys.stderr, flush=True)
                    return 1
        return 0
    except OSError:
        print("服务启动失败，请检查环境、执行程序和权限。", file=sys.stderr, flush=True)
        return 1
    finally:
        if children:
            print("正在停止服务，等待当前任务结束；再次 Ctrl+C 可强制停止。", flush=True)
        for _name, process in children:
            send(process, signal.SIGTERM)
        deadline = time.monotonic() + shutdown_timeout
        while any(process.poll() is None for _name, process in children):
            if force.is_set() or time.monotonic() >= deadline:
                for _name, process in children:
                    send(process, signal.SIGKILL)
                break
            time.sleep(0.1)
        # Also remove descendants left behind by an unexpectedly exited master.
        for _name, process in children:
            send(process, signal.SIGKILL)
            process.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> int:
    """Start both services with identical configuration and inherited terminal logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18989)
    parser.add_argument(
        "--workers", type=int, help="覆盖 gunicorn.conf.py 中的 Web 进程数（默认使用配置文件）"
    )
    parser.add_argument("--threads", type=int, default=2, help="每个 Web 进程的请求线程数")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--env-file", type=Path, help="默认读取项目 .env（存在时）")
    group.add_argument("--no-env", action="store_true", help="只使用已有环境变量")
    args = parser.parse_args()
    if (
        not 1 <= args.port <= 65535
        or (args.workers is not None and args.workers < 1)
        or args.threads < 1
    ):
        parser.error("端口、Web 进程数或线程数无效。")
    path = args.env_file or (ROOT / ".env" if (ROOT / ".env").is_file() else None)
    try:
        environ = load_environment(None if args.no_env else path)
        # Refuse an occupied port before starting a worker that could claim tasks.
        address = socket.getaddrinfo(args.host, args.port, type=socket.SOCK_STREAM)[0]
        with socket.socket(address[0], address[1], address[2]) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address[4])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("无法读取环境文件或监听端口；请检查路径/权限及端口占用。", file=sys.stderr)
        return 1
    bind = f"[{args.host}]:{args.port}" if ":" in args.host else f"{args.host}:{args.port}"
    host = "127.0.0.1" if args.host == "0.0.0.0" else "::1" if args.host == "::" else args.host
    web = [
        sys.executable,
        "-m",
        "gunicorn",
        "--config",
        str(ROOT / "gunicorn.conf.py"),
    ]
    if args.workers is not None:
        web.extend(["--workers", str(args.workers)])
    web.extend(
        [
            "--worker-class",
            "gthread",
            "--threads",
            str(args.threads),
            "--bind",
            bind,
            "--access-logfile",
            "-",
            "--error-logfile",
            "-",
            "sheetweave_agent.web:create_app()",
        ]
    )
    worker = [sys.executable, "-m", "sheetweave_agent.task_worker"]
    return run_services(
        web, worker, cwd=ROOT, environ=environ, ready=lambda: web_ready(host, args.port)
    )


if __name__ == "__main__":
    raise SystemExit(main())
