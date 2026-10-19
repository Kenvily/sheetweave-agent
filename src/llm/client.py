"""Minimal OpenAI-compatible HTTP client for DeepSeek and injectable test doubles."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from typing import Protocol

from ..timing import TimingRecorder, measure
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
            try:
                with (
                    measure(self._timings, "http_attempt", attempt=attempt + 1),
                    urllib.request.urlopen(request, timeout=self._config.timeout) as response,
                ):
                    envelope = json.loads(response.read().decode("utf-8"))
                content = envelope["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LLMClientError("LLM 返回了空内容。")
                return content
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 == attempts:
                    raise LLMClientError(f"LLM 服务请求失败（HTTP {exc.code}）。") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 == attempts:
                    raise LLMClientError("LLM 服务连接超时或不可用。") from exc
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
                raise LLMClientError("LLM 服务返回格式无效。") from exc
            if attempt + 1 < attempts:
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
                if visible or attempt == self._config.max_retries:
                    raise LLMClientError("模型流式连接中断或超时，请重试。") from exc
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                AttributeError,
            ) as exc:
                raise LLMClientError("模型流式事件格式无效。") from exc
            time.sleep(min(0.25 * (2**attempt), 1.0))
