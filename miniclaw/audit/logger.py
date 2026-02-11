"""Structured audit logging to JSON lines file."""

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from loguru import logger


class AuditLogger:
    """Structured JSON-lines audit logger."""

    _SENSITIVE_KEY_RE = re.compile(
        r"(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|authorization|bearer)",
        re.IGNORECASE,
    )
    _INLINE_SECRET_PATTERNS = (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"),
        re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*(bearer\s+[^\s,;]+)"),
        re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{12,}"),
    )

    def __init__(
        self,
        log_path: Path,
        level: Literal["minimal", "standard", "verbose"] = "standard",
    ):
        self.log_path = log_path
        self.level = level
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: dict[str, Any]) -> None:
        entry["ts"] = time.time()
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"Audit write failed: {e}")

    @staticmethod
    def _looks_binary_string(value: str) -> bool:
        lower = value.lower()
        if lower.startswith("data:image/") or lower.startswith("data:application/octet-stream"):
            return True
        if "base64," in lower and len(value) > 120:
            return True
        if len(value) > 800 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
            return True
        return False

    @classmethod
    def _sanitize(cls, value: Any, max_len: int = 500) -> Any:
        if isinstance(value, bytes):
            return "<redacted:binary-bytes>"
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if cls._SENSITIVE_KEY_RE.search(key):
                    out[key] = "<redacted:sensitive>"
                else:
                    out[key] = cls._sanitize(v, max_len=max_len)
            return out
        if isinstance(value, list):
            return [cls._sanitize(v, max_len=max_len) for v in value]
        if isinstance(value, str):
            if cls._looks_binary_string(value):
                return "<redacted:binary-payload>"
            value = cls._mask_sensitive_text(value)
            if len(value) > max_len:
                return value[:max_len] + f"... (truncated, {len(value) - max_len} more chars)"
            return value
        return value

    @classmethod
    def _mask_sensitive_text(cls, text: str) -> str:
        out = text
        for pattern in cls._INLINE_SECRET_PATTERNS:
            if "authorization" in pattern.pattern.lower():
                out = pattern.sub(lambda m: f"{m.group(1)}=<redacted:sensitive>", out)
            elif "bearer" in pattern.pattern.lower() and "authorization" not in pattern.pattern.lower():
                out = pattern.sub("Bearer <redacted:sensitive>", out)
            else:
                out = pattern.sub(lambda m: f"{m.group(1)}=<redacted:sensitive>", out)
        return out

    def log_tool(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        result: str | None = None,
        duration_ms: float = 0,
        success: bool = True,
    ) -> None:
        """Log a tool execution."""
        entry: dict[str, Any] = {
            "type": "tool",
            "tool": tool_name,
            "ok": success,
            "ms": round(duration_ms, 1),
        }
        if self.level in ("standard", "verbose") and params:
            entry["params"] = self._sanitize(params, max_len=300)
        if self.level == "verbose" and result:
            entry["result"] = self._sanitize(result, max_len=800)
        self._write(entry)

    def log_message(
        self,
        direction: Literal["inbound", "outbound"],
        channel: str,
        length: int,
        sender: str | None = None,
    ) -> None:
        """Log a message event."""
        entry: dict[str, Any] = {
            "type": "message",
            "dir": direction,
            "channel": channel,
            "len": length,
        }
        if self.level in ("standard", "verbose") and sender:
            entry["sender"] = sender
        self._write(entry)

    def log_event(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Log a generic event."""
        entry: dict[str, Any] = {"type": "event", "event": event}
        if data:
            entry["data"] = data
        self._write(entry)
