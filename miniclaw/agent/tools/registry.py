"""Tool registry for dynamic tool management."""

import time
import uuid
import inspect
import re
from typing import Any, Awaitable, Callable

from miniclaw.bus.events import OutboundMessage
from miniclaw.config.schema import ToolApprovalConfig
from miniclaw.audit.logger import AuditLogger
from miniclaw.bus.queue import MessageBus

from miniclaw.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    """
    
    def __init__(
        self,
        bus: MessageBus | None = None,
        approval_config: ToolApprovalConfig | None = None,
        approval_timeout_s: float = 60.0,
        audit_logger: AuditLogger | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._bus = bus
        self._approval_config = approval_config
        self._approval_timeout_s = approval_timeout_s
        self._audit_logger = audit_logger
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""
        self._user_key = ""
        self._run_id = ""
        self._on_tool_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        self._last_execution: dict[str, Any] | None = None

    def set_context(
        self,
        channel: str,
        chat_id: str,
        user_key: str | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Set context for approvals and logging."""
        self._channel = channel
        self._chat_id = chat_id
        self._session_key = (session_key or f"{channel}:{chat_id}").strip()
        self._user_key = user_key or ""
        self._run_id = run_id or ""
        for tool in self._tools.values():
            setter = getattr(tool, "set_registry_context", None)
            if callable(setter):
                try:
                    setter(
                        channel=self._channel,
                        chat_id=self._chat_id,
                        session_key=self._session_key,
                        user_key=self._user_key,
                        run_id=self._run_id,
                    )
                except Exception:
                    continue

    def set_tool_event_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    ) -> None:
        """Set callback for streaming tool events to dashboard/internal listeners."""
        self._on_tool_event = callback

    def get_last_execution(self) -> dict[str, Any] | None:
        """Get metadata from the most recent tool execution."""
        return dict(self._last_execution) if self._last_execution else None
    
    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]
    
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.
        
        Args:
            name: Tool name.
            params: Tool parameters.
        
        Returns:
            Tool execution result as string.
        
        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        errors = tool.validate_params(params)
        if errors:
            return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)

        # Tool approval check
        approval_mode = self._get_approval_mode(name)
        if approval_mode == "always_deny":
            return f"Error: Tool '{name}' is not allowed by policy"
        if approval_mode == "always_ask":
            approved = await self._request_approval(name, params)
            if not approved:
                return f"Error: Tool '{name}' denied or approval timed out"

        await self._emit_tool_event(
            event_type="tool_start",
            tool_name=name,
            params=params,
            ok=None,
            result=None,
            duration_ms=0,
        )

        start = time.monotonic()
        ok = True
        result = ""
        try:
            result = await tool.execute(**params)
            return result
        except Exception as e:
            ok = False
            result = f"Error executing {name}: {str(e)}"
            return result
        finally:
            duration = (time.monotonic() - start) * 1000
            sanitized_params = self._sanitize(params, max_str_len=500)
            sanitized_result = self._sanitize(result, max_str_len=1200)
            self._last_execution = {
                "run_id": self._run_id,
                "session_key": self._session_key,
                "channel": self._channel,
                "chat_id": self._chat_id,
                "tool_name": name,
                "params": sanitized_params,
                "result": sanitized_result,
                "ok": ok,
                "duration_ms": round(duration, 1),
                "at": time.time(),
            }
            await self._emit_tool_event(
                event_type="tool_end",
                tool_name=name,
                params=sanitized_params,
                ok=ok,
                result=sanitized_result,
                duration_ms=duration,
            )
            if self._audit_logger:
                self._audit_logger.log_tool(
                    tool_name=name,
                    params=sanitized_params,
                    result=sanitized_result if isinstance(sanitized_result, str) else str(sanitized_result),
                    duration_ms=duration,
                    success=ok,
                )

    def _get_approval_mode(self, tool_name: str) -> str:
        """Return approval mode for tool name."""
        if not self._approval_config:
            return "always_allow"
        if tool_name in {"exec", "process"}:
            return self._approval_config.exec
        if tool_name == "browser":
            return self._approval_config.browser
        if tool_name == "web_fetch":
            return self._approval_config.web_fetch
        if tool_name in ("write_file", "edit_file", "apply_patch"):
            return self._approval_config.write_file
        return "always_allow"

    async def _request_approval(self, tool_name: str, params: dict[str, Any]) -> bool:
        """Ask user for approval via channel and await response."""
        if not self._bus or not self._channel or not self._chat_id:
            return False
        approval_id = str(uuid.uuid4())[:8]
        # Notify dashboard listeners
        try:
            event = {
                "id": approval_id,
                "session_key": self._session_key,
                "channel": self._channel,
                "chat_id": self._chat_id,
                "run_id": self._run_id,
                "tool": tool_name,
                "params": self._sanitize(params, max_str_len=500),
                "created_at": time.time(),
            }
            self._bus.add_pending_approval(event)
            await self._bus.publish_approval(event)
        except Exception:
            pass

        # Send approval request to user
        summary = str(params)
        if len(summary) > 300:
            summary = summary[:300] + "..."
        content = (
            f"Approval required for tool '{tool_name}'.\n"
            f"Params: {summary}\n"
            "Reply with 'approve' or 'deny'."
        )
        await self._bus.publish_outbound(OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=content,
        ))

        response = await self._bus.wait_for_response(self._session_key, timeout=self._approval_timeout_s)
        self._bus.resolve_pending_approval(approval_id=approval_id, session_key=self._session_key)
        if not response:
            return False
        norm = response.strip().lower()
        if norm in ("approve", "approved", "yes", "y"):
            return True
        if norm in ("deny", "denied", "no", "n"):
            return False
        return False

    @classmethod
    def _looks_binary_string(cls, value: str) -> bool:
        lower = value.lower()
        if lower.startswith("data:image/") or lower.startswith("data:application/octet-stream"):
            return True
        if "base64," in lower and len(value) > 120:
            return True
        if len(value) > 800 and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r\t " for ch in value):
            return True
        return False

    @classmethod
    def _sanitize(cls, value: Any, max_str_len: int = 500) -> Any:
        sensitive_key_re = re.compile(
            r"(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|authorization|bearer)",
            re.IGNORECASE,
        )
        inline_patterns = (
            re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"),
            re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*(bearer\s+[^\s,;]+)"),
            re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{12,}"),
        )

        def _mask_text(text: str) -> str:
            out = text
            for pattern in inline_patterns:
                if "authorization" in pattern.pattern.lower():
                    out = pattern.sub(lambda m: f"{m.group(1)}=<redacted:sensitive>", out)
                elif "bearer" in pattern.pattern.lower() and "authorization" not in pattern.pattern.lower():
                    out = pattern.sub("Bearer <redacted:sensitive>", out)
                else:
                    out = pattern.sub(lambda m: f"{m.group(1)}=<redacted:sensitive>", out)
            return out

        if isinstance(value, bytes):
            return "<redacted:binary-bytes>"
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if sensitive_key_re.search(key):
                    out[key] = "<redacted:sensitive>"
                else:
                    out[key] = cls._sanitize(v, max_str_len=max_str_len)
            return out
        if isinstance(value, list):
            return [cls._sanitize(v, max_str_len=max_str_len) for v in value]
        if isinstance(value, str):
            if cls._looks_binary_string(value):
                return "<redacted:binary-payload>"
            value = _mask_text(value)
            if len(value) > max_str_len:
                return value[:max_str_len] + f"... (truncated, {len(value) - max_str_len} more chars)"
            return value
        return value

    async def _emit_tool_event(
        self,
        event_type: str,
        tool_name: str,
        params: dict[str, Any] | None,
        ok: bool | None,
        result: Any,
        duration_ms: float,
    ) -> None:
        if not self._on_tool_event:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "kind": "tool",
            "run_id": self._run_id,
            "session_key": self._session_key,
            "channel": self._channel,
            "chat_id": self._chat_id,
            "tool_name": tool_name,
            "params": self._sanitize(params or {}, max_str_len=500),
            "ok": ok,
            "result": self._sanitize(result, max_str_len=1200),
            "duration_ms": round(duration_ms, 1),
            "ts": time.time(),
        }
        try:
            maybe = self._on_tool_event(payload)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            pass
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools
