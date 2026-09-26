"""
Calibração e teste interativo do detector de palmas.

    python -m scripts.clap_test

1. Fica 5 s em silêncio (calibra o ruído ambiente).
2. Bata palmas: cada candidato mostra pico, ataque, decaimento e fração de
   energia em 2–4 kHz, e o motivo de ter sido aceito ou rejeitado.
3. Duas palmas seguidas mostram "ATIVAR".

Use os números para ajustar CLAP_THRESHOLD_MULT, CLAP_BAND_RATIO e
CLAP_ATTACK_MS no .env.
"""
from __future__ import annotations

import sys
import time

import numpy as np

from audio.clap import ClapDetector
from config import settings


def main() -> int:
    import sounddevice as sd

    detector = ClapDetector(settings.sample_rate)
    block = settings.block_size

    print(f"Calibrando por {settings.calibration_seconds:.0f} s — fique em silêncio...")
    audio = sd.rec(
        int(settings.calibration_seconds * settings.sample_rate),
        samplerate=settings.sample_rate,
        channels=1,
        dtype="float32",
        device=settings.input_device,
    )
    sd.wait()
    floor = detector.calibrate(audio[:, 0])
    print(f"Ruído: {floor:.5f}   Limiar: {detector.threshold:.5f}\n")
    print("Bata palmas (Ctrl+C para sair).\n")

    try:
        with sd.InputStream(
            samplerate=settings.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=settings.input_device,
        ) as stream:
            while True:
                chunk, _overflow = stream.read(block)
                mono = np.ascontiguousarray(chunk[:, 0])
                fired = detector.process(mono, time.monotonic() * 1000.0)
                analysis = detector.last_analysis
                if analysis is not None and analysis.peak >= detector.threshold * 0.5:
                    mark = "PALMA " if analysis.is_clap else "  ·   "
                    print(
                        f"{mark} pico {analysis.peak:.3f} (lim {analysis.threshold:.3f})  "
                        f"ataque {analysis.attack_ms:5.1f}ms  decai {analysis.decay_ratio:.2f}  "
                        f"banda {analysis.band_ratio:.0%}  → {analysis.reason}"
                    )
                if fired:
                    print("\n  >>> ATIVAR (palma dupla) <<<\n")
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
