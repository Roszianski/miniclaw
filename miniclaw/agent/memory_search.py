"""BM25 keyword search over memory files."""

import math
import re
from pathlib import Path


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer, lowercase."""
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    """Simple BM25 index for memory files."""

    def __init__(self) -> None:
        self._docs: list[tuple[Path, list[str]]] = []  # (path, tokens)
        self._avgdl: float = 0.0
        self._df: dict[str, int] = {}  # doc frequency per term
        self._N: int = 0

    def build(self, memory_dir: Path) -> None:
        """Build index from all .md files in memory_dir."""
        self._docs = []
        self._df = {}

        if not memory_dir.exists():
            return

        for f in sorted(memory_dir.glob("**/*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            tokens = _tokenize(text)
            if not tokens:
                continue
            self._docs.append((f, tokens))
            seen = set(tokens)
            for term in seen:
                self._df[term] = self._df.get(term, 0) + 1

        self._N = len(self._docs)
        total_len = sum(len(toks) for _, toks in self._docs)
        self._avgdl = total_len / self._N if self._N else 1.0

    def search(self, query: str, max_results: int = 5) -> list[tuple[Path, float, str]]:
        """
        Search the index.

        Returns list of (path, score, snippet) sorted by relevance.
        """
        if not self._docs:
            return []

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        k1 = 1.5
        b = 0.75
        scores: list[tuple[Path, float]] = []

        for path, doc_tokens in self._docs:
            dl = len(doc_tokens)
            tf: dict[str, int] = {}
            for t in doc_tokens:
                tf[t] = tf.get(t, 0) + 1

            score = 0.0
            for qt in q_tokens:
                if qt not in self._df:
                    continue
                df = self._df[qt]
                idf = math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)
                freq = tf.get(qt, 0)
                tf_norm = (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / self._avgdl))
                score += idf * tf_norm

            if score > 0:
                scores.append((path, score))

        scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for path, score in scores[:max_results]:
            try:
                text = path.read_text(encoding="utf-8")
                snippet = text[:200].replace("\n", " ")
            except Exception:
                snippet = ""
            results.append((path, score, snippet))

        return results
