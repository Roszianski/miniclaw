from pathlib import Path

from miniclaw.ratelimit.limiter import RateLimiter


def test_rate_limiter_shared_store_blocks_across_instances(tmp_path: Path) -> None:
    store = tmp_path / "ratelimit" / "state.json"
    limiter_a = RateLimiter(messages_per_minute=1, tool_calls_per_minute=1, store_path=store)
    limiter_b = RateLimiter(messages_per_minute=1, tool_calls_per_minute=1, store_path=store)

    assert limiter_a.check_message("user-1") is True
    assert limiter_b.check_message("user-1") is False


def test_rate_limiter_persists_state_across_restart(tmp_path: Path) -> None:
    store = tmp_path / "ratelimit" / "state.json"
    first = RateLimiter(messages_per_minute=1, tool_calls_per_minute=1, store_path=store)
    assert first.check_tool_call("user-2") is True
    assert first.check_tool_call("user-2") is False

    restarted = RateLimiter(messages_per_minute=1, tool_calls_per_minute=1, store_path=store)
    assert restarted.check_tool_call("user-2") is False
