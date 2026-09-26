"""
Detecção de fim de fala (VAD) com silero-vad.

`SpeechRecorder.record_until_silence()` grava a partir do microfone e para
sozinho quando o usuário cala a boca. Se o silero não estiver instalado,
cai automaticamente num VAD de energia calibrado pelo ruído ambiente.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np
import structlog

from config import settings
from core.events import EventBus

log = structlog.get_logger(__name__)

#: O silero espera exatamente 512 amostras por frame em 16 kHz.
FRAME_SAMPLES = 512


@dataclass(slots=True)
class Recording:
    """Resultado de uma gravação."""

    audio: np.ndarray
    sample_rate: int
    duration_s: float
    speech_detected: bool
    stop_reason: str

    @property
    def is_usable(self) -> bool:
        """True se há fala suficiente para valer uma transcrição."""
        return self.speech_detected and self.duration_s >= settings.vad_min_speech_ms / 1000.0


class _EnergyVAD:
    """VAD de reserva baseado em energia — usado se o silero falhar."""

    def __init__(self, noise_floor: float = 0.01) -> None:
        self.threshold = max(noise_floor * 3.0, 0.012)
        self._speaking = False

    def __call__(self, frame: np.ndarray) -> dict[str, float] | None:
        rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        if rms > self.threshold and not self._speaking:
            self._speaking = True
            return {"start": 0.0}
        if rms <= self.threshold * 0.6 and self._speaking:
            self._speaking = False
            return {"end": 0.0}
        return None

    def reset_states(self) -> None:
        self._speaking = False


class VoiceActivityDetector:
    """Wrapper fino sobre o `VADIterator` do silero-vad."""

    def __init__(self, sample_rate: int | None = None) -> None:
        self.sample_rate = sample_rate or settings.sample_rate
        self._iterator = None
        self._fallback: _EnergyVAD | None = None
        self.backend = "none"

    def load(self, noise_floor: float = 0.01) -> None:
        """Carrega o modelo silero (ou ativa o fallback de energia)."""
        if self._iterator is not None or self._fallback is not None:
            return
        try:
            from silero_vad import VADIterator, load_silero_vad

            model = load_silero_vad(onnx=False)
            self._iterator = VADIterator(
                model,
                threshold=settings.vad_threshold,
                sampling_rate=self.sample_rate,
                min_silence_duration_ms=settings.vad_silence_ms,
                speech_pad_ms=120,
            )
            self.backend = "silero"
            log.info("vad.loaded", backend="silero")
        except Exception as exc:
            self._fallback = _EnergyVAD(noise_floor)
            self.backend = "energy"
            log.warning("vad.fallback", error=str(exc), backend="energy")

    def reset(self) -> None:
        """Zera o estado interno entre gravações."""
        if self._iterator is not None:
            try:
                self._iterator.reset_states()
            except Exception:  # pragma: no cover
                pass
        if self._fallback is not None:
            self._fallback.reset_states()

    def __call__(self, frame: np.ndarray) -> dict | None:
        """Processa um frame de 512 amostras; devolve `{'start'|'end': ...}`."""
        if self._iterator is not None:
            try:
                import torch

                return self._iterator(torch.from_numpy(frame.astype(np.float32)))
            except Exception as exc:  # pragma: no cover
                log.warning("vad.error", error=str(exc))
                return None
        if self._fallback is not None:
            return self._fallback(frame)
        return None


class SpeechRecorder:
    """Grava uma fala completa do microfone, parando no silêncio."""

    def __init__(self, bus: EventBus, mic, vad: VoiceActivityDetector | None = None) -> None:  # noqa: ANN001
        self.bus = bus
        self.mic = mic
        self.vad = vad or VoiceActivityDetector(mic.sample_rate)

    def load(self, noise_floor: float = 0.01) -> None:
        self.vad.load(noise_floor)

    async def record_until_silence(
        self,
        max_seconds: float | None = None,
        initial_timeout: float = 4.0,
    ) -> Recording:
        """
        Grava até o silêncio final.

        Args:
            max_seconds: duração máxima absoluta.
            initial_timeout: tempo para o usuário começar a falar antes de
                desistir.

        Returns:
            `Recording` com o áudio float32 mono.
        """
        limit = max_seconds or settings.vad_max_record_s
        self.vad.reset()

        queue = self.mic.subscribe()
        # Padding: pega o que já estava no buffer circular antes da ativação,
        # senão a primeira sílaba se perde.
        pre = self.mic.read_last(settings.vad_prespeech_ms / 1000.0)
        collected: list[np.ndarray] = [pre] if pre.size else []

        pending = np.zeros(0, dtype=np.float32)
        started = time.monotonic()
        speech_started_at: float | None = None
        silence_started_at: float | None = None
        speech_detected = False
        stop_reason = "max_duration"

        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed > limit:
                    stop_reason = "max_duration"
                    break
                if not speech_detected and elapsed > initial_timeout:
                    stop_reason = "no_speech"
                    break

                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                collected.append(chunk)
                pending = np.concatenate([pending, chunk])

                while pending.size >= FRAME_SAMPLES:
                    frame, pending = pending[:FRAME_SAMPLES], pending[FRAME_SAMPLES:]
                    verdict = self.vad(frame)
                    if verdict is None:
                        continue
                    if "start" in verdict:
                        speech_detected = True
                        silence_started_at = None
                        if speech_started_at is None:
                            speech_started_at = time.monotonic()
                    elif "end" in verdict:
                        silence_started_at = time.monotonic()

                if silence_started_at is not None and speech_detected:
                    quiet_ms = (time.monotonic() - silence_started_at) * 1000.0
                    if quiet_ms >= max(0.0, settings.vad_silence_ms - 200):
                        stop_reason = "silence"
                        break
        finally:
            self.mic.unsubscribe(queue)

        audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
        duration = audio.size / self.mic.sample_rate
        log.info(
            "vad.recorded",
            duration_s=round(duration, 2),
            speech=speech_detected,
            reason=stop_reason,
            backend=self.vad.backend,
        )
        return Recording(
            audio=audio,
            sample_rate=self.mic.sample_rate,
            duration_s=duration,
            speech_detected=speech_detected,
            stop_reason=stop_reason,
        )


__all__ = ["FRAME_SAMPLES", "Recording", "SpeechRecorder", "VoiceActivityDetector"]
