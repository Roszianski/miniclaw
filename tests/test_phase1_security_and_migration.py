import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from miniclaw.audit.logger import AuditLogger
from miniclaw.config.loader import _migrate_config
from miniclaw.config.schema import Config
from miniclaw.session.manager import Session, SessionManager


def test_secure_defaults_enabled_in_schema() -> None:
    cfg = Config()
    assert cfg.channels.whatsapp.bridge_url == "ws://127.0.0.1:3001"
    assert cfg.channels.whatsapp.bridge_host == "127.0.0.1"
    assert cfg.tools.sandbox.mode == "all"
    assert cfg.tools.sandbox.scope == "agent"
    assert cfg.tools.sandbox.workspace_access == "rw"
    assert cfg.tools.restrict_to_workspace is True
    assert cfg.tools.approval_profile == "coding"
    assert cfg.tools.approval.exec == "always_ask"
    assert cfg.tools.approval.write_file == "always_ask"
    assert cfg.retention.default_days == 60


def test_config_migration_backfills_secure_defaults_and_new_surfaces() -> None:
    raw = {
        "tools": {"exec": {"timeout": 30}},
        "agents": {"defaults": {"queue": {"global": True}}},
    }
    migrated = _migrate_config(raw)
    tools = migrated["tools"]
    assert tools["sandbox"]["mode"] == "all"
    assert tools["sandbox"]["scope"] == "agent"
    assert tools["sandbox"]["workspaceAccess"] == "rw"
    assert tools["restrictToWorkspace"] is True
    assert tools["approvalProfile"] == "coding"
    assert tools["approval"]["exec"] == "always_ask"

    queue = migrated["agents"]["defaults"]["queue"]
    assert queue["mode"] == "queue"
    assert queue["collectWindowMs"] == 1200
    assert queue["maxBacklog"] == 8
    assert migrated["channels"]["whatsapp"]["bridgeUrl"] == "ws://127.0.0.1:3001"
    assert migrated["channels"]["whatsapp"]["bridgeHost"] == "127.0.0.1"
    assert migrated["channels"]["whatsapp"]["bridgeAuthToken"] == ""

    assert migrated["api"]["openaiCompat"]["rateLimits"]["requestsPerMinute"] == 120
    assert migrated["api"]["openaiCompat"]["maxAudioUploadBytes"] == 25 * 1024 * 1024
    assert migrated["retention"]["defaultDays"] == 60
    assert migrated["plugins"]["manifestRequired"] is True
    assert migrated["providers"]["failover"]["enabled"] is True
    assert migrated["workflows"]["enabled"] is True
    assert migrated["transcription"]["tts"]["engine"] == "kokoro"
    assert migrated["identity"]["enabled"] is True
    assert migrated["distributed"]["heartbeatTimeoutS"] == 90


def test_config_migration_overrides_insecure_tool_flags() -> None:
    raw = {
        "tools": {
            "sandbox": False,
            "restrictToWorkspace": False,
            "approvalProfile": "messaging",
        }
    }
    migrated = _migrate_config(raw)
    tools = migrated["tools"]
    assert tools["sandbox"]["mode"] == "off"
    assert tools["restrictToWorkspace"] is True
    assert tools["approvalProfile"] == "messaging"


def test_audit_logger_redacts_sensitive_patterns(tmp_path) -> None:
    log_path = tmp_path / "audit.log"
    logger = AuditLogger(log_path=log_path, level="verbose")

    logger.log_tool(
        tool_name="exec",
        params={
            "api_key": "sk-secret",
            "nested": {"token": "abc123"},
            "plain": "password=hidden",
        },
        result="Authorization: Bearer sk-live-token",
        duration_ms=5.1,
        success=True,
    )

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows
    entry = rows[0]
    params = entry.get("params", {})
    assert params.get("api_key") == "<redacted:sensitive>"
    assert params.get("nested", {}).get("token") == "<redacted:sensitive>"
    assert "<redacted:sensitive>" in str(params.get("plain"))
    assert "sk-live-token" not in str(entry.get("result"))


def test_session_manager_idle_reset_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(workspace=workspace, idle_reset_minutes=5)

    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")
    session.summary = "old summary"
    session.updated_at = datetime.now() - timedelta(minutes=30)
    manager.save(session)

    reset = manager.apply_idle_reset(session)
    assert reset is True
    assert session.messages == []
    assert session.summary == ""
    assert "idle_reset_at" in session.metadata


def test_session_manager_bulk_reset_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(workspace=workspace, idle_reset_minutes=0)

    first = manager.get_or_create("cli:first")
    first.add_message("user", "hello")
    manager.save(first)

    second = manager.get_or_create("cli:second")
    second.add_message("assistant", "world")
    second.summary = "summary"
    manager.save(second)

    reset_count = manager.reset_all(reason="scheduled_cron", actor="cron")
    assert reset_count == 2

    first_after = manager.get_or_create("cli:first")
    second_after = manager.get_or_create("cli:second")
    assert first_after.messages == []
    assert second_after.messages == []
    assert first_after.summary == ""
    assert second_after.summary == ""
    assert first_after.metadata.get("bulk_reset_reason") == "scheduled_cron"
    assert second_after.metadata.get("bulk_reset_actor") == "cron"


def test_session_manager_recovers_from_backup_when_primary_corrupted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    manager = SessionManager(workspace=workspace)
    session = manager.get_or_create("cli:recover")
    session.add_message("user", "v1")
    manager.save(session)
    session.add_message("assistant", "v2")
    manager.save(session)

    session_path = manager._get_session_path("cli:recover")
    session_path.write_text("{\"_type\":\"metadata\"\n", encoding="utf-8")

    recovered_manager = SessionManager(workspace=workspace)
    recovered = recovered_manager.get_or_create("cli:recover")
    assert len(recovered.messages) == 1
    assert recovered.messages[0]["content"] == "v1"

    first_line = session_path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["_type"] == "metadata"


def test_session_manager_serializes_concurrent_saves(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(workspace=workspace)

    state = {"active": 0, "max_active": 0}
    state_lock = threading.Lock()
    original_write = manager._write_session_payload

    def wrapped_write(path: Path, payload: str) -> None:
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        original_write(path, payload)
        with state_lock:
            state["active"] -= 1

    monkeypatch.setattr(manager, "_write_session_payload", wrapped_write)

    session_key = "cli:concurrent"
    session_a = Session(key=session_key)
    session_a.add_message("user", "a")
    session_b = Session(key=session_key)
    session_b.add_message("assistant", "b")

    go = threading.Event()

    def _save(session: Session) -> None:
        go.wait()
        manager.save(session)

    threads = [
        threading.Thread(target=_save, args=(session_a,)),
        threading.Thread(target=_save, args=(session_b,)),
    ]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=2.0)

    assert state["max_active"] == 1

    path = manager._get_session_path(session_key)
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows
    for row in rows:
        assert isinstance(json.loads(row), dict)
