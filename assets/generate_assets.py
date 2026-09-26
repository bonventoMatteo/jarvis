"""
Gera os efeitos sonoros sci-fi do JARVIS em `assets/*.wav`.

Tudo é sintetizado com numpy (senoides + envelopes ADSR + ruído filtrado),
então o projeto não depende de nenhum arquivo binário externo.

Uso: `python -m assets.generate_assets [--force]`
"""
from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

from config import ASSETS_DIR

SR = 44100
"""Taxa de amostragem dos assets gerados."""

SOUND_NAMES: tuple[str, ...] = ("boot", "activate", "thinking", "error", "success", "confirm", "alarm")


# --------------------------------------------------------------------------- #
# Primitivas de síntese
# --------------------------------------------------------------------------- #
def _t(duration: float) -> np.ndarray:
    return np.linspace(0.0, duration, int(SR * duration), endpoint=False, dtype=np.float64)


def _adsr(
    n: int,
    attack: float = 0.02,
    decay: float = 0.1,
    sustain: float = 0.7,
    release: float = 0.3,
) -> np.ndarray:
    """Envelope ADSR normalizado com `n` amostras."""
    a = max(1, int(n * attack))
    d = max(1, int(n * decay))
    r = max(1, int(n * release))
    s = max(1, n - a - d - r)
    env = np.concatenate(
        [
            np.linspace(0.0, 1.0, a),
            np.linspace(1.0, sustain, d),
            np.full(s, sustain),
            np.linspace(sustain, 0.0, r),
        ]
    )
    if env.size < n:
        env = np.pad(env, (0, n - env.size), constant_values=0.0)
    return env[:n]


def _sweep(duration: float, f0: float, f1: float, *, log: bool = True) -> np.ndarray:
    """Varredura de frequência com fase integrada (sem clicks)."""
    t = _t(duration)
    if log and f0 > 0 and f1 > 0:
        freq = f0 * (f1 / f0) ** (t / max(duration, 1e-9))
    else:
        freq = f0 + (f1 - f0) * (t / max(duration, 1e-9))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return np.sin(phase)


def _tone(duration: float, freq: float, harmonics: tuple[float, ...] = (1.0,)) -> np.ndarray:
    t = _t(duration)
    out = np.zeros_like(t)
    for index, amp in enumerate(harmonics, start=1):
        out += amp * np.sin(2 * np.pi * freq * index * t)
    return out


def _noise(duration: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 1.0, int(SR * duration))


def _lowpass(signal: np.ndarray, cutoff: float) -> np.ndarray:
    """Filtro IIR de 1ª ordem (suficiente para colorir ruído)."""
    alpha = float(np.clip(cutoff / (SR / 2.0), 1e-4, 0.999))
    out = np.empty_like(signal)
    acc = 0.0
    for index, sample in enumerate(signal):
        acc += alpha * (sample - acc)
        out[index] = acc
    return out


def _normalize(signal: np.ndarray, peak: float = 0.85) -> np.ndarray:
    maximum = float(np.max(np.abs(signal))) or 1.0
    return signal / maximum * peak


def _write(path: Path, signal: np.ndarray) -> Path:
    """Grava um WAV mono 16-bit."""
    data = np.clip(_normalize(signal), -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm.tobytes())
    return path


# --------------------------------------------------------------------------- #
# Os sons
# --------------------------------------------------------------------------- #
def make_boot() -> np.ndarray:
    """Power-up: varredura ascendente + acorde de reatores + shimmer."""
    duration = 2.2
    sweep = _sweep(duration, 90.0, 1200.0) * _adsr(int(SR * duration), 0.35, 0.15, 0.75, 0.3)
    pad = (
        _tone(duration, 110.0, (1.0, 0.45, 0.2))
        + _tone(duration, 164.81, (0.8, 0.3))
        + _tone(duration, 220.0, (0.5, 0.2))
    ) * _adsr(int(SR * duration), 0.25, 0.2, 0.6, 0.35)
    air = _lowpass(_noise(duration), 5200.0) * _adsr(int(SR * duration), 0.5, 0.2, 0.25, 0.3) * 0.25
    ping = np.zeros(int(SR * duration))
    tail = _tone(0.6, 1760.0, (1.0, 0.3)) * _adsr(int(SR * 0.6), 0.01, 0.2, 0.25, 0.7)
    ping[-tail.size :] = tail * 0.5
    return sweep * 0.55 + pad * 0.5 + air + ping


def make_activate() -> np.ndarray:
    """"Bwoom": sub descendente + ping brilhante — o som de ativação."""
    duration = 0.85
    n = int(SR * duration)
    sub = _sweep(duration, 420.0, 58.0) * _adsr(n, 0.005, 0.25, 0.45, 0.6)
    body = _tone(duration, 196.0, (1.0, 0.5, 0.25)) * _adsr(n, 0.01, 0.3, 0.3, 0.5)
    ping = np.zeros(n)
    bell = _tone(0.35, 2400.0, (1.0, 0.35, 0.15)) * _adsr(int(SR * 0.35), 0.002, 0.15, 0.15, 0.8)
    ping[: bell.size] = bell * 0.45
    whoosh = _lowpass(_noise(duration, seed=11), 3000.0) * _adsr(n, 0.02, 0.2, 0.2, 0.7) * 0.3
    return sub * 0.75 + body * 0.35 + ping + whoosh


def make_thinking() -> np.ndarray:
    """Hum eletrônico grave, desenhado para loop perfeito (2.0 s)."""
    duration = 2.0
    t = _t(duration)
    # Frequências múltiplas de 1/duration => começo e fim em fase (loop limpo).
    base = 1.0 / duration
    partials = [(55.0, 1.0), (110.0, 0.55), (164.5, 0.3), (221.0, 0.18), (330.0, 0.08)]
    signal = np.zeros_like(t)
    for freq, amp in partials:
        snapped = round(freq / base) * base
        signal += amp * np.sin(2 * np.pi * snapped * t)
    # Modulação de amplitude lenta, também periódica no ciclo.
    lfo = 1.0 + 0.18 * np.sin(2 * np.pi * (2 * base) * t)
    shimmer = 0.05 * np.sin(2 * np.pi * (round(1500.0 / base) * base) * t)
    return (signal * lfo + shimmer) * 0.6


def make_error() -> np.ndarray:
    """Dois tons graves descendentes, levemente dissonantes."""
    segments: list[np.ndarray] = []
    for freq in (196.0, 146.83):
        piece = _tone(0.28, freq, (1.0, 0.6, 0.35, 0.15))
        piece += 0.25 * _tone(0.28, freq * 1.03, (1.0,))  # batimento
        segments.append(piece * _adsr(piece.size, 0.02, 0.2, 0.55, 0.4))
        segments.append(np.zeros(int(SR * 0.05)))
    return np.concatenate(segments)


def make_success() -> np.ndarray:
    """Dois bipes ascendentes e curtos."""
    segments: list[np.ndarray] = []
    for freq in (880.0, 1318.5):
        piece = _tone(0.11, freq, (1.0, 0.25))
        segments.append(piece * _adsr(piece.size, 0.02, 0.15, 0.6, 0.45))
        segments.append(np.zeros(int(SR * 0.03)))
    return np.concatenate(segments) * 0.8


def make_confirm() -> np.ndarray:
    """Bipe único e seco, usado antes de executar uma ação."""
    piece = _tone(0.09, 1046.5, (1.0, 0.3, 0.1))
    return piece * _adsr(piece.size, 0.02, 0.2, 0.5, 0.5)


def make_alarm() -> np.ndarray:
    """Três pulsos ascendentes repetidos duas vezes — timer/alarme disparado."""
    segments: list[np.ndarray] = []
    for _ in range(2):
        for freq in (880.0, 1174.66, 1567.98):
            piece = _tone(0.14, freq, (1.0, 0.4, 0.15))
            segments.append(piece * _adsr(piece.size, 0.02, 0.2, 0.7, 0.3))
            segments.append(np.zeros(int(SR * 0.04)))
        segments.append(np.zeros(int(SR * 0.25)))
    return np.concatenate(segments)


_GENERATORS = {
    "boot": make_boot,
    "activate": make_activate,
    "thinking": make_thinking,
    "error": make_error,
    "success": make_success,
    "confirm": make_confirm,
    "alarm": make_alarm,
}


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def ensure_assets(force: bool = False, directory: Path | None = None) -> list[Path]:
    """
    Garante que todos os WAVs existam.

    Args:
        force: regenera mesmo que o arquivo já exista.
        directory: destino (padrão `assets/`).

    Returns:
        Lista dos arquivos criados nesta chamada.
    """
    target = directory or ASSETS_DIR
    created: list[Path] = []
    for name, generator in _GENERATORS.items():
        path = target / f"{name}.wav"
        if path.exists() and not force:
            continue
        created.append(_write(path, generator()))
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera os efeitos sonoros do JARVIS.")
    parser.add_argument("--force", action="store_true", help="Regenera arquivos existentes.")
    args = parser.parse_args()
    created = ensure_assets(force=args.force)
    if created:
        for path in created:
            print(f"[+] {path}")
    else:
        print("Todos os assets já existem (use --force para regenerar).")


if __name__ == "__main__":
    main()
