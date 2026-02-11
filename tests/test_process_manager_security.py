from pathlib import Path

import pytest

import miniclaw.processes.manager as process_manager_mod
from miniclaw.processes.manager import ProcessManager


def test_start_process_blocks_dangerous_command_patterns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_manager_mod, "get_data_path", lambda: tmp_path / ".miniclaw-data")
    manager = ProcessManager(workspace=tmp_path, restrict_to_workspace=True)
    with pytest.raises(ValueError, match="dangerous pattern"):
        manager.start_process("rm -rf /tmp/miniclaw-danger")


def test_start_process_restricts_working_directory_to_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_manager_mod, "get_data_path", lambda: tmp_path / ".miniclaw-data")
    manager = ProcessManager(workspace=tmp_path, restrict_to_workspace=True)
    with pytest.raises(ValueError, match="inside workspace"):
        manager.start_process("echo ok", cwd="/tmp")
