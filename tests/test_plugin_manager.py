import json
from pathlib import Path

import pytest

from miniclaw.config.schema import Config
from miniclaw.plugins.manager import PluginManager, PluginValidationError


def test_plugin_install_and_list_from_local_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    source = tmp_path / "plugin-src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "permissions": ["filesystem:read"]}),
        encoding="utf-8",
    )

    config = Config()
    manager = PluginManager(workspace=workspace, config=config.plugins)
    installed = manager.install(str(source))

    assert installed["name"] == "demo-plugin"
    listed = manager.list_plugins()
    assert listed[0]["name"] == "demo-plugin"
    assert listed[0]["permissions"] == ["filesystem:read"]


def test_plugin_policy_enforcement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    source = tmp_path / "plugin-src"
    source.mkdir(parents=True, exist_ok=True)

    config = Config()
    config.plugins.allow_local = False
    manager = PluginManager(workspace=workspace, config=config.plugins)
    with pytest.raises(PluginValidationError):
        manager.install(str(source))

    config.plugins.allow_local = True
    config.plugins.manifest_required = True
    manager = PluginManager(workspace=workspace, config=config.plugins)
    with pytest.raises(PluginValidationError):
        manager.install(str(source))

    (source / "plugin.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    config.plugins.signature_mode = "required"
    manager = PluginManager(workspace=workspace, config=config.plugins)
    with pytest.raises(PluginValidationError):
        manager.install(str(source))


def test_plugin_name_rejects_path_traversal_and_invalid_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "plugin-src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

    config = Config()
    manager = PluginManager(workspace=workspace, config=config.plugins)

    with pytest.raises(PluginValidationError):
        manager.install(str(source), name="..")
    with pytest.raises(PluginValidationError):
        manager.install(str(source), name="../evil")

    outside = workspace / "keep.txt"
    outside.write_text("safe", encoding="utf-8")
    with pytest.raises(PluginValidationError):
        manager.remove("..")
    assert outside.exists()


def test_plugin_manifest_name_cannot_escape_plugins_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    source = tmp_path / "plugin-src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(
        json.dumps({"name": "../escape", "permissions": []}),
        encoding="utf-8",
    )

    config = Config()
    manager = PluginManager(workspace=workspace, config=config.plugins)
    with pytest.raises(PluginValidationError):
        manager.install(str(source))
