import json
from pathlib import Path

from miniclaw.cli import commands as cli_commands
from miniclaw.cli.onboarding import save_onboarding_state
from miniclaw.config.schema import Config


class _FakeSecretStore:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str) -> bool:
        if not self.ok:
            return False
        self.values[key] = value
        return True


def _empty_report() -> dict:
    return {
        "mode": "guided",
        "host": "macos_local",
        "configured": [],
        "skipped": [],
        "checks": [],
        "auto_fixes": [],
        "next_steps": [],
    }


def test_configure_telegram_channel_uses_secret_store_when_available(monkeypatch) -> None:
    config = Config()
    report = _empty_report()
    store = _FakeSecretStore(ok=True)
    monkeypatch.setenv("MINICLAW_TELEGRAM_TOKEN", "abc123")
    monkeypatch.setattr(cli_commands, "_validate_telegram_token", lambda _token: (True, "ok"))

    ok = cli_commands._configure_telegram_channel(
        config,
        secret_store=store,
        non_interactive=True,
        advanced_mode=False,
        report=report,
    )

    assert ok is True
    assert config.channels.telegram.enabled is True
    assert config.channels.telegram.token == ""
    assert store.values["channels:telegram:token"] == "abc123"


def test_configure_telegram_channel_falls_back_to_config_token(monkeypatch) -> None:
    config = Config()
    report = _empty_report()
    store = _FakeSecretStore(ok=False)
    monkeypatch.setenv("MINICLAW_TELEGRAM_TOKEN", "abc123")
    monkeypatch.setattr(cli_commands, "_validate_telegram_token", lambda _token: (True, "ok"))

    ok = cli_commands._configure_telegram_channel(
        config,
        secret_store=store,
        non_interactive=True,
        advanced_mode=False,
        report=report,
    )

    assert ok is True
    assert config.channels.telegram.enabled is True
    assert config.channels.telegram.token == "abc123"


def test_onboard_non_interactive_writes_state_and_report(tmp_path, monkeypatch) -> None:
    from miniclaw.config import loader as config_loader
    from miniclaw.utils import helpers as helpers_mod

    cfg_path = tmp_path / ".miniclaw" / "config.json"
    workspace = tmp_path / ".miniclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config_loader, "get_config_path", lambda: cfg_path)
    monkeypatch.setattr(helpers_mod, "get_workspace_path", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(cli_commands, "_create_workspace_templates", lambda _workspace: None)
    monkeypatch.setattr(
        cli_commands,
        "_run_skill_setup_checklist",
        lambda workspace, secret_store, non_interactive=False: [],
    )
    monkeypatch.setattr(cli_commands, "_resolve_host_default", lambda: "other_linux")
    monkeypatch.setattr(cli_commands, "_prune_logs", lambda _log_dir, _days: 0)
    monkeypatch.setattr(
        "miniclaw.cli.doctor.run_doctor",
        lambda fix=False: type(
            "R",
            (),
            {
                "checks": [],
                "has_errors": False,
            },
        )(),
    )

    cli_commands.onboard(non_interactive=True)

    state_path = tmp_path / ".miniclaw" / "onboarding-state.json"
    report_path = tmp_path / ".miniclaw" / "onboarding-report.json"
    assert state_path.exists()
    assert report_path.exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert report["mode"] == "guided"


def test_onboard_resume_normalizes_legacy_mac_mini_host(tmp_path, monkeypatch) -> None:
    from miniclaw.config import loader as config_loader
    from miniclaw.utils import helpers as helpers_mod

    cfg_path = tmp_path / ".miniclaw" / "config.json"
    workspace = tmp_path / ".miniclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config_loader, "get_config_path", lambda: cfg_path)
    monkeypatch.setattr(helpers_mod, "get_workspace_path", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(cli_commands, "_create_workspace_templates", lambda _workspace: None)
    monkeypatch.setattr(
        cli_commands,
        "_run_skill_setup_checklist",
        lambda workspace, secret_store, non_interactive=False: [],
    )
    monkeypatch.setattr(cli_commands, "_prune_logs", lambda _log_dir, _days: 0)
    monkeypatch.setattr(cli_commands, "service_install", lambda auto_start=None: None)
    monkeypatch.setattr(
        "miniclaw.cli.doctor.run_doctor",
        lambda fix=False: type(
            "R",
            (),
            {
                "checks": [],
                "has_errors": False,
            },
        )(),
    )

    save_onboarding_state(
        mode="guided",
        step="host",
        status="in_progress",
        data={"mode": "guided", "host": "mac_mini"},
        home=tmp_path,
    )

    cli_commands.onboard(resume=True, non_interactive=True)

    state = json.loads((tmp_path / ".miniclaw" / "onboarding-state.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / ".miniclaw" / "onboarding-report.json").read_text(encoding="utf-8"))
    assert state["data"]["host"] == "macos_local"
    assert report["host"] == "macos_local"


def test_apply_approval_profile_updates_tool_policy() -> None:
    cfg = Config()
    selected = cli_commands._apply_approval_profile(cfg, "locked_down")

    assert selected == "locked_down"
    assert cfg.tools.approval_profile == "locked_down"
    assert cfg.tools.approval.exec == "always_deny"
    assert cfg.tools.approval.web_fetch == "always_deny"

    fallback = cli_commands._apply_approval_profile(cfg, "invalid-profile")
    assert fallback == "coding"
    assert cfg.tools.approval_profile == "coding"
    assert cfg.tools.approval.exec == "always_ask"
