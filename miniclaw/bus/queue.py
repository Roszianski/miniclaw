"""Async message queue for decoupled channel-agent communication."""

import asyncio
from typing import Any, Awaitable, Callable
from collections import deque

from loguru import logger

from miniclaw.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.
    
    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """
    
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._outbound_subscribers: dict[str, list[Callable[[OutboundMessage], Awaitable[None]]]] = {}
        self._running = False
        self._response_waiters: dict[str, asyncio.Future[str]] = {}
        self._approval_listeners: list[asyncio.Queue[dict]] = []
        self._run_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._steer_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._agent_message_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._recent_agent_messages: deque[dict[str, Any]] = deque(maxlen=500)
        self._pending_approvals: dict[str, dict] = {}
    
    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        waiter = self._response_waiters.get(msg.session_key)
        if waiter and not waiter.done():
            waiter.set_result(msg.content)
            self._response_waiters.pop(msg.session_key, None)
            return
        await self.inbound.put(msg)
    
    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()
    
    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)
    
    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    async def wait_for_response(self, session_key: str, timeout: float = 60.0) -> str | None:
        """Wait for the next inbound message for a given session key."""
        if session_key in self._response_waiters and not self._response_waiters[session_key].done():
            return None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._response_waiters[session_key] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._response_waiters.pop(session_key, None)

    def submit_response(self, session_key: str, content: str, approval_id: str | None = None) -> bool:
        """Submit a response to a waiting approval without inbound message."""
        waiter = self._response_waiters.get(session_key)
        if waiter and not waiter.done():
            waiter.set_result(content)
            self._response_waiters.pop(session_key, None)
            if approval_id:
                self._pending_approvals.pop(approval_id, None)
            else:
                self._pending_approvals = {
                    k: v for k, v in self._pending_approvals.items()
                    if v.get("session_key") != session_key
                }
            return True
        return False

    def add_pending_approval(self, event: dict) -> None:
        """Track pending approval request."""
        approval_id = event.get("id")
        if approval_id:
            self._pending_approvals[approval_id] = event

    def list_pending_approvals(self) -> list[dict]:
        """List pending approval requests."""
        return list(self._pending_approvals.values())

    def resolve_pending_approval(self, approval_id: str | None = None, session_key: str | None = None) -> None:
        """Remove pending approval entries."""
        if approval_id:
            self._pending_approvals.pop(approval_id, None)
            return
        if session_key:
            self._pending_approvals = {
                k: v for k, v in self._pending_approvals.items()
                if v.get("session_key") != session_key
            }

    def register_approval_listener(self) -> asyncio.Queue[dict]:
        """Register a listener for approval events (dashboard)."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._approval_listeners.append(q)
        return q

    def unregister_approval_listener(self, q: asyncio.Queue[dict]) -> None:
        """Unregister an approval listener."""
        if q in self._approval_listeners:
            self._approval_listeners.remove(q)

    async def publish_approval(self, event: dict) -> None:
        """Publish an approval event to all listeners."""
        for q in list(self._approval_listeners):
            try:
                q.put_nowait(event)
            except Exception:
                continue

    def register_run_listener(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a listener for run/lifecycle streaming events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._run_listeners.append(q)
        return q

    def unregister_run_listener(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Unregister a run event listener."""
        if q in self._run_listeners:
            self._run_listeners.remove(q)

    async def publish_run_event(self, event: dict[str, Any]) -> None:
        """Publish a run event to all listeners."""
        for q in list(self._run_listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    continue
            except Exception:
                continue

    def register_steer_listener(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a listener for steer events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._steer_listeners.append(q)
        return q

    def unregister_steer_listener(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Unregister a steer listener."""
        if q in self._steer_listeners:
            self._steer_listeners.remove(q)

    async def publish_steer_event(self, event: dict[str, Any]) -> None:
        """Publish a steer event to all listeners."""
        for q in list(self._steer_listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    continue
            except Exception:
                continue

    def register_agent_message_listener(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a listener for agent-to-agent message events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._agent_message_listeners.append(q)
        return q

    def unregister_agent_message_listener(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Unregister agent message listener."""
        if q in self._agent_message_listeners:
            self._agent_message_listeners.remove(q)

    async def publish_agent_message(self, event: dict[str, Any]) -> None:
        """Publish an agent-to-agent message event."""
        self._recent_agent_messages.append(dict(event))
        for q in list(self._agent_message_listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    continue
            except Exception:
                continue

    def list_agent_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent agent message events."""
        cap = max(1, min(500, int(limit)))
        rows = list(self._recent_agent_messages)
        return rows[-cap:]
    
    def subscribe_outbound(
        self, 
        channel: str, 
        callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """Subscribe to outbound messages for a specific channel."""
        if channel not in self._outbound_subscribers:
            self._outbound_subscribers[channel] = []
        self._outbound_subscribers[channel].append(callback)
    
    async def dispatch_outbound(self) -> None:
        """
        Dispatch outbound messages to subscribed channels.
        Run this as a background task.
        """
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self.outbound.get(), timeout=1.0)
                subscribers = self._outbound_subscribers.get(msg.channel, [])
                for callback in subscribers:
                    try:
                        await callback(msg)
                    except Exception as e:
                        logger.error(f"Error dispatching to {msg.channel}: {e}")
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the dispatcher loop."""
        self._running = False
    
    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()
    
    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
