"""Message tool for sending messages to users."""

import time
from typing import Any, Callable, Awaitable

from miniclaw.agent.tools.base import Tool
from miniclaw.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""
    
    def __init__(
        self, 
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = ""
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._run_id = ""
        self._sent_by_run: dict[str, list[dict[str, Any]]] = {}
    
    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_run_context(self, run_id: str) -> None:
        """Set the current run context for dedup/suppression logic."""
        self._run_id = run_id

    def get_run_sends(self, run_id: str) -> list[dict[str, Any]]:
        """Get message sends recorded for a run."""
        return list(self._sent_by_run.get(run_id, []))

    def clear_run_sends(self, run_id: str) -> None:
        """Clear message-send records for a completed run."""
        self._sent_by_run.pop(run_id, None)
    
    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback
    
    @property
    def name(self) -> str:
        return "message"
    
    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                }
            },
            "required": ["content"]
        }
    
    async def execute(
        self, 
        content: str, 
        channel: str | None = None, 
        chat_id: str | None = None,
        **kwargs: Any
    ) -> str:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        
        if not channel or not chat_id:
            return "Error: No target channel/chat specified"
        
        if not self._send_callback:
            return "Error: Message sending not configured"
        
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content
        )
        
        try:
            await self._send_callback(msg)
            if self._run_id:
                entries = self._sent_by_run.setdefault(self._run_id, [])
                entries.append({
                    "channel": channel,
                    "chat_id": chat_id,
                    "content": content,
                    "at": time.time(),
                })
                # Bound memory usage.
                if len(entries) > 10:
                    self._sent_by_run[self._run_id] = entries[-10:]
            return f"Message sent to {channel}:{chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
