"""Shell execution tool."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniclaw.agent.tools.base import Tool


@dataclass
class SandboxRuntimeContext:
    """Execution context used to select sandbox container scope."""

    session_key: str = ""
    agent_id: str = "default"


@dataclass
class _ContainerRecord:
    """Tracked long-lived sandbox container."""

    name: str
    created_at: float
    last_used_at: float


class DockerSandboxManager:
    """Manage long-lived Docker sandbox containers keyed by scope."""

    def __init__(
        self,
        *,
        image: str,
        scope: str,
        workspace_access: str,
        workspace_root: Path,
        resource_limits: dict[str, int],
        prune_idle_seconds: int,
        prune_max_age_seconds: int,
    ):
        self.image = image
        self.scope = scope
        self.workspace_access = workspace_access
        self.workspace_root = workspace_root
        self.resource_limits = dict(resource_limits)
        self.prune_idle_seconds = max(30, int(prune_idle_seconds))
        self.prune_max_age_seconds = max(60, int(prune_max_age_seconds))
        self._containers: dict[str, _ContainerRecord] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        command: str,
        cwd: str,
        timeout: int,
        context: SandboxRuntimeContext,
    ) -> tuple[int, str, str]:
        scope_key = self._scope_key(context)
        async with self._lock:
            await self._prune_locked()
            record = await self._ensure_container_locked(scope_key=scope_key, cwd=cwd)
            record.last_used_at = time.time()

        payload = self._build_limited_payload(command=command, cwd=cwd)
        args = self._build_exec_args(container_name=record.name, payload=payload)
        code, stdout, stderr = await self._run_cmd(args, timeout=timeout)
        if code != 0 and self._should_recreate_container(stderr):
            async with self._lock:
                await self._remove_scope_container_locked(scope_key=scope_key)
                record = await self._ensure_container_locked(scope_key=scope_key, cwd=cwd)
                record.last_used_at = time.time()
            args = self._build_exec_args(container_name=record.name, payload=payload)
            code, stdout, stderr = await self._run_cmd(args, timeout=timeout)

        async with self._lock:
            rec = self._containers.get(scope_key)
            if rec:
                rec.last_used_at = time.time()
        return code, stdout, stderr

    def _scope_key(self, context: SandboxRuntimeContext) -> str:
        agent = str(context.agent_id or "default").strip() or "default"
        if self.scope == "shared":
            return "shared"
        if self.scope == "agent":
            return f"agent:{agent}"
        session = str(context.session_key or "default").strip() or "default"
        return f"session:{agent}:{session}"

    async def _ensure_container_locked(self, *, scope_key: str, cwd: str) -> _ContainerRecord:
        existing = self._containers.get(scope_key)
        if existing and await self._is_container_running(existing.name):
            return existing
        if existing:
            await self._remove_container_by_name(existing.name)
            self._containers.pop(scope_key, None)

        name = self._container_name(scope_key)
        await self._remove_container_by_name(name)
        args = self._build_run_args(container_name=name, scope_key=scope_key, cwd=cwd)
        code, stdout, stderr = await self._run_cmd(args, timeout=30)
        if code != 0:
            detail = stderr.strip() or stdout.strip() or "unknown docker error"
            raise RuntimeError(f"Docker sandbox container start failed: {detail}")

        now = time.time()
        record = _ContainerRecord(name=name, created_at=now, last_used_at=now)
        self._containers[scope_key] = record
        return record

    async def _remove_scope_container_locked(self, *, scope_key: str) -> None:
        record = self._containers.pop(scope_key, None)
        if record:
            await self._remove_container_by_name(record.name)

    async def _prune_locked(self) -> None:
        now = time.time()
        stale: list[str] = []
        for scope_key, record in self._containers.items():
            idle = now - record.last_used_at
            age = now - record.created_at
            if idle >= self.prune_idle_seconds or age >= self.prune_max_age_seconds:
                stale.append(scope_key)
        for scope_key in stale:
            await self._remove_scope_container_locked(scope_key=scope_key)

    async def _is_container_running(self, name: str) -> bool:
        args = ["docker", "inspect", "-f", "{{.State.Running}}", name]
        code, stdout, _ = await self._run_cmd(args, timeout=8)
        return code == 0 and stdout.strip().lower() == "true"

    async def _remove_container_by_name(self, name: str) -> None:
        args = ["docker", "rm", "-f", name]
        await self._run_cmd(args, timeout=8)

    def _build_run_args(self, *, container_name: str, scope_key: str, cwd: str) -> list[str]:
        limits = self.resource_limits
        tmp_size_mb = max(16, int(limits.get("file_size_mb", 64)))
        mem_mb = max(64, int(limits.get("memory_mb", 512)))
        pids = max(4, int(limits.get("max_processes", 64)))
        workdir = self._container_cwd(cwd)

        args = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--read-only",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(pids),
            "--memory",
            f"{mem_mb}m",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={tmp_size_mb}m",
            "--tmpfs",
            "/run:rw,nosuid,nodev,noexec,size=16m",
            "--user",
            "65532:65532",
            "--workdir",
            workdir,
            "--label",
            "miniclaw.sandbox=true",
            "--label",
            f"miniclaw.scope={self.scope}",
            "--label",
            f"miniclaw.scope_key={self._short_hash(scope_key)}",
        ]

        if self.workspace_access in {"ro", "rw"}:
            args.extend(
                [
                    "-v",
                    f"{self.workspace_root}:/workspace:{self.workspace_access}",
                ]
            )
        else:
            args.extend(
                [
                    "--tmpfs",
                    "/workspace:rw,nosuid,nodev,noexec,size=64m",
                ]
            )

        args.extend(
            [
                self.image,
                "/bin/sh",
                "-lc",
                "while true; do sleep 3600; done",
            ]
        )
        return args

    @staticmethod
    def _build_exec_args(*, container_name: str, payload: str) -> list[str]:
        return ["docker", "exec", "-i", container_name, "/bin/sh", "-lc", payload]

    def _container_cwd(self, cwd: str) -> str:
        if self.workspace_access not in {"ro", "rw"}:
            return "/workspace"
        try:
            requested = Path(cwd).expanduser().resolve()
            rel = requested.relative_to(self.workspace_root)
            rel_s = rel.as_posix()
            return "/workspace" if rel_s in {"", "."} else f"/workspace/{rel_s}"
        except Exception:
            return "/workspace"

    def _build_limited_payload(self, *, command: str, cwd: str) -> str:
        limits = self.resource_limits
        container_cwd = self._container_cwd(cwd)
        cwd_q = shlex.quote(container_cwd)
        limit_cmds = [
            f"ulimit -t {limits['cpu_seconds']}",
            f"ulimit -v {limits['memory_mb'] * 1024}",
            f"ulimit -f {limits['file_size_mb'] * 2048}",
            f"ulimit -u {limits['max_processes']}",
        ]
        return "; ".join(["set -e", *limit_cmds, f"mkdir -p {cwd_q}", f"cd {cwd_q}", command])

    @staticmethod
    def _should_recreate_container(stderr: str) -> bool:
        lower = (stderr or "").lower()
        return any(
            marker in lower
            for marker in (
                "no such container",
                "is not running",
                "container not found",
            )
        )

    @staticmethod
    def _container_name(scope_key: str) -> str:
        return f"miniclaw-sbx-{DockerSandboxManager._short_hash(scope_key)}"

    @staticmethod
    def _short_hash(value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]

    async def _run_cmd(self, args: list[str], timeout: int) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=float(timeout))
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return 124, "", f"Command timed out after {timeout} seconds"
            return (
                int(process.returncode or 0),
                (stdout or b"").decode("utf-8", errors="replace"),
                (stderr or b"").decode("utf-8", errors="replace"),
            )
        except Exception as exc:
            return 1, "", str(exc)


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        sandbox_enabled: bool = False,
        sandbox_mode: str | None = None,
        sandbox_scope: str = "agent",
        sandbox_workspace_access: str = "rw",
        sandbox_image: str = "openclaw-sandbox:bookworm-slim",
        sandbox_prune_idle_seconds: int = 1800,
        sandbox_prune_max_age_seconds: int = 21600,
        sandbox_agent_id: str = "default",
        resource_limits: Any | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"\b(format|mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.resource_limits = self._normalize_resource_limits(resource_limits)

        mode_input = sandbox_mode if sandbox_mode is not None else ("all" if sandbox_enabled else "off")
        self.sandbox_mode = self._normalize_sandbox_mode(mode_input)
        self.sandbox_scope = self._normalize_sandbox_scope(sandbox_scope)
        self.sandbox_workspace_access = self._normalize_workspace_access(sandbox_workspace_access)
        self.sandbox_image = sandbox_image or "openclaw-sandbox:bookworm-slim"
        self.sandbox_prune_idle_seconds = max(30, int(sandbox_prune_idle_seconds))
        self.sandbox_prune_max_age_seconds = max(60, int(sandbox_prune_max_age_seconds))
        self.sandbox_agent_id = str(sandbox_agent_id or "default").strip() or "default"

        self._sandbox_context = SandboxRuntimeContext(agent_id=self.sandbox_agent_id)
        self._docker_sandbox: DockerSandboxManager | None = None
        if self.sandbox_mode != "off":
            root = Path(self.working_dir or os.getcwd()).expanduser().resolve()
            self._docker_sandbox = DockerSandboxManager(
                image=self.sandbox_image,
                scope=self.sandbox_scope,
                workspace_access=self.sandbox_workspace_access,
                workspace_root=root,
                resource_limits=self.resource_limits,
                prune_idle_seconds=self.sandbox_prune_idle_seconds,
                prune_max_age_seconds=self.sandbox_prune_max_age_seconds,
            )

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
            },
            "required": ["command"],
        }

    def set_registry_context(
        self,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        user_key: str,
        run_id: str,
    ) -> None:
        del channel, chat_id, user_key, run_id
        self._sandbox_context.session_key = str(session_key or "").strip()

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        del kwargs
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        try:
            if self._sandbox_is_active():
                if not shutil.which("docker"):
                    return "Error: Sandbox is enabled but Docker is unavailable (fail-closed)."
                if not self._docker_sandbox:
                    return "Error: Sandbox runtime is not initialized."
                code, stdout, stderr = await self._docker_sandbox.execute(
                    command=command,
                    cwd=cwd,
                    timeout=self.timeout,
                    context=self._sandbox_context,
                )
                return self._format_result(code=code, stdout=stdout, stderr=stderr)

            code, stdout, stderr = await self._run_host_command(command=command, cwd=cwd)
            return self._format_result(code=code, stdout=stdout, stderr=stderr)
        except Exception as exc:
            return f"Error executing command: {exc}"

    async def _run_host_command(self, *, command: str, cwd: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=float(self.timeout))
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return 124, "", f"Command timed out after {self.timeout} seconds"
        return (
            int(process.returncode or 0),
            (stdout or b"").decode("utf-8", errors="replace"),
            (stderr or b"").decode("utf-8", errors="replace"),
        )

    def _format_result(self, *, code: int, stdout: str, stderr: str) -> str:
        if code == 124 and "timed out" in (stderr or "").lower():
            return f"Error: Command timed out after {self.timeout} seconds"

        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout)
        if stderr and stderr.strip():
            output_parts.append(f"STDERR:\n{stderr}")
        if code != 0:
            output_parts.append(f"\nExit code: {code}")

        result = "\n".join(output_parts) if output_parts else "(no output)"
        max_len = 10000
        if len(result) > max_len:
            result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
        return result

    def _sandbox_is_active(self) -> bool:
        if self.sandbox_mode == "off":
            return False
        if self.sandbox_mode == "all":
            return True
        return self.sandbox_agent_id != "default"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"/[^\s\"']+", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _normalize_resource_limits(resource_limits: Any | None) -> dict[str, int]:
        defaults = {
            "cpu_seconds": 30,
            "memory_mb": 512,
            "file_size_mb": 64,
            "max_processes": 64,
        }
        if resource_limits is None:
            return defaults

        for key in defaults:
            if isinstance(resource_limits, dict):
                value = resource_limits.get(key)
            else:
                value = getattr(resource_limits, key, None)
            if isinstance(value, int) and value > 0:
                defaults[key] = value
        return defaults

    @staticmethod
    def _normalize_sandbox_mode(value: str | None) -> str:
        normalized = str(value or "off").strip().lower().replace("-", "_")
        if normalized in {"off", "non_main", "all"}:
            return normalized
        return "off"

    @staticmethod
    def _normalize_sandbox_scope(value: str | None) -> str:
        normalized = str(value or "agent").strip().lower()
        if normalized in {"session", "agent", "shared"}:
            return normalized
        return "agent"

    @staticmethod
    def _normalize_workspace_access(value: str | None) -> str:
        normalized = str(value or "rw").strip().lower()
        if normalized in {"none", "ro", "rw"}:
            return normalized
        return "rw"
