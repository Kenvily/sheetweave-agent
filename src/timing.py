"""Thread-safe duration records containing only stage names and numeric metadata."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from time import perf_counter


class TimingRecorder:
    """Collect per-run elapsed times without retaining input or output content."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._lock = Lock()

    @contextmanager
    def measure(self, stage: str, **counts: int) -> Iterator[None]:
        """Record successful and failed stages, including concurrent batches."""
        started = perf_counter()
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
            with self._lock:
                self._events.append(event)

    def snapshot(self) -> list[dict[str, object]]:
        """Return detached, serializable timing events."""
        with self._lock:
            return [dict(event) for event in self._events]


@contextmanager
def measure(recorder: TimingRecorder | None, stage: str, **counts: int) -> Iterator[None]:
    """Measure only when a caller explicitly supplies a recorder."""
    if recorder is None:
        yield
    else:
        with recorder.measure(stage, **counts):
            yield
