from pathlib import Path

from fastapi.testclient import TestClient

from miniclaw.bus.events import OutboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.channels.base import BaseChannel
from miniclaw.config.schema import Config
from miniclaw.dashboard.app import create_app
from miniclaw.identity import IdentityStore


class _DummyChannel(BaseChannel):
    name = "telegram"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        return None


def test_pairing_request_approve_and_revoke_endpoints(tmp_path: Path) -> None:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    identity_store = IdentityStore(tmp_path / "identity" / "state.json")

    app = create_app(
        config=config,
        config_path=tmp_path / "config.json",
        token="t",
        identity_store=identity_store,
    )
    client = TestClient(app)

    request_resp = client.post(
        "/api/pairing/request",
        headers={"Authorization": "Bearer t"},
        json={
            "platform": "telegram",
            "platform_user_id": "12345",
            "device_id": "device-a",
            "display_name": "Jude Phone",
        },
    )
    assert request_resp.status_code == 200
    req = request_resp.json()
    assert req["ok"] is True
    assert req["request_id"]
    assert len(req["code"]) == 6

    approve = client.post(
        "/api/pairing/approve",
        headers={"Authorization": "Bearer t"},
        json={
            "request_id": req["request_id"],
            "code": req["code"],
            "canonical_user_id": "owner",
        },
    )
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["ok"] is True
    assert payload["pairing"]["status"] == "active"

    resolved = identity_store.resolve_canonical("telegram", "12345")
    assert resolved == "owner"

    revoke = client.post(
        "/api/pairing/revoke",
        headers={"Authorization": "Bearer t"},
        json={"pairing_id": payload["pairing"]["id"]},
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] == 1
    assert identity_store.resolve_canonical("telegram", "12345") is None


async def test_channel_uses_canonical_identity_for_sender_and_session(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "identity" / "state.json")
    store.link_identity(
        canonical_user_id="owner",
        platform="telegram",
        platform_user_id="42",
    )
    bus = MessageBus()
    cfg = type("C", (), {"allow_from": ["42"]})()
    channel = _DummyChannel(cfg, bus, identity_store=store)

    await channel._handle_message(
        sender_id="42|username",
        chat_id="chat-1",
        content="hello",
    )
    msg = await bus.consume_inbound()
    assert msg.sender_id == "owner"
    assert msg.metadata.get("canonical_user_id") == "owner"
    assert msg.metadata.get("session_key") == "user:owner"
