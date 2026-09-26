"""
Saída de áudio para efeitos sonoros.

Um único `sounddevice.OutputStream` com callback mixa:
  * one-shots (activate.wav, success.wav, error.wav, ...)
  * uma trilha ambiente em loop (thinking.wav)

Manter um só stream evita disputa pelo dispositivo no Windows (WASAPI) e
permite tocar um bipe por cima do hum de "processando".
"""
from __future__ import annotations

import asyncio
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import structlog

from assets.generate_assets import ensure_assets
from config import ASSETS_DIR, settings

log = structlog.get_logger(__name__)

MIX_RATE = 44100
"""Taxa do mixer de efeitos."""

_BLOCK = 512


def load_wav(path: Path, target_rate: int = MIX_RATE) -> np.ndarray:
    """
    Lê um WAV mono/estéreo e devolve float32 mono na taxa alvo.

    Raises:
        FileNotFoundError: se o arquivo não existir.
    """
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:  # pragma: no cover - formatos exóticos
        raise ValueError(f"Largura de amostra não suportada: {width * 8} bits")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    if rate != target_rate and data.size:
        duration = data.size / rate
        new_len = max(1, int(duration * target_rate))
        data = np.interp(
            np.linspace(0.0, data.size - 1, new_len, dtype=np.float64),
            np.arange(data.size, dtype=np.float64),
            data,
        ).astype(np.float32)

    return np.ascontiguousarray(data, dtype=np.float32)


@dataclass
class _Voice:
    """Um one-shot em reprodução."""

    data: np.ndarray
    gain: float
    position: int = 0
    done: threading.Event = field(default_factory=threading.Event)


class AudioOutput:
    """Mixer de efeitos sonoros com uma trilha ambiente em loop."""

    def __init__(self) -> None:
        self._voices: list[_Voice] = []
        self._ambient: np.ndarray | None = None
        self._ambient_pos = 0
        self._ambient_gain = 0.0
        self._ambient_target = 0.0
        self._lock = threading.Lock()
        self._stream = None
        self._cache: dict[str, np.ndarray] = {}
        self._muted = False
        self._started = False

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Abre o stream de saída. Falhas não são fatais (roda mudo)."""
        if self._started:
            return
        ensure_assets()
        try:
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=MIX_RATE,
                channels=1,
                dtype="float32",
                blocksize=_BLOCK,
                device=settings.output_device,
                callback=self._callback,
            )
            self._stream.start()
            self._started = True
            log.info("audio.output.started", rate=MIX_RATE)
        except Exception as exc:  # pragma: no cover - depende do hardware
            self._muted = True
            self._started = True
            log.error("audio.output.failed", error=str(exc))

    def stop(self) -> None:
        """Fecha o stream e libera as vozes pendentes."""
        with self._lock:
            for voice in self._voices:
                voice.done.set()
            self._voices.clear()
            self._ambient = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # pragma: no cover
                pass
            self._stream = None
        self._started = False

    # ------------------------------------------------------------------ #
    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        """Callback de áudio (roda em thread de alta prioridade)."""
        mix = np.zeros(frames, dtype=np.float32)

        with self._lock:
            # Trilha ambiente em loop, com rampa suave de ganho.
            if self._ambient is not None and self._ambient.size:
                buffer = self._ambient
                pos = self._ambient_pos
                idx = (np.arange(frames) + pos) % buffer.size
                ramp = np.linspace(self._ambient_gain, self._ambient_target, frames, dtype=np.float32)
                mix += buffer[idx] * ramp
                self._ambient_pos = int((pos + frames) % buffer.size)
                self._ambient_gain = self._ambient_target
                if self._ambient_target <= 0.0:
                    self._ambient = None

            # One-shots.
            finished: list[_Voice] = []
            for voice in self._voices:
                chunk = voice.data[voice.position : voice.position + frames]
                if chunk.size:
                    mix[: chunk.size] += chunk * voice.gain
                voice.position += frames
                if voice.position >= voice.data.size:
                    finished.append(voice)
            for voice in finished:
                voice.done.set()
                self._voices.remove(voice)

        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:, 0] = mix

    # ------------------------------------------------------------------ #
    def _resolve(self, name_or_path: str | Path) -> np.ndarray | None:
        """Carrega (com cache) um efeito por nome (`"activate"`) ou caminho."""
        key = str(name_or_path)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = Path(name_or_path)
        if not path.suffix:
            path = ASSETS_DIR / f"{path.name}.wav"
        if not path.is_absolute() and not path.exists():
            path = ASSETS_DIR / path.name
        if not path.exists():
            ensure_assets()
        if not path.exists():
            log.warning("audio.sound.missing", path=str(path))
            return None

        try:
            data = load_wav(path)
        except Exception as exc:
            log.warning("audio.sound.load_failed", path=str(path), error=str(exc))
            return None

        self._cache[key] = data
        return data

    # ------------------------------------------------------------------ #
    def play(self, name: str | Path, gain: float | None = None) -> threading.Event | None:
        """Dispara um efeito sonoro. Retorna um Event sinalizado ao terminar."""
        if self._muted or not self._started:
            return None
        data = self._resolve(name)
        if data is None:
            return None
        voice = _Voice(data=data, gain=gain if gain is not None else settings.sfx_volume)
        with self._lock:
            # Evita empilhar vozes demais (ex.: cliques repetidos).
            if len(self._voices) >= 8:
                oldest = self._voices.pop(0)
                oldest.done.set()
            self._voices.append(voice)
        return voice.done

    async def play_async(self, name: str | Path, gain: float | None = None, wait: bool = False) -> None:
        """Versão assíncrona de `play`; com `wait=True` aguarda o fim do som."""
        done = self.play(name, gain)
        if wait and done is not None:
            await asyncio.get_running_loop().run_in_executor(None, done.wait, 10.0)

    # ------------------------------------------------------------------ #
    def start_ambient(self, name: str | Path = "thinking", gain: float | None = None) -> None:
        """Inicia (ou troca) a trilha ambiente em loop."""
        if self._muted or not self._started:
            return
        data = self._resolve(name)
        if data is None:
            return
        with self._lock:
            self._ambient = data
            self._ambient_pos = 0
            self._ambient_target = gain if gain is not None else settings.ambient_volume

    def stop_ambient(self) -> None:
        """Faz fade-out e para a trilha ambiente."""
        with self._lock:
            self._ambient_target = 0.0

    @property
    def ambient_active(self) -> bool:
        return self._ambient is not None

    def __enter__(self) -> AudioOutput:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


__all__ = ["MIX_RATE", "AudioOutput", "load_wav"]
