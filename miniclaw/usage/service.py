"""Usage and cost aggregation service."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class UsageTracker:
    """Append-only token usage tracker with aggregation helpers."""

    def __init__(
        self,
        *,
        store_path: Path,
        pricing: dict[str, Any] | None = None,
        aggregation_windows: list[str] | None = None,
    ):
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._pricing = self._normalize_pricing(pricing or {})
        self._aggregation_windows = [
            item
            for item in (aggregation_windows or ["1h", "1d", "30d"])
            if self._window_seconds(item) is not None
        ]
        self._lock = threading.Lock()

    @property
    def aggregation_windows(self) -> list[str]:
        return list(self._aggregation_windows)

    def record(
        self,
        *,
        source: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        run_id: str = "",
        session_key: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one usage event."""
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        total = max(0, int(total_tokens or 0))
        if total <= 0:
            total = prompt + completion

        resolved_model = str(model or "").strip() or "unknown"
        ts_ms = int(time.time() * 1000)
        cost = self._estimate_cost_usd(resolved_model, prompt, completion)
        event = {
            "ts_ms": ts_ms,
            "source": str(source or "unknown"),
            "model": resolved_model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cost_usd": cost,
            "run_id": str(run_id or ""),
            "session_key": str(session_key or ""),
            "user_id": str(user_id or ""),
            "metadata": dict(metadata or {}),
        }
        self._append(event)
        return event

    def summary(
        self,
        *,
        windows: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return overall and windowed usage summary."""
        now_ms = int(time.time() * 1000)
        selected_windows = [
            item for item in (windows or self._aggregation_windows)
            if self._window_seconds(item) is not None
        ]

        all_events = self._load_events()
        out = {
            "generated_at_ms": now_ms,
            "overall": self._aggregate_events(all_events, window="all", start_ms=None, end_ms=now_ms),
            "windows": {},
            "pricing_models": sorted(self._pricing.keys()),
        }
        for window in selected_windows:
            seconds = self._window_seconds(window)
            if seconds is None:
                continue
            start_ms = now_ms - (seconds * 1000)
            scoped = [event for event in all_events if int(event.get("ts_ms") or 0) >= start_ms]
            out["windows"][window] = self._aggregate_events(
                scoped,
                window=window,
                start_ms=start_ms,
                end_ms=now_ms,
            )
        return out

    def purge(
        self,
        *,
        session_key: str | None = None,
        user_id: str | None = None,
        before_ts_ms: int | None = None,
    ) -> int:
        """Delete matching usage events from the ledger."""
        if not self.store_path.exists():
            return 0

        session_filter = str(session_key or "").strip()
        user_filter = str(user_id or "").strip()
        cutoff = int(before_ts_ms) if before_ts_ms is not None else None

        with self._lock:
            lines = self.store_path.read_text(encoding="utf-8").splitlines()
            kept: list[str] = []
            removed = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if self._matches_purge_filter(
                    obj=obj,
                    session_key=session_filter,
                    user_id=user_filter,
                    before_ts_ms=cutoff,
                ):
                    removed += 1
                    continue
                kept.append(line)
            self._rewrite_lines(kept)
        return removed

    def _matches_purge_filter(
        self,
        *,
        obj: dict[str, Any],
        session_key: str,
        user_id: str,
        before_ts_ms: int | None,
    ) -> bool:
        ts_ms = int(obj.get("ts_ms") or 0)
        event_session = str(obj.get("session_key") or "")
        event_user = str(obj.get("user_id") or "")

        if session_key and event_session != session_key:
            return False
        if user_id:
            if event_user == user_id:
                pass
            elif user_id in event_session:
                pass
            else:
                return False
        if before_ts_ms is not None and ts_ms >= before_ts_ms:
            return False
        return bool(session_key or user_id or before_ts_ms is not None)

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            with open(self.store_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _load_events(self) -> list[dict[str, Any]]:
        if not self.store_path.exists():
            return []
        with self._lock:
            lines = self.store_path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        return events

    def _rewrite_lines(self, lines: list[str]) -> None:
        if lines:
            self.store_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            self.store_path.write_text("", encoding="utf-8")

    def _aggregate_events(
        self,
        events: list[dict[str, Any]],
        *,
        window: str,
        start_ms: int | None,
        end_ms: int,
    ) -> dict[str, Any]:
        totals = {
            "events": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
        by_model: dict[str, dict[str, Any]] = {}

        for event in events:
            model = str(event.get("model") or "unknown")
            prompt = int(event.get("prompt_tokens") or 0)
            completion = int(event.get("completion_tokens") or 0)
            total = int(event.get("total_tokens") or 0)
            if total <= 0:
                total = prompt + completion
            cost = float(event.get("cost_usd") or 0.0)

            totals["events"] += 1
            totals["prompt_tokens"] += prompt
            totals["completion_tokens"] += completion
            totals["total_tokens"] += total
            totals["cost_usd"] += cost

            row = by_model.setdefault(
                model,
                {
                    "model": model,
                    "events": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            row["events"] += 1
            row["prompt_tokens"] += prompt
            row["completion_tokens"] += completion
            row["total_tokens"] += total
            row["cost_usd"] += cost

        totals["cost_usd"] = round(float(totals["cost_usd"]), 8)
        models = sorted(by_model.values(), key=lambda item: item["cost_usd"], reverse=True)
        for row in models:
            row["cost_usd"] = round(float(row["cost_usd"]), 8)
        return {
            "window": window,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "totals": totals,
            "models": models,
        }

    @staticmethod
    def _window_seconds(window: str) -> int | None:
        raw = str(window or "").strip().lower()
        if not raw:
            return None
        unit = raw[-1]
        number = raw[:-1]
        if not number.isdigit():
            return None
        amount = int(number)
        if amount <= 0:
            return None
        if unit == "h":
            return amount * 3600
        if unit == "d":
            return amount * 86400
        if unit == "m":
            return amount * 60
        return None

    def _estimate_cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        price = self._find_price(model)
        if not price:
            return 0.0
        input_rate = float(price.get("input_per_1m_tokens_usd") or 0.0)
        output_rate = float(price.get("output_per_1m_tokens_usd") or 0.0)
        return round(((prompt_tokens / 1_000_000.0) * input_rate) + ((completion_tokens / 1_000_000.0) * output_rate), 8)

    def _find_price(self, model: str) -> dict[str, float] | None:
        raw = str(model or "").strip()
        if not raw:
            return None
        direct = self._pricing.get(raw)
        if direct:
            return direct

        lower = raw.lower()
        for key, value in self._pricing.items():
            if key.lower() == lower:
                return value

        if "/" in raw:
            suffix = raw.split("/", 1)[1]
            if suffix in self._pricing:
                return self._pricing[suffix]

        # Prefix fallback: "anthropic/" should match "anthropic/claude..."
        prefix_matches = [
            (key, value)
            for key, value in self._pricing.items()
            if key.endswith("/") and raw.startswith(key)
        ]
        if prefix_matches:
            prefix_matches.sort(key=lambda item: len(item[0]), reverse=True)
            return prefix_matches[0][1]
        return None

    @staticmethod
    def _normalize_pricing(pricing: dict[str, Any]) -> dict[str, dict[str, float]]:
        normalized: dict[str, dict[str, float]] = {}
        for model, value in pricing.items():
            key = str(model or "").strip()
            if not key:
                continue
            if isinstance(value, dict):
                input_rate = float(value.get("input_per_1m_tokens_usd") or 0.0)
                output_rate = float(value.get("output_per_1m_tokens_usd") or 0.0)
            else:
                input_rate = float(getattr(value, "input_per_1m_tokens_usd", 0.0) or 0.0)
                output_rate = float(getattr(value, "output_per_1m_tokens_usd", 0.0) or 0.0)
            normalized[key] = {
                "input_per_1m_tokens_usd": input_rate,
                "output_per_1m_tokens_usd": output_rate,
            }
        return normalized
