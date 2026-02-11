"""Voice transcription providers and manager."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from loguru import logger


class GroqTranscriptionProvider:
    """Groq Whisper transcription provider."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error(f"Audio file not found: {file_path}")
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0,
                    )

                    response.raise_for_status()
                    data = response.json()
                    return str(data.get("text") or "")

        except Exception as e:
            logger.error(f"Groq transcription error: {e}")
            return ""


class WhisperCppTranscriptionProvider:
    """Local whisper.cpp provider using `whisper-cli`."""

    def __init__(self, cli: str = "whisper-cli", model_path: str | Path = "~/.miniclaw/models/whisper-small.en.bin"):
        self.cli = cli
        self.model_path = Path(model_path).expanduser()

    def is_available(self) -> bool:
        return bool(shutil.which(self.cli)) and self.model_path.exists()

    def missing_reason(self) -> str:
        if not shutil.which(self.cli):
            return f"missing binary: {self.cli}"
        if not self.model_path.exists():
            return f"missing model: {self.model_path}"
        return ""

    async def transcribe(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            logger.error(f"Audio file not found: {file_path}")
            return ""
        if not self.is_available():
            return ""

        out_text = ""
        with tempfile.TemporaryDirectory(prefix="miniclaw-whisper-") as tmp:
            out_base = Path(tmp) / "transcript"
            proc = await asyncio.create_subprocess_exec(
                self.cli,
                "-m",
                str(self.model_path),
                "-f",
                str(path),
                "-of",
                str(out_base),
                "-otxt",
                "-nt",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = (stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")).strip()
                logger.warning(f"whisper-cli transcription failed: {detail[:300]}")
                return ""

            txt_file = Path(f"{out_base}.txt")
            if txt_file.exists():
                out_text = txt_file.read_text(encoding="utf-8").strip()
            elif stdout:
                out_text = stdout.decode("utf-8", errors="ignore").strip()
        return out_text


class TranscriptionManager:
    """Selects local Whisper first, then optional Groq fallback."""

    def __init__(
        self,
        *,
        local_provider: WhisperCppTranscriptionProvider | None = None,
        local_enabled: bool = False,
        groq_provider: GroqTranscriptionProvider | None = None,
        groq_fallback: bool = True,
    ):
        self.local_provider = local_provider
        self.local_enabled = bool(local_enabled)
        self.groq_provider = groq_provider
        self.groq_fallback = bool(groq_fallback)

    async def transcribe(self, file_path: str | Path) -> str:
        if self.local_enabled and self.local_provider:
            if self.local_provider.is_available():
                text = await self.local_provider.transcribe(file_path)
                if text.strip():
                    return text
            else:
                logger.info(f"Local whisper unavailable: {self.local_provider.missing_reason()}")

        if self.groq_fallback and self.groq_provider and self.groq_provider.is_configured():
            text = await self.groq_provider.transcribe(file_path)
            if text.strip():
                return text
        return ""

    @classmethod
    def from_config(cls, config, groq_api_key: str | None = None) -> "TranscriptionManager":
        local_cfg = getattr(config, "local_whisper", None)
        local_enabled = bool(getattr(local_cfg, "enabled", False))
        local_cli = str(getattr(local_cfg, "cli", "whisper-cli"))
        local_model_path = str(getattr(local_cfg, "model_path", "~/.miniclaw/models/whisper-small.en.bin"))
        groq_fallback = bool(getattr(config, "groq_fallback", True))
        return cls(
            local_provider=WhisperCppTranscriptionProvider(cli=local_cli, model_path=local_model_path),
            local_enabled=local_enabled,
            groq_provider=GroqTranscriptionProvider(api_key=groq_api_key),
            groq_fallback=groq_fallback,
        )
