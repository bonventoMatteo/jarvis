"""jarvis_voice com o subprocess do piper simulado."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_voice import COMMON_PHRASES, MOODS, JarvisVoice
from jarvis_voice.download_voice import voice_urls


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "pt_BR-faber-medium.onnx"
    path.write_bytes(b"fake")
    Path(f"{path}.json").write_text('{"audio": {"sample_rate": 22050}}', encoding="utf-8")
    return path


@pytest.fixture
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        t = np.arange(int(22050 * 0.5)) / 22050
        pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype(np.int16)
        return subprocess.CompletedProcess(command, 0, stdout=pcm.tobytes(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_missing_model_points_to_downloader(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"download_voice\.py"):
        JarvisVoice(tmp_path / "nao_existe.onnx", tmp_path)


def test_render_uses_piper_and_cache(model: Path, tmp_path: Path, fake_piper: list[dict[str, Any]]) -> None:
    voice = JarvisVoice(model, tmp_path / "cache")
    audio = voice.render("Sim senhor.", "urgent")
    assert audio.dtype == np.float32 and audio.size > 10000
    assert np.max(np.abs(audio)) <= 1.0

    command = fake_piper[0]["command"]
    assert command[1:3] == ["-m", "piper"] and "--output-raw" in command
    assert command[command.index("--length-scale") + 1] == str(MOODS["urgent"][0])
    assert fake_piper[0]["input"] == b"Sim senhor."
    assert fake_piper[0]["env"]["PYTHONIOENCODING"] == "utf-8"

    # 2ª vez: memória, sem subprocess.
    voice.render("Sim senhor.", "urgent")
    assert len(fake_piper) == 1

    # Nova instância: disco (.npy), sem subprocess, em menos de 10 ms.
    fresh = JarvisVoice(model, tmp_path / "cache")
    started = time.perf_counter()
    again = fresh.render("Sim senhor.", "urgent")
    assert (time.perf_counter() - started) < 0.01
    assert len(fake_piper) == 1 and np.array_equal(again, audio)


def test_cache_key_depends_on_mood(model: Path, tmp_path: Path) -> None:
    voice = JarvisVoice(model, tmp_path)
    assert voice._cache_key("Feito.", "neutral") != voice._cache_key("Feito.", "calm")
    assert voice._cache_key("Feito.") == voice._cache_key("  Feito.  ")
    assert voice._cache_key("Feito.").suffix == ".npy"


def test_preload_and_clear(model: Path, tmp_path: Path, fake_piper: list[dict[str, Any]]) -> None:
    voice = JarvisVoice(model, tmp_path / "cache")
    assert voice.preload() == len(COMMON_PHRASES)
    assert voice.clear_cache() == len(COMMON_PHRASES)


def test_piper_error_is_reraised(model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: list[str], **_: Any) -> None:
        raise subprocess.CalledProcessError(1, command, output=b"", stderr=b"boom")

    monkeypatch.setattr(subprocess, "run", fail)
    voice = JarvisVoice(model, tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        voice.render("teste")


def test_voice_urls() -> None:
    onnx, cfg = voice_urls("pt_BR-faber-medium")
    assert onnx == "https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx"
    assert cfg == onnx + ".json"
