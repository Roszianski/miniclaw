"""Tests for BOOTSTRAP.md onboarding and workspace file API."""

import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from miniclaw.agent.context import ContextBuilder
from miniclaw.config.schema import Config
from miniclaw.dashboard.app import create_app


# === Part 1: BOOTSTRAP.md Context Loading ===


def test_bootstrap_loads_as_priority_context() -> None:
    """BOOTSTRAP.md in workspace causes it to load as priority context."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        bootstrap = workspace / "BOOTSTRAP.md"
        bootstrap.write_text("# Onboarding instructions here")
        soul = workspace / "SOUL.md"
        soul.write_text("# Soul content")

        cb = ContextBuilder(workspace)
        prompt = cb.build_system_prompt()

        # BOOTSTRAP.md content should appear before SOUL.md content
        bootstrap_pos = prompt.find("Onboarding instructions here")
        soul_pos = prompt.find("Soul content")
        assert bootstrap_pos != -1, "BOOTSTRAP.md content should be in the prompt"
        assert soul_pos != -1, "SOUL.md content should be in the prompt"
        assert bootstrap_pos < soul_pos, "BOOTSTRAP.md should appear before SOUL.md"


def test_bootstrap_absent_has_no_effect() -> None:
    """BOOTSTRAP.md absent has no effect on existing behavior."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        soul = workspace / "SOUL.md"
        soul.write_text("# Soul content")

        cb = ContextBuilder(workspace)
        prompt = cb.build_system_prompt()

        assert "BOOTSTRAP.md" not in prompt
        assert "Soul content" in prompt


def test_onboard_copies_bootstrap_template() -> None:
    """Onboard command copies BOOTSTRAP.md template to workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)

        # Import the function
        from miniclaw.cli.commands import _create_workspace_templates

        # Suppress Rich console output during test
        from unittest.mock import MagicMock
        import miniclaw.cli.commands as cmd_mod
        original_console = cmd_mod.console
        cmd_mod.console = MagicMock()
        try:
            _create_workspace_templates(workspace)
        finally:
            cmd_mod.console = original_console

        bootstrap = workspace / "BOOTSTRAP.md"
        assert bootstrap.exists(), "BOOTSTRAP.md should be created during onboard"

        content = bootstrap.read_text()
        assert "First-Run Onboarding" in content


# === Part 2: Workspace File API ===


def _make_app(workspace: Path):
    """Create a test app with a real workspace directory."""
    config = Config(agents={"defaults": {"workspace": str(workspace)}})
    return create_app(
        config=config,
        config_path=Path("/tmp/claude/miniclaw-test-config.json"),
        token="t",
    )


def test_workspace_get_returns_file_content() -> None:
    """GET /api/workspace/SOUL.md returns file content."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        soul = workspace / "SOUL.md"
        soul.write_text("I am the soul")

        app = _make_app(workspace)
        client = TestClient(app)
        headers = {"Authorization": "Bearer t"}

        res = client.get("/api/workspace/SOUL.md", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "SOUL.md"
        assert data["content"] == "I am the soul"


def test_workspace_put_saves_and_reads_back() -> None:
    """PUT /api/workspace/SOUL.md saves content and can be re-read."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)

        app = _make_app(workspace)
        client = TestClient(app)
        headers = {"Authorization": "Bearer t"}

        # Write
        res = client.put(
            "/api/workspace/SOUL.md",
            headers=headers,
            json={"content": "Updated soul content"},
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True

        # Read back
        res = client.get("/api/workspace/SOUL.md", headers=headers)
        assert res.status_code == 200
        assert res.json()["content"] == "Updated soul content"


def test_workspace_whitelist_enforced() -> None:
    """GET /api/workspace/evil.md returns 404 (whitelist enforced)."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        # Even if the file exists on disk, it should be rejected
        evil = workspace / "evil.md"
        evil.write_text("malicious content")

        app = _make_app(workspace)
        client = TestClient(app)
        headers = {"Authorization": "Bearer t"}

        res = client.get("/api/workspace/evil.md", headers=headers)
        assert res.status_code == 404

        res = client.put(
            "/api/workspace/evil.md",
            headers=headers,
            json={"content": "nope"},
        )
        assert res.status_code == 404


def test_workspace_list_returns_all_whitelisted_files() -> None:
    """GET /api/workspace lists all whitelisted files with exists status."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "SOUL.md").write_text("soul")
        (workspace / "USER.md").write_text("user")

        app = _make_app(workspace)
        client = TestClient(app)
        headers = {"Authorization": "Bearer t"}

        res = client.get("/api/workspace", headers=headers)
        assert res.status_code == 200
        data = res.json()
        names = {f["name"] for f in data}
        assert "SOUL.md" in names
        assert "USER.md" in names
        assert "AGENTS.md" in names
        assert "HEARTBEAT.md" in names
        assert "MEMORY.md" not in names

        # Check exists flags
        soul = next(f for f in data if f["name"] == "SOUL.md")
        assert soul["exists"] is True
        agents = next(f for f in data if f["name"] == "AGENTS.md")
        assert agents["exists"] is False


def test_workspace_get_missing_file_returns_empty() -> None:
    """GET for a whitelisted but nonexistent file returns empty content."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)

        app = _make_app(workspace)
        client = TestClient(app)
        headers = {"Authorization": "Bearer t"}

        res = client.get("/api/workspace/AGENTS.md", headers=headers)
        assert res.status_code == 200
        assert res.json()["content"] == ""
