import asyncio
import os
from types import SimpleNamespace

from miniclaw.providers.litellm_provider import LiteLLMProvider


def test_litellm_provider_does_not_mutate_global_env(monkeypatch) -> None:
    keys = [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "HOSTED_VLLM_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for key in keys:
        monkeypatch.setenv(key, f"before-{key.lower()}")
    before = {key: os.environ.get(key) for key in keys}

    LiteLLMProvider(
        api_key="sk-test",
        api_base="https://example.invalid/v1",
        default_model="openai/gpt-4o-mini",
    )

    after = {key: os.environ.get(key) for key in keys}
    assert after == before


def test_litellm_provider_passes_api_key_and_base_per_request(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr("miniclaw.providers.litellm_provider.acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="sk-test",
        api_base="https://example.invalid/v1",
        default_model="openai/gpt-4o-mini",
    )

    response = asyncio.run(
        provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o-mini")
    )
    assert response.content == "ok"
    assert captured.get("api_key") == "sk-test"
    assert captured.get("api_base") == "https://example.invalid/v1"
