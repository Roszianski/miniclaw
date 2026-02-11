from pathlib import Path
from types import SimpleNamespace

from miniclaw.cli import commands as cli_commands
from miniclaw.session.manager import SessionManager


class _FakeCronService:
    def __init__(self):
        self.jobs = []
        self._counter = 0

    def list_jobs(self, include_disabled: bool = False):
        return list(self.jobs)

    def add_job(self, **kwargs):
        self._counter += 1
        job = SimpleNamespace(
            id=f"job-{self._counter}",
            name=kwargs["name"],
            enabled=True,
            schedule=kwargs["schedule"],
            payload=SimpleNamespace(
                kind=kwargs.get("kind", "task"),
                message=kwargs.get("message", ""),
            ),
        )
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str):
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.id != job_id]
        return len(self.jobs) < before


def test_reconcile_scheduled_session_reset_adds_job_when_enabled() -> None:
    cron = _FakeCronService()
    cli_commands._reconcile_scheduled_session_reset_job(cron, "0 5 * * *")

    assert len(cron.jobs) == 1
    job = cron.jobs[0]
    assert job.name == "system:session_reset"
    assert job.schedule.kind == "cron"
    assert job.schedule.expr == "0 5 * * *"
    assert job.payload.kind == "session_reset"


def test_reconcile_scheduled_session_reset_removes_job_when_disabled() -> None:
    cron = _FakeCronService()
    cli_commands._reconcile_scheduled_session_reset_job(cron, "0 5 * * *")
    assert len(cron.jobs) == 1

    cli_commands._reconcile_scheduled_session_reset_job(cron, "")
    assert cron.jobs == []


def test_run_scheduled_session_reset_resets_once_for_persisted_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    manager_primary = SessionManager(workspace=workspace)
    manager_secondary = SessionManager(workspace=workspace)

    session = manager_primary.get_or_create("cli:reset")
    session.add_message("user", "hello")
    manager_primary.save(session)

    # Load into secondary cache so include_persisted=False still clears in-memory state.
    _ = manager_secondary.get_or_create("cli:reset")

    loops = {
        "default": SimpleNamespace(sessions=manager_primary),
        "helper": SimpleNamespace(sessions=manager_secondary),
    }
    reset_count = cli_commands._run_scheduled_session_reset(loops)
    assert reset_count == 1

    first = manager_primary.get_or_create("cli:reset")
    second = manager_secondary.get_or_create("cli:reset")
    assert first.messages == []
    assert second.messages == []
    assert first.metadata.get("bulk_reset_reason") == "scheduled_cron"


def test_session_manager_isolates_sessions_by_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir(parents=True, exist_ok=True)
    workspace_b.mkdir(parents=True, exist_ok=True)

    manager_a = SessionManager(workspace=workspace_a)
    manager_b = SessionManager(workspace=workspace_b)

    session_a = manager_a.get_or_create("cli:shared_room")
    session_a.add_message("user", "hello from a")
    manager_a.save(session_a)

    listed_a = manager_a.list_sessions()
    listed_b = manager_b.list_sessions()
    assert len(listed_a) == 1
    assert listed_a[0]["key"] == "cli:shared_room"
    assert listed_b == []
