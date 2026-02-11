from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from miniclaw.compliance.service import ComplianceService
from miniclaw.config.schema import Config
from miniclaw.utils.helpers import workspace_scope_id
from miniclaw.usage import UsageTracker


def test_usage_tracker_aggregates_tokens_and_cost(tmp_path: Path) -> None:
    tracker = UsageTracker(
        store_path=tmp_path / "usage" / "events.jsonl",
        pricing={
            "fake/model": {
                "input_per_1m_tokens_usd": 2.0,
                "output_per_1m_tokens_usd": 4.0,
            }
        },
        aggregation_windows=["1h", "1d"],
    )
    tracker.record(
        source="agent",
        model="fake/model",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        session_key="cli:test",
    )

    summary = tracker.summary()
    totals = summary["overall"]["totals"]
    assert totals["events"] == 1
    assert totals["total_tokens"] == 1500
    assert totals["cost_usd"] == 0.004
    assert "1h" in summary["windows"]


def test_compliance_sweep_export_and_targeted_purge(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    workspace.mkdir(parents=True, exist_ok=True)
    (data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "identity").mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)

    now = time.time()
    old_age_s = 45 * 86400
    session_prefix = f"{workspace_scope_id(workspace)}__"

    old_session = data_dir / "sessions" / f"{session_prefix}cli_old.jsonl"
    old_session.write_text('{"_type":"metadata","updated_at":"2024-01-01T00:00:00"}\n', encoding="utf-8")
    old_session.touch()
    old_mtime = now - old_age_s

    os.utime(old_session, (old_mtime, old_mtime))

    new_session = data_dir / "sessions" / f"{session_prefix}cli_new.jsonl"
    new_session.write_text('{"_type":"metadata","updated_at":"2099-01-01T00:00:00"}\n', encoding="utf-8")

    old_run = {
        "run_id": "old",
        "session_key": "cli:old",
        "created_at": "2020-01-01T00:00:00",
    }
    new_run = {
        "run_id": "new",
        "session_key": "cli:new",
        "created_at": "2099-01-01T00:00:00",
    }
    runs_path = data_dir / "runs" / "runs.jsonl"
    runs_path.write_text(
        json.dumps(old_run) + "\n" + json.dumps(new_run) + "\n",
        encoding="utf-8",
    )

    audit_path = data_dir / "audit.log"
    audit_path.write_text(
        json.dumps({"ts": now - old_age_s, "data": {"session_key": "cli:old"}})
        + "\n"
        + json.dumps({"ts": now, "data": {"session_key": "cli:new"}})
        + "\n",
        encoding="utf-8",
    )

    (workspace / "memory" / "2000-01-01.md").write_text("old", encoding="utf-8")
    (workspace / "memory" / "2099-01-01.md").write_text("new", encoding="utf-8")

    cfg = Config()
    cfg.retention.default_days = 30
    cfg.retention.sessions_days = 30
    cfg.retention.runs_days = 30
    cfg.retention.audit_days = 30
    cfg.retention.memory_days = 30

    usage = UsageTracker(
        store_path=data_dir / "usage" / "events.jsonl",
        pricing={},
        aggregation_windows=["1d"],
    )
    usage.record(
        source="agent",
        model="fake/model",
        total_tokens=10,
        session_key="cli:purge_room",
    )

    service = ComplianceService(
        workspace=workspace,
        retention=cfg.retention,
        data_dir=data_dir,
        usage_tracker=usage,
    )

    sweep = service.sweep()
    assert sweep["removed"]["sessions"] == 1
    assert sweep["removed"]["runs"] == 1
    assert sweep["removed"]["audit"] == 1
    assert sweep["removed"]["memory"] == 1

    to_purge = data_dir / "sessions" / f"{session_prefix}cli_purge_room.jsonl"
    to_purge.write_text(
        '{"_type":"metadata","session_key":"cli:purge_room","updated_at":"2020-01-01T00:00:00"}\n',
        encoding="utf-8",
    )
    runs_path.write_text(
        json.dumps({"run_id": "purge", "session_key": "cli:purge_room", "created_at": "2020-01-01T00:00:00"}) + "\n",
        encoding="utf-8",
    )
    audit_path.write_text(
        json.dumps({"ts": now - old_age_s, "data": {"session_key": "cli:purge_room"}}) + "\n",
        encoding="utf-8",
    )

    purge = service.purge(
        session_key="cli:purge_room",
        before_date="2099-01-01",
        domains=["sessions", "runs", "audit", "usage"],
    )
    assert purge["ok"] is True
    assert purge["removed"]["sessions"] == 1
    assert purge["removed"]["runs"] == 1
    assert purge["removed"]["audit"] == 1
    assert purge["removed"]["usage"] == 1

    exported = service.export_bundle(include=["sessions", "runs", "audit", "memory", "usage"])
    assert exported["ok"] is True
    assert Path(exported["path"]).exists()

    with pytest.raises(ValueError, match="inside workspace"):
        service.export_bundle(output_path="/tmp/miniclaw-outside-export.zip")
