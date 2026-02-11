"""Structured patch tool for batch file edits."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from miniclaw.agent.tools.base import Tool
from miniclaw.agent.tools.filesystem import _resolve_path


class PatchError(Exception):
    """Raised when patch parsing or application fails."""


@dataclass
class HunkLine:
    """Single line in a patch hunk."""

    kind: Literal[" ", "+", "-"]
    text: str


@dataclass
class PatchOperation:
    """Parsed patch operation."""

    kind: Literal["add", "delete", "update"]
    path: str
    add_lines: list[str] | None = None
    hunks: list[list[HunkLine]] | None = None
    move_to: str | None = None


class ApplyPatchTool(Tool):
    """Tool to apply structured multi-file patches."""

    BEGIN_MARKER = "*** Begin Patch"
    END_MARKER = "*** End Patch"
    ADD_PREFIX = "*** Add File: "
    DELETE_PREFIX = "*** Delete File: "
    UPDATE_PREFIX = "*** Update File: "
    MOVE_PREFIX = "*** Move to: "

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a structured patch with Add/Delete/Update file operations "
            "across multiple files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Patch text using the structured format beginning with "
                        "'*** Begin Patch' and ending with '*** End Patch'."
                    ),
                }
            },
            "required": ["patch"],
        }

    async def execute(self, patch: str, **kwargs: Any) -> str:
        try:
            operations = self._parse_patch(patch)
            changes = self._apply_operations(operations)
            return "\n".join(changes)
        except PermissionError as exc:
            return f"Error: {exc}"
        except PatchError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error applying patch: {exc}"

    def _parse_patch(self, patch: str) -> list[PatchOperation]:
        lines = (patch or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if not lines or lines[0] != self.BEGIN_MARKER:
            raise PatchError("Patch must start with '*** Begin Patch'.")
        if lines[-1] != self.END_MARKER:
            raise PatchError("Patch must end with '*** End Patch'.")

        ops: list[PatchOperation] = []
        index = 1
        end = len(lines) - 1
        while index < end:
            line = lines[index]
            if not line.strip():
                index += 1
                continue

            if line.startswith(self.ADD_PREFIX):
                path = line[len(self.ADD_PREFIX) :].strip()
                if not path:
                    raise PatchError("Add operation requires a file path.")
                index += 1
                add_lines: list[str] = []
                while index < end and not lines[index].startswith("*** "):
                    raw = lines[index]
                    if not raw.startswith("+"):
                        raise PatchError(
                            f"Add operation for '{path}' expects '+' lines, got: {raw!r}"
                        )
                    add_lines.append(raw[1:])
                    index += 1
                if not add_lines:
                    raise PatchError(f"Add operation for '{path}' must include at least one line.")
                ops.append(PatchOperation(kind="add", path=path, add_lines=add_lines))
                continue

            if line.startswith(self.DELETE_PREFIX):
                path = line[len(self.DELETE_PREFIX) :].strip()
                if not path:
                    raise PatchError("Delete operation requires a file path.")
                ops.append(PatchOperation(kind="delete", path=path))
                index += 1
                continue

            if line.startswith(self.UPDATE_PREFIX):
                path = line[len(self.UPDATE_PREFIX) :].strip()
                if not path:
                    raise PatchError("Update operation requires a file path.")
                index += 1
                move_to: str | None = None
                if index < end and lines[index].startswith(self.MOVE_PREFIX):
                    move_to = lines[index][len(self.MOVE_PREFIX) :].strip()
                    if not move_to:
                        raise PatchError(f"Update operation for '{path}' has an empty move target.")
                    index += 1

                hunks: list[list[HunkLine]] = []
                current: list[HunkLine] = []
                has_change = False
                while index < end:
                    raw = lines[index]
                    if (
                        raw == self.END_MARKER
                        or raw.startswith(self.ADD_PREFIX)
                        or raw.startswith(self.DELETE_PREFIX)
                        or raw.startswith(self.UPDATE_PREFIX)
                    ):
                        break
                    if raw.startswith("@@"):
                        if current:
                            hunks.append(current)
                            current = []
                        index += 1
                        continue
                    if raw == "*** End of File":
                        index += 1
                        continue
                    if raw and raw[0] in {" ", "+", "-"}:
                        current.append(HunkLine(kind=raw[0], text=raw[1:]))
                        if raw[0] in {"+", "-"}:
                            has_change = True
                        index += 1
                        continue
                    raise PatchError(
                        f"Update operation for '{path}' has invalid hunk line: {raw!r}"
                    )

                if current:
                    hunks.append(current)
                if not hunks and not move_to:
                    raise PatchError(f"Update operation for '{path}' has no hunks.")
                if hunks and not has_change:
                    raise PatchError(f"Update operation for '{path}' has no changes.")
                ops.append(PatchOperation(kind="update", path=path, hunks=hunks, move_to=move_to))
                continue

            raise PatchError(f"Unknown patch operation line: {line!r}")

        if not ops:
            raise PatchError("Patch contains no operations.")
        return ops

    def _apply_operations(self, operations: list[PatchOperation]) -> list[str]:
        changes: list[str] = []
        for op in operations:
            if op.kind == "add":
                path = self._resolve_user_path(op.path)
                if path.exists():
                    raise PatchError(f"Cannot add '{op.path}': target already exists.")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(op.add_lines or []), encoding="utf-8")
                changes.append(f"Added {op.path}")
                continue

            if op.kind == "delete":
                path = self._resolve_user_path(op.path)
                if not path.exists():
                    raise PatchError(f"Cannot delete '{op.path}': file does not exist.")
                if not path.is_file():
                    raise PatchError(f"Cannot delete '{op.path}': not a file.")
                path.unlink()
                changes.append(f"Deleted {op.path}")
                continue

            if op.kind == "update":
                source = self._resolve_user_path(op.path)
                if not source.exists():
                    raise PatchError(f"Cannot update '{op.path}': file does not exist.")
                if not source.is_file():
                    raise PatchError(f"Cannot update '{op.path}': not a file.")
                content = source.read_text(encoding="utf-8")
                lines, trailing_newline = self._split_lines(content)
                for hunk in op.hunks or []:
                    lines = self._apply_hunk(lines, hunk, display_path=op.path)
                updated = self._join_lines(lines, trailing_newline)

                target = source
                if op.move_to:
                    target = self._resolve_user_path(op.move_to)
                    if target != source and target.exists():
                        raise PatchError(f"Cannot move '{op.path}' to '{op.move_to}': target exists.")
                    target.parent.mkdir(parents=True, exist_ok=True)

                target.write_text(updated, encoding="utf-8")
                if op.move_to and target != source:
                    source.unlink()
                    changes.append(f"Updated {op.path} -> {op.move_to}")
                else:
                    changes.append(f"Updated {op.path}")
                continue

            raise PatchError(f"Unsupported operation kind: {op.kind}")
        return changes

    def _resolve_user_path(self, value: str) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute() and self._allowed_dir:
            raw = self._allowed_dir / raw
        return _resolve_path(str(raw), self._allowed_dir)

    @staticmethod
    def _split_lines(content: str) -> tuple[list[str], bool]:
        return content.splitlines(), content.endswith("\n")

    @staticmethod
    def _join_lines(lines: list[str], trailing_newline: bool) -> str:
        result = "\n".join(lines)
        if trailing_newline and lines:
            result += "\n"
        return result

    def _apply_hunk(self, lines: list[str], hunk: list[HunkLine], display_path: str) -> list[str]:
        old_lines = [entry.text for entry in hunk if entry.kind in {" ", "-"}]
        new_lines = [entry.text for entry in hunk if entry.kind in {" ", "+"}]
        has_change = any(entry.kind in {"+", "-"} for entry in hunk)

        if not has_change:
            raise PatchError(f"Hunk for '{display_path}' has no changes.")
        if not old_lines:
            raise PatchError(
                f"Hunk for '{display_path}' has no match context; include context or removed lines."
            )

        width = len(old_lines)
        matches: list[int] = []
        for i in range(0, len(lines) - width + 1):
            if lines[i : i + width] == old_lines:
                matches.append(i)
                if len(matches) > 1:
                    break

        if not matches:
            raise PatchError(f"Hunk did not match target file '{display_path}'.")
        if len(matches) > 1:
            raise PatchError(f"Hunk matched multiple regions in '{display_path}'; add more context.")

        idx = matches[0]
        return lines[:idx] + new_lines + lines[idx + width :]
