import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from miniclaw.api.webhooks import WebhookService, create_webhook_router
from miniclaw.config.schema import Config


class FakeAgentRuntime:
    def __init__(self):
        self.messages = []

    async def process_direct(
        self,
        content: str,
        session_key: str = "webhook:test",
        channel: str = "webhook",
        chat_id: str = "source",
        model_override: str | None = None,
    ) -> str:
        self.messages.append((content, session_key, channel, chat_id, model_override))
        return "ok"


def _sign(secret: str, ts: int, body: bytes) -> str:
    payload = f"{ts}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_signature_validation_dedupe_and_event_lookup() -> None:
    config = Config()
    config.webhooks.enabled = True
    config.webhooks.secret_refs = {"github": "plain-secret"}
    config.webhooks.allowed_events = ["push"]
    config.webhooks.rules = [
        config.webhooks.ActionRule(
            source="github",
            event="push",
            mode="agent",
            message_template="event={event}",
            session_key="webhook:{source}",
        )
    ]
    runtime = FakeAgentRuntime()
    service = WebhookService(config=config, agent_runtime=runtime)
    app = FastAPI()
    app.include_router(create_webhook_router(service=service))
    client = TestClient(app)

    body = json.dumps({"hello": "world"}).encode("utf-8")
    ts = int(time.time())
    headers = {
        "x-event-type": "push",
        "x-event-id": "evt-1",
        "x-timestamp": str(ts),
        "x-signature": _sign("plain-secret", ts, body),
        "content-type": "application/json",
    }

    ok = client.post("/api/webhooks/github", data=body, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["id"] == "evt-1"
    assert runtime.messages

    duplicate = client.post("/api/webhooks/github", data=body, headers=headers)
    assert duplicate.status_code == 409

    bad_headers = dict(headers)
    bad_headers["x-event-id"] = "evt-2"
    bad_headers["x-signature"] = "sha256=deadbeef"
    bad = client.post("/api/webhooks/github", data=body, headers=bad_headers)
    assert bad.status_code == 401

    lookup = client.get("/api/webhooks/events/evt-1")
    assert lookup.status_code == 200
    assert lookup.json()["event"] == "push"


def test_webhook_rejects_when_secret_not_configured() -> None:
    config = Config()
    config.webhooks.enabled = True
    config.webhooks.secret_refs = {}
    config.webhooks.allowed_events = ["push"]
    config.webhooks.rules = [
        config.webhooks.ActionRule(
            source="github",
            event="push",
            mode="agent",
        )
    ]
    runtime = FakeAgentRuntime()
    service = WebhookService(config=config, agent_runtime=runtime)
    app = FastAPI()
    app.include_router(create_webhook_router(service=service))
    client = TestClient(app)

    body = json.dumps({"hello": "world"}).encode("utf-8")
    headers = {
        "x-event-type": "push",
        "x-event-id": "evt-no-secret",
        "content-type": "application/json",
    }
    denied = client.post("/api/webhooks/github", data=body, headers=headers)
    assert denied.status_code == 401
    assert "secret" in denied.json().get("detail", "").lower()
