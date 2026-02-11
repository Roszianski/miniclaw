from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from miniclaw.config.schema import Config
from miniclaw.dashboard.app import create_app


class DummyAgent:
    def list_runs(self, limit: int = 100):
        return [{"run_id": "r1", "session_key": "cli:test", "status": "running"}]

    def get_queue_snapshot(self):
        return {
            "mode": "steer",
            "collect_window_ms": 1000,
            "max_backlog": 8,
            "sessions": [{"session_key": "cli:test", "running": {"run_id": "r1"}, "queued": []}],
        }

    def cancel_run(self, run_id: str) -> bool:
        return run_id == "r1"

    def steer_run(self, run_id: str, instruction: str, *, source: str = "api", sender_id: str | None = None) -> bool:
        return run_id == "r1" and bool(instruction)

    async def process_direct(self, content: str, session_key: str = "dashboard:web", channel: str = "dashboard", chat_id: str = "web") -> str:
        return "ok"


class FakeRunBus:
    def __init__(self):
        self._q = None
        self.unregistered = False

    def register_run_listener(self):
        import asyncio

        self._q = asyncio.Queue()
        self._q.put_nowait({"type": "run_start", "run_id": "r1"})
        return self._q

    def unregister_run_listener(self, q):
        self.unregistered = True

    # Dashboard also references approval APIs in other routes.
    def list_pending_approvals(self):
        return []

    def submit_response(self, session_key: str, content: str, approval_id: str | None = None):
        return False


class FakeCronService:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"enabled": False, "jobs": 0}

    def list_jobs(self, include_disabled: bool = False):
        return []

    def add_job(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="job-1")

    def remove_job(self, job_id: str):
        return True

    def enable_job(self, job_id: str, enabled: bool = True):
        return SimpleNamespace(id=job_id, enabled=enabled)


class FakeProcessManager:
    def __init__(self):
        self.started = []
        self.stopped = []

    def list_processes(self):
        return [{"id": "p1", "running": True}]

    def start_process(self, command: str, cwd: str | None = None, name: str | None = None):
        row = {"id": "p2", "command": command, "cwd": cwd or "", "name": name or ""}
        self.started.append(row)
        return row

    def stop_process(self, process_id: str):
        self.stopped.append(process_id)
        return process_id == "p2"

    def read_logs(self, process_id: str, tail_lines: int = 200):
        if process_id != "p2":
            raise KeyError("missing")
        return "hello log"


class FakeIdentityStore:
    def create_pairing_request(
        self,
        *,
        platform: str,
        platform_user_id: str,
        device_id: str = "",
        display_name: str = "",
        expires_in_s: int = 600,
        metadata: dict | None = None,
    ):
        del device_id, display_name, expires_in_s, metadata
        return {
            "id": "req-1",
            "code": "123456",
            "expires_at_ms": 1_700_000_000_000,
            "platform": platform,
            "platform_user_id": platform_user_id,
        }



def test_runs_api_and_cancel_endpoint() -> None:
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        agent_loop=DummyAgent(),
        token="t",
        bus=FakeRunBus(),
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    runs = client.get("/api/runs", headers=headers)
    assert runs.status_code == 200
    assert runs.json()[0]["run_id"] == "r1"

    cancel = client.post("/api/runs/r1/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json() == {"ok": True}

    queue = client.get("/api/runs/queue", headers=headers)
    assert queue.status_code == 200
    assert queue.json()["mode"] == "steer"
    assert queue.json()["sessions"][0]["session_key"] == "cli:test"

    steer = client.post("/api/runs/r1/steer", headers=headers, json={"instruction": "focus on summary"})
    assert steer.status_code == 200
    assert steer.json() == {"ok": True}



def test_ws_runs_streams_events_and_unregisters_listener() -> None:
    fake_bus = FakeRunBus()
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        agent_loop=DummyAgent(),
        token="t",
        bus=fake_bus,
    )
    client = TestClient(app)

    with client.websocket_connect("/ws/runs?token=t") as ws:
        event = ws.receive_json()
        assert event["type"] == "run_start"
        assert event["run_id"] == "r1"

    assert fake_bus.unregistered is True


def test_cron_endpoint_rejects_bad_retry_input_with_400() -> None:
    cron = FakeCronService()
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        token="t",
        bus=FakeRunBus(),
        cron_service=cron,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    bad_attempts = client.post(
        "/api/cron",
        headers=headers,
        json={
            "message": "run task",
            "every_seconds": 10,
            "retry_max_attempts": "NaN",
        },
    )
    assert bad_attempts.status_code == 400
    assert "retry_max_attempts" in bad_attempts.json().get("error", "")

    bad_backoff = client.post(
        "/api/cron",
        headers=headers,
        json={
            "message": "run task",
            "every_seconds": 10,
            "retry_backoff_ms": "oops",
        },
    )
    assert bad_backoff.status_code == 400
    assert "retry_backoff_ms" in bad_backoff.json().get("error", "")


def test_process_api_start_stop_list_and_logs() -> None:
    processes = FakeProcessManager()
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        token="t",
        bus=FakeRunBus(),
        process_manager=processes,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    listed = client.get("/api/processes", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "p1"

    missing_command = client.post("/api/processes/start", headers=headers, json={})
    assert missing_command.status_code == 400

    started = client.post(
        "/api/processes/start",
        headers=headers,
        json={"command": "echo hi", "cwd": "/tmp", "name": "demo"},
    )
    assert started.status_code == 200
    assert started.json()["ok"] is True
    assert started.json()["process"]["id"] == "p2"

    logs = client.get("/api/processes/p2/logs", headers=headers)
    assert logs.status_code == 200
    assert logs.json()["logs"] == "hello log"

    stop = client.post("/api/processes/stop", headers=headers, json={"id": "p2"})
    assert stop.status_code == 200
    assert stop.json() == {"ok": True}


def test_pairing_request_requires_auth_and_rejects_query_token() -> None:
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        token="t",
        bus=FakeRunBus(),
        identity_store=FakeIdentityStore(),
    )
    client = TestClient(app)

    unauthorized = client.post(
        "/api/pairing/request",
        json={"platform": "telegram", "platform_user_id": "u1"},
    )
    assert unauthorized.status_code == 401

    query_token = client.post(
        "/api/pairing/request?token=t",
        json={"platform": "telegram", "platform_user_id": "u1"},
    )
    assert query_token.status_code == 401

    authorized = client.post(
        "/api/pairing/request",
        headers={"Authorization": "Bearer t"},
        json={"platform": "telegram", "platform_user_id": "u1"},
    )
    assert authorized.status_code == 200
    assert authorized.json()["ok"] is True


def test_plugin_endpoints_reject_invalid_name_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    plugin_src = tmp_path / "plugin-src"
    plugin_src.mkdir(parents=True, exist_ok=True)
    (plugin_src / "plugin.json").write_text('{"name":"demo"}', encoding="utf-8")

    cfg = Config()
    cfg.agents.defaults.workspace = str(workspace)

    app = create_app(
        config=cfg,
        config_path=tmp_path / "config.json",
        token="t",
        bus=FakeRunBus(),
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    install = client.post(
        "/api/plugins/install",
        headers=headers,
        json={"source": str(plugin_src), "name": ".."},
    )
    assert install.status_code == 400

    remove = client.delete("/api/plugins/%2E%2E", headers=headers)
    assert remove.status_code == 400
