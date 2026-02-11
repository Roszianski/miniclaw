"""Distributed node registration and task dispatch manager."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows.
    fcntl = None


class DistributedNodeManager:
    """Track remote workers and assign tasks by capabilities."""

    def __init__(
        self,
        *,
        store_path: Path,
        local_node_id: str = "local-node",
        peer_allowlist: list[str] | None = None,
        heartbeat_timeout_s: int = 90,
        max_tasks: int = 1000,
    ):
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.store_path.with_suffix(f"{self.store_path.suffix}.lock")
        self.local_node_id = str(local_node_id or "local-node").strip() or "local-node"
        self.peer_allowlist = {str(v).strip() for v in (peer_allowlist or []) if str(v).strip()}
        self.heartbeat_timeout_s = max(15, int(heartbeat_timeout_s))
        self.max_tasks = max(100, int(max_tasks))
        self._lock = RLock()
        self._state = self._load_from_disk()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"nodes": {}, "tasks": {}}

    @classmethod
    def _normalize_state(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return cls._empty_state()
        nodes = value.get("nodes")
        tasks = value.get("tasks")
        out = {
            "nodes": dict(nodes) if isinstance(nodes, dict) else {},
            "tasks": dict(tasks) if isinstance(tasks, dict) else {},
        }
        return out

    def _load_from_disk(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return self._empty_state()
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty_state()
        return self._normalize_state(raw)

    def _save_to_disk(self, state: dict[str, Any]) -> None:
        normalized = self._normalize_state(state)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(normalized, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.store_path.parent),
            prefix=f"{self.store_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.store_path)

    @contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            handle.close()

    @contextmanager
    def _state_guard(self, *, write: bool):
        with self._lock:
            with self._file_lock():
                state = self._load_from_disk()
                try:
                    yield state
                finally:
                    if write:
                        self._save_to_disk(state)
                    self._state = self._normalize_state(state)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _enforce_allowlist(self, node_id: str) -> None:
        if not self.peer_allowlist:
            return
        if node_id == self.local_node_id:
            return
        if node_id not in self.peer_allowlist:
            raise ValueError(f"Node '{node_id}' is not in distributed.peer_allowlist.")

    def register_node(
        self,
        *,
        node_id: str,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        address: str = "",
    ) -> dict[str, Any]:
        node_id = str(node_id or "").strip()
        if not node_id:
            raise ValueError("node_id is required.")
        self._enforce_allowlist(node_id)

        now_ms = self._now_ms()
        with self._state_guard(write=True) as state:
            existing = state["nodes"].get(node_id, {})
            row = {
                "node_id": node_id,
                "capabilities": sorted({str(c).strip() for c in (capabilities or []) if str(c).strip()}),
                "metadata": dict(metadata or existing.get("metadata") or {}),
                "address": str(address or existing.get("address") or ""),
                "status": "online",
                "registered_at_ms": int(existing.get("registered_at_ms") or now_ms),
                "updated_at_ms": now_ms,
                "last_heartbeat_ms": now_ms,
            }
            state["nodes"][node_id] = row
            return dict(row)

    def heartbeat(
        self,
        *,
        node_id: str,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        node_id = str(node_id or "").strip()
        if not node_id:
            raise ValueError("node_id is required.")
        self._enforce_allowlist(node_id)

        now_ms = self._now_ms()
        with self._state_guard(write=True) as state:
            existing = state["nodes"].get(node_id)
            if not isinstance(existing, dict):
                row = {
                    "node_id": node_id,
                    "capabilities": sorted({str(c).strip() for c in (capabilities or []) if str(c).strip()}),
                    "metadata": dict(metadata or {}),
                    "address": "",
                    "status": "online",
                    "registered_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                    "last_heartbeat_ms": now_ms,
                }
                state["nodes"][node_id] = row
                return dict(row)

            if capabilities is not None:
                existing["capabilities"] = sorted({str(c).strip() for c in capabilities if str(c).strip()})
            if metadata is not None:
                existing["metadata"] = dict(metadata)
            existing["status"] = "online"
            existing["last_heartbeat_ms"] = now_ms
            existing["updated_at_ms"] = now_ms
            return dict(existing)

    def _list_nodes_from_state(self, state: dict[str, Any], *, include_stale: bool = False) -> list[dict[str, Any]]:
        now_ms = self._now_ms()
        out: list[dict[str, Any]] = []
        timeout_ms = self.heartbeat_timeout_s * 1000
        for node_id, row in state["nodes"].items():
            if not isinstance(row, dict):
                continue
            last_hb = int(row.get("last_heartbeat_ms") or 0)
            alive = (now_ms - last_hb) <= timeout_ms
            item = dict(row)
            item["alive"] = alive
            item["node_id"] = node_id
            if include_stale or alive:
                out.append(item)
        out.sort(key=lambda r: int(r.get("updated_at_ms") or 0), reverse=True)
        return out

    def list_nodes(self, *, include_stale: bool = False) -> list[dict[str, Any]]:
        with self._state_guard(write=False) as state:
            return self._list_nodes_from_state(state, include_stale=include_stale)

    def _select_node(
        self,
        state: dict[str, Any],
        *,
        required_capabilities: list[str] | None = None,
        preferred_node_id: str | None = None,
    ) -> str | None:
        required = {str(c).strip() for c in (required_capabilities or []) if str(c).strip()}
        nodes = self._list_nodes_from_state(state, include_stale=False)
        if preferred_node_id:
            preferred = next((n for n in nodes if n.get("node_id") == preferred_node_id), None)
            if preferred and required.issubset(set(preferred.get("capabilities") or [])):
                return str(preferred.get("node_id"))

        for node in nodes:
            caps = set(node.get("capabilities") or [])
            if required.issubset(caps):
                return str(node.get("node_id"))
        return None

    def dispatch_task(
        self,
        *,
        payload: dict[str, Any],
        required_capabilities: list[str] | None = None,
        preferred_node_id: str | None = None,
        kind: str = "generic",
    ) -> dict[str, Any]:
        with self._state_guard(write=True) as state:
            node_id = self._select_node(
                state,
                required_capabilities=required_capabilities,
                preferred_node_id=preferred_node_id,
            )
            if not node_id:
                raise ValueError("No eligible online node available for task dispatch.")

            now_ms = self._now_ms()
            task_id = f"task_{uuid.uuid4().hex[:14]}"
            row = {
                "task_id": task_id,
                "kind": str(kind or "generic"),
                "payload": dict(payload or {}),
                "required_capabilities": sorted(
                    {str(c).strip() for c in (required_capabilities or []) if str(c).strip()}
                ),
                "assigned_node_id": node_id,
                "status": "queued",
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
                "claimed_at_ms": None,
                "completed_at_ms": None,
                "result": None,
                "error": None,
            }
            state["tasks"][task_id] = row
            self._prune_tasks(state["tasks"])
            return dict(row)

    def claim_task(self, *, node_id: str) -> dict[str, Any] | None:
        node_id = str(node_id or "").strip()
        with self._state_guard(write=True) as state:
            queued = [
                row
                for row in state["tasks"].values()
                if isinstance(row, dict)
                and str(row.get("assigned_node_id") or "") == node_id
                and str(row.get("status") or "") == "queued"
            ]
            if not queued:
                return None
            queued.sort(key=lambda r: int(r.get("created_at_ms") or 0))
            row = queued[0]
            now_ms = self._now_ms()
            row["status"] = "running"
            row["claimed_at_ms"] = now_ms
            row["updated_at_ms"] = now_ms
            return dict(row)

    def complete_task(
        self,
        *,
        task_id: str,
        node_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._state_guard(write=True) as state:
            row = state["tasks"].get(str(task_id))
            if not isinstance(row, dict):
                logger.warning(
                    "Distributed task completion rejected: task not found (task_id={}, node_id={})",
                    task_id,
                    node_id,
                )
                raise KeyError("Task not found.")
            if str(row.get("assigned_node_id") or "") != str(node_id):
                raise ValueError("Task is assigned to a different node.")
            now_ms = self._now_ms()
            row["status"] = "error" if error else "completed"
            row["error"] = str(error) if error else None
            row["result"] = dict(result or {}) if result is not None else None
            row["completed_at_ms"] = now_ms
            row["updated_at_ms"] = now_ms
            return dict(row)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._state_guard(write=False) as state:
            row = state["tasks"].get(str(task_id))
            if not isinstance(row, dict):
                return None
            return dict(row)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        node_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._state_guard(write=False) as state:
            rows = []
            for row in state["tasks"].values():
                if not isinstance(row, dict):
                    continue
                if status and str(row.get("status") or "") != str(status):
                    continue
                if node_id and str(row.get("assigned_node_id") or "") != str(node_id):
                    continue
                rows.append(dict(row))
            rows.sort(key=lambda r: int(r.get("created_at_ms") or 0), reverse=True)
            return rows[: max(1, int(limit))]

    def _prune_tasks(self, tasks: dict[str, Any]) -> None:
        if len(tasks) <= self.max_tasks:
            return
        rows = [(task_id, row) for task_id, row in tasks.items() if isinstance(row, dict)]
        terminal_statuses = {"completed", "error"}
        active_ids = {
            task_id
            for task_id, row in rows
            if str(row.get("status") or "").strip().lower() not in terminal_statuses
        }
        terminal_rows = [
            (task_id, row)
            for task_id, row in rows
            if str(row.get("status") or "").strip().lower() in terminal_statuses
        ]
        terminal_rows.sort(key=lambda item: int(item[1].get("updated_at_ms") or 0), reverse=True)

        terminal_budget = max(0, self.max_tasks - len(active_ids))
        keep = set(active_ids)
        keep.update(task_id for task_id, _ in terminal_rows[:terminal_budget])
        drop = [task_id for task_id in list(tasks.keys()) if task_id not in keep]
        for task_id in drop:
            tasks.pop(task_id, None)
