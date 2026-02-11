"""Skills loader for agent capabilities."""

import asyncio
import json
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.
    
    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """
    
    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, secret_store: Any | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.secret_store = secret_store
        self._content_cache: dict[str, str | None] = {}
        self._metadata_cache: dict[str, dict | None] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._watch_running = False
        self._watch_interval_s = 1.0
        self._last_signature: tuple[tuple[str, int, int], ...] = self._snapshot_signature()

    def invalidate_cache(self) -> None:
        """Clear skill content/metadata caches."""
        self._content_cache.clear()
        self._metadata_cache.clear()

    def start_hot_reload(self, poll_interval_s: float = 1.0) -> None:
        """Start background watcher to invalidate caches on filesystem changes."""
        if self._watch_task and not self._watch_task.done():
            return
        self._watch_interval_s = max(0.2, float(poll_interval_s))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watch_running = True
        self._watch_task = loop.create_task(self._watch_loop())

    def stop_hot_reload(self) -> None:
        """Stop background watcher."""
        self._watch_running = False
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

    def _snapshot_signature(self) -> tuple[tuple[str, int, int], ...]:
        rows: list[tuple[str, int, int]] = []
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)

        for root in roots:
            if not root.exists():
                continue
            for skill_dir in root.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue
                try:
                    st = skill_file.stat()
                    rows.append((f"{root}:{skill_dir.name}", int(st.st_mtime_ns), int(st.st_size)))
                except Exception:
                    continue
        rows.sort(key=lambda item: item[0])
        return tuple(rows)

    async def _watch_loop(self) -> None:
        while self._watch_running:
            try:
                sig = self._snapshot_signature()
                if sig != self._last_signature:
                    self._last_signature = sig
                    self.invalidate_cache()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Skills hot-reload watcher error: {exc}")
            await asyncio.sleep(self._watch_interval_s)
    
    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, Any]]:
        """
        List all available skills.
        
        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.
        
        Returns:
            List of skill info dicts.
        """
        skills: list[dict[str, Any]] = []
        
        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})
        
        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        enriched: list[dict[str, Any]] = []
        for s in skills:
            skill_meta = self._get_skill_meta(s["name"])
            requires = self._normalize_requires(skill_meta)
            missing = self._get_missing_requirements(skill_meta, skill_name=s["name"])
            item = dict(s)
            item["requires"] = requires
            item["missing"] = missing
            item["available"] = len(missing) == 0
            item["secret_requirements"] = self.get_secret_requirement_status(s["name"])
            enriched.append(item)

        if filter_unavailable:
            return [s for s in enriched if bool(s.get("available"))]
        return enriched
    
    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.
        
        Args:
            name: Skill name (directory name).
        
        Returns:
            Skill content or None if not found.
        """
        if name in self._content_cache:
            return self._content_cache[name]

        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            content = workspace_skill.read_text(encoding="utf-8")
            self._content_cache[name] = content
            return content
        
        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                content = builtin_skill.read_text(encoding="utf-8")
                self._content_cache[name] = content
                return content

        self._content_cache[name] = None
        return None
    
    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.
        
        Args:
            skill_names: List of skill names to load.
        
        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        
        return "\n\n---\n\n".join(parts) if parts else ""
    
    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).
        
        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.
        
        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""
        
        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        
        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta, skill_name=s["name"])
            
            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")
            
            # Show missing requirements for unavailable skills
            if not available:
                missing = ", ".join(self._get_missing_requirements(skill_meta, skill_name=s["name"]))
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")
            
            lines.append("  </skill>")
        lines.append("</skills>")
        
        return "\n".join(lines)
    
    def _get_missing_requirements(self, skill_meta: dict, skill_name: str) -> list[str]:
        """Get missing dependency markers for a skill."""
        missing: list[str] = []
        requires = self._normalize_requires(skill_meta)
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI:{b}")
        for env_name in requires.get("env", []):
            if not self._has_secret_or_env(skill_name=skill_name, env_name=env_name):
                missing.append(f"ENV:{env_name}")
        return missing
    
    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name
    
    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content
    
    def _parse_metadata(self, raw: str) -> dict:
        """Parse metadata JSON from frontmatter."""
        try:
            data = json.loads(raw)
            return data.get("miniclaw", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    
    def _check_requirements(self, skill_meta: dict, skill_name: str = "") -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = self._normalize_requires(skill_meta)
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env_name in requires.get("env", []):
            if not self._has_secret_or_env(skill_name=skill_name, env_name=env_name):
                return False
        return True
    
    def _get_skill_meta(self, name: str) -> dict:
        """Get metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_metadata(meta.get("metadata", ""))

    @staticmethod
    def secret_key_for(skill_name: str, env_name: str) -> str:
        return f"skill:{skill_name}:env:{env_name}"

    def _has_secret_or_env(self, *, skill_name: str, env_name: str) -> bool:
        if os.environ.get(env_name):
            return True
        if self.secret_store:
            try:
                key = self.secret_key_for(skill_name, env_name)
                return self.secret_store.has(key)
            except Exception:
                return False
        return False

    @staticmethod
    def _normalize_requires(skill_meta: dict) -> dict[str, list[str]]:
        requires = skill_meta.get("requires", {}) if isinstance(skill_meta, dict) else {}
        bins = requires.get("bins", [])
        envs = requires.get("env", [])
        if isinstance(bins, str):
            bins = [bins]
        if isinstance(envs, str):
            envs = [envs]
        return {
            "bins": [str(v) for v in bins if str(v).strip()],
            "env": [str(v) for v in envs if str(v).strip()],
        }

    def get_required_env_vars(self, name: str) -> list[str]:
        skill_meta = self._get_skill_meta(name)
        return self._normalize_requires(skill_meta).get("env", [])

    def get_secret_requirement_status(self, name: str) -> dict[str, list[str]]:
        required = self.get_required_env_vars(name)
        present = [env_name for env_name in required if self._has_secret_or_env(skill_name=name, env_name=env_name)]
        missing = [env_name for env_name in required if env_name not in present]
        return {
            "required": required,
            "present": present,
            "missing": missing,
        }

    def get_install_commands(self, name: str, system_name: str | None = None) -> list[str]:
        meta = self._get_skill_meta(name)
        entries = meta.get("install", []) if isinstance(meta, dict) else []
        if not isinstance(entries, list):
            return []
        system_name = (system_name or platform.system()).lower()

        commands: list[str] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "brew":
                formula = str(item.get("formula") or "").strip()
                if formula and system_name == "darwin":
                    commands.append(f"brew install {formula}")
            elif kind == "apt":
                package = str(item.get("package") or "").strip()
                if package and system_name == "linux":
                    commands.append(f"sudo apt-get install -y {package}")
            elif kind == "shell":
                cmd = str(item.get("cmd") or "").strip()
                if cmd:
                    commands.append(cmd)
        return commands
    
    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result
    
    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.
        
        Args:
            name: Skill name.
        
        Returns:
            Metadata dict or None.
        """
        if name in self._metadata_cache:
            return self._metadata_cache[name]

        content = self.load_skill(name)
        if not content:
            self._metadata_cache[name] = None
            return None
        
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                self._metadata_cache[name] = metadata
                return metadata

        self._metadata_cache[name] = None
        return None
