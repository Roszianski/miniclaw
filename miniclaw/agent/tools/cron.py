"""Cron tool for scheduling reminders and tasks."""

from typing import Any

from miniclaw.agent.tools.base import Tool
from miniclaw.cron.service import CronService
from miniclaw.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            "Use kind='reminder' to deliver messages directly, kind='task' to run through the agent."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {
                    "type": "string",
                    "description": "Reminder message or task description (for add)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["reminder", "task"],
                    "description": "reminder = deliver message directly; task = agent processes and sends result (default: task)",
                },
                "isolated": {
                    "type": "boolean",
                    "description": "Run in isolated session to avoid polluting main chat history (default: false)",
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "deliver_to": {
                    "type": "object",
                    "description": "Optional delivery target: {channel, chat_id} for cross-channel delivery",
                    "properties": {
                        "channel": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        kind: str = "task",
        isolated: bool = False,
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        deliver_to: dict[str, str] | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add_job(message, kind, isolated, every_seconds, cron_expr, deliver_to)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        kind: str,
        isolated: bool,
        every_seconds: int | None,
        cron_expr: str | None,
        deliver_to: dict[str, str] | None,
    ) -> str:
        if not message:
            return "Error: message is required for add"

        # Determine delivery target
        channel = self._channel
        chat_id = self._chat_id
        if deliver_to:
            channel = deliver_to.get("channel", channel)
            chat_id = deliver_to.get("chat_id", chat_id)

        if not channel or not chat_id:
            return "Error: no session context (channel/chat_id)"

        # Build schedule
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr)
        else:
            return "Error: either every_seconds or cron_expr is required"

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=channel,
            to=chat_id,
            kind=kind,
            isolated=isolated,
        )
        kind_label = "reminder" if kind == "reminder" else "task"
        return f"Created {kind_label} '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            kind_label = j.payload.kind
            lines.append(f"- {j.name} (id: {j.id}, {j.schedule.kind}, {kind_label})")
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
