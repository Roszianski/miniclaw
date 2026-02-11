"""Deterministic multi-agent router for inbound messages."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus

if TYPE_CHECKING:
    from miniclaw.agent.loop import AgentLoop
    from miniclaw.config.schema import AgentRoutingRule


class AgentRouter:
    """Routes messages to one of up to three agent loops."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        agents: dict[str, "AgentLoop"],
        default_agent_id: str = "default",
        routing_rules: list["AgentRoutingRule"] | None = None,
    ):
        if not agents:
            raise ValueError("AgentRouter requires at least one agent instance.")
        if len(agents) > 3:
            raise ValueError("AgentRouter supports at most 3 agent instances.")
        if default_agent_id not in agents:
            raise ValueError(f"Default agent '{default_agent_id}' is not configured.")

        self.bus = bus
        self.agents = dict(agents)
        self.default_agent_id = default_agent_id
        self.routing_rules = list(routing_rules or [])

        self._running = False
        self._session_bindings: dict[str, str] = {}

    @property
    def default_agent(self) -> "AgentLoop":
        return self.agents[self.default_agent_id]

    async def run(self) -> None:
        """Consume inbound messages from the bus and dispatch to target agents."""
        self._running = True
        logger.info("Agent router started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            binding_key, namespace_channel, namespace_chat_id = self._routing_context(msg)
            agent_id = self._resolve_agent_id(msg=msg, binding_key=binding_key)
            session_key = self._namespaced_session_key(
                agent_id=agent_id,
                channel=namespace_channel,
                chat_id=namespace_chat_id,
            )
            routed_msg = self._with_session_override(msg=msg, session_key=session_key)
            self.agents[agent_id].submit_inbound(routed_msg, publish_outbound=True)

    def stop(self) -> None:
        """Stop router dispatch and all managed agents."""
        self._running = False
        for agent in self.agents.values():
            agent.stop()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        model_override: str | None = None,
    ) -> str:
        """
        Process a direct message through routed agent selection.

        Explicit session keys are preserved under an agent namespace.
        """
        binding_key = session_key or f"{channel}:{chat_id}"
        synthetic = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata={"is_group": False},
        )
        agent_id = self._resolve_agent_id(msg=synthetic, binding_key=binding_key)
        if binding_key == f"{channel}:{chat_id}":
            namespaced = self._namespaced_session_key(agent_id=agent_id, channel=channel, chat_id=chat_id)
        else:
            namespaced = f"agent:{agent_id}:{binding_key}"
        return await self.agents[agent_id].process_direct(
            content=content,
            session_key=namespaced,
            channel=channel,
            chat_id=chat_id,
            model_override=model_override,
        )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent runs across agents, newest-first."""
        limit = max(1, min(500, int(limit)))
        merged: list[dict[str, Any]] = []
        for agent_id, agent in self.agents.items():
            for run in agent.list_runs(limit=limit):
                item = dict(run)
                item["agent_id"] = agent_id
                merged.append(item)

        def _created_ts(run: dict[str, Any]) -> float:
            raw = run.get("created_at")
            if isinstance(raw, str) and raw:
                try:
                    return datetime.fromisoformat(raw).timestamp()
                except ValueError:
                    return 0.0
            return 0.0

        merged.sort(key=_created_ts, reverse=True)
        return merged[:limit]

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a run by id across all routed agents."""
        for agent in self.agents.values():
            if agent.cancel_run(run_id):
                return True
        return False

    def steer_run(self, run_id: str, instruction: str, *, source: str = "api", sender_id: str | None = None) -> bool:
        """Steer an in-flight run by id across all routed agents."""
        for agent in self.agents.values():
            if hasattr(agent, "steer_run") and agent.steer_run(
                run_id,
                instruction,
                source=source,
                sender_id=sender_id,
            ):
                return True
        return False

    def get_queue_snapshot(self) -> dict[str, Any]:
        """Return merged queue state across agents."""
        sessions: list[dict[str, Any]] = []
        mode = "queue"
        collect_window_ms = 0
        max_backlog = 0
        for agent_id, agent in self.agents.items():
            if not hasattr(agent, "get_queue_snapshot"):
                continue
            snap = agent.get_queue_snapshot()
            mode = str(snap.get("mode") or mode)
            collect_window_ms = int(snap.get("collect_window_ms") or collect_window_ms or 0)
            max_backlog = int(snap.get("max_backlog") or max_backlog or 0)
            for session in snap.get("sessions", []):
                row = dict(session)
                row["agent_id"] = agent_id
                sessions.append(row)
        sessions.sort(key=lambda s: str(s.get("session_key") or ""))
        return {
            "mode": mode,
            "collect_window_ms": collect_window_ms,
            "max_backlog": max_backlog,
            "sessions": sessions,
        }

    def send_agent_message(
        self,
        *,
        from_agent_id: str,
        to_agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Send a direct message from one agent to another."""
        text = str(content or "").strip()
        if not text:
            return False
        sender = str(from_agent_id or "").strip() or self.default_agent_id
        target = str(to_agent_id or "").strip()
        if target not in self.agents:
            return False
        session_key = f"agent:{target}:inbox:{sender}"
        msg = InboundMessage(
            channel="system",
            sender_id=f"agent:{sender}",
            chat_id=f"{sender}:{target}",
            content=text,
            metadata={
                "session_key": session_key,
                "agent_message": True,
                "from_agent_id": sender,
                "to_agent_id": target,
                **dict(metadata or {}),
            },
        )
        self.agents[target].submit_inbound(msg, publish_outbound=False)
        event = {
            "type": "agent_message",
            "kind": "agent",
            "from_agent_id": sender,
            "to_agent_id": target,
            "session_key": session_key,
            "content_preview": text[:300],
            "ts": datetime.now().timestamp(),
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.bus.publish_agent_message(event))
        except RuntimeError:
            pass
        return True

    def _resolve_agent_id(self, *, msg: InboundMessage, binding_key: str) -> str:
        existing = self._session_bindings.get(binding_key)
        if existing in self.agents:
            return existing

        selected = self.default_agent_id
        for rule in self.routing_rules:
            if self._rule_matches(rule=rule, msg=msg):
                selected = rule.agent.strip()
                break

        if selected not in self.agents:
            selected = self.default_agent_id
        self._session_bindings[binding_key] = selected
        return selected

    @staticmethod
    def _with_session_override(msg: InboundMessage, session_key: str) -> InboundMessage:
        metadata = dict(msg.metadata or {})
        metadata["session_key"] = session_key
        return InboundMessage(
            channel=msg.channel,
            sender_id=msg.sender_id,
            chat_id=msg.chat_id,
            content=msg.content,
            timestamp=msg.timestamp,
            media=list(msg.media or []),
            metadata=metadata,
        )

    @staticmethod
    def _namespaced_session_key(*, agent_id: str, channel: str, chat_id: str) -> str:
        return f"agent:{agent_id}:{channel}:{chat_id}"

    @staticmethod
    def _routing_context(msg: InboundMessage) -> tuple[str, str, str]:
        """
        Return (binding_key, namespace_channel, namespace_chat_id).

        System messages carry origin in chat_id as '<channel>:<chat_id>'.
        """
        if msg.channel == "system" and ":" in msg.chat_id:
            origin_channel, origin_chat_id = msg.chat_id.split(":", 1)
            key = f"{origin_channel}:{origin_chat_id}"
            return key, origin_channel, origin_chat_id
        return msg.session_key, msg.channel, msg.chat_id

    @staticmethod
    def _match_value(rule_value: str | list[str] | None, actual: str) -> bool:
        if rule_value is None:
            return True
        if isinstance(rule_value, list):
            return actual in {str(v) for v in rule_value}
        return actual == str(rule_value)

    @classmethod
    def _rule_matches(cls, *, rule: "AgentRoutingRule", msg: InboundMessage) -> bool:
        if not cls._match_value(rule.channel, msg.channel):
            return False
        if not cls._match_value(rule.chat_id, msg.chat_id):
            return False
        if not cls._match_value(rule.sender_id, msg.sender_id):
            return False

        if rule.is_group is not None:
            metadata = msg.metadata or {}
            is_group = bool(metadata.get("is_group"))
            if "isGroup" in metadata:
                is_group = bool(metadata.get("isGroup"))
            if is_group != rule.is_group:
                return False
        return True
