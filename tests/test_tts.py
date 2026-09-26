"""Motor de voz: vozes online com cache em disco e fallback."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest

from tts import piper_engine
from tts.cloud import decode_audio


def _sine(rate: int = 24000, seconds: float = 0.4) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.3 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def test_decode_mp3_roundtrip() -> None:
    from pedalboard.io import AudioFile

    buffer = io.BytesIO()
    with AudioFile(buffer, "w", 24000, 1, format="mp3") as handle:
        handle.write(_sine().reshape(1, -1))
    audio, rate = decode_audio(buffer.getvalue())
    assert rate == 24000 and audio.dtype == np.float32 and audio.size > 8000


@pytest.fixture
def engine(tmp_path: Path) -> piper_engine.PiperEngine:
    tts = piper_engine.PiperEngine()
    tts.cloud = "edge"
    tts.backend = "none"
    tts._disk_cache = tmp_path / "cache"
    return tts


def test_cloud_voice_with_disk_cache(engine: piper_engine.PiperEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_edge(text: str, mood: str) -> tuple[np.ndarray, int]:
        calls.append(text)
        return _sine(), 24000

    monkeypatch.setattr(piper_engine, "synthesize_edge", fake_edge)
    audio, rate = engine.synthesize("Um relatório bem longo que não cabe no cache de memória, senhor, " * 2)
    assert rate == 24000 and audio.size > 0 and len(calls) == 1
    engine.synthesize("Um relatório bem longo que não cabe no cache de memória, senhor, " * 2)
    assert len(calls) == 1  # veio do .npz em disco
    assert "AntonioNeural" in engine.label


def test_cloud_failure_falls_back_to_piper(engine: piper_engine.PiperEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(text: str, mood: str) -> tuple[np.ndarray, int]:
        raise OSError("sem internet")

    monkeypatch.setattr(piper_engine, "synthesize_edge", broken)
    engine.backend = "piper"
    engine._voice = object()
    monkeypatch.setattr(engine, "_synthesize_piper", lambda text, mood: _sine(22050))
    audio, rate = engine.synthesize("Sim?")
    assert rate == 22050 and audio.size > 0
    assert engine.label == "piper"  # online suspenso por 2 minutos
