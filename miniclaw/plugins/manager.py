"""Plugin manager for local/git plugin distribution."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class PluginValidationError(ValueError):
    """Raised when a plugin fails policy or manifest validation."""


class PluginManager:
    """Install/list/remove plugins with manifest and permission checks."""

    MANIFEST_FILES = ("plugin.yaml", "plugin.yml", "plugin.json")

    def __init__(self, *, workspace: Path, config: Any):
        self.workspace = workspace
        self.config = config
        self.plugins_dir = workspace / "plugins"

    def list_plugins(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.plugins_dir.exists():
            return rows
        for path in sorted(self.plugins_dir.iterdir()):
            if not path.is_dir():
                continue
            manifest_path = self._find_manifest(path)
            manifest = self._load_manifest(manifest_path) if manifest_path else {}
            rows.append(
                {
                    "name": manifest.get("name") or path.name,
                    "path": str(path),
                    "manifest": manifest,
                    "permissions": self._extract_permissions(manifest),
                }
            )
        return rows

    def install(self, source: str, *, name: str | None = None) -> dict[str, Any]:
        source = str(source or "").strip()
        if not source:
            raise PluginValidationError("source is required")

        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="miniclaw-plugin-") as tmp:
            temp_root = Path(tmp)
            staged = self._stage_source(source=source, temp_root=temp_root)
            manifest_path = self._find_manifest(staged)

            if bool(getattr(self.config, "manifest_required", True)) and not manifest_path:
                raise PluginValidationError("plugin manifest is required (plugin.yaml/yml/json).")

            manifest = self._load_manifest(manifest_path) if manifest_path else {}
            plugin_name = str(name or manifest.get("name") or staged.name).strip()
            if not plugin_name:
                raise PluginValidationError("plugin name is required")

            permissions = self._extract_permissions(manifest)
            self._validate_signature_policy(staged, manifest)

            target = self.plugins_dir / plugin_name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(staged, target)

            return {
                "name": plugin_name,
                "path": str(target),
                "manifest": manifest,
                "permissions": permissions,
            }

    def remove(self, name: str) -> bool:
        target = self.plugins_dir / str(name)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    def _stage_source(self, *, source: str, temp_root: Path) -> Path:
        maybe_local = Path(source).expanduser()
        if maybe_local.exists():
            if not bool(getattr(self.config, "allow_local", True)):
                raise PluginValidationError("Local plugin install is disabled by policy.")
            staged = temp_root / maybe_local.name
            shutil.copytree(maybe_local, staged)
            return staged

        if source.startswith("http://") or source.startswith("https://") or source.endswith(".git"):
            if not bool(getattr(self.config, "allow_git", True)):
                raise PluginValidationError("Git plugin install is disabled by policy.")
            staged = temp_root / "git"
            subprocess.run(
                ["git", "clone", source, str(staged)],
                check=True,
                capture_output=True,
                text=True,
            )
            git_meta = staged / ".git"
            if git_meta.exists():
                shutil.rmtree(git_meta, ignore_errors=True)
            return staged

        raise PluginValidationError("source must be a local path or git URL.")

    def _find_manifest(self, plugin_root: Path) -> Path | None:
        for filename in self.MANIFEST_FILES:
            candidate = plugin_root / filename
            if candidate.exists():
                return candidate
        return None

    def _load_manifest(self, path: Path | None) -> dict[str, Any]:
        if path is None or not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            data = self._load_yaml_like(raw)
        if not isinstance(data, dict):
            raise PluginValidationError(f"Plugin manifest must be an object: {path}")
        return data

    @staticmethod
    def _load_yaml_like(raw: str) -> dict[str, Any]:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        # Tiny fallback parser for simple "key: value" manifests.
        out: dict[str, Any] = {}
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            out[key.strip()] = value.strip().strip("'\"")
        return out

    @staticmethod
    def _extract_permissions(manifest: dict[str, Any]) -> list[str]:
        permissions = manifest.get("permissions", [])
        if isinstance(permissions, list):
            return [str(p) for p in permissions if str(p).strip()]
        if isinstance(permissions, dict):
            return [f"{key}:{value}" for key, value in permissions.items()]
        return []

    def _validate_signature_policy(self, plugin_root: Path, manifest: dict[str, Any]) -> None:
        mode = str(getattr(self.config, "signature_mode", "optional") or "optional").strip().lower()
        has_signature = bool(manifest.get("signature")) or (plugin_root / "plugin.sig").exists()
        if mode == "required" and not has_signature:
            raise PluginValidationError("Plugin signature is required by policy.")
