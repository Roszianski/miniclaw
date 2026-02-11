"""Session management for conversation history."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger

from miniclaw.utils.helpers import get_sessions_path, safe_filename, workspace_scope_id


@dataclass
class RunState:
    """Runtime state for a single agent run."""

    run_id: str
    session_key: str
    channel: str = ""
    chat_id: str = ""
    model: str = ""
    status: str = "queued"
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    usage_total_tokens: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_key": self.session_key,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "usage_prompt_tokens": int(self.usage_prompt_tokens or 0),
            "usage_completion_tokens": int(self.usage_completion_tokens or 0),
            "usage_total_tokens": int(self.usage_total_tokens or 0),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        def _dt(value: Any) -> datetime | None:
            if isinstance(value, str) and value:
                try:
                    return datetime.fromisoformat(value)
                except ValueError:
                    return None
            return None

        return cls(
            run_id=str(data.get("run_id") or ""),
            session_key=str(data.get("session_key") or ""),
            channel=str(data.get("channel") or ""),
            chat_id=str(data.get("chat_id") or ""),
            model=str(data.get("model") or ""),
            status=str(data.get("status") or "queued"),
            created_at=_dt(data.get("created_at")) or datetime.now(),
            started_at=_dt(data.get("started_at")),
            ended_at=_dt(data.get("ended_at")),
            usage_prompt_tokens=int(data.get("usage_prompt_tokens") or 0),
            usage_completion_tokens=int(data.get("usage_completion_tokens") or 0),
            usage_total_tokens=int(data.get("usage_total_tokens") or 0),
            error=str(data.get("error")) if data.get("error") is not None else None,
        )


@dataclass
class Session:
    """
    A conversation session.
    
    Stores messages in JSONL format for easy reading and persistence.
    """
    
    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    
    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()
    
    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.
        
        Args:
            max_messages: Maximum messages to return.
        
        Returns:
            List of messages in LLM format.
        """
        # Get recent messages
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        
        # Convert to LLM format (just role and content)
        history = [{"role": m["role"], "content": m["content"]} for m in recent]

        # Prepend summary if present
        if self.summary:
            history = [{"role": "system", "content": f"Conversation summary:\n{self.summary}"}] + history
        return history
    
    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()

    def set_last_run(self, run: RunState) -> None:
        """Store lightweight run metadata on the session."""
        self.metadata["last_run"] = run.to_dict()
        self.metadata["last_run_id"] = run.run_id
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.
    
    Sessions are stored as JSONL files in the sessions directory.
    """
    
    def __init__(self, workspace: Path, idle_reset_minutes: int = 0):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace_scope = workspace_scope_id(self.workspace)
        self.sessions_dir = get_sessions_path()
        self.idle_reset_minutes = max(0, int(idle_reset_minutes))
        self._cache: dict[str, Session] = {}
        self._save_locks: dict[str, RLock] = {}
        self._save_locks_guard = RLock()

    def _scoped_stem(self, key: str) -> str:
        safe_key = safe_filename(key.replace(":", "_"))
        return f"{self.workspace_scope}__{safe_key}"

    @staticmethod
    def _decode_safe_session_key(safe_key: str) -> str:
        if "_" not in safe_key:
            return safe_key
        channel, chat_id = safe_key.split("_", 1)
        return f"{channel}:{chat_id}"

    def _legacy_session_path(self, key: str) -> Path:
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_dir / f"{self._scoped_stem(key)}.jsonl"

    @staticmethod
    def _backup_path(path: Path) -> Path:
        return path.with_suffix(f"{path.suffix}.bak")

    def _get_save_lock(self, key: str) -> RLock:
        with self._save_locks_guard:
            lock = self._save_locks.get(key)
            if lock is None:
                lock = RLock()
                self._save_locks[key] = lock
            return lock

    def _load_from_path(self, path: Path, key: str) -> Session:
        messages: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        created_at = None
        updated_at = None

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                data = json.loads(line)

                if data.get("_type") == "metadata":
                    metadata = data.get("metadata", {})
                    created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                    updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                else:
                    messages.append(data)

        return Session(
            key=key,
            messages=messages,
            created_at=created_at or datetime.now(),
            updated_at=updated_at or created_at or datetime.now(),
            metadata=metadata,
            summary=metadata.get("summary", ""),
        )

    def _recover_from_backup(self, path: Path, key: str) -> Session | None:
        backup_path = self._backup_path(path)
        if not backup_path.exists():
            return None
        try:
            recovered = self._load_from_path(backup_path, key)
        except Exception as e:
            logger.warning(f"Failed to load session backup for {key}: {e}")
            return None

        logger.warning(f"Recovered session {key} from backup file: {backup_path}")
        try:
            backup_path.replace(path)
        except Exception:
            pass
        return recovered

    def _write_session_payload(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_path(path)
        tmp_path: Path | None = None
        had_existing = path.exists()
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f"{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)

            if tmp_path is None:
                raise OSError("Failed to create temporary session file.")

            if had_existing:
                path.replace(backup_path)
            tmp_path.replace(path)
        except Exception:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if had_existing and backup_path.exists() and not path.exists():
                backup_path.replace(path)
            raise
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        # Check cache
        if key in self._cache:
            return self._cache[key]
        
        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        
        self._cache[key] = session
        return session

    def apply_idle_reset(self, session: Session) -> bool:
        """Reset a session if it has been idle beyond policy."""
        if self.idle_reset_minutes <= 0:
            return False
        if not session.messages and not session.summary:
            return False
        elapsed_minutes = (datetime.now() - session.updated_at).total_seconds() / 60.0
        if elapsed_minutes < float(self.idle_reset_minutes):
            return False

        session.clear()
        session.summary = ""
        session.metadata = {
            "idle_reset_at": datetime.now().isoformat(),
            "idle_reset_minutes": self.idle_reset_minutes,
        }
        self.save(session)
        return True

    def reset_all(
        self,
        *,
        reason: str = "scheduled",
        actor: str = "system",
        include_persisted: bool = True,
    ) -> int:
        """Reset all known sessions and persist reset metadata."""
        keys: set[str] = set()
        if include_persisted:
            for item in self.list_sessions():
                key = str(item.get("key") or "").strip()
                if key:
                    keys.add(key)

        for key, cached in self._cache.items():
            if cached.messages or cached.summary or cached.metadata:
                keys.add(key)

        if not keys:
            return 0

        reset_at = datetime.now().isoformat()
        reset_count = 0
        for key in sorted(keys):
            session = self.get_or_create(key)
            had_content = bool(session.messages or session.summary or session.metadata)
            session.clear()
            session.summary = ""
            session.metadata = {
                "bulk_reset_at": reset_at,
                "bulk_reset_reason": reason,
                "bulk_reset_actor": actor,
            }
            self.save(session)
            if had_content:
                reset_count += 1
        return reset_count
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        fallback = self._legacy_session_path(key)
        legacy_loaded = False

        if not path.exists() and fallback.exists():
            path = fallback
            legacy_loaded = True
        
        if not path.exists():
            return None
        
        try:
            session = self._load_from_path(path, key)
            # Migrate legacy unscoped session files on read.
            if legacy_loaded:
                try:
                    self.save(session)
                except Exception:
                    pass
            return session
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}; trying backup")
            recovered = self._recover_from_backup(path, key)
            if recovered is not None:
                return recovered
            return None
    
    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)
        session.metadata["summary"] = session.summary

        metadata_line = {
            "_type": "metadata",
            "session_key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
        }
        lines = [json.dumps(metadata_line)] + [json.dumps(msg) for msg in session.messages]
        payload = "\n".join(lines) + "\n"

        save_lock = self._get_save_lock(session.key)
        with save_lock:
            self._write_session_payload(path, payload)

        self._cache[session.key] = session
    
    def delete(self, key: str) -> bool:
        """
        Delete a session.
        
        Args:
            key: Session key.
        
        Returns:
            True if deleted, False if not found.
        """
        # Remove from cache
        self._cache.pop(key, None)
        
        # Remove file
        path = self._get_session_path(key)
        legacy = self._legacy_session_path(key)
        removed = False
        if path.exists():
            path.unlink()
            removed = True
        if legacy.exists():
            legacy.unlink()
            removed = True
        return removed
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        prefix = f"{self.workspace_scope}__"
        pattern = f"{prefix}*.jsonl"
        for path in self.sessions_dir.glob(pattern):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            # Count messages by counting remaining lines
                            msg_count = 0
                            for _ in f:
                                msg_count += 1
                            stem = path.stem
                            if not stem.startswith(prefix):
                                continue
                            scoped_key = stem[len(prefix):]
                            restored_key = str(data.get("session_key") or "").strip()
                            if not restored_key:
                                restored_key = self._decode_safe_session_key(scoped_key)
                            sessions.append({
                                "key": restored_key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path),
                                "messages": msg_count,
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
