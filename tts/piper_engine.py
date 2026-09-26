"""
Síntese de voz com piper-tts + efeitos + reprodução.

Fluxo: texto → piper (int16 mono) → `VoiceEffects` (pedalboard) →
`sounddevice.OutputStream`.

Se o piper não estiver disponível, usa SAPI (voz nativa do Windows) para o
sistema nunca ficar mudo.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import structlog

from config import DATA_DIR, IS_WINDOWS, MODELS_DIR, settings
from tts.cloud import cloud_available, synthesize_edge, synthesize_elevenlabs
from tts.effects import VoiceEffects

log = structlog.get_logger(__name__)

#: Substituições para o piper pronunciar melhor em pt-BR.
_PRONUNCIATION = (
    (re.compile(r"\bJARVIS\b", re.IGNORECASE), "Jarvis"),
    (re.compile(r"\bCPU\b"), "cê pê u"),
    (re.compile(r"\bRAM\b"), "ram"),
    (re.compile(r"\bGPU\b"), "gê pê u"),
    (re.compile(r"\bOK\b", re.IGNORECASE), "ok"),
    (re.compile(r"\bPC\b"), "pê cê"),
    (re.compile(r"\bURL\b"), "u erre ele"),
    (re.compile(r"\bWi-?Fi\b", re.IGNORECASE), "uaifai"),
    (re.compile(r"%"), " por cento"),
    (re.compile(r"\s+"), " "),
)


#: Presets de "emoção": multiplicadores sobre (length_scale, noise_scale).
MOODS: dict[str, tuple[float, float]] = {
    "neutral": (1.00, 1.00),
    "calm": (1.10, 0.75),
    "urgent": (0.90, 1.12),
    "confirm": (0.96, 0.90),
}


def normalize_text(text: str) -> str:
    """Limpa markdown e expande siglas antes de sintetizar."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    for pattern, replacement in _PRONUNCIATION:
        text = pattern.sub(replacement, text)
    return text.strip()


class PiperEngine:
    """Motor de TTS com playback interrompível."""

    def __init__(self, voice_name: str | None = None) -> None:
        self.voice_name = voice_name or settings.piper_voice
        self.sample_rate = 22050
        self.backend = "none"
        self._voice = None
        self._effects = VoiceEffects(self.sample_rate)
        self._stop = threading.Event()
        self._lock = asyncio.Lock()
        self.speaking = False
        self.last_text = ""
        self._cache: dict[tuple[str, str], tuple[np.ndarray, int]] = {}
        self._fallback_tool = ""
        # Motor online (edge/elevenlabs) com piper como reserva offline.
        self.cloud = settings.tts_engine if cloud_available(settings.tts_engine) else ""
        self._cloud_down_until = 0.0
        self._disk_cache = DATA_DIR / "tts_cache"

    @property
    def label(self) -> str:
        """Descrição do motor ativo para o painel."""
        if self.cloud and time.monotonic() >= self._cloud_down_until:
            voice = settings.edge_voice if self.cloud == "edge" else "elevenlabs"
            return f"{voice} (reserva: {self.backend})"
        return self.backend

    @property
    def neural(self) -> bool:
        """True se há um motor que gera áudio (online ou piper)."""
        return bool(self.cloud) or self.backend == "piper"

    # ------------------------------------------------------------------ #
    # Carregamento
    # ------------------------------------------------------------------ #
    @property
    def model_path(self) -> Path:
        return MODELS_DIR / "piper" / f"{self.voice_name}.onnx"

    def _load_sync(self) -> None:
        model = self.model_path
        config = Path(f"{model}.json")
        try:
            from piper import PiperVoice

            if not model.exists():
                raise FileNotFoundError(
                    f"modelo piper ausente em {model} — rode `install.ps1` ou "
                    f"`python -m scripts.download_models`"
                )
            self._voice = PiperVoice.load(str(model), config_path=str(config) if config.exists() else None)
            self.sample_rate = int(getattr(self._voice.config, "sample_rate", 22050))
            self.backend = "piper"
            self._effects.build(self.sample_rate)
            log.info("tts.loaded", backend="piper", voice=self.voice_name, rate=self.sample_rate)
            return
        except Exception as exc:
            log.warning("tts.piper_unavailable", error=str(exc))

        # Fallback Linux: espeak-ng / speech-dispatcher.
        if not IS_WINDOWS:
            for tool in ("espeak-ng", "spd-say"):
                if shutil.which(tool):
                    self.backend = "espeak"
                    self._fallback_tool = tool
                    log.info("tts.loaded", backend=tool)
                    return
            self.backend = "none"
            log.error("tts.unavailable", hint="rode python -m scripts.download_models ou instale espeak-ng")
            return

        # Fallback Windows: SAPI (pywin32).
        try:
            import win32com.client  # noqa: F401

            self.backend = "sapi"
            log.info("tts.loaded", backend="sapi")
        except Exception as exc:
            self.backend = "none"
            log.error("tts.unavailable", error=str(exc))

    async def load(self) -> None:
        """Carrega a voz (ou decide pelo fallback)."""
        if self.backend != "none" or getattr(self, "_loaded", False):
            return
        self._loaded = True
        await asyncio.to_thread(self._load_sync)

    # ------------------------------------------------------------------ #
    # Síntese
    # ------------------------------------------------------------------ #
    def _synthesize_piper(self, text: str, mood: str = "neutral") -> np.ndarray:
        """Sintetiza com piper, lidando com as duas APIs (1.2 e 1.3+)."""
        assert self._voice is not None
        chunks: list[np.ndarray] = []
        length_mult, noise_mult = MOODS.get(mood, MOODS["neutral"])
        length_scale = settings.piper_length_scale * length_mult
        noise_scale = settings.piper_noise_scale * noise_mult

        # API nova (piper >= 1.3): iterador de AudioChunk.
        if hasattr(self._voice, "synthesize"):
            try:
                syn_config = None
                try:
                    from piper import SynthesisConfig

                    syn_config = SynthesisConfig(
                        length_scale=length_scale,
                        noise_scale=noise_scale,
                        noise_w_scale=settings.piper_noise_w,
                    )
                except Exception:
                    syn_config = None

                stream = (
                    self._voice.synthesize(text, syn_config=syn_config)
                    if syn_config is not None
                    else self._voice.synthesize(text)
                )
                for chunk in stream:
                    if isinstance(chunk, (bytes, bytearray)):
                        chunks.append(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0)
                        continue
                    array = getattr(chunk, "audio_float_array", None)
                    if array is not None:
                        chunks.append(np.asarray(array, dtype=np.float32).reshape(-1))
                    else:
                        raw = getattr(chunk, "audio_int16_bytes", b"")
                        chunks.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
                    rate = getattr(chunk, "sample_rate", None)
                    if rate:
                        self.sample_rate = int(rate)
                if chunks:
                    return np.concatenate(chunks)
            except TypeError:
                chunks.clear()
            except Exception as exc:
                log.warning("tts.synthesize_new_api_failed", error=str(exc))
                chunks.clear()

        # API antiga: bytes PCM crus.
        if hasattr(self._voice, "synthesize_stream_raw"):
            for raw in self._voice.synthesize_stream_raw(
                text,
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w=settings.piper_noise_w,
            ):
                chunks.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)

        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def _disk_key(self, clean: str, mood: str) -> Path:
        tag = "|".join(
            (self.cloud, settings.edge_voice, settings.elevenlabs_voice_id, settings.edge_rate, settings.edge_pitch)
        )
        digest = hashlib.md5(f"{tag}|{mood}|{clean}".encode(), usedforsecurity=False).hexdigest()
        return self._disk_cache / f"{digest}.npz"

    def _synthesize_raw(self, clean: str, mood: str) -> tuple[np.ndarray, int, bool]:
        """
        Áudio cru (sem efeitos): motor online primeiro, piper na falha.

        Returns:
            `(áudio, taxa, veio_do_online)`
        """
        if self.cloud and time.monotonic() >= self._cloud_down_until:
            path = self._disk_key(clean, mood)
            if path.exists():
                try:
                    with np.load(path) as stored:
                        return stored["audio"], int(stored["rate"]), True
                except (OSError, ValueError, KeyError):
                    path.unlink(missing_ok=True)
            try:
                audio, rate = (synthesize_edge if self.cloud == "edge" else synthesize_elevenlabs)(clean, mood)
                if audio.size:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    np.savez(path, audio=audio, rate=rate)
                    return audio, rate, True
            except Exception as exc:
                # Sem internet/serviço fora: usa o piper por 2 minutos antes de tentar de novo.
                self._cloud_down_until = time.monotonic() + 120.0
                log.warning("tts.cloud_failed", engine=self.cloud, error=str(exc)[:200], fallback=self.backend)
        if self.backend == "piper":
            return self._synthesize_piper(clean, mood), self.sample_rate, False
        return np.zeros(0, dtype=np.float32), self.sample_rate, False

    def synthesize(self, text: str, mood: str = "neutral") -> tuple[np.ndarray, int]:
        """
        Sintetiza e processa o texto (com cache em memória por texto+mood).

        Returns:
            `(áudio float32 mono, sample_rate)`. Array vazio se só houver
            SAPI/espeak (que falam direto) ou nenhum backend.
        """
        clean = normalize_text(text)
        if not clean or not self.neural:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        key = (clean, mood)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        audio, rate, _online = self._synthesize_raw(clean, mood)
        if audio.size == 0:
            return audio, rate
        processed = self._effects.process(audio, rate)
        # Só frases curtas vão para o cache em memória (falas de estado se repetem).
        if len(clean) <= 80:
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (processed, rate)
        return processed, rate

    async def preload(self, phrases: list[str] | tuple[str, ...], mood: str = "neutral") -> int:
        """Pré-sintetiza frases fixas no boot (ativação sem latência)."""
        await self.load()
        if not self.neural:
            return 0
        count = 0
        for phrase in phrases:
            try:
                await asyncio.to_thread(self.synthesize, phrase, mood)
                count += 1
            except Exception as exc:
                log.warning("tts.preload_failed", phrase=phrase, error=str(exc))
        log.info("tts.preloaded", count=count)
        return count

    # ------------------------------------------------------------------ #
    # Reprodução
    # ------------------------------------------------------------------ #
    def _play_blocking(self, audio: np.ndarray, sample_rate: int) -> None:
        """Toca o áudio em blocos, checando o pedido de interrupção."""
        import sounddevice as sd

        block = 1024
        with sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=settings.output_device,
        ) as stream:
            for start in range(0, audio.size, block):
                if self._stop.is_set():
                    break
                piece = audio[start : start + block]
                if piece.size < block:
                    piece = np.pad(piece, (0, block - piece.size))
                stream.write(piece.reshape(-1, 1))

    def _speak_espeak(self, text: str) -> None:
        """Fallback Linux: voz sintética do espeak-ng / speech-dispatcher."""
        if self._fallback_tool == "espeak-ng":
            cmd = ["espeak-ng", "-v", "pt-br", "-s", "165", text]
        else:
            cmd = ["spd-say", "-w", "-l", "pt", text]
        try:
            subprocess.run(cmd, check=False, timeout=60, capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("tts.espeak_failed", error=str(exc))

    def _speak_sapi(self, text: str) -> None:
        """Fallback: voz nativa do Windows."""
        try:
            import pythoncom  # type: ignore[import-not-found]
            import win32com.client

            pythoncom.CoInitialize()
            try:
                voice = win32com.client.Dispatch("SAPI.SpVoice")
                for candidate in voice.GetVoices():
                    description = candidate.GetDescription()
                    if "Portug" in description or "Brazil" in description or "Maria" in description:
                        voice.Voice = candidate
                        break
                voice.Speak(text)
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:  # pragma: no cover
            log.error("tts.sapi_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    async def speak(self, text: str, mood: str = "neutral") -> float:
        """
        Fala um texto e aguarda o fim.

        Returns:
            Duração em segundos (0.0 se nada foi falado).
        """
        if not text or not text.strip():
            return 0.0

        await self.load()
        async with self._lock:
            self._stop.clear()
            self.speaking = True
            self.last_text = text
            started = time.monotonic()
            try:
                audio = np.zeros(0, dtype=np.float32)
                if self.neural:
                    audio, rate = await asyncio.to_thread(self.synthesize, text, mood)
                    if audio.size:
                        await asyncio.to_thread(self._play_blocking, audio, rate)
                if audio.size == 0:
                    # Sem voz neural (ou ela falhou): vozes do sistema.
                    if self.backend == "sapi":
                        await asyncio.to_thread(self._speak_sapi, normalize_text(text))
                    elif self.backend == "espeak":
                        await asyncio.to_thread(self._speak_espeak, normalize_text(text))
                    else:
                        log.warning("tts.no_backend", text=text[:80])
            except Exception as exc:
                log.error("tts.speak_failed", error=str(exc))
            finally:
                self.speaking = False
            elapsed = time.monotonic() - started

        log.info("tts.spoke", text=text[:120], seconds=round(elapsed, 2), engine=self.label)
        return elapsed

    def interrupt(self) -> None:
        """Interrompe a fala em andamento."""
        self._stop.set()

    async def warmup(self) -> None:
        """Pré-carrega o modelo e sintetiza um silêncio para aquecer o ONNX."""
        await self.load()
        if self.backend == "piper":
            await asyncio.to_thread(self.synthesize, "ok")


__all__ = ["MOODS", "PiperEngine", "normalize_text"]
