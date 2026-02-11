"""Memory search tool using BM25."""

import json
from pathlib import Path
from typing import Any, Callable

from miniclaw.agent.tools.base import Tool
from miniclaw.agent.memory_search import BM25Index, cosine_similarity
from miniclaw.providers.base import LLMProvider


class MemorySearchTool(Tool):
    """Search through memory files for relevant past context."""

    def __init__(self, workspace: Path, provider: LLMProvider | None = None, embedding_model: str = ""):
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self._index: BM25Index | None = None
        self._last_build_mtime: float = 0.0
        self._vector_docs: list[tuple[Path, list[float], str]] = []
        self._provider = provider
        self._embedding_model = embedding_model
        self._embedding_cache_path = self.memory_dir / ".embeddings_cache.json"
        self._embedding_cache: dict[str, dict[str, Any]] = self._load_embedding_cache()
        self._batch_size = 16
        self._index_hooks: list[Callable[[str, dict[str, Any]], None]] = []

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return "Search through memory files for relevant past context using keyword search."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                },
            },
            "required": ["query"],
        }

    async def _maybe_rebuild(self) -> BM25Index:
        """Rebuild index if memory files have changed."""
        if not self.memory_dir.exists():
            self._index = BM25Index()
            return self._index

        # Check if any file changed
        max_mtime = 0.0
        files: list[Path] = []
        for f in self.memory_dir.glob("**/*.md"):
            try:
                mt = f.stat().st_mtime
                if mt > max_mtime:
                    max_mtime = mt
                files.append(f)
            except Exception:
                pass

        if self._index is None or max_mtime > self._last_build_mtime:
            self._index = BM25Index()
            self._index.build(self.memory_dir)
            self._last_build_mtime = max_mtime
            await self._maybe_build_vectors(files)

        return self._index

    def add_index_hook(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a lightweight callback for indexing lifecycle events."""
        if callback not in self._index_hooks:
            self._index_hooks.append(callback)

    def _emit_index_hook(self, event: str, payload: dict[str, Any]) -> None:
        for callback in list(self._index_hooks):
            try:
                callback(event, payload)
            except Exception:
                continue

    def _load_embedding_cache(self) -> dict[str, dict[str, Any]]:
        if not self._embedding_cache_path.exists():
            return {}
        try:
            raw = json.loads(self._embedding_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                out[key] = value
        return out

    def _save_embedding_cache(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._embedding_cache_path.write_text(
            json.dumps(self._embedding_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _coerce_vector(value: Any) -> list[float] | None:
        if not isinstance(value, list):
            return None
        out: list[float] = []
        for item in value:
            try:
                out.append(float(item))
            except Exception:
                return None
        return out

    async def _maybe_build_vectors(self, files: list[Path]) -> None:
        """Build vector embeddings for memory files if enabled."""
        if not self._provider or not self._embedding_model:
            self._vector_docs = []
            return
        try:
            texts_to_embed: list[str] = []
            batch_meta: list[tuple[Path, str, str, str]] = []
            docs: list[tuple[Path, list[float], str]] = []
            cache_changed = False

            for f in files:
                try:
                    text = f.read_text(encoding="utf-8")
                    stat = f.stat()
                except Exception:
                    continue
                if not text.strip():
                    continue

                body = text[:2000]
                snippet = body[:200].replace("\n", " ")
                cache_key = str(f.resolve())
                fingerprint = f"{int(stat.st_mtime_ns)}:{int(stat.st_size)}:{self._embedding_model}"
                cached = self._embedding_cache.get(cache_key, {})
                cached_vec = self._coerce_vector(cached.get("vector")) if isinstance(cached, dict) else None

                if cached_vec is not None and cached.get("fingerprint") == fingerprint:
                    docs.append((f, cached_vec, snippet))
                    self._emit_index_hook(
                        "cache_hit",
                        {"path": cache_key, "model": self._embedding_model},
                    )
                    continue

                texts_to_embed.append(body)
                batch_meta.append((f, snippet, cache_key, fingerprint))

            if texts_to_embed:
                for start in range(0, len(texts_to_embed), self._batch_size):
                    batch_texts = texts_to_embed[start : start + self._batch_size]
                    batch_rows = batch_meta[start : start + self._batch_size]
                    self._emit_index_hook(
                        "batch_start",
                        {
                            "size": len(batch_texts),
                            "offset": start,
                            "model": self._embedding_model,
                        },
                    )
                    vectors = await self._provider.embed(batch_texts, model=self._embedding_model)
                    for (path, snippet, cache_key, fingerprint), vec in zip(batch_rows, vectors):
                        docs.append((path, vec, snippet))
                        self._embedding_cache[cache_key] = {
                            "fingerprint": fingerprint,
                            "vector": [float(v) for v in vec],
                        }
                        cache_changed = True
                    self._emit_index_hook(
                        "batch_done",
                        {
                            "size": len(batch_texts),
                            "offset": start,
                            "model": self._embedding_model,
                        },
                    )

            if cache_changed:
                self._save_embedding_cache()

            self._vector_docs = docs
            self._emit_index_hook(
                "index_done",
                {"documents": len(self._vector_docs), "model": self._embedding_model},
            )
        except Exception:
            self._vector_docs = []

    async def execute(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        index = await self._maybe_rebuild()
        results = index.search(query, max_results=max_results)

        vector_results: list[tuple[Path, float, str]] = []
        if self._vector_docs and self._provider and self._embedding_model:
            try:
                q_vec = (await self._provider.embed([query], model=self._embedding_model))[0]
                scored = []
                for path, vec, snippet in self._vector_docs:
                    score = cosine_similarity(q_vec, vec)
                    scored.append((path, score, snippet))
                scored.sort(key=lambda x: x[1], reverse=True)
                vector_results = scored[:max_results]
            except Exception:
                vector_results = []

        # Merge results (vector first, then BM25)
        merged: list[tuple[Path, float, str, str]] = []
        seen: set[Path] = set()
        for path, score, snippet in vector_results:
            merged.append((path, score, snippet, "vector"))
            seen.add(path)
        for path, score, snippet in results:
            if path in seen:
                continue
            merged.append((path, score, snippet, "bm25"))
            if len(merged) >= max_results:
                break

        if not merged:
            return "No matching memory files found."

        lines = []
        for path, score, snippet, source in merged:
            rel = path.relative_to(self.workspace) if path.is_relative_to(self.workspace) else path
            lines.append(f"**{rel}** ({source}, score: {score:.2f})\n  {snippet}")

        return "\n\n".join(lines)
