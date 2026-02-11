"""Token bucket rate limiter."""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows.
    fcntl = None


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    capacity: float
    rate: float  # tokens per second

    def consume(self, now: float) -> bool:
        """Try to consume one token. Returns True if allowed."""
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    @classmethod
    def from_row(cls, row: Any, *, capacity: float, rate: float, now: float) -> "_Bucket":
        if not isinstance(row, dict):
            return cls(tokens=capacity, last_refill=now, capacity=capacity, rate=rate)
        try:
            tokens = float(row.get("tokens", capacity))
        except Exception:
            tokens = capacity
        try:
            last_refill = float(row.get("last_refill", now))
        except Exception:
            last_refill = now
        tokens = max(0.0, min(capacity, tokens))
        if last_refill <= 0:
            last_refill = now
        return cls(tokens=tokens, last_refill=last_refill, capacity=capacity, rate=rate)

    def to_row(self) -> dict[str, float]:
        return {
            "tokens": float(self.tokens),
            "last_refill": float(self.last_refill),
        }


@dataclass
class RateLimiter:
    """Per-user and per-tool token bucket rate limiter."""

    messages_per_minute: int = 20
    tool_calls_per_minute: int = 60
    store_path: Path | str | None = None
    _user_buckets: dict[str, _Bucket] = field(default_factory=dict)
    _tool_buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _lock_path: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.messages_per_minute = max(1, int(self.messages_per_minute))
        self.tool_calls_per_minute = max(1, int(self.tool_calls_per_minute))
        if self.store_path is not None:
            path = Path(self.store_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path = path
            self._lock_path = path.with_suffix(f"{path.suffix}.lock")
        else:
            self.store_path = None

    def _bucket_params(self, *, tool: bool) -> tuple[float, float]:
        cap = float(self.tool_calls_per_minute if tool else self.messages_per_minute)
        return cap, cap / 60.0

    def _get_user_bucket(self, user_key: str) -> _Bucket:
        if user_key not in self._user_buckets:
            cap, rate = self._bucket_params(tool=False)
            self._user_buckets[user_key] = _Bucket(
                tokens=cap,
                last_refill=time.time(),
                capacity=cap,
                rate=rate,
            )
        return self._user_buckets[user_key]

    def _get_tool_bucket(self, user_key: str) -> _Bucket:
        if user_key not in self._tool_buckets:
            cap, rate = self._bucket_params(tool=True)
            self._tool_buckets[user_key] = _Bucket(
                tokens=cap,
                last_refill=time.time(),
                capacity=cap,
                rate=rate,
            )
        return self._tool_buckets[user_key]

    def _load_state(self) -> dict[str, Any]:
        if self.store_path is None or not self.store_path.exists():
            return {"user_buckets": {}, "tool_buckets": {}}
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception:
            return {"user_buckets": {}, "tool_buckets": {}}
        if not isinstance(raw, dict):
            return {"user_buckets": {}, "tool_buckets": {}}
        users = raw.get("user_buckets")
        tools = raw.get("tool_buckets")
        return {
            "user_buckets": dict(users) if isinstance(users, dict) else {},
            "tool_buckets": dict(tools) if isinstance(tools, dict) else {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        if self.store_path is None:
            return
        payload = json.dumps(
            {
                "version": 1,
                "updated_at": int(time.time()),
                "user_buckets": state.get("user_buckets", {}),
                "tool_buckets": state.get("tool_buckets", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.store_path.parent),
            prefix=f"{self.store_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.store_path)

    @contextmanager
    def _file_lock(self):
        if self.store_path is None or self._lock_path is None:
            yield
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            handle.close()

    @staticmethod
    def _prune_rows(rows: dict[str, Any], *, now: float, max_idle_seconds: float = 3600.0) -> None:
        stale: list[str] = []
        for key, row in rows.items():
            if not isinstance(row, dict):
                stale.append(key)
                continue
            try:
                last_refill = float(row.get("last_refill", 0))
            except Exception:
                stale.append(key)
                continue
            if now - last_refill > max_idle_seconds:
                stale.append(key)
        for key in stale:
            rows.pop(key, None)

    def _consume_persistent(self, *, user_key: str, tool: bool) -> bool:
        if self.store_path is None:
            raise RuntimeError("Persistent store path is not configured.")
        now = time.time()
        bucket_map_key = "tool_buckets" if tool else "user_buckets"
        cap, rate = self._bucket_params(tool=tool)

        with self._lock:
            with self._file_lock():
                state = self._load_state()
                bucket_map = state.setdefault(bucket_map_key, {})
                row = bucket_map.get(user_key)
                bucket = _Bucket.from_row(row, capacity=cap, rate=rate, now=now)
                allowed = bucket.consume(now)
                bucket_map[user_key] = bucket.to_row()
                self._prune_rows(bucket_map, now=now)
                self._save_state(state)
                return allowed

    def check_message(self, user_key: str) -> bool:
        """Check if user can send a message. Returns True if allowed."""
        key = str(user_key or "")
        if self.store_path is None:
            with self._lock:
                return self._get_user_bucket(key).consume(time.time())
        return self._consume_persistent(user_key=key, tool=False)

    def check_tool_call(self, user_key: str) -> bool:
        """Check if user can make a tool call. Returns True if allowed."""
        key = str(user_key or "")
        if self.store_path is None:
            with self._lock:
                return self._get_tool_bucket(key).consume(time.time())
        return self._consume_persistent(user_key=key, tool=True)
