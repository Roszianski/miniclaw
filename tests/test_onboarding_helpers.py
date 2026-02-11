import json
from pathlib import Path

from miniclaw.cli.onboarding import (
    clear_onboarding_state,
    get_onboarding_report_path,
    get_onboarding_state_path,
    load_onboarding_state,
    save_onboarding_state,
    write_onboarding_report,
)


def test_onboarding_state_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    path = save_onboarding_state(
        mode="guided",
        step="channels",
        status="in_progress",
        data={"host": "macos_local"},
    )
    assert path == get_onboarding_state_path()

    loaded = load_onboarding_state()
    assert loaded is not None
    assert loaded["mode"] == "guided"
    assert loaded["step"] == "channels"
    assert loaded["status"] == "in_progress"
    assert loaded["data"]["host"] == "macos_local"

    clear_onboarding_state()
    assert load_onboarding_state() is None


def test_onboarding_report_written_with_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    path = write_onboarding_report({"mode": "guided", "checks": []})
    assert path == get_onboarding_report_path()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["mode"] == "guided"
    assert isinstance(data["generated_at"], str)
