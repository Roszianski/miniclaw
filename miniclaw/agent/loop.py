"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.subagent import SubagentManager
from miniclaw.agent.tools.apply_patch import ApplyPatchTool
from miniclaw.agent.tools.cron import CronTool
from miniclaw.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from miniclaw.agent.tools.message import MessageTool
from miniclaw.agent.tools.process import ProcessTool
from miniclaw.agent.tools.registry import ToolRegistry
from miniclaw.agent.tools.shell import ExecTool
from miniclaw.agent.tools.spawn import SpawnTool
from miniclaw.agent.tools.web import WebFetchTool, WebSearchTool
from miniclaw.audit.logger import AuditLogger
from miniclaw.bus.events import InboundMessage, OutboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.hooks.runner import HookRunner
from miniclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniclaw.ratelimit.limiter import RateLimiter
from miniclaw.session.manager import RunState, Session, SessionManager
from miniclaw.session.run_store import RunStore

if TYPE_CHECKING:
    from miniclaw.config.schema import (
        ExecToolConfig,
        HooksConfig,
        QueueConfig,
        SessionsPolicyConfig,
        ToolApprovalConfig,
    )
    from miniclaw.cron.service import CronService
    from miniclaw.heartbeat.service import HeartbeatService
    from miniclaw.processes.manager import ProcessManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        heartbeat_service: "HeartbeatService | None" = None,
        sandbox_enabled: bool = False,
        sandbox_mode: str | None = None,
        sandbox_scope: str = "agent",
        sandbox_workspace_access: str = "rw",
        sandbox_image: str = "openclaw-sandbox:bookworm-slim",
        sandbox_prune_idle_seconds: int = 1800,
        sandbox_prune_max_age_seconds: int = 21600,
        agent_id: str = "default",
        restrict_to_workspace: bool = False,
        approval_config: "ToolApprovalConfig | None" = None,
        approval_timeout_s: float = 60.0,
        audit_logger: AuditLogger | None = None,
        rate_limiter: RateLimiter | None = None,
        context_window: int = 32768,
        embedding_model: str = "",
        supports_vision: bool = True,
        timeout_seconds: int = 180,
        stream_events: bool = True,
        queue_config: "QueueConfig | None" = None,
        session_policy: "SessionsPolicyConfig | None" = None,
        process_manager: "ProcessManager | None" = None,
        reply_shaping: bool = True,
        no_reply_token: str = "NO_REPLY",
        hook_config: "HooksConfig | None" = None,
        secret_store: Any | None = None,
        usage_tracker: Any | None = None,
    ):
        from miniclaw.config.schema import ExecToolConfig, HooksConfig, QueueConfig, SessionsPolicyConfig

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.heartbeat_service = heartbeat_service
        derived_mode = (sandbox_mode or ("all" if sandbox_enabled else "off")).strip().lower().replace("-", "_")
        self.sandbox_mode = derived_mode if derived_mode in {"off", "non_main", "all"} else "off"
        self.sandbox_scope = (sandbox_scope or "agent").strip().lower()
        if self.sandbox_scope not in {"session", "agent", "shared"}:
            self.sandbox_scope = "agent"
        self.sandbox_workspace_access = (sandbox_workspace_access or "rw").strip().lower()
        if self.sandbox_workspace_access not in {"none", "ro", "rw"}:
            self.sandbox_workspace_access = "rw"
        self.sandbox_image = sandbox_image or "openclaw-sandbox:bookworm-slim"
        self.sandbox_prune_idle_seconds = max(30, int(sandbox_prune_idle_seconds))
        self.sandbox_prune_max_age_seconds = max(60, int(sandbox_prune_max_age_seconds))
        self.agent_id = (agent_id or "default").strip() or "default"
        self.restrict_to_workspace = restrict_to_workspace
        self.approval_config: ToolApprovalConfig | None = approval_config
        self.approval_timeout_s = approval_timeout_s
        self.audit_logger = audit_logger
        self.rate_limiter = rate_limiter
        self.context_window = context_window
        self.embedding_model = embedding_model

        self.timeout_seconds = max(1, int(timeout_seconds))
        self.stream_events = bool(stream_events)
        self.reply_shaping = bool(reply_shaping)
        self.no_reply_token = no_reply_token or "NO_REPLY"
        self.usage_tracker = usage_tracker

        self.queue_config = queue_config or QueueConfig()
        self.session_policy = session_policy or SessionsPolicyConfig()
        self.process_manager = process_manager
        self._global_semaphore: asyncio.Semaphore | None = None
        if self.queue_config.global_ and self.queue_config.max_concurrency > 0:
            self._global_semaphore = asyncio.Semaphore(self.queue_config.max_concurrency)

        hook_cfg = hook_config or HooksConfig()
        self.hooks = HookRunner(
            workspace=self.workspace,
            enabled=hook_cfg.enabled,
            path=hook_cfg.path,
            config_file=hook_cfg.config_file,
            timeout_seconds=hook_cfg.timeout_seconds,
            safe_mode=hook_cfg.safe_mode,
            allow_command_prefixes=hook_cfg.allow_command_prefixes,
            deny_command_patterns=hook_cfg.deny_command_patterns,
        )

        self.context = ContextBuilder(workspace, supports_vision=supports_vision, secret_store=secret_store)
        self.sessions = SessionManager(
            workspace,
            idle_reset_minutes=self.session_policy.idle_reset_minutes,
        )
        self.tools = ToolRegistry(
            bus=bus,
            approval_config=approval_config,
            approval_timeout_s=approval_timeout_s,
            audit_logger=audit_logger,
        )
        self.tools.set_tool_event_callback(self._on_tool_event)

        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            agent_id=self.agent_id,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            sandbox_mode=self.sandbox_mode,
            sandbox_scope=self.sandbox_scope,
            sandbox_workspace_access=self.sandbox_workspace_access,
            sandbox_image=self.sandbox_image,
            sandbox_prune_idle_seconds=self.sandbox_prune_idle_seconds,
            sandbox_prune_max_age_seconds=self.sandbox_prune_max_age_seconds,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_run_tasks: dict[str, asyncio.Task[OutboundMessage | None]] = {}
        self._active_runs: dict[str, RunState] = {}
        self._run_messages: dict[str, InboundMessage] = {}
        self._steer_buffers: dict[str, deque[dict[str, Any]]] = {}
        self._recent_runs: deque[RunState] = deque(maxlen=200)
        self._cancel_requested: set[str] = set()
        self._closed_run_set: set[str] = set()
        self._closed_run_order: deque[str] = deque(maxlen=2000)
        self._run_store = RunStore(max_records=5000)
        self._load_persisted_runs()

        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ApplyPatchTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        # Shell tool
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                sandbox_mode=self.sandbox_mode,
                sandbox_scope=self.sandbox_scope,
                sandbox_workspace_access=self.sandbox_workspace_access,
                sandbox_image=self.sandbox_image,
                sandbox_prune_idle_seconds=self.sandbox_prune_idle_seconds,
                sandbox_prune_max_age_seconds=self.sandbox_prune_max_age_seconds,
                sandbox_agent_id=self.agent_id,
                resource_limits=self.exec_config.resource_limits,
                restrict_to_workspace=self.restrict_to_workspace,
            )
        )

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())

        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        if self.process_manager:
            self.tools.register(ProcessTool(self.process_manager))

        # Browser tool (optional, requires playwright)
        try:
            from miniclaw.agent.tools.browser import BrowserTool

            self.tools.register(BrowserTool(workspace=self.workspace))
        except ImportError:
            logger.debug("Playwright not installed, browser tool unavailable")

        # Memory search tool
        from miniclaw.agent.tools.memory_search import MemorySearchTool

        self.tools.register(
            MemorySearchTool(
                workspace=self.workspace,
                provider=self.provider,
                embedding_model=self.embedding_model,
            )
        )

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        self.context.skills.start_hot_reload()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self.submit_inbound(msg, publish_outbound=True)

    def submit_inbound(self, msg: InboundMessage, publish_outbound: bool = True) -> str:
        """
        Submit an inbound message for async processing.

        Returns:
            The created run id.
        """
        session_key = self._session_key_for_msg(msg)
        mode = self._queue_mode()
        running = self._find_running_run(session_key)
        queued = self._list_queued_runs(session_key)

        # Session control commands should bypass steer/backlog transforms.
        if not self._is_session_control_command(msg.content):
            if mode in {"steer", "steer_backlog"} and running is not None:
                steered = self.steer_run(
                    running.run_id,
                    msg.content,
                    source="inbound",
                    sender_id=msg.sender_id,
                )
                if steered and mode == "steer":
                    return running.run_id
                if steered and mode == "steer_backlog":
                    replaced = self._replace_followup_queued_run(queued, msg)
                    if replaced:
                        return replaced

            if mode == "collect":
                merged = self._merge_collect_queued_run(queued, msg)
                if merged:
                    return merged

            if mode == "followup":
                replaced = self._replace_followup_queued_run(queued, msg)
                if replaced:
                    return replaced

            if len(queued) >= int(self.queue_config.max_backlog):
                target = queued[0]
                replaced = self._replace_queued_message(
                    run_id=target.run_id,
                    msg=msg,
                    mode="replace",
                    reason="overflow_replace",
                )
                if replaced:
                    return target.run_id

        run_id = self._new_run_id()
        self._register_run_state(run_id, msg)
        self._start_run_task(run_id=run_id, msg=msg, publish_outbound=publish_outbound)
        return run_id

    def _start_run_task(self, *, run_id: str, msg: InboundMessage, publish_outbound: bool) -> None:
        self._run_messages[run_id] = msg
        task: asyncio.Task[OutboundMessage | None] = asyncio.create_task(
            self._run_with_lifecycle(msg, run_id=run_id, publish_outbound=publish_outbound)
        )
        self._active_run_tasks[run_id] = task
        task.add_done_callback(lambda t, rid=run_id: self._on_run_task_done(rid, t))

    def _queue_mode(self) -> str:
        mode = (self.queue_config.mode or "queue").strip().lower().replace("-", "_")
        if mode not in {"queue", "collect", "steer", "followup", "steer_backlog"}:
            return "queue"
        return mode

    def _find_running_run(self, session_key: str) -> RunState | None:
        running = [
            run for run in self._active_runs.values() if run.session_key == session_key and run.status == "running"
        ]
        if not running:
            return None
        running.sort(key=lambda r: r.created_at)
        return running[0]

    def _list_queued_runs(self, session_key: str) -> list[RunState]:
        queued = [
            run for run in self._active_runs.values() if run.session_key == session_key and run.status == "queued"
        ]
        queued.sort(key=lambda r: r.created_at)
        return queued

    def _merge_collect_queued_run(self, queued: list[RunState], msg: InboundMessage) -> str | None:
        if not queued:
            return None
        latest = queued[-1]
        elapsed_ms = (datetime.now() - latest.created_at).total_seconds() * 1000.0
        if elapsed_ms > float(self.queue_config.collect_window_ms):
            return None
        changed = self._replace_queued_message(
            run_id=latest.run_id,
            msg=msg,
            mode="collect",
            reason="collect_merge",
        )
        return latest.run_id if changed else None

    def _replace_followup_queued_run(self, queued: list[RunState], msg: InboundMessage) -> str | None:
        if not queued:
            return None
        latest = queued[-1]
        changed = self._replace_queued_message(
            run_id=latest.run_id,
            msg=msg,
            mode="replace",
            reason="followup_replace",
        )
        return latest.run_id if changed else None

    def _replace_queued_message(
        self,
        *,
        run_id: str,
        msg: InboundMessage,
        mode: Literal["replace", "collect"],
        reason: str,
    ) -> bool:
        queued_run = self._active_runs.get(run_id)
        queued_msg = self._run_messages.get(run_id)
        if queued_run is None or queued_msg is None:
            return False
        if queued_run.status != "queued":
            return False

        existing = (queued_msg.content or "").strip()
        incoming = (msg.content or "").strip()
        if mode == "collect":
            if existing and incoming:
                queued_msg.content = f"{existing}\n\n[Collected Followup]\n{incoming}"
            elif incoming:
                queued_msg.content = incoming
        else:
            queued_msg.content = msg.content

        if msg.media:
            queued_msg.media = list(dict.fromkeys(list(queued_msg.media or []) + list(msg.media or [])))

        merged_meta = dict(queued_msg.metadata or {})
        for key, value in dict(msg.metadata or {}).items():
            if key == "session_key":
                continue
            merged_meta[key] = value
        queued_msg.metadata = merged_meta
        queued_msg.timestamp = msg.timestamp

        self._publish_queue_event(
            {
                "type": "queue_update",
                "kind": "queue",
                "run_id": run_id,
                "session_key": queued_run.session_key,
                "mode": self._queue_mode(),
                "reason": reason,
                "ts": time.time(),
            }
        )
        return True

    def _publish_queue_event(self, payload: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_run_event(payload))
        except RuntimeError:
            pass

    def stop(self) -> None:
        """Stop the agent loop and cancel active runs."""
        self._running = False
        logger.info("Agent loop stopping")
        self.context.skills.stop_hot_reload()

        for task in list(self._active_run_tasks.values()):
            if not task.done():
                task.cancel()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.hooks.run("Stop", {"reason": "agent_stop"}))
        except RuntimeError:
            pass

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List active and recent runs for dashboard/API usage."""
        limit = max(1, min(500, int(limit)))
        seen: set[str] = set()
        runs: list[RunState] = []

        active_sorted = sorted(
            self._active_runs.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )
        for run in active_sorted:
            runs.append(run)
            seen.add(run.run_id)

        for run in self._recent_runs:
            if run.run_id in seen:
                continue
            runs.append(run)
            seen.add(run.run_id)
            if len(runs) >= limit:
                break

        return [r.to_dict() for r in runs[:limit]]

    def get_queue_snapshot(self) -> dict[str, Any]:
        """Return queue/backlog state grouped by session."""
        sessions: dict[str, dict[str, Any]] = {}
        for run in self._active_runs.values():
            if run.status not in {"queued", "running"}:
                continue
            entry = sessions.setdefault(
                run.session_key,
                {
                    "session_key": run.session_key,
                    "running": None,
                    "queued": [],
                },
            )
            run_data = run.to_dict()
            if run.status == "running":
                entry["running"] = run_data
            else:
                entry["queued"].append(run_data)

        ordered = sorted(sessions.values(), key=lambda item: item["session_key"])
        for item in ordered:
            item["queued"] = sorted(item["queued"], key=lambda r: r.get("created_at") or "")

        return {
            "mode": self._queue_mode(),
            "collect_window_ms": int(self.queue_config.collect_window_ms),
            "max_backlog": int(self.queue_config.max_backlog),
            "sessions": ordered,
        }

    def steer_run(
        self,
        run_id: str,
        instruction: str,
        *,
        source: str = "api",
        sender_id: str | None = None,
    ) -> bool:
        """Queue a steer instruction for an in-flight run."""
        text = (instruction or "").strip()
        if not text:
            return False
        run = self._active_runs.get(run_id)
        if run is None or run.status != "running":
            return False

        queue = self._steer_buffers.setdefault(run_id, deque(maxlen=32))
        queue.append(
            {
                "text": text,
                "source": source,
                "sender_id": sender_id or "",
                "at": time.time(),
            }
        )
        event = {
            "type": "run_steer",
            "kind": "queue",
            "run_id": run_id,
            "session_key": run.session_key,
            "source": source,
            "sender_id": sender_id or "",
            "instruction_preview": self._truncate_text(text, max_len=180),
            "pending": len(queue),
            "ts": time.time(),
        }
        self._publish_queue_event(event)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.bus.publish_steer_event(event))
        except RuntimeError:
            pass
        return True

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a currently queued/running run by ID."""
        self._cancel_requested.add(run_id)
        task = self._active_run_tasks.get(run_id)
        run = self._active_runs.get(run_id)
        if run and run.status == "queued":
            run.error = "Run cancelled"
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        model_override: str | None = None,
    ) -> str:
        """
        Process a message directly (for CLI/dashboard/cron usage).

        Args:
            content: The message content.
            session_key: Session identifier override.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).

        Returns:
            The agent's response.
        """
        self.context.skills.start_hot_reload()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata={
                "session_key": session_key,
                "model_override": model_override or "",
            },
        )
        run_id = self.submit_inbound(msg, publish_outbound=False)
        task = self._active_run_tasks.get(run_id)
        if task is None:
            return ""
        response = await task
        return response.content if response else ""

    async def _run_with_lifecycle(
        self,
        msg: InboundMessage,
        run_id: str,
        publish_outbound: bool,
    ) -> OutboundMessage | None:
        run = self._active_runs.get(run_id) or self._register_run_state(run_id, msg)
        session_key = run.session_key
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks[run_id] = current_task

        session_started = False
        response: OutboundMessage | None = None
        typing_started = False

        async def _execute_started() -> OutboundMessage | None:
            nonlocal session_started, typing_started
            self._check_cancelled(run_id)
            run.status = "running"
            run.started_at = datetime.now()
            await self._emit_lifecycle_event("run_start", run_id=run_id, session_key=session_key, msg=msg)
            if publish_outbound and self._supports_typing_control(msg.channel):
                await self._emit_typing_control(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    action="typing_start",
                    run_id=run_id,
                    session_key=session_key,
                )
                typing_started = True

            session_started = True
            await self._run_hook(
                "SessionStart",
                {
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "sender_id": msg.sender_id,
                },
            )

            try:
                out = await asyncio.wait_for(
                    self._process_message(msg, run_id=run_id),
                    timeout=self.timeout_seconds,
                )
                run.status = "completed"
                await self._emit_lifecycle_event(
                    "run_end",
                    run_id=run_id,
                    session_key=session_key,
                    msg=msg,
                    extra={"has_response": bool(out and (out.content or "").strip())},
                )
                return out
            except asyncio.TimeoutError:
                run.status = "error"
                run.error = f"Run timed out after {self.timeout_seconds} seconds"
                await self._emit_lifecycle_event(
                    "run_error",
                    run_id=run_id,
                    session_key=session_key,
                    msg=msg,
                    extra={"error": run.error},
                )
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Sorry, this run timed out after {self.timeout_seconds} seconds.",
                )
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = "Run cancelled"
                await self._emit_lifecycle_event(
                    "run_cancelled",
                    run_id=run_id,
                    session_key=session_key,
                    msg=msg,
                )
                return None
            except Exception as exc:
                run.status = "error"
                run.error = str(exc)
                logger.error(f"Error processing run {run_id}: {exc}")
                await self._emit_lifecycle_event(
                    "run_error",
                    run_id=run_id,
                    session_key=session_key,
                    msg=msg,
                    extra={"error": run.error},
                )
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Sorry, I encountered an error: {str(exc)}",
                )

        try:
            if self._is_cancel_command(msg.content):
                response = await _execute_started()
            elif self._global_semaphore:
                async with self._global_semaphore:
                    async with lock:
                        response = await _execute_started()
            else:
                async with lock:
                    response = await _execute_started()
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error = "Run cancelled"
            await self._emit_lifecycle_event(
                "run_cancelled",
                run_id=run_id,
                session_key=session_key,
                msg=msg,
            )
            response = None
        finally:
            if typing_started and publish_outbound and self._supports_typing_control(msg.channel):
                await self._emit_typing_control(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    action="typing_stop",
                    run_id=run_id,
                    session_key=session_key,
                )
            run.ended_at = datetime.now()
            if session_started:
                await self._run_hook(
                    "SessionEnd",
                    {
                        "run_id": run_id,
                        "session_key": session_key,
                        "status": run.status,
                        "error": run.error,
                    },
                )

            self._store_run_on_session(run)
            self._archive_run(run)
            self._active_run_tasks.pop(run_id, None)
            self._run_messages.pop(run_id, None)
            self._steer_buffers.pop(run_id, None)
            self._clear_run_message_state(run_id)
            self._cancel_requested.discard(run_id)
            self._mark_run_closed(run_id)

            if self.usage_tracker and int(run.usage_total_tokens or 0) > 0:
                try:
                    self.usage_tracker.record(
                        source="agent",
                        model=run.model or self.model,
                        prompt_tokens=int(run.usage_prompt_tokens or 0),
                        completion_tokens=int(run.usage_completion_tokens or 0),
                        total_tokens=int(run.usage_total_tokens or 0),
                        run_id=run.run_id,
                        session_key=run.session_key,
                        user_id=run.session_key,
                        metadata={"channel": run.channel, "status": run.status},
                    )
                except Exception as exc:
                    logger.debug(f"Usage tracking failed for run {run.run_id}: {exc}")

        if publish_outbound and response:
            await self.bus.publish_outbound(response)
        return response

    async def _process_message(self, msg: InboundMessage, run_id: str) -> OutboundMessage | None:
        """Process a single inbound message."""
        if msg.channel == "system":
            return await self._process_system_message(msg, run_id=run_id)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message ({run_id}) from {msg.channel}:{msg.sender_id}: {preview}")

        if self.audit_logger:
            self.audit_logger.log_message(
                direction="inbound",
                channel=msg.channel,
                length=len(msg.content or ""),
                sender=msg.sender_id,
            )

        if self.rate_limiter and not self.rate_limiter.check_message(msg.sender_id):
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="You're sending messages too quickly. Please wait a bit and try again.",
                reply_to=self._reply_to_for_msg(msg),
            )

        session_key = self._session_key_for_msg(msg)
        session = self.sessions.get_or_create(session_key)
        if self.sessions.apply_idle_reset(session):
            await self._emit_run_event(
                {
                    "type": "session_idle_reset",
                    "kind": "session",
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "ts": time.time(),
                }
            )
        command_response = self._handle_session_command(msg=msg, run_id=run_id, session=session)
        if command_response is not None:
            return command_response

        content = msg.content
        model_override = str((msg.metadata or {}).get("model_override") or "").strip() or None
        thinking_override = self._session_thinking_mode(session)
        m = re.match(r"^/think:(off|low|medium|high)\b", content.strip(), re.IGNORECASE)
        if m:
            thinking_override = m.group(1).lower()
            content = content[m.end() :].lstrip()

        final_content, usage = await self._run_dialog(
            session=session,
            content=content,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            media=msg.media if msg.media else None,
            run_id=run_id,
            thinking_override=thinking_override,
            model_override=model_override,
        )
        total_tokens = int(usage.get("total_tokens") or 0)
        active_model = model_override or self.model

        session.add_message("user", content)
        if final_content is not None and final_content.strip():
            session.add_message("assistant", final_content)
        self.sessions.save(session)

        run_state = self._active_runs.get(run_id)
        if run_state is not None:
            run_state.model = active_model
            run_state.usage_prompt_tokens = int(usage.get("prompt_tokens") or 0)
            run_state.usage_completion_tokens = int(usage.get("completion_tokens") or 0)
            run_state.usage_total_tokens = int(usage.get("total_tokens") or 0)

        if self.audit_logger and final_content is not None and final_content.strip():
            self.audit_logger.log_message(
                direction="outbound",
                channel=msg.channel,
                length=len(final_content or ""),
                sender=msg.chat_id,
            )

        if total_tokens and total_tokens > int(self.context_window * 0.85):
            await self._compact_session(session, run_id=run_id, reason="token_threshold")

        if final_content is None or not final_content.strip():
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response ({run_id}) to {msg.channel}:{msg.sender_id}: {preview}")
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            reply_to=self._reply_to_for_msg(msg),
        )

    async def _process_system_message(self, msg: InboundMessage, run_id: str) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message ({run_id}) from {msg.sender_id}")

        if ":" in msg.chat_id:
            origin_channel, origin_chat_id = msg.chat_id.split(":", 1)
        else:
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        override = (msg.metadata or {}).get("session_key")
        if isinstance(override, str) and override.strip():
            session_key = override.strip()
        else:
            session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        if self.sessions.apply_idle_reset(session):
            await self._emit_run_event(
                {
                    "type": "session_idle_reset",
                    "kind": "session",
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": origin_channel,
                    "chat_id": origin_chat_id,
                    "ts": time.time(),
                }
            )

        model_override = str((msg.metadata or {}).get("model_override") or "").strip() or None
        final_content, usage = await self._run_dialog(
            session=session,
            content=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
            sender_id=msg.sender_id,
            media=None,
            run_id=run_id,
            max_iterations=min(self.max_iterations, 12),
            model_override=model_override,
        )

        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        if final_content is not None and final_content.strip():
            session.add_message("assistant", final_content)
        self.sessions.save(session)

        run_state = self._active_runs.get(run_id)
        if run_state is not None:
            run_state.model = model_override or self.model
            run_state.usage_prompt_tokens = int(usage.get("prompt_tokens") or 0)
            run_state.usage_completion_tokens = int(usage.get("completion_tokens") or 0)
            run_state.usage_total_tokens = int(usage.get("total_tokens") or 0)

        if final_content is None or not final_content.strip():
            return None
        return OutboundMessage(channel=origin_channel, chat_id=origin_chat_id, content=final_content)

    async def _run_dialog(
        self,
        *,
        session: Session,
        content: str,
        channel: str,
        chat_id: str,
        sender_id: str,
        media: list[str] | None,
        run_id: str,
        thinking_override: str | None = None,
        max_iterations: int | None = None,
        model_override: str | None = None,
    ) -> tuple[str | None, dict[str, int]]:
        """Run an LLM + tool-call turn sequence and shape the final reply."""
        active_model = model_override or self.model
        if len(session.messages) > 40:
            await self._compact_session(session, run_id=run_id, reason="history_limit")

        self._set_tool_context(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            run_id=run_id,
            session_key=session.key,
        )

        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=content,
            media=media,
            channel=channel,
            chat_id=chat_id,
        )

        final_content: str | None = None
        suppressed = False
        asked_visible_reply = False
        usage_totals: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        max_iters = max_iterations or self.max_iterations
        iteration = 0
        while iteration < max_iters:
            self._check_cancelled(run_id)
            iteration += 1
            messages = await self._inject_steer_updates(
                run_id=run_id,
                session_key=session.key,
                channel=channel,
                chat_id=chat_id,
                messages=messages,
            )

            response, streamed = await self._chat_with_optional_stream(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=active_model,
                thinking=thinking_override,
                run_id=run_id,
                session_key=session.key,
                channel=channel,
                chat_id=chat_id,
            )
            usage_totals = self._merge_usage(usage_totals, response.usage if hasattr(response, "usage") else {})

            if response.finish_reason == "overloaded":
                logger.warning(f"Run {run_id}: model overloaded, compacting and retrying")
                did_compact = await self._compact_session(
                    session,
                    run_id=run_id,
                    reason="overloaded_retry",
                )
                if did_compact:
                    messages = self.context.build_messages(
                        history=session.get_history(),
                        current_message=content,
                        media=media,
                        channel=channel,
                        chat_id=chat_id,
                    )
                    asked_visible_reply = False
                    continue
                raise RuntimeError("Model overloaded and compaction failed")

            if response.content and response.content.strip() and not streamed:
                await self._emit_assistant_deltas(
                    run_id=run_id,
                    session_key=session.key,
                    channel=channel,
                    chat_id=chat_id,
                    text=response.content,
                )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(messages, response.content, tool_call_dicts)

                for tool_call in response.tool_calls:
                    result = await self._execute_tool_call(
                        tool_call=tool_call,
                        run_id=run_id,
                        session_key=session.key,
                        channel=channel,
                        chat_id=chat_id,
                        sender_id=sender_id,
                    )
                    messages = self.context.add_tool_result(messages, tool_call.id, tool_call.name, result)
                continue

            shaped = self._shape_reply(response.content or "", run_id=run_id)
            if shaped is None:
                suppressed = True
                break
            if shaped.strip():
                final_content = shaped
                break

            if asked_visible_reply:
                final_content = "Completed; no user-visible output."
                break

            asked_visible_reply = True
            messages = self.context.add_assistant_message(messages, response.content or "")
            messages.append({"role": "user", "content": "[system: please provide a user-visible reply.]"})
            logger.warning(f"Run {run_id}: empty response from LLM, nudging for visible reply")

        if final_content is None and not suppressed:
            self._check_cancelled(run_id)
            logger.warning(f"Run {run_id}: final reply missing, forcing summary reply")
            messages = await self._inject_steer_updates(
                run_id=run_id,
                session_key=session.key,
                channel=channel,
                chat_id=chat_id,
                messages=messages,
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[system: please provide a user-visible reply. "
                        "If nothing else should be shown, state what was completed.]"
                    ),
                }
            )
            summary_response, streamed = await self._chat_with_optional_stream(
                messages=messages,
                tools=None,
                model=active_model,
                thinking=thinking_override,
                run_id=run_id,
                session_key=session.key,
                channel=channel,
                chat_id=chat_id,
            )
            usage_totals = self._merge_usage(
                usage_totals,
                summary_response.usage if hasattr(summary_response, "usage") else {},
            )
            if summary_response.content and summary_response.content.strip() and not streamed:
                await self._emit_assistant_deltas(
                    run_id=run_id,
                    session_key=session.key,
                    channel=channel,
                    chat_id=chat_id,
                    text=summary_response.content,
                )

            shaped = self._shape_reply(summary_response.content or "", run_id=run_id)
            if shaped is None:
                suppressed = True
            elif shaped.strip():
                final_content = shaped
            else:
                final_content = "Completed; no user-visible output."

        if suppressed:
            return None, usage_totals
        return final_content, usage_totals

    async def _inject_steer_updates(
        self,
        *,
        run_id: str,
        session_key: str,
        channel: str,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updates = self._drain_steer_updates(run_id)
        if not updates:
            return messages

        lines = []
        for idx, update in enumerate(updates, start=1):
            text = str(update.get("text") or "").strip()
            if not text:
                continue
            src = str(update.get("source") or "unknown")
            lines.append(f"{idx}. ({src}) {text}")
        if not lines:
            return messages

        steer_content = (
            "[system: steer update received during run. "
            "Incorporate these adjustments for the next steps and final response.]\n"
            + "\n".join(lines)
        )
        messages.append({"role": "user", "content": steer_content})
        await self._emit_run_event(
            {
                "type": "run_steer_applied",
                "kind": "queue",
                "run_id": run_id,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "count": len(lines),
                "ts": time.time(),
            }
        )
        return messages

    async def _execute_tool_call(
        self,
        *,
        tool_call: ToolCallRequest,
        run_id: str,
        session_key: str,
        channel: str,
        chat_id: str,
        sender_id: str,
    ) -> str:
        self._check_cancelled(run_id)
        args_preview = json.dumps(tool_call.arguments, ensure_ascii=False)
        logger.info(f"Run {run_id} tool call: {tool_call.name}({args_preview[:200]})")

        pre_payload = {
            "run_id": run_id,
            "session_key": session_key,
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "tool_name": tool_call.name,
            "args": tool_call.arguments,
        }
        pre_result = await self._run_hook("PreToolUse", pre_payload)
        if pre_result.blocked:
            blocked_msg = f"Error: Tool '{tool_call.name}' blocked by PreToolUse hook"
            await self._emit_run_event(
                {
                    "type": "tool_end",
                    "kind": "tool",
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": channel,
                    "chat_id": chat_id,
                    "tool_name": tool_call.name,
                    "ok": False,
                    "result": blocked_msg,
                    "blocked_by_hook": True,
                    "ts": time.time(),
                }
            )
            return blocked_msg

        self._check_cancelled(run_id)
        if self.rate_limiter and not self.rate_limiter.check_tool_call(sender_id):
            rate_msg = "Error: Rate limit exceeded for tool calls. Please try again later."
            await self._emit_run_event(
                {
                    "type": "tool_end",
                    "kind": "tool",
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": channel,
                    "chat_id": chat_id,
                    "tool_name": tool_call.name,
                    "ok": False,
                    "result": rate_msg,
                    "rate_limited": True,
                    "ts": time.time(),
                }
            )
            return rate_msg

        self._check_cancelled(run_id)
        result = await self.tools.execute(tool_call.name, tool_call.arguments)

        await self._run_hook(
            "PostToolUse",
            {
                "run_id": run_id,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "tool_name": tool_call.name,
                "args": tool_call.arguments,
                "result_preview": self._truncate_text(result, max_len=1500),
            },
        )
        return result

    async def _compact_session(
        self,
        session: Session,
        run_id: str,
        reason: str = "manual",
    ) -> bool:
        """Compact session history into a summary and emit compaction events."""
        from miniclaw.session.compaction import compact_session

        await self._emit_run_event(
            {
                "type": "compaction_start",
                "kind": "compaction",
                "run_id": run_id,
                "session_key": session.key,
                "reason": reason,
                "message_count": len(session.messages),
                "ts": time.time(),
            }
        )

        await self._run_hook(
            "PreCompact",
            {
                "run_id": run_id,
                "session_key": session.key,
                "reason": reason,
                "message_count": len(session.messages),
            },
        )

        history = [{"role": m["role"], "content": m["content"]} for m in session.messages]
        try:
            summary, _ = await compact_session(
                history=history,
                provider=self.provider,
                model=self.model,
                keep_recent=10,
            )
        except Exception as exc:
            await self._emit_run_event(
                {
                    "type": "compaction_error",
                    "kind": "compaction",
                    "run_id": run_id,
                    "session_key": session.key,
                    "reason": reason,
                    "error": str(exc),
                    "ts": time.time(),
                }
            )
            logger.error(f"Run {run_id}: compaction failed: {exc}")
            return False

        if summary:
            session.summary = summary
            if len(session.messages) > 10:
                session.messages = session.messages[-10:]
            self.sessions.save(session)
            await self._emit_run_event(
                {
                    "type": "compaction_end",
                    "kind": "compaction",
                    "run_id": run_id,
                    "session_key": session.key,
                    "reason": reason,
                    "ok": True,
                    "summary_length": len(summary),
                    "remaining_messages": len(session.messages),
                    "ts": time.time(),
                }
            )
            return True

        await self._emit_run_event(
            {
                "type": "compaction_end",
                "kind": "compaction",
                "run_id": run_id,
                "session_key": session.key,
                "reason": reason,
                "ok": False,
                "summary_length": 0,
                "remaining_messages": len(session.messages),
                "ts": time.time(),
            }
        )
        return False

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        sender_id: str,
        run_id: str,
        session_key: str,
    ) -> None:
        """Set context on context-aware tools and registry."""
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(channel, chat_id)
            message_tool.set_run_context(run_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(channel, chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(channel, chat_id)

        self.tools.set_context(
            channel,
            chat_id,
            user_key=sender_id,
            run_id=run_id,
            session_key=session_key,
        )

    def _handle_session_command(
        self,
        *,
        msg: InboundMessage,
        run_id: str,
        session: Session,
    ) -> OutboundMessage | None:
        command, arg = self._split_slash_command(msg.content)
        if not command:
            return None

        if command == "/cancel":
            session_key = self._session_key_for_msg(msg)
            active = [
                run
                for run in self._active_runs.values()
                if run.session_key == session_key
                and run.run_id != run_id
                and run.status in {"queued", "running"}
            ]
            if not active:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="No active run to cancel for this session.",
                    reply_to=self._reply_to_for_msg(msg),
                )

            active.sort(
                key=lambda r: (0 if r.status == "running" else 1, r.created_at.timestamp())
            )
            target = active[0]
            cancelled = self.cancel_run(target.run_id)
            if cancelled:
                text = f"Cancelled run `{target.run_id}`."
            else:
                text = f"Run `{target.run_id}` is no longer cancellable."
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text,
                reply_to=self._reply_to_for_msg(msg),
            )

        if command == "/status":
            active_total = [
                run for run in self._active_runs.values() if run.status in {"queued", "running"}
            ]
            session_key = self._session_key_for_msg(msg)
            active_session = [
                run
                for run in active_total
                if run.session_key == session_key and run.run_id != run_id
            ]

            cron_status = self.cron_service.status() if self.cron_service else None
            heartbeat_status = self.heartbeat_service.status() if self.heartbeat_service else None
            thinking_mode = self._session_thinking_mode(session) or "default"

            cron_line = "Cron: unavailable"
            if cron_status is not None:
                cron_state = "running" if cron_status.get("enabled") else "stopped"
                cron_jobs = int(cron_status.get("jobs") or 0)
                cron_line = f"Cron: {cron_state}, jobs={cron_jobs}"

            hb_line = "Heartbeat: unavailable"
            if heartbeat_status is not None:
                hb_running = bool(heartbeat_status.get("running"))
                hb_interval = heartbeat_status.get("interval_s")
                hb_state = "running" if hb_running else "stopped"
                hb_line = f"Heartbeat: {hb_state}, interval={hb_interval}s"

            lines = [
                f"Model: {self.model}",
                f"Thinking: {thinking_mode}",
                f"Active runs (session): {len(active_session)}",
                f"Active runs (total): {len(active_total)}",
                cron_line,
                hb_line,
            ]
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
                reply_to=self._reply_to_for_msg(msg),
            )

        if command == "/reset":
            session.clear()
            session.summary = ""
            session.metadata = {}
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Session reset.",
                reply_to=self._reply_to_for_msg(msg),
            )

        if command == "/think":
            if not arg:
                mode = self._session_thinking_mode(session) or "default"
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Current thinking mode: {mode}.",
                    reply_to=self._reply_to_for_msg(msg),
                )

            mode = arg.split()[0].strip().lower()
            if mode not in {"off", "low", "medium", "high"}:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Usage: /think off|low|medium|high",
                    reply_to=self._reply_to_for_msg(msg),
                )
            session.metadata["thinking_mode"] = mode
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Thinking mode set to {mode}.",
                reply_to=self._reply_to_for_msg(msg),
            )

        # Support /think:mode as a setter when used standalone.
        if command.startswith("/think:") and not arg:
            mode = command.split(":", 1)[1].strip().lower()
            if mode in {"off", "low", "medium", "high"}:
                session.metadata["thinking_mode"] = mode
                self.sessions.save(session)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Thinking mode set to {mode}.",
                    reply_to=self._reply_to_for_msg(msg),
                )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Usage: /think off|low|medium|high",
                reply_to=self._reply_to_for_msg(msg),
            )

        return None

    def _shape_reply(self, content: str, run_id: str) -> str | None:
        """
        Shape/suppress final replies.

        Returns:
            - str: user-visible reply
            - None: suppress outbound reply
        """
        text = content or ""
        if not self.reply_shaping:
            return text.strip()

        token = self.no_reply_token.strip()
        had_no_reply_token = bool(token and token in text)
        if token:
            text = text.replace(token, "")

        shaped = text.strip()
        if had_no_reply_token and not shaped:
            return None

        if self._is_duplicate_message_confirmation(shaped, run_id=run_id):
            return None

        return shaped

    def _is_duplicate_message_confirmation(self, reply: str, run_id: str) -> bool:
        """Suppress trivial 'message sent' confirmations when message tool already sent output."""
        if not reply:
            return False
        message_tool = self.tools.get("message")
        if not isinstance(message_tool, MessageTool):
            return False
        sends = message_tool.get_run_sends(run_id)
        if not sends:
            return False

        norm = re.sub(r"\s+", " ", reply.strip().lower())
        if len(norm) > 180:
            return False

        patterns = (
            r"^message sent(?: successfully)?(?: to [^\n]+)?\.?$",
            r"^sent (?:the )?message(?: to [^\n]+)?\.?$",
            r"^done\.?$",
            r"^completed\.?$",
            r"^all set\.?$",
        )
        return any(re.match(pattern, norm) for pattern in patterns)

    def _drain_steer_updates(self, run_id: str) -> list[dict[str, Any]]:
        queue = self._steer_buffers.get(run_id)
        if not queue:
            return []
        out: list[dict[str, Any]] = []
        while queue:
            out.append(queue.popleft())
        return out

    def _new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _register_run_state(self, run_id: str, msg: InboundMessage) -> RunState:
        run = RunState(
            run_id=run_id,
            session_key=self._session_key_for_msg(msg),
            channel=msg.channel,
            chat_id=msg.chat_id,
            model=self.model,
            status="queued",
        )
        self._active_runs[run_id] = run
        return run

    def _archive_run(self, run: RunState) -> None:
        self._active_runs.pop(run.run_id, None)
        self._recent_runs.appendleft(run)
        self._run_store.append(run)

    def _store_run_on_session(self, run: RunState) -> None:
        try:
            session = self.sessions.get_or_create(run.session_key)
            session.set_last_run(run)
            self.sessions.save(session)
        except Exception as exc:
            logger.debug(f"Failed to persist run metadata for {run.run_id}: {exc}")

    def _session_key_for_msg(self, msg: InboundMessage) -> str:
        override = (msg.metadata or {}).get("session_key")
        if isinstance(override, str) and override.strip():
            return override.strip()
        return msg.session_key

    @staticmethod
    def _split_slash_command(content: str | None) -> tuple[str, str]:
        text = (content or "").strip()
        if not text.startswith("/"):
            return "", ""
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return command, arg

    @classmethod
    def _is_cancel_command(cls, content: str | None) -> bool:
        command, _ = cls._split_slash_command(content)
        return command == "/cancel"

    @classmethod
    def _is_session_control_command(cls, content: str | None) -> bool:
        command, _ = cls._split_slash_command(content)
        if not command:
            return False
        if command in {"/cancel", "/status", "/reset", "/think"}:
            return True
        return command.startswith("/think:")

    @staticmethod
    def _reply_to_for_msg(msg: InboundMessage) -> str | None:
        value = (msg.metadata or {}).get("message_id")
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _session_thinking_mode(session: Session) -> str | None:
        mode = str(session.metadata.get("thinking_mode") or "").strip().lower()
        return mode if mode in {"off", "low", "medium", "high"} else None

    @staticmethod
    def _supports_typing_control(channel: str) -> bool:
        return channel in {"telegram", "whatsapp"}

    async def _emit_typing_control(
        self,
        *,
        channel: str,
        chat_id: str,
        action: str,
        run_id: str,
        session_key: str,
    ) -> None:
        try:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content="",
                    control=action,
                    metadata={"run_id": run_id, "session_key": session_key},
                )
            )
        except Exception:
            pass

    def _clear_run_message_state(self, run_id: str) -> None:
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.clear_run_sends(run_id)

    def _on_run_task_done(self, run_id: str, task: asyncio.Task[OutboundMessage | None]) -> None:
        if self._active_run_tasks.get(run_id) is task:
            self._active_run_tasks.pop(run_id, None)
        if task.cancelled():
            return
        try:
            _ = task.result()
        except Exception as exc:
            logger.error(f"Run task {run_id} failed unexpectedly: {exc}")

    async def _emit_lifecycle_event(
        self,
        event: str,
        *,
        run_id: str,
        session_key: str,
        msg: InboundMessage,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": event,
            "kind": "lifecycle",
            "run_id": run_id,
            "session_key": session_key,
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "sender_id": msg.sender_id,
            "ts": time.time(),
        }
        if extra:
            payload.update(extra)

        if self.audit_logger:
            self.audit_logger.log_event(event, {
                "run_id": run_id,
                "session_key": session_key,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "error": payload.get("error"),
            })

        await self._emit_run_event(payload)

    async def _emit_assistant_deltas(
        self,
        *,
        run_id: str,
        session_key: str,
        channel: str,
        chat_id: str,
        text: str,
        chunk_size: int = 220,
    ) -> None:
        clean = text or ""
        if not clean.strip():
            return
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            await self._emit_run_event(
                {
                    "type": "assistant_delta",
                    "kind": "assistant",
                    "run_id": run_id,
                    "session_key": session_key,
                    "channel": channel,
                    "chat_id": chat_id,
                    "delta": chunk,
                    "index": i // chunk_size,
                    "ts": time.time(),
                }
            )

    async def _emit_run_event(self, payload: dict[str, Any]) -> None:
        if not self.stream_events:
            return
        run_id = str(payload.get("run_id") or "")
        event_type = str(payload.get("type") or "")
        if run_id and run_id in self._closed_run_set and event_type not in {"run_end", "run_error", "run_cancelled"}:
            return
        if run_id and run_id in self._cancel_requested and event_type not in {"run_cancelled"}:
            return
        try:
            await self.bus.publish_run_event(payload)
        except Exception:
            pass

    async def _on_tool_event(self, payload: dict[str, Any]) -> None:
        await self._emit_run_event(payload)

    async def _run_hook(self, event: str, payload: dict[str, Any]) -> Any:
        result = await self.hooks.run(event, payload)
        if result.errors:
            await self._emit_run_event(
                {
                    "type": "hook_error",
                    "kind": "hook",
                    "event": event,
                    "run_id": payload.get("run_id", ""),
                    "session_key": payload.get("session_key", ""),
                    "errors": result.errors,
                    "ts": time.time(),
                }
            )
        return result

    @staticmethod
    def _merge_usage(base: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
        raw = usage or {}
        prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        total = int(raw.get("total_tokens") or 0)
        if total <= 0:
            total = prompt + completion
        return {
            "prompt_tokens": int(base.get("prompt_tokens") or 0) + prompt,
            "completion_tokens": int(base.get("completion_tokens") or 0) + completion,
            "total_tokens": int(base.get("total_tokens") or 0) + total,
        }

    @staticmethod
    def _truncate_text(text: str | None, max_len: int = 1000) -> str:
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"... (truncated, {len(text) - max_len} more chars)"

    def _check_cancelled(self, run_id: str) -> None:
        if run_id in self._cancel_requested:
            raise asyncio.CancelledError

    def _mark_run_closed(self, run_id: str) -> None:
        if not run_id:
            return
        if run_id in self._closed_run_set:
            return
        self._closed_run_set.add(run_id)
        self._closed_run_order.append(run_id)
        while len(self._closed_run_order) > 1500:
            old = self._closed_run_order.popleft()
            self._closed_run_set.discard(old)

    def _load_persisted_runs(self) -> None:
        rows = self._run_store.load_recent(limit=200)
        for row in reversed(rows):
            try:
                run = RunState.from_dict(row)
            except Exception:
                continue
            if not run.run_id:
                continue
            self._recent_runs.appendleft(run)

    async def _chat_with_optional_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: str | None,
        run_id: str,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> tuple[LLMResponse, bool]:
        had_deltas = False
        if self.stream_events and hasattr(self.provider, "stream_chat"):
            try:
                idx = 0
                final: LLMResponse | None = None
                async for event in self.provider.stream_chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    thinking=thinking,
                ):
                    if event.type == "delta" and event.delta:
                        self._check_cancelled(run_id)
                        had_deltas = True
                        await self._emit_run_event(
                            {
                                "type": "assistant_delta",
                                "kind": "assistant",
                                "run_id": run_id,
                                "session_key": session_key,
                                "channel": channel,
                                "chat_id": chat_id,
                                "delta": event.delta,
                                "index": idx,
                                "ts": time.time(),
                            }
                        )
                        idx += 1
                    elif event.type == "final" and event.response:
                        final = event.response
                if final is not None:
                    return final, had_deltas
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fall through to non-streaming fallback.
                pass

        response = await self.provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            thinking=thinking,
        )
        return response, False
