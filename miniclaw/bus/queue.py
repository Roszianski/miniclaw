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
        self._response_waiters: dict[str, deque[tuple[asyncio.Future[str], str | None]]] = {}
        self._response_waiters_by_approval: dict[str, tuple[str, asyncio.Future[str]]] = {}
        self._approval_listeners: list[asyncio.Queue[dict]] = []
        self._run_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._steer_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._agent_message_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._recent_agent_messages: deque[dict[str, Any]] = deque(maxlen=500)
        self._pending_approvals: dict[str, dict] = {}
    
    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        waiter = self._pop_next_waiter(msg.session_key)
        if waiter is not None:
            future, _approval_id = waiter
            future.set_result(msg.content)
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

    async def wait_for_response(
        self,
        session_key: str,
        timeout: float = 60.0,
        approval_id: str | None = None,
    ) -> str | None:
        """Wait for the next inbound message for a given session key."""
        key = str(session_key or "")
        approval_key = str(approval_id or "").strip() or None
        if approval_key:
            existing = self._response_waiters_by_approval.get(approval_key)
            if existing and not existing[1].done():
                return None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        queue = self._response_waiters.setdefault(key, deque())
        queue.append((fut, approval_key))
        if approval_key:
            self._response_waiters_by_approval[approval_key] = (key, fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._remove_waiter(key, fut, approval_key)

    def submit_response(self, session_key: str, content: str, approval_id: str | None = None) -> bool:
        """Submit a response to a waiting approval without inbound message."""
        key = str(session_key or "")
        approval_key = str(approval_id or "").strip() or None
        if approval_key:
            match = self._response_waiters_by_approval.get(approval_key)
            if not match:
                return False
            matched_session_key, waiter = match
            if key and matched_session_key != key:
                return False
            if waiter.done():
                self._remove_waiter(matched_session_key, waiter, approval_key)
                return False
            waiter.set_result(content)
            self._remove_waiter(matched_session_key, waiter, approval_key)
            self._pending_approvals.pop(approval_key, None)
            return True

        next_waiter = self._pop_next_waiter(key)
        if next_waiter is not None:
            waiter, _approval = next_waiter
            waiter.set_result(content)
            self._pending_approvals = {
                k: v for k, v in self._pending_approvals.items()
                if v.get("session_key") != key
            }
            return True
        return False

    def _pop_next_waiter(self, session_key: str) -> tuple[asyncio.Future[str], str | None] | None:
        queue = self._response_waiters.get(session_key)
        if not queue:
            return None
        while queue:
            future, approval_id = queue.popleft()
            if approval_id:
                mapped = self._response_waiters_by_approval.get(approval_id)
                if mapped and mapped[1] is future:
                    self._response_waiters_by_approval.pop(approval_id, None)
            if future.done():
                continue
            if not queue:
                self._response_waiters.pop(session_key, None)
            return future, approval_id
        self._response_waiters.pop(session_key, None)
        return None

    def _remove_waiter(
        self,
        session_key: str,
        future: asyncio.Future[str],
        approval_id: str | None,
    ) -> None:
        queue = self._response_waiters.get(session_key)
        if queue:
            filtered = deque((f, aid) for f, aid in queue if f is not future)
            if filtered:
                self._response_waiters[session_key] = filtered
            else:
                self._response_waiters.pop(session_key, None)
        if approval_id:
            mapped = self._response_waiters_by_approval.get(approval_id)
            if mapped and mapped[1] is future:
                self._response_waiters_by_approval.pop(approval_id, None)

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
