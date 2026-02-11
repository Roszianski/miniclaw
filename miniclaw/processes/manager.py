"""Background process manager."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger

from miniclaw.utils.helpers import ensure_dir, get_data_path


@dataclass
class ProcessRecord:
    """Persistent process metadata."""

    process_id: str
    name: str
    command: str
    cwd: str
    pid: int
    started_at: float
    stopped_at: float | None = None
    return_code: int | None = None
    last_error: str | None = None
    log_path: str = ""


class ProcessManager:
    """Manage long-running background processes."""

    _DEFAULT_DENY_PATTERNS = [
        r"\brm\s+-[rf]{1,2}\b",
        r"\bdel\s+/[fq]\b",
        r"\brmdir\s+/s\b",
        r"\b(format|mkfs|diskpart)\b",
        r"\bdd\s+if=",
        r">\s*/dev/sd",
        r"\b(shutdown|reboot|poweroff)\b",
        r":\(\)\s*\{.*\};\s*:",
    ]

    def __init__(
        self,
        workspace: Path,
        max_processes: int = 8,
        *,
        restrict_to_workspace: bool = True,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
    ):
        self.workspace = workspace.expanduser().resolve()
        self.max_processes = max(1, int(max_processes))
        self.restrict_to_workspace = bool(restrict_to_workspace)
        self.deny_patterns = list(deny_patterns or self._DEFAULT_DENY_PATTERNS)
        self.allow_patterns = list(allow_patterns or [])
        self.base_dir = ensure_dir(get_data_path() / "processes")
        self.logs_dir = ensure_dir(self.base_dir / "logs")
        self.store_path = self.base_dir / "processes.json"
        self._lock = RLock()
        self._records: dict[str, ProcessRecord] = {}
        self._handles: dict[str, subprocess.Popen[Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to load process store: {exc}")
            return
        rows = raw.get("processes", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                record = ProcessRecord(
                    process_id=str(row.get("process_id") or row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    command=str(row.get("command") or ""),
                    cwd=str(row.get("cwd") or ""),
                    pid=int(row.get("pid") or 0),
                    started_at=float(row.get("started_at") or time.time()),
                    stopped_at=float(row.get("stopped_at")) if row.get("stopped_at") is not None else None,
                    return_code=int(row.get("return_code")) if row.get("return_code") is not None else None,
                    last_error=str(row.get("last_error")) if row.get("last_error") is not None else None,
                    log_path=str(row.get("log_path") or ""),
                )
            except Exception:
                continue
            if record.process_id:
                self._records[record.process_id] = record

    def _save(self) -> None:
        payload = {
            "version": 1,
            "processes": [asdict(r) for r in sorted(self._records.values(), key=lambda item: item.started_at)],
        }
        self.store_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False
        return True

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if cwd:
            path = Path(cwd).expanduser()
            if not path.is_absolute():
                path = (self.workspace / path).resolve()
            else:
                path = path.resolve()
        else:
            path = self.workspace.resolve()
        if self.restrict_to_workspace:
            workspace = self.workspace.resolve()
            if path != workspace and workspace not in path.parents:
                raise ValueError(f"working directory must stay inside workspace: {workspace}")
        return path

    def _guard_command(self, command: str) -> str | None:
        cmd = (command or "").strip()
        lower = cmd.lower()
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns and not any(re.search(pattern, lower) for pattern in self.allow_patterns):
            return "command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "command blocked by safety guard (path traversal detected)"

            workspace = self.workspace.resolve()
            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"/[^\s\"']+", cmd)
            for raw in win_paths + posix_paths:
                try:
                    path = Path(raw).resolve()
                except Exception:
                    continue
                if path != workspace and workspace not in path.parents:
                    return "command blocked by safety guard (path outside workspace)"
        return None

    def _sync_status(self, record: ProcessRecord) -> None:
        handle = self._handles.get(record.process_id)
        if handle is not None:
            code = handle.poll()
            if code is not None:
                record.return_code = code
                record.stopped_at = record.stopped_at or time.time()
                self._handles.pop(record.process_id, None)
                return
        if record.stopped_at is None and not self._pid_running(record.pid):
            record.stopped_at = time.time()
            if record.return_code is None:
                record.return_code = 0

    def _running_count(self) -> int:
        running = 0
        for record in self._records.values():
            self._sync_status(record)
            if record.stopped_at is None:
                running += 1
        return running

    def list_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            for record in self._records.values():
                self._sync_status(record)
                rows.append(
                    {
                        "id": record.process_id,
                        "name": record.name,
                        "command": record.command,
                        "cwd": record.cwd,
                        "pid": record.pid,
                        "started_at": record.started_at,
                        "stopped_at": record.stopped_at,
                        "return_code": record.return_code,
                        "last_error": record.last_error,
                        "running": record.stopped_at is None,
                        "log_path": record.log_path,
                    }
                )
            rows.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
            self._save()
            return rows

    def start_process(self, command: str, cwd: str | None = None, name: str | None = None) -> dict[str, Any]:
        cmd = (command or "").strip()
        if not cmd:
            raise ValueError("command is required")

        with self._lock:
            if self._running_count() >= self.max_processes:
                raise RuntimeError(f"process limit reached ({self.max_processes})")

            working_dir = self._resolve_cwd(cwd)
            if not working_dir.exists() or not working_dir.is_dir():
                raise ValueError(f"working directory does not exist: {working_dir}")
            guard_error = self._guard_command(cmd)
            if guard_error:
                raise ValueError(guard_error)

            process_id = uuid.uuid4().hex[:8]
            display_name = (name or cmd[:40]).strip() or process_id
            log_path = self.logs_dir / f"{process_id}.log"

            log_file = open(log_path, "ab")
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(working_dir),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_file.close()

            record = ProcessRecord(
                process_id=process_id,
                name=display_name,
                command=cmd,
                cwd=str(working_dir),
                pid=int(proc.pid or 0),
                started_at=time.time(),
                log_path=str(log_path),
            )
            self._records[process_id] = record
            self._handles[process_id] = proc
            self._save()
            return {
                "id": process_id,
                "name": display_name,
                "pid": record.pid,
                "running": True,
                "started_at": record.started_at,
                "log_path": str(log_path),
            }

    def stop_process(self, process_id: str, timeout_s: float = 4.0) -> bool:
        with self._lock:
            record = self._records.get(process_id)
            if record is None:
                return False
            self._sync_status(record)
            if record.stopped_at is not None:
                self._save()
                return True

            pid = int(record.pid or 0)
            if pid <= 0:
                record.stopped_at = time.time()
                record.return_code = record.return_code if record.return_code is not None else 0
                self._save()
                return True

            def _kill(sig: int) -> None:
                try:
                    os.killpg(pid, sig)
                except Exception:
                    try:
                        os.kill(pid, sig)
                    except Exception:
                        pass

            _kill(signal.SIGTERM)
            deadline = time.time() + max(0.1, float(timeout_s))
            while time.time() < deadline:
                if not self._pid_running(pid):
                    break
                time.sleep(0.1)

            if self._pid_running(pid):
                _kill(signal.SIGKILL)
                time.sleep(0.1)

            if not self._pid_running(pid):
                record.stopped_at = time.time()
                handle = self._handles.pop(process_id, None)
                if handle is not None:
                    try:
                        record.return_code = handle.poll()
                    except Exception:
                        pass
                if record.return_code is None:
                    record.return_code = 0
                self._save()
                return True

            record.last_error = "failed to terminate process"
            self._save()
            return False

    def read_logs(self, process_id: str, tail_lines: int = 200) -> str:
        with self._lock:
            record = self._records.get(process_id)
            if record is None:
                raise KeyError(f"process '{process_id}' not found")
            log_path = Path(record.log_path)
            if not log_path.exists():
                return ""
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = max(1, min(2000, int(tail_lines)))
            return "\n".join(lines[-tail:])
