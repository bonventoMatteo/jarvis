"""
Lista os dispositivos de áudio e testa o microfone escolhido.

    python -m scripts.audio_devices            # lista
    python -m scripts.audio_devices --meter    # VU-meter do microfone por 10 s
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from audio.mic import list_devices
from config import settings


def meter(seconds: float = 10.0) -> None:
    """Mostra o nível do microfone em tempo real."""
    import sounddevice as sd

    print(f"Dispositivo de entrada: {settings.input_device if settings.input_device is not None else 'padrão'}")
    print("Fale ou bata palmas. Ctrl+C para sair.\n")

    def callback(indata, frames, time_info, status) -> None:
        peak = float(np.max(np.abs(indata[:, 0])))
        bar = "#" * int(min(1.0, peak) * 60)
        print(f"\r  pico {peak:6.3f} |{bar:<60}|", end="", flush=True)

    with sd.InputStream(
        samplerate=settings.sample_rate,
        channels=1,
        dtype="float32",
        blocksize=settings.block_size,
        device=settings.input_device,
        callback=callback,
    ):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(0.1)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico de áudio do JARVIS.")
    parser.add_argument("--meter", action="store_true", help="mostra o nível do microfone")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        for line in list_devices():
            print(line)
        if args.meter:
            print()
            meter(args.seconds)
    except KeyboardInterrupt:
        print()
    except Exception as exc:
        print(f"Erro de áudio: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
