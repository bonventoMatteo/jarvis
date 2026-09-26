"""
Cadeia de efeitos da voz do JARVIS (pedalboard).

O objetivo é tirar o "peso" natural da voz e dar a ela um timbre de
transmissão: passa-alta sutil, leve realce de agudos, compressão para manter
a fala sempre presente e um reverb curtíssimo que sugere uma sala metálica.
"""
from __future__ import annotations

import numpy as np
import structlog

from config import settings

log = structlog.get_logger(__name__)


class VoiceEffects:
    """Processa o áudio do TTS. Degrada para passthrough sem pedalboard."""

    def __init__(self, sample_rate: int = 22050) -> None:
        self.sample_rate = sample_rate
        self._board = None
        self.available = False
        self.enabled = settings.tts_effects

    # ------------------------------------------------------------------ #
    def build(self, sample_rate: int | None = None) -> None:
        """Monta (ou remonta) a cadeia para uma taxa de amostragem."""
        if sample_rate is not None:
            if self._board is not None and sample_rate == self.sample_rate:
                return
            self.sample_rate = sample_rate
        if not self.enabled:
            return

        try:
            from pedalboard import (
                Compressor,
                Gain,
                HighpassFilter,
                HighShelfFilter,
                Limiter,
                Pedalboard,
                Reverb,
            )

            self._board = Pedalboard(
                [
                    # 1. Tira o grave da voz — sensação de "rádio"/IA.
                    HighpassFilter(cutoff_frequency_hz=settings.tts_highpass_hz),
                    # 2. Realça a inteligibilidade nos agudos.
                    HighShelfFilter(cutoff_frequency_hz=3200.0, gain_db=3.0, q=0.7),
                    # 3. Nivela a dinâmica: nenhuma sílaba some.
                    Compressor(threshold_db=-18.0, ratio=3.2, attack_ms=4.0, release_ms=110.0),
                    # 4. Sala curta e metálica.
                    Reverb(
                        room_size=settings.tts_reverb_room,
                        damping=0.55,
                        wet_level=settings.tts_reverb_wet,
                        dry_level=1.0 - settings.tts_reverb_wet * 0.5,
                        width=0.9,
                    ),
                    Gain(gain_db=settings.tts_gain_db),
                    Limiter(threshold_db=-1.0, release_ms=80.0),
                ]
            )
            self.available = True
            log.info("tts.effects.ready", sample_rate=self.sample_rate)
        except Exception as exc:
            self._board = None
            self.available = False
            log.warning("tts.effects.unavailable", error=str(exc))

    # ------------------------------------------------------------------ #
    def process(self, audio: np.ndarray, sample_rate: int | None = None) -> np.ndarray:
        """
        Aplica a cadeia a um sinal float32 mono.

        Sem pedalboard (ou com efeitos desligados) devolve o sinal apenas
        normalizado, para o volume ficar consistente.
        """
        if audio.size == 0:
            return audio

        rate = sample_rate or self.sample_rate
        if self.enabled:
            self.build(rate)

        if self._board is not None:
            try:
                processed = self._board(audio.astype(np.float32), rate)
                audio = np.asarray(processed, dtype=np.float32).reshape(-1)
            except Exception as exc:  # pragma: no cover
                log.warning("tts.effects.failed", error=str(exc))

        peak = float(np.max(np.abs(audio))) or 1.0
        if peak > 0.99:
            audio = audio / peak * 0.99
        return np.clip(audio * settings.tts_volume, -1.0, 1.0).astype(np.float32)


__all__ = ["VoiceEffects"]
