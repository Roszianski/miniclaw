import asyncio
import inspect
import json
from pathlib import Path

import pytest

from miniclaw.agent.loop import AgentLoop
from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.config.schema import HooksConfig, QueueConfig
from miniclaw.providers.base import LLMProvider, LLMResponse, LLMStreamEvent, ToolCallRequest


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
        if not self.steps:
            return LLMResponse(content="")
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


class StreamingProvider(LLMProvider):
    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ) -> LLMResponse:
        return LLMResponse(content="Hello")

    async def stream_chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ):
        yield LLMStreamEvent(type="delta", delta="Hel")
        yield LLMStreamEvent(type="delta", delta="lo")
        yield LLMStreamEvent(type="final", response=LLMResponse(content="Hello"))

    def get_default_model(self) -> str:
        return "test-model"


def _drain_events(q: asyncio.Queue):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def test_run_lifecycle_events_are_emitted(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([LLMResponse(content="hello")])
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    q = bus.register_run_listener()
    response = await agent.process_direct("hi", session_key="cli:test", channel="cli", chat_id="test")

    assert response == "hello"
    await asyncio.sleep(0)
    events = _drain_events(q)
    event_types = [e.get("type") for e in events]
    assert "run_start" in event_types
    assert "run_end" in event_types


async def test_per_session_queue_serializes_runs(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([
        (0.25, "first"),
        LLMResponse(content="second"),
    ])
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    q = bus.register_run_listener()

    task1 = asyncio.create_task(agent.process_direct("one", session_key="cli:same", channel="cli", chat_id="same"))
    await asyncio.sleep(0.03)
    task2 = asyncio.create_task(agent.process_direct("two", session_key="cli:same", channel="cli", chat_id="same"))

    out1, out2 = await asyncio.gather(task1, task2)
    assert out1 == "first"
    assert out2 == "second"

    await asyncio.sleep(0)
    events = [e for e in _drain_events(q) if e.get("session_key") == "cli:same"]
    starts = [e for e in events if e.get("type") == "run_start"]
    ends = [e for e in events if e.get("type") == "run_end"]

    assert len(starts) == 2
    assert len(ends) == 2

    starts_by_time = sorted(starts, key=lambda e: e.get("ts", 0))
    first_run = starts_by_time[0].get("run_id")
    second_start_ts = starts_by_time[1].get("ts", 0)
    first_end_ts = next(e.get("ts", 0) for e in ends if e.get("run_id") == first_run)

    assert second_start_ts >= first_end_ts


async def test_timeout_returns_clean_error(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(1.5, "too late")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        timeout_seconds=1,
        stream_events=True,
    )

    response = await agent.process_direct("slow", session_key="cli:slow", channel="cli", chat_id="slow")
    assert "timed out" in response.lower()

    runs = agent.list_runs(limit=5)
    assert runs
    latest = runs[0]
    assert latest["status"] == "error"
    assert "timed out" in (latest.get("error") or "").lower()


async def test_cancellation_marks_run_cancelled(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(5.0, "late")])
    q = bus.register_run_listener()
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        timeout_seconds=10,
        stream_events=True,
    )

    task = asyncio.create_task(
        agent.process_direct("cancel me", session_key="cli:cancel", channel="cli", chat_id="cancel")
    )
    await asyncio.sleep(0.1)

    active = [r for r in agent.list_runs(limit=20) if r.get("status") in ("queued", "running")]
    assert active
    run_id = active[0]["run_id"]
    assert agent.cancel_run(run_id) is True

    result = await task
    assert result == ""

    runs = agent.list_runs(limit=20)
    cancelled = next(r for r in runs if r.get("run_id") == run_id)
    assert cancelled["status"] == "cancelled"

    events = [e for e in _drain_events(q) if e.get("run_id") == run_id]
    types = [e.get("type") for e in events]
    assert "run_cancelled" in types
    if "run_cancelled" in types:
        cancelled_idx = types.index("run_cancelled")
        assert all(t in {"run_cancelled"} for t in types[cancelled_idx:])


async def test_no_reply_token_suppression_and_fallback(sandbox_home) -> None:
    bus = MessageBus()

    suppress_provider = ScriptedProvider([LLMResponse(content="NO_REPLY")])
    suppress_agent = AgentLoop(bus=bus, provider=suppress_provider, workspace=sandbox_home, stream_events=True)
    suppressed = await suppress_agent.process_direct("skip", session_key="cli:no-reply")
    assert suppressed == ""

    fallback_provider = ScriptedProvider([
        LLMResponse(content="   "),
        LLMResponse(content=""),
    ])
    fallback_agent = AgentLoop(bus=bus, provider=fallback_provider, workspace=sandbox_home, stream_events=True)
    fallback = await fallback_agent.process_direct("empty", session_key="cli:fallback")
    assert fallback == "Completed; no user-visible output."


async def test_global_queue_serializes_different_sessions(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(0.25, "a"), (0.01, "b")])
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        stream_events=True,
        queue_config=QueueConfig(global_=True, max_concurrency=1),
    )
    q = bus.register_run_listener()

    t1 = asyncio.create_task(agent.process_direct("one", session_key="cli:a", channel="cli", chat_id="a"))
    await asyncio.sleep(0.03)
    t2 = asyncio.create_task(agent.process_direct("two", session_key="cli:b", channel="cli", chat_id="b"))
    out1, out2 = await asyncio.gather(t1, t2)

    assert out1 == "a"
    assert out2 == "b"

    events = _drain_events(q)
    starts = [e for e in events if e.get("type") == "run_start"]
    ends = [e for e in events if e.get("type") == "run_end"]
    assert len(starts) == 2
    assert len(ends) == 2

    starts_by_time = sorted(starts, key=lambda e: e.get("ts", 0))
    first_run = starts_by_time[0]["run_id"]
    first_end = next(e.get("ts", 0) for e in ends if e.get("run_id") == first_run)
    second_start = starts_by_time[1].get("ts", 0)
    assert second_start >= first_end


async def test_hook_blocks_tool_use(sandbox_home) -> None:
    hooks_dir = sandbox_home / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    block_script = hooks_dir / "block.sh"
    block_script.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")

    hooks_cfg = {
        "PreToolUse": [
            {"command": "sh hooks/block.sh", "matchers": ["exec"]},
        ]
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hooks_cfg), encoding="utf-8")

    provider = ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="tc1", name="exec", arguments={"command": "echo hi"}),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    bus = MessageBus()
    q = bus.register_run_listener()
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=sandbox_home,
        stream_events=True,
        hook_config=HooksConfig(enabled=True, path="workspace/hooks"),
    )

    out = await agent.process_direct("run tool", session_key="cli:hooks", channel="cli", chat_id="hooks")
    assert out == "done"

    events = _drain_events(q)
    blocked = [e for e in events if e.get("type") == "tool_end" and e.get("blocked_by_hook")]
    assert blocked
    assert blocked[0].get("ok") is False


async def test_compaction_retry_on_overload_resets_flow(sandbox_home, monkeypatch) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(content="", finish_reason="overloaded"),
            LLMResponse(content="after retry"),
        ]
    )
    bus = MessageBus()
    q = bus.register_run_listener()
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    calls: list[str] = []

    async def fake_compact(session, run_id, reason="manual"):
        calls.append(reason)
        return True

    monkeypatch.setattr(agent, "_compact_session", fake_compact)
    out = await agent.process_direct("retry me", session_key="cli:compact", channel="cli", chat_id="compact")
    assert out == "after retry"
    assert calls == ["overloaded_retry"]

    events = [e for e in _drain_events(q) if e.get("type") == "assistant_delta"]
    assert len(events) == 1
    assert events[0].get("delta") == "after retry"


async def test_streaming_provider_emits_true_deltas(sandbox_home) -> None:
    bus = MessageBus()
    q = bus.register_run_listener()
    agent = AgentLoop(bus=bus, provider=StreamingProvider(), workspace=sandbox_home, stream_events=True)
    out = await agent.process_direct("stream", session_key="cli:stream", channel="cli", chat_id="stream")
    assert out == "Hello"

    events = [e for e in _drain_events(q) if e.get("type") == "assistant_delta"]
    assert [e.get("delta") for e in events] == ["Hel", "lo"]


async def test_run_history_persists_across_agent_instances(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([LLMResponse(content="persisted response")])
    agent1 = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)
    _ = await agent1.process_direct("persist", session_key="cli:persist", channel="cli", chat_id="persist")

    agent2 = AgentLoop(bus=MessageBus(), provider=ScriptedProvider([]), workspace=sandbox_home, stream_events=True)
    runs = agent2.list_runs(limit=20)
    assert runs
    assert any((r.get("session_key") == "cli:persist" and r.get("status") == "completed") for r in runs)


async def test_typing_start_stop_emitted_once_for_channel_run(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([LLMResponse(content="hello")])
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    runner = asyncio.create_task(agent.run())
    await bus.publish_inbound(
        InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="123",
            content="hi",
            metadata={"message_id": 42},
        )
    )

    outbound = [await asyncio.wait_for(bus.consume_outbound(), timeout=2.0) for _ in range(3)]
    controls = [m.control for m in outbound if m.control]
    normal = [m for m in outbound if not m.control]

    assert controls == ["typing_start", "typing_stop"]
    assert len(normal) == 1
    assert normal[0].content == "hello"
    assert normal[0].reply_to == "42"

    agent.stop()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass


async def test_cancel_command_can_interrupt_inflight_same_session_run(sandbox_home) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([(5.0, "too late"), LLMResponse(content="unused")])
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    slow = asyncio.create_task(
        agent.process_direct("long task", session_key="telegram:room", channel="telegram", chat_id="room")
    )
    await asyncio.sleep(0.1)

    cancel_reply = await asyncio.wait_for(
        agent.process_direct("/cancel", session_key="telegram:room", channel="telegram", chat_id="room"),
        timeout=1.0,
    )
    slow_result = await slow

    assert "Cancelled run" in cancel_reply
    assert slow_result == ""


async def test_status_reset_and_think_commands(sandbox_home) -> None:
    seen_thinking: list[str | None] = []

    def capture_step(messages, tools, model, max_tokens, temperature, thinking):
        seen_thinking.append(thinking)
        return LLMResponse(content="ok")

    bus = MessageBus()
    provider = ScriptedProvider([capture_step, capture_step])
    agent = AgentLoop(bus=bus, provider=provider, workspace=sandbox_home, stream_events=True)

    status = await agent.process_direct("/status", session_key="cli:cmd", channel="cli", chat_id="cmd")
    assert "Model:" in status
    assert "Active runs (session):" in status
    assert "Cron:" in status
    assert "Heartbeat:" in status
    assert provider.calls == 0

    think_set = await agent.process_direct("/think high", session_key="cli:cmd", channel="cli", chat_id="cmd")
    assert "Thinking mode set to high" in think_set
    assert provider.calls == 0

    normal = await agent.process_direct("hello", session_key="cli:cmd", channel="cli", chat_id="cmd")
    assert normal == "ok"
    assert provider.calls == 1
    assert seen_thinking == ["high"]

    bad_think = await agent.process_direct("/think nope", session_key="cli:cmd", channel="cli", chat_id="cmd")
    assert "Usage: /think" in bad_think
    assert provider.calls == 1

    reset = await agent.process_direct("/reset", session_key="cli:cmd", channel="cli", chat_id="cmd")
    assert reset == "Session reset."

    session = agent.sessions.get_or_create("cli:cmd")
    assert session.messages == []
