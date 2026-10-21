"""Thread-safe duration records containing only stage names and numeric metadata."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter

_CONTEXT: ContextVar[dict | None] = ContextVar("diagnostic_context", default=None)
LOGGER = logging.getLogger(__name__)


def diagnostic_context() -> dict:
    return dict(_CONTEXT.get() or {})


@contextmanager
def diagnostic_scope(**fields) -> Iterator[None]:
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def diagnostic_event(event: str, **fields) -> None:
    """Only call with identifiers, counts and exception types, never user content."""
    LOGGER.info(
        json.dumps(
            {
                "event": event,
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                **(_CONTEXT.get() or {}),
                **fields,
            },
            ensure_ascii=False,
        )
    )


class TimingRecorder:
    """Collect per-run elapsed times without retaining input or output content."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._lock = Lock()

    @contextmanager
    def measure(self, stage: str, **counts: int) -> Iterator[None]:
        """Record successful and failed stages, including concurrent batches."""
        started = perf_counter()
        diagnostic_event("stage_start", stage=stage, **counts)
        success = False
        try:
            yield
            success = True
        finally:
            event = {
                "stage": stage,
                "seconds": round(perf_counter() - started, 6),
                "success": success,
                **counts,
            }
            diagnostic_event("stage_end", **event)
            with self._lock:
                self._events.append(event)

    def snapshot(self) -> list[dict[str, object]]:
        """Return detached, serializable timing events."""
        with self._lock:
            return [dict(event) for event in self._events]


@contextmanager
def measure(recorder: TimingRecorder | None, stage: str, **counts: int) -> Iterator[None]:
    """Log stage timings and optionally retain them in a shared recorder."""
    with (recorder or TimingRecorder()).measure(stage, **counts):
        yield
