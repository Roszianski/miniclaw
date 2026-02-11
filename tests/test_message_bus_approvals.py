import asyncio

from miniclaw.bus.events import InboundMessage
from miniclaw.bus.queue import MessageBus


async def test_wait_for_response_supports_parallel_approval_waiters() -> None:
    bus = MessageBus()
    wait_a = asyncio.create_task(
        bus.wait_for_response("telegram:42", timeout=1.0, approval_id="approval-a")
    )
    wait_b = asyncio.create_task(
        bus.wait_for_response("telegram:42", timeout=1.0, approval_id="approval-b")
    )
    await asyncio.sleep(0)

    assert bus.submit_response("telegram:42", "approve", approval_id="approval-a") is True
    assert bus.submit_response("telegram:42", "deny", approval_id="approval-b") is True

    assert await wait_a == "approve"
    assert await wait_b == "deny"


async def test_submit_response_unknown_approval_id_does_not_consume_waiter() -> None:
    bus = MessageBus()
    waiter = asyncio.create_task(
        bus.wait_for_response("telegram:99", timeout=1.0, approval_id="approval-known")
    )
    await asyncio.sleep(0)

    assert bus.submit_response("telegram:99", "approve", approval_id="approval-missing") is False
    assert bus.submit_response("telegram:99", "approve", approval_id="approval-known") is True
    assert await waiter == "approve"


async def test_publish_inbound_resolves_oldest_waiter_fifo() -> None:
    bus = MessageBus()
    first = asyncio.create_task(bus.wait_for_response("telegram:7", timeout=1.0))
    second = asyncio.create_task(bus.wait_for_response("telegram:7", timeout=1.0))
    await asyncio.sleep(0)

    await bus.publish_inbound(
        InboundMessage(channel="telegram", sender_id="u1", chat_id="7", content="first")
    )
    await bus.publish_inbound(
        InboundMessage(channel="telegram", sender_id="u1", chat_id="7", content="second")
    )

    assert await first == "first"
    assert await second == "second"
