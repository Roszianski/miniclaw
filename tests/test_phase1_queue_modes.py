import asyncio
import inspect
from pathlib import Path

import pytest

from miniclaw.agent.loop import AgentLoop
from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.config.schema import QueueConfig
from miniclaw.providers.base import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    def __init__(self, steps):
        super().__init__(api_key=None, api_base=None)
        self.steps = list(steps)
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ) -> LLMResponse:
        idx = min(self.calls, len(self.steps) - 1)
        step = self.steps[idx]
        self.calls += 1

        if isinstance(step, tuple):
            delay, content = step
            await asyncio.sleep(delay)
            return LLMResponse(content=content)
        if isinstance(step, LLMResponse):
            return step
        if callable(step):
            maybe = step(messages, tools, model, max_tokens, temperature, thinking)
            if inspect.isawaitable(maybe):
                return await maybe
            return maybe
        return LLMResponse(content=str(step))

    def get_default_model(self) -> str:
        return "test-model"


def _active_session_runs(agent: AgentLoop, session_key: str) -> list[dict]:
    return [
        run for run in agent.list_runs(limit=20) if run.get("session_key") == session_key and run.get("status") in {"queued", "running"}
    ]


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def test_queue_mode_steer_reuses_running_run(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.3, "first")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        queue_config=QueueConfig(mode="steer"),
        stream_events=True,
    )
    events = bus.register_run_listener()

    primary = asyncio.create_task(agent.process_direct("first", session_key="cli:q1", channel="cli", chat_id="q1"))
    await asyncio.sleep(0.05)

    active = _active_session_runs(agent, "cli:q1")
    assert len(active) == 1
    running_id = active[0]["run_id"]

    steer_id = agent.submit_inbound(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="q1",
            content="adjust direction",
            metadata={"session_key": "cli:q1"},
        ),
        publish_outbound=False,
    )
    assert steer_id == running_id
    assert len(_active_session_runs(agent, "cli:q1")) == 1

    out = await primary
    assert out == "first"

    await asyncio.sleep(0)
    payloads = []
    while not events.empty():
        payloads.append(events.get_nowait())
    assert any(p.get("type") == "run_steer" for p in payloads)


async def test_steer_mode_keeps_session_control_commands_as_commands(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.3, "first"), LLMResponse(content="second")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        queue_config=QueueConfig(mode="steer"),
        stream_events=True,
    )

    slow = asyncio.create_task(agent.process_direct("first", session_key="cli:ctrl", channel="cli", chat_id="ctrl"))
    await asyncio.sleep(0.05)

    status_task = asyncio.create_task(
        agent.process_direct("/status", session_key="cli:ctrl", channel="cli", chat_id="ctrl")
    )
    await asyncio.sleep(0.05)

    active = _active_session_runs(agent, "cli:ctrl")
    assert len(active) == 2

    slow_out, status_out = await asyncio.gather(slow, status_task)
    assert slow_out == "first"
    assert "Model:" in status_out
    assert provider.calls == 1


async def test_queue_mode_collect_merges_messages(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.25, "first"), LLMResponse(content="second")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        queue_config=QueueConfig(mode="collect", collect_window_ms=5000, max_backlog=4),
    )

    first = asyncio.create_task(agent.process_direct("one", session_key="cli:collect", channel="cli", chat_id="collect"))
    await asyncio.sleep(0.05)

    run2 = agent.submit_inbound(
        InboundMessage(channel="cli", sender_id="user", chat_id="collect", content="two", metadata={"session_key": "cli:collect"}),
        publish_outbound=False,
    )
    task2 = agent._active_run_tasks[run2]
    run3 = agent.submit_inbound(
        InboundMessage(channel="cli", sender_id="user", chat_id="collect", content="three", metadata={"session_key": "cli:collect"}),
        publish_outbound=False,
    )

    assert run2 == run3
    queued_content = agent._run_messages[run2].content
    assert "two" in queued_content
    assert "three" in queued_content
    assert "Collected Followup" in queued_content

    assert await first == "first"
    second = await task2
    assert second is not None and second.content == "second"


async def test_queue_mode_followup_replaces_queued_message(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.25, "first"), LLMResponse(content="second")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        queue_config=QueueConfig(mode="followup", max_backlog=4),
    )

    first = asyncio.create_task(agent.process_direct("one", session_key="cli:follow", channel="cli", chat_id="follow"))
    await asyncio.sleep(0.05)

    run2 = agent.submit_inbound(
        InboundMessage(channel="cli", sender_id="user", chat_id="follow", content="two", metadata={"session_key": "cli:follow"}),
        publish_outbound=False,
    )
    task2 = agent._active_run_tasks[run2]
    run3 = agent.submit_inbound(
        InboundMessage(channel="cli", sender_id="user", chat_id="follow", content="three", metadata={"session_key": "cli:follow"}),
        publish_outbound=False,
    )
    assert run2 == run3
    assert agent._run_messages[run2].content == "three"

    assert await first == "first"
    second = await task2
    assert second is not None and second.content == "second"


async def test_queue_overflow_replaces_oldest_queued_message(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.3, "first"), LLMResponse(content="second")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        queue_config=QueueConfig(mode="queue", max_backlog=1),
    )

    first = asyncio.create_task(agent.process_direct("one", session_key="cli:overflow", channel="cli", chat_id="overflow"))
    await asyncio.sleep(0.05)

    run2 = agent.submit_inbound(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="overflow",
            content="two",
            metadata={"session_key": "cli:overflow"},
        ),
        publish_outbound=False,
    )
    task2 = agent._active_run_tasks[run2]

    run3 = agent.submit_inbound(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="overflow",
            content="three",
            metadata={"session_key": "cli:overflow"},
        ),
        publish_outbound=False,
    )

    assert run3 == run2
    assert agent._run_messages[run2].content == "three"

    assert await first == "first"
    second = await task2
    assert second is not None and second.content == "second"
