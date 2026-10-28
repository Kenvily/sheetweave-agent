"""Minimal OpenAI-compatible HTTP client for DeepSeek and injectable test doubles."""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from typing import Protocol

from ..timing import TimingRecorder, diagnostic_event, measure
from .config import LLMConfig


class LLMClientError(RuntimeError):
    """Raised when a remote LLM request fails or returns an invalid envelope."""


class LLMClient(Protocol):
    """Interface implemented by remote clients and deterministic test doubles."""

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        """Return the assistant response content for one chat completion."""
        ...


class OpenAICompatibleClient:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint using urllib."""

    def __init__(self, config: LLMConfig, *, timings: TimingRecorder | None = None) -> None:
        config.require_available()
        self._config = config
        self._timings = timings

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        """Execute one structured chat-completion request with bounded retries."""
        payload = json.dumps(
            {
                "model": model,
                "messages": list(messages),
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                **(
                    {"reasoning_effort": self._config.reasoning_effort}
                    if self._config.reasoning_effort
                    else {}
                ),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.api_base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            started = time.perf_counter()
            stage, status, output_bytes = "await_headers", None, 0
            diagnostic_event(
                "http_start",
                model=model,
                attempt=attempt + 1,
                attempts=attempts,
                request_bytes=len(payload),
                input_chars=sum(len(message["content"]) for message in messages),
                timeout_seconds=self._config.timeout,
            )
            try:
                with measure(self._timings, "http_attempt", attempt=attempt + 1):
                    with urllib.request.urlopen(request, timeout=self._config.timeout) as response:
                        status = getattr(response, "status", None)
                        diagnostic_event(
                            "http_headers",
                            status=status,
                            elapsed_seconds=round(time.perf_counter() - started, 6),
                        )
                        stage = "read_body"
                        raw = response.read()
                        output_bytes = len(raw)
                    stage = "decode_response"
                    envelope = json.loads(raw.decode("utf-8"))
                    content = envelope["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise LLMClientError("LLM 返回了空内容。")
                diagnostic_event(
                    "http_end",
                    success=True,
                    stage=stage,
                    status=status,
                    attempt=attempt + 1,
                    attempts=attempts,
                    output_bytes=output_bytes,
                    elapsed_seconds=round(time.perf_counter() - started, 6),
                )
                return content
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                LLMClientError,
            ) as exc:
                cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                status = exc.code if isinstance(exc, urllib.error.HTTPError) else status
                timed_out = isinstance(cause, TimeoutError)
                retryable = (
                    isinstance(exc, urllib.error.HTTPError)
                    and (exc.code == 429 or 500 <= exc.code < 600)
                ) or (
                    isinstance(exc, urllib.error.URLError | ConnectionError)
                    and stage == "await_headers"
                    and not isinstance(exc, urllib.error.HTTPError)
                    and not timed_out
                    and not isinstance(cause, ssl.SSLCertVerificationError)
                )
                retry = retryable and attempt + 1 < attempts
                diagnostic_event(
                    "http_end",
                    success=False,
                    stage=stage,
                    status=status,
                    attempt=attempt + 1,
                    attempts=attempts,
                    output_bytes=output_bytes,
                    elapsed_seconds=round(time.perf_counter() - started, 6),
                    exception_type=type(exc).__name__,
                    cause_type=type(cause).__name__,
                    retry=retry,
                )
                if not retry:
                    if isinstance(exc, LLMClientError):
                        raise
                    if isinstance(exc, urllib.error.HTTPError):
                        message = f"LLM 服务请求失败（HTTP {exc.code}）。"
                    elif timed_out:
                        location = (
                            "等待响应头（连接或模型首响应）"
                            if stage == "await_headers"
                            else "读取响应正文"
                        )
                        message = (
                            f"LLM {location}超时（等待阈值 {self._config.timeout:g} 秒）；"
                            "未自动重试，请缩小批次或联系模型服务管理员。"
                        )
                    elif isinstance(
                        exc, urllib.error.URLError | OSError | http.client.HTTPException
                    ):
                        message = "LLM 网络连接失败或响应中断，请检查网络及模型服务。"
                    else:
                        message = "LLM 服务响应解析失败，返回格式无效。"
                    raise LLMClientError(message) from exc
            time.sleep(min(0.25 * (2**attempt), 1.0))
        raise LLMClientError("LLM 请求失败。")  # pragma: no cover

    def stream(self, messages: Sequence[dict[str, str]], model: str) -> Iterator[str]:
        """Yield provider content, retrying only before any visible output."""
        payload = json.dumps(
            {
                "model": model,
                "messages": list(messages),
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                **(
                    {"reasoning_effort": self._config.reasoning_effort}
                    if self._config.reasoning_effort
                    else {}
                ),
                "stream": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.api_base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        visible = False
        size = 0
        for attempt in range(self._config.max_retries + 1):
            try:
                with (
                    measure(self._timings, "http_attempt", attempt=attempt + 1),
                    urllib.request.urlopen(request, timeout=self._config.timeout) as response,
                ):
                    data: list[str] = []
                    while True:
                        line = response.readline(1024 * 1024 + 1)
                        if len(line) > 1024 * 1024:
                            raise LLMClientError("模型流式事件过大。")
                        if not line:
                            raise LLMClientError("模型流式连接提前结束，未生成可下载报告。")
                        line = line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        if line or not data:
                            continue
                        value = "\n".join(data)
                        data = []
                        if value == "[DONE]":
                            if not visible:
                                raise LLMClientError("模型返回了空内容。")
                            return
                        event = json.loads(value)
                        if "error" in event:
                            raise LLMClientError("模型流式服务返回错误。")
                        choices = event.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        content = choice.get("delta", {}).get("content")
                        if content:
                            if not isinstance(content, str):
                                raise LLMClientError("模型流式内容格式无效。")
                            visible = True
                            size += len(content.encode("utf-8"))
                            if size > 4 * 1024 * 1024:
                                raise LLMClientError("模型输出过大，请减少输入内容。")
                            yield content
                        finish = choice.get("finish_reason")
                        if finish:
                            if finish != "stop" or not visible:
                                raise LLMClientError("模型流式输出未完整结束，请重试。")
                            return
            except urllib.error.HTTPError as exc:
                if visible or exc.code not in {429, 500, 502, 503, 504}:
                    raise LLMClientError(f"模型流式请求失败（HTTP {exc.code}）。") from exc
                if attempt == self._config.max_retries:
                    raise LLMClientError(f"模型流式请求失败（HTTP {exc.code}）。") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                if (
                    visible
                    or isinstance(cause, TimeoutError)
                    or attempt == self._config.max_retries
                ):
                    diagnostic_event(
                        "http_stream_failure",
                        model=model,
                        exception_type=type(exc).__name__,
                        cause_type=type(cause).__name__,
                    )
                    raise LLMClientError(
                        "模型流式连接中断或超时，未自动重试，请核对服务。"
                    ) from exc
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                AttributeError,
            ) as exc:
                raise LLMClientError("模型流式事件格式无效。") from exc
            time.sleep(min(0.25 * (2**attempt), 1.0))
