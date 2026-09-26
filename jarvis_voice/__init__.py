"""Voz do Jarvis: piper-tts + pedalboard + sounddevice, offline e sem GPU."""
from __future__ import annotations

from .config import CACHE_DIR, DEFAULT_MODEL, MODELS_DIR, SAMPLE_RATE
from .engine import COMMON_PHRASES, MOODS, JarvisVoice, build_chain

__all__ = [
    "CACHE_DIR",
    "COMMON_PHRASES",
    "DEFAULT_MODEL",
    "MODELS_DIR",
    "MOODS",
    "SAMPLE_RATE",
    "JarvisVoice",
    "build_chain",
]
