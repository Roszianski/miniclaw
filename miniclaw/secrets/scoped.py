"""Per-agent secret scoping wrapper."""

from __future__ import annotations

from typing import Any


class ScopedSecretStore:
    """Namespace secret keys by agent credential scope."""

    def __init__(
        self,
        base_store: Any,
        *,
        scope: str = "shared",
        passthrough_prefixes: tuple[str, ...] | None = None,
    ):
        self.base_store = base_store
        self.scope = str(scope or "shared").strip() or "shared"
        self.passthrough_prefixes = passthrough_prefixes or (
            "oauth:",
            "channels:",
            "providers:",
            "global:",
        )

    @property
    def backend_name(self) -> str:
        return str(getattr(self.base_store, "backend_name", "unknown"))

    def _map_key(self, key: str) -> str:
        raw = str(key or "")
        normalized_scope = self.scope.lower()
        if normalized_scope in {"shared", "default", "global", ""}:
            return raw
        if raw.startswith(self.passthrough_prefixes):
            return raw
        return f"agent:{self.scope}:{raw}"

    def get(self, key: str) -> str | None:
        return self.base_store.get(self._map_key(key))

    def set(self, key: str, value: str) -> bool:
        return bool(self.base_store.set(self._map_key(key), value))

    def delete(self, key: str) -> bool:
        return bool(self.base_store.delete(self._map_key(key)))

    def has(self, key: str) -> bool:
        return bool(self.base_store.has(self._map_key(key)))
