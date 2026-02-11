"""Lifecycle hook runner for agent/session events."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from loguru import logger

HOOK_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "Stop",
}


@dataclass
class HookRunResult:
    """Result summary for a hook event run."""

    event: str
    executed: int = 0
    blocked: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class HookRunner:
    """Executes workspace-configured lifecycle hooks."""

    def __init__(
        self,
        workspace: Path,
        enabled: bool = False,
        path: str = "workspace/hooks",
        config_file: str = "hooks.json",
        timeout_seconds: int = 8,
        safe_mode: bool = True,
        allow_command_prefixes: list[str] | None = None,
        deny_command_patterns: list[str] | None = None,
    ):
        self.workspace = workspace
        self.enabled = enabled
        self.timeout_seconds = max(1, timeout_seconds)
        self.hooks_dir = self._resolve_hooks_dir(path)
        self.config_path = self.hooks_dir / config_file
        self.safe_mode = safe_mode
        self.allow_command_prefixes = [p for p in (allow_command_prefixes or []) if p]
        self.deny_command_patterns = [p.lower() for p in (deny_command_patterns or []) if p]

    def _resolve_hooks_dir(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        # Backward-compatible handling for defaults like "workspace/hooks"
        parts = list(p.parts)
        if parts and parts[0] == "workspace":
            return self.workspace.joinpath(*parts[1:])
        return self.workspace / p

    def _load_config(self) -> dict[str, Any]:
        if not self.enabled or not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
                return data["hooks"]
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            logger.warning(f"Failed loading hook config {self.config_path}: {exc}")
            return {}

    def _normalize_entries(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [{"command": raw}]
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            out: list[dict[str, Any]] = []
            for item in raw:
                if isinstance(item, str):
                    out.append({"command": item})
                elif isinstance(item, dict):
                    out.append(item)
            return out
        return []

    def _matches_tool(self, entry: dict[str, Any], tool_name: str | None) -> bool:
        if not tool_name:
            return True
        patterns = (
            entry.get("matchers")
            or entry.get("tool_matchers")
            or entry.get("toolMatchers")
            or entry.get("tools")
            or []
        )
        if not patterns:
            return True
        if isinstance(patterns, str):
            patterns = [patterns]
        return any(fnmatch(tool_name, str(pat)) for pat in patterns)

    async def run(self, event: str, payload: dict[str, Any] | None = None) -> HookRunResult:
        """Run hooks for an event. PreToolUse hooks can block execution."""
        result = HookRunResult(event=event)
        if not self.enabled:
            return result
        if event not in HOOK_EVENTS:
            result.errors.append(f"Unknown hook event: {event}")
            return result

        cfg = self._load_config()
        entries = self._normalize_entries(cfg.get(event))
        if not entries:
            return result

        payload = payload or {}
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip() or None
        for entry in entries:
            if entry.get("enabled", True) is False:
                continue
            if not self._matches_tool(entry, tool_name):
                continue
            command = str(entry.get("command") or entry.get("cmd") or "").strip()
            if not command:
                continue
            allowed, reason = self._is_command_allowed(command)
            if not allowed:
                msg = reason or f"{event} hook command denied by safety policy"
                result.errors.append(msg)
                if event == "PreToolUse":
                    result.blocked = True
                    break
                continue
            timeout_s = int(entry.get("timeout_seconds") or self.timeout_seconds)
            ok, err = await self._run_command(command, event=event, payload=payload, timeout_s=timeout_s)
            result.executed += 1
            if not ok and err:
                result.errors.append(err)
            if event == "PreToolUse" and not ok:
                result.blocked = True
                break
        return result

    async def _run_command(
        self,
        command: str,
        event: str,
        payload: dict[str, Any],
        timeout_s: int,
    ) -> tuple[bool, str | None]:
        env = os.environ.copy()
        env["MINICLAW_HOOK_EVENT"] = event
        env["MINICLAW_HOOK_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout_s))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            msg = f"{event} hook timed out after {timeout_s}s: {command}"
            logger.warning(msg)
            return False, msg
        except Exception as exc:
            msg = f"{event} hook failed to execute: {exc}"
            logger.warning(msg)
            return False, msg

        if proc.returncode == 0:
            return True, None

        stderr_text = (stderr.decode(errors="replace") if stderr else "").strip()
        stdout_text = (stdout.decode(errors="replace") if stdout else "").strip()
        snippet = stderr_text or stdout_text or f"exit {proc.returncode}"
        if len(snippet) > 800:
            snippet = snippet[:800] + "... (truncated)"
        msg = f"{event} hook returned non-zero ({proc.returncode}): {snippet}"
        logger.warning(msg)
        return False, msg

    def _is_command_allowed(self, command: str) -> tuple[bool, str | None]:
        if not self.safe_mode:
            return True, None

        lower = command.lower()
        for pattern in self.deny_command_patterns:
            if pattern and pattern in lower:
                return False, f"Hook command blocked by deny pattern: {pattern}"

        if self.allow_command_prefixes and not any(command.startswith(prefix) for prefix in self.allow_command_prefixes):
            joined = ", ".join(self.allow_command_prefixes)
            return False, f"Hook command not in allow prefixes ({joined})"

        return True, None
