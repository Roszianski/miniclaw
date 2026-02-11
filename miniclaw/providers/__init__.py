"""LLM provider module for miniclaw."""

from miniclaw.providers.base import LLMProvider, LLMResponse
from miniclaw.providers.failover import FailoverProvider
from miniclaw.providers.litellm_provider import LiteLLMProvider
from miniclaw.providers.oauth import OAuthDeviceFlow, OAuthDeviceFlowAdapter, OAuthTokenSet
from miniclaw.providers.transcription import (
    GroqTranscriptionProvider,
    TranscriptionManager,
    WhisperCppTranscriptionProvider,
)
from miniclaw.providers.tts import KokoroTTSAdapter

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LiteLLMProvider",
    "FailoverProvider",
    "OAuthDeviceFlow",
    "OAuthDeviceFlowAdapter",
    "OAuthTokenSet",
    "GroqTranscriptionProvider",
    "WhisperCppTranscriptionProvider",
    "TranscriptionManager",
    "KokoroTTSAdapter",
]
