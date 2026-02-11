import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from miniclaw.agent.skills import SkillsLoader
from miniclaw.bus.queue import MessageBus
from miniclaw.channels.whatsapp import WhatsAppChannel
from miniclaw.cli import commands as cli_commands
from miniclaw.config.schema import Config, WhatsAppConfig
from miniclaw.dashboard.app import create_app
from miniclaw.providers.transcription import TranscriptionManager, WhisperCppTranscriptionProvider
from miniclaw.secrets import SecretStore


def test_onboarding_optional_skill_checklist_selection_install_flow(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = SecretStore(namespace="test", backend="file", home=tmp_path)

    # configure? yes, then whisper/elevenlabs/nanobanana/github no, weather yes
    answers = iter([True, False, False, False, False, True])
    monkeypatch.setattr(cli_commands.typer, "confirm", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(cli_commands.typer, "prompt", lambda *_args, **_kwargs: "")

    installed = cli_commands._run_optional_skill_checklist(workspace=workspace, secret_store=store)
    assert installed == ["weather"]
    assert (workspace / "skills" / "weather" / "SKILL.md").exists()


def test_download_default_whisper_model(tmp_path, monkeypatch) -> None:
    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    model_path = tmp_path / ".miniclaw" / "models" / "whisper-small.en.bin"
    monkeypatch.setenv("MINICLAW_WHISPER_MODEL_URL", "https://example.invalid/model.bin")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(b"fake-model-bytes"),
    )

    assert cli_commands._download_default_whisper_model(model_path) is True
    assert model_path.exists()
    assert model_path.read_bytes() == b"fake-model-bytes"


def test_onboard_whisper_selection_triggers_model_download(tmp_path, monkeypatch) -> None:
    from miniclaw.config import loader as config_loader
    from miniclaw.utils import helpers as helpers_mod

    cfg_path = tmp_path / ".miniclaw" / "config.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config_loader, "get_config_path", lambda: cfg_path)
    monkeypatch.setattr(config_loader, "save_config", lambda config, _path=None: None)
    monkeypatch.setattr(helpers_mod, "get_workspace_path", lambda: workspace)
    monkeypatch.setattr(cli_commands, "_create_workspace_templates", lambda _workspace: None)
    monkeypatch.setattr(
        cli_commands,
        "_run_skill_setup_checklist",
        lambda workspace, secret_store, non_interactive=False: ["whisper-local"],
    )

    download_calls: list[Path] = []
    monkeypatch.setattr(
        cli_commands,
        "_download_default_whisper_model",
        lambda model_path: download_calls.append(Path(model_path)) or True,
    )
    monkeypatch.setattr(cli_commands.typer, "confirm", lambda *_args, **_kwargs: True)

    cli_commands.onboard(non_interactive=True)
    assert len(download_calls) == 1


def test_skill_secret_requirement_status_api(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    assert cli_commands._install_builtin_skill(workspace, "elevenlabs") is True

    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    loader = SkillsLoader(workspace=workspace, secret_store=store)
    cfg = Config(agents={"defaults": {"workspace": str(workspace)}})
    app = create_app(
        config=cfg,
        config_path=tmp_path / "config.json",
        skills_loader=loader,
        token="t",
        secret_store=store,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    skills = client.get("/api/skills", headers=headers)
    assert skills.status_code == 200
    eleven = next(s for s in skills.json() if s["name"] == "elevenlabs")
    assert eleven["secret_requirements"]["required"] == ["ELEVENLABS_API_KEY"]
    assert eleven["secret_requirements"]["missing"] == ["ELEVENLABS_API_KEY"]

    before = client.get("/api/skills/elevenlabs/secrets", headers=headers)
    assert before.status_code == 200
    assert before.json()["values"]["ELEVENLABS_API_KEY"] is False

    write = client.put(
        "/api/skills/elevenlabs/secrets",
        headers=headers,
        json={"secrets": {"ELEVENLABS_API_KEY": "secret-value"}},
    )
    assert write.status_code == 200
    assert write.json()["values"]["ELEVENLABS_API_KEY"] is True


async def test_whisper_cpp_provider_success(tmp_path) -> None:
    cli = tmp_path / "fake-whisper-cli"
    cli.write_text(
        "#!/bin/sh\n"
        "OUT=\"\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-of\" ]; then OUT=\"$2\"; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        "echo \"hello from whisper\" > \"${OUT}.txt\"\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)

    model = tmp_path / "whisper-small.en.bin"
    model.write_bytes(b"model")
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"audio")

    provider = WhisperCppTranscriptionProvider(cli=str(cli), model_path=model)
    assert provider.is_available() is True
    text = await provider.transcribe(audio)
    assert "hello from whisper" in text


async def test_whisper_cpp_provider_missing_binary_or_model(tmp_path) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"audio")

    missing_bin = WhisperCppTranscriptionProvider(cli="definitely-not-installed", model_path=tmp_path / "m.bin")
    assert missing_bin.is_available() is False
    assert "missing binary" in missing_bin.missing_reason()
    assert await missing_bin.transcribe(audio) == ""

    missing_model = WhisperCppTranscriptionProvider(cli="/bin/echo", model_path=tmp_path / "m.bin")
    assert missing_model.is_available() is False
    assert "missing model" in missing_model.missing_reason()


class _FakeLocalProvider:
    def __init__(self, available: bool, text: str) -> None:
        self._available = available
        self.text = text
        self.calls = 0

    def is_available(self) -> bool:
        return self._available

    def missing_reason(self) -> str:
        return "missing"

    async def transcribe(self, _file_path) -> str:
        self.calls += 1
        return self.text


class _FakeGroqProvider:
    def __init__(self, configured: bool, text: str) -> None:
        self._configured = configured
        self.text = text
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def transcribe(self, _file_path) -> str:
        self.calls += 1
        return self.text


async def test_transcription_manager_priority_local_then_groq() -> None:
    local = _FakeLocalProvider(available=True, text="local text")
    groq = _FakeGroqProvider(configured=True, text="groq text")
    manager = TranscriptionManager(
        local_provider=local,
        local_enabled=True,
        groq_provider=groq,
        groq_fallback=True,
    )
    text = await manager.transcribe("x.wav")
    assert text == "local text"
    assert local.calls == 1
    assert groq.calls == 0

    local2 = _FakeLocalProvider(available=False, text="")
    groq2 = _FakeGroqProvider(configured=True, text="groq text")
    manager2 = TranscriptionManager(
        local_provider=local2,
        local_enabled=True,
        groq_provider=groq2,
        groq_fallback=True,
    )
    text2 = await manager2.transcribe("x.wav")
    assert text2 == "groq text"
    assert local2.calls == 0
    assert groq2.calls == 1


class _FakeTranscriptionManager:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def transcribe(self, file_path: str | Path) -> str:
        self.calls.append(str(file_path))
        return self.text


async def test_whatsapp_audio_transcription_flow(tmp_path) -> None:
    manager = _FakeTranscriptionManager("voice transcript")
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, allow_from=["123"]),
        bus=MessageBus(),
        transcription_manager=manager,
    )

    captured: dict = {}

    async def _capture(sender_id, chat_id, content, media=None, metadata=None):
        captured["sender_id"] = sender_id
        captured["chat_id"] = chat_id
        captured["content"] = content
        captured["media"] = media or []
        captured["metadata"] = metadata or {}

    channel._handle_message = _capture  # type: ignore[method-assign]

    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"audio")
    raw = json.dumps(
        {
            "type": "message",
            "id": "m1",
            "sender": "123@s.whatsapp.net",
            "pn": "",
            "content": "[Voice Message]",
            "mediaPath": str(audio_file),
            "mediaType": "audio",
            "timestamp": 1,
            "isGroup": False,
        }
    )
    await channel._handle_bridge_message(raw)

    assert captured["sender_id"] == "123"
    assert captured["chat_id"] == "123@s.whatsapp.net"
    assert captured["content"] == "[transcription: voice transcript]"
    assert captured["media"] == [str(audio_file)]
    assert manager.calls == [str(audio_file)]
