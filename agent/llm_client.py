"""LLM 客户端：OpenAI 兼容接口封装，支持「多模型池」。

- 生效 provider 由 settings.llm_active 决定（WebUI「设置」里选默认模型），
  池中每个 provider 独立 base_url / api_key / model；
- chat()       : 普通文本对话
- chat_json()  : 强制输出合法 JSON（通过 response_format + 兜底解析）
- 内置指数退避重试、超时、日志

向后兼容：保留 DeepSeekClient 类名别名（旧代码 import 不受影响）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .config import Settings, resolve_llm_provider

log = logging.getLogger("social_agent.llm")


class LLMError(RuntimeError):
    """LLM 调用异常（网络/限流/格式错误统一包装）。"""


class LLMClient:
    """OpenAI 兼容 LLM 客户端（deepseek / 通义 / kimi / 本地网关等通用）。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None  # 延迟初始化，避免无 Key 时 import 即失败
        self._max_retries = 3
        # 快照当前生效 provider，保证一次实例生命周期内模型一致
        self._provider = resolve_llm_provider(settings) or {}

    @property
    def provider_id(self) -> str:
        return self._provider.get("id", "")

    @property
    def provider_name(self) -> str:
        return self._provider.get("name", self.provider_id) or self.provider_id

    @property
    def model(self) -> str:
        return self._provider.get("model", "")

    @property
    def available(self) -> bool:
        """是否具备调用条件（有生效 provider 且已填 API Key）。"""
        return bool(self._provider and self._provider.get("api_key"))

    # ---- 内部 ----
    def _get_client(self):
        """惰性创建 OpenAI 客户端（httpx 底层）。"""
        if self._client is None:
            if not self.available:
                raise LLMError(
                    "未配置可用的 LLM API Key：请到 WebUI「设置 → 模型与密钥」"
                    "为当前默认模型填写密钥。"
                )
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._provider.get("api_key"),
                base_url=self._provider.get("base_url") or None,
                timeout=120.0,
                max_retries=0,  # 重试由本类自管（指数退避）
            )
        return self._client

    def _chat_once(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        """单次对话请求，带超时；异常上抛由 _chat_with_retry 处理。"""
        resp = self._get_client().chat.completions.create(
            model=self._provider.get("model") or self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.debug("LLM usage: prompt=%s completion=%s", usage.prompt_tokens, usage.completion_tokens)
        return content

    def _chat_with_retry(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        """指数退避重试（网络抖动/限流）。"""
        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._chat_once(messages, temperature, max_tokens)
            except Exception as exc:  # noqa: BLE001 —— 统一重试策略
                last_err = exc
                wait = 2**attempt
                log.warning("LLM 调用失败(第 %s/%s 次): %s，%ss 后重试", attempt, self._max_retries, exc, wait)
                time.sleep(wait)
        raise LLMError(f"LLM 调用多次重试仍失败: {last_err}")

    # ---- 对外 ----
    def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """普通对话。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            return self._chat_with_retry(messages, temperature, max_tokens).strip()
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM 调用异常: {exc}") from exc

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """要求 LLM 输出一个 JSON 对象，并做容错解析（剥离代码块/前后杂文）。"""
        messages = [
            {"role": "system", "content": system + "\n你必须只输出一个合法的 JSON 对象，不要输出任何其他内容。"},
            {"role": "user", "content": user},
        ]
        raw = self._chat_with_retry(messages, temperature, max_tokens)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """容错解析 JSON：去掉 markdown 代码块围栏，截取首个 { ... } 段。"""
        text = raw.strip()
        # 去掉 ```json ... ``` 围栏
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 兜底：截取第一个 { 到最后一个 }
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"LLM 输出无法解析为 JSON: {raw[:200]}...")


# 向后兼容别名：旧代码（skills/*）沿用 DeepSeekClient 类名
DeepSeekClient = LLMClient
