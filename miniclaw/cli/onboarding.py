"""Onboarding state/report helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_onboarding_state_path(home: Path | None = None) -> Path:
    home_dir = home or Path.home()
    return home_dir / ".miniclaw" / "onboarding-state.json"


def get_onboarding_report_path(home: Path | None = None) -> Path:
    home_dir = home or Path.home()
    return home_dir / ".miniclaw" / "onboarding-report.json"


def load_onboarding_state(home: Path | None = None) -> dict[str, Any] | None:
    path = get_onboarding_state_path(home=home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def save_onboarding_state(
    *,
    mode: str,
    step: str,
    status: str,
    data: dict[str, Any] | None = None,
    home: Path | None = None,
) -> Path:
    path = get_onboarding_state_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "mode": mode,
        "step": step,
        "status": status,
        "updated_at": _now_iso(),
        "data": data or {},
    }
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            started_at = previous.get("started_at")
            if started_at:
                payload["started_at"] = started_at
        except Exception:
            pass
    payload.setdefault("started_at", payload["updated_at"])
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def clear_onboarding_state(home: Path | None = None) -> None:
    path = get_onboarding_state_path(home=home)
    path.unlink(missing_ok=True)


def write_onboarding_report(report: dict[str, Any], home: Path | None = None) -> Path:
    path = get_onboarding_report_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "generated_at": _now_iso(),
    }
    payload.update(report)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
