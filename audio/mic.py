"""
Captura contínua de microfone com buffer circular e fan-out.

Um único `sounddevice.InputStream` roda o tempo todo em 16 kHz mono. Cada
bloco (40 ms por padrão) é:
  1. escrito no buffer circular (usado para calibração e padding pré-fala);
  2. distribuído para os assinantes (palmas, wake word, VAD);
  3. resumido em nível RMS/pico para o dashboard.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Iterable

import numpy as np
import structlog

from config import settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


class Microphone:
    """Captura de áudio contínua, thread-safe, com assinantes assíncronos."""

    def __init__(self, bus: EventBus, sample_rate: int | None = None) -> None:
        self.bus = bus
        self.sample_rate = sample_rate or settings.sample_rate
        self.block_size = int(self.sample_rate * settings.block_ms / 1000)

        self._ring = np.zeros(int(self.sample_rate * settings.ring_seconds), dtype=np.float32)
        self._ring_pos = 0
        self._ring_filled = 0
        self._lock = threading.Lock()

        self._subscribers: list[asyncio.Queue[np.ndarray]] = []
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._paused = False

        self.level: float = 0.0
        self.peak: float = 0.0
        self.waveform: deque[float] = deque(maxlen=64)
        self._level_counter = 0

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Abre o stream de entrada."""
        if self._running:
            return
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.block_size,
                device=settings.input_device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            log.error("mic.start_failed", error=str(exc))
            raise RuntimeError(
                "Não consegui abrir o microfone. Verifique o dispositivo de entrada "
                "(`python -m scripts.audio_devices`) e a permissão de microfone do Windows."
            ) from exc

        self._running = True
        log.info(
            "mic.started",
            rate=self.sample_rate,
            block=self.block_size,
            device=settings.input_device,
        )

    def stop(self) -> None:
        """Fecha o stream de entrada."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # pragma: no cover
                pass
            self._stream = None
        log.info("mic.stopped")

    @property
    def running(self) -> bool:
        return self._running

    def pause(self) -> None:
        """Para de distribuir blocos (usado enquanto o TTS fala)."""
        self._paused = True

    def resume(self) -> None:
        """Volta a distribuir blocos e limpa as filas acumuladas."""
        self._paused = False
        for queue in self._subscribers:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    break

    # ------------------------------------------------------------------ #
    # Assinatura
    # ------------------------------------------------------------------ #
    def subscribe(self, maxsize: int = 64) -> asyncio.Queue[np.ndarray]:
        """Cria uma fila que recebe cada bloco capturado."""
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[np.ndarray]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    # ------------------------------------------------------------------ #
    # Callback de áudio
    # ------------------------------------------------------------------ #
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:  # pragma: no cover - overflow/underflow ocasional
            log.debug("mic.status", status=str(status))

        chunk = np.ascontiguousarray(indata[:, 0], dtype=np.float32).copy()
        self._write_ring(chunk)

        peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
        self.peak = peak
        self.level = rms
        self.waveform.append(peak)

        if self._paused or self._loop is None or self._loop.is_closed():
            return

        for queue in list(self._subscribers):
            try:
                self._loop.call_soon_threadsafe(self._offer, queue, chunk)
            except RuntimeError:  # pragma: no cover - loop encerrando
                return

        # Publica nível para a UI ~5x/s em vez de 25x/s.
        self._level_counter += 1
        if self._level_counter % 5 == 0:
            self.bus.emit_threadsafe(
                EventType.AUDIO_LEVEL,
                source="mic",
                rms=rms,
                peak=peak,
                waveform=list(self.waveform),
            )

    @staticmethod
    def _offer(queue: asyncio.Queue[np.ndarray], chunk: np.ndarray) -> None:
        """Enfileira descartando o bloco mais antigo se a fila estiver cheia."""
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(chunk)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass

    # ------------------------------------------------------------------ #
    # Buffer circular
    # ------------------------------------------------------------------ #
    def _write_ring(self, chunk: np.ndarray) -> None:
        size = self._ring.size
        n = chunk.size
        if n >= size:
            with self._lock:
                self._ring[:] = chunk[-size:]
                self._ring_pos = 0
                self._ring_filled = size
            return

        with self._lock:
            end = self._ring_pos + n
            if end <= size:
                self._ring[self._ring_pos : end] = chunk
            else:
                split = size - self._ring_pos
                self._ring[self._ring_pos :] = chunk[:split]
                self._ring[: n - split] = chunk[split:]
            self._ring_pos = end % size
            self._ring_filled = min(size, self._ring_filled + n)

    def read_last(self, seconds: float) -> np.ndarray:
        """Devolve os últimos `seconds` de áudio capturado (pode vir menor)."""
        want = int(self.sample_rate * seconds)
        with self._lock:
            available = min(want, self._ring_filled)
            if available <= 0:
                return np.zeros(0, dtype=np.float32)
            start = (self._ring_pos - available) % self._ring.size
            if start + available <= self._ring.size:
                return self._ring[start : start + available].copy()
            split = self._ring.size - start
            return np.concatenate([self._ring[start:], self._ring[: available - split]])

    async def collect(self, seconds: float, queue: asyncio.Queue[np.ndarray] | None = None) -> np.ndarray:
        """Coleta `seconds` de áudio novo a partir de agora."""
        own = queue is None
        queue = queue or self.subscribe()
        needed = int(self.sample_rate * seconds)
        parts: list[np.ndarray] = []
        total = 0
        try:
            while total < needed:
                chunk = await asyncio.wait_for(queue.get(), timeout=seconds + 5.0)
                parts.append(chunk)
                total += chunk.size
        except TimeoutError:
            log.warning("mic.collect_timeout", seconds=seconds)
        finally:
            if own:
                self.unsubscribe(queue)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)[:needed]

    async def __aenter__(self) -> Microphone:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.stop()


def list_devices() -> Iterable[str]:
    """Lista os dispositivos de áudio disponíveis (para diagnóstico)."""
    import sounddevice as sd

    for index, device in enumerate(sd.query_devices()):
        kind = []
        if device["max_input_channels"] > 0:
            kind.append("in")
        if device["max_output_channels"] > 0:
            kind.append("out")
        yield f"[{index}] {device['name']} ({'/'.join(kind) or '-'}) @ {int(device['default_samplerate'])} Hz"


__all__ = ["Microphone", "list_devices"]
