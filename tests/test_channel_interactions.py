import asyncio
import json

import pytest

from miniclaw.bus.events import OutboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.channels.telegram import TelegramChannel
from miniclaw.channels.whatsapp import WhatsAppChannel
from miniclaw.config.schema import TelegramConfig, WhatsAppConfig


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.chat_actions: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)

    async def send_chat_action(self, **kwargs):
        self.chat_actions.append(kwargs)


class _FakeTelegramApp:
    def __init__(self) -> None:
        self.bot = _FakeTelegramBot()


class _FakeWs:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send(self, data: str) -> None:
        self.payloads.append(json.loads(data))


class _NoopTranscriptionManager:
    async def transcribe(self, _file_path: str) -> str:
        return ""


async def test_telegram_send_wires_reply_to_message_id() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="x", allow_from=["1"]),
        MessageBus(),
    )
    channel._app = _FakeTelegramApp()
    channel._running = True

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="hello",
            reply_to="42",
        )
    )

    sent = channel._app.bot.sent_messages
    assert len(sent) == 1
    assert sent[0]["reply_to_message_id"] == 42
    assert sent[0]["allow_sending_without_reply"] is True


async def test_telegram_typing_control_start_and_stop() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="x", allow_from=["1"]),
        MessageBus(),
    )
    channel._app = _FakeTelegramApp()
    channel._running = True
    channel._typing_interval_s = 0.01

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="",
            control="typing_start",
        )
    )
    await asyncio.sleep(0.03)
    assert 123 in channel._typing_tasks
    assert channel._app.bot.chat_actions

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="",
            control="typing_stop",
        )
    )
    await asyncio.sleep(0.01)
    assert 123 not in channel._typing_tasks


def test_telegram_command_normalization() -> None:
    assert TelegramChannel._normalize_command_text("/status") == "/status"
    assert TelegramChannel._normalize_command_text("/cancel@my_bot") == "/cancel"
    assert TelegramChannel._normalize_command_text("/think high") == "/think high"
    assert TelegramChannel._normalize_command_text("/unknown") == ""


async def test_whatsapp_send_supports_reply_and_presence_control() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(enabled=True, allow_from=["1"]), MessageBus())
    fake_ws = _FakeWs()
    channel._ws = fake_ws
    channel._connected = True

    await channel.send(
        OutboundMessage(
            channel="whatsapp",
            chat_id="abc@s.whatsapp.net",
            content="hello",
            reply_to="msg-1",
        )
    )
    await channel.send(
        OutboundMessage(
            channel="whatsapp",
            chat_id="abc@s.whatsapp.net",
            content="",
            control="typing_start",
        )
    )
    await channel.send(
        OutboundMessage(
            channel="whatsapp",
            chat_id="abc@s.whatsapp.net",
            content="",
            control="typing_stop",
        )
    )

    assert fake_ws.payloads[0] == {
        "type": "send",
        "to": "abc@s.whatsapp.net",
        "text": "hello",
        "replyTo": "msg-1",
    }
    assert fake_ws.payloads[1] == {
        "type": "presence",
        "to": "abc@s.whatsapp.net",
        "state": "composing",
    }
    assert fake_ws.payloads[2] == {
        "type": "presence",
        "to": "abc@s.whatsapp.net",
        "state": "paused",
    }


def test_whatsapp_command_normalization() -> None:
    assert WhatsAppChannel._normalize_slash_command("/status now") == "/status"
    assert WhatsAppChannel._normalize_slash_command("/reset") == "/reset"
    assert WhatsAppChannel._normalize_slash_command("/think medium") == "/think medium"
    assert WhatsAppChannel._normalize_slash_command("/think:high") == "/think:high"
    assert WhatsAppChannel._normalize_slash_command("/foo") == "/foo"


async def test_whatsapp_start_requires_bridge_auth_token() -> None:
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, bridge_auth_token="", allow_from=["1"]),
        MessageBus(),
        transcription_manager=_NoopTranscriptionManager(),
    )
    with pytest.raises(RuntimeError, match="bridge_auth_token"):
        await channel.start()


async def test_whatsapp_start_sends_bridge_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, bridge_auth_token="token-abc", allow_from=["1"]),
        MessageBus(),
        transcription_manager=_NoopTranscriptionManager(),
    )
    observed: dict = {}

    class _EmptyAsyncMessages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeConnectedWs:
        def __aiter__(self):
            return _EmptyAsyncMessages()

    class _FakeContext:
        async def __aenter__(self):
            channel._running = False
            return _FakeConnectedWs()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_connect(url: str, **kwargs):
        observed["url"] = url
        observed["kwargs"] = kwargs
        return _FakeContext()

    import websockets

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    await channel.start()

    assert observed["url"] == channel.config.bridge_url
    assert observed["kwargs"]["additional_headers"] == {"x-bridge-token": "token-abc"}
