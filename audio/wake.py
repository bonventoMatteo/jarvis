"""
Wake word com openWakeWord ("hey jarvis").

Roda em paralelo ao detector de palmas sobre o mesmo stream de microfone.
O openWakeWord espera frames de 1280 amostras int16 em 16 kHz, então os
blocos de 40 ms do microfone são acumulados antes da inferência.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import structlog

from config import MODELS_DIR, settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)

WAKE_FRAME = 1280
"""Amostras por inferência do openWakeWord (80 ms @ 16 kHz)."""


class WakeWordEngine:
    """Wrapper sobre `openwakeword.model.Model`."""

    def __init__(self, model_name: str | None = None, threshold: float | None = None) -> None:
        self.model_name = model_name or settings.wake_model
        self.threshold = threshold if threshold is not None else settings.wake_threshold
        self._model = None
        self._pending = np.zeros(0, dtype=np.float32)
        self.available = False
        self.last_score: float = 0.0

    # ------------------------------------------------------------------ #
    def load(self) -> bool:
        """
        Carrega o modelo. Retorna False (sem levantar) se o openWakeWord não
        estiver instalado ou o modelo não for encontrado — o JARVIS continua
        funcionando por palma e hotkey.
        """
        if self._model is not None:
            return True
        try:
            from openwakeword.model import Model
            from openwakeword.utils import download_models

            candidate = Path(self.model_name)
            local = candidate if candidate.exists() else MODELS_DIR / "openwakeword" / self.model_name
            if local.suffix and local.exists():
                wakeword_models = [str(local)]
            else:
                # Modelo pré-treinado por nome; baixa se ainda não houver.
                try:
                    download_models([self.model_name.replace("_v0.1", "")])
                except Exception:
                    download_models()
                wakeword_models = [self.model_name]

            self._model = Model(wakeword_models=wakeword_models, inference_framework="onnx")
            self.available = True
            log.info("wake.loaded", model=self.model_name, threshold=self.threshold)
            return True
        except Exception as exc:
            log.warning("wake.unavailable", error=str(exc), model=self.model_name)
            self.available = False
            return False

    # ------------------------------------------------------------------ #
    def feed(self, chunk: np.ndarray) -> float:
        """
        Alimenta o modelo com um bloco float32 e devolve o maior score.

        Returns:
            Score entre 0 e 1 (0 se não houve frame completo ainda).
        """
        if self._model is None:
            return 0.0

        self._pending = np.concatenate([self._pending, chunk])
        best = 0.0
        while self._pending.size >= WAKE_FRAME:
            frame, self._pending = self._pending[:WAKE_FRAME], self._pending[WAKE_FRAME:]
            pcm = np.clip(frame * 32767.0, -32768, 32767).astype(np.int16)
            try:
                scores = self._model.predict(pcm)
            except Exception as exc:  # pragma: no cover
                log.warning("wake.predict_error", error=str(exc))
                return 0.0
            if scores:
                best = max(best, max(float(value) for value in scores.values()))

        self.last_score = best
        return best

    def reset(self) -> None:
        """Limpa buffers internos (após uma ativação)."""
        self._pending = np.zeros(0, dtype=np.float32)
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:  # pragma: no cover
                pass


class WakeWordListener:
    """Task assíncrona que observa o stream do microfone."""

    def __init__(self, bus: EventBus, mic, engine: WakeWordEngine | None = None) -> None:  # noqa: ANN001
        self.bus = bus
        self.mic = mic
        self.engine = engine or WakeWordEngine()
        self.enabled = settings.wake_enabled
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._last_fire = 0.0

    async def load(self) -> bool:
        """Carrega o modelo fora do event loop (é lento)."""
        if not self.enabled:
            return False
        return await asyncio.to_thread(self.engine.load)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            chunk = await self._queue.get()
            if not self.enabled or not self.engine.available:
                continue
            score = self.engine.feed(chunk)
            if score < self.engine.threshold:
                continue
            now = time.monotonic() * 1000.0
            if now - self._last_fire < settings.wake_cooldown_ms:
                continue
            self._last_fire = now
            self.engine.reset()
            log.info("wake.activate", score=round(score, 3))
            self.bus.emit(EventType.WAKE_WORD, source="wake", score=score)

    def start(self) -> None:
        if self._task is not None or not self.engine.available:
            return
        self._queue = self.mic.subscribe()
        self._task = asyncio.create_task(self._run(), name="wake-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._queue is not None:
            self.mic.unsubscribe(self._queue)
            self._queue = None


__all__ = ["WAKE_FRAME", "WakeWordEngine", "WakeWordListener"]
