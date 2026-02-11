from pathlib import Path

import pytest

from miniclaw.cli.doctor import run_doctor
from miniclaw.config.loader import save_config
from miniclaw.config.schema import Config
from miniclaw.providers.oauth import OAuthTokenSet, save_token_to_store
from miniclaw.secrets import SecretStore


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _check_status(report, key: str) -> str:
    for check in report.checks:
        if check.key == key:
            return check.status
    raise AssertionError(f"Missing check: {key}")


def test_doctor_reports_missing_config_without_fix(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")

    assert _check_status(report, "config.file") == "error"
    assert report.has_errors is True


def test_doctor_fix_creates_config_workspace_and_skills(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    report = run_doctor(fix=True, config_path=cfg_path, home=fake_home, system_name="Linux")

    workspace = fake_home / ".miniclaw" / "workspace"

    assert cfg_path.exists()
    assert workspace.exists()
    assert (workspace / "skills").exists()
    assert _check_status(report, "config.file") == "ok"
    assert _check_status(report, "workspace.path") == "ok"
    assert report.has_errors is False


def test_doctor_fix_creates_service_definition_when_enabled(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.service.enabled = True
    save_config(config, cfg_path)

    report = run_doctor(fix=True, config_path=cfg_path, home=fake_home, system_name="Linux")
    service_file = fake_home / ".config" / "systemd" / "user" / "miniclaw.service"

    assert service_file.exists()
    service_check = next(c for c in report.checks if c.key == "service.definition")
    assert service_check.status == "ok"
    assert service_check.fixed is True
    assert report.has_errors is False


def test_doctor_fix_creates_launchd_log_dir_when_service_enabled(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.service.enabled = True
    save_config(config, cfg_path)

    report = run_doctor(fix=True, config_path=cfg_path, home=fake_home, system_name="Darwin")
    log_dir = fake_home / ".miniclaw" / "logs"

    assert log_dir.exists()
    logs_check = next(c for c in report.checks if c.key == "service.logs")
    assert logs_check.status == "ok"
    assert logs_check.fixed is True


def test_doctor_reports_sandbox_error_when_docker_missing(fake_home, monkeypatch) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.tools.sandbox.mode = "all"
    save_config(config, cfg_path)

    monkeypatch.setattr("miniclaw.cli.doctor.shutil.which", lambda name: None)
    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")

    assert _check_status(report, "sandbox.runtime") == "error"
    assert report.has_errors is True


def test_doctor_reports_sandbox_ok_when_mode_off(fake_home, monkeypatch) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.tools.sandbox.mode = "off"
    save_config(config, cfg_path)

    monkeypatch.setattr("miniclaw.cli.doctor.shutil.which", lambda name: None)
    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")

    assert _check_status(report, "sandbox.runtime") == "ok"


def test_doctor_reports_local_whisper_prereqs_when_enabled(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.transcription.local_whisper.enabled = True
    config.transcription.local_whisper.cli = "definitely-not-installed"
    config.transcription.local_whisper.model_path = str(fake_home / ".miniclaw" / "models" / "missing.bin")
    save_config(config, cfg_path)

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")

    assert _check_status(report, "transcription.local_whisper.cli") == "error"
    assert _check_status(report, "transcription.local_whisper.model") == "error"
    assert report.has_errors is True


def test_doctor_passes_local_whisper_prereqs_when_present(fake_home) -> None:
    cfg_path = fake_home / ".miniclaw" / "config.json"
    cli_path = fake_home / "whisper-cli"
    cli_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    model_path = fake_home / ".miniclaw" / "models" / "whisper-small.en.bin"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"model")

    config = Config()
    config.transcription.local_whisper.enabled = True
    config.transcription.local_whisper.cli = str(cli_path)
    config.transcription.local_whisper.model_path = str(model_path)
    save_config(config, cfg_path)

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")

    assert _check_status(report, "transcription.local_whisper.cli") == "ok"
    assert _check_status(report, "transcription.local_whisper.model") == "ok"


def test_doctor_reports_oauth_missing_token(fake_home, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.providers.openai.auth_mode = "oauth"
    save_config(config, cfg_path)

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")
    assert _check_status(report, "providers.openai.oauth") == "error"


def test_doctor_reports_oauth_token_ok(fake_home, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.providers.openai.auth_mode = "oauth"
    config.providers.openai.oauth_token_ref = "oauth:openai:token"
    save_config(config, cfg_path)

    store = SecretStore(namespace="miniclaw", backend="file", home=fake_home)
    save_token_to_store(
        store,
        "oauth:openai:token",
        OAuthTokenSet(access_token="ok", refresh_token="refresh", expires_at=2_000_000_000),
    )

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")
    assert _check_status(report, "providers.openai.oauth") == "ok"


def test_doctor_reports_oauth_expired_without_refresh(fake_home, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.providers.openai.auth_mode = "oauth"
    config.providers.openai.oauth_token_ref = "oauth:openai:token"
    save_config(config, cfg_path)

    store = SecretStore(namespace="miniclaw", backend="file", home=fake_home)
    save_token_to_store(
        store,
        "oauth:openai:token",
        OAuthTokenSet(access_token="expired", refresh_token=None, expires_at=1),
    )

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")
    assert _check_status(report, "providers.openai.oauth") == "error"


def test_doctor_accepts_telegram_token_from_secret_store(fake_home, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    cfg_path = fake_home / ".miniclaw" / "config.json"
    config = Config()
    config.channels.telegram.enabled = True
    config.channels.telegram.token = ""
    save_config(config, cfg_path)

    store = SecretStore(namespace="miniclaw", backend="file", home=fake_home)
    assert store.set("channels:telegram:token", "token-value") is True

    report = run_doctor(fix=False, config_path=cfg_path, home=fake_home, system_name="Linux")
    assert _check_status(report, "channels.telegram") == "ok"
