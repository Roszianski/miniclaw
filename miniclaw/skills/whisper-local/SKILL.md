---
name: whisper-local
description: Local speech-to-text via whisper.cpp (`whisper-cli`) for offline/private transcription.
metadata: {"miniclaw":{"emoji":"🎙️","requires":{"bins":["whisper-cli"]},"install":[{"id":"brew","kind":"brew","formula":"whisper-cpp","bins":["whisper-cli"],"label":"Install whisper.cpp (brew)"},{"id":"apt","kind":"apt","package":"whisper-cpp","bins":["whisper-cli"],"label":"Install whisper.cpp (apt)"}]}}
---

# Whisper Local STT

Use local `whisper-cli` for private/offline transcription tasks.

## Quick usage

```bash
whisper-cli -m ~/.miniclaw/models/whisper-small.en.bin -f /path/to/audio.m4a -otxt -of /tmp/out -nt
cat /tmp/out.txt
```

## Notes

- This skill expects `whisper-cli` on PATH.
- Default model for miniclaw local transcription is `~/.miniclaw/models/whisper-small.en.bin`.
