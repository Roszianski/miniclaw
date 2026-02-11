"""Alerting primitives for runtime health monitoring."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any

from loguru import logger


class AlertService:
    """Collect runtime alerts from event streams + health scans."""

    def __init__(
        self,
        config: Any,
        *,
        dedupe_window_s: int = 120,
        max_events: int = 500,
        health_interval_s: int = 30,
    ):
        self.config = config
        self.enabled = bool(getattr(config, "enabled", False))
        self.dedupe_window_s = max(1, int(dedupe_window_s))
        self.max_events = max(10, int(max_events))
        self.health_interval_s = max(5, int(health_interval_s))

        self._events: deque[dict[str, Any]] = deque(maxlen=self.max_events)
        self._dedupe: dict[str, float] = {}

        self._run_listener = None
        self._run_listener_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None

        self._bus = None
        self._agent_loop = None
        self._cron_service = None
        self._channels_manager = None
        self._distributed_manager = None

    async def start(
        self,
        *,
        bus: Any | None = None,
        agent_loop: Any | None = None,
        cron_service: Any | None = None,
        channels_manager: Any | None = None,
        distributed_manager: Any | None = None,
    ) -> None:
        self._bus = bus
        self._agent_loop = agent_loop
        self._cron_service = cron_service
        self._channels_manager = channels_manager
        self._distributed_manager = distributed_manager

        if not self.enabled:
            return

        if bus is not None and hasattr(bus, "register_run_listener"):
            self._run_listener = bus.register_run_listener()
            self._run_listener_task = asyncio.create_task(self._consume_run_events())
        self._health_task = asyncio.create_task(self._poll_health())

    async def stop(self) -> None:
        for task in (self._run_listener_task, self._health_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._run_listener_task = None
        self._health_task = None

        if (
            self._bus is not None
            and self._run_listener is not None
            and hasattr(self._bus, "unregister_run_listener")
        ):
            try:
                self._bus.unregister_run_listener(self._run_listener)
            except Exception:
                pass
        self._run_listener = None

    def emit(
        self,
        *,
        event: str,
        message: str,
        severity: str = "warn",
        dedupe_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        now = time.time()
        key = dedupe_key or f"{event}:{message}"
        last = self._dedupe.get(key)
        if last is not None and (now - last) < float(self.dedupe_window_s):
            return None
        self._dedupe[key] = now

        alert = {
            "id": f"alert_{uuid.uuid4().hex[:12]}",
            "event": event,
            "severity": severity,
            "message": message,
            "created_at_ms": int(now * 1000),
            "targets": self._targets_for_event(event),
            "metadata": dict(metadata or {}),
        }
        self._events.appendleft(alert)
        return alert

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        cap = max(1, min(self.max_events, int(limit)))
        rows = list(self._events)
        return rows[:cap]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self._events:
            key = str(row.get("event") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return {
            "enabled": self.enabled,
            "total": len(self._events),
            "by_event": counts,
        }

    def scan_health(self) -> None:
        if not self.enabled:
            return
        self._scan_channels()
        self._scan_cron()
        self._scan_distributed()
        self._scan_queue_health()

    async def _consume_run_events(self) -> None:
        if self._run_listener is None:
            return
        while True:
            try:
                event = await self._run_listener.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.1)
                continue

            event_type = str(event.get("type") or "")
            if event_type == "queue_update" and str(event.get("reason") or "") == "overflow_replace":
                self.emit(
                    event="backlog_overflow",
                    severity="warn",
                    message="Queue backlog overflow replaced an older queued run.",
                    dedupe_key=f"backlog_overflow:{event.get('session_key')}",
                    metadata={
                        "session_key": event.get("session_key"),
                        "run_id": event.get("run_id"),
                    },
                )

    async def _poll_health(self) -> None:
        while True:
            try:
                self.scan_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Alert health poll failed: {exc}")
            await asyncio.sleep(self.health_interval_s)

    def _scan_channels(self) -> None:
        manager = self._channels_manager
        if manager is None or not hasattr(manager, "get_status"):
            return
        try:
            status = manager.get_status()
        except Exception:
            return
        if not isinstance(status, dict):
            return
        for channel, row in status.items():
            running = bool((row or {}).get("running"))
            if running:
                continue
            self.emit(
                event="channel_disconnected",
                severity="error",
                message=f"Channel '{channel}' is disconnected.",
                dedupe_key=f"channel_disconnected:{channel}",
                metadata={"channel": channel},
            )

    def _scan_cron(self) -> None:
        service = self._cron_service
        if service is None or not hasattr(service, "list_jobs"):
            return
        try:
            jobs = service.list_jobs(include_disabled=True)
        except Exception:
            return
        for job in jobs:
            state = getattr(job, "state", None)
            if state is None:
                continue
            if str(getattr(state, "last_status", "") or "") != "error":
                continue
            run_ms = int(getattr(state, "last_run_at_ms", 0) or 0)
            self.emit(
                event="cron_failure",
                severity="error",
                message=f"Cron job '{getattr(job, 'name', '')}' failed.",
                dedupe_key=f"cron_failure:{getattr(job, 'id', '')}:{run_ms}",
                metadata={
                    "job_id": getattr(job, "id", ""),
                    "job_name": getattr(job, "name", ""),
                    "last_error": getattr(state, "last_error", ""),
                    "last_run_at_ms": run_ms,
                },
            )

    def _scan_distributed(self) -> None:
        manager = self._distributed_manager
        if manager is None or not hasattr(manager, "list_nodes"):
            return
        try:
            rows = manager.list_nodes(include_stale=True)
        except Exception:
            return
        for row in rows:
            alive = bool(row.get("alive"))
            if alive:
                continue
            node_id = str(row.get("node_id") or "")
            beat = int(row.get("last_heartbeat_ms") or 0)
            self.emit(
                event="node_failure",
                severity="error",
                message=f"Distributed node '{node_id}' missed heartbeat.",
                dedupe_key=f"node_failure:{node_id}:{beat}",
                metadata={"node_id": node_id, "last_heartbeat_ms": beat},
            )

    def _scan_queue_health(self) -> None:
        loop = self._agent_loop
        if loop is None or not hasattr(loop, "get_queue_snapshot"):
            return
        try:
            snap = loop.get_queue_snapshot()
        except Exception:
            return
        sessions = snap.get("sessions") if isinstance(snap, dict) else None
        max_backlog = int(snap.get("max_backlog") or 0) if isinstance(snap, dict) else 0
        if not isinstance(sessions, list) or max_backlog <= 0:
            return
        for row in sessions:
            queued = row.get("queued") if isinstance(row, dict) else []
            if not isinstance(queued, list):
                continue
            if len(queued) < max_backlog:
                continue
            session_key = str(row.get("session_key") or "")
            self.emit(
                event="backlog_overflow",
                severity="warn",
                message=f"Session '{session_key}' reached queue backlog capacity.",
                dedupe_key=f"backlog_capacity:{session_key}",
                metadata={"session_key": session_key, "queued": len(queued), "max_backlog": max_backlog},
            )

    def _targets_for_event(self, event_name: str) -> list[str]:
        rules = getattr(self.config, "rules", [])
        configured = getattr(self.config, "channels", {}) or {}
        out: list[str] = []
        for rule in rules:
            rule_event = str(getattr(rule, "event", "") or "")
            if rule_event not in {"*", event_name}:
                continue
            for alias in getattr(rule, "channels", []) or []:
                key = str(alias or "").strip()
                if not key:
                    continue
                target = str(configured.get(key) or key)
                out.append(target)
        return sorted(set(out))
