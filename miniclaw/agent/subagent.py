"""Subagent manager for background task execution."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniclaw.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from miniclaw.agent.tools.registry import ToolRegistry
from miniclaw.agent.tools.shell import ExecTool
from miniclaw.agent.tools.web import WebFetchTool, WebSearchTool
from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.providers.base import LLMProvider

if TYPE_CHECKING:
    from miniclaw.config.schema import ExecToolConfig


class SubagentManager:
    """
    Manages background subagent execution.

    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        agent_id: str = "default",
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        sandbox_mode: str = "off",
        sandbox_scope: str = "agent",
        sandbox_workspace_access: str = "rw",
        sandbox_image: str = "openclaw-sandbox:bookworm-slim",
        sandbox_prune_idle_seconds: int = 1800,
        sandbox_prune_max_age_seconds: int = 21600,
        restrict_to_workspace: bool = False,
    ):
        from miniclaw.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.agent_id = (agent_id or "default").strip() or "default"
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.sandbox_mode = (sandbox_mode or "off").strip().lower().replace("-", "_")
        if self.sandbox_mode not in {"off", "non_main", "all"}:
            self.sandbox_mode = "off"
        self.sandbox_scope = (sandbox_scope or "agent").strip().lower()
        if self.sandbox_scope not in {"session", "agent", "shared"}:
            self.sandbox_scope = "agent"
        self.sandbox_workspace_access = (sandbox_workspace_access or "rw").strip().lower()
        if self.sandbox_workspace_access not in {"none", "ro", "rw"}:
            self.sandbox_workspace_access = "rw"
        self.sandbox_image = sandbox_image or "openclaw-sandbox:bookworm-slim"
        self.sandbox_prune_idle_seconds = max(30, int(sandbox_prune_idle_seconds))
        self.sandbox_prune_max_age_seconds = max(60, int(sandbox_prune_max_age_seconds))
        self.restrict_to_workspace = restrict_to_workspace
        self._max_concurrency = 3
        self._task_timeout_s = 15 * 60
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_usage: dict[str, dict[str, int]] = {}
        self._usage_totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.

        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.

        Returns:
            Status message indicating the subagent was started.
        """
        if len(self._running_tasks) >= self._max_concurrency:
            return (
                f"Subagent capacity reached ({self._max_concurrency}). "
                "Wait for an active task to finish before spawning another."
            )

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }

        # Create background task
        bg_task = asyncio.create_task(
            self._run_subagent_guarded(task_id, task, display_label, origin)
        )
        self._running_tasks[task_id] = bg_task

        # Cleanup when done
        bg_task.add_done_callback(lambda _: self._running_tasks.pop(task_id, None))

        logger.info(f"Spawned subagent [{task_id}]: {display_label}")
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent_guarded(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        async with self._semaphore:
            try:
                await asyncio.wait_for(
                    self._run_subagent(task_id, task, label, origin),
                    timeout=float(self._task_timeout_s),
                )
            except asyncio.TimeoutError:
                timeout_msg = (
                    f"Subagent timed out after {self._task_timeout_s} seconds before completing the task."
                )
                logger.warning(f"Subagent [{task_id}] timed out")
                await self._announce_result(task_id, label, task, timeout_msg, origin, "error")
            finally:
                self._task_usage.pop(task_id, None)

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info(f"Subagent [{task_id}] starting task: {label}")

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            tools.register(ReadFileTool(allowed_dir=allowed_dir))
            tools.register(WriteFileTool(allowed_dir=allowed_dir))
            tools.register(ListDirTool(allowed_dir=allowed_dir))
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                sandbox_mode=self.sandbox_mode,
                sandbox_scope=self.sandbox_scope,
                sandbox_workspace_access=self.sandbox_workspace_access,
                sandbox_image=self.sandbox_image,
                sandbox_prune_idle_seconds=self.sandbox_prune_idle_seconds,
                sandbox_prune_max_age_seconds=self.sandbox_prune_max_age_seconds,
                sandbox_agent_id=f"{self.agent_id}:subagent",
                resource_limits=self.exec_config.resource_limits,
                restrict_to_workspace=self.restrict_to_workspace,
            ))
            tools.register(WebSearchTool(api_key=self.brave_api_key))
            tools.register(WebFetchTool())
            tools.set_context(
                channel="system",
                chat_id=f"subagent:{task_id}",
                user_key=f"subagent:{task_id}",
                run_id=f"subagent:{task_id}",
                session_key=f"subagent:{self.agent_id}:{task_id}",
            )

            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(task)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            while iteration < max_iterations:
                iteration += 1

                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )
                usage = self._accumulate_usage(usage, response.usage if hasattr(response, "usage") else {})

                if response.has_tool_calls:
                    # Add assistant message with tool calls
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
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug(f"Subagent [{task_id}] executing: {tool_call.name} with arguments: {args_str}")
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                else:
                    final_result = response.content
                    break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            self._task_usage[task_id] = dict(usage)
            self._usage_totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
            self._usage_totals["completion_tokens"] += usage.get("completion_tokens", 0)
            self._usage_totals["total_tokens"] += usage.get("total_tokens", 0)

            usage_line = ""
            if usage.get("total_tokens", 0) > 0:
                usage_line = (
                    "\n\nUsage: "
                    f"prompt={usage.get('prompt_tokens', 0)}, "
                    f"completion={usage.get('completion_tokens', 0)}, "
                    f"total={usage.get('total_tokens', 0)} tokens"
                )
                final_result = (final_result or "") + usage_line

            logger.info(f"Subagent [{task_id}] completed successfully")
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error(f"Subagent [{task_id}] failed: {e}")
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )

        await self.bus.publish_inbound(msg)
        logger.debug(f"Subagent [{task_id}] announced result to {origin['channel']}:{origin['chat_id']}")

    def _build_subagent_prompt(self, task: str) -> str:
        """Build a focused system prompt for the subagent."""
        return f"""# Subagent

You are a subagent spawned by the main agent to complete a specific task.

## Your Task
{task}

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}

	When you have completed the task, provide a clear summary of your findings or actions."""

    @staticmethod
    def _accumulate_usage(base: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion) or 0)
        return {
            "prompt_tokens": int(base.get("prompt_tokens", 0)) + prompt,
            "completion_tokens": int(base.get("completion_tokens", 0)) + completion,
            "total_tokens": int(base.get("total_tokens", 0)) + total,
        }

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
