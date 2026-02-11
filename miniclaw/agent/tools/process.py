"""Background process management tool."""

import json
from typing import Any

from miniclaw.agent.tools.base import Tool
from miniclaw.processes.manager import ProcessManager


class ProcessTool(Tool):
    """Tool for managing background processes."""

    def __init__(self, manager: ProcessManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Manage background processes. "
            "Use action=start|stop|list|logs to launch and manage long-running commands."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "list", "logs"]},
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "name": {"type": "string"},
                "process_id": {"type": "string"},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        command: str | None = None,
        cwd: str | None = None,
        name: str | None = None,
        process_id: str | None = None,
        tail_lines: int = 200,
        **kwargs: Any,
    ) -> str:
        op = (action or "").strip().lower()
        if op == "list":
            return json.dumps({"processes": self._manager.list_processes()}, ensure_ascii=False, indent=2)

        if op == "start":
            if not command or not command.strip():
                return "Error: command is required for action=start"
            try:
                started = self._manager.start_process(command=command, cwd=cwd, name=name)
            except Exception as exc:
                return f"Error: failed to start process: {exc}"
            return json.dumps(started, ensure_ascii=False, indent=2)

        if op == "stop":
            if not process_id:
                return "Error: process_id is required for action=stop"
            ok = self._manager.stop_process(process_id)
            return json.dumps({"ok": bool(ok), "process_id": process_id}, ensure_ascii=False)

        if op == "logs":
            if not process_id:
                return "Error: process_id is required for action=logs"
            try:
                logs = self._manager.read_logs(process_id, tail_lines=tail_lines)
            except KeyError as exc:
                return f"Error: {exc}"
            return logs or "(no logs)"

        return "Error: action must be one of start|stop|list|logs"
