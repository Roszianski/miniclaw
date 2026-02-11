"""Persistent run history store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from miniclaw.session.manager import RunState
from miniclaw.utils.helpers import ensure_dir, get_data_path


class RunStore:
    """Append-only JSONL store for run history."""

    def __init__(self, max_records: int = 5000):
        self.max_records = max(100, int(max_records))
        self.dir = ensure_dir(get_data_path() / "runs")
        self.path = self.dir / "runs.jsonl"
        self._append_since_trim = 0

    def append(self, run: RunState) -> None:
        data = run.to_dict()
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
            self._append_since_trim += 1
            if self._append_since_trim >= 100:
                self._append_since_trim = 0
                self._trim()
        except Exception as exc:
            logger.warning(f"Failed appending run record: {exc}")

    def load_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(5000, int(limit)))
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            rows: list[dict[str, Any]] = []
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
                if len(rows) >= limit:
                    break
            return rows
        except Exception as exc:
            logger.warning(f"Failed loading run history: {exc}")
            return []

    def _trim(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.max_records:
                return
            trimmed = lines[-self.max_records :]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:
            logger.debug(f"Run history trim skipped: {exc}")
