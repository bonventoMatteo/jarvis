"""
Transcrição com faster-whisper.

Detecta CUDA automaticamente e cai para CPU int8 quando não há GPU.

Nota sobre modelos: `distil-large-v3` é **somente inglês**. Para português
use `large-v3-turbo` (padrão, rápido e multilíngue), `large-v3`, `medium`
ou `small`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import numpy as np
import structlog

from config import MODELS_DIR, settings

log = structlog.get_logger(__name__)

#: Modelos que só transcrevem inglês — avisamos se forem usados com pt.
ENGLISH_ONLY = {"distil-large-v3", "distil-large-v2", "distil-medium.en", "distil-small.en"}


@dataclass(slots=True)
class Transcription:
    """Resultado de uma transcrição."""

    text: str
    language: str = ""
    duration_s: float = 0.0
    latency_s: float = 0.0
    avg_logprob: float = 0.0
    segments: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _pick_device() -> tuple[str, str]:
    """Escolhe (device, compute_type) conforme a configuração e o hardware."""
    device = settings.whisper_device
    if device == "auto":
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    compute = settings.whisper_compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


class WhisperEngine:
    """Wrapper assíncrono sobre `faster_whisper.WhisperModel`."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.whisper_model
        self.device = "cpu"
        self.compute_type = "int8"
        self._model = None
        self._lock = asyncio.Lock()
        self.ready = False

    # ------------------------------------------------------------------ #
    def _load_sync(self) -> None:
        from faster_whisper import WhisperModel

        self.device, self.compute_type = _pick_device()

        if self.model_name in ENGLISH_ONLY and settings.language != "en":
            log.warning(
                "stt.english_only_model",
                model=self.model_name,
                language=settings.language,
                hint="use large-v3-turbo para português",
            )

        started = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(MODELS_DIR / "whisper"),
            )
        except Exception as exc:
            if self.device == "cuda":
                log.warning("stt.cuda_failed", error=str(exc), fallback="cpu/int8")
                self.device, self.compute_type = "cpu", "int8"
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(MODELS_DIR / "whisper"),
                )
            else:
                raise

        self.ready = True
        log.info(
            "stt.loaded",
            model=self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            load_s=round(time.monotonic() - started, 2),
        )

    async def load(self) -> None:
        """Carrega o modelo (lento na primeira vez: faz download)."""
        if self.ready:
            return
        async with self._lock:
            if not self.ready:
                await asyncio.to_thread(self._load_sync)

    # ------------------------------------------------------------------ #
    def _transcribe_sync(self, audio: np.ndarray, language: str | None) -> Transcription:
        assert self._model is not None
        started = time.monotonic()

        segments, info = self._model.transcribe(
            audio.astype(np.float32),
            language=language,
            beam_size=settings.whisper_beam_size,
            vad_filter=settings.whisper_vad_filter,
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,
            temperature=0.0,
        )

        texts: list[str] = []
        logprobs: list[float] = []
        for segment in segments:
            texts.append(segment.text.strip())
            logprobs.append(float(getattr(segment, "avg_logprob", 0.0)))

        text = " ".join(part for part in texts if part).strip()
        return Transcription(
            text=text,
            language=getattr(info, "language", language or ""),
            duration_s=float(getattr(info, "duration", audio.size / settings.sample_rate)),
            latency_s=time.monotonic() - started,
            avg_logprob=float(np.mean(logprobs)) if logprobs else 0.0,
            segments=texts,
        )

    async def transcribe(self, audio: np.ndarray, language: str | None = None) -> Transcription:
        """
        Transcreve um array float32 mono em 16 kHz.

        Returns:
            `Transcription`; `text` vazio se o áudio for curto demais ou
            se o modelo não devolver nada.
        """
        if audio.size < settings.sample_rate * 0.2:
            return Transcription(text="", duration_s=audio.size / settings.sample_rate)

        await self.load()
        lang = language if language is not None else settings.language
        if self.model_name in ENGLISH_ONLY:
            lang = "en"

        try:
            result = await asyncio.to_thread(self._transcribe_sync, audio, lang)
        except Exception as exc:
            log.error("stt.failed", error=str(exc))
            raise RuntimeError(f"falha na transcrição: {exc}") from exc

        log.info(
            "stt.done",
            text=result.text[:120],
            latency_s=round(result.latency_s, 2),
            audio_s=round(result.duration_s, 2),
        )
        return result

    def unload(self) -> None:
        """Libera o modelo da memória/VRAM."""
        self._model = None
        self.ready = False


__all__ = ["ENGLISH_ONLY", "Transcription", "WhisperEngine"]
