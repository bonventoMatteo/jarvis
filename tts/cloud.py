"""
Vozes neurais online (qualidade muito acima do piper).

* **edge** — vozes neurais da Microsoft via `edge-tts` (grátis, sem chave).
  Padrão: `pt-BR-AntonioNeural` (masculina, grave). Outras boas em pt-BR:
  `pt-BR-FranciscaNeural`, `pt-BR-ThalitaMultilingualNeural`.
* **elevenlabs** — a voz mais realista disponível (API paga). Usa o modelo
  multilíngue e pede PCM cru, então não há decodificação.

As duas devolvem `(float32 mono, sample_rate)`; os efeitos de "IA de cinema"
(pedalboard) são aplicados depois, igual ao piper.
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request

import numpy as np
import structlog

from config import settings

log = structlog.get_logger(__name__)

#: Ajuste de velocidade (%) por mood, somado a `EDGE_RATE`.
_EDGE_MOOD_RATE: dict[str, int] = {"neutral": 0, "calm": -8, "urgent": 8, "confirm": 3}

#: Estabilidade da ElevenLabs por mood (mais baixo = mais expressivo).
_ELEVEN_MOOD_STABILITY: dict[str, float] = {"neutral": 0.5, "calm": 0.65, "urgent": 0.35, "confirm": 0.55}


def _percent(value: str) -> int:
    try:
        return int(value.strip().rstrip("%").replace("+", ""))
    except ValueError:
        return 0


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Decodifica MP3/WAV/OGG em memória para float32 mono (pedalboard)."""
    from pedalboard.io import AudioFile

    with AudioFile(io.BytesIO(data)) as handle:
        audio = handle.read(handle.frames)
        rate = int(handle.samplerate)
    mono = audio.mean(axis=0) if audio.ndim == 2 else audio
    return np.ascontiguousarray(mono, dtype=np.float32), rate


async def _edge_bytes(text: str, voice: str, rate: str, pitch: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            chunks.append(chunk["data"])
    return b"".join(chunks)


def synthesize_edge(text: str, mood: str = "neutral") -> tuple[np.ndarray, int]:
    """
    Sintetiza com Edge TTS. Roda num event loop próprio (chamado de uma
    thread de trabalho). Levanta exceção em falha de rede.
    """
    rate_pct = _percent(settings.edge_rate) + _EDGE_MOOD_RATE.get(mood, 0)
    rate = f"{rate_pct:+d}%"
    data = asyncio.run(
        asyncio.wait_for(_edge_bytes(text, settings.edge_voice, rate, settings.edge_pitch), timeout=15.0)
    )
    if not data:
        raise RuntimeError("o Edge TTS não devolveu áudio")
    return decode_audio(data)


def synthesize_elevenlabs(text: str, mood: str = "neutral") -> tuple[np.ndarray, int]:
    """Sintetiza com a ElevenLabs (PCM 16-bit, 22,05 kHz)."""
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY não configurada")
    sample_rate = 22050
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
        f"?output_format=pcm_{sample_rate}"
    )
    body = {
        "text": text,
        "model_id": settings.elevenlabs_model,
        "voice_settings": {
            "stability": _ELEVEN_MOOD_STABILITY.get(mood, 0.5),
            "similarity_boost": 0.8,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"xi-api-key": settings.elevenlabs_api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"ElevenLabs recusou ({exc.code}): {detail}") from exc
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sample_rate


def cloud_available(engine: str) -> bool:
    """O motor online está instalado/configurado?"""
    if engine == "edge":
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return True
    if engine == "elevenlabs":
        return bool(settings.elevenlabs_api_key)
    return False


__all__ = ["cloud_available", "decode_audio", "synthesize_edge", "synthesize_elevenlabs"]
