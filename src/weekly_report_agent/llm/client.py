"""Minimal OpenAI-compatible HTTP client for DeepSeek and injectable test doubles."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Protocol

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

    def __init__(self, config: LLMConfig) -> None:
        config.require_available()
        self._config = config

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        """Execute one structured chat-completion request with bounded retries."""
        payload = json.dumps(
            {
                "model": model,
                "messages": list(messages),
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
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
                with urllib.request.urlopen(request, timeout=self._config.timeout) as response:
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
