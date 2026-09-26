"""Escuta contínua: filtro de alucinações e laço com gravador/Whisper simulados."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from config import settings
from core.events import EventType
from core.orchestrator import Orchestrator, is_noise_transcript
from core.state import State


@pytest.mark.parametrize(
    ("text", "logprob", "noise"),
    [
        ("Obrigado.", 0.0, True),
        ("Legendas pela comunidade Amara.org", 0.0, True),
        ("...", 0.0, True),
        ("ok", 0.0, True),
        ("que horas são", 0.0, False),
        ("abre o Chrome", -0.3, False),
        ("abre o Chrome", -1.4, True),
    ],
)
def test_is_noise_transcript(text: str, logprob: float, noise: bool) -> None:
    assert is_noise_transcript(text, logprob) is noise


class FakeRecorder:
    def __init__(self, n: int) -> None:
        self.left = n

    async def record_until_silence(self, **_: Any) -> Any:
        await asyncio.sleep(0.01)
        if self.left <= 0:
            await asyncio.sleep(3600)
        self.left -= 1
        return SimpleNamespace(is_usable=True, speech_detected=True, audio=np.zeros(16000, dtype=np.float32))


class FakeWhisper:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    async def transcribe(self, _audio: Any) -> Any:
        return SimpleNamespace(text=self.texts.pop(0), avg_logprob=-0.2)


async def _run(texts: list[str], monkeypatch: pytest.MonkeyPatch, require_name: bool) -> list[str]:
    monkeypatch.setattr(settings, "always_listen_require_name", require_name)
    orc = Orchestrator(text_mode=True, mute_voice=True)
    orc.bus.bind_loop()
    orc.memory.db_path = orc.memory.db_path.parent / "test_listen.db"
    orc.recorder = FakeRecorder(len(texts))  # type: ignore[assignment]
    orc.whisper = FakeWhisper(list(texts))  # type: ignore[assignment]
    queue = orc.bus.subscribe(maxsize=500)
    orc.state.set(State.IDLE, force=True)
    task = asyncio.create_task(orc._listen_loop())
    await asyncio.sleep(0.8)
    task.cancel()
    handled = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.type is EventType.RESULT:
            handled.append(event.get("transcript"))
    orc.memory.close()
    return handled


async def test_listen_loop_executes_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    handled = await _run(["Obrigado.", "que horas são", "valeu", "que dia é hoje"], monkeypatch, False)
    assert handled == ["que horas são", "que dia é hoje"]


async def test_listen_loop_requires_name(monkeypatch: pytest.MonkeyPatch) -> None:
    handled = await _run(["que horas são", "Jarvis, que dia é hoje"], monkeypatch, True)
    assert handled == ["Jarvis, que dia é hoje"]
