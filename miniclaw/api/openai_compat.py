"""OpenAI-compatible API surface for miniclaw runtime."""

from __future__ import annotations

import asyncio
import secrets
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

from miniclaw.providers.base import LLMProvider
from miniclaw.providers.transcription import TranscriptionManager
from miniclaw.providers.tts import KokoroTTSAdapter


class _OpenAIRateLimiter:
    """Simple in-memory 60-second fixed window limiter."""

    def __init__(self, *, requests_per_minute: int, tokens_per_minute: int):
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.tokens_per_minute = max(1, int(tokens_per_minute))
        self._request_times: deque[float] = deque()
        self._token_times: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def allow(self, *, estimated_tokens: int) -> bool:
        now = time.time()
        cutoff = now - 60.0
        tokens = max(1, int(estimated_tokens))

        async with self._lock:
            while self._request_times and self._request_times[0] < cutoff:
                self._request_times.popleft()
            while self._token_times and self._token_times[0][0] < cutoff:
                self._token_times.popleft()

            used_tokens = sum(v for _, v in self._token_times)
            if len(self._request_times) >= self.requests_per_minute:
                return False
            if used_tokens + tokens > self.tokens_per_minute:
                return False

            self._request_times.append(now)
            self._token_times.append((now, tokens))
            return True


def create_openai_compat_app(
    *,
    config: Any,
    provider: LLMProvider,
    agent_runtime: Any,
    transcription_manager: TranscriptionManager | None = None,
    tts_adapter: KokoroTTSAdapter | None = None,
    usage_tracker: Any | None = None,
) -> FastAPI:
    """Create OpenAI-compatible app."""
    app = FastAPI(title="miniclaw openai-compat", docs_url=None, redoc_url=None)
    compat_cfg = config.api.openai_compat
    limiter = _OpenAIRateLimiter(
        requests_per_minute=compat_cfg.rate_limits.requests_per_minute,
        tokens_per_minute=compat_cfg.rate_limits.tokens_per_minute,
    )
    tts = tts_adapter or KokoroTTSAdapter(
        output_dir=Path(config.transcription.tts.output_dir).expanduser(),
        default_voice=config.transcription.tts.default_voice,
    )
    transcriber = transcription_manager or TranscriptionManager.from_config(
        config.transcription,
        groq_api_key=config.providers.groq.api_key or None,
    )

    async def require_auth(authorization: str = Header(default="")) -> None:
        token = str(compat_cfg.auth_token or "").strip()
        if not token:
            raise HTTPException(
                status_code=503,
                detail="OpenAI compatibility API auth token is not configured.",
            )
        parts = authorization.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Unauthorized")
        provided_token = parts[1].strip()
        if not provided_token or not secrets.compare_digest(provided_token, token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    async def check_limit(payload: Any) -> None:
        estimated_tokens = _estimate_payload_tokens(payload)
        allowed = await limiter.allow(estimated_tokens=estimated_tokens)
        if not allowed:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    @app.get("/v1/models", dependencies=[Depends(require_auth)])
    async def list_models() -> dict[str, Any]:
        names = [config.agents.defaults.model]
        default_model = provider.get_default_model()
        if default_model not in names:
            names.append(default_model)
        data = [{"id": model, "object": "model", "owned_by": "miniclaw"} for model in names]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
    async def chat_completions(body: dict[str, Any]) -> dict[str, Any]:
        await check_limit(body)
        if bool(body.get("stream")):
            raise HTTPException(status_code=400, detail="stream=true is not supported yet.")

        model = str(body.get("model") or config.agents.defaults.model)
        messages = _normalize_messages(body.get("messages") or [])
        tools = body.get("tools") if isinstance(body.get("tools"), list) else None
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or config.agents.defaults.max_tokens)
        temperature = float(body.get("temperature", config.agents.defaults.temperature))

        response = await provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        message_obj: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            message_obj["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _safe_json_dump(call.arguments)},
                }
                for call in response.tool_calls
            ]

        finish_reason = "tool_calls" if response.tool_calls else (response.finish_reason or "stop")
        usage = {
            "prompt_tokens": int(response.usage.get("prompt_tokens") or 0),
            "completion_tokens": int(response.usage.get("completion_tokens") or 0),
            "total_tokens": int(response.usage.get("total_tokens") or 0),
        }
        if usage_tracker is not None:
            metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            try:
                usage_tracker.record(
                    source="openai_compat.chat_completions",
                    model=model,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    session_key=str(
                        body.get("session_key")
                        or metadata.get("session_key", "")
                        or "openai:chat_completions"
                    ),
                )
            except Exception:
                pass
        return {
            "id": f"chatcmpl_{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message_obj, "finish_reason": finish_reason}],
            "usage": usage,
        }

    @app.post("/v1/responses", dependencies=[Depends(require_auth)])
    async def responses(body: dict[str, Any]) -> dict[str, Any]:
        await check_limit(body)
        model = str(body.get("model") or config.agents.defaults.model)
        input_text = _extract_responses_input(body.get("input"))
        if not input_text.strip():
            raise HTTPException(status_code=400, detail="input is required.")

        session_key = str(
            body.get("session_key")
            or body.get("conversation")
            or body.get("metadata", {}).get("session_key", "")
            or "openai:responses"
        )
        result = await agent_runtime.process_direct(
            content=input_text,
            session_key=session_key,
            channel="openai",
            chat_id="responses",
            model_override=model,
        )
        content = str(result or "")
        return {
            "id": f"resp_{uuid.uuid4().hex[:24]}",
            "object": "response",
            "created": int(time.time()),
            "model": model,
            "status": "completed",
            "output": [
                {
                    "id": f"msg_{uuid.uuid4().hex[:20]}",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ],
        }

    @app.post("/v1/embeddings", dependencies=[Depends(require_auth)])
    async def embeddings(body: dict[str, Any]) -> dict[str, Any]:
        await check_limit(body)
        raw_input = body.get("input")
        model = str(body.get("model") or config.agents.defaults.embedding_model or config.agents.defaults.model)
        if isinstance(raw_input, str):
            texts = [raw_input]
        elif isinstance(raw_input, list):
            texts = [str(item) for item in raw_input]
        else:
            raise HTTPException(status_code=400, detail="input must be a string or list of strings.")

        vectors = await provider.embed(texts, model=model)
        if usage_tracker is not None:
            try:
                est_prompt = sum(max(1, len(item) // 4) for item in texts)
                usage_tracker.record(
                    source="openai_compat.embeddings",
                    model=model,
                    prompt_tokens=est_prompt,
                    completion_tokens=0,
                    total_tokens=est_prompt,
                    session_key="openai:embeddings",
                )
            except Exception:
                pass
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": idx, "embedding": vec}
                for idx, vec in enumerate(vectors)
            ],
            "model": model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(require_auth)])
    async def audio_transcriptions(
        file: UploadFile = File(...),
        model: str = Form(default="whisper-1"),
    ) -> dict[str, Any]:
        await check_limit({"size": file.size or 0, "model": model})
        max_upload_bytes = max(1024, int(compat_cfg.max_audio_upload_bytes or 25 * 1024 * 1024))
        if file.size is not None and int(file.size) > max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file is too large (max {max_upload_bytes} bytes)",
            )
        suffix = Path(file.filename or "audio.bin").suffix or ".bin"
        total_written = 0
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="miniclaw-audio-", suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_written += len(chunk)
                    if total_written > max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file is too large (max {max_upload_bytes} bytes)",
                        )
                    tmp.write(chunk)
            if tmp_path is None:
                raise HTTPException(status_code=400, detail="failed to stage upload")
            text = await transcriber.transcribe(tmp_path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return {"text": text or ""}

    @app.post("/v1/audio/speech", dependencies=[Depends(require_auth)])
    async def audio_speech(body: dict[str, Any]) -> dict[str, Any]:
        await check_limit(body)
        text = str(body.get("input") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="input is required.")
        voice = str(body.get("voice") or config.transcription.tts.default_voice)
        speed = float(body.get("speed") or 1.0)
        output_path_raw = body.get("output_path")
        output_path = Path(output_path_raw).expanduser() if output_path_raw else None

        if output_path is not None and config.tools.restrict_to_workspace:
            workspace = config.workspace_path.resolve()
            resolved = output_path if output_path.is_absolute() else (workspace / output_path).resolve()
            if not (resolved == workspace or workspace in resolved.parents):
                raise HTTPException(status_code=400, detail="output_path must stay inside workspace when restriction is enabled.")

        written = tts.synthesize_to_path(
            text=text,
            output_path=output_path,
            voice=voice,
            speed=speed,
        )
        return {
            "id": f"speech_{uuid.uuid4().hex[:20]}",
            "object": "audio.speech",
            "output_path": str(written),
            "format": written.suffix.lstrip("."),
            "bytes": written.stat().st_size,
        }

    return app


def _normalize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = _normalize_content(item.get("content"))
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            out.append({"role": role, "content": content, "tool_calls": item.get("tool_calls")})
        else:
            out.append({"role": role, "content": content})
    return out


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif "input_text" in block:
                parts.append(str(block.get("input_text") or ""))
        return "\n".join([p for p in parts if p])
    if content is None:
        return ""
    return str(content)


def _extract_responses_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(_normalize_content(item.get("content") or item.get("input") or item.get("text")))
            else:
                parts.append(str(item))
        return "\n".join([p for p in parts if p])
    if isinstance(value, dict):
        return _normalize_content(value.get("content") or value.get("input") or value.get("text"))
    return str(value or "")


def _estimate_payload_tokens(payload: Any) -> int:
    serialized = str(payload or "")
    # Approximate English tokenization for rate limiting guardrails.
    return max(1, len(serialized) // 4)


def _safe_json_dump(value: Any) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)
