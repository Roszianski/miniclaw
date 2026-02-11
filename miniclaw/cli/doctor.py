"""System diagnostics for miniclaw."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniclaw.cli.service import get_service_file_path, render_service_definition, write_if_changed
from miniclaw.config.loader import _migrate_config, convert_keys, get_config_path, save_config
from miniclaw.config.schema import Config

TELEGRAM_TOKEN_SECRET_KEY = "channels:telegram:token"


@dataclass
class DoctorCheck:
    """Single diagnostic check result."""

    key: str
    status: str  # ok | warn | error
    message: str
    fixed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "message": self.message,
            "fixed": self.fixed,
        }


@dataclass
class DoctorReport:
    """Full diagnostic report."""

    checks: list[DoctorCheck]

    @property
    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def counts(self) -> dict[str, int]:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warn": sum(1 for check in self.checks if check.status == "warn"),
            "error": sum(1 for check in self.checks if check.status == "error"),
            "fixed": sum(1 for check in self.checks if check.fixed),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "summary": self.counts(),
            "has_errors": self.has_errors,
        }


def _load_config_strict(config_path: Path) -> tuple[Config | None, str | None]:
    if not config_path.exists():
        return None, "Config file is missing."
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw = _migrate_config(raw)
        cfg = Config.model_validate(convert_keys(raw))
        return cfg, None
    except Exception as exc:
        return None, f"Config is invalid: {exc}"


def _resolve_workspace_path(config: Config, home_dir: Path) -> Path:
    raw = config.agents.defaults.workspace.strip()
    if raw == "~":
        return home_dir
    if raw.startswith("~/"):
        return home_dir / raw[2:]
    return Path(raw).expanduser()


def _resolve_executable(tool: str) -> bool:
    tool = (tool or "").strip()
    if not tool:
        return False
    expanded = Path(tool).expanduser()
    if expanded.is_absolute() or "/" in tool:
        return expanded.exists() and expanded.is_file() and os.access(expanded, os.X_OK)
    return bool(shutil.which(tool))


def run_doctor(
    fix: bool = False,
    config_path: Path | None = None,
    home: Path | None = None,
    system_name: str | None = None,
) -> DoctorReport:
    """Run diagnostics and optional safe fixes."""
    from miniclaw.providers.oauth import load_token_from_store, resolve_oauth_token_ref
    from miniclaw.secrets import SecretStore

    checks: list[DoctorCheck] = []
    cfg_path = config_path or get_config_path()
    resolved_system = system_name or platform.system()
    home_dir = home or Path.home()

    config, config_error = _load_config_strict(cfg_path)

    if config_error and not cfg_path.exists() and fix:
        config = Config()
        save_config(config, cfg_path)
        checks.append(DoctorCheck("config.file", "ok", f"Created default config at {cfg_path}", fixed=True))
    elif config_error:
        checks.append(DoctorCheck("config.file", "error", config_error))
        config = Config()
    else:
        checks.append(DoctorCheck("config.file", "ok", f"Config loaded from {cfg_path}"))

    workspace = _resolve_workspace_path(config, home_dir)
    if workspace.exists() and workspace.is_dir():
        checks.append(DoctorCheck("workspace.path", "ok", f"Workspace exists: {workspace}"))
    elif fix:
        workspace.mkdir(parents=True, exist_ok=True)
        checks.append(DoctorCheck("workspace.path", "ok", f"Created workspace: {workspace}", fixed=True))
    else:
        checks.append(DoctorCheck("workspace.path", "error", f"Workspace missing: {workspace}"))

    skills_dir = workspace / "skills"
    if skills_dir.exists() and skills_dir.is_dir():
        checks.append(DoctorCheck("skills.workspace", "ok", f"Workspace skills dir exists: {skills_dir}"))
    elif fix:
        skills_dir.mkdir(parents=True, exist_ok=True)
        checks.append(DoctorCheck("skills.workspace", "ok", f"Created skills dir: {skills_dir}", fixed=True))
    else:
        checks.append(DoctorCheck("skills.workspace", "warn", f"Workspace skills dir missing: {skills_dir}"))

    secret_store = SecretStore(home=home_dir)

    secure_defaults_ok = bool(config.tools.sandbox.mode != "off" and config.tools.restrict_to_workspace)
    if secure_defaults_ok:
        checks.append(DoctorCheck("security.defaults", "ok", "Secure tool defaults are enabled."))
    elif fix:
        config.tools.sandbox.mode = "all"
        config.tools.restrict_to_workspace = True
        save_config(config, cfg_path)
        checks.append(
            DoctorCheck(
                "security.defaults",
                "ok",
                "Enabled secure tool defaults (sandbox.mode=all + workspace restriction).",
                fixed=True,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "security.defaults",
                "warn",
                "Secure tool defaults are disabled (tools.sandbox.mode/tools.restrictToWorkspace).",
            )
        )

    # Transcription prerequisites
    local_whisper = config.transcription.local_whisper
    if not local_whisper.enabled:
        checks.append(DoctorCheck("transcription.local_whisper", "ok", "Local Whisper transcription is disabled."))
    else:
        cli_tool = (local_whisper.cli or "whisper-cli").strip() or "whisper-cli"
        if _resolve_executable(cli_tool):
            checks.append(DoctorCheck("transcription.local_whisper.cli", "ok", f"Whisper CLI available: {cli_tool}"))
        else:
            checks.append(
                DoctorCheck(
                    "transcription.local_whisper.cli",
                    "error",
                    f"Local Whisper is enabled but CLI is missing: {cli_tool}",
                )
            )

        model_path = Path(local_whisper.model_path).expanduser()
        if model_path.exists() and model_path.is_file():
            checks.append(
                DoctorCheck("transcription.local_whisper.model", "ok", f"Whisper model exists: {model_path}")
            )
        else:
            checks.append(
                DoctorCheck(
                    "transcription.local_whisper.model",
                    "error",
                    f"Local Whisper is enabled but model file is missing: {model_path}",
                )
            )

    # OAuth provider auth diagnostics
    for provider_name in ("openai", "anthropic"):
        provider_cfg = getattr(config.providers, provider_name)
        key = f"providers.{provider_name}.oauth"
        if provider_cfg.auth_mode != "oauth":
            checks.append(DoctorCheck(key, "ok", f"{provider_name} authMode=api_key"))
            continue

        token_ref = resolve_oauth_token_ref(provider_name, provider_cfg.oauth_token_ref)
        token = load_token_from_store(secret_store, token_ref)
        if not token:
            checks.append(
                DoctorCheck(
                    key,
                    "error",
                    f"{provider_name} authMode=oauth but token is missing in SecretStore ({token_ref}).",
                )
            )
            continue

        if token.is_expired(skew_seconds=0):
            if token.refresh_token:
                checks.append(
                    DoctorCheck(
                        key,
                        "warn",
                        f"{provider_name} OAuth token is expired, but refresh token is present ({token_ref}).",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        key,
                        "error",
                        f"{provider_name} OAuth token is expired and no refresh token is stored ({token_ref}).",
                    )
                )
            continue

        seconds_remaining = token.seconds_remaining()
        if seconds_remaining is not None and seconds_remaining < 600:
            checks.append(
                DoctorCheck(
                    key,
                    "warn",
                    f"{provider_name} OAuth token expires soon ({seconds_remaining}s remaining).",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    key,
                    "ok",
                    f"{provider_name} OAuth token present ({token_ref}).",
                )
            )

    if sys.version_info >= (3, 11):
        checks.append(DoctorCheck("deps.python", "ok", f"Python {sys.version.split()[0]}"))
    else:
        checks.append(DoctorCheck("deps.python", "error", "Python >= 3.11 is required."))

    # Channel diagnostics
    tg = config.channels.telegram
    tg_token = tg.token or (secret_store.get(TELEGRAM_TOKEN_SECRET_KEY) or "")
    if tg.enabled and not tg_token:
        checks.append(DoctorCheck("channels.telegram", "error", "Telegram is enabled but token is missing."))
    else:
        if tg.enabled and not tg.token and tg_token:
            tg_msg = "Telegram prerequisites satisfied (token loaded from SecretStore)."
        elif tg.enabled:
            tg_msg = "Telegram prerequisites satisfied."
        else:
            tg_msg = "Telegram is disabled."
        checks.append(
            DoctorCheck(
                "channels.telegram",
                "ok",
                tg_msg,
            )
        )

    wa = config.channels.whatsapp
    npm = shutil.which("npm")
    if wa.enabled and not npm:
        checks.append(DoctorCheck("channels.whatsapp", "error", "WhatsApp is enabled but npm is not installed."))
    else:
        checks.append(
            DoctorCheck(
                "channels.whatsapp",
                "ok",
                "WhatsApp prerequisites satisfied." if wa.enabled else "WhatsApp is disabled.",
            )
        )

    # Service prerequisites
    service_file = get_service_file_path(home=home_dir, system_name=resolved_system)
    if config.service.enabled:
        if not service_file:
            checks.append(DoctorCheck("service.platform", "error", f"Unsupported service platform: {resolved_system}"))
        else:
            log_dir = home_dir / ".miniclaw" / "logs"
            if not service_file.exists() and fix:
                rendered, _ = render_service_definition(
                    workspace=workspace,
                    auto_start=config.service.auto_start,
                    log_dir=log_dir,
                    system_name=resolved_system,
                )
                if rendered:
                    write_if_changed(service_file, rendered)
                    checks.append(
                        DoctorCheck(
                            "service.definition",
                            "ok",
                            f"Created service definition at {service_file}",
                            fixed=True,
                        )
                    )
                else:
                    checks.append(
                        DoctorCheck("service.definition", "error", "Unable to render service definition for platform.")
                    )
            elif service_file.exists():
                checks.append(DoctorCheck("service.definition", "ok", f"Service definition exists: {service_file}"))
            else:
                checks.append(
                    DoctorCheck("service.definition", "error", f"Service definition missing: {service_file}")
                )

            if resolved_system == "Darwin":
                if log_dir.exists() and log_dir.is_dir():
                    checks.append(DoctorCheck("service.logs", "ok", f"Log dir exists: {log_dir}"))
                elif fix:
                    log_dir.mkdir(parents=True, exist_ok=True)
                    checks.append(DoctorCheck("service.logs", "ok", f"Created log dir: {log_dir}", fixed=True))
                else:
                    checks.append(DoctorCheck("service.logs", "error", f"Log dir missing: {log_dir}"))
    else:
        checks.append(DoctorCheck("service.definition", "ok", "Service is disabled in config."))

    # Sandbox prerequisites
    if config.tools.sandbox.mode == "off":
        checks.append(DoctorCheck("sandbox.runtime", "ok", "Sandbox mode is off."))
    elif shutil.which("docker"):
        checks.append(
            DoctorCheck(
                "sandbox.runtime",
                "ok",
                f"Docker runtime available (mode={config.tools.sandbox.mode}, scope={config.tools.sandbox.scope}).",
            )
        )
    else:
        status = "warn" if fix else "error"
        checks.append(
            DoctorCheck(
                "sandbox.runtime",
                status,
                "Sandbox mode is enabled but Docker is unavailable.",
            )
        )

    return DoctorReport(checks=checks)
