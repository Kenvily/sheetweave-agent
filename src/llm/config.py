"""Server-side configuration for OpenAI-compatible LLM services."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MODELS = ("deepseek-chat", "deepseek-reasoner")


class LLMConfigurationError(ValueError):
    """Raised when server-side LLM configuration is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Validated server-side LLM settings without exposing the API key to clients."""

    api_key: str = field(repr=False)
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    allowed_models: tuple[str, ...] = DEFAULT_MODELS
    timeout: float = 60.0
    max_retries: int = 1
    max_batch_chars: int = 12000
    max_concurrency: int = 2
    auto_correct: bool = False

    @property
    def available(self) -> bool:
        """Return whether LLM calls can be made with this configuration."""
        return bool(self.api_key)

    def require_available(self) -> None:
        """Require a server-side API key for a remote LLM call."""
        if not self.available:
            raise LLMConfigurationError(
                "服务器尚未配置 DeepSeek API Key，请配置后重试或选择“原文保留”。"
            )

    def select_model(self, model: str | None) -> LLMConfig:
        """Return a copy using an allowed model selected by the user."""
        selected = (model or self.model).strip()
        if selected not in self.allowed_models:
            raise LLMConfigurationError(f"模型“{selected}”不在服务器允许列表中。")
        return replace(self, model=selected)


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise LLMConfigurationError(f"{name} 必须是数字。") from exc
    if parsed <= 0:
        raise LLMConfigurationError(f"{name} 必须大于 0。")
    return parsed


def _nonnegative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise LLMConfigurationError(f"{name} 必须是整数。") from exc
    if parsed < 0:
        raise LLMConfigurationError(f"{name} 不能小于 0。")
    return parsed


def _positive_int(value: str, name: str) -> int:
    parsed = _nonnegative_int(value, name)
    if parsed == 0:
        raise LLMConfigurationError(f"{name} 必须大于 0。")
    return parsed


def validate_llm_config(config: LLMConfig) -> LLMConfig:
    """Validate an LLM configuration and normalize its API base URL."""
    api_base = config.api_base.strip().rstrip("/")
    parsed = urlparse(api_base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise LLMConfigurationError("LLM API 地址必须是有效的 HTTP/HTTPS 服务地址。")
    models = tuple(dict.fromkeys(item.strip() for item in config.allowed_models if item.strip()))
    if not models or any(not MODEL_PATTERN.fullmatch(item) for item in models):
        raise LLMConfigurationError("LLM 模型允许列表无效。")
    if config.model not in models:
        raise LLMConfigurationError("默认 LLM 模型必须包含在允许列表中。")
    if (
        config.timeout <= 0
        or config.max_retries < 0
        or config.max_batch_chars <= 0
        or not 1 <= config.max_concurrency <= 4
    ):
        raise LLMConfigurationError("LLM 超时、重试或批次长度配置无效。")
    return replace(config, api_base=api_base, allowed_models=models)


def load_llm_config(environ: dict[str, str] | None = None) -> LLMConfig:
    """Load DeepSeek/OpenAI-compatible settings from server environment variables."""
    values = os.environ if environ is None else environ
    models = tuple(
        item.strip()
        for item in values.get("DEEPSEEK_MODELS", ",".join(DEFAULT_MODELS)).split(",")
        if item.strip()
    )
    config = LLMConfig(
        api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
        api_base=values.get("DEEPSEEK_API_BASE", DEFAULT_API_BASE),
        model=values.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip(),
        allowed_models=models,
        timeout=_positive_float(values.get("LLM_TIMEOUT", "60"), "LLM_TIMEOUT"),
        max_retries=_nonnegative_int(values.get("LLM_MAX_RETRIES", "1"), "LLM_MAX_RETRIES"),
        max_batch_chars=_positive_int(
            values.get("LLM_MAX_BATCH_CHARS", "12000"), "LLM_MAX_BATCH_CHARS"
        ),
        max_concurrency=_positive_int(
            values.get("LLM_MAX_CONCURRENCY", "2"), "LLM_MAX_CONCURRENCY"
        ),
        auto_correct=values.get("LLM_AUTO_CORRECT", "false").strip().lower() == "true",
    )
    return validate_llm_config(config)
