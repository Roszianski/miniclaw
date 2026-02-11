import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
from pydantic import ValidationError

from miniclaw.agent.loop import AgentLoop
from miniclaw.agent.router import AgentRouter
from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.config.schema import AgentRoutingRule, Config
from miniclaw.providers.base import LLMProvider, LLMResponse


class _StaticProvider(LLMProvider):
    def __init__(self, reply: str):
        super().__init__(api_key=None, api_base=None)
        self.reply = reply

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ) -> LLMResponse:
        return LLMResponse(content=self.reply)

    def get_default_model(self) -> str:
        return "test-model"


class _FakeAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.inbound: list[tuple[InboundMessage, bool]] = []
        self.stopped = False

    def submit_inbound(self, msg: InboundMessage, publish_outbound: bool = True) -> str:
        self.inbound.append((msg, publish_outbound))
        return f"{self.agent_id}-{len(self.inbound)}"

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        return f"{self.agent_id}:{session_key}:{content}:{channel}:{chat_id}"

    def list_runs(self, limit: int = 50) -> list[dict]:
        return []

    def cancel_run(self, run_id: str) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def _wait_until(predicate, timeout_s: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


def test_agents_config_validates_max_three_instances() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "agents": {
                    "instances": [
                        {"id": "default"},
                        {"id": "a"},
                        {"id": "b"},
                        {"id": "c"},
                    ]
                }
            }
        )


def test_agents_config_requires_default_when_instances_present() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"agents": {"instances": [{"id": "alpha"}]}})


def test_agents_config_rejects_rule_for_unknown_agent() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "agents": {
                    "instances": [{"id": "default"}, {"id": "helper"}],
                    "routing": {"rules": [{"agent": "ghost", "channel": "telegram"}]},
                }
            }
        )


async def test_router_rule_priority_fallback_and_persistent_binding() -> None:
    bus = MessageBus()
    default_agent = _FakeAgent("default")
    helper_agent = _FakeAgent("helper")
    router = AgentRouter(
        bus=bus,
        agents={"default": default_agent, "helper": helper_agent},
        routing_rules=[
            AgentRoutingRule(agent="helper", channel="telegram"),
            AgentRoutingRule(agent="default", channel="telegram", chat_id="42"),
        ],
    )
    task = asyncio.create_task(router.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="telegram", sender_id="u1", chat_id="42", content="first")
        )
        await bus.publish_inbound(
            InboundMessage(channel="whatsapp", sender_id="u2", chat_id="99", content="second")
        )
        await bus.publish_inbound(
            InboundMessage(channel="telegram", sender_id="u3", chat_id="42", content="third")
        )
        await _wait_until(lambda: len(helper_agent.inbound) == 2 and len(default_agent.inbound) == 1)

        helper_msg_1 = helper_agent.inbound[0][0]
        helper_msg_2 = helper_agent.inbound[1][0]
        default_msg = default_agent.inbound[0][0]
        assert helper_msg_1.metadata.get("session_key") == "agent:helper:telegram:42"
        assert helper_msg_2.metadata.get("session_key") == "agent:helper:telegram:42"
        assert default_msg.metadata.get("session_key") == "agent:default:whatsapp:99"
    finally:
        router.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_router_keeps_system_messages_on_bound_agent() -> None:
    bus = MessageBus()
    default_agent = _FakeAgent("default")
    helper_agent = _FakeAgent("helper")
    router = AgentRouter(
        bus=bus,
        agents={"default": default_agent, "helper": helper_agent},
        routing_rules=[AgentRoutingRule(agent="helper", channel="telegram", chat_id="7")],
    )
    task = asyncio.create_task(router.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="telegram", sender_id="u1", chat_id="7", content="hello")
        )
        await _wait_until(lambda: len(helper_agent.inbound) == 1)

        await bus.publish_inbound(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="telegram:7",
                content="subagent result",
            )
        )
        await _wait_until(lambda: len(helper_agent.inbound) == 2)
        routed_system = helper_agent.inbound[1][0]
        assert routed_system.metadata.get("session_key") == "agent:helper:telegram:7"
    finally:
        router.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_router_session_isolation_between_agent_loops(sandbox_home) -> None:
    bus = MessageBus()
    default_loop = AgentLoop(bus=bus, provider=_StaticProvider("default"), workspace=sandbox_home, stream_events=False)
    helper_loop = AgentLoop(bus=bus, provider=_StaticProvider("helper"), workspace=sandbox_home, stream_events=False)
    router = AgentRouter(
        bus=bus,
        agents={"default": default_loop, "helper": helper_loop},
        routing_rules=[AgentRoutingRule(agent="helper", sender_id="helper-user")],
    )
    task = asyncio.create_task(router.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="telegram", sender_id="helper-user", chat_id="100", content="hello helper")
        )
        await bus.publish_inbound(
            InboundMessage(channel="telegram", sender_id="regular-user", chat_id="200", content="hello default")
        )

        normal: list = []
        while len(normal) < 2:
            msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            if msg.control:
                continue
            if not (msg.content or "").strip():
                continue
            normal.append(msg)
        assert {m.content for m in normal} == {"helper", "default"}

        assert "agent:helper:telegram:100" in helper_loop.sessions._cache
        assert "agent:default:telegram:200" in default_loop.sessions._cache
        assert "agent:default:telegram:200" not in helper_loop.sessions._cache
        assert "agent:helper:telegram:100" not in default_loop.sessions._cache
    finally:
        router.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_single_agent_compatibility_without_router(sandbox_home) -> None:
    bus = MessageBus()
    agent = AgentLoop(bus=bus, provider=_StaticProvider("ok"), workspace=sandbox_home, stream_events=False)
    response = await agent.process_direct(
        content="hi",
        session_key="cli:compat",
        channel="cli",
        chat_id="compat",
    )
    assert response == "ok"
    assert "cli:compat" in agent.sessions._cache
