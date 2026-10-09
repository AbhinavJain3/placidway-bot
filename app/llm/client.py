"""Thin LLM wrapper. Azure OpenAI and OpenAI share one SDK, so switching provider is env-only:
LLM_PROVIDER=azure  -> AzureOpenAI(endpoint, key, api_version); CHAT_MODEL is the deployment name
LLM_PROVIDER=openai -> OpenAI(api_key);                          CHAT_MODEL is the model name
"""
from __future__ import annotations

import json
import logging

from openai import APIError, AzureOpenAI, OpenAI

from app.config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.llm_provider == "azure":
            if not (settings.azure_openai_api_key and settings.azure_openai_endpoint):
                raise LLMError("AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT are not set")
            self._client = AzureOpenAI(
                api_key=settings.azure_openai_api_key,
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                timeout=settings.llm_timeout_s,
                max_retries=2,
            )
        elif settings.llm_provider == "openai":
            if not settings.openai_api_key:
                raise LLMError("OPENAI_API_KEY is not set")
            self._client = OpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_s, max_retries=2)
        else:
            raise LLMError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")

    def json_chat(self, model: str, system: str, user: str, max_tokens: int = 900,
                  reasoning_effort: str = "") -> dict:
        """One chat completion constrained to a JSON object; raises LLMError on any failure.

        reasoning_effort set (e.g. "low") -> reasoning-model parameters (no temperature, completion budget
        covers hidden reasoning tokens); empty -> classic chat model at temperature 0.
        """
        params: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        if reasoning_effort:
            params.update(reasoning_effort=reasoning_effort, max_completion_tokens=max_tokens + 3000)
        else:
            params.update(temperature=0, max_tokens=max_tokens)
        try:
            resp = self._client.chat.completions.create(**params)
        except APIError as exc:
            log.error("LLM call failed: %s", exc)
            raise LLMError(str(exc)) from exc
        content = resp.choices[0].message.content or ""
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {content[:200]}") from exc
