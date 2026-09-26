"""
`JarvisVoice` — voz do Jarvis: piper (subprocess) + pedalboard + sounddevice.

Pipeline de uma frase nova:

    texto ──stdin──▶ python -m piper --output-raw ──int16──▶ float32
          ──▶ cadeia pedalboard ("IA de cinema") ──▶ cache .npy ──▶ alto-falante

Frases repetidas saem direto do cache (memória → disco), em milissegundos.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from pedalboard import (
    Compressor,
    Gain,
    HighpassFilter,
    Limiter,
    LowpassFilter,
    PeakFilter,
    Pedalboard,
    Reverb,
)

from .config import CACHE_DIR, SAMPLE_RATE, default_model_path

logger = logging.getLogger(__name__)

#: mood -> (length_scale, noise_scale) do piper.
MOODS: dict[str, tuple[float, float]] = {
    "neutral": (1.05, 0.667),
    "calm": (1.15, 0.500),
    "urgent": (0.95, 0.750),
    "confirm": (1.00, 0.600),
}

#: Frases fixas pré-carregadas no boot.
COMMON_PHRASES: list[str] = [
    "Sim senhor.",
    "Pronto.",
    "Executando.",
    "Concluído senhor.",
    "Um momento.",
    "Não entendi, senhor.",
    "Feito.",
    "Escutando.",
    "Sistemas online. Ao seu dispor.",
    "Desculpe, não consegui.",
    "Sim?",
    "Diga.",
    "Certo.",
    "Já foi feito.",
    "Verificando.",
]

#: Muda sempre que a cadeia de efeitos mudar: invalida o cache antigo.
_CHAIN_VERSION = "v1"
_MEMORY_CACHE_SIZE = 128


def build_chain() -> Pedalboard:
    """Cadeia fixa de efeitos que dá o timbre "IA de cinema"."""
    return Pedalboard(
        [
            HighpassFilter(cutoff_frequency_hz=100.0),
            LowpassFilter(cutoff_frequency_hz=8500.0),
            PeakFilter(cutoff_frequency_hz=180.0, gain_db=1.5, q=1.0),
            PeakFilter(cutoff_frequency_hz=2800.0, gain_db=2.5, q=1.4),
            PeakFilter(cutoff_frequency_hz=5000.0, gain_db=1.0, q=2.0),
            Compressor(threshold_db=-16.0, ratio=3.5, attack_ms=3.0, release_ms=90.0),
            Reverb(room_size=0.12, damping=0.75, wet_level=0.06, dry_level=0.94, width=0.9),
            Gain(gain_db=2.0),
            Limiter(threshold_db=-1.0, release_ms=80.0),
        ]
    )


class JarvisVoice:
    """Sintetizador + player da voz do Jarvis, com cache persistente."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        """
        Args:
            model_path: caminho do `.onnx` da voz (padrão: `models/pt_BR-faber-medium.onnx`).
            cache_dir: pasta do cache `.npy` (padrão: `cache/`).
            sample_rate: taxa usada se o `.onnx.json` não informar a sua.

        Raises:
            FileNotFoundError: se o modelo não existir.
        """
        self.model_path = Path(model_path) if model_path else default_model_path()
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Modelo Piper não encontrado em {self.model_path}. "
                "Baixe com: python download_voice.py"
            )
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sample_rate = self._read_sample_rate(sample_rate)
        self._board = build_chain()
        self._memory: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _read_sample_rate(self, fallback: int) -> int:
        """Lê a taxa real da voz no `.onnx.json` (vozes "low" usam 16 kHz)."""
        config_path = Path(f"{self.model_path}.json")
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return int(data["audio"]["sample_rate"])
        except (OSError, KeyError, ValueError, TypeError):
            return fallback

    def _cache_key(self, text: str, mood: str = "neutral") -> Path:
        """Arquivo `.npy` do cache para (texto, mood) nesta voz."""
        raw = f"{_CHAIN_VERSION}|{self.model_path.stem}|{mood}|{text.strip()}"
        digest = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()
        return self.cache_dir / f"{digest}.npy"

    # ------------------------------------------------------------------ #
    def _run_piper(self, text: str, mood: str) -> np.ndarray:
        """Chama o piper em subprocess e devolve o áudio cru em float32."""
        length_scale, noise_scale = MOODS.get(mood, MOODS["neutral"])
        command = [
            sys.executable,
            "-m",
            "piper",
            "--model",
            str(self.model_path),
            "--output-raw",
            "--length-scale",
            f"{length_scale}",
            "--noise-scale",
            f"{noise_scale}",
        ]
        # Garante UTF-8 no stdin do filho (no Windows o padrão é cp1252 e os
        # acentos chegariam corrompidos).
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            completed = subprocess.run(
                command,
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.CalledProcessError as exc:
            logger.error("piper falhou (código %s): %s", exc.returncode, exc.stderr.decode("utf-8", "replace"))
            raise
        pcm = np.frombuffer(completed.stdout, dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0

    def _synthesize(self, text: str, mood: str = "neutral") -> np.ndarray:
        """Sintetiza com piper e aplica a cadeia de efeitos."""
        dry = self._run_piper(text, mood)
        if dry.size == 0:
            return dry
        wet = self._board(dry.reshape(1, -1), self.sample_rate)
        audio = np.asarray(wet, dtype=np.float32).reshape(-1)
        return np.clip(audio, -1.0, 1.0)

    def render(self, text: str, mood: str = "neutral") -> np.ndarray:
        """Áudio pronto para tocar (memória → disco → síntese)."""
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        path = self._cache_key(text, mood)
        key = path.stem

        with self._lock:
            cached = self._memory.get(key)
            if cached is not None:
                self._memory.move_to_end(key)
                return cached

        if path.exists():
            try:
                audio = np.load(path, allow_pickle=False)
            except (OSError, ValueError) as exc:
                logger.warning("cache corrompido em %s (%s); ressintetizando", path.name, exc)
                audio = self._synthesize(text, mood)
                np.save(path, audio)
        else:
            audio = self._synthesize(text, mood)
            np.save(path, audio)
            logger.debug("sintetizado e salvo em cache: %r (%s)", text, mood)

        with self._lock:
            self._memory[key] = audio
            if len(self._memory) > _MEMORY_CACHE_SIZE:
                self._memory.popitem(last=False)
        return audio

    # ------------------------------------------------------------------ #
    def speak(self, text: str, mood: str = "neutral", block: bool = True) -> None:
        """Fala o texto. Com `block=False` retorna assim que o áudio começa."""
        import sounddevice as sd

        audio = self.render(text, mood)
        if audio.size == 0:
            return
        sd.play(audio, self.sample_rate, blocking=block)

    def speak_async(self, text: str, mood: str = "neutral") -> asyncio.Task[None]:
        """Agenda a fala numa thread e devolve a `asyncio.Task` (use dentro de um loop)."""
        return asyncio.get_running_loop().create_task(asyncio.to_thread(self.speak, text, mood, True))

    def stop(self) -> None:
        """Interrompe o que estiver tocando."""
        import sounddevice as sd

        sd.stop()

    def preload(self, phrases: list[str] | None = None, mood: str = "neutral") -> int:
        """Gera o cache de várias frases de uma vez. Devolve quantas ficaram prontas."""
        ready = 0
        for phrase in phrases if phrases is not None else COMMON_PHRASES:
            try:
                self.render(phrase, mood)
                ready += 1
            except subprocess.CalledProcessError:
                logger.warning("falha ao pré-carregar %r", phrase)
        return ready

    def clear_cache(self) -> int:
        """Apaga o cache em disco e em memória. Devolve quantos arquivos saíram."""
        removed = 0
        for path in self.cache_dir.glob("*.npy"):
            path.unlink(missing_ok=True)
            removed += 1
        with self._lock:
            self._memory.clear()
        return removed


__all__ = ["COMMON_PHRASES", "MOODS", "JarvisVoice", "build_chain"]
