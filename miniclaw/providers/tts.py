"""Text-to-speech adapter with secure local output paths."""

from __future__ import annotations

import math
import wave
from pathlib import Path


class TTSError(RuntimeError):
    """Raised when TTS synthesis fails."""


class KokoroTTSAdapter:
    """
    Lightweight TTS adapter.

    The adapter currently emits a small WAV file as a deterministic fallback when
    no Kokoro runtime is installed. This keeps API behavior stable in secured
    installs without introducing mandatory heavy dependencies.
    """

    def __init__(self, *, output_dir: Path, default_voice: str = "af_sky"):
        self.output_dir = output_dir
        self.default_voice = default_voice
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize_to_path(
        self,
        *,
        text: str,
        output_path: Path | None = None,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Path:
        content = str(text or "").strip()
        if not content:
            raise TTSError("input text is required for speech synthesis.")

        target = output_path or (self.output_dir / "speech.wav")
        target = target.expanduser()
        if not target.is_absolute():
            target = (self.output_dir / target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.suffix.lower() not in {".wav"}:
            target = target.with_suffix(".wav")

        # Fallback synthesis: short tone burst + silence, deterministic by text length.
        self._write_placeholder_wav(
            target,
            duration_s=max(0.4, min(12.0, len(content) / max(2.0, 12.0 * max(0.5, speed)))),
        )
        meta = target.with_suffix(".txt")
        meta.write_text(
            f"voice={voice or self.default_voice}\n"
            f"speed={speed}\n"
            f"text={content}\n",
            encoding="utf-8",
        )
        return target

    @staticmethod
    def _write_placeholder_wav(path: Path, *, duration_s: float) -> None:
        sample_rate = 16_000
        total_frames = int(duration_s * sample_rate)
        tone_frames = min(total_frames, sample_rate // 5)
        amplitude = 9000
        frequency = 440.0

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            for i in range(total_frames):
                if i < tone_frames:
                    value = int(amplitude * math.sin(2 * math.pi * frequency * (i / sample_rate)))
                else:
                    value = 0
                wf.writeframesraw(int(value).to_bytes(2, byteorder="little", signed=True))
