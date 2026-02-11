"""Compliance helpers for retention, export, and targeted purge."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from miniclaw.utils.helpers import get_data_path, workspace_scope_id


class ComplianceService:
    """Compliance data operations for miniclaw runtime data."""

    def __init__(
        self,
        *,
        workspace: Path,
        retention: Any,
        data_dir: Path | None = None,
        usage_tracker: Any | None = None,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace_scope = workspace_scope_id(self.workspace)
        self.sessions_glob = f"{self.workspace_scope}__*.jsonl"
        self.retention = retention
        self.data_dir = Path(data_dir) if data_dir is not None else get_data_path()
        self.usage_tracker = usage_tracker

        self.sessions_dir = self.data_dir / "sessions"
        self.runs_path = self.data_dir / "runs" / "runs.jsonl"
        self.audit_path = self.data_dir / "audit.log"
        self.memory_dir = self.workspace / "memory"
        self.identity_path = self.data_dir / "identity" / "state.json"

    def sweep(self) -> dict[str, Any]:
        """Apply configured retention windows and delete expired data."""
        now = datetime.now()
        summary = {
            "ok": True,
            "swept_at": now.isoformat(),
            "retention_days": {},
            "removed": {"sessions": 0, "runs": 0, "audit": 0, "memory": 0},
        }

        session_days = self._retention_days("sessions")
        runs_days = self._retention_days("runs")
        audit_days = self._retention_days("audit")
        memory_days = self._retention_days("memory")

        summary["retention_days"] = {
            "sessions": session_days,
            "runs": runs_days,
            "audit": audit_days,
            "memory": memory_days,
        }

        session_cutoff = now - timedelta(days=session_days)
        runs_cutoff = now - timedelta(days=runs_days)
        audit_cutoff = now - timedelta(days=audit_days)
        memory_cutoff = now.date() - timedelta(days=memory_days)

        summary["removed"]["sessions"] = self._delete_files_older_than(
            self.sessions_dir,
            self.sessions_glob,
            session_cutoff.timestamp(),
        )
        summary["removed"]["runs"] = self._prune_runs_file(before_dt=runs_cutoff)
        summary["removed"]["audit"] = self._prune_audit_file(before_ts=audit_cutoff.timestamp())
        summary["removed"]["memory"] = self._prune_memory_files(before_date=memory_cutoff)
        return summary

    def export_bundle(
        self,
        *,
        include: list[str] | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Export selected data domains to a zip bundle."""
        domains = set(include or ["sessions", "runs", "audit", "memory", "identity", "usage"])
        export_dir = self.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        if output_path:
            output = self._resolve_output_path(output_path)
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output = export_dir / f"miniclaw-export-{stamp}.zip"

        output.parent.mkdir(parents=True, exist_ok=True)

        tmp_root = Path(tempfile.mkdtemp(prefix="miniclaw-export-"))
        staged = tmp_root / "bundle"
        staged.mkdir(parents=True, exist_ok=True)
        files_added = 0

        try:
            meta = {
                "created_at": datetime.now().isoformat(),
                "domains": sorted(domains),
            }
            (staged / "metadata.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            files_added += 1

            if "sessions" in domains and self.sessions_dir.exists():
                files_added += self._copy_matching_files(
                    self.sessions_dir,
                    staged / "sessions",
                    self.sessions_glob,
                )
            if "runs" in domains and self.runs_path.exists():
                (staged / "runs").mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.runs_path, staged / "runs" / self.runs_path.name)
                files_added += 1
            if "audit" in domains and self.audit_path.exists():
                (staged / "audit").mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.audit_path, staged / "audit" / self.audit_path.name)
                files_added += 1
            if "memory" in domains and self.memory_dir.exists():
                files_added += self._copy_tree(self.memory_dir, staged / "memory")
            if "identity" in domains and self.identity_path.exists():
                (staged / "identity").mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.identity_path, staged / "identity" / self.identity_path.name)
                files_added += 1
            if "usage" in domains and self.usage_tracker is not None:
                usage_path = Path(getattr(self.usage_tracker, "store_path", ""))
                if usage_path.exists():
                    (staged / "usage").mkdir(parents=True, exist_ok=True)
                    shutil.copy2(usage_path, staged / "usage" / usage_path.name)
                    files_added += 1

            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in staged.rglob("*"):
                    if not file_path.is_file():
                        continue
                    rel = file_path.relative_to(staged)
                    zf.write(file_path, arcname=str(rel))
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        return {
            "ok": True,
            "path": str(output),
            "files": files_added,
            "size_bytes": output.stat().st_size if output.exists() else 0,
            "domains": sorted(domains),
        }

    def purge(
        self,
        *,
        session_key: str | None = None,
        user_id: str | None = None,
        before_date: str | None = None,
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Purge data matching session/user/date filters."""
        filters_active = any([session_key, user_id, before_date])
        if not filters_active:
            return {
                "ok": False,
                "error": "at least one filter is required (session_key, user_id, before_date)",
                "removed": {},
            }

        selected_domains = set(domains or ["sessions", "runs", "audit", "memory", "usage"])
        cutoff_ts = self._before_date_to_timestamp(before_date)
        removed = {"sessions": 0, "runs": 0, "audit": 0, "memory": 0, "usage": 0}

        removed["sessions"] = self._purge_sessions(
            session_key=session_key,
            user_id=user_id,
            before_ts=cutoff_ts,
        ) if "sessions" in selected_domains else 0

        removed["runs"] = self._purge_runs(
            session_key=session_key,
            user_id=user_id,
            before_ts=cutoff_ts,
        ) if "runs" in selected_domains else 0

        removed["audit"] = self._purge_audit(
            session_key=session_key,
            user_id=user_id,
            before_ts=cutoff_ts,
        ) if "audit" in selected_domains else 0

        removed["memory"] = self._purge_memory(before_ts=cutoff_ts) if "memory" in selected_domains else 0

        if "usage" in selected_domains and self.usage_tracker is not None:
            removed["usage"] = int(
                self.usage_tracker.purge(
                    session_key=session_key,
                    user_id=user_id,
                    before_ts_ms=(int(cutoff_ts * 1000) if cutoff_ts is not None else None),
                )
            )

        return {"ok": True, "removed": removed}

    def _retention_days(self, domain: str) -> int:
        default_days = max(1, int(getattr(self.retention, "default_days", 60) or 60))
        specific = getattr(self.retention, f"{domain}_days", None)
        if specific is None:
            return default_days
        try:
            value = int(specific)
        except (TypeError, ValueError):
            return default_days
        return max(1, value)

    @staticmethod
    def _delete_files_older_than(base: Path, pattern: str, cutoff_ts: float) -> int:
        if not base.exists():
            return 0
        removed = 0
        for path in base.glob(pattern):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                continue
        return removed

    def _prune_runs_file(self, *, before_dt: datetime) -> int:
        if not self.runs_path.exists():
            return 0
        lines = self.runs_path.read_text(encoding="utf-8").splitlines()
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
            created_at = self._parse_iso_dt(obj.get("created_at"))
            if created_at is not None and created_at < before_dt:
                removed += 1
                continue
            kept.append(line)
        self._rewrite_jsonl(self.runs_path, kept)
        return removed

    def _prune_audit_file(self, *, before_ts: float) -> int:
        if not self.audit_path.exists():
            return 0
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
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
            ts = float(obj.get("ts") or 0.0)
            if ts > 0 and ts < before_ts:
                removed += 1
                continue
            kept.append(line)
        self._rewrite_jsonl(self.audit_path, kept)
        return removed

    def _prune_memory_files(self, *, before_date) -> int:
        if not self.memory_dir.exists():
            return 0
        removed = 0
        for path in self.memory_dir.glob("????-??-??.md"):
            stamp = self._memory_date(path)
            if stamp is None:
                continue
            if stamp < before_date:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    @staticmethod
    def _rewrite_jsonl(path: Path, lines: list[str]) -> None:
        if lines:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")

    @staticmethod
    def _parse_iso_dt(value: Any) -> datetime | None:
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _memory_date(path: Path):
        try:
            return datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _copy_tree(src: Path, dest: Path) -> int:
        count = 0
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(src)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            count += 1
        return count

    def _resolve_output_path(self, output_path: str) -> Path:
        output = Path(output_path).expanduser()
        if not output.is_absolute():
            resolved = (self.workspace / output).resolve()
        else:
            resolved = output.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError("output_path must stay inside workspace.")
        return resolved

    @staticmethod
    def _copy_matching_files(src: Path, dest: Path, pattern: str) -> int:
        count = 0
        for path in src.glob(pattern):
            if not path.is_file():
                continue
            out = dest / path.name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            count += 1
        return count

    @staticmethod
    def _before_date_to_timestamp(value: str | None) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValueError("before_date must be YYYY-MM-DD") from None
        return (parsed + timedelta(days=1)).timestamp()

    @staticmethod
    def _matches_filters(
        *,
        session: str,
        user: str,
        ts: float,
        session_key: str | None,
        user_id: str | None,
        before_ts: float | None,
    ) -> bool:
        session_filter = str(session_key or "").strip()
        user_filter = str(user_id or "").strip()

        if session_filter and session != session_filter:
            return False
        if user_filter:
            if user == user_filter:
                pass
            elif user_filter in session:
                pass
            else:
                return False
        if before_ts is not None and ts >= before_ts:
            return False
        return bool(session_filter or user_filter or before_ts is not None)

    def _purge_sessions(
        self,
        *,
        session_key: str | None,
        user_id: str | None,
        before_ts: float | None,
    ) -> int:
        if not self.sessions_dir.exists():
            return 0
        removed = 0
        for path in self.sessions_dir.glob(self.sessions_glob):
            if not path.is_file():
                continue
            key = self._session_key_for_path(path)
            ts = path.stat().st_mtime
            if not self._matches_filters(
                session=key,
                user="",
                ts=ts,
                session_key=session_key,
                user_id=user_id,
                before_ts=before_ts,
            ):
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _purge_runs(
        self,
        *,
        session_key: str | None,
        user_id: str | None,
        before_ts: float | None,
    ) -> int:
        if not self.runs_path.exists():
            return 0
        lines = self.runs_path.read_text(encoding="utf-8").splitlines()
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
            created = self._parse_iso_dt(obj.get("created_at"))
            ts = created.timestamp() if created else 0.0
            run_session = str(obj.get("session_key") or "")
            if self._matches_filters(
                session=run_session,
                user="",
                ts=ts,
                session_key=session_key,
                user_id=user_id,
                before_ts=before_ts,
            ):
                removed += 1
                continue
            kept.append(line)
        self._rewrite_jsonl(self.runs_path, kept)
        return removed

    def _purge_audit(
        self,
        *,
        session_key: str | None,
        user_id: str | None,
        before_ts: float | None,
    ) -> int:
        if not self.audit_path.exists():
            return 0
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
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
            ts = float(obj.get("ts") or 0.0)
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            event_session = str(data.get("session_key") or "")
            sender = str(data.get("sender_id") or "")
            if self._matches_filters(
                session=event_session,
                user=sender,
                ts=ts,
                session_key=session_key,
                user_id=user_id,
                before_ts=before_ts,
            ):
                removed += 1
                continue
            kept.append(line)
        self._rewrite_jsonl(self.audit_path, kept)
        return removed

    def _purge_memory(self, *, before_ts: float | None) -> int:
        if before_ts is None or not self.memory_dir.exists():
            return 0
        cutoff = datetime.fromtimestamp(before_ts).date()
        removed = 0
        for path in self.memory_dir.glob("????-??-??.md"):
            stamp = self._memory_date(path)
            if stamp is None:
                continue
            if stamp < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    @staticmethod
    def _session_key_for_path(path: Path) -> str:
        fallback_raw = path.stem.split("__", 1)[-1]
        if "_" in fallback_raw:
            channel, chat_id = fallback_raw.split("_", 1)
            fallback = f"{channel}:{chat_id}"
        else:
            fallback = fallback_raw
        try:
            with open(path, encoding="utf-8") as handle:
                first_line = handle.readline().strip()
            if not first_line:
                return fallback
            row = json.loads(first_line)
            if isinstance(row, dict):
                value = str(row.get("session_key") or "").strip()
                if value:
                    return value
        except Exception:
            return fallback
        return fallback
