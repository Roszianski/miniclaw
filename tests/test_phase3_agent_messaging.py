import asyncio

from miniclaw.agent.router import AgentRouter
from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus


class _FakeAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.inbound: list[tuple[InboundMessage, bool]] = []

    def submit_inbound(self, msg: InboundMessage, publish_outbound: bool = True) -> str:
        self.inbound.append((msg, publish_outbound))
        return f"{self.agent_id}-{len(self.inbound)}"

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        model_override: str | None = None,
    ) -> str:
        return "ok"

    def list_runs(self, limit: int = 50) -> list[dict]:
        return []

    def cancel_run(self, run_id: str) -> bool:
        return False

    def stop(self) -> None:
        return None


async def test_message_bus_agent_event_stream_and_history() -> None:
    bus = MessageBus()
    q = bus.register_agent_message_listener()
    event = {"type": "agent_message", "from_agent_id": "a", "to_agent_id": "b", "content_preview": "hello"}
    await bus.publish_agent_message(event)

    out = await asyncio.wait_for(q.get(), timeout=1.0)
    assert out["from_agent_id"] == "a"
    assert bus.list_agent_messages(limit=1)[0]["to_agent_id"] == "b"
    bus.unregister_agent_message_listener(q)


async def test_router_direct_agent_message_routes_to_target_agent() -> None:
    bus = MessageBus()
    default = _FakeAgent("default")
    helper = _FakeAgent("helper")
    router = AgentRouter(
        bus=bus,
        agents={"default": default, "helper": helper},
    )
    ok = router.send_agent_message(
        from_agent_id="default",
        to_agent_id="helper",
        content="please summarize queue state",
        metadata={"reason": "sync"},
    )
    assert ok is True
    assert len(helper.inbound) == 1
    msg, publish_outbound = helper.inbound[0]
    assert publish_outbound is False
    assert msg.sender_id == "agent:default"
    assert msg.metadata.get("agent_message") is True
    assert msg.metadata.get("session_key") == "agent:helper:inbox:default"

    await asyncio.sleep(0)
    events = bus.list_agent_messages(limit=5)
    assert events
    assert events[-1]["from_agent_id"] == "default"
    assert events[-1]["to_agent_id"] == "helper"
