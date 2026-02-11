from pathlib import Path

from fastapi.testclient import TestClient

from miniclaw.api.openai_compat import create_openai_compat_app
from miniclaw.config.schema import Config
from miniclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniclaw.providers.tts import KokoroTTSAdapter


class FakeProvider(LLMProvider):
    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ):
        if tools:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="echo", arguments={"value": "ok"})],
            )
        return LLMResponse(content="hello from provider", usage={"total_tokens": 12})

    async def embed(self, texts, model=None):
        return [[0.1, 0.2, 0.3] for _ in texts]

    def get_default_model(self) -> str:
        return "fake/model"


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def process_direct(
        self,
        content: str,
        session_key: str = "openai:responses",
        channel: str = "openai",
        chat_id: str = "responses",
        model_override: str | None = None,
    ) -> str:
        self.calls.append((content, session_key, channel, chat_id, model_override))
        return f"agent:{content}"


class FakeTranscriber:
    async def transcribe(self, file_path):
        return "voice text"


def _build_client(tmp_path: Path) -> tuple[TestClient, FakeAgent]:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.api.openai_compat.auth_token = "token-123"
    config.transcription.tts.output_dir = str(tmp_path / "tts")
    config.tools.restrict_to_workspace = True

    agent = FakeAgent()
    app = create_openai_compat_app(
        config=config,
        provider=FakeProvider(),
        agent_runtime=agent,
        transcription_manager=FakeTranscriber(),
        tts_adapter=KokoroTTSAdapter(output_dir=tmp_path / "tts"),
    )
    return TestClient(app), agent


def test_models_chat_responses_and_embeddings(tmp_path: Path) -> None:
    client, agent = _build_client(tmp_path)
    headers = {"Authorization": "Bearer token-123"}

    models = client.get("/v1/models", headers=headers)
    assert models.status_code == 200
    assert models.json()["data"][0]["object"] == "model"

    chat = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "fake/model",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}],
        },
    )
    assert chat.status_code == 200
    body = chat.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "echo"

    responses = client.post(
        "/v1/responses",
        headers=headers,
        json={"input": "run via runtime", "model": "fake/model"},
    )
    assert responses.status_code == 200
    assert responses.json()["output"][0]["content"][0]["text"] == "agent:run via runtime"
    assert agent.calls

    embeddings = client.post(
        "/v1/embeddings",
        headers=headers,
        json={"input": ["a", "b"], "model": "fake/emb"},
    )
    assert embeddings.status_code == 200
    assert len(embeddings.json()["data"]) == 2


def test_audio_transcription_and_speech_path_restrictions(tmp_path: Path) -> None:
    client, _agent = _build_client(tmp_path)
    headers = {"Authorization": "Bearer token-123"}

    transcription = client.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": ("voice.wav", b"fake-audio", "audio/wav")},
        data={"model": "whisper-1"},
    )
    assert transcription.status_code == 200
    assert transcription.json()["text"] == "voice text"

    denied = client.post(
        "/v1/audio/speech",
        headers=headers,
        json={"input": "hello", "output_path": "/tmp/outside.wav"},
    )
    assert denied.status_code == 400

    allowed = client.post(
        "/v1/audio/speech",
        headers=headers,
        json={"input": "hello", "output_path": "speech/out.wav"},
    )
    assert allowed.status_code == 200
    output_path = Path(allowed.json()["output_path"])
    assert output_path.exists()


def test_openai_compat_requires_configured_auth_token(tmp_path: Path) -> None:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.api.openai_compat.auth_token = ""
    config.transcription.tts.output_dir = str(tmp_path / "tts")

    app = create_openai_compat_app(
        config=config,
        provider=FakeProvider(),
        agent_runtime=FakeAgent(),
        transcription_manager=FakeTranscriber(),
        tts_adapter=KokoroTTSAdapter(output_dir=tmp_path / "tts"),
    )
    client = TestClient(app)

    denied = client.get("/v1/models")
    assert denied.status_code == 503
    assert "auth token is not configured" in denied.json().get("detail", "").lower()


def test_audio_transcription_rejects_file_over_size_limit(tmp_path: Path) -> None:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.api.openai_compat.auth_token = "token-123"
    config.api.openai_compat.max_audio_upload_bytes = 1024
    config.transcription.tts.output_dir = str(tmp_path / "tts")

    app = create_openai_compat_app(
        config=config,
        provider=FakeProvider(),
        agent_runtime=FakeAgent(),
        transcription_manager=FakeTranscriber(),
        tts_adapter=KokoroTTSAdapter(output_dir=tmp_path / "tts"),
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-123"}

    too_large = client.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": ("voice.wav", b"a" * 2048, "audio/wav")},
        data={"model": "whisper-1"},
    )
    assert too_large.status_code == 413
