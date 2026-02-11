"""Provider wrapper with retry/backoff and cross-provider failover."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, AsyncIterator

from miniclaw.providers.base import LLMProvider, LLMResponse, LLMStreamEvent


@dataclass
class FailoverCandidate:
    """Resolved provider candidate."""

    name: str
    provider: LLMProvider


class FailoverProvider(LLMProvider):
    """Wrap multiple providers and fail over on retryable errors."""

    RETRYABLE_FINISH_REASONS = {"error", "overloaded"}

    def __init__(
        self,
        *,
        candidates: list[FailoverCandidate],
        default_model: str,
        failover_policy: Any | None = None,
    ):
        if not candidates:
            raise ValueError("FailoverProvider requires at least one provider candidate.")
        super().__init__(api_key=None, api_base=None)
        self._candidates = list(candidates)
        self._default_model = default_model
        self._policy = failover_policy

    def get_default_model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._default_model
        fallback_response: LLMResponse | None = None
        fallback_error: str | None = None

        for candidate in self._candidates:
            attempts, base_ms, max_ms = self._policy_for(candidate.name, chosen_model)
            for attempt_index in range(attempts):
                try:
                    response = await candidate.provider.chat(
                        messages=messages,
                        tools=tools,
                        model=chosen_model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        thinking=thinking,
                    )
                except Exception as exc:  # pragma: no cover - provider implementations should return errors
                    fallback_error = str(exc)
                    response = LLMResponse(
                        content=f"Error calling LLM: {exc}",
                        finish_reason="error",
                    )

                if not self._is_retryable_response(response):
                    return response

                fallback_response = response
                if attempt_index < attempts - 1:
                    await asyncio.sleep(self._backoff_s(base_ms, max_ms, attempt_index))

        if fallback_response is not None:
            return fallback_response
        return LLMResponse(
            content=f"Error calling LLM: {fallback_error or 'failover candidates exhausted'}",
            finish_reason="error",
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        chosen_model = model or self._default_model
        fallback_final: LLMResponse | None = None

        for candidate in self._candidates:
            attempts, base_ms, max_ms = self._policy_for(candidate.name, chosen_model)
            for attempt_index in range(attempts):
                had_delta = False
                final_response: LLMResponse | None = None
                try:
                    async for event in candidate.provider.stream_chat(
                        messages=messages,
                        tools=tools,
                        model=chosen_model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        thinking=thinking,
                    ):
                        if event.type == "delta" and event.delta:
                            had_delta = True
                            yield event
                            continue
                        if event.type == "final" and event.response:
                            final_response = event.response

                    if final_response is None:
                        # Provider emitted no final event; fall back to non-streaming call.
                        final_response = await candidate.provider.chat(
                            messages=messages,
                            tools=tools,
                            model=chosen_model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            thinking=thinking,
                        )
                except Exception as exc:  # pragma: no cover - defensive guard
                    final_response = LLMResponse(
                        content=f"Error calling LLM: {exc}",
                        finish_reason="error",
                    )

                if final_response is None:
                    continue

                retryable = self._is_retryable_response(final_response)
                fallback_final = final_response
                if not retryable or had_delta:
                    yield LLMStreamEvent(type="final", response=final_response)
                    return
                if attempt_index < attempts - 1:
                    await asyncio.sleep(self._backoff_s(base_ms, max_ms, attempt_index))

        yield LLMStreamEvent(
            type="final",
            response=fallback_final
            or LLMResponse(
                content="Error calling LLM: failover candidates exhausted",
                finish_reason="error",
            ),
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        chosen_model = model or self._default_model
        last_error: Exception | None = None
        for candidate in self._candidates:
            attempts, base_ms, max_ms = self._policy_for(candidate.name, chosen_model)
            for attempt_index in range(attempts):
                try:
                    return await candidate.provider.embed(texts, model=chosen_model)
                except Exception as exc:
                    last_error = exc
                    if attempt_index < attempts - 1:
                        await asyncio.sleep(self._backoff_s(base_ms, max_ms, attempt_index))
        if last_error:
            raise last_error
        raise RuntimeError("No provider candidates available for embeddings.")

    @staticmethod
    def _is_retryable_response(response: LLMResponse) -> bool:
        reason = str(response.finish_reason or "").strip().lower()
        if reason in FailoverProvider.RETRYABLE_FINISH_REASONS:
            return True
        content = str(response.content or "")
        return content.strip().startswith("Error calling LLM:")

    def _policy_for(self, provider_name: str, model: str) -> tuple[int, int, int]:
        # Defaults if policy config is absent.
        attempts = 2
        base_ms = 350
        max_ms = 5000

        policy = self._policy
        if policy is None:
            return attempts, base_ms, max_ms

        default = getattr(policy, "default", None)
        if default is not None:
            attempts = int(getattr(default, "max_attempts", attempts) or attempts)
            base_ms = int(getattr(default, "base_backoff_ms", base_ms) or base_ms)
            max_ms = int(getattr(default, "max_backoff_ms", max_ms) or max_ms)

        provider_overrides = getattr(policy, "provider_overrides", {}) or {}
        provider_policy = provider_overrides.get(provider_name) or provider_overrides.get(provider_name.lower())
        if provider_policy is not None:
            attempts = int(getattr(provider_policy, "max_attempts", attempts) or attempts)
            base_ms = int(getattr(provider_policy, "base_backoff_ms", base_ms) or base_ms)
            max_ms = int(getattr(provider_policy, "max_backoff_ms", max_ms) or max_ms)

        model_overrides = getattr(policy, "model_overrides", {}) or {}
        model_policy = model_overrides.get(model) or model_overrides.get(model.lower())
        if model_policy is not None:
            attempts = int(getattr(model_policy, "max_attempts", attempts) or attempts)
            base_ms = int(getattr(model_policy, "base_backoff_ms", base_ms) or base_ms)
            max_ms = int(getattr(model_policy, "max_backoff_ms", max_ms) or max_ms)

        attempts = max(1, attempts)
        base_ms = max(0, base_ms)
        max_ms = max(1, max_ms)
        return attempts, base_ms, max_ms

    @staticmethod
    def _backoff_s(base_ms: int, max_ms: int, attempt_index: int) -> float:
        if base_ms <= 0:
            return 0.0
        raw = min(max_ms, base_ms * (2 ** max(0, attempt_index)))
        jitter = random.uniform(0, max(1, raw) * 0.2)
        return (raw + jitter) / 1000.0
