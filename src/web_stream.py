"""Bounded SSE delivery with request-scoped cancellation."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from queue import Empty, Full, Queue
from threading import Event, Thread


class GenerationCancelled(RuntimeError):
    """Raised when a disconnected browser no longer accepts output."""


def stream_events(work: Callable[[Callable[[str, dict], None]], None]) -> Iterator[str]:
    """Deliver progress immediately, bound queued data, and cancel disconnected runs."""
    events: Queue[tuple[str, dict]] = Queue(maxsize=32)
    cancelled, finished = Event(), Event()

    def emit(kind: str, payload: dict) -> None:
        while not cancelled.is_set():
            try:
                events.put((kind, payload), timeout=0.2)
                return
            except Full:
                continue
        raise GenerationCancelled()

    def run() -> None:
        try:
            work(emit)
        except GenerationCancelled:
            pass
        except Exception:
            if not cancelled.is_set():
                emit("error", {"message": "生成失败，请重试或切换原文模式。"})
        finally:
            finished.set()

    try:
        yield 'event: progress\ndata: {"message":"已接收文件，正在准备生成。"}\n\n'
        Thread(target=run, daemon=True, name="weekly-report-stream").start()
        while not finished.is_set() or not events.empty():
            try:
                kind, payload = events.get(timeout=5)
            except Empty:
                yield ": heartbeat\n\n"
                continue
            yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        cancelled.set()
