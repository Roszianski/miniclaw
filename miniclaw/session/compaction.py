"""Session compaction via LLM summarization."""

from typing import Any

from loguru import logger

from miniclaw.providers.base import LLMProvider


async def compact_session(
    history: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    keep_recent: int = 10,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Compact a conversation by summarizing older messages.

    Args:
        history: Full message history (user/assistant dicts).
        provider: LLM provider for summarization.
        model: Model to use.
        keep_recent: Number of recent messages to keep verbatim.

    Returns:
        Tuple of (summary_text, trimmed_history).
    """
    if len(history) <= keep_recent:
        return "", history

    to_summarize = history[:-keep_recent]
    recent = history[-keep_recent:]

    # Build summarization prompt
    lines = []
    for msg in to_summarize:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content[:500]}")

    if not lines:
        return "", recent

    summarize_prompt = (
        "Summarize the following conversation concisely, preserving key facts, "
        "decisions, and context that would be needed to continue the conversation:\n\n"
        + "\n".join(lines)
    )

    try:
        response = await provider.chat(
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise."},
                {"role": "user", "content": summarize_prompt},
            ],
            tools=None,
            model=model,
            max_tokens=1024,
        )
        summary = response.content or ""
        logger.info(f"Compacted {len(to_summarize)} messages into {len(summary)} char summary")
        return summary, recent
    except Exception as e:
        logger.error(f"Compaction failed: {e}")
        return "", history
