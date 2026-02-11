from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from miniclaw.config.schema import Config
from miniclaw.dashboard.app import create_app
from miniclaw.monitoring.alerts import AlertService


class _Bus:
    def list_pending_approvals(self):
        return []

    def submit_response(self, session_key: str, content: str, approval_id: str | None = None):
        return False


class _RunBus(_Bus):
    def __init__(self):
        self.q = asyncio.Queue()

    def register_run_listener(self):
        return self.q

    def unregister_run_listener(self, q):
        return None


class _FakeUsage:
    def summary(self, windows=None):
        return {
            "overall": {"totals": {"events": 2, "total_tokens": 123, "cost_usd": 0.12}},
            "windows": {"1d": {"totals": {"events": 1, "total_tokens": 12, "cost_usd": 0.02}}},
        }


class _FakeCompliance:
    def export_bundle(self, *, include=None, output_path=None):
        return {"ok": True, "path": "/tmp/export.zip", "domains": include or []}

    def purge(self, *, session_key=None, user_id=None, before_date=None, domains=None):
        return {"ok": True, "removed": {"sessions": 1, "runs": 0, "audit": 0, "memory": 0, "usage": 0}}

    def sweep(self):
        return {"ok": True, "removed": {"sessions": 0, "runs": 0, "audit": 0, "memory": 0}}


class _FakeAlerts:
    def __init__(self):
        self.scanned = False

    def scan_health(self):
        self.scanned = True

    def list_events(self, limit: int = 100):
        return [{"event": "backlog_overflow", "message": "overflow"}]

    def summary(self):
        return {"enabled": True, "total": 1, "by_event": {"backlog_overflow": 1}}


def test_dashboard_phase4_endpoints() -> None:
    app = create_app(
        config=Config(),
        config_path=Path("/tmp/miniclaw-config.json"),
        token="t",
        bus=_Bus(),
        usage_tracker=_FakeUsage(),
        compliance_service=_FakeCompliance(),
        alert_service=_FakeAlerts(),
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    usage = client.get("/api/usage/summary", headers=headers)
    assert usage.status_code == 200
    assert usage.json()["overall"]["totals"]["total_tokens"] == 123

    exported = client.post("/api/data/export", headers=headers, json={"include": ["sessions", "runs"]})
    assert exported.status_code == 200
    assert exported.json()["ok"] is True

    purged = client.post(
        "/api/data/purge",
        headers=headers,
        json={"session_key": "cli:test", "before_date": "2099-01-01"},
    )
    assert purged.status_code == 200
    assert purged.json()["removed"]["sessions"] == 1

    alerts = client.get("/api/alerts", headers=headers)
    assert alerts.status_code == 200
    assert alerts.json()["summary"]["total"] == 1


async def test_alert_service_detects_health_failures() -> None:
    cfg = Config()
    cfg.alerts.enabled = True
    run_bus = _RunBus()
    service = AlertService(cfg.alerts, dedupe_window_s=1, health_interval_s=999)

    cron_job = SimpleNamespace(
        id="job-1",
        name="daily",
        state=SimpleNamespace(last_status="error", last_error="boom", last_run_at_ms=10),
    )
    cron = SimpleNamespace(list_jobs=lambda include_disabled=True: [cron_job])
    channels = SimpleNamespace(get_status=lambda: {"telegram": {"enabled": True, "running": False}})
    distributed = SimpleNamespace(list_nodes=lambda include_stale=True: [{"node_id": "node-1", "alive": False, "last_heartbeat_ms": 5}])
    agent = SimpleNamespace(
        get_queue_snapshot=lambda: {
            "max_backlog": 1,
            "sessions": [{"session_key": "cli:q", "queued": [{"run_id": "r1"}], "running": None}],
        }
    )

    await service.start(
        bus=run_bus,
        agent_loop=agent,
        cron_service=cron,
        channels_manager=channels,
        distributed_manager=distributed,
    )
    run_bus.q.put_nowait(
        {
            "type": "queue_update",
            "reason": "overflow_replace",
            "session_key": "cli:q",
            "run_id": "r1",
        }
    )
    await asyncio.sleep(0.05)
    service.scan_health()
    events = service.list_events(limit=20)
    await service.stop()

    names = {item.get("event") for item in events}
    assert "backlog_overflow" in names
    assert "channel_disconnected" in names
    assert "cron_failure" in names
    assert "node_failure" in names
