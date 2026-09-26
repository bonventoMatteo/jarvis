"""
Detector de palmas — a ativação principal do JARVIS.

Uma palma tem três assinaturas que a distinguem de fala, música e batidas
na mesa:

1. **Pico alto e isolado** muito acima do ruído de fundo (limiar adaptativo,
   calibrado nos primeiros segundos).
2. **Ataque quase instantâneo** (< ~15 ms do início ao pico) e **decaimento
   rápido** (cai abaixo de 35 % do pico em ~60 ms).
3. **Energia concentrada em médias-altas** (2–4 kHz dominante), ao contrário
   da voz, cuja energia se concentra abaixo de 1 kHz.

Duas palmas dentro de 1,5 s (com um intervalo mínimo de 150 ms entre elas)
disparam `CLAP_DOUBLE`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np
import structlog
from scipy import signal as sp_signal

from config import settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ClapAnalysis:
    """Resultado detalhado da análise de um bloco de áudio."""

    is_clap: bool
    peak: float = 0.0
    threshold: float = 0.0
    attack_ms: float = 0.0
    decay_ratio: float = 1.0
    band_ratio: float = 0.0
    reason: str = ""


class ClapDetector:
    """
    Detector de palmas com limiar adaptativo ao ruído ambiente.

    Example:
        >>> detector = ClapDetector(sample_rate=16000)
        >>> detector.calibrate(ambient_audio)      # ~5 s de silêncio
        >>> detector.process(chunk, time.monotonic() * 1000)
        False
    """

    def __init__(self, sample_rate: int | None = None) -> None:
        self.sr = sample_rate or settings.sample_rate
        self.noise_floor: float = settings.clap_min_peak
        self.threshold_mult: float = settings.clap_threshold_mult
        self.min_gap_ms: int = settings.clap_min_gap_ms
        self.max_gap_ms: int = settings.clap_max_gap_ms
        self.debounce_ms: int = settings.clap_debounce_ms
        self.required: int = settings.clap_required

        self.last_clap_time: float = 0.0
        self.clap_count: int = 0
        self.calibrated: bool = False
        self.last_analysis: ClapAnalysis | None = None

        # Janela de análise de 2 blocos, para não perder palmas na emenda.
        self._window = np.zeros(0, dtype=np.float32)
        self._window_samples = max(int(self.sr * 0.08), 1)

        # Média móvel do ruído, para acompanhar mudanças no ambiente.
        self._noise_history: list[float] = []

    # ------------------------------------------------------------------ #
    # Calibração
    # ------------------------------------------------------------------ #
    def calibrate(self, audio_5s: np.ndarray) -> float:
        """
        Define o piso de ruído a partir de alguns segundos de ambiente.

        Usa o percentil 95 da amplitude absoluta — robusto a um estalo
        acidental durante a calibração.

        Returns:
            O piso de ruído resultante.
        """
        if audio_5s.size == 0:
            log.warning("clap.calibrate.empty")
            self.calibrated = True
            return self.noise_floor

        measured = float(np.percentile(np.abs(audio_5s), 95))
        self.noise_floor = max(measured, 1e-4)
        self.calibrated = True
        log.info(
            "clap.calibrated",
            noise_floor=round(self.noise_floor, 6),
            threshold=round(self.threshold, 6),
            seconds=round(audio_5s.size / self.sr, 2),
        )
        return self.noise_floor

    def update_noise(self, chunk: np.ndarray) -> None:
        """Acompanha lentamente a variação do ruído ambiente."""
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
        self._noise_history.append(rms)
        if len(self._noise_history) < 150:  # ~6 s
            return
        window = self._noise_history[-150:]
        self._noise_history = window
        median = float(np.median(window))
        # Só sobe/desce 5 % por atualização: evita que uma conversa longa
        # "surde" o detector de vez.
        self.noise_floor = max(1e-4, self.noise_floor * 0.95 + median * 1.3 * 0.05)

    @property
    def threshold(self) -> float:
        """Limiar absoluto de pico para considerar um candidato a palma."""
        return max(self.noise_floor * self.threshold_mult, settings.clap_min_peak)

    # ------------------------------------------------------------------ #
    # Análise
    # ------------------------------------------------------------------ #
    def analyze(self, chunk: np.ndarray) -> ClapAnalysis:
        """Avalia um bloco e explica por que ele é (ou não) uma palma."""
        if chunk.size < 64:
            return ClapAnalysis(False, reason="bloco curto")

        peak = float(np.max(np.abs(chunk)))
        threshold = self.threshold
        if peak < threshold:
            return ClapAnalysis(False, peak=peak, threshold=threshold, reason="abaixo do limiar")

        # --- Envelope de amplitude via transformada de Hilbert --------- #
        analytic = sp_signal.hilbert(chunk.astype(np.float64))
        env = np.abs(analytic)
        env_peak_idx = int(np.argmax(env))
        env_peak = float(env[env_peak_idx])
        if env_peak <= 0:
            return ClapAnalysis(False, peak=peak, threshold=threshold, reason="envelope nulo")

        # Ataque: do primeiro ponto acima de 15 % do pico até o pico.
        onset_candidates = np.nonzero(env[: env_peak_idx + 1] >= env_peak * 0.15)[0]
        onset_idx = int(onset_candidates[0]) if onset_candidates.size else env_peak_idx
        attack_ms = (env_peak_idx - onset_idx) / self.sr * 1000.0
        if attack_ms > settings.clap_attack_ms:
            return ClapAnalysis(
                False, peak=peak, threshold=threshold, attack_ms=attack_ms, reason="ataque lento"
            )

        # Decaimento: 60 ms depois do pico já deve ter caído bastante.
        decay_idx = min(env.size - 1, env_peak_idx + int(self.sr * 0.06))
        decay_ratio = float(env[decay_idx] / env_peak)
        if decay_idx > env_peak_idx and decay_ratio > 0.45:
            return ClapAnalysis(
                False,
                peak=peak,
                threshold=threshold,
                attack_ms=attack_ms,
                decay_ratio=decay_ratio,
                reason="decaimento lento",
            )

        # --- Conteúdo espectral: 2–4 kHz precisa dominar -------------- #
        windowed = chunk.astype(np.float64) * np.hanning(chunk.size)
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(chunk.size, 1.0 / self.sr)
        band = (freqs >= settings.clap_band_low_hz) & (freqs <= settings.clap_band_high_hz)
        total_energy = float(np.sum(spectrum)) + 1e-9
        band_ratio = float(np.sum(spectrum[band])) / total_energy
        if band_ratio < settings.clap_band_ratio:
            return ClapAnalysis(
                False,
                peak=peak,
                threshold=threshold,
                attack_ms=attack_ms,
                decay_ratio=decay_ratio,
                band_ratio=band_ratio,
                reason="espectro grave (voz/batida)",
            )

        return ClapAnalysis(
            True,
            peak=peak,
            threshold=threshold,
            attack_ms=attack_ms,
            decay_ratio=decay_ratio,
            band_ratio=band_ratio,
            reason="palma",
        )

    def is_clap(self, chunk: np.ndarray) -> bool:
        """Compatibilidade: versão booleana de `analyze`."""
        analysis = self.analyze(chunk)
        self.last_analysis = analysis
        return analysis.is_clap

    # ------------------------------------------------------------------ #
    # Máquina de contagem
    # ------------------------------------------------------------------ #
    def process(self, chunk: np.ndarray, now_ms: float) -> bool:
        """
        Processa um bloco. Retorna True quando a sequência completa
        (`settings.clap_required` palmas) é reconhecida.
        """
        # Mantém uma janela de ~80 ms para cobrir palmas na emenda dos blocos.
        self._window = np.concatenate([self._window, chunk])[-self._window_samples :]

        since_last = now_ms - self.last_clap_time
        if self.last_clap_time and since_last < self.debounce_ms:
            self.update_noise(chunk)
            return False

        analysis = self.analyze(self._window)
        self.last_analysis = analysis
        if not analysis.is_clap:
            self.update_noise(chunk)
            return False

        # Expira uma sequência incompleta.
        if self.last_clap_time and since_last > self.max_gap_ms:
            self.clap_count = 0

        if self.last_clap_time and since_last < self.min_gap_ms:
            return False

        self.clap_count += 1
        self.last_clap_time = now_ms
        self._window = np.zeros(0, dtype=np.float32)

        log.debug(
            "clap.detected",
            count=self.clap_count,
            peak=round(analysis.peak, 4),
            attack_ms=round(analysis.attack_ms, 2),
            band_ratio=round(analysis.band_ratio, 3),
        )

        if self.clap_count >= self.required:
            self.clap_count = 0
            return True
        return False

    def reset(self) -> None:
        """Zera a contagem (chamado ao entrar em LISTENING)."""
        self.clap_count = 0
        self._window = np.zeros(0, dtype=np.float32)


class ClapListener:
    """Task assíncrona que roda o `ClapDetector` sobre o stream do microfone."""

    def __init__(self, bus: EventBus, mic, detector: ClapDetector | None = None) -> None:
        self.bus = bus
        self.mic = mic
        self.detector = detector or ClapDetector(mic.sample_rate)
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self.enabled = settings.clap_enabled

    async def calibrate(self, seconds: float | None = None) -> float:
        """Escuta o ambiente e ajusta o limiar."""
        duration = seconds if seconds is not None else settings.calibration_seconds
        audio = await self.mic.collect(duration)
        floor = self.detector.calibrate(audio)
        self.bus.emit(
            EventType.CALIBRATED,
            source="clap",
            noise_floor=floor,
            threshold=self.detector.threshold,
        )
        return floor

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            chunk = await self._queue.get()
            if not self.enabled:
                continue
            try:
                if self.detector.process(chunk, time.monotonic() * 1000.0):
                    log.info("clap.activate")
                    self.bus.emit(
                        EventType.CLAP_DOUBLE,
                        source="clap",
                        claps=self.detector.required,
                    )
            except Exception as exc:  # pragma: no cover - nunca derruba o loop
                log.warning("clap.process_error", error=str(exc))

    def start(self) -> None:
        """Inicia a task de escuta."""
        if self._task is not None:
            return
        self._queue = self.mic.subscribe()
        self._task = asyncio.create_task(self._run(), name="clap-listener")

    async def stop(self) -> None:
        """Encerra a task e cancela a assinatura."""
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


__all__ = ["ClapAnalysis", "ClapDetector", "ClapListener"]
