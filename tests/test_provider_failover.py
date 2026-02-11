from miniclaw.config.schema import Config
from miniclaw.providers.base import LLMProvider, LLMResponse
from miniclaw.providers.failover import FailoverCandidate, FailoverProvider


class ScriptProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__(api_key=None, api_base=None)
        self.responses = list(responses)
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ):
        self.calls += 1
        if not self.responses:
            return LLMResponse(content="", finish_reason="error")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def embed(self, texts, model=None):
        self.calls += 1
        return [[0.1, 0.2] for _ in texts]

    def get_default_model(self) -> str:
        return "test/model"


async def test_failover_moves_to_next_provider_on_error() -> None:
    p1 = ScriptProvider([LLMResponse(content="Error calling LLM: upstream", finish_reason="error")])
    p2 = ScriptProvider([LLMResponse(content="ok", finish_reason="stop")])
    config = Config()
    config.providers.failover.default.max_attempts = 1
    config.providers.failover.default.base_backoff_ms = 0
    wrapper = FailoverProvider(
        candidates=[FailoverCandidate("openai", p1), FailoverCandidate("anthropic", p2)],
        default_model="test/model",
        failover_policy=config.providers.failover,
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}], model="test/model")
    assert response.content == "ok"
    assert p1.calls == 1
    assert p2.calls == 1


async def test_failover_retries_same_provider_before_switch() -> None:
    p1 = ScriptProvider(
        [
            LLMResponse(content="", finish_reason="overloaded"),
            LLMResponse(content="recovered", finish_reason="stop"),
        ]
    )
    p2 = ScriptProvider([LLMResponse(content="never-used", finish_reason="stop")])
    config = Config()
    config.providers.failover.default.max_attempts = 2
    config.providers.failover.default.base_backoff_ms = 0
    wrapper = FailoverProvider(
        candidates=[FailoverCandidate("openai", p1), FailoverCandidate("anthropic", p2)],
        default_model="test/model",
        failover_policy=config.providers.failover,
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}], model="test/model")
    assert response.content == "recovered"
    assert p1.calls == 2
    assert p2.calls == 0
