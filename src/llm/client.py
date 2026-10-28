"""
大模型调用封装。

配置一律来自 config.llm_config，本模块不硬编码模型名/密钥。
日志只记录长度与耗时，不落盘完整 prompt / Key。

DeepSeek V4：思考模式默认开启；若不关闭，reasoning 易占满 max_tokens，
导致 content 为空。本产品结构化抽取默认关闭 thinking。
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from typing import Any, Optional

from openai import AsyncOpenAI, OpenAI

from config.llm_config import LLMStage, get_llm_config, get_llm_config_for

logger = logging.getLogger(__name__)


def _extract_message_text(message: Any) -> tuple[str, str, Optional[str]]:
    """返回 (content, reasoning_content, finish_reason 友好摘要)。"""
    content = (getattr(message, "content", None) or "").strip()
    reasoning = (getattr(message, "reasoning_content", None) or "").strip()
    return content, reasoning, None


class LLMClient:
    """同步 + 异步 chat completion 封装，内置重试。"""

    def __init__(self, config: Any = None):
        self.config = config or get_llm_config()
        if not self.config.api_key:
            logger.warning("环境变量 interview（API Key）未配置，调用大模型将会失败")
        else:
            logger.info(
                "LLM 客户端就绪 model=%s base_url=%s key_len=%s thinking=%s",
                self.config.model,
                self.config.base_url,
                len(self.config.api_key),
                "enabled" if self.config.thinking_enabled else "disabled",
            )

        self._sync = OpenAI(
            api_key=self.config.api_key or "EMPTY",
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            max_retries=0,
        )
        self._async = AsyncOpenAI(
            api_key=self.config.api_key or "EMPTY",
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            max_retries=0,
        )

    def _build_kwargs(
        self,
        *,
        messages: list[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        # DeepSeek 思考模式：关闭时 temperature 才生效；开启时文档称 temperature 无效
        thinking_type = "enabled" if self.config.thinking_enabled else "disabled"
        kwargs["extra_body"] = {"thinking": {"type": thinking_type}}
        if not self.config.thinking_enabled:
            kwargs["temperature"] = (
                temperature if temperature is not None else self.config.temperature
            )
        elif self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        return kwargs

    def _content_or_raise(self, resp: Any, *, attempt: int, elapsed: float) -> str:
        choice = resp.choices[0]
        message = choice.message
        content, reasoning, _ = _extract_message_text(message)
        finish = getattr(choice, "finish_reason", None)
        usage = getattr(resp, "usage", None)
        usage_info = ""
        if usage is not None:
            usage_info = (
                f" prompt_tokens={getattr(usage, 'prompt_tokens', '?')}"
                f" completion_tokens={getattr(usage, 'completion_tokens', '?')}"
            )

        if content:
            logger.info(
                "LLM 成功 attempt=%s reply_chars=%s reasoning_chars=%s finish=%s elapsed=%.2fs%s",
                attempt,
                len(content),
                len(reasoning),
                finish,
                elapsed,
                usage_info,
            )
            return content

        # content 空：常见于思考模式占满 token
        hint = ""
        if reasoning:
            hint = (
                f"；已产生思考链 reasoning_chars={len(reasoning)}，"
                "请增大 LLM_MAX_TOKENS 或设置 LLM_THINKING=false"
            )
        elif finish == "length":
            hint = "；finish_reason=length，输出被截断，请增大 LLM_MAX_TOKENS"
        logger.warning(
            "LLM 返回空 content attempt=%s finish=%s reasoning_chars=%s elapsed=%.2fs%s%s",
            attempt,
            finish,
            len(reasoning),
            elapsed,
            usage_info,
            hint,
        )
        raise RuntimeError(f"模型返回空内容{hint}")

    def chat(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        last_err: Optional[Exception] = None
        prompt_len = len(prompt or "")
        for attempt in range(1, self.config.max_retries + 1):
            started = time.perf_counter()
            try:
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})
                logger.info(
                    "LLM sync 请求 attempt=%s/%s model=%s prompt_chars=%s thinking=%s",
                    attempt,
                    self.config.max_retries,
                    self.config.model,
                    prompt_len,
                    self.config.thinking_enabled,
                )
                resp = self._sync.chat.completions.create(
                    **self._build_kwargs(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                )
                return self._content_or_raise(
                    resp, attempt=attempt, elapsed=time.perf_counter() - started
                )
            except Exception as e:
                last_err = e
                elapsed = time.perf_counter() - started
                logger.warning(
                    "LLM sync 失败 attempt=%s/%s elapsed=%.2fs err=%s",
                    attempt,
                    self.config.max_retries,
                    elapsed,
                    e,
                )
                if attempt < self.config.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"LLM 调用失败（已重试 {self.config.max_retries} 次）: {last_err}")

    async def achat(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        last_err: Optional[Exception] = None
        prompt_len = len(prompt or "")
        for attempt in range(1, self.config.max_retries + 1):
            started = time.perf_counter()
            try:
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})
                logger.info(
                    "LLM async 请求 attempt=%s/%s model=%s prompt_chars=%s thinking=%s",
                    attempt,
                    self.config.max_retries,
                    self.config.model,
                    prompt_len,
                    self.config.thinking_enabled,
                )
                resp = await self._async.chat.completions.create(
                    **self._build_kwargs(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                )
                return self._content_or_raise(
                    resp, attempt=attempt, elapsed=time.perf_counter() - started
                )
            except Exception as e:
                last_err = e
                elapsed = time.perf_counter() - started
                logger.warning(
                    "LLM async 失败 attempt=%s/%s elapsed=%.2fs err=%s",
                    attempt,
                    self.config.max_retries,
                    elapsed,
                    e,
                )
                if attempt < self.config.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"LLM 调用失败（已重试 {self.config.max_retries} 次）: {last_err}")


@lru_cache
def get_llm_client() -> LLMClient:
    return LLMClient()


@lru_cache
def get_llm_client_for(stage: LLMStage) -> LLMClient:
    """按 FR 阶段取独立配置的客户端。"""
    return LLMClient(get_llm_config_for(stage))
