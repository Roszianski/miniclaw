from pathlib import Path

from miniclaw.agent.tools.memory_search import MemorySearchTool
from miniclaw.providers.base import LLMProvider, LLMResponse


class EmbedProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls: list[int] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        thinking=None,
    ) -> LLMResponse:
        return LLMResponse(content="")

    async def embed(self, texts, model=None):
        self.calls.append(len(texts))
        return [[0.01 * (i + 1), 0.02, 0.03] for i in range(len(texts))]

    def get_default_model(self) -> str:
        return "test/model"


async def test_memory_embedding_cache_reuses_document_vectors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "2026-02-09.md").write_text("# notes\nremember this", encoding="utf-8")

    provider1 = EmbedProvider()
    tool1 = MemorySearchTool(workspace=workspace, provider=provider1, embedding_model="test-emb")
    _ = await tool1.execute(query="remember", max_results=3)
    assert provider1.calls
    cache_file = memory_dir / ".embeddings_cache.json"
    assert cache_file.exists()

    provider2 = EmbedProvider()
    tool2 = MemorySearchTool(workspace=workspace, provider=provider2, embedding_model="test-emb")
    _ = await tool2.execute(query="remember", max_results=3)
    # Cached docs should avoid document embedding call; query embedding still runs once.
    assert provider2.calls == [1]
