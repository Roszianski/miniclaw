"""Service helpers for launchd (macOS) and systemd user units (Linux)."""

from __future__ import annotations

import platform
import shlex
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

SERVICE_LABEL = "com.miniclaw.gateway"
SYSTEMD_UNIT_NAME = "miniclaw.service"


def detect_service_manager(system_name: str | None = None) -> str | None:
    """Detect supported user service manager for the current OS."""
    system = system_name or platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux":
        return "systemd"
    return None


def get_service_file_path(home: Path | None = None, system_name: str | None = None) -> Path | None:
    """Get the user service definition path for the current OS."""
    home_dir = home or Path.home()
    manager = detect_service_manager(system_name)
    if manager == "launchd":
        return home_dir / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if manager == "systemd":
        return home_dir / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    return None


def resolve_exec_args() -> list[str]:
    """Resolve command args used to run miniclaw gateway."""
    miniclaw_bin = shutil.which("miniclaw")
    if miniclaw_bin:
        return [miniclaw_bin, "gateway"]
    return [sys.executable, "-m", "miniclaw", "gateway"]


def quote_command(args: list[str]) -> str:
    """Build a shell-safe command line from argv."""
    return " ".join(shlex.quote(arg) for arg in args)


def render_launchd_plist(
    exec_args: list[str],
    workspace: Path,
    log_dir: Path,
    auto_start: bool,
) -> str:
    """Render launchd plist content."""
    program_args = "\n".join(f"      <string>{escape(arg)}</string>" for arg in exec_args)
    run_at_load = "true" if auto_start else "false"
    keep_alive = "true" if auto_start else "false"
    stdout_path = escape(str(log_dir / "gateway.out.log"))
    stderr_path = escape(str(log_dir / "gateway.err.log"))
    working_dir = escape(str(workspace))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
    <key>RunAtLoad</key>
    <{run_at_load}/>
    <key>KeepAlive</key>
    <{keep_alive}/>
  </dict>
</plist>
"""


def render_systemd_unit(exec_args: list[str], workspace: Path) -> str:
    """Render systemd user unit content."""
    command = quote_command(exec_args)
    working_dir = str(workspace)
    return f"""[Unit]
Description=miniclaw gateway
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={command}
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def render_service_definition(
    workspace: Path,
    auto_start: bool,
    log_dir: Path,
    system_name: str | None = None,
    exec_args: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Render service definition based on platform."""
    manager = detect_service_manager(system_name)
    args = exec_args or resolve_exec_args()
    if manager == "launchd":
        return render_launchd_plist(args, workspace=workspace, log_dir=log_dir, auto_start=auto_start), manager
    if manager == "systemd":
        return render_systemd_unit(args, workspace=workspace), manager
    return None, None


def write_if_changed(path: Path, content: str) -> bool:
    """Write file only when content differs. Returns True if changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True
