"""
Síntese de voz com piper-tts + efeitos + reprodução.

Fluxo: texto → piper (int16 mono) → `VoiceEffects` (pedalboard) →
`sounddevice.OutputStream`.

Se o piper não estiver disponível, usa SAPI (voz nativa do Windows) para o
sistema nunca ficar mudo.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path

import numpy as np
import structlog

from config import MODELS_DIR, settings
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

        # Fallback: SAPI do Windows (pywin32).
        try:
            import win32com.client  # noqa: F401

            self.backend = "sapi"
            log.info("tts.loaded", backend="sapi")
        except Exception as exc:
            self.backend = "none"
            log.error("tts.unavailable", error=str(exc))

    async def load(self) -> None:
        """Carrega a voz (ou decide pelo fallback)."""
        if self.backend != "none":
            return
        await asyncio.to_thread(self._load_sync)

    # ------------------------------------------------------------------ #
    # Síntese
    # ------------------------------------------------------------------ #
    def _synthesize_piper(self, text: str) -> np.ndarray:
        """Sintetiza com piper, lidando com as duas APIs (1.2 e 1.3+)."""
        assert self._voice is not None
        chunks: list[np.ndarray] = []

        # API nova (piper >= 1.3): iterador de AudioChunk.
        if hasattr(self._voice, "synthesize"):
            try:
                syn_config = None
                try:
                    from piper import SynthesisConfig

                    syn_config = SynthesisConfig(
                        length_scale=settings.piper_length_scale,
                        noise_scale=settings.piper_noise_scale,
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
                length_scale=settings.piper_length_scale,
                noise_scale=settings.piper_noise_scale,
                noise_w=settings.piper_noise_w,
            ):
                chunks.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)

        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """
        Sintetiza e processa o texto.

        Returns:
            `(áudio float32 mono, sample_rate)`. Array vazio se o backend
            for SAPI (que fala direto) ou se não houver backend.
        """
        clean = normalize_text(text)
        if not clean or self.backend != "piper":
            return np.zeros(0, dtype=np.float32), self.sample_rate
        audio = self._synthesize_piper(clean)
        if audio.size == 0:
            return audio, self.sample_rate
        return self._effects.process(audio, self.sample_rate), self.sample_rate

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
    async def speak(self, text: str) -> float:
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
                if self.backend == "piper":
                    audio, rate = await asyncio.to_thread(self.synthesize, text)
                    if audio.size:
                        await asyncio.to_thread(self._play_blocking, audio, rate)
                elif self.backend == "sapi":
                    await asyncio.to_thread(self._speak_sapi, normalize_text(text))
                else:
                    log.warning("tts.no_backend", text=text[:80])
            except Exception as exc:
                log.error("tts.speak_failed", error=str(exc))
            finally:
                self.speaking = False
            elapsed = time.monotonic() - started

        log.info("tts.spoke", text=text[:120], seconds=round(elapsed, 2), backend=self.backend)
        return elapsed

    def interrupt(self) -> None:
        """Interrompe a fala em andamento."""
        self._stop.set()

    async def warmup(self) -> None:
        """Pré-carrega o modelo e sintetiza um silêncio para aquecer o ONNX."""
        await self.load()
        if self.backend == "piper":
            await asyncio.to_thread(self.synthesize, "ok")


__all__ = ["PiperEngine", "normalize_text"]
