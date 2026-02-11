"""CLI commands for miniclaw."""

import asyncio
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from miniclaw import __logo__, __version__

app = typer.Typer(
    name="miniclaw",
    help=f"{__logo__} miniclaw - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()

DEFAULT_WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
)
TELEGRAM_TOKEN_SECRET_KEY = "channels:telegram:token"


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} miniclaw v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """miniclaw - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _onboarding_choice(
    title: str,
    options: list[tuple[str, str]],
    *,
    default_key: str,
    non_interactive: bool = False,
) -> str:
    keys = [item[0] for item in options]
    if default_key not in keys:
        default_key = keys[0]
    if non_interactive:
        return default_key

    console.print(f"\n{title}")
    for idx, (_key, label) in enumerate(options, start=1):
        default_hint = " [dim](default)[/dim]" if _key == default_key else ""
        console.print(f"  {idx}) {label}{default_hint}")

    default_idx = keys.index(default_key) + 1
    while True:
        raw = typer.prompt("Select", default=str(default_idx)).strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        lowered = raw.lower()
        if lowered in keys:
            return lowered
        console.print("[yellow]Please choose a valid option.[/yellow]")


def _onboarding_bool(prompt: str, *, default: bool, non_interactive: bool = False) -> bool:
    if non_interactive:
        return default
    return bool(typer.confirm(prompt, default=default))


def _parse_csv_values(raw: str) -> list[str]:
    items: list[str] = []
    for piece in (raw or "").split(","):
        value = piece.strip()
        if value:
            items.append(value)
    return items


def _telegram_api_json(token: str, method: str, payload: dict | None = None) -> dict:
    base = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    req = urllib.request.Request(base, data=data if payload is not None else None)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _validate_telegram_token(token: str) -> tuple[bool, str]:
    try:
        response = _telegram_api_json(token, "getMe")
    except Exception as exc:
        return False, f"Telegram API request failed: {exc}"

    if not response.get("ok"):
        desc = response.get("description") or "Unknown Telegram API error"
        return False, str(desc)
    result = response.get("result") or {}
    username = result.get("username") or "unknown"
    return True, f"Token valid (bot: @{username})"


def _wait_for_telegram_start_message(token: str, timeout_seconds: int = 45) -> bool:
    baseline = _telegram_api_json(token, "getUpdates", {"timeout": 1})
    max_update_id = 0
    for item in baseline.get("result") or []:
        if isinstance(item, dict):
            max_update_id = max(max_update_id, int(item.get("update_id") or 0))

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining = max(int(deadline - time.time()), 1)
        wait_s = min(10, remaining)
        response = _telegram_api_json(
            token,
            "getUpdates",
            {"offset": max_update_id + 1, "timeout": wait_s},
        )
        for item in response.get("result") or []:
            if not isinstance(item, dict):
                continue
            update_id = int(item.get("update_id") or 0)
            max_update_id = max(max_update_id, update_id)
            message = item.get("message") or {}
            text = str(message.get("text") or "").strip().lower()
            if text.startswith("/start"):
                return True
    return False


def _store_telegram_token(secret_store, config, token: str) -> None:
    if secret_store.set(TELEGRAM_TOKEN_SECRET_KEY, token):
        config.channels.telegram.token = ""
    else:
        config.channels.telegram.token = token


def _resolve_host_default() -> str:
    system_name = platform.system()
    if system_name == "Darwin":
        return "macos_local"
    if system_name == "Linux":
        return "linux_vps"
    return "other_linux"


def _normalize_host_key(host: str | None) -> str | None:
    raw = (host or "").strip().lower()
    if not raw:
        return None
    if raw == "mac_mini":
        return "macos_local"
    if raw in {"macos_local", "linux_vps", "other_linux"}:
        return raw
    return None


def _select_log_retention_days(non_interactive: bool) -> int:
    choice = _onboarding_choice(
        "Log retention window",
        [
            ("20", "20 days"),
            ("60", "60 days (Recommended)"),
            ("90", "90 days"),
        ],
        default_key="60",
        non_interactive=non_interactive,
    )
    try:
        return int(choice)
    except ValueError:
        return 60


def _prune_logs(log_dir: Path, retention_days: int) -> int:
    if not log_dir.exists() or not log_dir.is_dir():
        return 0
    now = time.time()
    cutoff = now - (retention_days * 86400)
    removed = 0
    for file_path in log_dir.iterdir():
        if not file_path.is_file():
            continue
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink(missing_ok=True)
                removed += 1
        except Exception:
            continue
    return removed


def _suggest_fix_for_doctor_check(check_key: str, config) -> str | None:
    if check_key == "service.definition":
        flag = "--auto-start" if config.service.auto_start else "--no-auto-start"
        return f"miniclaw service install {flag}"
    if check_key == "service.logs":
        return "mkdir -p ~/.miniclaw/logs"
    if check_key == "sandbox.runtime":
        if platform.system() == "Darwin":
            return "brew install --cask docker"
        return "sudo apt-get install -y docker.io"
    if check_key == "transcription.local_whisper.cli":
        if platform.system() == "Darwin":
            return "brew install whisper-cpp"
        return "sudo apt-get install -y whisper-cpp"
    if check_key == "transcription.local_whisper.model":
        model = Path(config.transcription.local_whisper.model_path).expanduser()
        return f"curl -L \"{DEFAULT_WHISPER_MODEL_URL}\" -o \"{model}\""
    if check_key == "channels.telegram":
        return "miniclaw onboard --resume"
    return None


def _apply_known_fix(check_key: str, config, fix_cmd: str) -> bool:
    try:
        if check_key == "service.definition":
            service_install(auto_start=config.service.auto_start)
            return True
        if check_key == "transcription.local_whisper.model":
            model = Path(config.transcription.local_whisper.model_path).expanduser()
            return _download_default_whisper_model(model)
    except Exception:
        return False
    return False


def _run_whatsapp_qr_login_flow() -> bool:
    bridge_dir = _get_bridge_dir()
    console.print("[cyan]Launching WhatsApp QR login...[/cyan]")
    proc = subprocess.Popen(["npm", "start"], cwd=bridge_dir)
    try:
        answer = typer.prompt(
            "Press Enter after scan/connection is complete (or type 'skip')",
            default="",
            show_default=False,
        ).strip().lower()
        if answer == "skip":
            return False
        return True
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _configure_telegram_channel(
    config,
    *,
    secret_store,
    non_interactive: bool,
    advanced_mode: bool,
    report: dict,
) -> bool:
    config.channels.telegram.enabled = True

    token = os.environ.get("MINICLAW_TELEGRAM_TOKEN", "").strip() if non_interactive else ""
    while not token:
        entered = typer.prompt("Telegram bot token (from @BotFather)", hide_input=True).strip()
        if entered:
            token = entered
            break
        if non_interactive:
            break
        choice = _onboarding_choice(
            "Telegram token is required to enable Telegram",
            [("retry", "Retry"), ("skip", "Skip Telegram"), ("fix", "Show fix command")],
            default_key="retry",
            non_interactive=non_interactive,
        )
        if choice == "skip":
            config.channels.telegram.enabled = False
            report["skipped"].append("telegram")
            return False
        if choice == "fix":
            console.print("  [dim]Fix:[/dim] Get token from @BotFather and run `miniclaw onboard --resume`.")

    if not token:
        config.channels.telegram.enabled = False
        report["skipped"].append("telegram")
        return False

    while True:
        ok, detail = _validate_telegram_token(token)
        if ok:
            console.print(f"[green]✓[/green] {detail}")
            report["configured"].append("telegram")
            break

        console.print(f"[yellow]•[/yellow] Telegram validation failed: {detail}")
        choice = _onboarding_choice(
            "Telegram token validation failed",
            [("retry", "Retry token"), ("skip", "Skip Telegram"), ("fix", "Show fix command")],
            default_key="retry" if advanced_mode else "skip",
            non_interactive=non_interactive,
        )
        if choice == "skip":
            config.channels.telegram.enabled = False
            report["skipped"].append("telegram")
            return False
        if choice == "fix":
            console.print("  [dim]Fix:[/dim] Confirm token with @BotFather and retry.")
            continue
        token = typer.prompt("Telegram bot token", hide_input=True).strip()
        if not token:
            if not advanced_mode:
                config.channels.telegram.enabled = False
                report["skipped"].append("telegram")
                return False

    _store_telegram_token(secret_store, config, token)

    if non_interactive:
        allow_from = _parse_csv_values(os.environ.get("MINICLAW_TELEGRAM_ALLOW_FROM", ""))
    else:
        allow_from = config.channels.telegram.allow_from
        if _onboarding_bool("Set Telegram allowlist now?", default=False, non_interactive=False):
            raw = typer.prompt("Allowed Telegram IDs/usernames (comma-separated)", default="")
            allow_from = _parse_csv_values(raw)
    config.channels.telegram.allow_from = allow_from

    run_live_test = _onboarding_bool(
        "Run Telegram live connectivity test now?",
        default=not non_interactive,
        non_interactive=non_interactive,
    )
    if run_live_test:
        console.print("Send /start to your bot now. Waiting for inbound confirmation...")
        try:
            ok = _wait_for_telegram_start_message(token)
        except Exception as exc:
            ok = False
            console.print(f"[yellow]•[/yellow] Telegram live test failed: {exc}")
        if ok:
            console.print("[green]✓[/green] Telegram inbound /start confirmed.")
            report["checks"].append(
                {"key": "onboarding.telegram.live", "status": "ok", "message": "Inbound /start confirmed."}
            )
        else:
            console.print("[yellow]•[/yellow] Telegram live test timed out.")
            report["checks"].append(
                {"key": "onboarding.telegram.live", "status": "warn", "message": "Inbound /start not confirmed."}
            )
    return True


def _configure_whatsapp_channel(
    config,
    *,
    non_interactive: bool,
    report: dict,
) -> bool:
    config.channels.whatsapp.enabled = True
    config.channels.whatsapp.bridge_url = str(config.channels.whatsapp.bridge_url or "ws://127.0.0.1:3001")
    config.channels.whatsapp.bridge_host = str(config.channels.whatsapp.bridge_host or "127.0.0.1")
    token = str(config.channels.whatsapp.bridge_auth_token or "").strip()
    if not token:
        token = str(os.environ.get("MINICLAW_WHATSAPP_BRIDGE_AUTH_TOKEN") or "").strip() or secrets.token_urlsafe(24)
        config.channels.whatsapp.bridge_auth_token = token
        report["checks"].append(
            {
                "key": "onboarding.whatsapp.bridge_auth_token",
                "status": "ok",
                "message": "Bridge auth token configured.",
            }
        )

    if non_interactive:
        raw_allow = os.environ.get("MINICLAW_WHATSAPP_ALLOW_FROM", "")
        config.channels.whatsapp.allow_from = _parse_csv_values(raw_allow)
        report["configured"].append("whatsapp")
        report["checks"].append(
            {
                "key": "onboarding.whatsapp.qr",
                "status": "warn",
                "message": "Non-interactive mode skipped QR login confirmation.",
            }
        )
        return True

    if _onboarding_bool("Set WhatsApp allowlist now?", default=False, non_interactive=False):
        raw = typer.prompt("Allowed phone numbers (comma-separated)", default="")
        config.channels.whatsapp.allow_from = _parse_csv_values(raw)

    if _onboarding_bool("Launch WhatsApp QR login now?", default=True, non_interactive=False):
        try:
            connected = _run_whatsapp_qr_login_flow()
        except Exception as exc:
            console.print(f"[yellow]•[/yellow] WhatsApp QR login failed: {exc}")
            connected = False
        if connected:
            console.print("[green]✓[/green] WhatsApp QR flow completed.")
            report["configured"].append("whatsapp")
            report["checks"].append(
                {"key": "onboarding.whatsapp.qr", "status": "ok", "message": "QR login completed."}
            )
        else:
            report["checks"].append(
                {"key": "onboarding.whatsapp.qr", "status": "warn", "message": "QR flow skipped or not confirmed."}
            )
    else:
        report["checks"].append({"key": "onboarding.whatsapp.qr", "status": "warn", "message": "QR flow skipped."})
    return True


@app.command()
def onboard(
    resume: bool = typer.Option(False, "--resume", help="Resume previous onboarding state."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Run onboarding with defaults only."),
    advanced: bool = typer.Option(False, "--advanced", help="Start directly in Advanced Setup mode."),
):
    """Run Guided Setup or Advanced Setup for miniclaw."""
    from miniclaw.cli.doctor import run_doctor
    from miniclaw.cli.onboarding import (
        load_onboarding_state,
        save_onboarding_state,
        write_onboarding_report,
    )
    from miniclaw.config.loader import get_config_path, load_config, save_config
    from miniclaw.config.schema import Config
    from miniclaw.secrets import SecretStore
    from miniclaw.utils.helpers import get_workspace_path

    # Keep direct function calls test-friendly (Typer passes OptionInfo only via CLI parsing).
    if not isinstance(resume, bool):
        resume = False
    if not isinstance(non_interactive, bool):
        non_interactive = False
    if not isinstance(advanced, bool):
        advanced = False
    if not non_interactive and not sys.stdin.isatty():
        non_interactive = True

    state = load_onboarding_state() if resume else None
    state_data = dict(state.get("data") or {}) if state else {}
    mode = "advanced" if advanced else state_data.get("mode")
    if not mode:
        mode = _onboarding_choice(
            "Choose onboarding mode",
            [
                ("guided", "Guided Setup (Recommended)"),
                ("advanced", "Advanced Setup"),
            ],
            default_key="guided",
            non_interactive=non_interactive,
        )

    config_path = get_config_path()
    if config_path.exists():
        config = load_config(config_path)
        console.print(f"[green]✓[/green] Loaded config from {config_path}")
    else:
        config = Config()
        save_config(config, config_path)
        console.print(f"[green]✓[/green] Created config at {config_path}")

    try:
        workspace = get_workspace_path(config.agents.defaults.workspace)
    except TypeError:
        workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Workspace ready at {workspace}")
    _create_workspace_templates(workspace)

    secret_store = SecretStore()
    report: dict = {
        "mode": mode,
        "host": "",
        "configured": [],
        "skipped": [],
        "checks": [],
        "auto_fixes": [],
        "next_steps": [],
    }
    run_data: dict = state_data
    run_data["mode"] = mode
    save_onboarding_state(mode=mode, step="start", status="in_progress", data=run_data)

    host = _normalize_host_key(run_data.get("host")) or _onboarding_choice(
        "Choose host type",
        [
            ("macos_local", "macOS (Local)"),
            ("linux_vps", "Linux VPS / Server"),
            ("other_linux", "Other Linux Host"),
        ],
        default_key=_resolve_host_default(),
        non_interactive=non_interactive,
    )
    host = _normalize_host_key(host) or "other_linux"
    run_data["host"] = host
    report["host"] = host
    save_onboarding_state(mode=mode, step="host", status="in_progress", data=run_data)

    auth_mode = run_data.get("auth_mode") or _onboarding_choice(
        "Choose provider auth mode",
        [
            ("oauth", "OAuth"),
            ("api_key", "API key"),
        ],
        default_key="oauth" if not non_interactive else "api_key",
        non_interactive=non_interactive,
    )
    run_data["auth_mode"] = auth_mode
    save_onboarding_state(mode=mode, step="auth", status="in_progress", data=run_data)

    if auth_mode == "oauth":
        provider = run_data.get("oauth_provider") or _onboarding_choice(
            "Choose OAuth provider",
            [
                ("openai", "OpenAI"),
                ("anthropic", "Anthropic"),
            ],
            default_key="openai",
            non_interactive=non_interactive,
        )
        run_data["oauth_provider"] = provider
        provider_cfg = getattr(config.providers, provider)
        provider_cfg.auth_mode = "oauth"
        if not non_interactive and _onboarding_bool("Run OAuth login now?", default=True, non_interactive=False):
            try:
                auth_login(provider=provider, no_browser=False)
                report["configured"].append(f"oauth:{provider}")
            except typer.Exit:
                report["checks"].append(
                    {
                        "key": f"onboarding.oauth.{provider}",
                        "status": "warn",
                        "message": "OAuth login did not complete; you can run auth login later.",
                    }
                )
        else:
            report["checks"].append(
                {
                    "key": f"onboarding.oauth.{provider}",
                    "status": "warn",
                    "message": "OAuth selected but login skipped.",
                }
            )
    else:
        provider = run_data.get("api_provider") or _onboarding_choice(
            "Choose API key provider",
            [
                ("openrouter", "OpenRouter"),
                ("openai", "OpenAI"),
                ("anthropic", "Anthropic"),
            ],
            default_key="openrouter",
            non_interactive=non_interactive,
        )
        run_data["api_provider"] = provider
        provider_cfg = getattr(config.providers, provider)
        provider_cfg.auth_mode = "api_key"
        key_value = ""
        if non_interactive:
            env_name = f"MINICLAW_{provider.upper()}_API_KEY"
            key_value = (os.environ.get(env_name) or "").strip()
        else:
            if _onboarding_bool(f"Set {provider} API key now?", default=True, non_interactive=False):
                key_value = typer.prompt(f"{provider} API key", hide_input=True).strip()
        if key_value:
            provider_cfg.api_key = key_value
            report["configured"].append(f"api_key:{provider}")
        else:
            report["checks"].append(
                {
                    "key": f"onboarding.api_key.{provider}",
                    "status": "warn",
                    "message": "API key not set during onboarding.",
                }
            )

    approval_profile = run_data.get("approval_profile") or _onboarding_choice(
        "Choose tool approval profile",
        [
            ("coding", "Coding (Balanced)"),
            ("automation", "Automation (Allow exec)"),
            ("messaging", "Messaging (No exec/browser)"),
            ("locked_down", "Locked Down (Deny tools)"),
        ],
        default_key=str(config.tools.approval_profile or "coding"),
        non_interactive=non_interactive,
    )
    selected_profile = _apply_approval_profile(config, str(approval_profile))
    run_data["approval_profile"] = selected_profile
    report["configured"].append(f"approval_profile:{selected_profile}")
    save_onboarding_state(mode=mode, step="approvals", status="in_progress", data=run_data)

    save_config(config, config_path)

    channel_choice = run_data.get("channels") or _onboarding_choice(
        "Choose channels",
        [
            ("telegram", "Telegram (Recommended)"),
            ("whatsapp", "WhatsApp"),
            ("both", "Both Telegram + WhatsApp"),
            ("skip", "Skip for now"),
        ],
        default_key="telegram" if not non_interactive else "skip",
        non_interactive=non_interactive,
    )
    run_data["channels"] = channel_choice
    save_onboarding_state(mode=mode, step="channels", status="in_progress", data=run_data)

    config.channels.telegram.enabled = False
    config.channels.whatsapp.enabled = False
    if channel_choice in {"telegram", "both"}:
        _configure_telegram_channel(
            config,
            secret_store=secret_store,
            non_interactive=non_interactive,
            advanced_mode=(mode == "advanced"),
            report=report,
        )
    if channel_choice in {"whatsapp", "both"}:
        _configure_whatsapp_channel(config, non_interactive=non_interactive, report=report)
    if channel_choice == "skip":
        report["skipped"].append("channels")

    save_config(config, config_path)

    save_onboarding_state(mode=mode, step="service", status="in_progress", data=run_data)
    enable_service_default = host in {"macos_local", "linux_vps"}
    enable_service = _onboarding_bool(
        "Enable miniclaw service now?",
        default=enable_service_default,
        non_interactive=non_interactive,
    )
    config.service.enabled = enable_service
    if enable_service:
        auto_start = _onboarding_bool(
            "Auto-start service on boot/login?",
            default=True,
            non_interactive=non_interactive,
        )
        config.service.auto_start = auto_start
        config.service.log_retention_days = _select_log_retention_days(non_interactive=non_interactive)

        if _onboarding_bool("Install/start service now?", default=True, non_interactive=non_interactive):
            try:
                service_install(auto_start=auto_start)
                report["configured"].append("service")
            except typer.Exit:
                report["checks"].append(
                    {
                        "key": "onboarding.service.install",
                        "status": "warn",
                        "message": "Service installation did not complete.",
                    }
                )
    else:
        config.service.auto_start = False
        report["skipped"].append("service")

    logs_dir = Path.home() / ".miniclaw" / "logs"
    removed_logs = _prune_logs(logs_dir, config.service.log_retention_days)
    if removed_logs > 0:
        report["checks"].append(
            {
                "key": "onboarding.logs.retention",
                "status": "ok",
                "message": f"Pruned {removed_logs} old log files.",
            }
        )

    save_config(config, config_path)

    save_onboarding_state(mode=mode, step="skills", status="in_progress", data=run_data)
    if non_interactive:
        selected = _run_skill_setup_checklist(
            workspace=workspace,
            secret_store=secret_store,
            non_interactive=True,
        )
    else:
        selected = _run_optional_skill_checklist(workspace=workspace, secret_store=secret_store)
    if "whisper-local" in selected:
        config.transcription.local_whisper.enabled = True
        save_config(config, config_path)
        console.print("[green]✓[/green] Enabled local Whisper transcription in config")
        model_path = Path(config.transcription.local_whisper.model_path).expanduser()
        if model_path.exists():
            console.print(f"  [green]✓[/green] Whisper model already present: {model_path}")
        elif _onboarding_bool("Download whisper-small.en model now? (~466MB)", default=True, non_interactive=non_interactive):
            _download_default_whisper_model(model_path)
        else:
            console.print(
                f"  [dim]Manual download:[/dim] curl -L \"{DEFAULT_WHISPER_MODEL_URL}\" -o \"{model_path}\""
            )
    elif config.transcription.local_whisper.enabled:
        model_path = Path(config.transcription.local_whisper.model_path).expanduser()
        if not model_path.exists():
            report["checks"].append(
                {
                    "key": "onboarding.whisper",
                    "status": "warn",
                    "message": "Local Whisper enabled but model is missing.",
                }
            )

    if mode == "guided":
        console.print(f"\n{__logo__} miniclaw is ready.")
        console.print("[dim]Running deep checks...[/dim]")
    else:
        console.print("\nRunning advanced readiness checks...")

    doctor_report = run_doctor(fix=False)
    for check in doctor_report.checks:
        report["checks"].append(check.to_dict())
        if check.status == "error":
            fix_cmd = _suggest_fix_for_doctor_check(check.key, config)
            if fix_cmd:
                report["next_steps"].append(fix_cmd)
                if mode == "advanced" and not non_interactive:
                    console.print(f"[yellow]•[/yellow] {check.message}")
                    console.print(f"  [dim]Fix:[/dim] {fix_cmd}")
                    if _onboarding_bool("Apply fix now?", default=False, non_interactive=False):
                        applied = _apply_known_fix(check.key, config, fix_cmd)
                        report["auto_fixes"].append(
                            {
                                "check": check.key,
                                "command": fix_cmd,
                                "applied": applied,
                            }
                        )
                        if applied:
                            console.print("[green]✓[/green] Applied fix.")
                        else:
                            console.print("[yellow]•[/yellow] Auto-apply unavailable; run command manually.")

    report_path = write_onboarding_report(report)
    save_onboarding_state(mode=mode, step="complete", status="completed", data=run_data)

    statuses = {
        str(item.get("status", "ok")).lower()
        for item in report["checks"]
        if isinstance(item, dict)
    }
    if "error" in statuses or "warn" in statuses:
        console.print("[yellow]Onboarding completed with warnings/errors.[/yellow]")
    else:
        console.print("[green]Onboarding checks passed.[/green]")

    console.print("\nNext steps:")
    console.print("  1. Check service: [cyan]miniclaw service status[/cyan]")
    console.print("  2. Run diagnostics: [cyan]miniclaw doctor[/cyan]")
    console.print("  3. Start chatting: [cyan]miniclaw agent -m \"Hello!\"[/cyan]")
    console.print(f"\n[dim]Onboarding report: {report_path}[/dim]")




def _download_default_whisper_model(model_path: Path) -> bool:
    """Download default whisper.cpp small.en model to the configured path."""
    import os
    import shutil
    import urllib.request

    target = model_path.expanduser()
    if target.exists():
        return True

    url = (os.environ.get("MINICLAW_WHISPER_MODEL_URL") or DEFAULT_WHISPER_MODEL_URL).strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")

    try:
        console.print(f"  [dim]Downloading Whisper model from:[/dim] {url}")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as fh:
            shutil.copyfileobj(response, fh)
        if partial.stat().st_size <= 0:
            raise RuntimeError("downloaded model file is empty")
        partial.replace(target)
        console.print(f"  [green]✓[/green] Downloaded Whisper model: {target}")
        return True
    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        console.print(f"  [yellow]•[/yellow] Failed to download Whisper model: {exc}")
        console.print(f"  [dim]Manual download:[/dim] curl -L \"{url}\" -o \"{target}\"")
        return False


def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
        "SOUL.md": """# Soul

I am miniclaw, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
    }

    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")

    # Copy BOOTSTRAP.md template for first-run onboarding
    bootstrap_dest = workspace / "BOOTSTRAP.md"
    if not bootstrap_dest.exists():
        bootstrap_src = Path(__file__).parent.parent / "workspace_defaults" / "BOOTSTRAP.md"
        if bootstrap_src.exists():
            bootstrap_dest.write_text(bootstrap_src.read_text(encoding="utf-8"), encoding="utf-8")
            console.print("  [dim]Created BOOTSTRAP.md (first-run onboarding)[/dim]")

    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


def _install_builtin_skill(workspace: Path, skill_name: str) -> bool:
    """Install a bundled skill into workspace/skills."""
    import shutil

    source = Path(__file__).parent.parent / "skills" / skill_name
    if not source.exists():
        return False

    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    dest = skills_dir / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return True


def _run_skill_setup_checklist(
    workspace: Path,
    secret_store,
    *,
    non_interactive: bool = False,
) -> list[str]:
    """
    Guided checklist for skills setup.

    Returns:
        List of installed skill names.
    """
    import os
    import platform

    from miniclaw.agent.skills import SkillsLoader

    choices = [
        ("whisper-local", "Whisper local STT"),
        ("elevenlabs", "ElevenLabs"),
        ("nanobanana", "NanoBanana"),
        ("github", "GitHub"),
        ("weather", "Weather"),
    ]
    installed: list[str] = []

    if non_interactive:
        return installed

    if not typer.confirm("Configure skills now?", default=True):
        return installed

    console.print("\nSkills Setup:")
    for skill_name, label in choices:
        if not typer.confirm(f"Install {label} skill?", default=False):
            continue
        ok = _install_builtin_skill(workspace, skill_name)
        if not ok:
            console.print(f"[yellow]•[/yellow] {label}: not bundled, skipped")
            continue
        installed.append(skill_name)
        console.print(f"[green]✓[/green] Installed {label}")

        loader = SkillsLoader(workspace=workspace, secret_store=secret_store)
        status = next((s for s in loader.list_skills(filter_unavailable=False) if s["name"] == skill_name), None)
        if not status:
            continue

        missing = list(status.get("missing") or [])
        missing_bins = [m.split(":", 1)[1] for m in missing if m.startswith("CLI:")]
        missing_env = [m.split(":", 1)[1] for m in missing if m.startswith("ENV:")]
        if missing_bins:
            console.print(f"  [yellow]Missing binaries:[/yellow] {', '.join(missing_bins)}")
            install_cmds = loader.get_install_commands(skill_name, system_name=platform.system())
            for cmd in install_cmds:
                console.print(f"  [dim]Install:[/dim] {cmd}")
            if not install_cmds:
                console.print("  [dim]Install required binaries manually to enable this skill.[/dim]")

        # Offer secret entry for required env vars.
        for env_name in missing_env:
            if os.environ.get(env_name):
                continue
            key = loader.secret_key_for(skill_name, env_name)
            if secret_store.has(key):
                continue
            if typer.confirm(f"Set secret {env_name} now?", default=False):
                value = typer.prompt(f"{env_name}", hide_input=True).strip()
                if value:
                    secret_store.set(key, value)
                    console.print(f"  [green]✓[/green] Saved secret {env_name}")

    return installed


def _run_optional_skill_checklist(workspace: Path, secret_store) -> list[str]:
    """Backward-compatible alias for legacy tests/callers."""
    return _run_skill_setup_checklist(workspace=workspace, secret_store=secret_store, non_interactive=False)


def _make_provider(
    config,
    *,
    model: str | None = None,
    thinking: str | None = None,
    secret_store=None,
):
    """Create LiteLLMProvider from config with OAuth/API-key auth resolution."""
    from miniclaw.providers.failover import FailoverCandidate, FailoverProvider
    from miniclaw.providers.litellm_provider import LiteLLMProvider
    from miniclaw.providers.oauth import (
        get_oauth_adapter,
        load_token_from_store,
        resolve_oauth_token_ref,
        save_token_to_store,
    )
    from miniclaw.secrets import SecretStore

    resolved_model = model or config.agents.defaults.model
    resolved_thinking = thinking or config.agents.defaults.thinking
    requires_api_key = not resolved_model.startswith("bedrock/")
    store = secret_store or SecretStore()
    candidates = config.get_provider_candidates(resolved_model)
    primary_provider_name = candidates[0] if candidates else None
    primary_provider_cfg = (
        getattr(config.providers, primary_provider_name) if primary_provider_name else None
    )
    resolved_candidates: list[tuple[str, object, str]] = []
    failover_cfg = getattr(config.providers, "failover", None)
    failover_enabled = bool(getattr(failover_cfg, "enabled", False))

    for idx, provider_name in enumerate(candidates):
        provider_cfg = getattr(config.providers, provider_name)
        resolved_api_key = provider_cfg.api_key
        auth_mode = (provider_cfg.auth_mode or "api_key").strip().lower()

        if auth_mode == "oauth":
            if provider_name in {"openai", "anthropic"}:
                token_ref = resolve_oauth_token_ref(provider_name, provider_cfg.oauth_token_ref)
                token = load_token_from_store(store, token_ref)

                if token:
                    if token.is_expired(skew_seconds=60):
                        if token.refresh_token:
                            try:
                                adapter = get_oauth_adapter(provider_name)
                                refreshed = adapter.refresh_token(token.refresh_token)
                                save_token_to_store(store, token_ref, refreshed)
                                token = refreshed
                                if idx == 0:
                                    console.print(
                                        f"[green]✓[/green] Refreshed OAuth token for {provider_name}."
                                    )
                            except Exception as exc:
                                token = None
                                if idx == 0:
                                    console.print(
                                        f"[yellow]OAuth refresh failed for {provider_name}:[/yellow] {exc}"
                                    )
                        else:
                            token = None
                            if idx == 0:
                                console.print(
                                    f"[yellow]OAuth token for {provider_name} is expired and has no refresh token.[/yellow]"
                                )
                    if token:
                        resolved_api_key = token.access_token
                elif idx == 0:
                    console.print(
                        f"[yellow]OAuth token for {provider_name} not found.[/yellow] "
                        f"Run [cyan]miniclaw auth login --provider {provider_name}[/cyan]."
                    )
            elif idx == 0:
                console.print(
                    f"[yellow]OAuth authMode is not supported for provider '{provider_name}'.[/yellow]"
                )

            if not resolved_api_key and provider_cfg.api_key and idx == 0:
                console.print(
                    f"[yellow]Falling back to API key for {provider_name} provider.[/yellow]"
                )
                resolved_api_key = provider_cfg.api_key

        # Choose first provider with usable auth (or keyless bedrock mode)
        if resolved_api_key or not requires_api_key:
            resolved_candidates.append((provider_name, provider_cfg, resolved_api_key))

    if requires_api_key and not resolved_candidates:
        console.print("[red]Error: No API key configured.[/red]")
        primary_auth_mode = (
            (primary_provider_cfg.auth_mode or "api_key").strip().lower()
            if primary_provider_cfg
            else "api_key"
        )
        if (
            primary_provider_cfg
            and primary_auth_mode == "oauth"
            and primary_provider_name in {"openai", "anthropic"}
        ):
            console.print(
                f"OAuth mode is enabled for {primary_provider_name}; login via "
                f"[cyan]miniclaw auth login --provider {primary_provider_name}[/cyan] "
                f"or set providers.{primary_provider_name}.apiKey for fallback."
            )
        else:
            console.print("Set one in ~/.miniclaw/config.json under providers section")
        raise typer.Exit(1)

    if not resolved_candidates:
        # Bedrock-like keyless path fallback.
        return LiteLLMProvider(
            api_key=None,
            api_base=config.get_api_base(resolved_model),
            default_model=resolved_model,
            extra_headers=None,
            thinking=resolved_thinking,
        )

    provider_candidates: list[FailoverCandidate] = []
    for provider_name, provider_cfg, resolved_api_key in resolved_candidates:
        provider = LiteLLMProvider(
            api_key=resolved_api_key if resolved_api_key else None,
            api_base=config.get_api_base_for_provider(provider_name, provider=provider_cfg),
            default_model=resolved_model,
            extra_headers=provider_cfg.extra_headers,
            thinking=resolved_thinking,
        )
        provider_candidates.append(FailoverCandidate(name=provider_name, provider=provider))

    if not failover_enabled or len(provider_candidates) == 1:
        return provider_candidates[0].provider

    return FailoverProvider(
        candidates=provider_candidates,
        default_model=resolved_model,
        failover_policy=failover_cfg,
    )


_SESSION_RESET_JOB_NAME = "system:session_reset"
_SESSION_RESET_JOB_MESSAGE = "__scheduled_session_reset__"
_RETENTION_SWEEP_JOB_NAME = "system:retention_sweep"
_RETENTION_SWEEP_JOB_MESSAGE = "__retention_sweep__"
_RETENTION_SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000


def _apply_approval_profile(config, profile: str) -> str:
    from miniclaw.config.schema import ToolApprovalConfig

    normalized = (profile or "coding").strip().lower()
    if normalized not in {"coding", "messaging", "automation", "locked_down"}:
        normalized = "coding"
    config.tools.approval_profile = normalized
    config.tools.approval = ToolApprovalConfig.from_profile(normalized)
    return normalized


def _is_session_reset_job(job: object) -> bool:
    payload = getattr(job, "payload", None)
    kind = str(getattr(payload, "kind", "") or "").strip().lower()
    if kind == "session_reset":
        return True
    name = str(getattr(job, "name", "") or "")
    message = str(getattr(payload, "message", "") or "")
    return name == _SESSION_RESET_JOB_NAME and message == _SESSION_RESET_JOB_MESSAGE


def _is_retention_sweep_job(job: object) -> bool:
    payload = getattr(job, "payload", None)
    kind = str(getattr(payload, "kind", "") or "").strip().lower()
    if kind == "retention_sweep":
        return True
    name = str(getattr(job, "name", "") or "")
    message = str(getattr(payload, "message", "") or "")
    return name == _RETENTION_SWEEP_JOB_NAME and message == _RETENTION_SWEEP_JOB_MESSAGE


def _reconcile_scheduled_session_reset_job(cron_service, cron_expr: str | None) -> None:
    expr = str(cron_expr or "").strip()
    jobs = cron_service.list_jobs(include_disabled=True)
    managed = [job for job in jobs if _is_session_reset_job(job)]

    if not expr:
        for job in managed:
            cron_service.remove_job(job.id)
        return

    keep_id: str | None = None
    for job in managed:
        schedule = getattr(job, "schedule", None)
        schedule_kind = str(getattr(schedule, "kind", "") or "")
        schedule_expr = str(getattr(schedule, "expr", "") or "").strip()
        if keep_id is None and schedule_kind == "cron" and schedule_expr == expr and bool(getattr(job, "enabled", False)):
            keep_id = job.id
            continue
        cron_service.remove_job(job.id)

    if keep_id is not None:
        return

    from miniclaw.cron.types import CronSchedule

    cron_service.add_job(
        name=_SESSION_RESET_JOB_NAME,
        schedule=CronSchedule(kind="cron", expr=expr),
        message=_SESSION_RESET_JOB_MESSAGE,
        deliver=False,
        kind="session_reset",
        isolated=True,
        retry_max_attempts=1,
        retry_backoff_ms=0,
    )


def _reconcile_retention_sweep_job(cron_service) -> None:
    from miniclaw.cron.types import CronSchedule

    jobs = cron_service.list_jobs(include_disabled=True)
    managed = [job for job in jobs if _is_retention_sweep_job(job)]
    keep_id: str | None = None
    for job in managed:
        schedule = getattr(job, "schedule", None)
        kind = str(getattr(schedule, "kind", "") or "")
        every_ms = int(getattr(schedule, "every_ms", 0) or 0)
        enabled = bool(getattr(job, "enabled", False))
        if keep_id is None and kind == "every" and every_ms == _RETENTION_SWEEP_INTERVAL_MS and enabled:
            keep_id = job.id
            continue
        cron_service.remove_job(job.id)

    if keep_id is not None:
        return

    cron_service.add_job(
        name=_RETENTION_SWEEP_JOB_NAME,
        schedule=CronSchedule(kind="every", every_ms=_RETENTION_SWEEP_INTERVAL_MS),
        message=_RETENTION_SWEEP_JOB_MESSAGE,
        deliver=False,
        kind="retention_sweep",
        isolated=True,
        retry_max_attempts=2,
        retry_backoff_ms=1500,
    )


def _run_scheduled_session_reset(agent_loops: dict[str, object]) -> int:
    managers = []
    seen_ids: set[int] = set()
    for loop in agent_loops.values():
        manager = getattr(loop, "sessions", None)
        if manager is None:
            continue
        marker = id(manager)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        managers.append(manager)

    if not managers:
        return 0

    reset_count = int(
        managers[0].reset_all(reason="scheduled_cron", actor="cron", include_persisted=True)
    )
    for manager in managers[1:]:
        manager.reset_all(reason="scheduled_cron", actor="cron", include_persisted=False)
    return reset_count


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the miniclaw gateway."""
    from miniclaw.agent.loop import AgentLoop
    from miniclaw.agent.router import AgentRouter
    from miniclaw.audit.logger import AuditLogger
    from miniclaw.bus.queue import MessageBus
    from miniclaw.channels.manager import ChannelManager
    from miniclaw.compliance.service import ComplianceService
    from miniclaw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from miniclaw.cron.service import CronService
    from miniclaw.cron.types import CronJob
    from miniclaw.dashboard.auth import generate_token
    from miniclaw.distributed.manager import DistributedNodeManager
    from miniclaw.heartbeat.service import HeartbeatService
    from miniclaw.identity import IdentityStore
    from miniclaw.monitoring.alerts import AlertService
    from miniclaw.processes.manager import ProcessManager
    from miniclaw.providers.transcription import TranscriptionManager
    from miniclaw.providers.tts import KokoroTTSAdapter
    from miniclaw.ratelimit.limiter import RateLimiter
    from miniclaw.secrets import ScopedSecretStore, SecretStore
    from miniclaw.usage import UsageTracker

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting miniclaw gateway on port {port}...")

    config = load_config()
    data_dir = get_data_dir()
    bus = MessageBus()
    secret_store = SecretStore()
    identity_store = IdentityStore(data_dir / "identity" / "state.json")
    distributed_manager = DistributedNodeManager(
        store_path=data_dir / "distributed" / "state.json",
        local_node_id=config.distributed.node_id,
        peer_allowlist=config.distributed.peer_allowlist,
        heartbeat_timeout_s=config.distributed.heartbeat_timeout_s,
        max_tasks=config.distributed.max_tasks,
    )
    if config.distributed.enabled:
        distributed_manager.register_node(
            node_id=config.distributed.node_id,
            capabilities=["agent", "workflow", "process"],
            metadata={"local": True},
            address="local",
        )
    process_manager = ProcessManager(
        workspace=config.workspace_path,
        restrict_to_workspace=config.tools.restrict_to_workspace,
    )
    if config.channels.telegram.enabled and not config.channels.telegram.token:
        stored_tg_token = secret_store.get(TELEGRAM_TOKEN_SECRET_KEY)
        if stored_tg_token:
            config.channels.telegram.token = stored_tg_token

    audit_logger = None
    if config.audit.enabled:
        audit_logger = AuditLogger(data_dir / "audit.log", level=config.audit.level)

    rate_limiter = None
    if config.rate_limit.enabled:
        rate_limiter = RateLimiter(
            messages_per_minute=config.rate_limit.messages_per_minute,
            tool_calls_per_minute=config.rate_limit.tool_calls_per_minute,
            store_path=data_dir / "ratelimit" / "state.json",
        )

    # Create cron service first (callback set after agent creation)
    cron_store_path = data_dir / "cron" / "jobs.json"
    cron = CronService(cron_store_path)
    usage_tracker = UsageTracker(
        store_path=data_dir / "usage" / "events.jsonl",
        pricing=config.usage.pricing,
        aggregation_windows=config.usage.aggregation_windows,
    )
    compliance_service = ComplianceService(
        workspace=config.workspace_path,
        retention=config.retention,
        data_dir=data_dir,
        usage_tracker=usage_tracker,
    )
    alert_service = AlertService(config.alerts)
    defaults = config.agents.defaults

    def _scoped_secret_store(scope: str | None):
        return ScopedSecretStore(secret_store, scope=str(scope or "shared"))

    def _build_agent_loop(
        *,
        agent_id: str,
        model: str,
        thinking: str,
        max_iterations: int,
        context_window: int,
        embedding_model: str,
        supports_vision: bool,
        timeout_seconds: int,
        stream_events: bool,
        queue_config,
        credential_scope: str,
        reply_shaping: bool,
        no_reply_token: str,
    ) -> AgentLoop:
        provider = _make_provider(
            config,
            model=model,
            thinking=thinking,
            secret_store=secret_store,
        )
        return AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            agent_id=agent_id,
            model=model,
            max_iterations=max_iterations,
            brave_api_key=config.tools.web.search.api_key or None,
            exec_config=config.tools.exec,
            cron_service=cron,
            sandbox_mode=config.tools.sandbox.mode,
            sandbox_scope=config.tools.sandbox.scope,
            sandbox_workspace_access=config.tools.sandbox.workspace_access,
            sandbox_image=config.tools.sandbox.image,
            sandbox_prune_idle_seconds=config.tools.sandbox.prune_idle_seconds,
            sandbox_prune_max_age_seconds=config.tools.sandbox.prune_max_age_seconds,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            approval_config=config.tools.approval,
            audit_logger=audit_logger,
            rate_limiter=rate_limiter,
            context_window=context_window,
            embedding_model=embedding_model,
            supports_vision=supports_vision,
            timeout_seconds=timeout_seconds,
            stream_events=stream_events,
            queue_config=queue_config,
            session_policy=config.sessions,
            process_manager=process_manager,
            reply_shaping=reply_shaping,
            no_reply_token=no_reply_token,
            hook_config=config.hooks,
            secret_store=_scoped_secret_store(credential_scope),
            usage_tracker=usage_tracker,
        )

    agent_loops: dict[str, AgentLoop] = {}
    if config.agents.instances:
        for instance in config.agents.instances:
            instance_id = instance.id.strip()
            model = instance.model or defaults.model
            thinking = instance.thinking or defaults.thinking
            agent_loops[instance_id] = _build_agent_loop(
                agent_id=instance_id,
                model=model,
                thinking=thinking,
                max_iterations=(
                    instance.max_tool_iterations
                    if instance.max_tool_iterations is not None
                    else defaults.max_tool_iterations
                ),
                context_window=(
                    instance.context_window
                    if instance.context_window is not None
                    else defaults.context_window
                ),
                embedding_model=(
                    instance.embedding_model
                    if instance.embedding_model is not None
                    else defaults.embedding_model
                ),
                supports_vision=(
                    instance.supports_vision
                    if instance.supports_vision is not None
                    else defaults.supports_vision
                ),
                timeout_seconds=(
                    instance.timeout_seconds
                    if instance.timeout_seconds is not None
                    else defaults.timeout_seconds
                ),
                stream_events=(
                    instance.stream_events
                    if instance.stream_events is not None
                    else defaults.stream_events
                ),
                queue_config=instance.queue if instance.queue is not None else defaults.queue,
                credential_scope=(
                    instance.credential_scope
                    if instance.credential_scope is not None
                    else defaults.credential_scope
                ),
                reply_shaping=(
                    instance.reply_shaping
                    if instance.reply_shaping is not None
                    else defaults.reply_shaping
                ),
                no_reply_token=(
                    instance.no_reply_token
                    if instance.no_reply_token is not None
                    else defaults.no_reply_token
                ),
            )
        agent_runtime: AgentLoop | AgentRouter = AgentRouter(
            bus=bus,
            agents=agent_loops,
            default_agent_id="default",
            routing_rules=config.agents.routing.rules,
        )
        console.print(f"[green]✓[/green] Multi-agent routing: {', '.join(agent_loops.keys())}")
    else:
        single = _build_agent_loop(
            agent_id="default",
            model=defaults.model,
            thinking=defaults.thinking,
            max_iterations=defaults.max_tool_iterations,
            context_window=defaults.context_window,
            embedding_model=defaults.embedding_model,
            supports_vision=defaults.supports_vision,
            timeout_seconds=defaults.timeout_seconds,
            stream_events=defaults.stream_events,
            queue_config=defaults.queue,
            credential_scope=defaults.credential_scope,
            reply_shaping=defaults.reply_shaping,
            no_reply_token=defaults.no_reply_token,
        )
        agent_loops["default"] = single
        agent_runtime = single

    default_agent = agent_loops["default"]
    _reconcile_scheduled_session_reset_job(cron, config.sessions.scheduled_reset_cron)
    _reconcile_retention_sweep_job(cron)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        channel = job.payload.channel or "cli"
        chat_id = job.payload.to or "direct"

        if job.payload.kind == "session_reset":
            reset_count = _run_scheduled_session_reset(agent_loops)
            return f"Reset {reset_count} sessions."

        if job.payload.kind == "retention_sweep":
            result = compliance_service.sweep()
            removed = result.get("removed", {}) if isinstance(result, dict) else {}
            summary = ", ".join(
                f"{name}={int(value)}"
                for name, value in (removed.items() if isinstance(removed, dict) else [])
            ) or "no deletions"
            return f"Retention sweep complete: {summary}."

        # Reminder: deliver directly without agent
        if job.payload.kind == "reminder":
            if job.payload.deliver and job.payload.to:
                from miniclaw.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=job.payload.message,
                ))
            return job.payload.message

        # Task: run through agent
        session_key = f"cron:{job.id}" if job.payload.isolated else f"{channel}:{chat_id}"
        target_runtime = agent_runtime
        if job.payload.agent_id and hasattr(agent_runtime, "agents"):
            maybe_agent = agent_runtime.agents.get(job.payload.agent_id)  # type: ignore[attr-defined]
            if maybe_agent is not None:
                target_runtime = maybe_agent
            else:
                console.print(
                    f"[yellow]Cron warning:[/yellow] unknown agent_id '{job.payload.agent_id}', using default routing."
                )

        response = await target_runtime.process_direct(
            job.payload.message,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            model_override=job.payload.model,
        )
        if job.payload.deliver and job.payload.to:
            from miniclaw.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=response or ""
            ))
        return response
    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent_runtime.process_direct(prompt, session_key="heartbeat")

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )
    for loop in agent_loops.values():
        loop.heartbeat_service = heartbeat

    # Create channel manager
    channels = ChannelManager(config, bus, identity_store=identity_store)
    transcription_manager = TranscriptionManager.from_config(
        config.transcription,
        groq_api_key=config.providers.groq.api_key or None,
    )
    tts_adapter = KokoroTTSAdapter(
        output_dir=Path(config.transcription.tts.output_dir).expanduser(),
        default_voice=config.transcription.tts.default_voice,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")

    # Prepare dashboard if enabled
    dashboard_server = None
    openai_server = None
    if config.dashboard.enabled:
        import uvicorn

        from miniclaw.dashboard.app import create_app
        token = config.dashboard.token
        if not token:
            token = generate_token()
            config.dashboard.token = token
            save_config(config, get_config_path())
            console.print(f"[yellow]Dashboard token generated:[/yellow] {token}")
        app = create_app(
            config=config,
            config_path=get_config_path(),
            sessions_manager=default_agent.sessions,
            cron_service=cron,
            heartbeat_service=heartbeat,
            skills_loader=default_agent.context.skills,
            agent_loop=agent_runtime,
            token=token,
            bus=bus,
            channels_manager=channels,
            memory_store=default_agent.context.memory,
            secret_store=secret_store,
            process_manager=process_manager,
            identity_store=identity_store,
            distributed_manager=distributed_manager if config.distributed.enabled else None,
            usage_tracker=usage_tracker,
            compliance_service=compliance_service,
            alert_service=alert_service,
        )
        uv_config = uvicorn.Config(app, host=config.gateway.host, port=config.dashboard.port, log_level="info")
        dashboard_server = uvicorn.Server(uv_config)
        console.print(f"[green]✓[/green] Dashboard: http://{config.gateway.host}:{config.dashboard.port}")

    if config.api.openai_compat.enabled:
        import uvicorn

        from miniclaw.api.openai_compat import create_openai_compat_app

        openai_app = create_openai_compat_app(
            config=config,
            provider=default_agent.provider,
            agent_runtime=agent_runtime,
            transcription_manager=transcription_manager,
            tts_adapter=tts_adapter,
            usage_tracker=usage_tracker,
        )
        openai_config = uvicorn.Config(
            openai_app,
            host=config.api.openai_compat.host,
            port=config.api.openai_compat.port,
            log_level="info",
        )
        openai_server = uvicorn.Server(openai_config)
        console.print(
            f"[green]✓[/green] OpenAI compat API: "
            f"http://{config.api.openai_compat.host}:{config.api.openai_compat.port}"
        )

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await alert_service.start(
                bus=bus,
                agent_loop=agent_runtime,
                cron_service=cron,
                channels_manager=channels,
                distributed_manager=distributed_manager if config.distributed.enabled else None,
            )
            dashboard_task = None
            openai_task = None
            if dashboard_server:
                dashboard_task = asyncio.create_task(dashboard_server.serve())
            if openai_server:
                openai_task = asyncio.create_task(openai_server.serve())
            await asyncio.gather(
                agent_runtime.run(),
                channels.start_all(),
                dashboard_task if dashboard_task else asyncio.sleep(0),
                openai_task if openai_task else asyncio.sleep(0),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            await alert_service.stop()
            heartbeat.stop()
            cron.stop()
            agent_runtime.stop()
            if dashboard_server:
                dashboard_server.should_exit = True
            if openai_server:
                openai_server.should_exit = True
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from miniclaw.agent.loop import AgentLoop
    from miniclaw.audit.logger import AuditLogger
    from miniclaw.bus.queue import MessageBus
    from miniclaw.config.loader import get_data_dir, load_config
    from miniclaw.processes.manager import ProcessManager
    from miniclaw.ratelimit.limiter import RateLimiter
    from miniclaw.secrets import ScopedSecretStore, SecretStore
    from miniclaw.usage import UsageTracker

    config = load_config()
    data_dir = get_data_dir()
    secret_store = SecretStore()
    process_manager = ProcessManager(
        workspace=config.workspace_path,
        restrict_to_workspace=config.tools.restrict_to_workspace,
    )
    usage_tracker = UsageTracker(
        store_path=data_dir / "usage" / "events.jsonl",
        pricing=config.usage.pricing,
        aggregation_windows=config.usage.aggregation_windows,
    )

    bus = MessageBus()
    provider = _make_provider(config, secret_store=secret_store)

    audit_logger = None
    if config.audit.enabled:
        from miniclaw.config.loader import get_data_dir
        audit_logger = AuditLogger(get_data_dir() / "audit.log", level=config.audit.level)

    rate_limiter = None
    if config.rate_limit.enabled:
        rate_limiter = RateLimiter(
            messages_per_minute=config.rate_limit.messages_per_minute,
            tool_calls_per_minute=config.rate_limit.tool_calls_per_minute,
            store_path=data_dir / "ratelimit" / "state.json",
        )

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        agent_id="default",
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        sandbox_mode=config.tools.sandbox.mode,
        sandbox_scope=config.tools.sandbox.scope,
        sandbox_workspace_access=config.tools.sandbox.workspace_access,
        sandbox_image=config.tools.sandbox.image,
        sandbox_prune_idle_seconds=config.tools.sandbox.prune_idle_seconds,
        sandbox_prune_max_age_seconds=config.tools.sandbox.prune_max_age_seconds,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        approval_config=config.tools.approval,
        audit_logger=audit_logger,
        rate_limiter=rate_limiter,
        context_window=config.agents.defaults.context_window,
        embedding_model=config.agents.defaults.embedding_model,
        supports_vision=config.agents.defaults.supports_vision,
        timeout_seconds=config.agents.defaults.timeout_seconds,
        stream_events=config.agents.defaults.stream_events,
        queue_config=config.agents.defaults.queue,
        session_policy=config.sessions,
        process_manager=process_manager,
        reply_shaping=config.agents.defaults.reply_shaping,
        no_reply_token=config.agents.defaults.no_reply_token,
        hook_config=config.hooks,
        secret_store=ScopedSecretStore(
            secret_store,
            scope=config.agents.defaults.credential_scope,
        ),
        usage_tracker=usage_tracker,
    )

    if message:
        # Single message mode
        async def run_once():
            response = await agent_loop.process_direct(message, session_id)
            console.print(f"\n{__logo__} {response}")

        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await agent_loop.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from miniclaw.config.loader import load_config
    from miniclaw.secrets import SecretStore

    config = load_config()
    store = SecretStore()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    wa_details = (
        f"{wa.bridge_url} | host={wa.bridge_host} | token={'configured' if wa.bridge_auth_token else 'missing'}"
    )
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa_details,
    )

    # Telegram
    tg = config.channels.telegram
    token = tg.token or (store.get(TELEGRAM_TOKEN_SECRET_KEY) or "")
    if token and not tg.token:
        tg_config = "[dim]token in SecretStore[/dim]"
    elif token:
        tg_config = f"token: {token[:10]}..."
    else:
        tg_config = "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    user_bridge = Path.home() / ".miniclaw" / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # miniclaw/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall miniclaw")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import os
    import subprocess
    from urllib.parse import urlparse

    from miniclaw.config.loader import load_config

    bridge_dir = _get_bridge_dir()
    config = load_config()
    wa = config.channels.whatsapp
    bridge_token = str(wa.bridge_auth_token or "").strip()
    if not bridge_token:
        console.print("[red]channels.whatsapp.bridge_auth_token is required before QR login.[/red]")
        console.print("[dim]Set it in config, then rerun: miniclaw channels login[/dim]")
        raise typer.Exit(1)
    parsed = urlparse(str(wa.bridge_url or ""))
    bridge_port = parsed.port or 3001
    bridge_host = str(wa.bridge_host or parsed.hostname or "127.0.0.1")
    env = os.environ.copy()
    env["BRIDGE_AUTH_TOKEN"] = bridge_token
    env["BRIDGE_HOST"] = bridge_host
    env["BRIDGE_PORT"] = str(bridge_port)

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from miniclaw.config.loader import get_data_dir
    from miniclaw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from miniclaw.config.loader import get_data_dir
    from miniclaw.cron.service import CronService
    from miniclaw.cron.types import CronSchedule

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from miniclaw.config.loader import get_data_dir
    from miniclaw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from miniclaw.config.loader import get_data_dir
    from miniclaw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from miniclaw.config.loader import get_data_dir
    from miniclaw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Service Commands
# ============================================================================


service_app = typer.Typer(help="Manage miniclaw as a user service")
app.add_typer(service_app, name="service")


def _service_cmd(args: list[str]) -> tuple[int, str, str]:
    """Run a service command and capture output."""
    import subprocess

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _launchd_domain() -> str:
    import os

    return f"gui/{os.getuid()}"


@service_app.command("install")
def service_install(
    auto_start: bool | None = typer.Option(
        None,
        "--auto-start/--no-auto-start",
        help="Enable or disable auto-start on login/startup.",
    ),
):
    """Install miniclaw gateway as a user service."""
    import shutil

    from miniclaw.cli.service import (
        SERVICE_LABEL,
        SYSTEMD_UNIT_NAME,
        detect_service_manager,
        get_service_file_path,
        render_service_definition,
        write_if_changed,
    )
    from miniclaw.config.loader import get_config_path, load_config, save_config

    manager = detect_service_manager()
    if not manager:
        console.print("[red]Unsupported OS for service management[/red]")
        raise typer.Exit(1)

    if manager == "launchd" and not shutil.which("launchctl"):
        console.print("[red]launchctl is not available[/red]")
        raise typer.Exit(1)
    if manager == "systemd" and not shutil.which("systemctl"):
        console.print("[red]systemctl is not available[/red]")
        raise typer.Exit(1)

    config = load_config()
    if auto_start is not None:
        config.service.auto_start = auto_start
    config.service.enabled = True

    service_path = get_service_file_path()
    if not service_path:
        console.print("[red]Unable to resolve service file path[/red]")
        raise typer.Exit(1)

    log_dir = Path.home() / ".miniclaw" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    content, _ = render_service_definition(
        workspace=config.workspace_path,
        auto_start=config.service.auto_start,
        log_dir=log_dir,
    )
    if not content:
        console.print("[red]Unable to render service definition[/red]")
        raise typer.Exit(1)

    changed = write_if_changed(service_path, content)

    if manager == "systemd":
        code, _, err = _service_cmd(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            console.print(f"[red]Failed to reload systemd user units:[/red] {err}")
            raise typer.Exit(1)
        if config.service.auto_start:
            code, _, err = _service_cmd(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
            if code != 0:
                console.print(f"[red]Failed to enable/start {SYSTEMD_UNIT_NAME}:[/red] {err}")
                raise typer.Exit(1)
    else:
        if config.service.auto_start:
            domain = _launchd_domain()
            target = f"{domain}/{SERVICE_LABEL}"
            _service_cmd(["launchctl", "bootout", target])
            code, _, err = _service_cmd(["launchctl", "bootstrap", domain, str(service_path)])
            if code != 0 and "already" not in err.lower() and "in use" not in err.lower():
                console.print(f"[red]Failed to bootstrap launch agent:[/red] {err}")
                raise typer.Exit(1)
            _service_cmd(["launchctl", "kickstart", "-k", target])

    save_config(config, get_config_path())
    action = "Updated" if changed else "Unchanged"
    console.print(f"[green]✓[/green] {action} service definition at {service_path}")


@service_app.command("uninstall")
def service_uninstall():
    """Uninstall miniclaw user service definition."""
    import shutil

    from miniclaw.cli.service import (
        SERVICE_LABEL,
        SYSTEMD_UNIT_NAME,
        detect_service_manager,
        get_service_file_path,
    )
    from miniclaw.config.loader import get_config_path, load_config, save_config

    manager = detect_service_manager()
    if not manager:
        console.print("[red]Unsupported OS for service management[/red]")
        raise typer.Exit(1)

    service_path = get_service_file_path()
    if not service_path:
        console.print("[red]Unable to resolve service file path[/red]")
        raise typer.Exit(1)

    if manager == "launchd" and shutil.which("launchctl"):
        target = f"{_launchd_domain()}/{SERVICE_LABEL}"
        _service_cmd(["launchctl", "bootout", target])
    elif manager == "systemd" and shutil.which("systemctl"):
        _service_cmd(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
        _service_cmd(["systemctl", "--user", "daemon-reload"])

    removed = False
    if service_path.exists():
        service_path.unlink()
        removed = True

    config = load_config()
    config.service.enabled = False
    save_config(config, get_config_path())

    if removed:
        console.print(f"[green]✓[/green] Removed service definition {service_path}")
    else:
        console.print(f"[yellow]Service definition not found:[/yellow] {service_path}")


@service_app.command("start")
def service_start():
    """Start the miniclaw user service."""
    import shutil

    from miniclaw.cli.service import (
        SERVICE_LABEL,
        SYSTEMD_UNIT_NAME,
        detect_service_manager,
        get_service_file_path,
    )

    manager = detect_service_manager()
    if not manager:
        console.print("[red]Unsupported OS for service management[/red]")
        raise typer.Exit(1)

    service_path = get_service_file_path()
    if not service_path or not service_path.exists():
        console.print("[red]Service is not installed. Run `miniclaw service install` first.[/red]")
        raise typer.Exit(1)

    if manager == "systemd":
        if not shutil.which("systemctl"):
            console.print("[red]systemctl is not available[/red]")
            raise typer.Exit(1)
        code, _, err = _service_cmd(["systemctl", "--user", "start", SYSTEMD_UNIT_NAME])
        if code != 0:
            console.print(f"[red]Failed to start {SYSTEMD_UNIT_NAME}:[/red] {err}")
            raise typer.Exit(1)
    else:
        if not shutil.which("launchctl"):
            console.print("[red]launchctl is not available[/red]")
            raise typer.Exit(1)
        (Path.home() / ".miniclaw" / "logs").mkdir(parents=True, exist_ok=True)
        domain = _launchd_domain()
        target = f"{domain}/{SERVICE_LABEL}"
        code, _, err = _service_cmd(["launchctl", "bootstrap", domain, str(service_path)])
        if code != 0 and "already" not in err.lower() and "in use" not in err.lower():
            console.print(f"[red]Failed to bootstrap launch agent:[/red] {err}")
            raise typer.Exit(1)
        code, _, err = _service_cmd(["launchctl", "kickstart", "-k", target])
        if code != 0:
            console.print(f"[red]Failed to start launch agent:[/red] {err}")
            raise typer.Exit(1)

    console.print("[green]✓[/green] Service started")


@service_app.command("stop")
def service_stop():
    """Stop the miniclaw user service."""
    import shutil

    from miniclaw.cli.service import SERVICE_LABEL, SYSTEMD_UNIT_NAME, detect_service_manager

    manager = detect_service_manager()
    if not manager:
        console.print("[red]Unsupported OS for service management[/red]")
        raise typer.Exit(1)

    if manager == "systemd":
        if not shutil.which("systemctl"):
            console.print("[red]systemctl is not available[/red]")
            raise typer.Exit(1)
        code, _, err = _service_cmd(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])
        if code != 0:
            console.print(f"[red]Failed to stop {SYSTEMD_UNIT_NAME}:[/red] {err}")
            raise typer.Exit(1)
    else:
        if not shutil.which("launchctl"):
            console.print("[red]launchctl is not available[/red]")
            raise typer.Exit(1)
        target = f"{_launchd_domain()}/{SERVICE_LABEL}"
        code, _, err = _service_cmd(["launchctl", "bootout", target])
        if code != 0 and "could not find service" not in err.lower():
            console.print(f"[red]Failed to stop launch agent:[/red] {err}")
            raise typer.Exit(1)

    console.print("[green]✓[/green] Service stopped")


@service_app.command("status")
def service_status():
    """Show miniclaw user service status."""
    import shutil

    from miniclaw.cli.service import (
        SERVICE_LABEL,
        SYSTEMD_UNIT_NAME,
        detect_service_manager,
        get_service_file_path,
    )

    manager = detect_service_manager()
    if not manager:
        console.print("[red]Unsupported OS for service management[/red]")
        raise typer.Exit(1)

    service_path = get_service_file_path()
    installed = bool(service_path and service_path.exists())
    console.print(f"Service manager: {manager}")
    console.print(f"Definition file: {service_path} {'[green]✓[/green]' if installed else '[red]✗[/red]'}")

    if manager == "systemd":
        if not shutil.which("systemctl"):
            console.print("[yellow]systemctl not available[/yellow]")
            return
        _, active_out, _ = _service_cmd(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
        _, enabled_out, _ = _service_cmd(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME])
        active = active_out == "active"
        enabled = enabled_out == "enabled"
        console.print(f"Active: {'[green]yes[/green]' if active else '[dim]no[/dim]'}")
        console.print(f"Enabled: {'[green]yes[/green]' if enabled else '[dim]no[/dim]'}")
    else:
        if not shutil.which("launchctl"):
            console.print("[yellow]launchctl not available[/yellow]")
            return
        target = f"{_launchd_domain()}/{SERVICE_LABEL}"
        code, _, _ = _service_cmd(["launchctl", "print", target])
        active = code == 0
        console.print(f"Active: {'[green]yes[/green]' if active else '[dim]no[/dim]'}")


# ============================================================================
# Auth Commands
# ============================================================================


auth_app = typer.Typer(help="Manage provider OAuth credentials")
app.add_typer(auth_app, name="auth")


def _normalize_oauth_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized not in {"openai", "anthropic"}:
        raise typer.BadParameter("provider must be one of: openai, anthropic")
    return normalized


@auth_app.command("login")
def auth_login(
    provider: str = typer.Option(..., "--provider", "-p", help="OAuth provider: openai|anthropic"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the browser automatically."),
):
    """Log in with OAuth device flow for OpenAI or Anthropic."""
    import datetime as dt
    import webbrowser

    from miniclaw.config.loader import get_config_path, load_config, save_config
    from miniclaw.providers.oauth import (
        get_oauth_adapter,
        resolve_oauth_token_ref,
        save_token_to_store,
    )
    from miniclaw.secrets import SecretStore

    selected_provider = _normalize_oauth_provider(provider)
    config = load_config()
    provider_cfg = getattr(config.providers, selected_provider)
    adapter = get_oauth_adapter(selected_provider)
    store = SecretStore()

    try:
        flow = adapter.start_device_flow()
    except Exception as exc:
        console.print(f"[red]OAuth device flow start failed for {selected_provider}:[/red] {exc}")
        raise typer.Exit(1)

    verify_url = flow.verification_uri_complete or flow.verification_uri
    console.print(f"Provider: [cyan]{selected_provider}[/cyan]")
    console.print(f"Verification URL: [cyan]{verify_url}[/cyan]")
    console.print(f"User code: [bold]{flow.user_code}[/bold]")

    if not no_browser:
        try:
            if webbrowser.open(verify_url):
                console.print("[green]✓[/green] Browser opened for authorization.")
        except Exception:
            pass

    console.print("Waiting for OAuth authorization...")
    try:
        token = adapter.poll_for_token(flow)
    except Exception as exc:
        console.print(f"[red]OAuth authorization failed for {selected_provider}:[/red] {exc}")
        if provider_cfg.api_key:
            console.print(
                f"[yellow]API-key fallback remains available via providers.{selected_provider}.apiKey.[/yellow]"
            )
        raise typer.Exit(1)

    token_ref = resolve_oauth_token_ref(selected_provider, provider_cfg.oauth_token_ref)
    if not save_token_to_store(store, token_ref, token):
        console.print("[red]Failed to persist OAuth token in SecretStore.[/red]")
        raise typer.Exit(1)

    provider_cfg.auth_mode = "oauth"
    provider_cfg.oauth_token_ref = token_ref
    save_config(config, get_config_path())

    expires_msg = ""
    if token.expires_at:
        expires_at = dt.datetime.fromtimestamp(token.expires_at).isoformat(sep=" ", timespec="seconds")
        expires_msg = f" (expires {expires_at})"
    console.print(
        f"[green]✓[/green] OAuth login successful for {selected_provider}{expires_msg}. "
        f"Token ref: [cyan]{token_ref}[/cyan]"
    )


@auth_app.command("status")
def auth_status():
    """Show OAuth/API-key auth status for OpenAI and Anthropic."""
    from miniclaw.config.loader import load_config
    from miniclaw.providers.oauth import load_token_from_store, resolve_oauth_token_ref
    from miniclaw.secrets import SecretStore

    config = load_config()
    store = SecretStore()

    table = Table(title="Provider Auth Status")
    table.add_column("Provider", style="cyan")
    table.add_column("Auth Mode")
    table.add_column("OAuth Token")
    table.add_column("API Key Fallback")
    table.add_column("Token Ref", style="dim")

    for provider_name in ("openai", "anthropic"):
        provider_cfg = getattr(config.providers, provider_name)
        token_ref = resolve_oauth_token_ref(provider_name, provider_cfg.oauth_token_ref)
        token = load_token_from_store(store, token_ref)

        if token is None:
            oauth_status = "[dim]missing[/dim]"
        elif token.is_expired(skew_seconds=0):
            oauth_status = (
                "[yellow]expired (refresh token present)[/yellow]"
                if token.refresh_token
                else "[red]expired[/red]"
            )
        else:
            oauth_status = "[green]valid[/green]"

        fallback = "[green]yes[/green]" if provider_cfg.api_key else "[dim]no[/dim]"
        table.add_row(
            provider_name,
            provider_cfg.auth_mode,
            oauth_status,
            fallback,
            token_ref,
        )

    console.print(table)


@auth_app.command("logout")
def auth_logout(
    provider: str = typer.Option(..., "--provider", "-p", help="OAuth provider: openai|anthropic"),
    revoke: bool = typer.Option(True, "--revoke/--no-revoke", help="Attempt provider token revocation."),
):
    """Log out OAuth credentials for the selected provider."""
    from miniclaw.config.loader import get_config_path, load_config, save_config
    from miniclaw.providers.oauth import (
        delete_token_from_store,
        get_oauth_adapter,
        load_token_from_store,
        resolve_oauth_token_ref,
    )
    from miniclaw.secrets import SecretStore

    selected_provider = _normalize_oauth_provider(provider)
    config = load_config()
    provider_cfg = getattr(config.providers, selected_provider)
    store = SecretStore()
    token_ref = resolve_oauth_token_ref(selected_provider, provider_cfg.oauth_token_ref)
    token = load_token_from_store(store, token_ref)

    if revoke and token and token.access_token:
        try:
            adapter = get_oauth_adapter(selected_provider)
            adapter.revoke_token(token.access_token)
        except Exception as exc:
            console.print(f"[yellow]OAuth token revocation failed:[/yellow] {exc}")

    removed = delete_token_from_store(store, token_ref)
    provider_cfg.auth_mode = "api_key"
    if not provider_cfg.oauth_token_ref:
        provider_cfg.oauth_token_ref = token_ref
    save_config(config, get_config_path())

    if removed:
        console.print(f"[green]✓[/green] Logged out {selected_provider} OAuth credentials.")
    else:
        console.print(f"[yellow]No stored OAuth token found for {selected_provider}.[/yellow]")


security_app = typer.Typer(help="Security audits and remediation")
app.add_typer(security_app, name="security")


@security_app.command("audit")
def security_audit(
    fix: bool = typer.Option(False, "--fix", help="Apply secure configuration remediations."),
    json_output: bool = typer.Option(False, "--json", help="Output report as JSON."),
):
    """Audit security posture and optionally fix policy drift."""
    import json

    from miniclaw.cli.security import run_security_audit

    report = run_security_audit(fix=fix)
    if json_output:
        console.print(json.dumps(report.to_dict(), indent=2))
    else:
        table = Table(title="miniclaw Security Audit")
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Message", style="yellow")
        style_map = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "error": "[red]error[/red]"}
        for item in report.checks:
            status = style_map.get(item.status, item.status)
            if item.fixed:
                status += " [dim](fixed)[/dim]"
            table.add_row(item.key, status, item.message)
        console.print(table)
        counts = report.counts()
        console.print(
            f"Summary: [green]{counts['ok']} ok[/green], "
            f"[yellow]{counts['warn']} warn[/yellow], "
            f"[red]{counts['error']} error[/red], "
            f"[cyan]{counts['fixed']} fixed[/cyan]"
        )

    if report.has_errors:
        raise typer.Exit(1)


# ============================================================================
# Doctor Command
# ============================================================================


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Apply safe, local remediations."),
    json_output: bool = typer.Option(False, "--json", help="Output diagnostics as JSON."),
):
    """Run diagnostics for config, runtime dependencies, and safety prerequisites."""
    import json

    from miniclaw.cli.doctor import run_doctor

    report = run_doctor(fix=fix)

    if json_output:
        console.print(json.dumps(report.to_dict(), indent=2))
    else:
        table = Table(title="miniclaw Doctor")
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Message", style="yellow")

        style_map = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "error": "[red]error[/red]"}
        for item in report.checks:
            status = style_map.get(item.status, item.status)
            if item.fixed:
                status += " [dim](fixed)[/dim]"
            table.add_row(item.key, status, item.message)

        console.print(table)
        counts = report.counts()
        console.print(
            f"Summary: [green]{counts['ok']} ok[/green], "
            f"[yellow]{counts['warn']} warn[/yellow], "
            f"[red]{counts['error']} error[/red], "
            f"[cyan]{counts['fixed']} fixed[/cyan]"
        )

    if report.has_errors:
        raise typer.Exit(1)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show miniclaw status."""
    from miniclaw.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} miniclaw Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys
        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        has_zhipu = bool(config.providers.zhipu.api_key)
        has_vllm = bool(config.providers.vllm.api_base)
        has_aihubmix = bool(config.providers.aihubmix.api_key)

        console.print(f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}")
        console.print(f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}")
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        console.print(f"Zhipu AI API: {'[green]✓[/green]' if has_zhipu else '[dim]not set[/dim]'}")
        console.print(f"AiHubMix API: {'[green]✓[/green]' if has_aihubmix else '[dim]not set[/dim]'}")
        vllm_status = f"[green]✓ {config.providers.vllm.api_base}[/green]" if has_vllm else "[dim]not set[/dim]"
        console.print(f"vLLM/Local: {vllm_status}")


if __name__ == "__main__":
    app()
